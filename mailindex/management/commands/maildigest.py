"""Run the maildigest loader by hand, or compare two models' digests:

    uv run manage.py maildigest --limit 20                # trial: 20 threads
    uv run manage.py maildigest --budget-minutes 0        # until done
    uv run manage.py maildigest --thread 18c2f...         # one thread, printed
    uv run manage.py maildigest --compare MODEL_A MODEL_B --sample 10
"""

import json
import random
from statistics import median

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count, Sum

from core.loaders import run_loader
from core.models import LoaderState
from mailindex.digest import MailDigestLoader
from mailindex.models import ThreadDigest


class Command(BaseCommand):
    help = "Summarise threads I wrote in with the local model (0 minutes = no time limit)."

    def add_arguments(self, parser):
        parser.add_argument("--budget-minutes", type=float, default=None)
        parser.add_argument("--model", default=None, help="digest model (default MAILDIGEST_MODEL)")
        parser.add_argument("--limit", type=int, default=None, help="at most this many threads")
        parser.add_argument("--thread", action="append", default=[], help="digest this thread and print it")
        parser.add_argument("--compare", nargs=2, metavar=("MODEL_A", "MODEL_B"))
        parser.add_argument("--sample", type=int, default=10, help="threads to show side by side with --compare")
        parser.add_argument("--stats", action="store_true", help="per-model counts and speed")

    def handle(self, *args, budget_minutes, model, limit, thread, compare, sample, stats, **opts):
        if stats:
            return self.stats()
        if compare:
            return self.compare(*compare, sample)
        budget = None if budget_minutes is None else int(budget_minutes * 60)
        loader = MailDigestLoader(budget_seconds=budget, model=model, limit=limit, threads=thread or None, enabled=True)
        run = run_loader(loader)
        self.stdout.write(run.message.splitlines()[0])
        for row in LoaderState.objects.filter(loader="maildigest").order_by("key"):
            self.stdout.write(f"  {row.key}: {row.value}")
        for t in thread:
            d = ThreadDigest.objects.filter(thread_id=t, model=loader.model).first()
            if d:
                self.stdout.write(f"\n{t} ({d.seconds:.1f}s, {d.prompt_tokens} in / {d.completion_tokens} out)")
                self.stdout.write(d.error or json.dumps(d.digest, indent=2, ensure_ascii=False))

    def stats(self):
        rows = (ThreadDigest.objects.values("model").annotate(
            n=Count("id"), secs=Avg("seconds"), tin=Sum("prompt_tokens"), tout=Sum("completion_tokens"))
            .order_by("model"))
        for r in rows:
            errors = ThreadDigest.objects.filter(model=r["model"]).exclude(error="").count()
            self.stdout.write(f"{r['model']}: {r['n']} threads ({errors} failed), {r['secs'] or 0:.1f}s each,"
                              f" {r['tin'] or 0} tokens in, {r['tout'] or 0} out")

    def compare(self, a, b, sample):
        da = {d.thread_id: d for d in ThreadDigest.objects.filter(model=a, error="")}
        db = {d.thread_id: d for d in ThreadDigest.objects.filter(model=b, error="")}
        both = sorted(set(da) & set(db))
        self.stdout.write(f"{a}: {len(da)} digests, {b}: {len(db)}, both: {len(both)}")
        if not both:
            return
        for name, ds in ((a, da), (b, db)):
            secs = [ds[t].seconds for t in both]
            self.stdout.write(f"  {name}: median {median(secs):.1f}s per thread on the shared threads")
        same_topic = sum(da[t].topic == db[t].topic for t in both)
        self.stdout.write(f"  same topic on {same_topic}/{len(both)} threads")
        for t in random.Random(0).sample(both, min(sample, len(both))):
            self.stdout.write(f"\n=== {t}")
            for name, d in ((a, da[t]), (b, db[t])):
                j = d.digest
                self.stdout.write(f"  [{name}] ({d.topic}) {d.summary}")
                self.stdout.write(f"      people: {', '.join(j.get('people', []))} | keywords: {', '.join(j.get('keywords', []))}")
                if j.get("open_question"):
                    self.stdout.write(f"      open: {j['open_question']}")
