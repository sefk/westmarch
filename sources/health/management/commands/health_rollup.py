from datetime import date

from django.core.management.base import BaseCommand

from sources.health.rollup import all_days, rebuild


class Command(BaseCommand):
    help = "Rebuild HealthDay rollups: everything (--full) or one day (--day YYYY-MM-DD)."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true")
        parser.add_argument("--day", type=date.fromisoformat, action="append", default=[])

    def handle(self, *args, full, day, **opts):
        days = all_days() if full else set(day)
        n = rebuild(days)
        self.stdout.write(f"{n} days rebuilt (of {len(days)} considered)")
