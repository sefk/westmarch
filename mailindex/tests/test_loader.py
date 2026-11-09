"""The mailindex loader over generated messages in a tmp RAW_DIR."""

import pytest

from core.loaders import Disabled
from core.models import LoaderState
from mailindex import search as search_mod
from mailindex.loader import MailIndexLoader
from mailindex.models import AttachmentBlob, MailAttachment, MailBody
from mailindex.ocr import OcrUnavailable
from sources.google.tests.fakes import Clock

from .fixtures import FakeOcr, email, picture, scanned_pdf, stored, text_pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def root(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "raw"
    settings.RAW_DIR.mkdir()
    return settings.RAW_DIR


def loader(ocr=None, budget=0, clock=None):
    ld = MailIndexLoader(budget_seconds=budget, workers=0, ocr=True, ocr_client=ocr or FakeOcr(), enabled=True)
    if clock:
        ld.clock = clock
    return ld


def state(key):
    return LoaderState.get("mailindex", key)


def test_hourly_runs_wait_for_the_switch(settings, root):
    settings.MAILINDEX_ENABLED = False
    with pytest.raises(Disabled, match="MAILINDEX_ENABLED"):
        MailIndexLoader().load()
    settings.MAILINDEX_ENABLED = True
    assert MailIndexLoader(ocr=False, workers=0).load() == 0


def test_disabled_without_volume(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "gone" / "westmarch"
    with pytest.raises(Disabled, match="not mounted"):
        loader().load()


def test_indexes_bodies_attachments_and_ocr(root):
    pdf, pic = text_pdf("walrus invoice"), picture()
    stored(root, 1, email(1, body="the giraffe meeting moved", attachments=[
        ("inv.pdf", "application", "pdf", pdf), ("photo.png", "image", "png", pic)]))
    stored(root, 2, email(2, body="same invoice again", attachments=[("copy.pdf", "application", "pdf", pdf)]))
    stored(root, 3, email(3, html="<p>only html <b>okapi</b></p>"))
    ocr = FakeOcr()

    assert loader(ocr).load() == 3 + 1
    assert MailBody.objects.count() == 3
    assert MailAttachment.objects.count() == 3
    assert AttachmentBlob.objects.count() == 2  # the pdf is stored once
    assert ocr.calls == 1
    photo = AttachmentBlob.objects.get(mime_type="image/png")
    assert (photo.status, photo.method, photo.text) == ("text", "ocr:fake", "transcribed zebra 800x400")
    assert state("messages_left") == "0" and state("ocr_left") == "0"

    hits = search_mod.search("giraffe")
    assert [h["subject"] for h in hits["bodies"]] == ["Test message 1"]
    assert "[giraffe]" in hits["bodies"][0]["snippet"]
    hits = search_mod.search("walrus")
    assert sorted(h["filename"] for h in hits["attachments"]) == ["copy.pdf", "inv.pdf"]
    assert search_mod.search("zebra")["attachments"][0]["filename"] == "photo.png"
    assert search_mod.search("okapi")["bodies"][0]["subject"] == "Test message 3"

    # A second run finds nothing new and does no OCR.
    assert loader(ocr).load() == 0
    assert ocr.calls == 1


def test_new_mail_is_picked_up_next_run(root):
    stored(root, 1, email(1, body="first"))
    loader().load()
    stored(root, 2, email(2, body="second penguin"))
    assert loader().load() == 1
    assert search_mod.search("penguin")["bodies"][0]["gmail_id"] == f"{2:016x}"


def test_scanned_pdf_is_ocrd_page_by_page(root):
    stored(root, 1, email(1, body="x", attachments=[("scan.pdf", "application", "pdf", scanned_pdf())]))
    loader().load()
    blob = AttachmentBlob.objects.get()
    assert blob.status == "text" and blob.text.startswith("transcribed zebra")


def test_ocr_server_down_leaves_work_for_later(root):
    stored(root, 1, email(1, body="x", attachments=[("p.png", "image", "png", picture())]))
    loader(FakeOcr(fail_with=OcrUnavailable("down"))).load()
    blob = AttachmentBlob.objects.get()
    assert (blob.status, blob.ocr_attempts) == ("ocr", 0)
    assert state("ocr_stopped") == "down"
    assert state("ocr_left") == "1"
    loader().load()
    assert AttachmentBlob.objects.get().status == "text"


def test_bad_file_gives_up_after_three_tries(root):
    stored(root, 1, email(1, body="x", attachments=[("p.png", "image", "png", picture())]))
    for _ in range(4):
        loader(FakeOcr(fail_with=ValueError("model said no"))).load()
    blob = AttachmentBlob.objects.get()
    assert (blob.status, blob.ocr_attempts) == ("error", 3)
    assert "model said no" in blob.error


def test_missing_raw_file_is_recorded(root):
    m = stored(root, 1, email(1, body="x"))
    (root / m.raw_path).unlink()
    loader().load()
    assert MailBody.objects.get().error == "raw file missing"


def test_time_box_resumes_newest_first(root, monkeypatch):
    import mailindex.loader as mod

    monkeypatch.setattr(mod, "BATCH", 2)
    for n in range(1, 6):
        stored(root, n, email(n, body=f"message {n}"))
    clock = Clock()
    ld = loader(budget=10, clock=clock)
    orig = ld.store

    def store_and_tick(rows):
        orig(rows)
        clock.t += 10

    ld.store = store_and_tick
    assert ld.load() == 2
    assert sorted(MailBody.objects.values_list("message__gmail_id", flat=True)) == [f"{4:016x}", f"{5:016x}"]
    assert state("messages_left") == "3"
    assert loader().load() == 3


def test_fts_follows_updates_and_deletes(root):
    m = stored(root, 1, email(1, body="old wording"))
    loader().load()
    body = MailBody.objects.get()
    body.text = "new wording"
    body.save()
    assert search_mod.search("old")["bodies"] == []
    assert len(search_mod.search("new")["bodies"]) == 1
    m.delete()
    assert search_mod.search("new")["bodies"] == []


def test_query_syntax_is_inert():
    assert search_mod.fts_query('budget OR "x" NEAR( inv*') == '"budget" "OR" "x" "NEAR" "inv"*'
    assert search_mod.fts_query("  ()  ") == ""


def test_worker_processes(root):
    for n in range(1, 4):
        stored(root, n, email(n, body=f"pooled heron {n}", attachments=[("a.txt", "text", "plain", b"t%d" % n)]))
    ld = MailIndexLoader(budget_seconds=0, workers=2, ocr=False, enabled=True)
    assert ld.load() == 3
    assert len(search_mod.search("heron")["bodies"]) == 3
    assert MailAttachment.objects.count() == 3
