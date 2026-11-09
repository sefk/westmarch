"""Run the mailindex loader by hand, e.g. the first backfill:

    uv run manage.py mailindex --budget-minutes 0           # until done
    uv run manage.py mailindex --no-ocr --budget-minutes 0  # text only
"""

from django.core.management.base import BaseCommand

from core.loaders import run_loader
from core.models import LoaderState
from mailindex.loader import MailIndexLoader


class Command(BaseCommand):
    help = "Index Gmail bodies and attachments (0 minutes = no time limit)."

    def add_arguments(self, parser):
        parser.add_argument("--budget-minutes", type=float, default=None)
        parser.add_argument("--workers", type=int, default=None)
        parser.add_argument("--no-ocr", action="store_true")

    def handle(self, *args, budget_minutes, workers, no_ocr, **opts):
        budget = None if budget_minutes is None else int(budget_minutes * 60)
        run = run_loader(MailIndexLoader(budget_seconds=budget, workers=workers, ocr=not no_ocr,
                                         enabled=True))
        self.stdout.write(run.message.splitlines()[0])
        for key in ("messages", "attachments", "blobs_new", "ocr_done", "ocr_failed", "ocr_stopped",
                    "messages_indexed", "messages_left", "ocr_left"):
            if (v := LoaderState.get("mailindex", key)) is not None:
                self.stdout.write(f"  {key}: {v}")
