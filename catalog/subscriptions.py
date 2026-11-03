"""Subscriptions analysis: "am I making good use of my AI subscriptions?"

Shown as usage-and-limits graphs, not one score: per vendor, each limit
window's latest reading, its history, and agentsview's API-equivalent
spend next to the plan price.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from sources.agentsview.models import Session
from sources.limits.models import LimitSnapshot, Subscription

VENDOR_ORDER = ["claude", "codex", "openrouter", "gemini"]
# Subscription vendor -> agentsview agent whose spend it covers.
AGENT_FOR_VENDOR = {"claude": "claude", "codex": "codex"}
VENDOR_TITLES = {"claude": "Claude", "codex": "Codex", "openrouter": "OpenRouter", "gemini": "Gemini"}

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
DEFAULT_RANGE = "7d"
# Consecutive samples further apart than this get a null between them so
# the chart shows a gap instead of a line across unknown time.
GAP = timedelta(hours=3)

WINDOW_ORDER = {"session_5h": 0, "weekly_all": 1, "credits": 0, "budget_workspace": 1, "budget_key_limit": 2, "spend_month": 4}
WINDOW_LABELS = {
    "session_5h": "5-hour session",
    "weekly_all": "Weekly, all models",
    "credits": "Prepaid credits",
    "budget_workspace": "Monthly budget",
    "budget_key_limit": "API key limit",
    "spend_month": "Spend this month",
}


def is_extra_usage(vendor, window, limit_usd):
    """Anthropic's "extra usage" dollar bucket arrives under an opaque key
    (iguana_necktie today); any other unknown claude window with a dollar
    limit is assumed to be the same thing renamed."""
    if vendor != "claude":
        return False
    if window == "iguana_necktie":
        return True
    known = window in WINDOW_LABELS or window.startswith("weekly_")
    return not known and limit_usd is not None


def window_label(vendor, window, limit_usd=None):
    if is_extra_usage(vendor, window, limit_usd):
        return "Extra usage ($)"
    if window in WINDOW_LABELS:
        return WINDOW_LABELS[window]
    if window.startswith("weekly_"):
        return f"Weekly, {window.removeprefix('weekly_')}"
    return window


def window_sort_key(window):
    return (WINDOW_ORDER.get(window, 2 if window.startswith("weekly_") else 3), window)


def _span(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def resets_in(resets_at, now=None):
    """'resets in 3h 52m' / '6d 15h' / '12m'; None when unknown; 'reset
    due' once the time has passed (the next snapshot will show the new
    window)."""
    if resets_at is None:
        return None
    now = now or timezone.now()
    if resets_at <= now:
        return "reset due"
    return f"resets in {_span(resets_at - now)}"


def age(taken_at, now=None):
    now = now or timezone.now()
    return f"{_span(now - taken_at)} ago"


def bar_level(pct):
    if pct is None:
        return ""
    if pct > 90:
        return "critical"
    if pct > 70:
        return "warn"
    return "ok"


def latest_snapshots():
    """Latest snapshot per (vendor, window): two queries."""
    latest = LimitSnapshot.objects.values("vendor", "window").annotate(at=Max("taken_at"))
    q = Q()
    for row in latest:
        q |= Q(vendor=row["vendor"], window=row["window"], taken_at=row["at"])
    if not q:
        return {}
    by_vendor = {}
    for snap in LimitSnapshot.objects.filter(q).defer("raw"):
        by_vendor.setdefault(snap.vendor, []).append(snap)
    for snaps in by_vendor.values():
        snaps.sort(key=lambda s: window_sort_key(s.window))
    return by_vendor


def now_rows(snaps, now=None):
    now = now or timezone.now()
    rows = []
    for s in snaps:
        pct = s.utilization_pct
        rows.append(
            {
                "label": window_label(s.vendor, s.window, s.limit_usd),
                "window": s.window,
                "pct": pct,
                "bar_pct": None if pct is None else min(max(pct, 0), 100),
                "level": bar_level(pct),
                "used_usd": s.used_usd,
                "limit_usd": s.limit_usd,
                "resets": resets_in(s.resets_at, now),
                "age": age(s.taken_at, now),
                "source": s.source,
            }
        )
    return rows


def agent_spend(agent, now=None):
    """API-equivalent spend for one agent: calendar month to date and
    trailing 30 days. Sums every row (subagents too) for true spend;
    counts only non-subagent sessions."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    since_30 = now - timedelta(days=30)
    qs = Session.objects.filter(agent=agent)
    not_sub = ~Q(relationship_type="subagent")
    agg = qs.aggregate(
        month_usd=Sum("estimated_cost_usd", filter=Q(started_at__gte=month_start)),
        month_sessions=Count("id", filter=Q(started_at__gte=month_start) & not_sub),
        d30_usd=Sum("estimated_cost_usd", filter=Q(started_at__gte=since_30)),
        d30_sessions=Count("id", filter=Q(started_at__gte=since_30) & not_sub),
        d30_subagents=Count("id", filter=Q(started_at__gte=since_30) & ~not_sub),
    )
    return {
        "month_usd": agg["month_usd"] or Decimal(0),
        "month_sessions": agg["month_sessions"],
        "d30_usd": agg["d30_usd"] or Decimal(0),
        "d30_sessions": agg["d30_sessions"],
        "d30_subagents": agg["d30_subagents"],
        "month_label": local.strftime("%B"),
    }


def _multiple(usd, price):
    if not price:
        return None
    return float(usd) / float(price)


def cards(now=None):
    now = now or timezone.now()
    subs = {s.vendor: s for s in Subscription.objects.all()}
    latest = latest_snapshots()
    order = VENDOR_ORDER + sorted(v for v in subs if v not in VENDOR_ORDER)
    result = []
    for vendor in order:
        sub = subs.get(vendor)
        if sub is None:
            continue
        card = {
            "vendor": vendor,
            "title": VENDOR_TITLES.get(vendor, vendor),
            "sub": sub,
            "rows": now_rows(latest.get(vendor, []), now),
            "has_data": vendor in latest,
        }
        agent = AGENT_FOR_VENDOR.get(vendor)
        if agent:
            spend = agent_spend(agent, now)
            spend["month_x"] = _multiple(spend["month_usd"], sub.monthly_usd)
            spend["d30_x"] = _multiple(spend["d30_usd"], sub.monthly_usd)
            card["spend"] = spend
        if vendor == "openrouter":
            month = next((s for s in latest.get(vendor, []) if s.window == "spend_month"), None)
            card["openrouter_month"] = month.used_usd if month else None
        result.append(card)
    return result


def range_start(range_key, now=None):
    delta = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    if delta is None:
        return None
    return (now or timezone.now()) - delta


def traces(range_key, now=None):
    """{vendor: {"traces": [{name, window, x, y, kind}]}} for the range.

    kind "pct" plots utilization_pct; windows that never report a
    percentage but do report dollars (OpenRouter spend_month) are "usd".
    Gaps longer than GAP get a null point so they draw as gaps.
    """
    start = range_start(range_key, now)
    qs = LimitSnapshot.objects.all()
    if start is not None:
        qs = qs.filter(taken_at__gte=start)
    rows = qs.order_by("vendor", "window", "taken_at").values_list(
        "vendor", "window", "taken_at", "utilization_pct", "used_usd", "limit_usd"
    )
    series = {}
    for vendor, window, taken_at, pct, used, limit in rows:
        s = series.setdefault((vendor, window), {"pts": [], "has_pct": False, "limit": None})
        s["pts"].append((taken_at, pct, used))
        s["has_pct"] |= pct is not None
        if limit is not None:
            s["limit"] = limit

    out = {}
    for (vendor, window) in sorted(series, key=lambda k: (k[0], window_sort_key(k[1]))):
        s = series[(vendor, window)]
        kind = "pct" if s["has_pct"] else "usd"
        x, y = [], []
        prev = None
        for taken_at, pct, used in s["pts"]:
            if prev is not None and taken_at - prev > GAP:
                x.append((prev + timedelta(seconds=1)).isoformat())
                y.append(None)
            x.append(taken_at.isoformat())
            y.append(pct if kind == "pct" else used)
            prev = taken_at
        if kind == "usd" and all(v is None for v in y):
            continue
        out.setdefault(vendor, {"traces": []})["traces"].append(
            {"name": window_label(vendor, window, s["limit"]), "window": window, "x": x, "y": y, "kind": kind}
        )
    return out
