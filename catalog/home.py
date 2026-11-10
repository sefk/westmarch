"""The home page: the PRD's tabs, each listing its question-shaped pages
with a few headline numbers, and what's planned there (linked to its
issue). Every headline reads a small precomputed table or a short indexed
range, so the page stays cheap; one that fails shows nothing rather than
breaking the page.
"""

import logging
from datetime import timedelta

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from core.models import LoaderRun

log = logging.getLogger(__name__)
ISSUES = "https://github.com/sefk/westmarch/issues/"
TABS = [
    ("work", "Work"),
    ("tech", "Tech"),
    ("projects", "Projects"),
    ("health", "Health"),
    ("social", "Social"),
    ("fun", "Fun"),
    ("money", "Money"),
]
DEFAULT_TAB = "work"


def _money(v):
    return f"${v:,.0f}"


def work_headlines(now):
    from derive.models import ProjectDay
    from derive.loader import local_day

    since = local_day(now) - timedelta(days=6)
    agg = ProjectDay.objects.filter(day__gte=since).aggregate(
        cost=Sum("cost_usd"), sessions=Sum("sessions"), projects=Count("project", distinct=True))
    return [("API-equivalent, 7 days", _money(agg["cost"] or 0)),
            ("Sessions, 7 days", f"{agg['sessions'] or 0:,}"),
            ("Projects touched, 7 days", f"{agg['projects']:,}")]


def subscriptions_headlines(now):
    from sources.limits.models import LimitSnapshot

    out = []
    for vendor, label in (("claude", "Claude weekly"), ("codex", "Codex weekly")):
        snap = LimitSnapshot.objects.filter(vendor=vendor, window="weekly_all").order_by("-taken_at").first()
        if snap and snap.utilization_pct is not None:
            out.append((label, f"{snap.utilization_pct:.0f}%"))
    from catalog.subscriptions import agent_spend

    out.append(("Claude API-equiv., this month", _money(agent_spend("claude", now)["month_usd"])))
    return out


def health_headlines(now):
    from catalog.health import summary

    s = summary(now)
    tiles = {t["label"]: t for t in s["tiles"]}
    out = []
    for label, short in (("Steps / day", "Steps / day"), ("Sleep / night", "Sleep / night"),
                         ("Resting HR", "Resting HR")):
        t = tiles.get(label)
        if t and t["text"]:
            out.append((f"{short}, 7 days", f"{t['text']}{(' ' + t['unit']) if t['unit'] else ''}"))
    out.append(("Workouts, 7 days", str(s["workouts_7d"])))
    return out


def trips_headlines(now):
    from derive.models import Trip

    last = Trip.objects.order_by("-start").first()
    year = Trip.objects.filter(start__year=timezone.localdate(now).year)
    agg = year.aggregate(n=Count("id"), days=Sum("days"))
    out = []
    if last:
        out.append(("Last trip", f"{last.label}, {last.start:%b %-d} ({last.days} days)"))
    out.append((f"Trips in {timezone.localdate(now).year}", f"{agg['n']} · {agg['days'] or 0} days away"))
    return out


def tech_headlines(now):
    runs = latest_runs()
    failing = [r.loader for r in runs if r.ok is False]
    backup = next((r for r in runs if r.loader == "backup" and r.ok), None)
    out = [("Loaders", f"{len(runs) - len(failing)} of {len(runs)} ok")]
    if backup:
        out.append(("Last backup", f"{_ago(now - backup.started)} ago"))
    return out


def _ago(delta):
    minutes = delta.total_seconds() / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.0f}h" if minutes < 48 * 60 else f"{minutes / 1440:.0f}d"


def latest_runs():
    latest = LoaderRun.objects.values("loader").annotate(at=Max("started"))
    q = Q()
    for row in latest:
        q |= Q(loader=row["loader"], started=row["at"])
    if not q:
        return []
    runs = []
    for run in LoaderRun.objects.filter(q).order_by("loader"):
        first = (run.message or "").strip().splitlines()
        run.first_line = first[0] if first else ""
        runs.append(run)
    return runs


# tab -> pages. A page has a url_name (live) or an issue (planned).
PAGES = {
    "work": [
        {"title": "Projects", "url_name": "catalog:projects", "question": "What have I been working on, and how?",
         "headlines": work_headlines},
        {"title": "Subscriptions", "url_name": "catalog:subscriptions",
         "question": "Am I making good use of my AI subscriptions?", "headlines": subscriptions_headlines},
        {"title": "Mail and calendar", "issue": 14,
         "question": "What's going on in my mail and calendar, summarized and sorted?"},
        {"title": "Drive", "issue": 1, "question": "My own documents, mirrored and searchable."},
    ],
    "tech": [
        {"title": "Data health", "anchor": "loaders", "question": "Is every loader running, and where are the gaps?",
         "headlines": tech_headlines},
        {"title": "Grafana", "issue": 10, "question": "Drill into any table over time."},
        {"title": "Front door", "issue": 12, "question": "Reach westmarch by name from anywhere on the tailnet."},
        {"title": "Mail search", "issue": 13, "question": "Full-text search over every message and attachment."},
    ],
    "projects": [
        {"title": "Home projects", "issue": 19,
         "question": "How are my non-code projects going: spending, milestones, correspondence?"},
    ],
    "health": [
        {"title": "Health", "url_name": "catalog:health", "question": "How am I doing physically?",
         "headlines": health_headlines},
        {"title": "Books", "issue": 15, "question": "What have I been reading?"},
    ],
    "social": [
        {"title": "Keeping in touch", "issue": 16, "question": "Who am I in touch with, and who am I losing touch with?"},
    ],
    "fun": [
        {"title": "Trips", "issue": 17, "question": "Where have I been, and when?", "headlines": trips_headlines,
         "note": "Data ready (geo loader); page planned."},
        {"title": "Poker and games", "issue": 17, "question": "How are WRGPT and my games going?"},
    ],
    "money": [
        {"title": "Financial plan", "issue": 18, "question": "Am I on plan?"},
    ],
}


def tab_context(tab, now=None):
    now = now or timezone.now()
    tab = tab if tab in PAGES else DEFAULT_TAB
    pages = []
    for spec in PAGES[tab]:
        page = {k: v for k, v in spec.items() if k != "headlines"}
        page["live"] = "url_name" in spec or "anchor" in spec
        if "issue" in spec:
            page["issue_url"] = f"{ISSUES}{spec['issue']}"
        page["stats"] = []
        if "headlines" in spec:
            try:
                page["stats"] = spec["headlines"](now)
            except Exception:
                log.exception("home headline for %s failed", spec["title"])
        pages.append(page)
    runs = latest_runs()
    failing = [r.loader for r in runs if r.ok is False]
    tabs = [{"key": k, "label": label, "live": sum(1 for p in PAGES[k] if "url_name" in p or "anchor" in p)}
            for k, label in TABS]
    return {"tab": tab, "tabs": tabs, "pages": pages, "runs": runs if tab == "tech" else [],
            "loader_count": len(runs), "failing": failing}
