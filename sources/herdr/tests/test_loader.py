import json
from datetime import datetime, timezone

import pytest
from django.test import override_settings

from core.loaders import Disabled, run_loader
from sources.herdr.loader import HerdrLoader
from sources.herdr.models import Tab, TabEvent, Workspace

pytestmark = pytest.mark.django_db

L = '{ts} INFO herdr::logging: x event="{ev}" subsystem="s" outcome="ok" workspace_id="{ws}"{tab}\n'


def log(ts, ev, ws, tab=None):
    return L.format(ts=ts, ev=ev, ws=ws, tab=f' tab_id="{tab}"' if tab else "")


def session(*workspaces):
    return {"version": 3, "workspaces": list(workspaces)}


def ws(wid, name, tabs):
    return {
        "id": wid, "custom_name": name, "identity_cwd": f"/src/{name}",
        "public_tab_numbers": [n for n, _ in tabs],
        "tabs": [
            {"custom_name": t, "root_pane": 1, "panes": {
                "1": {"cwd": f"/src/{name}", "managed_agent_kind": "claude",
                      "agent_session": {"agent": "claude", "value": f"sess-{wid}-{n}"}},
                "2": {"cwd": f"/src/{name}"}}}
            for n, t in tabs
        ],
    }


@pytest.fixture
def home(tmp_path):
    (tmp_path / "session-snapshots").mkdir()
    (tmp_path / "session.json").write_text(json.dumps(session(ws("wA", "alpha", [(1, "main"), (2, "side")]))))
    (tmp_path / "herdr-server.log").write_text(
        log("2026-10-01T10:00:00.000001Z", "workspace.create", "wA")
        + log("2026-10-01T10:00:05.000000Z", "tab.focus", "wA", "wA:t1")
        + log("2026-10-01T10:05:00.000000Z", "tab.rename", "wA", "wA:t2")
        + log("2026-10-01T10:06:00.000000Z", "workspace.create", "wB")
        + log("2026-10-01T11:00:00.000000Z", "workspace.close", "wB")
    )
    with override_settings(HERDR_DIR=tmp_path):
        yield tmp_path


def test_log_and_session(home):
    assert run_loader(HerdrLoader()).ok
    a = Workspace.objects.get(herdr_id="wA")
    assert a.name == "alpha" and a.created_at.isoformat().startswith("2026-10-01T10:00:00")
    t = Tab.objects.get(herdr_id="wA:t1")
    assert (t.title, t.agent, t.pane_count, t.agent_session_id) == ("main", "claude", 2, "sess-wA-1")
    assert t.cwd == "/src/alpha" and t.first_seen_at.minute == 0 and t.closed_at is None
    assert TabEvent.objects.filter(kind="focus").count() == 1
    assert TabEvent.objects.filter(kind="rename").count() == 1
    assert Workspace.objects.get(herdr_id="wB").closed_at.hour == 11  # exact, from the log


def test_idempotent_and_incremental(home):
    run_loader(HerdrLoader())
    counts = (Workspace.objects.count(), Tab.objects.count(), TabEvent.objects.count())
    run_loader(HerdrLoader())
    assert counts == (Workspace.objects.count(), Tab.objects.count(), TabEvent.objects.count())
    with (home / "herdr-server.log").open("a") as f:
        f.write(log("2026-10-01T12:00:00.000000Z", "tab.focus", "wA", "wA:t2"))
        f.write("2026-10-01T12:00:01Z partial line with no newline")
    run_loader(HerdrLoader())
    assert TabEvent.objects.filter(kind="focus").count() == 2
    assert Tab.objects.get(herdr_id="wA:t2").events.filter(kind="focus", at__gte=datetime(2026, 10, 1, 12, tzinfo=timezone.utc)).exists()


def test_snapshot_replay_closes_vanished_tab(home):
    old = session(ws("wA", "alpha", [(1, "main"), (2, "side"), (3, "gone")]))
    ns = int(1_790_000_000 * 1e9)  # 2026-09-21, before the log; replayed first
    (home / "session-snapshots" / f"session-{ns:036d}-1-0.json").write_text(json.dumps(old))
    run_loader(HerdrLoader())
    gone = Tab.objects.get(herdr_id="wA:t3")
    assert gone.closed_at is not None and gone.title == "gone"
    assert Tab.objects.get(herdr_id="wA:t1").closed_at is None


def test_closed_workspace_closes_its_tabs(home):
    # wB has a tab seen in the log, then the workspace-close event.
    with (home / "herdr-server.log").open("a") as f:
        f.write(log("2026-10-01T10:30:00.000000Z", "tab.focus", "wB", "wB:t1"))
    run_loader(HerdrLoader())
    assert Tab.objects.get(herdr_id="wB:t1").closed_at.hour == 11


def test_disabled_without_herdr(tmp_path):
    with override_settings(HERDR_DIR=tmp_path / "nope"):
        with pytest.raises(Disabled):
            HerdrLoader().load()
