"""Seed the Gmail mirror from a Google Takeout zip or mbox (no API quota).

Prints counts, sizes and rates only, never message content.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.loaders import Disabled
from sources.google.loader import gmail_root
from sources.google.takeout import default_workers, import_takeout


class Command(BaseCommand):
    help = "Import a Google Takeout mail export (.zip or .mbox) into the Gmail mirror."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Takeout zip (the .mbox member is streamed) or a bare .mbox")
        parser.add_argument("--include-spam-trash", action="store_true",
                            help="also import messages labelled Spam or Trash")
        parser.add_argument("--limit", type=int, help="stop after N messages (counting skipped ones)")
        parser.add_argument("--workers", type=int, default=default_workers(),
                            help="processes for gzip+write+parse (0 = inline; default %(default)s)")

    def handle(self, *args, path, include_spam_trash, limit, workers, **opts):
        p = Path(path).expanduser()
        if not p.is_file():
            raise CommandError(f"no such file: {p}")
        try:
            root = gmail_root()
        except Disabled as exc:
            raise CommandError(str(exc)) from None
        try:
            import_takeout(p, root, include_spam_trash=include_spam_trash, limit=limit,
                           workers=workers, out=self.stdout.write)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
