"""Takeout mbox import and the label/deletion sync. All mail here is made up."""

import zipfile

import pytest
from django.core.management import call_command

from core.models import LoaderState
from sources.google.loader import GmailLoader
from sources.google.models import GmailLabel, GmailMessage
from sources.google.parse import read_raw, write_raw
from sources.google.takeout import import_takeout, split_mbox, unpack

from .fakes import FakeClient, gid

pytestmark = pytest.mark.django_db

ID1, ID2, ID3, ID4, ID5 = 1852047678917183467, 1852047678917183468, 1852047678917183469, 1000, 1001


def msg(n, subject, body):
    return (f"From: Alice <alice{n}@example.com>\r\nTo: bob@example.org\r\nSubject: {subject}\r\n"
            f"Date: Tue, 03 Feb 2026 10:00:00 -0800\r\nMessage-ID: <t{n}@example.com>\r\n\r\n{body}")


ORIG1 = msg(1, "Escapes", "Line one\r\n>From the start\r\n>>From quoted\r\nbye\r\n")
ORIG2 = msg(2, "Folded", "From someone at the office, hello\r\nFrom 12@xxx not a date\r\n")
ORIG3 = msg(3, "Junk", "buy now\r\n")
ORIG4 = msg(4, "Already here", "hi\r\n")
ORIG5 = msg(5, "Binned", "old\r\n")


def mboxrd(body: str) -> str:
    return "".join(">" + line if line.lstrip(">").startswith("From ") else line
                   for line in body.splitlines(keepends=True))


MBOX = (
    f"From {ID1}@xxx Sat Dec 20 17:05:04 +0000 2025\r\n"
    f"X-GM-THRID: {ID1}\r\nX-Gmail-Labels: Inbox,Important,Opened\r\n"
    + mboxrd(ORIG1) + "\r\n"
    + f"From {ID2}@xxx Fri Dec  5 09:01:02 +0000 2025\r\n"
    f"X-GM-THRID: {ID1}\r\n"
    "X-Gmail-Labels: Archived,=?UTF-8?Q?Caf=C3=A9_Receipts?=,\r\n =?UTF-8?Q?Opened?=\r\n"
    + ORIG2 + "\r\n"  # body "From " lines left unescaped on purpose: they must not split
    + f"From {ID3}@xxx Mon Jan 01 00:00:00 +0000 2024\r\n"
    f"X-GM-THRID: {ID3}\r\nX-Gmail-Labels: Spam,Unread\r\n"
    + ORIG3 + "\r\n"
    + f"From {ID4}@xxx Mon Jan 01 00:00:00 +0000 2024\r\n"
    f"X-GM-THRID: {ID4}\r\nX-Gmail-Labels: Inbox\r\n"
    + ORIG4 + "\r\n"
    + f"From {ID5}@xxx Mon Jan 01 00:00:00 +0000 2024\r\n"
    f"X-GM-THRID: {ID5}\r\nX-Gmail-Labels: Trash\r\n"
    + ORIG5 + "\r\n"
).encode()

H1, H2, H3, H4, H5 = "19b3cb8d28f207eb", "19b3cb8d28f207ec", "19b3cb8d28f207ed", "3e8", "3e9"


@pytest.fixture
def raw_dir(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "raw"
    settings.RAW_DIR.mkdir()
    return settings.RAW_DIR


@pytest.fixture
def mbox(tmp_path):
    p = tmp_path / "All mail Including Spam and Trash.mbox"
    p.write_bytes(MBOX)
    return p


def run(path, root, **kw):
    return import_takeout(path, root, workers=0, out=lambda s: None, **kw)


def state(key):
    return LoaderState.get("gmail", key)


def test_split_ignores_body_from_lines():
    import io

    msgs = list(split_mbox(io.BytesIO(MBOX)))
    assert [m[0] for m in msgs] == [H1, H2, H3, H4, H5]
    assert msgs[1][1] == "Fri Dec  5 09:01:02 +0000 2025"


def test_unpack_strips_takeout_headers_and_unescapes():
    import io

    (_, _, lines), (_, _, lines2), *_ = split_mbox(io.BytesIO(MBOX))
    raw, thread, labels, ok = unpack(lines)
    assert raw == ORIG1.encode() and thread == H1 and ok
    assert labels == ["Inbox", "Important", "Opened"]
    raw, thread, labels, ok = unpack(lines2)
    assert raw == ORIG2.encode() and thread == H1
    assert labels == ["Archived", "Café Receipts", "Opened"]


def test_import_bare_mbox(raw_dir, mbox):
    r = run(mbox, raw_dir)
    assert r["imported"] == 3 and r["skipped_spam_trash"] == 2 and r["parse_errors"] == 0
    by = {m.gmail_id: m for m in GmailMessage.objects.all()}
    assert set(by) == {H1, H2, H4}
    m = by[H1]
    assert m.source == "takeout" and m.thread_id == H1 and m.label_ids == []
    assert m.takeout_labels == ["Inbox", "Important", "Opened"]
    assert m.internal_date.isoformat() == "2025-12-20T17:05:04+00:00"
    assert m.subject == "Escapes" and m.from_address == "alice1@example.com"
    assert m.size_estimate == len(ORIG1.encode()) and m.snippet == "" and m.history_id is None
    assert read_raw(raw_dir, m.raw_path) == ORIG1.encode()  # Takeout headers gone, unescaped
    assert read_raw(raw_dir, by[H2].raw_path) == ORIG2.encode()
    assert by[H2].thread_id == H1 and by[H2].takeout_labels == ["Archived", "Café Receipts", "Opened"]
    assert state("takeout_imported") == "3" and state("takeout_skipped_spam_trash") == "2"
    assert state("takeout_bytes_read") == str(len(MBOX))
    assert state("takeout_finished")


def test_spam_trash_kept_with_flag(raw_dir, mbox):
    run(mbox, raw_dir, include_spam_trash=True)
    assert GmailMessage.objects.count() == 5
    assert GmailMessage.objects.get(gmail_id=H3).takeout_labels == ["Spam", "Unread"]


def test_existing_skipped_and_rerun_idempotent(raw_dir, mbox):
    api = b"From: api@example.com\r\n\r\nfrom the API\r\n"
    GmailMessage.objects.create(gmail_id=H4, thread_id=H4, raw_path=write_raw(raw_dir, H4, api),
                                label_ids=["INBOX"])
    r = run(mbox, raw_dir)
    assert r["imported"] == 2 and r["skipped_existing"] == 1
    m4 = GmailMessage.objects.get(gmail_id=H4)
    assert m4.source == "api" and m4.label_ids == ["INBOX"] and read_raw(raw_dir, m4.raw_path) == api

    r = run(mbox, raw_dir)
    assert r["imported"] == 0 and r["skipped_existing"] == 3 and r["skipped_spam_trash"] == 2
    assert GmailMessage.objects.count() == 3
    assert state("takeout_skipped_existing") == "3"


def test_row_without_file_gets_its_file(raw_dir, mbox):
    GmailMessage.objects.create(gmail_id=H1, thread_id=H1, raw_path=f"gmail/19/{H1}.eml.gz")
    run(mbox, raw_dir)
    assert read_raw(raw_dir, f"gmail/19/{H1}.eml.gz") == ORIG1.encode()
    assert GmailMessage.objects.get(gmail_id=H1).source == "api"


def test_zip_limit_and_command(raw_dir, mbox, tmp_path):
    z = tmp_path / "takeout-001.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Takeout/Mail/user.json", "{}")
        zf.write(mbox, "Takeout/Mail/All mail Including Spam and Trash.mbox")
    r = run(z, raw_dir, limit=2)
    assert r["messages"] == 2 and r["imported"] == 2 and not r["complete"]
    assert set(GmailMessage.objects.values_list("gmail_id", flat=True)) == {H1, H2}
    assert state("takeout_finished") is None
    call_command("gmail_import_takeout", str(z), "--workers", "2", stdout=open("/dev/null", "w"))
    assert set(GmailMessage.objects.values_list("gmail_id", flat=True)) == {H1, H2, H4}
    assert read_raw(raw_dir, GmailMessage.objects.get(gmail_id=H4).raw_path) == ORIG4.encode()


# -- label sync ------------------------------------------------------------


def test_sync_labels(raw_dir):
    client = FakeClient(0)
    client.labels = [{"id": "INBOX", "name": "INBOX", "type": "system"},
                     {"id": "STARRED", "name": "STARRED", "type": "system"},
                     {"id": "Label_7", "name": "Receipts", "type": "user"}]
    client.messages = {
        gid(1): FakeClient.message(1, labels=("INBOX", "STARRED")),
        gid(2): FakeClient.message(2, labels=("Label_7",)),
        gid(3): FakeClient.message(3, labels=()),
        gid(9): FakeClient.message(9, labels=("INBOX",)),  # in Gmail, not stored
    }
    for n, src in ((1, "takeout"), (2, "takeout"), (3, "api"), (4, "takeout")):
        GmailMessage.objects.create(gmail_id=gid(n), thread_id=gid(n), raw_path="x", source=src,
                                    label_ids=["OLD"] if n == 3 else [])
    ld = GmailLoader()
    ld.client = client
    r = ld.sync_labels()
    by = {m.gmail_id: m for m in GmailMessage.objects.all()}
    assert by[gid(1)].label_ids == ["INBOX", "STARRED"]
    assert by[gid(2)].label_ids == ["Label_7"]
    assert by[gid(3)].label_ids == []
    assert by[gid(4)].deleted_at is not None  # gone from Gmail; row kept
    assert by[gid(1)].deleted_at is None
    assert r == dict(present=4, stored=4, labels_updated=3, deleted=1, undeleted=0, missing=1)
    assert state("missing_after_takeout") == "1"
    assert set(GmailLabel.objects.values_list("label_id", flat=True)) == {"INBOX", "STARRED", "Label_7"}
    assert all(c[1] for c in client.list_calls)  # spam/trash included everywhere
    assert (None, True, ("STARRED",)) in client.list_calls

    # Nothing changed: a rerun writes nothing.
    r = ld.sync_labels()
    assert r["labels_updated"] == 0 and r["deleted"] == 0


def test_sync_labels_respects_the_lock(raw_dir):
    import fcntl

    from core.loaders import Disabled

    with open(raw_dir / ".gmail.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        ld = GmailLoader()
        ld.client = FakeClient(1)
        with pytest.raises(Disabled, match="another gmail run"):
            ld.sync_labels()
