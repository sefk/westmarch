"""iMessage: ~/Library/Messages/chat.db, read-only, incremental.

A small helper (ops/imessage-snapshot, the only binary granted Full Disk
Access) copies chat.db to settings.IMESSAGE_SNAPSHOT; the loader reads the
copy. settings.IMESSAGE_DB, if set, is read directly and skips the helper.

New rows come by `message.ROWID > cursor`. Edits, retractions and read
receipts change old rows in place, so each run also re-reads rows dated in
the trailing two days. Message text and handles are private and never
logged; run messages carry counts only.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone as tz

from django.conf import settings

from sources.imessage.spawn import run_disclaimed
from core.loaders import Disabled, Loader

from .models import Attachment, Chat, Handle, Message

APPLE_EPOCH = 978307200  # 2001-01-01 UTC
BATCH = 2000
CHUNK = 500  # stay under SQLite's variable limit
REREAD = timedelta(days=2)
SNAPSHOT_TIMEOUT = 600
EX_NOPERM = 77  # helper exit code: Full Disk Access not granted

REACTIONS = {2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh", 2004: "emphasize", 2005: "question"}

MESSAGE_COLS = [
    "ROWID", "guid", "text", "attributedBody", "handle_id", "service", "date", "date_read", "date_delivered",
    "is_from_me", "cache_has_attachments", "associated_message_guid", "associated_message_type",
    "thread_originator_guid", "item_type", "date_edited", "date_retracted",
]


def apple_time(v):
    """chat.db date -> aware datetime. Modern rows are ns since 2001, old ones seconds."""
    if not v:
        return None
    secs = v / 1e9 if abs(v) >= 1e11 else v
    return datetime.fromtimestamp(APPLE_EPOCH + secs, tz.utc)


def decode_attributed_body(blob) -> str:
    """Pull the message text out of an NSArchiver "typedstream" blob.

    Newer macOS leaves `message.text` NULL and stores the body only in
    `attributedBody`. The text is the NSString that follows the marker
    b"NSString": a few preamble bytes ending in 0x2b ('+'), then a length,
    then UTF-8. Length is one byte if < 0x80; 0x81 means a 2-byte
    little-endian length follows, 0x82 a 4-byte one. Returns "" if the
    blob doesn't fit that shape; callers count that as a decode failure.
    """
    if not blob:
        return ""
    blob = bytes(blob)
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    rest = blob[i + 8:]
    plus = rest.find(b"\x2b", 0, 10)
    if plus < 0:
        return ""
    rest = rest[plus + 1:]
    if not rest:
        return ""
    n = rest[0]
    start = 1
    if n == 0x81:
        n, start = int.from_bytes(rest[1:3], "little"), 3
    elif n == 0x82:
        n, start = int.from_bytes(rest[1:5], "little"), 5
    elif n > 0x82:
        return ""
    body = rest[start:start + n]
    if len(body) != n:
        return ""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class IMessageLoader(Loader):
    name = "imessage"
    cadence = "hourly"

    def snapshot(self) -> str:
        """Run the helper; return the path of the fresh snapshot."""
        binary, dest = Path(settings.IMESSAGE_SNAPSHOT_BIN), Path(settings.IMESSAGE_SNAPSHOT)
        if not binary.is_file():
            raise Disabled("snapshot helper not built: run bin/build-imessage-snapshot")
        raw = Path(settings.RAW_DIR)
        root = raw if raw in dest.parents else dest.parent
        if not root.parent.exists():
            raise Disabled(f"{root.parent} not mounted; the iMessage snapshot lives in {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Disclaimed, so the grant on the helper itself is what counts.
            code, stderr = run_disclaimed([str(binary), str(dest)], SNAPSHOT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"snapshot helper timed out after {SNAPSHOT_TIMEOUT}s") from None
        if code == EX_NOPERM:
            raise Disabled("snapshot helper lacks Full Disk Access")
        if code:
            err = stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"snapshot helper exited {code}: {err[-1][:200] if err else 'no output'}")
        return str(dest)

    def connect(self) -> sqlite3.Connection:
        if settings.IMESSAGE_DB:
            path = os.path.expanduser(str(settings.IMESSAGE_DB))
        else:
            path = self.snapshot()
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.execute("PRAGMA table_info(message)").fetchall()  # opening is lazy; force it
        except sqlite3.Error:
            if settings.IMESSAGE_DB:
                raise Disabled(
                    f"cannot open {path}; grant Full Disk Access to {os.path.realpath(sys.executable)}"
                ) from None
            raise RuntimeError(f"cannot read snapshot {path}") from None
        return con

    @staticmethod
    def _cols(con, table):
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

    def load(self, full: bool = False) -> int:
        con = self.connect()
        try:
            return self._load(con, full)
        finally:
            con.close()

    def _load(self, con, full):
        mcols = self._cols(con, "message")
        sel = ", ".join(f"m.{c}" if c in mcols else f"NULL AS {c}" for c in MESSAGE_COLS)
        handles = self.sync_handles(con)
        chats = self.sync_chats(con, handles)
        cursor = 0 if full else int(self.state_get("last_rowid", "0"))
        failures = 0
        rows = 0
        seen = set()

        if not full and "date" in mcols:
            cut = (datetime.now(tz.utc) - REREAD).timestamp() - APPLE_EPOCH
            recent = con.execute(
                f"SELECT {sel} FROM message m WHERE m.ROWID <= ? AND "
                f"((m.date >= ?) OR (m.date < 1e11 AND m.date >= ?)) ORDER BY m.ROWID",
                (cursor, int(cut * 1e9), int(cut)),
            ).fetchall()
            for batch in _chunks(recent, BATCH):
                n, f = self.store(con, batch, handles, chats)
                rows, failures = rows + n, failures + f
                seen.update(r[0] for r in batch)

        while True:
            batch = con.execute(
                f"SELECT {sel} FROM message m WHERE m.ROWID > ? ORDER BY m.ROWID LIMIT {BATCH}", (cursor,)
            ).fetchall()
            if not batch:
                break
            n, f = self.store(con, batch, handles, chats)
            rows, failures = rows + n, failures + f
            cursor = batch[-1][0]
            self.state_set("last_rowid", str(cursor))
        if failures:
            total = int(self.state_get("body_decode_failures", "0")) + failures
            self.state_set("body_decode_failures", str(total))
        return rows

    def sync_handles(self, con) -> dict[int, Handle]:
        cols = self._cols(con, "handle")
        sel = "ROWID, id, " + ("service" if "service" in cols else "NULL")
        out = {}
        for rowid, ident, service in con.execute(f"SELECT {sel} FROM handle"):
            out[rowid], _ = Handle.objects.get_or_create(identifier=ident or "", service=service or "")
        return out

    def sync_chats(self, con, handles) -> dict[int, Chat]:
        cols = self._cols(con, "chat")
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in
                        ["ROWID", "guid", "chat_identifier", "display_name", "style", "service_name"])
        people: dict[int, list[str]] = {}
        if self._cols(con, "chat_handle_join"):
            for cid, hid in con.execute("SELECT chat_id, handle_id FROM chat_handle_join ORDER BY handle_id"):
                if hid in handles:
                    people.setdefault(cid, []).append(handles[hid].identifier)
        out = {}
        for rowid, guid, ident, name, style, service in con.execute(f"SELECT {sel} FROM chat"):
            chat, _ = Chat.objects.update_or_create(guid=guid, defaults={
                "identifier": ident or "", "display_name": name or "", "is_group": style == 43,
                "service": service or "", "participants": people.get(rowid, []),
            })
            out[rowid] = chat
        return out

    def store(self, con, batch, handles, chats) -> tuple[int, int]:
        ids = [r[0] for r in batch]
        chat_of, atts = {}, {}
        for ch in _chunks(ids):
            q = ",".join("?" * len(ch))
            for mid, cid in con.execute(
                f"SELECT message_id, MIN(chat_id) FROM chat_message_join WHERE message_id IN ({q}) GROUP BY message_id", ch
            ):
                chat_of[mid] = cid
            if self._cols(con, "attachment"):
                acols = self._cols(con, "attachment")
                asel = ", ".join(f"a.{c}" if c in acols else f"NULL" for c in
                                 ["guid", "filename", "mime_type", "transfer_name", "total_bytes"])
                for row in con.execute(
                    f"SELECT j.message_id, {asel} FROM message_attachment_join j "
                    f"JOIN attachment a ON a.ROWID = j.attachment_id WHERE j.message_id IN ({q})", ch
                ):
                    atts.setdefault(row[0], []).append(row[1:])
        failures = 0
        objs = []
        for (rowid, guid, text, body, hid, service, date, dread, ddeliv, from_me, has_att, assoc_guid,
             assoc_type, thread_guid, item_type, dedit, dretract) in batch:
            if not text and body:
                text = decode_attributed_body(body)
                if not text:
                    failures += 1
            reaction, target = "", ""
            if assoc_type and 2000 <= assoc_type <= 2005:
                reaction = REACTIONS[assoc_type]
            elif assoc_type and 3000 <= assoc_type <= 3005:
                reaction = "-" + REACTIONS[assoc_type - 1000]
            if reaction:
                target = (assoc_guid or "").split("/")[-1].split(":")[-1]
                text = ""
            objs.append(Message(
                rowid=rowid, guid=guid, chat=chats.get(chat_of.get(rowid)), handle=handles.get(hid),
                is_from_me=bool(from_me), sent_at=apple_time(date), read_at=apple_time(dread),
                delivered_at=apple_time(ddeliv), service=service or "", text=text or "",
                has_attachments=bool(has_att or atts.get(rowid)), attachment_count=len(atts.get(rowid, [])),
                reaction=reaction, reply_to_guid=target or thread_guid or "",
                edited_at=apple_time(dedit), retracted_at=apple_time(dretract), item_type=item_type or 0,
            ))
        fields = [f.name for f in Message._meta.concrete_fields if f.name not in ("id", "rowid")]
        Message.objects.bulk_create(objs, update_conflicts=True, unique_fields=["rowid"], update_fields=fields)
        pks = dict(Message.objects.filter(rowid__in=ids).values_list("rowid", "pk"))
        Attachment.objects.filter(message_id__in=pks.values()).delete()
        Attachment.objects.bulk_create([
            Attachment(message_id=pks[mid], guid=g or "", filename=fn or "", mime_type=mt or "",
                       transfer_name=tn or "", total_bytes=tb)
            for mid, items in atts.items() if mid in pks for g, fn, mt, tn, tb in items
        ])
        return len(objs), failures
