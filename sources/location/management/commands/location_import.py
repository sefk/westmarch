from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from sources.location.ingest import ingest_body


class Command(BaseCommand):
    help = "Import saved Overland payloads (.json or .json.gz)."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+")

    def handle(self, *args, files, **opts):
        for f in files:
            p = Path(f).expanduser()
            if not p.is_file():
                raise CommandError(f"no such file: {p}")
            try:
                r = ingest_body(p.read_bytes(), store_raw=False, raw_path=str(p.resolve()))
            except ValueError as e:
                raise CommandError(f"{p}: not valid JSON ({e})")
            self.stdout.write(f"{p.name}: {r['points']} points, {r['skipped']} skipped")
