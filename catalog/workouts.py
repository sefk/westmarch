"""Workout browser: every workout, filtered by time, type, route and
duration, with totals, minutes per week by type, and all matching routes
on one map."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.paginator import Paginator
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from sources.health.models import Workout, WorkoutRoutePoint
from sources.health.rollup import day_start, local_day

PRESETS = {"30d": 30, "90d": 90, "1y": 365, "all": None}
DEFAULT_PRESET = "1y"
SORTS = {
    "date": "-start",
    "duration": "-duration_seconds",
    "distance": "-distance",
    "kcal": "-active_energy_kcal",
    "hr": "-avg_heart_rate",
}
KM_PER_MI = 1.609344
PAGE_SIZE = 100
MAX_ROUTES = 300
POINTS_PER_ROUTE = 150
TOP_TYPES = 5
MONTHLY_AFTER_DAYS = 730


def _date(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _num(s):
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


@dataclass
class Filters:
    preset: str = DEFAULT_PRESET
    start: date | None = None
    end: date | None = None
    types: list[str] = field(default_factory=list)
    route: str = "any"  # any | yes | no
    min_minutes: float | None = None
    sort: str = "date"
    page: int = 1

    @classmethod
    def from_request(cls, get, today=None):
        today = today or timezone.localdate()
        f = cls()
        f.start, f.end = _date(get.get("from")), _date(get.get("to"))
        if f.start or f.end:
            f.preset = "custom"
        else:
            f.preset = get.get("range") if get.get("range") in PRESETS else DEFAULT_PRESET
            n = PRESETS[f.preset]
            f.start = today - timedelta(days=n - 1) if n else None
        if f.start and f.end and f.start > f.end:
            f.start, f.end = f.end, f.start
        f.types = [t for t in get.getlist("type") if t]
        f.route = get.get("route") if get.get("route") in ("yes", "no") else "any"
        f.min_minutes = _num(get.get("min"))
        f.sort = get.get("sort") if get.get("sort") in SORTS else "date"
        try:
            f.page = max(1, int(get.get("page", 1)))
        except ValueError:
            f.page = 1
        return f

    def queryset(self):
        qs = Workout.objects.defer("raw")
        if self.start:
            qs = qs.filter(start__gte=day_start(self.start))
        if self.end:
            qs = qs.filter(start__lt=day_start(self.end + timedelta(days=1)))
        if self.types:
            qs = qs.filter(name__in=self.types)
        if self.route == "yes":
            qs = qs.filter(route_points__gt=0)
        elif self.route == "no":
            qs = qs.filter(route_points=0)
        if self.min_minutes:
            qs = qs.filter(duration_seconds__gte=self.min_minutes * 60)
        return qs

    def query(self, **override):
        """The filters as a query string, with some values replaced."""
        from urllib.parse import urlencode

        d = {"sort": self.sort, "route": self.route}
        if self.preset == "custom":
            d["from"] = self.start.isoformat() if self.start else ""
            d["to"] = self.end.isoformat() if self.end else ""
        else:
            d["range"] = self.preset
        if self.min_minutes:
            d["min"] = f"{self.min_minutes:g}"
        d.update(override)
        pairs = [(k, v) for k, v in d.items() if v not in (None, "")]
        pairs += [("type", t) for t in self.types]
        return urlencode(pairs)


def km(distance, units):
    if distance is None:
        return None
    return distance * KM_PER_MI if (units or "").lower() in ("mi", "mile", "miles") else distance


def duration_text(seconds):
    if not seconds:
        return ""
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def all_types():
    return list(Workout.objects.values("name").annotate(n=Count("id")).order_by("-n").values_list("name", "n"))


def summary(qs):
    agg = qs.aggregate(n=Count("id"), secs=Sum("duration_seconds"), kcal=Sum("active_energy_kcal"),
                       hr=Avg("avg_heart_rate"))
    dist = sum(km(d, u) or 0 for d, u in qs.values_list("distance", "distance_units"))
    routes = qs.filter(route_points__gt=0).count()
    return {"count": agg["n"], "time": duration_text(agg["secs"] or 0) or "0m", "km": dist,
            "kcal": agg["kcal"] or 0, "hr": agg["hr"], "routes": routes}


def page(f: Filters):
    qs = f.queryset().order_by(SORTS[f.sort], "-start")
    p = Paginator(qs, PAGE_SIZE).get_page(f.page)
    for w in p.object_list:
        w.duration_text = duration_text(w.duration_seconds)
        w.km = km(w.distance, w.distance_units)
    return p


def chart(f: Filters, today=None):
    """Minutes per week (per month past two years) by type over the
    filtered workouts."""
    today = today or timezone.localdate()
    rows = [(local_day(s), n, (sec or 0) / 60) for s, n, sec in
            f.queryset().values_list("start", "name", "duration_seconds")]
    if not rows:
        return {"x": [], "types": [], "bucket": "week"}
    lo = f.start or min(r[0] for r in rows)
    hi = f.end or today
    totals = Counter()
    for _, n, m in rows:
        totals[n] += m
    named = [n for n, _ in totals.most_common(TOP_TYPES)]
    monthly = (hi - lo).days > MONTHLY_AFTER_DAYS  # weekly bars over years are hairlines
    if monthly:
        starts = []
        y, mo = lo.year, lo.month
        while (y, mo) <= (hi.year, hi.month):
            starts.append(date(y, mo, 1))
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        index = {(d.year, d.month): i for i, d in enumerate(starts)}
        slot = lambda d: index.get((d.year, d.month))  # noqa: E731
    else:
        w0 = lo - timedelta(days=lo.weekday())
        starts = [w0 + timedelta(weeks=i) for i in range((hi - w0).days // 7 + 1)]
        slot = lambda d: (d - w0).days // 7 if 0 <= (d - w0).days // 7 < len(starts) else None  # noqa: E731
    grid = defaultdict(lambda: [0.0] * len(starts))
    for d, n, m in rows:
        i = slot(d)
        if i is not None:
            grid[n if n in named else "Other"][i] += m
    order = named + (["Other"] if "Other" in grid else [])
    return {"x": [d.isoformat() for d in starts], "bucket": "month" if monthly else "week",
            "types": [{"name": n, "minutes": [round(v, 1) for v in grid[n]]} for n in order]}


def routes(f: Filters):
    """Up to MAX_ROUTES routes in the filtered set, newest first, each
    thinned to about POINTS_PER_ROUTE points."""
    ws = list(f.queryset().filter(route_points__gt=0).order_by("-start")
              .values("id", "name", "start", "route_points")[:MAX_ROUTES])
    if not ws:
        return {"routes": [], "truncated": False}
    by_id = {w["id"]: w for w in ws}
    pts = defaultdict(list)
    for wid, lat, lon in (WorkoutRoutePoint.objects.filter(workout_id__in=by_id).order_by("workout_id", "at")
                          .values_list("workout_id", "lat", "lon").iterator(chunk_size=20000)):
        pts[wid].append((lat, lon))
    out = []
    for w in ws:
        p = pts.get(w["id"], [])
        if not p:
            continue
        step = max(1, -(-len(p) // POINTS_PER_ROUTE))  # ceil, so at most POINTS_PER_ROUTE (+ the end)
        thin = p[::step]
        if thin[-1] != p[-1]:
            thin.append(p[-1])
        out.append({"id": w["id"], "name": w["name"], "date": timezone.localtime(w["start"]).strftime("%b %-d, %Y"),
                    "lat": [round(a, 5) for a, _ in thin], "lon": [round(b, 5) for _, b in thin]})
    total = f.queryset().filter(route_points__gt=0).count()
    return {"routes": out, "truncated": total > len(out), "total": total}
