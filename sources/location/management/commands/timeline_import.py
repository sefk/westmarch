from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import LoaderRun
from sources.location.timeline import import_path


class Command(BaseCommand):
    help = ("Import Google location history: an on-device Timeline export (iOS or Android), "
            "or old Takeout Records.json / Semantic Location History. Takes .json, .json.gz, "
            "a directory, or a Takeout .zip.")

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+")

    def handle(self, *args, paths, **opts):
        for raw in paths:
            p = Path(raw).expanduser()
            if not p.exists():
                raise CommandError(f"no such file: {p}")
            run = LoaderRun.objects.create(loader="timeline", started=timezone.now())
            try:
                counts = import_path(p, log=self.stdout.write)
            except Exception as e:
                run.ok, run.finished, run.message = False, timezone.now(), f"{p.name}: {type(e).__name__}: {e}"[:500]
                run.save()
                raise
            run.ok, run.finished, run.rows = True, timezone.now(), sum(counts.values())
            run.message = f"{p.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items())
            run.save()
            self.stdout.write(run.message)
