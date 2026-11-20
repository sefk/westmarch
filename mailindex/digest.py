"""maildigest: a local model's index entry for every thread I wrote in.

For each thread with a message I sent, the thread's text (each message's
own words, quoted copies cut off) goes to the local model, which returns
JSON: a short summary, a topic, the people and organisations, dates,
amounts, commitments, any open question and a few search keywords. That
lands in ThreadDigest, one row per thread per model, and mailvec embeds
the summary so meaning-based search finds threads by what they were about.

Newest threads first, time-boxed, resumable. A thread that gets new mail
is digested again. The model is settings.MAILDIGEST_MODEL; switching it
starts a fresh set of rows and leaves the old ones for comparison
(`manage.py maildigest --compare A B`).
"""

from __future__ import annotations

import fcntl
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC

from django.conf import settings
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.loaders import Disabled, Loader
from sources.google.models import GmailMessage

from .chunks import IS_SENT, MY_THREADS, NOT_NEVER, own_text
from .llm import LlmUnavailable, LocalLlm
from .models import MailAttachment, ThreadDigest

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 3
PER_MESSAGE_CHARS = 4000

TOPICS = ["work", "family", "friends", "home", "money", "shopping", "travel", "health", "school",
          "community", "legal", "tech", "other"]


def _items(props: dict) -> dict:
    return {"type": "array", "items": {"type": "object", "properties": props,
                                       "required": list(props), "additionalProperties": False}}


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topic": {"type": "string", "enum": TOPICS},
        "people": {"type": "array", "items": {"type": "string"}},
        "organizations": {"type": "array", "items": {"type": "string"}},
        "dates": _items({"date": {"type": "string"}, "what": {"type": "string"}}),
        "amounts": _items({"amount": {"type": "string"}, "what": {"type": "string"}}),
        "commitments": _items({"who": {"type": "string", "enum": ["me", "them"]}, "what": {"type": "string"}}),
        "open_question": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "topic", "people", "organizations", "dates", "amounts", "commitments",
                 "open_question", "keywords"],
    "additionalProperties": False,
}

SYSTEM = """You write index entries for a personal email archive. Its owner is {owner}.
You get one email thread; reply with a JSON object describing it, from the owner's point of view ("me" is the owner).

- summary: one or two plain sentences, at most 50 words: what the thread is about and where it ended up.
- topic: the single best fit.
- people: names of the other people involved (not the owner), as written. organizations: companies, schools, agencies.
- dates: concrete dates that matter (events, deadlines, appointments) as YYYY-MM-DD, resolving relative dates against the message date. Leave out dates you cannot pin down.
- amounts: money amounts with what they are for.
- commitments: things someone agreed or promised to do; who is "me" or "them".
- open_question: what is still unresolved or waiting on someone at the end of the thread, or "" if nothing.
- keywords: 3 to 8 words or short phrases someone might search for to find this thread.
Only use what the thread says. Do not guess."""


def owner() -> tuple[str, set[str]]:
    """('Name <addr, addr>' for the prompt, my addresses), from the mail I've sent."""
    with connection.cursor() as c:
        c.execute(f"SELECT m.from_name, m.from_address FROM src_google_gmailmessage m WHERE {IS_SENT}"
                  " ORDER BY m.internal_date DESC LIMIT 2000")
        rows = c.fetchall()
    names = Counter(n for n, _ in rows if n)
    addrs = [a for a, _ in Counter(a.lower() for _, a in rows if a).most_common(3)]
    name = names.most_common(1)[0][0] if names else "the owner"
    return f"{name} <{', '.join(addrs)}>", set(addrs)


def _when(dt) -> str:
    return timezone.localtime(dt).strftime("%a %Y-%m-%d %H:%M") if dt else "unknown date"


def thread_messages(thread_id: str) -> list[GmailMessage]:
    qs = (GmailMessage.objects.filter(thread_id=thread_id, deleted_at__isnull=True)
          .select_related("body").order_by("internal_date", "id"))
    return [m for m in qs if not {"SPAM", "TRASH"} & set(m.label_ids or [])]


def render_thread(messages: list[GmailMessage], my_addresses: set[str], max_chars: int) -> str:
    """The thread as plain text: subject once, then each message's header and
    own words. Over max_chars, the first message and the newest that fit are
    kept and the middle is marked as left out."""
    atts: dict[int, list[str]] = {}
    for message_id, filename in MailAttachment.objects.filter(
            message_id__in=[m.id for m in messages]).values_list("message_id", "filename"):
        atts.setdefault(message_id, []).append(filename or "(unnamed)")
    blocks = []
    for m in messages:
        me = " (me)" if m.from_address.lower() in my_addresses or "SENT" in (m.label_ids or []) else ""
        to = ", ".join((m.to or [])[:6])
        head = f"--- {_when(m.internal_date)} | From: {m.from_name} <{m.from_address}>{me} | To: {to}"
        try:
            text = own_text(m.body.text)
        except GmailMessage.body.RelatedObjectDoesNotExist:
            text = m.snippet
        text = text[:PER_MESSAGE_CHARS] + (" …[cut]" if len(text) > PER_MESSAGE_CHARS else "")
        if m.id in atts:
            text += "\n[attachments: " + ", ".join(atts[m.id]) + "]"
        blocks.append(f"{head}\n{text.strip()}")
    subject = messages[0].subject if messages else ""
    header = f"Subject: {subject}\nMessages: {len(messages)}\n"
    if sum(len(b) for b in blocks) + len(header) <= max_chars or len(blocks) <= 2:
        return header + "\n\n".join(blocks)[:max_chars]
    budget = max_chars - len(header) - len(blocks[0]) - 100
    tail: list[str] = []
    for b in reversed(blocks[1:]):
        if len(b) > budget:
            break
        tail.insert(0, b)
        budget -= len(b) + 2
    skipped = len(blocks) - 1 - len(tail)
    middle = [f"[… {skipped} messages left out …]"] if skipped else []
    return header + "\n\n".join([blocks[0], *middle, *tail])


def pending_threads(model: str) -> list[tuple[str, int, object]]:
    """(thread_id, message count, newest message time) for my threads that
    have no digest from `model`, or a stale one, newest first."""
    with connection.cursor() as c:
        c.execute(f"""
            WITH my_threads AS ({MY_THREADS})
            SELECT m.thread_id, COUNT(*), MAX(m.internal_date)
            FROM src_google_gmailmessage m
            WHERE m.thread_id IN (SELECT thread_id FROM my_threads) AND m.deleted_at IS NULL AND {NOT_NEVER}
            GROUP BY m.thread_id ORDER BY MAX(m.internal_date) DESC""")
        threads = c.fetchall()
    have = {t: (last, err, n) for t, last, err, n in
            ThreadDigest.objects.filter(model=model).values_list("thread_id", "last_message_at", "error", "attempts")}
    out = []
    for thread_id, count, last in threads:
        last = _aware(last)
        seen = have.get(thread_id)
        if seen is None:
            out.append((thread_id, count, last))
            continue
        seen_last, err, attempts = seen
        if err and attempts < MAX_ATTEMPTS:
            out.append((thread_id, count, last))
        elif not err and last and (seen_last is None or last > seen_last):
            out.append((thread_id, count, last))
    return out


def _aware(value):
    if value is None or hasattr(value, "tzinfo"):
        return value
    dt = parse_datetime(str(value))
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise Disabled(f"another run holds {path.name}") from None
    try:
        yield
    finally:
        lock.close()


class MailDigestLoader(Loader):
    name = "maildigest"
    cadence = "hourly"
    clock = staticmethod(time.monotonic)

    def __init__(self, budget_seconds=None, client=None, model=None, enabled=None, limit=None, threads=None):
        self.enabled = enabled
        self.budget_seconds = budget_seconds if budget_seconds is not None else settings.MAILDIGEST_RUN_SECONDS
        self.model = model or settings.MAILDIGEST_MODEL
        self.client = client
        self.limit = limit
        self.only_threads = threads
        self.counts = {}

    def out_of_time(self) -> bool:
        return self.budget_seconds > 0 and self.clock() >= self.deadline

    def load(self, full: bool = False) -> int:
        if not (settings.MAILDIGEST_ENABLED if self.enabled is None else self.enabled):
            raise Disabled("set MAILDIGEST_ENABLED=1 for hourly runs (after a first manage.py maildigest)")
        self.deadline = self.clock() + self.budget_seconds
        self.counts = dict(threads=0, failed=0)
        client = self.client or LocalLlm(settings.MAILDIGEST_URL, self.model)
        with file_lock(settings.BASE_DIR / "var" / ".maildigest.lock"):
            try:
                self.digest_pass(client)
            finally:
                self.record()
        return self.counts["threads"]

    def digest_pass(self, client):
        if self.only_threads:
            todo = []
            for t in self.only_threads:
                msgs = thread_messages(t)
                todo.append((t, len(msgs), msgs[-1].internal_date if msgs else None))
        else:
            todo = pending_threads(self.model)
        self.counts["pending_at_start"] = len(todo)
        who, mine = owner()
        system = SYSTEM.format(owner=who)
        for thread_id, count, last in todo:
            if self.out_of_time() or (self.limit and self.counts["threads"] + self.counts["failed"] >= self.limit):
                break
            messages = thread_messages(thread_id)
            if not messages:
                continue
            text = render_thread(messages, mine, settings.MAILDIGEST_MAX_CHARS)
            row = ThreadDigest.objects.filter(thread_id=thread_id, model=self.model).first() or ThreadDigest(
                thread_id=thread_id, model=self.model)
            row.prompt_version, row.message_count, row.last_message_at = PROMPT_VERSION, len(messages), last
            row.input_chars, row.attempts = len(text), row.attempts + 1
            started = time.monotonic()
            try:
                reply, usage = client.chat_json(system, text, SCHEMA)
            except LlmUnavailable as exc:
                self.counts["stopped"] = str(exc)
                return
            except Exception as exc:  # this thread, not the server
                row.error = f"{type(exc).__name__}: {exc}"[:500]
                row.seconds = time.monotonic() - started
                row.save()
                self.counts["failed"] += 1
                continue
            row.seconds = time.monotonic() - started
            row.digest = reply
            row.summary = str(reply.get("summary", ""))[:2000]
            row.topic = str(reply.get("topic", ""))[:40]
            row.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            row.completion_tokens = int(usage.get("completion_tokens") or 0)
            row.error, row.attempts = "", 0
            row.save()
            self.counts["threads"] += 1

    def record(self):
        for k, v in self.counts.items():
            self.state_set(k, str(v))
        self.state_set("model", self.model)
        self.state_set("digests", str(ThreadDigest.objects.filter(model=self.model, error="").count()))


def digest_text(d: ThreadDigest) -> str:
    """What gets embedded for a digest: the summary and the names and words
    someone might search by."""
    j = d.digest or {}
    parts = [d.summary, f"Topic: {d.topic}"]
    for key, label in (("people", "People"), ("organizations", "Organizations"), ("keywords", "Keywords")):
        if j.get(key):
            parts.append(f"{label}: " + ", ".join(map(str, j[key])))
    if j.get("open_question"):
        parts.append(f"Open: {j['open_question']}")
    return "\n".join(p for p in parts if p)
