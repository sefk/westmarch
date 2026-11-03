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
