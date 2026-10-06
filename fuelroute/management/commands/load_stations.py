from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from fuelroute.services.places import get_place_index
from fuelroute.services.station_loader import load_stations


class Command(BaseCommand):
    help = "Load the fuel price CSV into the database and geocode every station offline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(settings.FUEL_PLANNER["FUEL_PRICES_FILE"]),
            help="Path to the OPIS fuel price CSV.",
        )

    def handle(self, *args, **options):
        places = get_place_index()
        report = load_stations(Path(options["file"]), places)

        self.stdout.write(f"Rows read:            {report.rows_read:,}")
        self.stdout.write(f"Invalid rows skipped: {report.rows_invalid:,}")
        self.stdout.write(f"Unique stations (ID): {report.unique_stations:,}")
        self.stdout.write(f"Non-US skipped:       {report.non_us_skipped:,}")
        us_total = report.unique_stations - report.non_us_skipped
        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded:             {report.geocoded:,} / {us_total:,} "
                f"({100 * report.geocoded / max(us_total, 1):.1f}%) {report.geocoded_by_source}"
            )
        )
        self.stdout.write(self.style.WARNING(f"Not geocoded:         {len(report.unmatched):,}"))
        for (city, state), count in Counter(report.unmatched).most_common(25):
            self.stdout.write(f"  - {city}, {state} ({count})")
