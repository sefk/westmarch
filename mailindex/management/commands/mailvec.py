"""Run the mailvec loader by hand, e.g. the first backfill:

    uv run manage.py mailvec --limit 200              # trial: 200 items per pass
    uv run manage.py mailvec --budget-minutes 0       # until done
    uv run manage.py mailvec --scope all --budget-minutes 0
"""

from django.core.management.base import BaseCommand

from core.loaders import run_loader
from core.models import LoaderState
from mailindex.vecloader import MailVecLoader


class Command(BaseCommand):
    help = "Embed personal mail, attachments and thread digests (0 minutes = no time limit)."

    def add_arguments(self, parser):
        parser.add_argument("--budget-minutes", type=float, default=None)
        parser.add_argument("--model", default=None, help="embedding model (default MAILVEC_MODEL)")
        parser.add_argument("--scope", choices=["personal", "all"], default=None)
        parser.add_argument("--limit", type=int, default=None, help="at most this many items per pass")

    def handle(self, *args, budget_minutes, model, scope, limit, **opts):
        budget = None if budget_minutes is None else int(budget_minutes * 60)
        run = run_loader(MailVecLoader(budget_seconds=budget, model=model, scope=scope, limit=limit, enabled=True))
        self.stdout.write(run.message.splitlines()[0])
        for row in LoaderState.objects.filter(loader="mailvec").order_by("key"):
            self.stdout.write(f"  {row.key}: {row.value}")
