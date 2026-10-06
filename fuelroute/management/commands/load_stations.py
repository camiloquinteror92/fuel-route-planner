"""``python manage.py load_stations [--file CSV] [--price-policy median|min]``

Loads the OPIS price file into ``FuelStation`` (replacing every row) and geocodes
each station offline. Safe to run while the server is up: the server notices the
new data version on its next request. See ``services/station_loader.py``.
"""

from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from fuelroute.services.places import get_place_index
from fuelroute.services.station_loader import PRICE_POLICIES, load_stations


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

    def handle(self, *args, **options):
        report = load_stations(Path(options["file"]), get_place_index(), options["price_policy"])

        self.stdout.write(f"Rows read:            {report.rows_read:,}")
        self.stdout.write(f"Invalid rows skipped: {report.rows_invalid:,}")
        self.stdout.write(f"Unique stations (ID): {report.unique_stations:,}  (price policy: {options['price_policy']})")
        self.stdout.write(f"Non-US skipped:       {report.non_us_skipped:,}")
        us_total = report.unique_stations - report.non_us_skipped
        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded:             {report.geocoded:,} / {us_total:,} "
                f"({100 * report.geocoded / max(us_total, 1):.1f}%) {report.geocoded_by_source}"
            )
        )
        self.stdout.write(f"  homonym cities resolved from the address: {report.homonyms_resolved:,}")
        self.stdout.write(
            self.style.WARNING(f"Ambiguous city (left without coordinates): {len(report.ambiguous):,}")
        )
        for (city, state), count in Counter(report.ambiguous).most_common(10):
            self.stdout.write(f"  - {city}, {state} ({count})")
        self.stdout.write(self.style.WARNING(f"City not found:       {len(report.unmatched):,}"))
        for (city, state), count in Counter(report.unmatched).most_common(25):
            self.stdout.write(f"  - {city}, {state} ({count})")
