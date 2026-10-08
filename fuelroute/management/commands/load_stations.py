"""``python manage.py load_stations [--file CSV] [--price-policy median|min] [--coords CSV | --no-coords]``

Loads the OPIS price file into ``FuelStation`` (replacing every row) and geocodes
each station offline: its exact position from ``data/station_coords.csv`` when the
file has it, its city center otherwise. Safe to run while the server is up: the
server notices the new data version on its next request. See
``services/station_loader.py``.
"""

from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuelroute.services.places import get_place_index
from fuelroute.services.station_loader import EXACT_SOURCES, PRICE_POLICIES, load_stations


def _by_source(report, sources) -> str:
    counts = [(source, n) for source, n in report.geocoded_by_source.items() if source in sources]
    return ", ".join(f"{source} {n:,}" for source, n in sorted(counts, key=lambda item: (-item[1], item[0])))


class Command(BaseCommand):
    help = "Load the fuel price CSV into the database and geocode every station offline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(settings.FUEL_PLANNER["FUEL_PRICES_FILE"]),
            help="Path to the OPIS fuel price CSV.",
        )
        parser.add_argument(
            "--price-policy",
            choices=PRICE_POLICIES,
            default=settings.FUEL_PLANNER["PRICE_POLICY"],
            help="Price of a station listed several times: the median (default) or the lowest quote.",
        )
        coords = parser.add_mutually_exclusive_group()
        coords.add_argument(
            "--coords",
            default=None,
            help=(
                "Exact station positions (built by scripts/build_station_coords.py). "
                f"Default: {settings.FUEL_PLANNER['STATION_COORDS_FILE']}, city centers if it is missing; "
                "a file given here must exist."
            ),
        )
        coords.add_argument(
            "--no-coords",
            action="store_true",
            help="Ignore the exact positions: every station at its city center.",
        )

    def handle(self, *args, **options):
        if options["no_coords"]:
            coords_path = None
        elif options["coords"] is not None:
            coords_path = Path(options["coords"])
            if not coords_path.is_file():
                # Asked for by name: a typo must not silently load city centers.
                raise CommandError(f"No exact positions file at {coords_path}.")
        else:
            coords_path = Path(settings.FUEL_PLANNER["STATION_COORDS_FILE"])
        report = load_stations(Path(options["file"]), get_place_index(), options["price_policy"], coords_path)

        self.stdout.write(f"Rows read:            {report.rows_read:,}")
        self.stdout.write(f"Invalid rows skipped: {report.rows_invalid:,}")
        self.stdout.write(f"Unique stations (ID): {report.unique_stations:,}  (price policy: {options['price_policy']})")
        self.stdout.write(f"Non-US skipped:       {report.non_us_skipped:,}")
        us_total = report.unique_stations - report.non_us_skipped
        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded:             {report.geocoded:,} / {us_total:,} "
                f"({100 * report.geocoded / max(us_total, 1):.1f}%)"
            )
        )
        city_sources = set(report.geocoded_by_source) - set(EXACT_SOURCES)
        self.stdout.write(f"  exact position:     {report.exact:,}  ({_by_source(report, EXACT_SOURCES) or 'none'})")
        self.stdout.write(
            f"  city center:        {report.geocoded - report.exact:,}  ({_by_source(report, city_sources) or 'none'})"
        )
        self.stdout.write(f"  homonym cities resolved from the address: {report.homonyms_resolved:,}")

        if report.coords_file:
            self.stdout.write(
                f"Exact positions file: {report.coords_file} ({report.coords_rows:,} rows; "
                f"{report.coords_invalid:,} invalid, {report.coords_unused:,} for stations not in the price file)"
            )
            if report.coords_rejected:
                ids = ", ".join(str(opis_id) for opis_id in report.coords_rejected[:10])
                self.stdout.write(
                    self.style.WARNING(
                        f"  ignored, too far from the station's city: {len(report.coords_rejected):,} (opis_id {ids})"
                    )
                )
        elif coords_path is not None:
            self.stdout.write(
                self.style.WARNING(f"No exact positions file at {coords_path}: every station at its city center.")
            )

        self.stdout.write(f"Ambiguous city (no city center): {len(report.ambiguous):,}")
        for (city, state), count in Counter(report.ambiguous).most_common(10):
            self.stdout.write(f"  - {city}, {state} ({count})")
        self.stdout.write(f"City not found (no city center): {len(report.unmatched):,}")
        for (city, state), count in Counter(report.unmatched).most_common(25):
            self.stdout.write(f"  - {city}, {state} ({count})")
        self.stdout.write(f"No city center, placed by their exact position: {report.exact_without_city:,}")
        self.stdout.write(self.style.WARNING(f"Left without coordinates: {report.not_geocoded:,}"))
