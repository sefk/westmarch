"""Thin wrappers over the Google discovery clients.

Loaders call plain methods on `GoogleClient`, so tests swap in a fake with
the same methods and never touch the network. This module owns retries,
quota pacing and turning HttpErrors into a few meaningful exceptions.

Gmail quota: messages.get and messages.list cost 5 units, the per-user
limit is 250 units/second. Batches stay at 50 requests (250 units), and
`_throttle` paces calls to stay under the per-second budget; 429s and
403 rateLimitExceeded back off exponentially.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time

from .auth import load_credentials, service

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
UNITS_PER_SECOND = 250
GET_UNITS = 5
LIST_UNITS = 5
MAX_TRIES = 8
MAX_BACKOFF = 64

sleep = time.sleep  # patched in tests

RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class GoogleApiError(Exception):
    """An HTTP error from a Google API, reduced to status/reason/message."""

    def __init__(self, status: int, reason: str = "", message: str = ""):
        super().__init__(f"HTTP {status} {reason}: {message}".strip())
        self.status, self.reason, self.message = status, reason, message


class NotFound(GoogleApiError):
    """404: the message/calendar/history id doesn't exist (any more)."""


class Gone(GoogleApiError):
    """410: a sync token expired; do a full sync."""


class ApiDisabled(GoogleApiError):
    """403 accessNotConfigured: the API isn't enabled in the Cloud project."""

    def __init__(self, status, reason, message, url):
        super().__init__(status, reason, message)
        self.url = url


class RateLimited(GoogleApiError):
    pass


API_HOSTS = {"gmail": "gmail.googleapis.com", "calendar": "calendar-json.googleapis.com",
             "tasks": "tasks.googleapis.com"}


def enable_url(api: str, message: str = "") -> str:
    """The console URL that enables `api`; Google's own message carries one with the project."""
    m = re.search(r"https://console\.(?:developers|cloud)\.google\.com/\S+", message or "")
    if m:
        return m.group(0).rstrip(".")
    return f"https://console.cloud.google.com/apis/library/{API_HOSTS.get(api, api)}"


def translate(exc, api: str = ""):
    """HttpError -> one of our exceptions. Anything else passes through."""
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return exc
    status = int(getattr(exc.resp, "status", 0) or 0)
    reason, message = "", ""
    try:
        err = json.loads(exc.content.decode("utf-8", "replace")).get("error", {})
        message = err.get("message", "") if isinstance(err, dict) else str(err)
        errors = err.get("errors") or [] if isinstance(err, dict) else []
        reason = errors[0].get("reason", "") if errors else ""
        if not reason and isinstance(err, dict):
            for d in err.get("details") or []:
                reason = d.get("reason", "") or reason
    except (ValueError, AttributeError):
        message = getattr(exc, "reason", "") or ""
    if status == 404:
        return NotFound(status, reason, message)
    if status == 410:
        return Gone(status, reason, message)
    if status == 429 or (status == 403 and reason in RATE_REASONS):
        return RateLimited(status, reason, message)
    if status == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return ApiDisabled(status, reason, message, enable_url(api, message))
    return GoogleApiError(status, reason, message)


def retryable(exc) -> bool:
    return isinstance(exc, RateLimited) or (isinstance(exc, GoogleApiError) and exc.status >= 500)


class GoogleClient:
    def __init__(self, creds=None):
        self._creds = creds
        self._services = {}
        self._next_ok = 0.0
        self.calls = 0

    def _svc(self, name, version):
        if name not in self._services:
            if self._creds is None:
                self._creds = load_credentials()
            self._services[name] = service(name, version, self._creds)
        return self._services[name]

    def _throttle(self, units: int) -> None:
        """Pace so the moving average stays under UNITS_PER_SECOND."""
        now = time.monotonic()
        if self._next_ok > now:
            sleep(self._next_ok - now)
        self._next_ok = max(now, self._next_ok) + units / UNITS_PER_SECOND

    def _exec(self, request, api: str, units: int = 1):
        delay = 1
        for attempt in range(MAX_TRIES):
            self._throttle(units)
            self.calls += 1
            try:
                return request.execute()
            except Exception as raw:
                exc = translate(raw, api)
                if not retryable(exc) or attempt == MAX_TRIES - 1:
                    raise exc from None
                logger.info("google %s: %s; retrying in %ss", api, exc, delay)
                sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)

    # -- Gmail -------------------------------------------------------------

    @property
    def gmail(self):
        return self._svc("gmail", "v1")

    def gmail_profile(self) -> dict:
        return self._exec(self.gmail.users().getProfile(userId="me"), "gmail")

    def gmail_labels(self) -> list[dict]:
        return self._exec(self.gmail.users().labels().list(userId="me"), "gmail").get("labels", [])

    def gmail_list(self, page_token: str | None, include_spam_trash: bool = False) -> tuple[list[str], str | None]:
        """One page of message ids (newest first) and the next page token."""
        resp = self._exec(self.gmail.users().messages().list(
            userId="me", maxResults=500, pageToken=page_token or None,
            includeSpamTrash=include_spam_trash), "gmail", LIST_UNITS)
        return [m["id"] for m in resp.get("messages", [])], resp.get("nextPageToken")

    def gmail_get_raw(self, ids: list[str]) -> dict[str, dict | None]:
        """Fetch format=raw for up to BATCH_SIZE ids in one batch request.

        Returns {id: message} with message["raw"] decoded to bytes, or
        {id: None} for ids that 404 (deleted since listing). Rate-limited
        items are retried with exponential backoff."""
        assert len(ids) <= BATCH_SIZE
        gmail = self.gmail
        pending, out, delay = list(ids), {}, 1
        for attempt in range(MAX_TRIES):
            results = {}

            def cb(request_id, response, exception, results=results):
                results[request_id] = (response, exception)

            batch = gmail.new_batch_http_request(callback=cb)
            for i in pending:
                batch.add(gmail.users().messages().get(userId="me", id=i, format="raw"), request_id=i)
            self._throttle(GET_UNITS * len(pending))
            self.calls += 1
            try:
                batch.execute()
            except Exception as raw:
                exc = translate(raw, "gmail")
                if not retryable(exc):
                    raise exc from None
                results = {i: (None, exc) for i in pending}
            retry = []
            for i in pending:
                response, exception = results.get(i, (None, RateLimited(0, "missing", "no batch response")))
                if exception is None:
                    response["raw"] = base64.urlsafe_b64decode(response["raw"])
                    out[i] = response
                    continue
                exc = translate(exception, "gmail")
                if isinstance(exc, NotFound):
                    out[i] = None
                elif retryable(exc):
                    retry.append(i)
                else:
                    raise exc
            if not retry:
                return out
            logger.info("gmail batch: %d of %d rate limited; retrying in %ss", len(retry), len(pending), delay)
            sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)
            pending = retry
        raise RateLimited(429, "rateLimitExceeded", f"{len(pending)} messages still rate limited")

    def gmail_history(self, start_history_id: str) -> tuple[list[dict], str]:
        """All history records since `start_history_id`, and the mailbox's
        current historyId. Raises NotFound when the start id is too old."""
        records, token, latest = [], None, start_history_id
        while True:
            resp = self._exec(self.gmail.users().history().list(
                userId="me", startHistoryId=start_history_id, pageToken=token, maxResults=500,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]), "gmail", 2)
            records.extend(resp.get("history", []))
            latest = resp.get("historyId", latest)
            token = resp.get("nextPageToken")
            if not token:
                return records, latest

    # -- Calendar ----------------------------------------------------------

    @property
    def calendar(self):
        return self._svc("calendar", "v3")

    def calendars(self) -> list[dict]:
        out, token = [], None
        while True:
            resp = self._exec(self.calendar.calendarList().list(
                pageToken=token, showHidden=True, maxResults=250), "calendar")
            out.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def calendar_events(self, calendar_id: str, sync_token: str | None) -> tuple[list[dict], str | None]:
        """Every event (full sync) or every change since `sync_token`, and
        the next sync token. Raises Gone when the token has expired."""
        items, token, next_sync = [], None, None
        while True:
            params = dict(calendarId=calendar_id, pageToken=token, maxResults=2500,
                          singleEvents=False, showDeleted=True)
            if sync_token:
                params["syncToken"] = sync_token
            resp = self._exec(self.calendar.events().list(**params), "calendar")
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            next_sync = resp.get("nextSyncToken", next_sync)
            if not token:
                return items, next_sync

    # -- Tasks -------------------------------------------------------------

    @property
    def tasks(self):
        return self._svc("tasks", "v1")

    def tasklists(self) -> list[dict]:
        out, token = [], None
        while True:
            resp = self._exec(self.tasks.tasklists().list(pageToken=token, maxResults=100), "tasks")
            out.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def tasks_in(self, list_id: str, updated_min: str | None) -> list[dict]:
        out, token = [], None
        while True:
            params = dict(tasklist=list_id, pageToken=token, maxResults=100,
                          showCompleted=True, showHidden=True, showDeleted=True)
            if updated_min:
                params["updatedMin"] = updated_min
            resp = self._exec(self.tasks.tasks().list(**params), "tasks")
            out.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                return out
