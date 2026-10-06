"""``python manage.py benchmark``: measure the planner in this process and write data/benchmark.json.

No network while measuring: the New York -> Los Angeles route is read from
data/benchmark_route.json (created with ONE OSRM call the first time, or with
--refresh-route). See fuelroute/services/benchmark.py for what each scenario
measures. /api/stats publishes the file under "benchmark".
"""

from django.core.management.base import BaseCommand, CommandError

from fuelroute.services.benchmark import SCENARIOS, run_benchmark, write_benchmark
from fuelroute.services.errors import PlannerError


class Command(BaseCommand):
    help = "Measure cached plans, what-if re-plans and the planner itself (no network); write data/benchmark.json."

    def add_arguments(self, parser):
        parser.add_argument("--iterations", type=int, default=300, help="Requests for the plan-cache scenario.")
        parser.add_argument("--replans", type=int, default=100, help="Iterations of the re-planning scenarios.")
        parser.add_argument(
            "--refresh-route", action="store_true", help="Ask OSRM for the route again (one external call)."
        )
        parser.add_argument("--no-write", action="store_true", help="Print the results without writing the file.")

    def handle(self, *args, **options):
        if options["iterations"] < 1 or options["replans"] < 1:
            raise CommandError("--iterations and --replans must be at least 1.")
        try:
            document = run_benchmark(
                iterations=options["iterations"],
                replans=options["replans"],
                refresh_route=options["refresh_route"],
                log=lambda text: self.stdout.write(text),
            )
        except PlannerError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
        trip = document["trip"]
        self.stdout.write(
            f"\n{trip['start']} -> {trip['finish']}: {trip['distance_miles']} mi, {trip['corridor_candidates']} "
            f"corridor stations of {document['stations']}, {document['external_api_calls']} external calls "
            f"({document['osrm_answers_replayed']} saved OSRM answers replayed for the new trips)"
        )
        self.stdout.write(f"{'scenario':<22}{'p50 ms':>10}{'p95 ms':>10}{'req/s':>10}")
        for name in SCENARIOS:
            block = document["results"][name]
            self.stdout.write(
                f"{name:<22}{block['p50_ms']:>10.2f}{block['p95_ms']:>10.2f}{block['requests_per_second']:>10.1f}"
            )
        memory = document["memory"]
        if memory["rss_mb"] is not None:
            self.stdout.write(f"memory ({memory['kind']} RSS): {memory['rss_mb']} MB")
        if not options["no_write"]:
            path = write_benchmark(document)
            self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
