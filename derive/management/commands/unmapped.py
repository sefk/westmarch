"""Print the names each source uses that map to no project (as recorded
by the last derive run), to add as aliases in ~/src/AGENTS.md."""

from django.core.management.base import BaseCommand

from core.models import LoaderState
from derive.mapping import SOURCES


class Command(BaseCommand):
    help = "List unmapped project names per source (from the last derive run)."

    def handle(self, *args, **opts):
        for source in SOURCES:
            value = LoaderState.get("derive", f"unmapped:{source}")
            names = [n for n in (value or "").splitlines() if n]
            note = "" if value is not None else " (derive has not run)"
            self.stdout.write(f"{source}: {len(names)} unmapped{note}")
            for n in names:
                self.stdout.write(f"  {n}")
