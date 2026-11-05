"""Google loaders: Gmail (full mailbox mirror), Calendar, Tasks.

All three are read-only (settings.GOOGLE_SCOPES) and report Disabled
until `manage.py google_auth` has written a token.

Gmail
  Raw RFC 822 bytes go gzip-compressed to RAW_DIR/gmail/<xx>/<id>.eml.gz;
  SQLite holds parsed headers. Two phases:
  - Backfill: page messages.list (newest first) and batch-fetch every id
    not yet stored. Each run is time-boxed (GMAIL_RUN_SECONDS) and saves
    the list page token after each page, so hourly runs resume where the
    last stopped. The profile's historyId is recorded before the first
    page so the incremental sync later covers changes made meanwhile.
  - Incremental: history.list from the stored historyId: new messages
    are fetched, label changes update the row, deletions set deleted_at
    (row and file are kept; this is a backup). A 404 (history too old)
    falls back to a list-based reconcile.
  `full=True` re-walks the whole id list and fetches anything missing,
  without re-downloading what's stored.

Calendar: every calendar, all events (recurring masters, not expanded
instances), per-calendar sync tokens; 410 Gone resets to a full sync.

Tasks: every list, completed/hidden/deleted included; `updatedMin`
since the last run, everything on `full=True`.
"""

from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.loaders import Disabled, Loader

from .auth import GoogleAuthError
from .client import BATCH_SIZE, ApiDisabled, Gone, GoogleApiError, GoogleClient, NotFound
from .models import GCalCalendar, GCalEvent, GmailLabel, GmailMessage, GTask, GTaskList
from .parse import parse_headers, write_raw

logger = logging.getLogger(__name__)

GMAIL_RUN_SECONDS = 50 * 60  # most of the hour: the quota, not the time box, is the limit
SKIP_ADD_LABELS = {"DRAFT", "SPAM"}  # draft revisions churn; spam isn't in the backfill either


def get_client(loader) -> GoogleClient:
    if loader.client is not None:
        return loader.client
    try:
        from .auth import load_credentials

        return GoogleClient(load_credentials())
    except GoogleAuthError as exc:
        raise Disabled(str(exc)) from None


def api_disabled(exc: ApiDisabled):
    return Disabled(f"API not enabled in the Google Cloud project; enable it at {exc.url}")


def _dt(value) -> datetime | None:
    if not value:
        return None
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _ms(value) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, UTC) if value else None


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


@contextmanager
def gmail_lock(root):
    """One quota-spending gmail run at a time: the hourly job and a
    hand-started run (or label sync) would otherwise fight over the page
    token and split the quota. Raises Disabled if another run holds it."""
    lock = open(root / ".gmail.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise Disabled("another gmail run is in progress") from None
    try:
        yield
    finally:
        lock.close()


def gmail_root():
    """settings.RAW_DIR, created; Disabled if its volume isn't mounted."""
    root = settings.RAW_DIR
    if not root.parent.exists():
        raise Disabled(f"{root.parent} not mounted; raw mail lives in {root}")
    root.mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------


class GmailLoader(Loader):
    name = "gmail"
    cadence = "hourly"
    client = None  # tests inject a fake
    budget_seconds = GMAIL_RUN_SECONDS
    clock = staticmethod(time.monotonic)

    def load(self, full: bool = False) -> int:
        self.root = gmail_root()
        with gmail_lock(self.root):
            client = get_client(self)
            self.deadline = self.clock() + self.budget_seconds
            try:
                return self._load(client, full)
            except ApiDisabled as exc:
                raise api_disabled(exc) from None
            finally:
                self.state_set("messages_stored", str(GmailMessage.objects.count()))
                self.state_set("parse_errors", str(GmailMessage.objects.exclude(parse_error="").count()))

    def _load(self, client, full):
        self.refresh_labels(client)
        profile = client.gmail_profile()
        self.state_set("messages_total", str(profile.get("messagesTotal", "")))
        rows = 0
        if self.state_get("backfill_done") != "1":
            if not self.state_get("history_id"):
                self.state_set("history_id", str(profile["historyId"]))
            n, done = self.walk(client)
            rows += n
            if done:
                self.state_set("backfill_done", "1")
                self.state_set("backfill_finished", timezone.now().isoformat())
            return rows
        rows += self.incremental(client, profile)
        if full:
            self.state_set("rewalk", "1")
            self.state_set("backfill_page_token", "")
        if self.state_get("rewalk") == "1":
            n, done = self.walk(client)
            rows += n
            if done:
                self.state_set("rewalk", "0")
        return rows

    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def refresh_labels(self, client):
        for lb in client.gmail_labels():
            GmailLabel.objects.update_or_create(label_id=lb["id"], defaults=dict(
                name=lb.get("name", ""), type=lb.get("type", "")))

    # -- listing -----------------------------------------------------------

    def walk(self, client) -> tuple[int, bool]:
        """Page the id list from the saved token, fetching ids not stored.
        Returns (rows, finished). Saves the page token after each page."""
        token = self.state_get("backfill_page_token") or None
        rows = 0
        while not self.out_of_time():
            try:
                ids, nxt = client.gmail_list(token)
            except GoogleApiError as exc:
                if token and exc.status == 400:  # stale page token: start over, stored ids are skipped
                    logger.warning("gmail: page token rejected (%s); restarting the list walk", exc.status)
                    token = None
                    self.state_set("backfill_page_token", "")
                    continue
                raise
            n, complete = self.fetch_missing(client, ids)
            rows += n
            if not complete:
                return rows, False  # out of time mid-page; it's re-listed next run
            token = nxt
            self.state_set("backfill_page_token", token or "")
            self.state_set("messages_stored", str(GmailMessage.objects.count()))
            if not token:
                return rows, True
        return rows, False

    def fetch_missing(self, client, ids) -> tuple[int, bool]:
        have = set(GmailMessage.objects.filter(gmail_id__in=ids).values_list("gmail_id", flat=True))
        missing = [i for i in ids if i not in have]
        return self.fetch(client, missing)

    def fetch(self, client, ids) -> tuple[int, bool]:
        """Fetch and store `ids` in batches until the time box runs out.
        Returns (rows, all fetched). Ids that 404 count as fetched."""
        rows = 0
        for chunk in _chunks(ids, BATCH_SIZE):
            if self.out_of_time():
                return rows, False
            got = client.gmail_get_raw(chunk)
            for gid in chunk:
                if got.get(gid):
                    self.store(got[gid])
                    rows += 1
        return rows, True

    def store(self, m: dict) -> GmailMessage:
        raw = m["raw"]
        rel = write_raw(self.root, m["id"], raw)
        fields = parse_headers(raw)
        if fields["parse_error"]:
            logger.info("gmail %s: header parse problems: %s", m["id"], fields["parse_error"])
        obj, _ = GmailMessage.objects.update_or_create(gmail_id=m["id"], defaults=dict(
            thread_id=m.get("threadId", ""), history_id=int(m["historyId"]) if m.get("historyId") else None,
            internal_date=_ms(m.get("internalDate")), label_ids=m.get("labelIds", []),
            size_estimate=m.get("sizeEstimate"), snippet=m.get("snippet", ""),
            raw_path=rel, deleted_at=None, **fields))
        return obj

    # -- incremental -------------------------------------------------------

    def incremental(self, client, profile) -> int:
        start = self.state_get("history_id")
        try:
            records, latest = client.gmail_history(start)
        except NotFound:
            logger.warning("gmail: history id %s too old; reconciling by list", start)
            return self.reconcile(client, profile)

        to_fetch: dict[str, None] = {}  # ordered set
        labels: dict[str, list] = {}
        deleted: set[str] = set()
        for rec in records:
            for item in rec.get("messagesAdded", []):
                m = item["message"]
                deleted.discard(m["id"])
                if not SKIP_ADD_LABELS & set(m.get("labelIds", [])):
                    to_fetch[m["id"]] = None
            for key in ("labelsAdded", "labelsRemoved"):
                for item in rec.get(key, []):
                    m = item["message"]
                    labels[m["id"]] = m.get("labelIds", [])
            for item in rec.get("messagesDeleted", []):
                mid = item["message"]["id"]
                deleted.add(mid)
                to_fetch.pop(mid, None)
                labels.pop(mid, None)

        known = set(GmailMessage.objects.filter(
            gmail_id__in=list(to_fetch) + list(labels)).values_list("gmail_id", flat=True))
        # A label change on a message we never stored (a draft that was sent, or
        # one missed earlier): fetch it, unless it's still a draft or spam.
        for mid, lbls in labels.items():
            if mid not in known and not SKIP_ADD_LABELS & set(lbls):
                to_fetch[mid] = None
        fetch_ids = [i for i in to_fetch if i not in known]
        rows, complete = self.fetch(client, fetch_ids)

        fetched = set(fetch_ids)
        with transaction.atomic():
            for mid, lbls in labels.items():
                if mid in fetched:
                    continue  # just fetched: its labels are already current
                rows += GmailMessage.objects.filter(gmail_id=mid).exclude(label_ids=lbls).update(
                    label_ids=lbls, updated=timezone.now())
            rows += GmailMessage.objects.filter(gmail_id__in=deleted, deleted_at__isnull=True).update(
                deleted_at=timezone.now())
        if complete:  # otherwise replay the same window next run (idempotent)
            self.state_set("history_id", str(latest))
        return rows

    def reconcile(self, client, profile) -> int:
        """History expired: mark stored messages absent from the full list
        (spam and trash included) as deleted, then re-walk to fetch what's
        missing. Labels of existing rows are not refreshed here."""
        self.state_set("history_id", str(profile["historyId"]))
        present, token = set(), None
        while True:
            ids, token = client.gmail_list(token, include_spam_trash=True)
            present.update(ids)
            if not token:
                break
        stored = set(GmailMessage.objects.filter(deleted_at__isnull=True).values_list("gmail_id", flat=True))
        gone = stored - present
        rows = 0
        for chunk in _chunks(gone, 500):
            rows += GmailMessage.objects.filter(gmail_id__in=chunk).update(deleted_at=timezone.now())
        self.state_set("rewalk", "1")
        self.state_set("backfill_page_token", "")
        self.state_set("reconciled", timezone.now().isoformat())
        return rows

    # -- label sync ----------------------------------------------------------

    def sync_labels(self) -> dict:
        """Refresh label_ids on every stored row and mark rows gone from
        Gmail as deleted, by listing ids only (5 units per 500), never
        fetching messages. Meant for after a Takeout import, whose rows
        carry label names but no label ids. Takes the gmail lock."""
        root = gmail_root()
        with gmail_lock(root):
            client = get_client(self)
            try:
                return self._sync_labels(client)
            except ApiDisabled as exc:
                raise api_disabled(exc) from None

    def _list_all(self, client, label_id=None) -> set[str]:
        ids, token = set(), None
        while True:
            page, token = client.gmail_list(token, include_spam_trash=True, label_ids=[label_id] if label_id else None)
            ids.update(page)
            if not token:
                return ids

    def _sync_labels(self, client) -> dict:
        self.refresh_labels(client)
        present = self._list_all(client)
        by_id: dict[str, list[str]] = {}
        for label_id in GmailLabel.objects.order_by("label_id").values_list("label_id", flat=True):
            for mid in self._list_all(client, label_id):
                by_id.setdefault(mid, []).append(label_id)

        now = timezone.now()
        stored = list(GmailMessage.objects.values_list("pk", "gmail_id", "label_ids", "deleted_at"))
        stored_ids = {gid for _, gid, _, _ in stored}
        changed, gone, back = [], [], []
        for pk, gid, labels, deleted_at in stored:
            if gid not in present:
                if deleted_at is None:
                    gone.append(pk)  # keep its last known labels
                continue
            if deleted_at is not None:
                back.append(pk)
            new = by_id.get(gid, [])
            if sorted(labels or []) != new:
                changed.append(GmailMessage(pk=pk, label_ids=new, updated=now))
        for chunk in _chunks(changed, 500):
            with transaction.atomic():
                GmailMessage.objects.bulk_update(chunk, ["label_ids", "updated"])
        for pks, value in ((gone, now), (back, None)):
            for chunk in _chunks(pks, 500):
                GmailMessage.objects.filter(pk__in=chunk).update(deleted_at=value, updated=now)

        result = dict(present=len(present), stored=len(stored), labels_updated=len(changed),
                      deleted=len(gone), undeleted=len(back), missing=len(present - stored_ids))
        self.state_set("missing_after_takeout", str(result["missing"]))
        self.state_set("labels_synced", now.isoformat())
        logger.info("gmail label sync: %s", result)
        return result


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def _event_time(value: dict | None, tz) -> tuple[datetime | None, bool, str]:
    """(aware datetime, all_day, original string) from an event start/end."""
    if not value:
        return None, False, ""
    if value.get("dateTime"):
        return _dt(value["dateTime"]), False, value["dateTime"]
    if value.get("date"):
        zone = tz
        if value.get("timeZone"):
            try:
                zone = ZoneInfo(value["timeZone"])
            except ZoneInfoNotFoundError:
                pass
        d = datetime.fromisoformat(value["date"]).replace(tzinfo=zone)
        return d, True, value["date"]
    return None, False, ""


class CalendarLoader(Loader):
    name = "gcal"
    cadence = "hourly"
    client = None

    def load(self, full: bool = False) -> int:
        client = get_client(self)
        try:
            cals = client.calendars()
        except ApiDisabled as exc:
            raise api_disabled(exc) from None
        rows, skipped = 0, []
        for c in cals:
            cal, _ = GCalCalendar.objects.update_or_create(calendar_id=c["id"], defaults=dict(
                summary=c.get("summaryOverride") or c.get("summary", ""), description=c.get("description", ""),
                time_zone=c.get("timeZone", ""), access_role=c.get("accessRole", ""),
                primary=bool(c.get("primary")), raw=c))
            rows += 1
            try:
                rows += self.sync(client, cal, full)
            except (NotFound, GoogleApiError) as exc:
                if isinstance(exc, ApiDisabled) or exc.status not in (403, 404):
                    raise
                skipped.append(f"{cal.calendar_id}: HTTP {exc.status}")
        for s in skipped:
            logger.warning("gcal skipped %s", s)
        return rows

    def sync(self, client, cal, full) -> int:
        key = f"sync_token:{cal.calendar_id}"
        token = None if full else (self.state_get(key) or None)
        try:
            items, next_token = client.calendar_events(cal.calendar_id, token)
        except Gone:
            logger.info("gcal %s: sync token expired; full sync", cal.calendar_id)
            self.state_set(key, "")
            items, next_token = client.calendar_events(cal.calendar_id, None)
        try:
            tz = ZoneInfo(cal.time_zone or settings.TIME_ZONE)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo(settings.TIME_ZONE)
        with transaction.atomic():
            for e in items:
                self.save_event(cal, e, tz)
        if next_token:
            self.state_set(key, next_token)
        return len(items)

    def save_event(self, cal, e, tz):
        if e.get("status") == "cancelled" and "start" not in e:
            # A deleted event (or cancelled instance) arrives as a stub: keep what we had.
            obj, created = GCalEvent.objects.get_or_create(calendar=cal, event_id=e["id"], defaults=dict(
                status="cancelled", recurring_event_id=e.get("recurringEventId", ""),
                updated=_dt(e.get("updated")), raw=e))
            if not created:
                obj.status = "cancelled"
                obj.updated = _dt(e.get("updated")) or obj.updated
                obj.save(update_fields=["status", "updated"])
            return
        start, all_day, start_raw = _event_time(e.get("start"), tz)
        end, _, end_raw = _event_time(e.get("end"), tz)
        GCalEvent.objects.update_or_create(calendar=cal, event_id=e["id"], defaults=dict(
            status=e.get("status", ""), summary=e.get("summary", ""), description=e.get("description", ""),
            location=e.get("location", ""), start=start, end=end, all_day=all_day,
            start_raw=start_raw, end_raw=end_raw, recurrence=e.get("recurrence", []),
            recurring_event_id=e.get("recurringEventId", ""),
            organizer_email=(e.get("organizer") or {}).get("email", ""),
            attendees=[{"email": a.get("email", ""), "responseStatus": a.get("responseStatus", ""),
                        "self": bool(a.get("self"))} for a in e.get("attendees", [])],
            created=_dt(e.get("created")), updated=_dt(e.get("updated")), raw=e))


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

TASKS_OVERLAP = timedelta(hours=1)


class TasksLoader(Loader):
    name = "gtasks"
    cadence = "daily"
    client = None

    def load(self, full: bool = False) -> int:
        client = get_client(self)
        started = timezone.now()
        last = None if full else _dt(self.state_get("updated_min"))
        updated_min = (last - TASKS_OVERLAP).isoformat().replace("+00:00", "Z") if last else None
        try:
            lists = client.tasklists()
            rows = 0
            for tl in lists:
                lst, _ = GTaskList.objects.update_or_create(list_id=tl["id"], defaults=dict(
                    title=tl.get("title", ""), updated=_dt(tl.get("updated")), raw=tl))
                rows += 1
                with transaction.atomic():
                    for t in client.tasks_in(tl["id"], updated_min):
                        self.save_task(lst, t)
                        rows += 1
        except ApiDisabled as exc:
            raise api_disabled(exc) from None
        self.state_set("updated_min", started.isoformat())
        return rows

    def save_task(self, lst, t):
        GTask.objects.update_or_create(tasklist=lst, task_id=t["id"], defaults=dict(
            title=t.get("title", ""), notes=t.get("notes", ""), status=t.get("status", ""),
            due=_dt(t.get("due")), completed=_dt(t.get("completed")), updated=_dt(t.get("updated")),
            parent=t.get("parent", ""), position=t.get("position", ""),
            deleted=bool(t.get("deleted")), hidden=bool(t.get("hidden")), raw=t))
