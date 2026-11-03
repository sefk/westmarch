import json
import sqlite3
import subprocess
import time
from unittest import mock

import pytest

from core.loaders import run_loader
from core.models import LoaderRun
from sources.limits import loader as L
from sources.limits.models import LimitSnapshot, Subscription

pytestmark = pytest.mark.django_db

FAKE_TOKEN = "test-token-not-real"

CLAUDE_USAGE = {
    "five_hour": {"utilization": 13.0, "resets_at": "2026-11-03T05:29:59.934299+00:00", "limit_dollars": None},
    "seven_day": {"utilization": 3.0, "resets_at": "2026-11-09T21:59:59+00:00"},
    "seven_day_sonnet": None,
    "iguana_necktie": {"utilization": 0.0, "resets_at": None, "limit_dollars": 250, "used_dollars": 0.0},
    "extra_usage": {"is_enabled": False, "utilization": None},
    "limits": [
        {"kind": "session", "percent": 13, "scope": None},
        {"kind": "weekly_scoped", "percent": 4, "resets_at": "2026-11-09T21:59:59+00:00",
         "scope": {"model": {"display_name": "Sonnet"}}},
    ],
}


def keychain(expires_in=3600, returncode=0):
    payload = json.dumps({"claudeAiOauth": {"accessToken": FAKE_TOKEN, "expiresAt": (time.time() + expires_in) * 1000}})
    return mock.patch.object(L.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, payload, ""))


def http(*bodies, status=200):
    return mock.patch.object(L.requests, "get", side_effect=[mock.Mock(status_code=status, json=lambda b=b: b) for b in bodies])


@pytest.fixture
def no_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(L.ClaudeLimitsLoader, "monitor_db", tmp_path / "absent.db")


def test_subscriptions_seeded():
    assert Subscription.objects.get(vendor="claude").monthly_usd == 100
    assert Subscription.objects.get(vendor="gemini").shared
    assert Subscription.objects.get(vendor="openrouter").monthly_usd is None


def test_claude_appends_one_row_per_window(no_monitor):
    with keychain(), http(CLAUDE_USAGE) as get:
        run = run_loader(L.ClaudeLimitsLoader())
    assert run.ok and run.rows == 4
    assert get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    snaps = {s.window: s for s in LimitSnapshot.objects.filter(vendor="claude")}
    assert set(snaps) == {"session_5h", "weekly_all", "weekly_sonnet", "iguana_necktie"}
    assert snaps["session_5h"].utilization_pct == 13.0
    assert snaps["session_5h"].resets_at.hour == 5
    assert snaps["iguana_necktie"].limit_usd == 250
    assert FAKE_TOKEN not in json.dumps([s.raw for s in snaps.values()]) + run.message


def test_claude_missing_keychain_is_disabled(no_monitor):
    with keychain(returncode=44):
        run = run_loader(L.ClaudeLimitsLoader())
    assert run.ok and run.message.startswith("disabled") and "Keychain" in run.message


def test_claude_expired_token_is_disabled_not_refreshed(no_monitor):
    with keychain(expires_in=-60), http() as get:
        run = run_loader(L.ClaudeLimitsLoader())
    assert run.ok and "expired" in run.message
    get.assert_not_called()


def test_claude_http_error_fails_without_leaking(no_monitor):
    with keychain(), http({}, status=401):
        run = run_loader(L.ClaudeLimitsLoader())
    assert run.ok is False and "401" in run.message and FAKE_TOKEN not in run.message


def make_monitor_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE usage_history (id INTEGER PRIMARY KEY, account_id TEXT, timestamp TEXT, primary_percent REAL,"
        " session_percent REAL, weekly_all_percent REAL, weekly_sonnet_percent REAL, session_reset TEXT,"
        " weekly_reset TEXT, raw_data TEXT, is_synthetic INTEGER DEFAULT 0)"
    )
    rows = [
        ("2026-03-30T19:34:29Z", 23.0, 33.0, 0.0, '{"ping_status":200}', 0),
        ("2026-03-30T19:34:29Z", 23.0, 33.0, 0.0, '{"ping_status":200}', 0),  # duplicate timestamp
        ("2026-03-29T10:00:00Z", 5.0, 30.0, 12.0, "{}", 0),  # pre-ping era: has Sonnet
        ("2026-04-02T05:44:46Z", 28.0, 65.0, 0.0, "", 1),  # synthetic, skipped
    ]
    con.executemany(
        "INSERT INTO usage_history (account_id, timestamp, session_percent, weekly_all_percent,"
        " weekly_sonnet_percent, session_reset, weekly_reset, raw_data, is_synthetic)"
        " VALUES ('a', ?, ?, ?, ?, '2026-03-31T00:00:00Z', '2026-04-03T16:00:00Z', ?, ?)", rows,
    )
    con.commit()
    con.close()


def test_claude_monitor_backfill_runs_once(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    make_monitor_db(db)
    monkeypatch.setattr(L.ClaudeLimitsLoader, "monitor_db", db)
    with keychain(), http(CLAUDE_USAGE, CLAUDE_USAGE):
        first = run_loader(L.ClaudeLimitsLoader())
        second = run_loader(L.ClaudeLimitsLoader())
    backfilled = LimitSnapshot.objects.filter(source="claude-monitor")
    assert backfilled.count() == 5  # 2 + 3 windows; dup and synthetic dropped
    assert not backfilled.filter(window="weekly_sonnet", raw__ping_status=200).exists()
    assert first.rows == 4 + 5
    assert second.rows <= 4  # live rows only (0 if within the same second)


OR_CREDITS = {"total_credits": 5, "total_usage": 4.5}
OR_KEY = {
    "label": "sk-or-v1-abc...def", "usage_monthly": 0.0086,
    "budgets": [{"scope": "workspace", "limit_usd": 20, "spend_usd": 5, "resets_at": "2026-12-01T00:00:00.000Z"}],
}


def test_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_TOKEN)
    with http({"data": OR_CREDITS}, {"data": OR_KEY}):
        run = run_loader(L.OpenRouterLimitsLoader())
    assert run.ok and run.rows == 3
    snaps = {s.window: s for s in LimitSnapshot.objects.filter(vendor="openrouter")}
    assert snaps["credits"].utilization_pct == 90.0
    assert snaps["budget_workspace"].utilization_pct == 25.0 and snaps["budget_workspace"].resets_at.month == 12
    assert snaps["spend_month"].used_usd == 0.0086 and snaps["spend_month"].utilization_pct is None
    assert "label" not in snaps["spend_month"].raw


def test_openrouter_without_key_is_disabled(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert run_loader(L.OpenRouterLimitsLoader()).message.startswith("disabled")


def codex_event(ts, pct):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": "codex", "primary": {"used_percent": pct, "window_minutes": 10080, "resets_at": 1793915358},
        "secondary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1793900000},
    }}})


def test_codex_reads_session_logs_incrementally(tmp_path, monkeypatch):
    day = tmp_path / "2026" / "11" / "02"
    day.mkdir(parents=True)
    log = day / "rollout-a.jsonl"
    log.write_text("\n".join([
        json.dumps({"timestamp": "2026-11-02T19:00:00Z", "type": "session_meta", "payload": {}}),
        codex_event("2026-11-02T19:55:32.840Z", 3.0),
        codex_event("2026-11-02T20:00:00Z", 4.0),
    ]) + "\n")
    monkeypatch.setattr(L.CodexLimitsLoader, "sessions", tmp_path)
    assert run_loader(L.CodexLimitsLoader()).rows == 4
    assert run_loader(L.CodexLimitsLoader()).rows == 0  # file unchanged: not reread
    weekly = LimitSnapshot.objects.filter(vendor="codex", window="weekly_all").order_by("taken_at")
    assert [s.utilization_pct for s in weekly] == [3.0, 4.0]
    assert LimitSnapshot.objects.filter(vendor="codex", window="session_5h").count() == 2
    assert run_loader(L.CodexLimitsLoader(), full=True).rows == 0  # full rerun is idempotent


def test_codex_without_sessions_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(L.CodexLimitsLoader, "sessions", tmp_path / "nope")
    assert run_loader(L.CodexLimitsLoader()).message.startswith("disabled")
    assert LoaderRun.objects.count() == 1
