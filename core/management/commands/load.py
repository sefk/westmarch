"""Run loaders.

    manage.py load                  # list loaders and their last run
    manage.py load --all            # every loader, settings order
    manage.py load agentsview github
    manage.py load --all --full     # ignore incremental cursors
    manage.py load --all --cadence hourly
    manage.py load --all --cadence session   # loaders that need the login Keychain
"""

from django.core.management.base import BaseCommand, CommandError

from core import freshness
from core.loaders import registry, run_loader
from core.models import LoaderRun


class Command(BaseCommand):
    help = "Run one or more loaders, or list them."

    def add_arguments(self, parser):
        parser.add_argument("names", nargs="*")
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--full", action="store_true", help="ignore incremental cursors")
        parser.add_argument("--cadence", choices=["hourly", "daily", "session"], help="with --all: only this cadence")

    def handle(self, *args, **opts):
        loaders = registry()
        if not opts["names"] and not opts["all"]:
            rows = [(name, loader.cadence) for name, loader in loaders.items()]
            rows += [(name, "push") for name in freshness.STALE_AFTER if name not in loaders]  # the phone runs these
            for name, cadence in rows:
                last = LoaderRun.objects.filter(loader=name).first()
                late = freshness.stale(name)
                self.stdout.write(f"{name:18} {cadence:7} {last or 'never run'}"
                                  + (f"  STALE: {late.text}" if late else ""))
            return
        if opts["all"]:
            chosen = [l for l in loaders.values() if not opts["cadence"] or l.cadence == opts["cadence"]]
        else:
            missing = [n for n in opts["names"] if n not in loaders]
            if missing:
                raise CommandError(f"unknown loader(s): {', '.join(missing)}; known: {', '.join(loaders)}")
            chosen = [loaders[n] for n in opts["names"]]
        failed = 0
        for loader in chosen:
            run = run_loader(loader, full=opts["full"])
            self.stdout.write(f"{loader.name:18} {'ok' if run.ok else 'FAILED':6} {run.message.splitlines()[0]}")
            failed += not run.ok
        if failed:
            raise CommandError(f"{failed} loader(s) failed")
