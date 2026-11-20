"""mailvec: embeddings of mail for search by meaning.

Three passes per run, each newest first, time-boxed and resumable (the
store's `done` table is the cursor):
  1. digests: ThreadDigest summaries from the current MAILDIGEST_MODEL,
     re-embedded when a digest is rewritten
  2. bodies: each message's own words (quoted copies cut off), in chunks
  3. attachments: the extracted or OCR'd text of each distinct file

MAILVEC_SCOPE "personal" (the default) covers threads I wrote in and mail
outside the bulk tabs and mailing lists (chunks.PERSONAL); "all" adds the
rest after that. The embedding model is MAILVEC_MODEL on the local server;
each model gets its own store file, so changing it starts a fresh index.
"""

from __future__ import annotations

import time

import requests
from django.conf import settings
from django.db import connection

from core.loaders import Disabled, Loader
from sources.google.loader import gmail_root

from .chunks import MY_THREADS, NOT_NEVER, PERSONAL, chunk, own_text
from .digest import digest_text, file_lock
from .llm import LlmUnavailable, LocalLlm
from .models import AttachmentBlob, MailAttachment, MailBody, ThreadDigest
from .vecstore import VecStore, day_of, store_path

BATCH = 32  # chunks per embedding request
BODY_CHUNKS = 6
ATTACHMENT_CHUNKS = 8


class MailVecLoader(Loader):
    name = "mailvec"
    cadence = "hourly"
    clock = staticmethod(time.monotonic)

    def __init__(self, budget_seconds=None, client=None, model=None, enabled=None, scope=None, limit=None):
        self.enabled = enabled
        self.budget_seconds = budget_seconds if budget_seconds is not None else settings.MAILVEC_RUN_SECONDS
        self.model = model or settings.MAILVEC_MODEL
        self.scope = scope or settings.MAILVEC_SCOPE
        self.client = client
        self.limit = limit  # items per pass, for trial runs
        self.counts = {}

    def out_of_time(self) -> bool:
        return self.budget_seconds > 0 and self.clock() >= self.deadline

    def load(self, full: bool = False) -> int:
        if not (settings.MAILVEC_ENABLED if self.enabled is None else self.enabled):
            raise Disabled("set MAILVEC_ENABLED=1 for hourly runs (after a first manage.py mailvec)")
        gmail_root()  # Disabled when the external volume isn't mounted
        self.deadline = self.clock() + self.budget_seconds
        self.counts = dict(digests=0, bodies=0, attachments=0, chunks=0, skipped=0)
        client = self.client or LocalLlm(settings.MAILVEC_URL, self.model)
        path = store_path(settings.MAILINDEX_VEC_DIR, self.model)
        with file_lock(path.parent / ".mailvec.lock"):
            self.store = VecStore(path, self.model)
            self.doc_prefix = self.store.meta("doc_prefix", "")
            try:
                for step in (self.digest_pass, self.body_pass, self.attachment_pass):
                    if self.out_of_time():
                        break
                    step(client)
            except LlmUnavailable as exc:
                self.counts["stopped"] = str(exc)
            finally:
                self.record(path)
                self.store.close()
        return self.counts["digests"] + self.counts["bodies"] + self.counts["attachments"]

    # -- shared --

    def embed_items(self, client, kind: str, items, counter: str):
        """items: iterable of (ref, stamp, [chunk dicts]). Embeds in batches of
        about BATCH chunks; each item is stored whole, with its chunks."""
        pending, n_chunks = [], 0

        def flush():
            nonlocal pending, n_chunks
            if not pending:
                return
            texts = [c["embed"] for _, _, cs in pending for c in cs]
            vectors = self.embed(client, texts) if texts else []
            at = 0
            for ref, stamp, cs in pending:
                vecs = vectors[at:at + len(cs)]
                at += len(cs)
                if len(vecs) < len(cs) or any(v is None for v in vecs):  # the server refused this text
                    self.store.replace(kind, ref, "error", [], [])
                    self.counts["skipped"] += 1
                    continue
                self.store.replace(kind, ref, stamp, cs, vecs)
                self.counts[counter] += 1
                self.counts["chunks"] += len(cs)
            self.store.commit()
            pending, n_chunks = [], 0

        for done, (ref, stamp, cs) in enumerate(items):
            if self.out_of_time() or (self.limit and done >= self.limit):
                break
            pending.append((ref, stamp, cs))
            n_chunks += len(cs)
            if n_chunks >= BATCH:
                flush()
        flush()

    def embed(self, client, texts: list[str]) -> list:
        """Vectors for texts; on a 4xx for the batch, one at a time, with a
        refused text getting None (and its item skipped)."""
        try:
            return client.embed(texts)
        except requests.HTTPError:
            out = []
            for t in texts:
                try:
                    out.extend(client.embed([t]))
                except requests.HTTPError:
                    out.append(None)
            return out

    def _scope_sql(self) -> str:
        return PERSONAL if self.scope == "personal" else "1"

    # -- pass 1: digests --

    def digest_pass(self, client):
        model = settings.MAILDIGEST_MODEL
        done = self.store.done_stamps("digest")
        rows = (ThreadDigest.objects.filter(model=model, error="").exclude(summary="")
                .order_by("-last_message_at"))
        todo = [d for d in rows if done.get(d.id) != d.updated.isoformat()]
        self.counts["digests_left_at_start"] = len(todo)

        def items():
            for d in todo:
                text = digest_text(d)
                yield d.id, d.updated.isoformat(), [dict(
                    seq=0, thread_id=d.thread_id, message_id=None, day=day_of(d.last_message_at),
                    text=text, embed=self.doc_prefix + text)]

        self.embed_items(client, "digest", items(), "digests")

    # -- pass 2: bodies --

    def body_pass(self, client):
        done = self.store.done_stamps("body")
        with connection.cursor() as c:
            c.execute(f"""
                WITH my_threads AS ({MY_THREADS})
                SELECT m.id FROM src_google_gmailmessage m JOIN mailindex_mailbody b ON b.message_id = m.id
                WHERE m.deleted_at IS NULL AND {NOT_NEVER} AND {self._scope_sql()}
                ORDER BY m.internal_date DESC""")
            todo = [r[0] for r in c.fetchall() if r[0] not in done]
        self.counts["bodies_left_at_start"] = len(todo)

        def items():
            for start in range(0, len(todo), 200):
                ids = todo[start:start + 200]
                bodies = {b.message_id: b for b in MailBody.objects.filter(message_id__in=ids).select_related("message")}
                for mid in ids:
                    b = bodies.get(mid)
                    if b is None:
                        continue
                    m = b.message
                    head = f"Subject: {m.subject}\nFrom: {m.from_name} <{m.from_address}>\n\n"
                    cs = [dict(seq=i, thread_id=m.thread_id, message_id=m.id, day=day_of(m.internal_date),
                               text=piece, embed=self.doc_prefix + head + piece)
                          for i, piece in enumerate(chunk(own_text(b.text), max_chunks=BODY_CHUNKS))]
                    if not cs and m.subject:  # empty body: the subject alone still says something
                        cs = [dict(seq=0, thread_id=m.thread_id, message_id=m.id, day=day_of(m.internal_date),
                                   text=m.subject, embed=self.doc_prefix + head)]
                    yield mid, "", cs

        self.embed_items(client, "body", items(), "bodies")

    # -- pass 3: attachments --

    def attachment_pass(self, client):
        done = self.store.done_stamps("att")
        with connection.cursor() as c:
            c.execute(f"""
                WITH my_threads AS ({MY_THREADS})
                SELECT a.blob_id, MAX(m.internal_date) FROM mailindex_mailattachment a
                JOIN src_google_gmailmessage m ON m.id = a.message_id
                JOIN mailindex_attachmentblob ab ON ab.id = a.blob_id
                WHERE ab.status = 'text' AND ab.text != '' AND m.deleted_at IS NULL AND {NOT_NEVER}
                  AND {self._scope_sql()}
                GROUP BY a.blob_id ORDER BY MAX(m.internal_date) DESC""")
            todo = [r[0] for r in c.fetchall() if r[0] not in done]
        self.counts["attachments_left_at_start"] = len(todo)

        def items():
            for start in range(0, len(todo), 100):
                ids = todo[start:start + 100]
                blobs = AttachmentBlob.objects.filter(id__in=ids).select_related("source_message")
                by_id = {b.id: b for b in blobs}
                names = dict(MailAttachment.objects.filter(blob_id__in=ids)
                             .values_list("blob_id", "filename"))
                for bid in ids:
                    b = by_id.get(bid)
                    if b is None:
                        continue
                    m = b.source_message
                    head = f"Attachment: {names.get(bid) or '(unnamed)'}\nIn email: {m.subject}\n\n"
                    cs = [dict(seq=i, thread_id=m.thread_id, message_id=m.id, day=day_of(m.internal_date),
                               text=piece, embed=self.doc_prefix + head + piece)
                          for i, piece in enumerate(chunk(b.text, max_chunks=ATTACHMENT_CHUNKS))]
                    yield bid, "", cs

        self.embed_items(client, "att", items(), "attachments")

    def record(self, path):
        for k, v in self.counts.items():
            self.state_set(k, str(v))
        # Readers (mailsearch, Samwise) find the store and model here.
        self.state_set("store", str(path))
        self.state_set("model", self.model)
        self.state_set("url", settings.MAILVEC_URL)
        self.state_set("scope", self.scope)
        for k, v in self.store.counts().items():
            self.state_set(k, str(v))
