"""Search indexed mail:

    uv run manage.py mailsearch quarterly invoice           # keywords
    uv run manage.py mailsearch --meaning the roof leak     # keywords and meaning, by thread
"""

from django.core.management.base import BaseCommand

from mailindex.search import hybrid, search
from sources.google.models import GmailMessage


class Command(BaseCommand):
    help = "Search mail bodies and attachments; --meaning adds the embedding index."

    def add_arguments(self, parser):
        parser.add_argument("words", nargs="+")
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--meaning", action="store_true", help="rank threads by keywords and meaning together")

    def handle(self, *args, words, limit, meaning, **opts):
        if meaning:
            return self.hybrid(" ".join(words), limit)
        hits = search(" ".join(words), limit)
        for h in hits["bodies"]:
            self.stdout.write(f"{str(h['date'] or '')[:10]} {h['gmail_id']} {h['subject'][:60]}\n    {h['snippet']}")
        for h in hits["attachments"]:
            self.stdout.write(f"{str(h['date'] or '')[:10]} {h['gmail_id']} [attachment {h['part']}: {h['filename']}]"
                              f" {h['subject'][:50]}\n    {h['snippet']}")

    def hybrid(self, text, limit):
        for h in hybrid(text, limit):
            last = GmailMessage.objects.filter(thread_id=h["thread_id"]).order_by("-internal_date").first()
            when = str(last.internal_date)[:10] if last else ""
            subject = last.subject[:60] if last else ""
            snippet = " ".join(h["snippet"].split())[:200]
            self.stdout.write(f"{when} {h['thread_id']} [{'+'.join(h['how'])}] {subject}\n    {snippet}")
