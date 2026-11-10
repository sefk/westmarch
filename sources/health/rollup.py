"""Rebuild HealthDay rows from the raw health tables.

Called after every ingest for the days that ingest touched, and by
`manage.py health_rollup --full` for everything.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .models import HealthDay, Metric, SleepNight, Workout

KM_PER_MI = 1.609344

# metric name -> (HealthDay field, how to combine a day's points)
SUMS = {
    "step_count": "steps",
    "active_energy": "active_kcal",
    "apple_exercise_time": "exercise_min",
    "apple_stand_hour": "stand_hours",
    "time_in_daylight": "daylight_min",
    "walking_running_distance": "distance_mi",
    "flights_climbed": "flights",
}
MEANS = {
    "resting_heart_rate": "resting_hr",
    "heart_rate_variability": "hrv_ms",
    "walking_heart_rate_average": "walking_hr",
    "respiratory_rate": "respiratory_rate",
    "blood_oxygen_saturation": "spo2_pct",
}
LASTS = {"vo2_max": "vo2_max"}
NAMES = [*SUMS, *MEANS, *LASTS]


def local_day(dt: datetime) -> date:
    return timezone.localtime(dt).date()


def day_start(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


def _qty(name, units, qty):
    if name == "walking_running_distance" and units.lower() in ("km", "kilometers"):
        return qty / KM_PER_MI
    if name == "active_energy" and units.lower() in ("kj", "kilojoules"):
        return qty / 4.184
    return qty


def _build(days: set[date]) -> list[HealthDay]:
    lo, hi = day_start(min(days)), day_start(max(days) + timedelta(days=1))
    acc = defaultdict(lambda: defaultdict(list))  # day -> field -> [(at, qty)]
    rows = (
        Metric.objects.filter(name__in=NAMES, at__gte=lo, at__lt=hi, qty__isnull=False)
        .order_by("at")
        .values_list("name", "units", "at", "qty")
        .iterator(chunk_size=5000)
    )
    for name, units, at, qty in rows:
        d = local_day(at)
        if d in days:
            field = SUMS.get(name) or MEANS.get(name) or LASTS[name]
            acc[d][field].append(_qty(name, units, qty))

    sleep = {}
    for n in SleepNight.objects.filter(night__in=days):
        best = sleep.get(n.night)
        if best is None or (n.total_sleep_hr or 0) > (best.total_sleep_hr or 0):
            sleep[n.night] = n

    work = defaultdict(lambda: [0, 0.0, 0.0])
    for start, secs, kcal in Workout.objects.filter(start__gte=lo, start__lt=hi).values_list(
        "start", "duration_seconds", "active_energy_kcal"
    ):
        d = local_day(start)
        if d in days:
            w = work[d]
            w[0] += 1
            w[1] += (secs or 0) / 60
            w[2] += kcal or 0

    out = []
    for d in sorted(days):
        vals = acc.get(d, {})
        s = sleep.get(d)
        w = work.get(d)
        if not vals and s is None and w is None:
            continue
        row = HealthDay(day=d)
        for field in SUMS.values():
            if field in vals:
                setattr(row, field, sum(vals[field]))
        for field in MEANS.values():
            if field in vals:
                setattr(row, field, sum(vals[field]) / len(vals[field]))
        for field in LASTS.values():
            if field in vals:
                setattr(row, field, vals[field][-1])
        if s is not None:
            row.sleep_hr = s.total_sleep_hr
            row.sleep_deep_hr, row.sleep_core_hr = s.deep_hr, s.core_hr
            row.sleep_rem_hr, row.sleep_awake_hr = s.rem_hr, s.awake_hr
        if w is not None:
            row.workouts, row.workout_min, row.workout_kcal = w[0], w[1], w[2]
        out.append(row)
    return out


@transaction.atomic
def rebuild(days) -> int:
    """Rebuild the given local days (iterable of date). Returns rows written."""
    days = set(days)
    if not days:
        return 0
    n = 0
    ordered = sorted(days)
    # Contiguous-ish chunks of at most 31 days keep each metric query bounded.
    for i in range(0, len(ordered), 31):
        chunk = set(ordered[i : i + 31])
        HealthDay.objects.filter(day__in=chunk).delete()
        rows = _build(chunk)
        HealthDay.objects.bulk_create(rows)
        n += len(rows)
    return n


def all_days() -> set[date]:
    first = [
        x
        for x in (
            Metric.objects.order_by("at").values_list("at", flat=True).first(),
            Workout.objects.order_by("start").values_list("start", flat=True).first(),
        )
        if x
    ]
    nights = SleepNight.objects.order_by("night").values_list("night", flat=True)
    starts = [local_day(x) for x in first] + ([nights.first()] if nights.exists() else [])
    if not starts:
        return set()
    lo, hi = min(starts), local_day(timezone.now())
    hi = max(hi, nights.last() or hi)
    return {lo + timedelta(days=i) for i in range((hi - lo).days + 1)}
