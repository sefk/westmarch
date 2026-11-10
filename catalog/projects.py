"""Projects analysis: "what have I been working on, and how?"

Reads ProjectDay / UnmappedDay (built by derive) for per-project totals
over time, and agentsview Sessions for time of day and the drilldown.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Max, Q, Sum
from django.utils import timezone

from core.models import Project, ProjectAlias
from derive.loader import day_start, local_day
from derive.models import ProjectDay, UnmappedDay
from sources.agentsview.models import Session

RANGES = {"30d": 30, "90d": 90, "1y": 365, "all": None}
DEFAULT_RANGE = "90d"
TOP = 6  # projects named in the stacked chart; the rest fold into "Other"
METRICS = {
    "cost": ("API-equivalent $", "cost_usd"),
    "sessions": ("Sessions", "sessions"),
    "commits": ("Commits", "commits"),
}
SUMS = ["sessions", "subagent_sessions", "agent_seconds", "cost_usd", "input_tokens", "output_tokens", "commits",
        "insertions", "deletions", "gh_commits", "gh_issues_opened", "gh_issues_closed", "gh_prs_opened",
        "gh_prs_merged", "herdr_focus_events"]


def today(now=None):
    return local_day(now or timezone.now())


def range_start(range_key, now=None):
    n = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    if n is None:
        return ProjectDay.objects.order_by("day").values_list("day", flat=True).first() or today(now)
    return today(now) - timedelta(days=n - 1)


def _f(v):
    return float(v) if isinstance(v, Decimal) else (v or 0)


def table(range_key, now=None):
    """One row per project active in the range, by API-equivalent cost."""
    lo = range_start(range_key, now)
    qs = (ProjectDay.objects.filter(day__gte=lo).values("project_id", "project__name", "project__tab")
          .annotate(**{k: Sum(k) for k in SUMS}, last_day=Max("day")))
    rows = []
    for r in qs:
        r = {k: _f(v) if k in SUMS else v for k, v in r.items()}
        r["all_commits"] = r["commits"] + r["gh_commits"]
        rows.append(r)
    rows.sort(key=lambda r: (-r["cost_usd"], -r["all_commits"], r["project__name"]))
    unmapped = UnmappedDay.objects.filter(day__gte=lo).aggregate(sessions=Sum("sessions"), cost=Sum("cost_usd"))
    totals = {k: sum(r[k] for r in rows) for k in ("sessions", "cost_usd", "all_commits", "gh_prs_merged")}
    totals["cost_usd"] += _f(unmapped["cost"])
    totals["sessions"] += _f(unmapped["sessions"])
    totals["projects"] = len(rows)
    return {"rows": rows, "unmapped": {"sessions": _f(unmapped["sessions"]), "cost": _f(unmapped["cost"])},
            "totals": totals}


def _buckets(lo, hi, weekly):
    if not weekly:
        return [lo + timedelta(days=i) for i in range((hi - lo).days + 1)], (lambda d: (d - lo).days)
    w0 = lo - timedelta(days=lo.weekday())
    n = (hi - w0).days // 7 + 1
    return [w0 + timedelta(weeks=i) for i in range(n)], (lambda d: (d - w0).days // 7)


def series(range_key, now=None):
    """Stacked series per metric: top projects by that metric, rest as Other;
    unmapped agentsview usage as its own series for cost and sessions. Also
    cost by agent, and a weekday x hour count of session starts."""
    lo, hi = range_start(range_key, now), today(now)
    weekly = (hi - lo).days > 45
    xs, slot = _buckets(lo, hi, weekly)
    days = list(ProjectDay.objects.filter(day__gte=lo, day__lte=hi).values(
        "day", "project__name", "cost_usd", "sessions", "agent_seconds", "commits", "gh_commits", "cost_by_agent"))
    out = {"x": [d.isoformat() for d in xs], "bucket": "week" if weekly else "day", "metrics": {}}
    for key, (label, field) in METRICS.items():
        grid = defaultdict(lambda: [0.0] * len(xs))
        totals = defaultdict(float)
        for r in days:
            v = _f(r[field]) + (_f(r["gh_commits"]) if key == "commits" else 0)
            grid[r["project__name"]][slot(r["day"])] += v
            totals[r["project__name"]] += v
        named = [p for p, t in sorted(totals.items(), key=lambda kv: -kv[1])[:TOP] if t > 0]
        traces = [{"name": p, "y": [round(v, 2) for v in grid[p]]} for p in named]
        other = [0.0] * len(xs)
        for p, ys in grid.items():
            if p not in named:
                other = [a + b for a, b in zip(other, ys)]
        if any(other):
            traces.append({"name": "Other", "y": [round(v, 2) for v in other]})
        if key in ("cost", "sessions"):
            un = [0.0] * len(xs)
            for d, s, c in UnmappedDay.objects.filter(day__gte=lo, day__lte=hi).values_list("day", "sessions",
                                                                                         "cost_usd"):
                un[slot(d)] += _f(c) if key == "cost" else s
            if any(un):
                traces.append({"name": "Unmapped", "y": [round(v, 2) for v in un]})
        out["metrics"][key] = {"label": label, "traces": traces}

    agents = defaultdict(lambda: [0.0] * len(xs))
    for r in days:
        for agent, cost in (r["cost_by_agent"] or {}).items():
            agents[agent][slot(r["day"])] += _f(cost)
    order = sorted(agents, key=lambda a: -sum(agents[a]))
    out["agents"] = [{"name": a, "y": [round(v, 2) for v in agents[a]]} for a in order]
    out["clock"] = clock(lo, hi)
    return out


def clock(lo, hi):
    """Session starts by weekday (Mon=0) x local hour, subagents excluded."""
    z = [[0] * 24 for _ in range(7)]
    qs = Session.objects.filter(started_at__gte=day_start(lo), started_at__lt=day_start(hi + timedelta(days=1))) \
        .exclude(relationship_type="subagent").values_list("started_at", flat=True)
    for at in qs.iterator(chunk_size=5000):
        t = timezone.localtime(at)
        z[t.weekday()][t.hour] += 1
    return z


def agentsview_link(session, request_host=None):
    """agentsview's own web_url, re-pointed at the host the page was
    requested on (its stored URL says 127.0.0.1)."""
    url = ((session.raw or {}).get("list") or {}).get("web_url") or ""
    if not url:
        return ""
    parts = urlsplit(url)
    base = urlsplit(settings.AGENTSVIEW_URL)
    host = (request_host or "").split(":")[0] or base.hostname
    return f"{base.scheme}://{host}:{base.port or parts.port or 8089}{parts.path}"


def project_detail(project: Project, range_key, now=None, request_host=None):
    lo, hi = range_start(range_key, now), today(now)
    rows = {r.day: r for r in ProjectDay.objects.filter(project=project, day__gte=lo, day__lte=hi)}
    xs = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
    names = list(ProjectAlias.objects.filter(project=project, source="agentsview").values_list("name", flat=True))
    sessions = []
    if names:
        qs = (Session.objects.filter(project__in=names, started_at__gte=day_start(lo))
              .filter(~Q(relationship_type="subagent")).order_by("-started_at").defer("raw")[:50])
        ids = [s.id for s in qs]
        raws = dict(Session.objects.filter(id__in=ids).values_list("id", "raw"))
        for s in qs:
            s.raw = raws.get(s.id) or {}
            s.link = agentsview_link(s, request_host)
            s.hours = (s.duration_seconds or 0) / 3600
            sessions.append(s)
    return {
        "x": [d.isoformat() for d in xs],
        "cost": [_f(rows[d].cost_usd) if d in rows else 0 for d in xs],
        "commits": [(rows[d].commits + rows[d].gh_commits) if d in rows else 0 for d in xs],
        "sessions": sessions,
    }
