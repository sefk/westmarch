"""Health analysis: "how am I doing physically?"

Reads the HealthDay rollup for the daily series and Workout /
WorkoutRoutePoint for workouts. Missing days are gaps (null), never zero.
"""

from collections import Counter, defaultdict
from datetime import date, timedelta

from django.utils import timezone

from sources.health.ingest import parse_date
from sources.health.models import HealthDay, Workout, WorkoutRoutePoint
from sources.health.rollup import day_start, local_day

RANGES = {"30d": 30, "90d": 90, "1y": 365, "all": None}
DEFAULT_RANGE = "90d"
SERIES = ["steps", "exercise_min", "daylight_min", "resting_hr", "hrv_ms", "active_kcal", "distance_mi",
          "sleep_hr", "sleep_deep_hr", "sleep_core_hr", "sleep_rem_hr", "sleep_awake_hr", "vo2_max"]
COVERAGE = [  # (label, HealthDay field or special key)
    ("Steps", "steps"),
    ("Active energy", "active_kcal"),
    ("Daylight", "daylight_min"),
    ("Resting HR", "resting_hr"),
    ("HRV", "hrv_ms"),
    ("Sleep", "sleep_hr"),
    ("Workouts", "workouts"),
    ("Routes", "routes"),
]
TOP_TYPES = 4  # workout types shown by name; the rest fold into "Other"
MAX_ROUTE_POINTS = 2000


def today(now=None):
    return local_day(now or timezone.now())


def range_start(range_key, now=None) -> date | None:
    n = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    if n is None:
        first = HealthDay.objects.order_by("day").values_list("day", flat=True).first()
        return first
    return today(now) - timedelta(days=n - 1)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def rolling(values, window=7, min_count=4):
    """Trailing mean over `window` slots, None where fewer than
    `min_count` of them have data."""
    out = []
    for i in range(len(values)):
        xs = [v for v in values[max(0, i - window + 1) : i + 1] if v is not None]
        out.append(sum(xs) / len(xs) if len(xs) >= min_count else None)
    return out


def summary(now=None):
    """Stat tiles: last 7 days against the 90 before them."""
    end = today(now)
    recent_lo, base_lo = end - timedelta(days=6), end - timedelta(days=96)
    rows = list(HealthDay.objects.filter(day__gte=base_lo, day__lte=end))
    recent = [r for r in rows if r.day >= recent_lo]
    base = [r for r in rows if r.day < recent_lo]

    def tile(label, field, unit, fmt, better=None):
        now_v = _mean(getattr(r, field) for r in recent)
        base_v = _mean(getattr(r, field) for r in base)
        delta = None if now_v is None or base_v is None else now_v - base_v
        return {"label": label, "value": now_v, "base": base_v, "delta": delta, "unit": unit, "fmt": fmt,
                "better": better}

    tiles = [
        tile("Steps / day", "steps", "", "{:,.0f}", "up"),
        tile("Sleep / night", "sleep_hr", "h", "{:.1f}", "up"),
        tile("Resting HR", "resting_hr", "bpm", "{:.0f}", "down"),
        tile("HRV", "hrv_ms", "ms", "{:.0f}", "up"),
        tile("Daylight / day", "daylight_min", "min", "{:.0f}", "up"),
        tile("Exercise / day", "exercise_min", "min", "{:.0f}", "up"),
    ]
    for t in tiles:
        t["text"] = None if t["value"] is None else t["fmt"].format(t["value"])
        t["base_text"] = None if t["base"] is None else t["fmt"].format(t["base"])
        if t["delta"] is None or t["base"] in (None, 0) or abs(t["delta"]) / abs(t["base"]) < 0.03:
            t["trend"] = "flat"
        else:
            t["trend"] = "up" if t["delta"] > 0 else "down"
    workouts_7d = Workout.objects.filter(start__gte=day_start(recent_lo)).count()
    latest = HealthDay.objects.order_by("-day").values_list("day", flat=True).first()
    return {"tiles": tiles, "workouts_7d": workouts_7d, "latest_day": latest}


def series(range_key, now=None):
    """Daily series on a continuous day axis (null for days without data),
    7-day trailing means for the noisy ones, and workouts per week by type."""
    lo, hi = range_start(range_key, now), today(now)
    if lo is None:
        return {"days": [], "series": {}, "avg7": {}, "weeks": {"x": [], "types": []}}
    by_day = {r.day: r for r in HealthDay.objects.filter(day__gte=lo, day__lte=hi)}
    days = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
    out = {f: [getattr(by_day[d], f) if d in by_day else None for d in days] for f in SERIES}
    out["sleep_unstaged_hr"] = [unstaged(by_day.get(d)) for d in days]
    avg7 = {f: rolling(out[f]) for f in ("steps", "resting_hr", "hrv_ms", "daylight_min", "exercise_min", "sleep_hr")}
    return {"days": [d.isoformat() for d in days], "series": out, "avg7": avg7, "weeks": weekly_workouts(lo, hi)}


def unstaged(row):
    """Sleep the stages don't account for: all of it on nights recorded
    before Apple tracked stages (2022), else any remainder."""
    if row is None or not row.sleep_hr:
        return None
    rest = row.sleep_hr - sum(v or 0 for v in (row.sleep_deep_hr, row.sleep_core_hr, row.sleep_rem_hr))
    return round(rest, 3) if rest > 0.05 else None


def weekly_workouts(lo: date, hi: date):
    """Minutes per ISO week (Monday start) by workout type; the top
    TOP_TYPES types by total minutes are named, the rest are "Other"."""
    qs = Workout.objects.filter(start__gte=day_start(lo), start__lt=day_start(hi + timedelta(days=1)))
    rows = [(local_day(s), n, (sec or 0) / 60) for s, n, sec in qs.values_list("start", "name", "duration_seconds")]
    totals = Counter()
    for _, n, m in rows:
        totals[n] += m
    named = [n for n, _ in totals.most_common(TOP_TYPES)]
    week0 = lo - timedelta(days=lo.weekday())
    weeks = [week0 + timedelta(weeks=i) for i in range((hi - week0).days // 7 + 1)]
    grid = defaultdict(lambda: [0.0] * len(weeks))
    for d, n, m in rows:
        grid[n if n in named else "Other"][(d - week0).days // 7] += m
    order = named + (["Other"] if "Other" in grid else [])
    return {"x": [w.isoformat() for w in weeks],
            "types": [{"name": n, "minutes": [round(v, 1) for v in grid[n]]} for n in order]}


def workouts(range_key, now=None, limit=50):
    lo = range_start(range_key, now)
    qs = Workout.objects.defer("raw")
    if lo is not None:
        qs = qs.filter(start__gte=day_start(lo))
    out = list(qs.order_by("-start")[:limit])
    for w in out:
        w.duration_text = duration_text(w.duration_seconds)
    return out


def duration_text(seconds):
    if not seconds:
        return ""
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _decimate(seq, n):
    if len(seq) <= n:
        return seq
    step = len(seq) / n
    picked = [seq[int(i * step)] for i in range(n)]
    if picked[-1] is not seq[-1]:
        picked.append(seq[-1])
    return picked


def workout_detail(w: Workout):
    """Route (decimated), elevation and heart rate over elapsed minutes."""
    pts = list(WorkoutRoutePoint.objects.filter(workout=w).order_by("at").values_list("at", "lat", "lon", "altitude"))
    pts = _decimate(pts, MAX_ROUTE_POINTS)
    t0 = w.start

    def mins(at):
        return round((at - t0).total_seconds() / 60, 2)

    route = {"lat": [p[1] for p in pts], "lon": [p[2] for p in pts]}
    elev = {"x": [mins(p[0]) for p in pts if p[3] is not None], "y": [p[3] for p in pts if p[3] is not None]}
    hr = {"x": [], "y": []}
    for pt in (w.raw or {}).get("heartRateData") or []:
        at = parse_date(pt.get("date")) if isinstance(pt, dict) else None
        avg = pt.get("Avg", pt.get("qty")) if isinstance(pt, dict) else None
        if at is not None and isinstance(avg, (int, float)):
            hr["x"].append(mins(at))
            hr["y"].append(avg)
    return {"route": route, "elevation": elev, "heart_rate": hr}


def coverage(now=None):
    """Share of days per month with data, per data type, from
    HEALTH_HISTORY_START (or the first data, if earlier) to this month.
    For Workouts and Routes it is days with at least one workout / route,
    so a low share there is normal; what matters is a month at zero."""
    from django.conf import settings

    end = today(now)
    start = date.fromisoformat(settings.HEALTH_HISTORY_START + "-01")
    first = HealthDay.objects.order_by("day").values_list("day", flat=True).first()
    if first and first < start:
        start = first.replace(day=1)
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    idx = {ym: i for i, ym in enumerate(months)}
    counts = {key: [0] * len(months) for _, key in COVERAGE}
    fields = [k for _, k in COVERAGE if k not in ("workouts", "routes")]
    for row in HealthDay.objects.filter(day__gte=start).values("day", "workouts", *fields).iterator():
        i = idx.get((row["day"].year, row["day"].month))
        if i is None:
            continue
        for f in fields:
            if row[f] is not None:
                counts[f][i] += 1
        if row["workouts"]:
            counts["workouts"][i] += 1
    route_days = {local_day(s) for s in Workout.objects.filter(route_points__gt=0).values_list("start", flat=True)}
    for d in route_days:
        i = idx.get((d.year, d.month))
        if i is not None:
            counts["routes"][i] += 1

    def days_in(y, m):
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        last = min(nxt - timedelta(days=1), end)
        return (last - date(y, m, 1)).days + 1

    totals = [days_in(y, m) for y, m in months]
    return {
        "months": [f"{y}-{m:02d}" for y, m in months],
        "days": totals,
        "rows": [{"label": label, "days_with_data": counts[key],
                  "share": [round(c / t, 3) if t else 0 for c, t in zip(counts[key], totals)]}
                 for label, key in COVERAGE],
    }
