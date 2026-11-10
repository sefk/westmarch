"""Back up the SQLite database to the external drive and prune old copies.

    manage.py backup_db                  # to $WESTMARCH_RAW_DIR/backups
    manage.py backup_db --dest /some/dir

SQLite's online backup API, so the copy is consistent while loaders and
ingests keep writing (copying db.sqlite3 alone would miss the -wal). The
copy must pass PRAGMA quick_check before it is gzipped into place under
its real name; a half-written backup never has one. Keeps every backup
from the last DAILY_DAYS days plus the first of each ISO week for
WEEKLY_WEEKS weeks: most of the bulk (mail, messages, health) can be
rebuilt from the raw store anyway. Refuses to run when the drive is
missing, so an unmounted volume fails the job instead of filling the boot
disk. Each run is a `backup` row in the loader table. Run nightly by
`bin/load daily`.

No offsite copy: this database holds mail headers and message text, so
the Synology copy waits on a decision about encrypting it (issue #11).
"""

import gzip
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from core.models import LoaderRun

PREFIX = "westmarch-"
SUFFIX = ".sqlite3.gz"
DAILY_DAYS = 7
WEEKLY_WEEKS = 8


def backup_name(day: date) -> str:
    return f"{PREFIX}{day.isoformat()}{SUFFIX}"


def backup_day(name: str) -> date | None:
    if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
        return None
    try:
        return date.fromisoformat(name[len(PREFIX) : -len(SUFFIX)])
    except ValueError:
        return None


def to_prune(days: list[date], today: date) -> list[date]:
    """Older than DAILY_DAYS and not its ISO week's first, or older than
    WEEKLY_WEEKS weeks."""
    first_of_week: dict[tuple[int, int], date] = {}
    for d in sorted(days):
        first_of_week.setdefault(d.isocalendar()[:2], d)
    doomed = []
    for d in days:
        age = (today - d).days
        if age < DAILY_DAYS:
            continue
        if age < WEEKLY_WEEKS * 7 and first_of_week[d.isocalendar()[:2]] == d:
            continue
        doomed.append(d)
    return sorted(doomed)


def prune(dest: Path, today: date) -> list[str]:
    days = [d for p in dest.iterdir() if (d := backup_day(p.name))]
    removed = []
    for d in to_prune(days, today):
        (dest / backup_name(d)).unlink()
        removed.append(backup_name(d))
    return removed


def backup(dest: Path, today: date) -> Path:
    final = dest / backup_name(today)
    raw = dest / f".{final.name}.partial.sqlite3"
    gz = dest / f".{final.name}.partial"
    try:
        connection.ensure_connection()
        out = sqlite3.connect(raw)
        try:
            # One step: under WAL that reads a single snapshot without blocking
            # writers. A paged copy restarts whenever another process writes.
            connection.connection.backup(out)
            check = out.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            out.close()
        if check != "ok":
            raise CommandError(f"backup failed quick_check: {check}")
        with open(raw, "rb") as f_in, gzip.open(gz, "wb", compresslevel=4) as f_out:
            shutil.copyfileobj(f_in, f_out, 1 << 20)
        gz.replace(final)
    finally:
        raw.unlink(missing_ok=True)
        gz.unlink(missing_ok=True)
    return final


class Command(BaseCommand):
    help = "Back up db.sqlite3 to the external drive (daily for a week, weekly for two months)"

    def add_arguments(self, parser):
        parser.add_argument("--dest", type=Path, default=None)

    def handle(self, *args, dest, **opts):
        dest = dest or Path(settings.RAW_DIR) / "backups"
        run = LoaderRun.objects.create(loader="backup", started=timezone.now())
        try:
            if not dest.parent.is_dir():
                raise CommandError(f"backup volume missing (drive not mounted?): {dest.parent}")
            dest.mkdir(exist_ok=True)
            today = timezone.localdate()
            final = backup(dest, today)
            removed = prune(dest, today)
        except Exception as e:
            run.ok, run.finished, run.message = False, timezone.now(), f"{type(e).__name__}: {e}"[:500]
            run.save()
            raise
        size = final.stat().st_size / 1e9
        run.ok, run.finished, run.rows = True, timezone.now(), 1
        run.message = f"{final.name} {size:.2f} GB" + (f", pruned {len(removed)}" if removed else "")
        run.save()
        self.stdout.write(f"wrote {final} ({size:.2f} GB)")
        for name in removed:
            self.stdout.write(f"pruned {name}")
