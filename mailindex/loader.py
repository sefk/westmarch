"""mailindex: searchable text for every stored Gmail message.

Two passes per run, both time-boxed and resumable (the cursor is simply
"what has no row yet"):
  1. Extract: newest messages first, each raw file is read in a worker
     process; body text, attachment rows and the text of documents go
     into SQLite. Images and scanned PDFs are marked status "ocr".
  2. OCR: blobs marked "ocr" go one at a time to the local model
     (settings.MAILINDEX_OCR_URL, loopback only). If it's down the pass
     stops and the next run retries.
"""

from __future__ import annotations

import fcntl
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager

from django.conf import settings
from django.db import transaction

from core.loaders import Disabled, Loader
from sources.google.loader import gmail_root
from sources.google.models import GmailMessage

from . import extract
from .models import AttachmentBlob, MailAttachment, MailBody
from .ocr import LocalOcr, OcrUnavailable

logger = logging.getLogger(__name__)

BATCH = 500
MAX_OCR_ATTEMPTS = 3


class InlineExecutor:
    """ProcessPoolExecutor's map, in-process (tests, workers=0)."""

    def map(self, fn, *its, chunksize=1):
        return map(fn, *its)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextmanager
def index_lock(root):
    lock = open(root / ".mailindex.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise Disabled("another mailindex run is in progress") from None
    try:
        yield
    finally:
        lock.close()


class MailIndexLoader(Loader):
    name = "mailindex"
    cadence = "hourly"
    clock = staticmethod(time.monotonic)

    def __init__(self, budget_seconds=None, workers=None, ocr=True, ocr_client=None, enabled=None):
        # Hourly runs only once MAILINDEX_ENABLED is set; `manage.py mailindex`
        # passes enabled=True, so the backfill can be started by hand first.
        self.enabled = enabled
        self.budget_seconds = budget_seconds if budget_seconds is not None else settings.MAILINDEX_RUN_SECONDS
        self.workers = workers if workers is not None else settings.MAILINDEX_WORKERS
        self.do_ocr = ocr
        self.ocr_client = ocr_client
        self.counts = {}

    def out_of_time(self) -> bool:
        return self.budget_seconds > 0 and self.clock() >= self.deadline

    def load(self, full: bool = False) -> int:
        if not (settings.MAILINDEX_ENABLED if self.enabled is None else self.enabled):
            raise Disabled("set MAILINDEX_ENABLED=1 for hourly runs (after the first manage.py mailindex)")
        root = gmail_root()
        self.deadline = self.clock() + self.budget_seconds
        self.counts = dict(messages=0, attachments=0, blobs_new=0, ocr_done=0, ocr_failed=0)
        with index_lock(root):
            try:
                self.extract_pass(root)
                if self.do_ocr and not self.out_of_time():
                    self.ocr_pass(root)
            finally:
                self.record()
        return self.counts["messages"] + self.counts["ocr_done"]

    # -- pass 1 --

    def pending(self):
        """(id, raw_path) of every message without a body row, newest first.
        Listed once per run: re-querying per batch would sort the whole
        mailbox each time. Mail that arrives meanwhile waits for the next run."""
        return list(GmailMessage.objects.filter(body__isnull=True).order_by("-internal_date", "-id")
                    .values_list("id", "raw_path"))

    def extract_pass(self, root):
        pool = ProcessPoolExecutor(self.workers) if self.workers > 0 else InlineExecutor()
        with pool:
            todo = self.pending()
            for start in range(0, len(todo), BATCH):
                if self.out_of_time():
                    break
                batch = todo[start:start + BATCH]
                # extract has no Django imports, so spawned workers can load it.
                results = pool.map(extract.index_file, [str(root)] * len(batch), [rel for _, rel in batch],
                                   chunksize=8)
                self.store(zip([mid for mid, _ in batch], results))

    def store(self, rows):
        with transaction.atomic():
            for mid, r in rows:
                MailBody.objects.create(message_id=mid, text=r["body"], error=r["body_error"])
                for a in r["attachments"]:
                    blob, created = AttachmentBlob.objects.get_or_create(sha256=a["sha256"], defaults=dict(
                        mime_type=a["mime_type"], size=a["size"], status=a["status"], method=a["method"],
                        text=a["text"], error=a["error"], source_message_id=mid, source_part=a["part"]))
                    self.counts["blobs_new"] += created
                    MailAttachment.objects.create(message_id=mid, part=a["part"], filename=a["filename"],
                                                  mime_type=a["mime_type"], size=a["size"], blob=blob)
                    self.counts["attachments"] += 1
                self.counts["messages"] += 1

    # -- pass 2 --

    def ocr(self):
        if self.ocr_client is None:
            self.ocr_client = LocalOcr(settings.MAILINDEX_OCR_URL, settings.MAILINDEX_OCR_MODEL)
        return self.ocr_client

    def ocr_pass(self, root):
        try:
            client = self.ocr()
        except OcrUnavailable as exc:
            self.counts["ocr_stopped"] = str(exc)
            return
        queue = AttachmentBlob.objects.filter(status=extract.OCR, ocr_attempts__lt=MAX_OCR_ATTEMPTS)
        for blob in queue.order_by("-source_message__internal_date").iterator(chunk_size=50):
            if self.out_of_time():
                break
            try:
                text = self.ocr_blob(root, client, blob)
            except OcrUnavailable as exc:
                self.counts["ocr_stopped"] = str(exc)
                return
            except Exception as exc:  # this file, not the server
                blob.ocr_attempts += 1
                blob.error = f"ocr: {type(exc).__name__}: {exc}"[:500]
                if blob.ocr_attempts >= MAX_OCR_ATTEMPTS:
                    blob.status = extract.ERROR
                blob.save()
                self.counts["ocr_failed"] += 1
                continue
            blob.text = extract.clean(text)[:extract.MAX_TEXT]
            blob.status, blob.method, blob.error = extract.TEXT, client.method, ""
            blob.ocr_attempts += 1
            blob.save()
            self.counts["ocr_done"] += 1

    def ocr_blob(self, root, client, blob) -> str:
        raw = extract.read_raw(root, blob.source_message.raw_path)
        mime, filename, data = extract.find_attachment(raw, blob.source_part)
        if extract.kind(mime, filename) == "pdf":
            pages = extract.pdf_page_images(data)
            return "\n\n".join(client.image_text(p) for p in pages)
        return client.image_text(extract.open_image(data))

    def record(self):
        for k, v in self.counts.items():
            self.state_set(k, str(v))
        self.state_set("messages_indexed", str(MailBody.objects.count()))
        self.state_set("messages_left", str(GmailMessage.objects.filter(body__isnull=True).count()))
        self.state_set("ocr_left", str(AttachmentBlob.objects.filter(
            status=extract.OCR, ocr_attempts__lt=MAX_OCR_ATTEMPTS).count()))
