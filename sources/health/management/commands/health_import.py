from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from sources.health.ingest import ingest_body


class Command(BaseCommand):
    help = "Import a saved Health Auto Export payload (.json or .json.gz)."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+")
        parser.add_argument("--store-raw", action="store_true", help="also copy into the raw archive")

    def handle(self, *args, files, store_raw, **opts):
        for f in files:
            p = Path(f).expanduser()
            if not p.is_file():
                raise CommandError(f"no such file: {p}")
            try:
                r = ingest_body(p.read_bytes(), store_raw=store_raw, raw_path="" if store_raw else str(p.resolve()))
            except ValueError as e:
                raise CommandError(f"{p}: not valid JSON ({e})")
            self.stdout.write(f"{p.name}: {r['metrics']} points, {r['workouts']} workouts, {r['route_points']} route points, {r['skipped']} skipped")
