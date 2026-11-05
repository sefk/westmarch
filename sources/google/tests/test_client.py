"""GoogleClient's error mapping, batch retry, and the token file. No network."""

import base64
import json
import stat

import httplib2
import pytest
from googleapiclient.errors import HttpError

from sources.google import auth
from sources.google import client as client_mod
from sources.google.client import ApiDisabled, Gone, GoogleClient, NotFound, RateLimited, translate


def http_error(status, reason="", message="m"):
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


def test_translate():
    assert isinstance(translate(http_error(404)), NotFound)
    assert isinstance(translate(http_error(410, "fullSyncRequired")), Gone)
    assert isinstance(translate(http_error(429)), RateLimited)
    assert isinstance(translate(http_error(403, "userRateLimitExceeded")), RateLimited)
    msg = ("Gmail API has not been used in project 123 before or it is disabled. Enable it by visiting "
           "https://console.developers.google.com/apis/api/gmail.googleapis.com/overview?project=123 then retry.")
    exc = translate(http_error(403, "accessNotConfigured", msg), "gmail")
    assert isinstance(exc, ApiDisabled)
    assert exc.url == "https://console.developers.google.com/apis/api/gmail.googleapis.com/overview?project=123"
    exc = translate(http_error(403, "accessNotConfigured", "disabled"), "tasks")
    assert exc.url == "https://console.cloud.google.com/apis/library/tasks.googleapis.com"


class FakeRequest:
    def __init__(self, mid):
        self.mid = mid


class FakeBatch:
    def __init__(self, svc, callback):
        self.svc, self.callback, self.items = svc, callback, []

    def add(self, request, request_id):
        self.items.append((request_id, request))

    def execute(self):
        self.svc.batches.append([rid for rid, _ in self.items])
        for rid, _ in self.items:
            fails = self.svc.failures.get(rid, 0)
            if rid == "gone":
                self.callback(rid, None, http_error(404))
            elif fails:
                self.svc.failures[rid] -= 1
                self.callback(rid, None, http_error(429))
            else:
                raw = base64.urlsafe_b64encode(f"mail {rid}".encode()).decode()
                self.callback(rid, {"id": rid, "raw": raw}, None)


class FakeGmail:
    def __init__(self, failures):
        self.failures, self.batches = failures, []

    def new_batch_http_request(self, callback):
        return FakeBatch(self, callback)

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format):
        assert format == "raw"
        return FakeRequest(id)


def test_batch_retries_rate_limited_items(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client_mod, "sleep", sleeps.append)
    c = GoogleClient(creds=object())
    c._services["gmail"] = FakeGmail({"b": 2})
    out = c.gmail_get_raw(["a", "b", "gone"])
    assert out["a"]["raw"] == b"mail a" and out["b"]["raw"] == b"mail b" and out["gone"] is None
    assert c._services["gmail"].batches == [["a", "b", "gone"], ["b"], ["b"]]
    assert [s for s in sleeps if s in (5, 10)] == [5, 10]  # MIN_BACKOFF, doubled


def test_save_token_is_private(settings, tmp_path):
    settings.GOOGLE_TOKEN_FILE = tmp_path / "secrets" / "google-token.json"

    class Creds:
        def to_json(self):
            return json.dumps({"token": "x", "refresh_token": "y"})

    auth._save(Creds(), issued_at=123.0)
    mode = stat.S_IMODE(settings.GOOGLE_TOKEN_FILE.stat().st_mode)
    assert mode == 0o600
    assert json.loads(settings.GOOGLE_TOKEN_FILE.read_text())["issued_at"] == 123.0
    auth._save(Creds())  # a refresh keeps the consent stamp
    assert json.loads(settings.GOOGLE_TOKEN_FILE.read_text())["issued_at"] == 123.0


def test_no_token(settings, tmp_path):
    settings.GOOGLE_TOKEN_FILE = tmp_path / "none.json"
    with pytest.raises(auth.NoToken, match="run: manage.py google_auth"):
        auth.load_credentials()


def test_throttle_paces_to_the_per_minute_quota(monkeypatch, settings):
    settings.GMAIL_UNITS_PER_MINUTE, settings.GMAIL_GET_RAW_UNITS = 6000, 60
    sleeps = []
    monkeypatch.setattr(client_mod, "sleep", sleeps.append)
    c = GoogleClient(creds=object())
    c._throttle(60 * 25)  # one batch of 25 raw gets: free, the bucket was empty
    c._throttle(60 * 25)  # the next must wait about 25 messages' worth: 25 / 90 per minute
    assert not sleeps[:-1] and 16 < sleeps[-1] < 17


def test_network_errors_are_retried(monkeypatch):
    monkeypatch.setattr(client_mod, "sleep", lambda s: None)

    class Flaky:
        calls = 0

        def execute(self):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise OSError(65, "No route to host")
            return {"ok": True}

    assert GoogleClient(creds=object())._exec(Flaky(), "gmail") == {"ok": True}
    assert Flaky.calls == 3
