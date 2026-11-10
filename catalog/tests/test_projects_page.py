from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone

from catalog import projects as pj
from core.models import Project, ProjectAlias
from derive.models import ProjectDay, UnmappedDay
from sources.agentsview.models import Session

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=dt_timezone.utc)
END = date(2026, 11, 9)


@pytest.fixture
def data():
    names = ["alpha", "beta", "c", "d", "e", "f", "g", "h"]
    projects = {n: Project.objects.create(name=n, slug=n, path=f"/src/{n}") for n in names}
    for i, n in enumerate(names):
        for k in range(3):
            ProjectDay.objects.create(project=projects[n], day=END - timedelta(days=k), sessions=1,
                                      cost_usd=Decimal(100 - i * 10), commits=i, gh_commits=1,
                                      cost_by_agent={"claude": 100 - i * 10})
    UnmappedDay.objects.create(day=END, sessions=2, cost_usd=Decimal("7.5"))
    ProjectAlias.objects.create(project=projects["alpha"], source="agentsview", name="alpha-av")
    Session.objects.create(session_id="s1", agent="claude", project="alpha-av", display_name="Fix the thing",
                           started_at=timezone.make_aware(datetime(2026, 11, 9, 9, 30)), duration_seconds=5400,
                           estimated_cost_usd=Decimal("3.25"),
                           raw={"list": {"web_url": "http://127.0.0.1:8089/sessions/s1"}})
    Session.objects.create(session_id="s2", agent="claude", project="alpha-av", relationship_type="subagent",
                           started_at=timezone.make_aware(datetime(2026, 11, 9, 9, 40)))
    return projects


def test_table(data):
    t = pj.table("30d", now=NOW)
    assert [r["project__name"] for r in t["rows"]][:2] == ["alpha", "beta"]
    alpha = t["rows"][0]
    assert alpha["cost_usd"] == 300 and alpha["all_commits"] == 3 and alpha["last_day"] == END
    assert t["unmapped"] == {"sessions": 2, "cost": 7.5}
    assert t["totals"]["projects"] == 8 and t["totals"]["sessions"] == 24 + 2


def test_series_top_other_unmapped_and_clock(data):
    s = pj.series("30d", now=NOW)
    assert s["bucket"] == "day" and len(s["x"]) == 30
    cost = [t["name"] for t in s["metrics"]["cost"]["traces"]]
    assert cost == ["alpha", "beta", "c", "d", "e", "f", "Other", "Unmapped"]
    other = next(t for t in s["metrics"]["cost"]["traces"] if t["name"] == "Other")
    assert other["y"][-1] == 40 + 30  # g and h
    commits = s["metrics"]["commits"]["traces"]
    assert commits[0]["name"] == "h" and commits[0]["y"][-1] == 8  # 7 local + 1 GitHub
    assert "Unmapped" not in [t["name"] for t in commits]
    assert s["agents"][0]["name"] == "claude"
    assert s["clock"][END.weekday()][9] == 1  # subagent not counted
    assert pj.series("1y", now=NOW)["bucket"] == "week"


def test_detail_and_link(data):
    d = pj.project_detail(data["alpha"], "30d", now=NOW, request_host="desk.example.ts.net:8520")
    assert d["cost"][-1] == 100 and d["commits"][-1] == 1
    assert [s.session_id for s in d["sessions"]] == ["s1"]
    assert d["sessions"][0].link == "http://desk.example.ts.net:8089/sessions/s1"


def test_pages(client, data):
    r = client.get("/projects/?range=30d")
    assert r.status_code == 200 and b"What have I been working on" in r.content and b"Unmapped" in r.content
    assert client.get("/projects/data/?range=bogus").json()["metrics"]["cost"]["traces"]
    p = client.get(f"/projects/{data['alpha'].pk}/")
    assert p.status_code == 200 and b"Fix the thing" in p.content
    assert client.get("/projects/99999/").status_code == 404


def test_empty(client):
    assert client.get("/projects/").status_code == 200
    assert pj.series("90d")["metrics"]["cost"]["traces"] == []
