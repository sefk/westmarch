import gzip
import sqlite3
from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.management.commands.backup_db import backup_name, to_prune
from core.models import LoaderRun

pytestmark = pytest.mark.django_db(transaction=True)


def test_to_prune():
    today = date(2026, 11, 9)  # a Monday
    days = [date(2026, 11, d) for d in range(1, 10)] + [date(2026, 10, 20), date(2026, 10, 21), date(2026, 8, 3)]
    # Nov 3-9: within the week. Nov 2 (Mon) and Nov 1 (Sun) each open an ISO week, as does Oct 20.
    # Oct 21 is second in its week; Aug 3 is older than eight weeks.
    assert to_prune(days, today) == [date(2026, 8, 3), date(2026, 10, 21)]


def test_backup_roundtrip(tmp_path, settings):
    settings.RAW_DIR = tmp_path
    stale = tmp_path / "backups" / backup_name(date(2020, 1, 6))
    stale.parent.mkdir()
    stale.write_bytes(b"old")
    call_command("backup_db")
    files = list((tmp_path / "backups").glob("westmarch-*.sqlite3.gz"))
    assert len(files) == 1 and not stale.exists()
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(gzip.decompress(files[0].read_bytes()))
    con = sqlite3.connect(restored)
    assert con.execute("pragma quick_check").fetchone()[0] == "ok"
    assert con.execute("select count(*) from sqlite_master where name='core_loaderrun'").fetchone()[0] == 1
    run = LoaderRun.objects.filter(loader="backup").latest("started")
    assert run.ok and "GB" in run.message and "pruned 1" in run.message


def test_refuses_without_volume(tmp_path, settings):
    settings.RAW_DIR = tmp_path / "not-mounted" / "westmarch"
    with pytest.raises(CommandError):
        call_command("backup_db")
    assert LoaderRun.objects.get(loader="backup").ok is False
