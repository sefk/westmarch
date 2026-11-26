"""agentsview loader against recorded CLI output (tests/fixtures), no
agentsview needed. Fixtures: a datatalk session with two models, its
subagent, and a synthetic LM Studio pi session."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.loaders import run_loader
from core.models import LoaderState
from sources.agentsview import loader as mod
from sources.agentsview.models import ModelUsage, Session

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "fixtures"
PARENT = "b4c1e531-d36f-4128-8242-fefcffd7fc76"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cli(monkeypatch):
    """Fake run_cli; records every call's args."""
    calls = []

    def fake(args):
        calls.append(args)
        if args[:2] == ["export", "sessions"]:
            return fixture("export_sessions.json")
        page = 2 if "--cursor" in args else 1
        return fixture(f"session_list_{page}.json")

    monkeypatch.setattr(mod, "run_cli", fake)
    return calls


def test_full_load(cli):
    run = run_loader(mod.AgentsviewLoader())
    assert run.ok, run.message
    assert Session.objects.count() == 3 and ModelUsage.objects.count() == 4
    assert run.rows == 7
    assert len(cli) == 3  # one export, two list pages

    s = Session.objects.get(session_id=PARENT)
    assert (s.agent, s.project, s.cwd) == ("claude", "datatalk", "/Users/sefk/src/biglocalnews/datatalk")
    assert s.display_name == "Staging vs prod eval playbook"
    assert s.estimated_cost_usd == Decimal("8.870801")
    assert s.primary_model == "claude-sonnet-5-5"
    assert s.cache_read_tokens > s.cache_write_tokens > s.input_tokens > 0
    assert s.raw["list"]["first_message"] == "(scrubbed)"
    assert set(s.usage.values_list("model", flat=True)) == {"claude-sonnet-5-5", "claude-opus-5-5"}
    assert sum(u.estimated_cost_usd for u in s.usage.all()) == s.estimated_cost_usd

    # Subagent spend is its own row, linked to the parent.
    child = Session.objects.get(parent_session_id=PARENT)
    assert child.relationship_type == "subagent"
    assert child.estimated_cost_usd == Decimal("6.660268")

    # Local LM Studio usage arrives as a pi session; kept, agent as given.
    pi = Session.objects.get(agent="pi")
    assert pi.project == "lmstudio_other" and pi.primary_model == "qwen/qwen3.6-35b-a3b"
    assert pi.has_cost is False and pi.output_tokens > 0


def test_reload_is_idempotent(cli):
    loader = mod.AgentsviewLoader()
    run_loader(loader)
    first_ids = set(Session.objects.values_list("id", flat=True))
    run_loader(loader, full=True)
    assert set(Session.objects.values_list("id", flat=True)) == first_ids
    assert ModelUsage.objects.count() == 4


def test_incremental_uses_active_since_with_overlap(cli):
    loader = mod.AgentsviewLoader()
    run_loader(loader)
    assert not any("--active-since" in c for c in cli)
    LoaderState.set("agentsview", "active_since", "2026-11-02T12:00:00+00:00")
    cli.clear()
    run_loader(loader)
    for call in cli:
        assert call[call.index("--active-since") + 1] == "2026-11-01T12:00:00Z"
    cli.clear()
    run_loader(loader, full=True)
    assert not any("--active-since" in c for c in cli)


def test_session_missing_from_list_still_loads(cli, monkeypatch):
    monkeypatch.setattr(mod.AgentsviewLoader, "list_sessions", lambda self, filt: iter([]))
    run_loader(mod.AgentsviewLoader())
    s = Session.objects.get(session_id=PARENT)
    assert s.project == "datatalk" and s.cwd == ""  # falls back to export's label


def test_missing_binary_is_disabled(monkeypatch):
    monkeypatch.delenv("AGENTSVIEW_BIN", raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "FALLBACK_BIN", "/nonexistent/agentsview")
    run = run_loader(mod.AgentsviewLoader())
    assert run.ok and run.message.startswith("disabled")


def test_run_cli_needs_resync_says_how_to_fix(monkeypatch):
    err = '{"message":"opening read-only database: archive data needs resync"}'
    monkeypatch.setattr(mod, "find_binary", lambda: "agentsview")
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: mod.subprocess.CompletedProcess(a, 1, stdout="", stderr=err))
    with pytest.raises(RuntimeError) as exc:
        mod.run_cli(["export", "sessions"])
    text = str(exc.value)
    assert text.startswith("agentsview archive needs resync")
    assert "agentsview doctor sync" in text
    assert "agentsview daemon restart" in text
    assert "needs resync" in text


def test_run_cli_other_failures_unchanged(monkeypatch):
    monkeypatch.setattr(mod, "find_binary", lambda: "agentsview")
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: mod.subprocess.CompletedProcess(a, 2, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match=r"^agentsview export sessions exit 2: boom"):
        mod.run_cli(["export", "sessions"])


# --- typed prompts (for invoicing) -----------------------------------------------------------------------------------

def msg(ordinal, at, source, role="user"):
    return {"ordinal": ordinal, "role": role, "timestamp": at, "prompt_source": source}


@pytest.fixture
def prompts(cli, monkeypatch):
    """Billing on the datatalk checkout; `pages` maps session id -> list of message pages."""
    from core.models import BillingProject
    BillingProject.objects.all().delete()
    BillingProject.objects.create(slug="t", name="T", rate_usd=1, term_start="2026-01-01", term_end="2026-12-31",
                                  use_agent_activity=True, agent_paths=["/Users/sefk/src/biglocalnews"])
    pages, sent = {}, []
    inner = mod.run_cli

    def fake(args):
        if args[:2] != ["session", "messages"]:
            return inner(args)
        sent.append(args)
        sid, start = args[2], int(args[args.index("--from") + 1])
        return {"messages": [m for m in pages.get(sid, []) if m["ordinal"] >= start][: mod.PAGE]}

    monkeypatch.setattr(mod, "run_cli", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return pages, sent


def test_typed_prompts_are_recorded_for_billing_paths_only(prompts):
    pages, sent = prompts
    pages[PARENT] = [msg(0, "2026-11-16T15:51:59Z", None), msg(1, "2026-11-16T15:52:44Z", "typed"),
                     msg(2, "2026-11-16T15:52:47Z", None), msg(5, "2026-11-16T15:55:00Z", "queued"),
                     msg(6, "2026-11-16T15:56:00Z", "suggestion_accepted"), msg(7, "2026-11-16T15:57:00Z", "system")]
    run = run_loader(mod.AgentsviewLoader())
    assert run.ok, run.message
    assert Session.objects.get(session_id=PARENT).typed_prompts == [
        "2026-11-16T15:52:44Z", "2026-11-16T15:55:00Z", "2026-11-16T15:56:00Z"]
    assert Session.objects.get(agent="pi").typed_prompts is None  # outside every agent path: never read
    assert {a[2] for a in sent} <= set(Session.objects.filter(cwd__startswith="/Users/sefk/src/biglocalnews")
                                       .values_list("session_id", flat=True))


def test_unread_sessions_are_backfilled_and_read_ones_left_alone(prompts):
    pages, sent = prompts
    pages[PARENT] = [msg(0, "2026-11-16T15:52:44Z", "typed")]
    run_loader(mod.AgentsviewLoader())
    Session.objects.filter(session_id=PARENT).update(typed_prompts=None)  # as if loaded before this feature
    sent.clear()
    mod.AgentsviewLoader().record_prompts([])  # no fresh sessions this run
    assert [a[2] for a in sent] == [PARENT, PARENT]  # the page, then the empty page that ends it
    assert Session.objects.get(session_id=PARENT).typed_prompts == ["2026-11-16T15:52:44Z"]
    sent.clear()
    mod.AgentsviewLoader().record_prompts([])
    assert sent == []  # already read and not fresh: untouched


def test_prompt_pages_are_followed(prompts):
    pages, sent = prompts
    monkey = mod.PAGE
    mod.PAGE = 2
    try:
        pages[PARENT] = [msg(i, f"2026-11-16T15:5{i}:00Z", "typed") for i in range(5)]
        run_loader(mod.AgentsviewLoader())
    finally:
        mod.PAGE = monkey
    assert len(Session.objects.get(session_id=PARENT).typed_prompts) == 5


def test_a_failed_prompt_read_fails_the_run_but_keeps_the_sessions(prompts, monkeypatch):
    pages, sent = prompts
    inner = mod.run_cli

    def flaky(args):
        if args[:2] == ["session", "messages"]:
            raise RuntimeError("agentsview session messages exit 69: daemon not responding")
        return inner(args)

    monkeypatch.setattr(mod, "run_cli", flaky)
    run = run_loader(mod.AgentsviewLoader())
    assert not run.ok and "couldn't read prompts" in run.message
    assert Session.objects.count() == 3 and Session.objects.get(session_id=PARENT).typed_prompts is None


def test_a_transient_prompt_failure_is_retried(prompts, monkeypatch):
    pages, sent = prompts
    pages[PARENT] = [msg(0, "2026-11-16T15:52:44Z", "typed")]
    inner, tries = mod.run_cli, []

    def once(args):
        if args[:2] == ["session", "messages"] and not tries:
            tries.append(1)
            raise RuntimeError("daemon not responding")
        return inner(args)

    monkeypatch.setattr(mod, "run_cli", once)
    assert run_loader(mod.AgentsviewLoader()).ok
    assert Session.objects.get(session_id=PARENT).typed_prompts == ["2026-11-16T15:52:44Z"]
