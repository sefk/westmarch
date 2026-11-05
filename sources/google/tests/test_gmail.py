"""Gmail loader against FakeClient; raw files go to tmp_path."""

import pytest

from core.loaders import Disabled
from core.models import LoaderState
from sources.google.loader import GmailLoader
from sources.google.models import GmailLabel, GmailMessage
from sources.google.parse import read_raw

from .fakes import Clock, FakeClient, eml, gid

pytestmark = pytest.mark.django_db


@pytest.fixture
def raw_dir(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "raw"
    return settings.RAW_DIR


def state(key):
    return LoaderState.get("gmail", key)


def loader(client, budget=60, clock=None):
    ld = GmailLoader()
    ld.client = client
    ld.budget_seconds = budget
    if clock:
        ld.clock = clock
    return ld


def test_disabled_without_token(settings, raw_dir, tmp_path):
    settings.GOOGLE_TOKEN_FILE = tmp_path / "nope.json"
    with pytest.raises(Disabled, match="manage.py google_auth"):
        GmailLoader().load()


def test_disabled_without_volume(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "not-mounted" / "life-metrics"
    with pytest.raises(Disabled, match="not mounted"):
        loader(FakeClient(2)).load()
    assert not settings.RAW_DIR.exists()


def test_backfill_stores_rows_and_raw(raw_dir):
    client = FakeClient(5, page_size=2)
    client.messages[gid(4)] = FakeClient.message(4, labels=("INBOX", "Label_1"), raw=eml(4, attach=True))
    assert loader(client).load() == 5
    assert state("backfill_done") == "1"
    assert state("history_id") == "1000"
    assert state("messages_total") == "5"
    assert state("messages_stored") == "5"
    assert state("parse_errors") == "0"
    m = GmailMessage.objects.get(gmail_id=gid(4))
    assert m.raw_path == f"gmail/00/{gid(4)}.eml.gz"
    assert read_raw(raw_dir, m.raw_path) == eml(4, attach=True)
    assert m.label_ids == ["INBOX", "Label_1"]
    assert m.has_attachments and m.attachment_count == 2
    assert m.from_address == "alice4@example.com" and m.subject == "Test message 4"
    assert m.internal_date.year == 2026 and m.history_id == 904
    assert set(GmailLabel.objects.values_list("name", flat=True)) == {"INBOX", "Receipts"}


def test_backfill_resumes_across_time_boxed_runs(raw_dir):
    clock = Clock()
    client = FakeClient(230, page_size=100, clock=clock, tick=10)  # each 50-id batch "takes" 10s
    # Run 1, budget 25s: page 1 (two batches, t=20), page 2's first batch (t=30), then stop
    # mid-page: page 2's token isn't saved.
    assert loader(client, budget=25, clock=clock).load() == 150
    assert state("backfill_done") is None
    assert state("backfill_page_token") == "100"
    assert state("messages_stored") == "150"
    client.history_id = "2000"  # mail arrives during the backfill
    # Run 2 re-lists page 2, fetches only its missing half (t=10), then page 3 (t=20).
    clock.t = 1000
    assert loader(client, budget=25, clock=clock).load() == 80
    assert state("backfill_done") == "1"
    assert state("backfill_page_token") == ""
    assert state("history_id") == "1000"  # recorded at the START of the backfill
    assert GmailMessage.objects.count() == 230
    assert sorted(client.fetched) == sorted(client.messages)  # nothing fetched twice


def test_corrupt_message_counted_not_fatal(raw_dir):
    client = FakeClient(2)
    client.messages[gid(2)]["raw"] = b"From: <<<\r\nDate: someday\r\n\r\nx"
    assert loader(client).load() == 2
    assert state("parse_errors") == "1"
    assert GmailMessage.objects.get(gmail_id=gid(2)).parse_error


def test_incremental_history(raw_dir):
    client = FakeClient(3)
    loader(client).load()
    client.fetched.clear()
    client.messages[gid(9)] = FakeClient.message(9, labels=("INBOX", "IMPORTANT"))
    client.messages[gid(10)] = FakeClient.message(10, labels=("DRAFT",))
    client.history_id = "1500"
    client.history = [
        {"id": "1001", "messagesAdded": [{"message": {"id": gid(9), "labelIds": ["INBOX"]}}]},
        {"id": "1002", "messagesAdded": [{"message": {"id": gid(10), "labelIds": ["DRAFT"]}}]},
        {"id": "1003", "labelsAdded": [{"message": {"id": gid(1), "labelIds": ["INBOX", "STARRED"]},
                                         "labelIds": ["STARRED"]}]},
        {"id": "1004", "labelsRemoved": [{"message": {"id": gid(2), "labelIds": []}, "labelIds": ["INBOX"]}]},
        {"id": "1005", "messagesDeleted": [{"message": {"id": gid(3)}}]},
        {"id": "1006", "labelsAdded": [{"message": {"id": gid(9), "labelIds": ["INBOX", "IMPORTANT"]}}]},
    ]
    rows = loader(client).load()
    assert client.fetched == [gid(9)]  # the draft is skipped
    assert rows == 4  # one new, two label changes, one delete
    by = {m.gmail_id: m for m in GmailMessage.objects.all()}
    assert by[gid(1)].label_ids == ["INBOX", "STARRED"]
    assert by[gid(2)].label_ids == []
    assert by[gid(3)].deleted_at is not None
    assert (raw_dir / by[gid(3)].raw_path).exists()  # a backup keeps the file
    assert gid(10) not in by
    assert state("history_id") == "1500"

    # Replaying the same history is a no-op.
    client.history_id = "1500"
    assert loader(client).load() == 0


def test_history_too_old_reconciles(raw_dir):
    client = FakeClient(4)
    loader(client).load()
    del client.messages[gid(1)]  # deleted while we weren't looking
    client.messages[gid(8)] = FakeClient.message(8)
    client.history = 404
    client.history_id = "3000"
    loader(client).load()
    assert GmailMessage.objects.get(gmail_id=gid(1)).deleted_at is not None
    assert GmailMessage.objects.filter(gmail_id=gid(8)).exists()  # fetched by the re-walk
    assert state("history_id") == "3000"
    assert state("rewalk") == "0"
    assert (None, True) in client.list_calls  # presence check includes spam/trash


def test_full_rewalks_without_refetching(raw_dir):
    client = FakeClient(4)
    loader(client).load()
    GmailMessage.objects.filter(gmail_id=gid(2)).delete()  # lost somehow
    client.fetched.clear()
    loader(client).load(full=True)
    assert client.fetched == [gid(2)]
    assert GmailMessage.objects.count() == 4
