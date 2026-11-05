"""Seed the Gmail mirror from a Google Takeout mbox, without the API.

The API quota allows about 100 raw messages a minute; a Takeout of the
whole mailbox imports in a fraction of the time. Takeout's mbox:

  From 1852047678917183467@xxx Sat Dec 20 17:05:04 +0000 2025
  X-GM-THRID: 1852047678917183467
  X-Gmail-Labels: Inbox,Important,Opened
  <the original message, mboxrd-escaped>
  <blank line>

The decimal in the separator, as lowercase hex, is the Gmail message id;
X-GM-THRID likewise is the thread id; the separator date is the internal
date (UTC). The two X- headers are Takeout's additions and are removed,
`>From ` escaping is undone and the trailing blank line dropped, so the
stored bytes are the original message, as the API would return it.

Rows get source="takeout", the Takeout label names in takeout_labels and
empty label_ids; `manage.py gmail_sync_labels` fills label_ids from the
API afterwards. Spam and Trash are skipped unless asked for (the API
backfill skips them too). Ids that already have a row and a file are
skipped, so a rerun is cheap and safe alongside the hourly loader.

The mbox is streamed line by line (from inside the zip when given one),
so memory holds one message per worker. gzip+write+header parsing runs
in a process pool; rows go in with bulk_create(ignore_conflicts) in
short transactions, since the web server and loaders share the database.
"""

from __future__ import annotations

import io
import os
import re
import time
import zipfile
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from core.models import LoaderState

from .models import GmailMessage
from .parse import raw_relpath, store_and_parse

# Only this exact shape splits messages: "From <decimal>@xxx Sat Dec 20 17:05:04 +0000 2025".
SEPARATOR = re.compile(
    rb"^From (\d+)@xxx ([A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]?\d \d\d:\d\d:\d\d [+-]\d{4} \d{4})\r?\n?$")
ESCAPED_FROM = re.compile(rb"^>+From ")
TAKEOUT_HEADERS = (b"x-gm-thrid", b"x-gmail-labels")
SPAM_TRASH = {"Spam", "Trash"}
BATCH = 500
PROGRESS_EVERY = 10_000
STATE = "gmail"
COUNTERS = ("imported", "skipped_existing", "skipped_spam_trash", "parse_errors", "header_errors", "bytes_read")


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 2))


# -- reading ---------------------------------------------------------------


class Counted(io.RawIOBase):
    """Counts bytes as they're read, for progress."""

    def __init__(self, f):
        self.f, self.n = f, 0

    def readable(self):
        return True

    def readinto(self, b):
        n = self.f.readinto(b)
        self.n += n or 0
        return n


@contextmanager
def open_mbox(path: Path):
    """(binary stream, uncompressed size) for a Takeout zip or a bare mbox."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            members = [i for i in z.infolist() if i.filename.endswith(".mbox")]
            if not members:
                raise ValueError(f"{path.name}: no .mbox member in the zip")
            if len(members) > 1:
                raise ValueError(f"{path.name}: {len(members)} .mbox members; expected one")
            with z.open(members[0]) as f:
                yield f, members[0].file_size
    else:
        with open(path, "rb") as f:
            yield f, path.stat().st_size


def split_mbox(f, skip=lambda gid: False):
    """Yield (gmail id, separator date, lines) per message. Lines of a
    message whose id `skip` accepts are not kept (lines is None)."""
    gid = date = None
    lines: list[bytes] | None = None
    for line in f:
        m = SEPARATOR.match(line) if line.startswith(b"From ") else None
        if m:
            if gid is not None:
                yield gid, date, lines
            gid, date = f"{int(m.group(1)):x}", m.group(2).decode()
            lines = None if skip(gid) else []
        elif lines is not None:
            lines.append(line)
    if gid is not None:
        yield gid, date, lines


def _header_text(value: bytes) -> str:
    s = value.decode("utf-8", "replace").replace("\r", "").replace("\n", "").strip()
    try:
        return str(make_header(decode_header(s)))
    except Exception:  # a malformed encoded word: keep the text as is
        return s


def unpack(lines: list[bytes]) -> tuple[bytes, str, list[str], bool]:
    """(original raw message, thread id hex, label names, had thread id)
    from one message's lines (after the separator)."""
    takeout: dict[bytes, bytes] = {}
    i = 0
    while i < len(lines):
        name, sep, value = lines[i].partition(b":")
        name = name.strip().lower()
        if not sep or name not in TAKEOUT_HEADERS:
            break
        i += 1
        while i < len(lines) and lines[i][:1] in (b" ", b"\t"):  # folded
            value += lines[i]
            i += 1
        takeout[name] = value
    end = len(lines)
    if end > i and lines[end - 1] in (b"\r\n", b"\n"):
        end -= 1  # mbox's blank line before the next separator
    raw = b"".join(line[1:] if line[:1] == b">" and ESCAPED_FROM.match(line) else line
                   for line in lines[i:end])
    thread = ""
    if b"x-gm-thrid" in takeout:
        try:
            thread = f"{int(takeout[b'x-gm-thrid'].strip()):x}"
        except ValueError:
            pass
    labels = []
    if b"x-gmail-labels" in takeout:
        labels = [s.strip() for s in _header_text(takeout[b"x-gmail-labels"]).split(",") if s.strip()]
    return raw, thread, labels, bool(thread)


def separator_date(s: str):
    try:
        return datetime.strptime(" ".join(s.split()), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


# -- import ----------------------------------------------------------------


class InlineExecutor:
    """Same interface as the process pool, for workers=0 (and tests)."""

    class Done:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    def submit(self, fn, *args):
        return self.Done(fn(*args))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def import_takeout(path, root: Path, *, include_spam_trash=False, limit=None, workers=None,
                   out=print, progress_every=PROGRESS_EVERY) -> dict:
    """Import one Takeout zip or mbox into GmailMessage rows and raw files
    under `root`. Returns the counts. Never logs message content."""
    path = Path(path).expanduser()
    workers = default_workers() if workers is None else workers
    have_rows = set(GmailMessage.objects.values_list("gmail_id", flat=True))
    counts = dict.fromkeys(COUNTERS, 0)
    seen = 0
    started = time.monotonic()
    LoaderState.set(STATE, "takeout_started", timezone.now().isoformat())

    def exists(gid):
        return gid in have_rows and (root / raw_relpath(gid)).exists()

    def save_state():
        for k, v in counts.items():
            LoaderState.set(STATE, f"takeout_{k}", str(v))

    def report(final=False):
        secs = max(time.monotonic() - started, 1e-9)
        gb = counts["bytes_read"] / 1e9
        pct = f" ({100 * counts['bytes_read'] / total:.1f}%)" if total else ""
        out(f"{'done' if final else 'progress'}: {seen:,} messages, {counts['imported']:,} imported, "
            f"{counts['skipped_existing']:,} existing, {counts['skipped_spam_trash']:,} spam/trash, "
            f"{counts['parse_errors']:,} takeout parse errors, {counts['header_errors']:,} header problems; "
            f"{gb:.2f} GB read{pct}, {seen / secs:,.0f} msg/s, {gb * 1000 / secs:.1f} MB/s, {secs:,.0f} s")

    rows: list[GmailMessage] = []

    def flush():
        if rows:
            with transaction.atomic():
                GmailMessage.objects.bulk_create(rows, batch_size=BATCH, ignore_conflicts=True)
            rows.clear()

    pending: deque = deque()  # (future, row) in mbox order

    def drain(keep):
        while len(pending) > keep:
            fut, row = pending.popleft()
            fields = fut.result()
            if fields["parse_error"]:
                counts["header_errors"] += 1
            if row is not None:
                for k, v in fields.items():
                    setattr(row, k, v)
                rows.append(row)
                if len(rows) >= BATCH:
                    flush()

    pool = ProcessPoolExecutor(workers) if workers > 0 else InlineExecutor()
    with open_mbox(path) as (f, total), pool:
        counted = Counted(f)
        reader = io.BufferedReader(counted, buffer_size=1 << 20)
        try:
            for gid, date, lines in split_mbox(reader, skip=exists):
                if limit is not None and seen >= limit:
                    break
                seen += 1
                counts["bytes_read"] = counted.n
                if lines is None:
                    counts["skipped_existing"] += 1
                else:
                    raw, thread, labels, ok = unpack(lines)
                    del lines
                    internal = separator_date(date)
                    if not ok or internal is None:
                        counts["parse_errors"] += 1
                    if not include_spam_trash and SPAM_TRASH & set(labels):
                        counts["skipped_spam_trash"] += 1
                    else:
                        rel = raw_relpath(gid)
                        has_file = (root / rel).exists()
                        row = None if gid in have_rows else GmailMessage(
                            gmail_id=gid, thread_id=thread, internal_date=internal, label_ids=[],
                            size_estimate=len(raw), snippet="", raw_path=rel, source="takeout",
                            takeout_labels=labels)
                        if row is None and has_file:
                            counts["skipped_existing"] += 1
                        else:
                            counts["imported"] += 1
                            pending.append((pool.submit(store_and_parse, root, gid, raw, not has_file), row))
                            drain(4 * max(workers, 1))
                if seen % progress_every == 0:
                    drain(0)
                    flush()
                    save_state()
                    report()
        finally:
            drain(0)
            flush()
            counts["bytes_read"] = counted.n
            save_state()
    complete = limit is None or seen < limit
    if complete:
        LoaderState.set(STATE, "takeout_finished", timezone.now().isoformat())
    report(final=True)
    return counts | {"messages": seen, "seconds": time.monotonic() - started, "complete": complete}
