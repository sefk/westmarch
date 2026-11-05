"""Refresh Gmail label ids and deletions for every stored message by listing
ids only (cheap in quota). Run after a Takeout import. Prints counts only."""

from django.core.management.base import BaseCommand, CommandError

from core.loaders import Disabled
from sources.google.loader import GmailLoader


class Command(BaseCommand):
    help = "Set label_ids and deleted_at on stored Gmail rows from the API's id lists."

    def handle(self, *args, **opts):
        try:
            r = GmailLoader().sync_labels()
        except Disabled as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            f"{r['present']:,} messages in Gmail, {r['stored']:,} stored; "
            f"labels updated on {r['labels_updated']:,}, {r['deleted']:,} marked deleted, "
            f"{r['undeleted']:,} undeleted, {r['missing']:,} in Gmail but not stored (the backfill fetches those)")
