"""derive: path/name/repo mapping and the ProjectDay rollup, on tiny
in-test fixtures."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

from core.loaders import run_loader
from core.models import LoaderState, Project, ProjectAlias
from derive.loader import DeriveLoader
from derive.mapping import project_for_name, project_for_path, project_for_repo
from derive.models import ProjectDay, UnmappedDay
from sources.agentsview.models import Session
from sources.github.models import Issue, PullRequest, Repo
from sources.gitlocal.models import Commit
from sources.herdr.models import Tab, TabEvent, Workspace

pytestmark = pytest.mark.django_db
LA = ZoneInfo("America/Los_Angeles")


def at(day, hour=10):
    return datetime(2026, 10, day, hour, tzinfo=LA)


@pytest.fixture
def projects():
    return [
        Project.objects.create(slug="datatalk", name="datatalk",
                               path="/Users/sefk/src/biglocalnews/datatalk",
                               repo="biglocalnews/datatalk"),
        Project.objects.create(slug="datatalk-311", name="datatalk-311",
                               path="/Users/sefk/src/biglocalnews/datatalk-311"),
        Project.objects.create(slug="leafletter", name="leafletter",
                               path="/Users/sefk/src/leafletter-app", repo="sefk/leafletter-app"),
        Project.objects.create(slug="wrgpt-buddy", name="wrgpt-buddy",
                               path="/Users/sefk/src/wrgpt-buddy"),
    ]


def test_path_prefix(projects):
    assert project_for_path("/Users/sefk/src/biglocalnews/datatalk", projects).slug == "datatalk"
    assert project_for_path("/Users/sefk/src/biglocalnews/datatalk/eval", projects).slug == "datatalk"
    assert project_for_path("/Users/sefk/src/leafletter-app/.claude/worktrees/x",
                            projects).slug == "leafletter"
    assert project_for_path("/Users/sefk/src/biglocalnews/datatalk-311/a", projects).slug == "datatalk-311"


def test_path_sibling_worktree(projects):
    assert project_for_path("/Users/sefk/src/leafletter-app-225-features-combine",
                            projects).slug == "leafletter"
    assert project_for_path("/Users/sefk/src/biglocalnews/datatalk-1281-donor-totals/sub",
                            projects).slug == "datatalk"
    # longest basename wins: a worktree of datatalk-311
    assert project_for_path("/Users/sefk/src/biglocalnews/datatalk-311-7-fix",
                            projects).slug == "datatalk-311"


@pytest.mark.parametrize("path", ["/Users/sefk", "/Users/sefk/src", "/private/tmp",
                                  "/sessions/upbeat-lucid", "/Users/sefk/src/leafletter",
                                  "/Users/sefk/src/biglocalnews", ""])
def test_path_no_match(projects, path):
    assert project_for_path(path, projects) is None


def test_name_fallback(projects):
    assert project_for_name("wrgpt_buddy", projects).slug == "wrgpt-buddy"
    assert project_for_name("leafletter_app", projects).slug == "leafletter"  # path basename
    ProjectAlias.objects.create(project=projects[2], source="registry", name="leaf")
    assert project_for_name("leaf", projects).slug == "leafletter"
    assert project_for_name("nope", projects) is None


def test_repo(projects):
    assert project_for_repo("SEFK/Leafletter-App", projects).slug == "leafletter"
    assert project_for_repo("sefk/other", projects) is None


def session(sid, project, cwd, started, cost, agent="claude", rel="", secs=60):
    return Session.objects.create(
        session_id=sid, project=project, cwd=cwd, agent=agent, relationship_type=rel,
        started_at=started, duration_seconds=secs, estimated_cost_usd=Decimal(cost),
        input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=1)


@pytest.fixture
def world(projects):
    dt, leaf = projects[0], projects[2]
    session("s1", "datatalk", "/Users/sefk/src/biglocalnews/datatalk", at(1), "1.50")
    session("s2", "datatalk", "/Users/sefk/src/biglocalnews/datatalk", at(1, 11), "0.25",
            agent="codex", rel="subagent")
    session("s3", "datatalk", "/Users/sefk/src/biglocalnews/datatalk", at(2), "2.00")
    # 23:30 local on the 1st is the 2nd in UTC; must land on the 1st.
    session("s4", "wrgpt_buddy", "/Users/sefk/src/wrgpt-buddy/.claude-advice", at(1, 23), "3.00")
    session("s5", "tmp", "/private/tmp", at(2), "0.40")
    session("s6", "tmp", "/private/tmp", at(2), "0.10", rel="subagent")
    Commit.objects.create(project=leaf, sha="a" * 40, author_name="s", author_email="s",
                          authored_at=at(1), committed_at=at(1), subject="x",
                          insertions=5, deletions=2)
    Commit.objects.create(project=leaf, sha="b" * 40, author_name="s", author_email="s",
                          authored_at=at(2), committed_at=at(2), subject="y",
                          insertions=1, deletions=0)
    repo = Repo.objects.create(full_name="biglocalnews/datatalk", owner="biglocalnews",
                               name="datatalk")
    Repo.objects.create(full_name="sefk/unknown", owner="sefk", name="unknown")
    Issue.objects.create(repo=repo, number=1, state="closed", created_at=at(1), closed_at=at(2))
    PullRequest.objects.create(repo=repo, number=2, state="closed", created_at=at(1),
                               merged_at=at(1, 15))
    ws = Workspace.objects.create(herdr_id="w1")
    tab = Tab.objects.create(herdr_id="w1:t1", workspace=ws,
                             cwd="/Users/sefk/src/leafletter-app-225-features-combine",
                             first_seen_at=at(1), last_seen_at=at(2))
    Tab.objects.create(herdr_id="w1:t2", workspace=ws, cwd="/Users/sefk/src",
                       first_seen_at=at(1), last_seen_at=at(1))
    TabEvent.objects.create(tab=tab, kind="focus", at=at(2))
    TabEvent.objects.create(tab=tab, kind="focus", at=at(2, 12))
    TabEvent.objects.create(tab=tab, kind="rename", at=at(2, 13))
    return projects


def days():
    return {(r.project.slug, r.day.day): r for r in ProjectDay.objects.select_related("project")}


def test_rollup(world):
    run = run_loader(DeriveLoader(), full=True)
    assert run.ok, run.message
    assert ProjectAlias.objects.get(source="agentsview", name="wrgpt_buddy").project.slug == "wrgpt-buddy"
    assert ProjectAlias.objects.get(source="github", name="biglocalnews/datatalk").project.slug == "datatalk"
    assert LoaderState.get("derive", "unmapped:agentsview") == "tmp"
    assert LoaderState.get("derive", "unmapped:github") == "sefk/unknown"
    assert LoaderState.get("derive", "unmapped:herdr") == "/Users/sefk/src"

    d = days()
    d1 = d[("datatalk", 1)]
    assert (d1.sessions, d1.subagent_sessions, d1.agent_seconds) == (1, 1, 120)
    assert d1.cost_usd == Decimal("1.75")
    assert d1.cost_by_agent == {"claude": 1.5, "codex": 0.25}
    assert (d1.input_tokens, d1.cache_read_tokens) == (20, 200)
    assert (d1.gh_issues_opened, d1.gh_prs_opened, d1.gh_prs_merged) == (1, 1, 1)
    assert d[("datatalk", 2)].gh_issues_closed == 1
    assert d[("wrgpt-buddy", 1)].cost_usd == Decimal("3")
    assert (d[("leafletter", 1)].commits, d[("leafletter", 1)].insertions) == (1, 5)
    assert d[("leafletter", 1)].herdr_tabs == 1
    assert (d[("leafletter", 2)].herdr_tabs, d[("leafletter", 2)].herdr_focus_events) == (1, 2)

    u = UnmappedDay.objects.get()
    assert (u.day.day, u.sessions, u.cost_usd) == (2, 1, Decimal("0.5"))
    total = sum(r.cost_usd for r in ProjectDay.objects.all()) + u.cost_usd
    assert total == sum(s.estimated_cost_usd for s in Session.objects.all())


def test_idempotent_and_hand_edits_win(world):
    run_loader(DeriveLoader(), full=True)
    # a hand edit re-points an alias; derive must not undo it
    alias = ProjectAlias.objects.get(source="agentsview", name="wrgpt_buddy")
    alias.project = world[2]
    alias.save()
    first = {k: (r.sessions, r.cost_usd, r.commits, r.herdr_tabs) for k, r in days().items()}
    aliases = ProjectAlias.objects.count()
    run_loader(DeriveLoader(), full=True)
    run_loader(DeriveLoader())  # incremental
    assert ProjectAlias.objects.count() == aliases
    assert ProjectAlias.objects.get(source="agentsview", name="wrgpt_buddy").project == world[2]
    again = {k: (r.sessions, r.cost_usd, r.commits, r.herdr_tabs) for k, r in days().items()}
    assert ("leafletter", 1) in again and ("wrgpt-buddy", 1) not in again
    assert again[("leafletter", 1)][1] == Decimal("3")
    assert {k: v for k, v in again.items() if k[0] != "leafletter"} == \
        {k: v for k, v in first.items() if k[0] != "leafletter" and k[0] != "wrgpt-buddy"}
    assert UnmappedDay.objects.count() == 1


def test_unmapped_command(world, capsys):
    run_loader(DeriveLoader(), full=True)
    call_command("unmapped")
    out = capsys.readouterr().out
    assert "agentsview: 1 unmapped" in out and "  tmp" in out
