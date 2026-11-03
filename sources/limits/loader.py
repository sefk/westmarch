"""Subscription rate-limit loaders: Claude, OpenRouter, Codex.

Each run appends one LimitSnapshot per window the vendor reports. Rows
are keyed (vendor, window, taken_at) and inserted with ignore_conflicts,
so reruns and overlapping backfills are harmless.

Credentials are read, used for one request, and dropped: never logged,
stored, or put in a raw blob or an exception message.

- Claude: no public API for plan limits. Claude Code's OAuth token sits in
  the macOS Keychain; the internal endpoint claude.ai/settings/usage uses
  (found via github.com/rjwalters/claude-monitor) returns per-window
  utilization. We never refresh the token: refreshing rotates Claude
  Code's refresh token and would sign Claude Code out. An expired token
  means Claude Code hasn't run lately; the run reports disabled.
  claude-monitor's own history (~/.claude-monitor/usage.db) is imported once.
- OpenRouter: the documented /key and /credits endpoints (dollars).
- Codex: no network at all. The Codex CLI writes the rate limits it gets
  back from ChatGPT into its session logs; we read those incrementally.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from django.utils import timezone
from django.utils.text import slugify

from core.loaders import Disabled, Loader

from .models import LimitSnapshot

TIMEOUT = 30


def _now() -> datetime:
    return timezone.now().replace(microsecond=0)


def _dt(value) -> datetime | None:
    """ISO-8601 string or epoch seconds -> aware datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _pct(used, limit) -> float | None:
    return round(100 * used / limit, 3) if used is not None and limit else None


def _save(vendor: str, source: str, rows: list[dict]) -> int:
    """Insert snapshot dicts (each with window, taken_at, ...); return how
    many were new."""
    qs = LimitSnapshot.objects.filter(vendor=vendor)
    before = qs.count()
    LimitSnapshot.objects.bulk_create(
        [LimitSnapshot(vendor=vendor, source=source, **r) for r in rows],
        ignore_conflicts=True,
        batch_size=500,
    )
    return qs.count() - before


# --- Claude -----------------------------------------------------------------

KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_MONITOR_DB = Path("~/.claude-monitor/usage.db").expanduser()

# Response keys -> our window names. Other buckets with a numeric
# `utilization` keep their own key (Anthropic adds codenamed ones).
CLAUDE_BUCKETS = {
    "five_hour": "session_5h",
    "seven_day": "weekly_all",
    "seven_day_sonnet": "weekly_sonnet",
    "seven_day_opus": "weekly_opus",
}


def claude_token() -> str:
    """Claude Code's current access token from the Keychain, or Disabled."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise Disabled("no macOS `security` command; Claude limits need the Keychain") from None
    if out.returncode != 0:
        raise Disabled(f"no {KEYCHAIN_SERVICE!r} Keychain item; sign in with Claude Code")
    try:
        oauth = json.loads(out.stdout).get("claudeAiOauth") or {}
    except ValueError:
        raise Disabled("Claude Code Keychain item is not JSON") from None
    if not oauth.get("accessToken"):
        raise Disabled("Claude Code Keychain item has no access token")
    if oauth.get("expiresAt", float("inf")) / 1000 < time.time():
        raise Disabled("Claude Code's token has expired; it refreshes next time Claude Code runs")
    return oauth["accessToken"]


def parse_claude(data: dict, taken_at: datetime) -> list[dict]:
    rows: dict[str, dict] = {}
    for key, b in data.items():
        if isinstance(b, dict) and isinstance(b.get("utilization"), (int, float)):
            window = CLAUDE_BUCKETS.get(key, key)
            rows[window] = dict(
                window=window, utilization_pct=float(b["utilization"]), resets_at=_dt(b.get("resets_at")),
                used_usd=b.get("used_dollars"), limit_usd=b.get("limit_dollars"), raw={key: b},
            )
    # Per-model weekly caps appear only in `limits` (e.g. Sonnet, newer models).
    for lim in data.get("limits") or []:
        model = (((lim.get("scope") or {}).get("model")) or {}).get("display_name")
        if lim.get("kind") == "weekly_scoped" and model and lim.get("percent") is not None:
            window = "weekly_" + slugify(model).replace("-", "_")
            rows.setdefault(window, dict(
                window=window, utilization_pct=float(lim["percent"]),
                resets_at=_dt(lim.get("resets_at")), raw={"limits": lim},
            ))
    return [dict(r, taken_at=taken_at) for r in rows.values()]


def backfill_claude_monitor(path: Path) -> int:
    """One-time import of claude-monitor's usage_history. Its rows came
    from rate-limit headers, which carry no Sonnet figure (stored as a 0
    placeholder), so weekly_sonnet is taken only from non-ping rows."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT timestamp, session_percent, session_reset, weekly_all_percent, weekly_reset,"
            " weekly_sonnet_percent, raw_data FROM usage_history WHERE NOT is_synthetic"
        )
        rows = []
        for ts, session, session_reset, weekly, weekly_reset, sonnet, raw in cur:
            raw = json.loads(raw or "{}")
            windows = [("session_5h", session, session_reset), ("weekly_all", weekly, weekly_reset)]
            if "ping_status" not in raw:
                windows.append(("weekly_sonnet", sonnet, weekly_reset))
            rows += [
                dict(window=w, taken_at=_dt(ts), utilization_pct=pct, resets_at=_dt(reset), raw=raw)
                for w, pct, reset in windows if pct is not None
            ]
    finally:
        con.close()
    return _save("claude", "claude-monitor", rows)


class ClaudeLimitsLoader(Loader):
    name = "claude_limits"
    cadence = "session"  # reads the login Keychain; a system daemon can't
    monitor_db = CLAUDE_MONITOR_DB

    def load(self, full: bool = False) -> int:
        token = claude_token()
        resp = requests.get(
            CLAUDE_USAGE_URL, timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Claude usage endpoint returned HTTP {resp.status_code}")
        n = _save("claude", "api", parse_claude(resp.json(), _now()))
        if (full or not self.state_get("monitor_backfill")) and self.monitor_db.exists():
            n += backfill_claude_monitor(self.monitor_db)
            self.state_set("monitor_backfill", _now().isoformat())
        return n


# --- OpenRouter -------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1"


def parse_openrouter(credits: dict, key: dict, taken_at: datetime) -> list[dict]:
    key = {k: v for k, v in key.items() if k != "label"}  # label is a key fragment
    used, bought = credits.get("total_usage"), credits.get("total_credits")
    rows = [dict(window="credits", utilization_pct=_pct(used, bought), used_usd=used, limit_usd=bought, raw=credits)]
    for b in key.get("budgets") or []:
        rows.append(dict(
            window=f"budget_{b.get('scope')}", utilization_pct=_pct(b.get("spend_usd"), b.get("limit_usd")),
            used_usd=b.get("spend_usd"), limit_usd=b.get("limit_usd"), resets_at=_dt(b.get("resets_at")), raw=b,
        ))
    # This key's month-to-date spend, uncapped: the cost side of cost-vs-usage.
    rows.append(dict(window="spend_month", used_usd=key.get("usage_monthly"), raw=key))
    return [dict(r, taken_at=taken_at) for r in rows]


class OpenRouterLimitsLoader(Loader):
    name = "openrouter_limits"
    cadence = "hourly"

    def load(self, full: bool = False) -> int:
        token = os.environ.get("OPENROUTER_API_KEY")
        if not token:
            raise Disabled("OPENROUTER_API_KEY is not set")
        data = {}
        for path in ("credits", "key"):
            resp = requests.get(f"{OPENROUTER_URL}/{path}", headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            if resp.status_code != 200:
                raise RuntimeError(f"OpenRouter /{path} returned HTTP {resp.status_code}")
            data[path] = resp.json()["data"]
        return _save("openrouter", "api", parse_openrouter(data["credits"], data["key"], _now()))


# --- Codex ------------------------------------------------------------------

CODEX_SESSIONS = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
CODEX_WINDOWS = {300: "session_5h", 10080: "weekly_all"}


def parse_codex(timestamp: str, limits: dict) -> list[dict]:
    """One `token_count` event's rate_limits -> snapshots at its timestamp."""
    prefix = "" if limits.get("limit_id") in (None, "codex") else f"{limits['limit_id']}_"
    rows = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if w and w.get("used_percent") is not None:
            minutes = w.get("window_minutes")
            rows.append(dict(
                window=prefix + CODEX_WINDOWS.get(minutes, f"window_{minutes}m"), taken_at=_dt(timestamp),
                utilization_pct=float(w["used_percent"]), resets_at=_dt(w.get("resets_at")), raw=limits,
            ))
    return rows


class CodexLimitsLoader(Loader):
    """Codex records limits only when it is used, so gaps mean idle."""

    name = "codex_limits"
    cadence = "hourly"
    sessions = CODEX_SESSIONS

    def load(self, full: bool = False) -> int:
        if not self.sessions.is_dir():
            raise Disabled(f"no Codex sessions at {self.sessions}")
        since = 0.0 if full else float(self.state_get("mtime", "0"))
        newest, rows = since, []
        for path in self.sessions.rglob("rollout-*.jsonl"):
            mtime = path.stat().st_mtime
            if mtime <= since:
                continue
            newest = max(newest, mtime)
            with path.open() as f:
                for line in f:
                    if '"rate_limits"' not in line:  # cheap filter before parsing
                        continue
                    event = json.loads(line)
                    limits = (event.get("payload") or {}).get("rate_limits")
                    if limits and event.get("timestamp"):
                        rows += parse_codex(event["timestamp"], limits)
        n = _save("codex", "codex-sessions", rows)
        self.state_set("mtime", str(newest))
        return n
