"""agentsview loader: AI coding sessions with tokens, cost and models.

Integration is the agentsview CLI only (never its SQLite file). Two bulk
calls cover everything:

- `export sessions --all`: one JSON document with every session's usage
  (tokens by kind, cost in microdollars, `by_model` breakdown).
- `session list` (500 per page, cursor-paged): cwd, git branch, machine,
  display name, and the project name agentsview's UI uses.

The HTTP API on :8089 has per-session usage only, so a full load there is
one request per session; the CLI export takes ~5 s for ~5,000 sessions.

Subagent, one-shot and automated sessions are all included: each
transcript's usage is its own, so total spend is the sum over all rows.
agentsview's "one-shot" flag isn't exposed; `entrypoint`/`session_kind`
(e.g. sdk-cli for `claude -p`) are the nearest proxy.

Typed prompts: for sessions whose cwd is under an active billing project's
`agent_paths`, one `session messages --role user` read per session records
when a human typed a prompt (`prompt_source` typed, queued or an accepted
suggestion; hook and tool-result messages are also "user" messages). Invoicing
uses these to tell my time from agents working alone. Sessions never read yet
(`typed_prompts` is NULL) are backfilled on the next run.

Incremental: `--active-since` the last successful run, minus a day of
overlap. Rows are upserted on the agentsview id, so overlap is harmless.
`full=True` refetches everything. Sessions deleted from agentsview stay.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.loaders import Disabled, Loader
from core.models import BillingProject

from .models import ModelUsage, Session

logger = logging.getLogger(__name__)
OVERLAP = timedelta(days=1)
INCLUDE = ["--include-one-shot", "--include-automated", "--include-children"]
FALLBACK_BIN = "/opt/homebrew/bin/agentsview"  # LaunchDaemons have a bare PATH
HUMAN_PROMPTS = {"typed", "queued", "suggestion_accepted"}  # `prompt_source` values for a person at the keyboard
PAGE = 500
RETRIES = 3  # the daemon sometimes answers "not responding" while it syncs
# typed_prompts is filled by its own step: the session upsert must not reset it
SESSION_FIELDS = [f.name for f in Session._meta.concrete_fields if f.name not in ("id", "session_id", "typed_prompts")]


def find_binary() -> str:
    path = os.environ.get("AGENTSVIEW_BIN") or shutil.which("agentsview")
    if path:
        return path
    if os.path.exists(FALLBACK_BIN):
        return FALLBACK_BIN
    raise Disabled("agentsview binary not found")


def run_cli(args: list[str]) -> dict:
    """Run `agentsview <args>` and parse its JSON stdout. Tests replace this."""
    proc = subprocess.run([find_binary(), *args], capture_output=True, text=True, timeout=300)
    if proc.returncode:
        msg = f"agentsview {' '.join(args[:2])} exit {proc.returncode}: {proc.stderr[-500:]}"
        if "needs resync" in proc.stderr:  # new agentsview version; only a writable daemon can upgrade the archive
            msg = f"agentsview archive needs upgrading: run `agentsview daemon restart`. {msg}"
        raise RuntimeError(msg)
    return json.loads(proc.stdout)


def fetch_prompts(session_id: str) -> list[str]:
    """Sorted ISO times of the prompts a person typed in one session."""
    times, start = set(), 0
    while True:
        for attempt in range(RETRIES):
            try:
                page = run_cli(["session", "messages", session_id, "--role", "user",
                                "--from", str(start), "--limit", str(PAGE), "--json"])["messages"]
                break
            except RuntimeError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(2)
        if not page:
            return sorted(times)
        times.update(m["timestamp"] for m in page if m.get("prompt_source") in HUMAN_PROMPTS and m.get("timestamp"))
        start = page[-1]["ordinal"] + 1


def dt(value):
    return parse_datetime(value) if value else None


def usd(cost) -> Decimal:
    return Decimal((cost or {}).get("microdollars") or 0) / 1_000_000


def tokens(u: dict) -> dict:
    """agentsview's token names -> ours (shared by Session and ModelUsage)."""
    return {
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
        "cache_write_tokens": u.get("cache_creation_input_tokens") or 0,
        "reasoning_tokens": u.get("reasoning_tokens") or 0,
        "estimated_cost_usd": usd(u.get("cost")),
        "has_cost": bool(u.get("has_cost")),
    }


def primary_model(by_model: dict) -> str:
    """The model that moved the most tokens, cache reads excluded."""
    def weight(m):
        return (m.get("input_tokens") or 0) + (m.get("output_tokens") or 0) + (m.get("cache_creation_input_tokens") or 0)
    return max(by_model.values(), key=weight)["model"] if by_model else ""


def to_session(exp: dict, lst: dict) -> Session:
    """One `export sessions` record plus its `session list` record (may be {})."""
    usage = exp.get("model_usage") or {}
    proj = exp.get("project") or {}
    return Session(
        session_id=exp["id"],
        parent_session_id=exp.get("parent_session_id"),
        relationship_type=exp.get("relationship_type") or "",
        agent=exp.get("agent") or "",
        project=lst.get("project") or proj.get("display_label") or "",
        project_label=proj.get("display_label") or "",
        cwd=lst.get("cwd") or "",
        git_branch=lst.get("git_branch") or "",
        machine=lst.get("machine") or "",
        display_name=lst.get("display_name") or "",
        classification=exp.get("classification") or "",
        is_automated=bool(exp.get("is_automated")),
        entrypoint=lst.get("entrypoint") or "",
        session_kind=lst.get("session_kind") or "",
        started_at=dt(exp.get("started_at")),
        ended_at=dt(exp.get("ended_at")),
        last_activity_at=dt(exp.get("last_activity_at")),
        duration_seconds=exp.get("duration_seconds"),
        message_count=exp.get("message_count") or 0,
        user_message_count=exp.get("user_message_count") or 0,
        assistant_message_count=exp.get("assistant_message_count") or 0,
        turn_count=exp.get("turn_count") or 0,
        primary_model=primary_model(usage.get("by_model") or {}),
        model_names=usage.get("models") or [],
        peak_context_tokens=exp.get("peak_context_tokens") if exp.get("has_peak_context_tokens") else None,
        raw={"export": exp, "list": lst},
        loaded_at=timezone.now(),
        **tokens(usage),
    )


class AgentsviewLoader(Loader):
    name = "agentsview"
    cadence = "hourly"

    def load(self, full: bool = False) -> int:
        started = timezone.now()
        since = None if full else self.state_get("active_since")
        if since:
            since = (parse_datetime(since) - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
        filt = ["--active-since", since] if since else []

        exported = run_cli(["export", "sessions", "--all", "--format", "json", *INCLUDE, *filt])["sessions"]
        listed = {s["id"]: s for s in self.list_sessions(filt)}
        rows = self.save([to_session(e, listed.get(e["id"], {})) for e in exported])
        self.record_prompts([e["id"] for e in exported])

        self.state_set("active_since", started.isoformat())
        logger.info("agentsview: %d sessions (%s)", len(exported), f"since {since}" if since else "full")
        return rows

    def record_prompts(self, fresh_ids: list[str]) -> None:
        """Read typed-prompt times for this run's sessions (and any never read) under billing paths."""
        dirs = [d for bp in BillingProject.objects.filter(active=True, use_agent_activity=True) for d in bp.agent_dirs()]
        if not dirs:
            return
        under = Q()
        for d in dirs:
            under |= Q(cwd=d) | Q(cwd__startswith=d + "/")
        wanted = (Session.objects.filter(under, user_message_count__gt=0)
                  .filter(Q(session_id__in=fresh_ids) | Q(typed_prompts__isnull=True)))
        failed = []
        for s in wanted:
            try:
                s.typed_prompts = fetch_prompts(s.session_id)
            except (RuntimeError, ValueError, KeyError) as exc:
                logger.warning("agentsview: typed prompts for %s: %s", s.session_id, exc)
                failed.append(s.session_id)
                continue
            s.save(update_fields=["typed_prompts"])
        if failed:  # sessions are saved; the next run retries these
            raise RuntimeError(f"agentsview: couldn't read prompts for {len(failed)} session(s): {', '.join(failed[:3])}")

    def list_sessions(self, filt: list[str]):
        cursor = None
        while True:
            page = run_cli(["session", "list", "--format", "json", "--limit", "500", *INCLUDE, *filt,
                            *(["--cursor", cursor] if cursor else [])])
            yield from page["sessions"]
            cursor = page.get("next_cursor")
            if not cursor or not page["sessions"]:
                return

    @transaction.atomic
    def save(self, sessions: list[Session]) -> int:
        """Upsert sessions, then replace their per-model rows. Returns rows written."""
        Session.objects.bulk_create(sessions, batch_size=500, update_conflicts=True,
                                    unique_fields=["session_id"], update_fields=SESSION_FIELDS)
        ids = dict(Session.objects.filter(session_id__in=[s.session_id for s in sessions])
                   .values_list("session_id", "id"))  # bulk upsert doesn't set pks on SQLite
        ModelUsage.objects.filter(session_id__in=ids.values()).delete()
        usage = [ModelUsage(session_id=ids[s.session_id], model=m["model"], **tokens(m))
                 for s in sessions
                 for m in (s.raw["export"].get("model_usage") or {}).get("by_model", {}).values()]
        ModelUsage.objects.bulk_create(usage, batch_size=500)
        return len(sessions) + len(usage)
