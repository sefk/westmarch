"""Search indexed mail: uv run manage.py mailsearch quarterly invoice"""

from django.core.management.base import BaseCommand

from mailindex.search import search


class Command(BaseCommand):
    help = "Full-text search over mail bodies and attachments."

    def add_arguments(self, parser):
        parser.add_argument("words", nargs="+")
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, words, limit, **opts):
        hits = search(" ".join(words), limit)
        for h in hits["bodies"]:
            self.stdout.write(f"{str(h['date'] or '')[:10]} {h['gmail_id']} {h['subject'][:60]}\n    {h['snippet']}")
        for h in hits["attachments"]:
            self.stdout.write(f"{str(h['date'] or '')[:10]} {h['gmail_id']} [attachment {h['part']}: {h['filename']}]"
                              f" {h['subject'][:50]}\n    {h['snippet']}")
