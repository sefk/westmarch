from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from catalog import health as hl
from sources.health.models import HealthDay, Metric, SleepNight, Workout, WorkoutRoutePoint
from sources.health.rollup import all_days, rebuild

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=dt_timezone.utc)  # Nov 9, 12:00 PST


def at(d, hour):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour))


def metric(name, when, qty, units="", source=""):
    return Metric.objects.create(name=name, at=when, qty=qty, units=units, source=source)


def test_rollup_sums_means_and_local_day():
    d = date(2026, 11, 8)
    metric("step_count", at(d, 9), 1000)
    metric("step_count", at(d, 23), 500)  # 23:00 PST is Nov 9 UTC; still the 8th locally
    metric("step_count", at(d + timedelta(days=1), 1), 7)
    metric("resting_heart_rate", at(d, 7), 60)
    metric("resting_heart_rate", at(d, 8), 64)
    metric("walking_running_distance", at(d, 9), 2.0, units="km")
    metric("heart_rate", at(d, 9), 120)  # not rolled up
    SleepNight.objects.create(night=d, source="Watch", total_sleep_hr=7.0, deep_hr=1.0)
    SleepNight.objects.create(night=d, source="iPhone", total_sleep_hr=5.0)
    Workout.objects.create(external_id="w", name="Walk", start=at(d, 10), duration_seconds=1800,
                           active_energy_kcal=150)
    assert rebuild({d, d + timedelta(days=1)}) == 2
    r = HealthDay.objects.get(day=d)
    assert r.steps == 1500 and r.resting_hr == 62 and round(r.distance_mi, 3) == 1.243
    assert r.sleep_hr == 7.0 and r.sleep_deep_hr == 1.0  # the longer source wins
    assert (r.workouts, r.workout_min, r.workout_kcal) == (1, 30, 150)
    assert r.hrv_ms is None  # no data is null, not zero
    assert HealthDay.objects.get(day=d + timedelta(days=1)).steps == 7
    # idempotent
    rebuild({d})
    assert HealthDay.objects.filter(day=d).count() == 1


def test_ingest_rebuilds_touched_days(client, settings, tmp_path):
    settings.HEALTH_INGEST_TOKEN = "t"
    settings.RAW_DIR = tmp_path
    body = {"data": {"metrics": [{"name": "step_count", "units": "count",
                                  "data": [{"date": "2026-11-01 07:00:00 -0800", "qty": 321}]}]}}
    client.post("/ingest/health/", data=body, content_type="application/json", HTTP_X_INGEST_TOKEN="t")
    assert HealthDay.objects.get(day=date(2026, 11, 1)).steps == 321


def test_all_days_spans_data():
    metric("step_count", at(date(2026, 11, 1), 9), 1)
    days = all_days()
    assert min(days) == date(2026, 11, 1) and max(days) >= date(2026, 11, 1)


def test_rolling():
    assert hl.rolling([1, None, 3, 5, None, 7, 9], window=3, min_count=2) == [None, None, 2, 4, 4, 6, 8]


def test_series_and_summary():
    end = date(2026, 11, 9)
    for i in range(100):
        HealthDay.objects.create(day=end - timedelta(days=i), steps=8000 if i < 7 else 5000, resting_hr=60)
    Workout.objects.create(external_id="a", name="Hiking", start=at(end, 9), duration_seconds=3600)
    Workout.objects.create(external_id="b", name="Yoga", start=at(end - timedelta(days=1), 9), duration_seconds=600)
    s = hl.series("30d", now=NOW)
    assert len(s["days"]) == 30 and s["days"][-1] == "2026-11-09"
    assert s["series"]["steps"][-1] == 8000 and s["series"]["hrv_ms"][-1] is None
    assert {t["name"] for t in s["weeks"]["types"]} == {"Hiking", "Yoga"}
    assert sum(sum(t["minutes"]) for t in s["weeks"]["types"]) == 70
    tiles = {t["label"]: t for t in hl.summary(now=NOW)["tiles"]}
    assert tiles["Steps / day"]["text"] == "8,000" and tiles["Steps / day"]["trend"] == "up"
    assert tiles["Resting HR"]["trend"] == "flat" and tiles["HRV"]["text"] is None


def test_weekly_folds_other():
    d = date(2026, 11, 2)
    for i, name in enumerate(["A", "B", "C", "D", "E", "F"]):
        Workout.objects.create(external_id=name, name=name, start=at(d, 8), duration_seconds=(10 - i) * 60)
    w = hl.weekly_workouts(d, d)
    assert [t["name"] for t in w["types"]] == ["A", "B", "C", "D", "Other"]
    assert w["types"][-1]["minutes"] == [11.0]  # E 6 + F 5


def test_coverage(settings):
    settings.HEALTH_HISTORY_START = "2026-09"
    HealthDay.objects.create(day=date(2026, 10, 1), steps=1, sleep_hr=7)
    HealthDay.objects.create(day=date(2026, 10, 2), steps=1)
    w = Workout.objects.create(external_id="r", name="Walk", start=at(date(2026, 10, 2), 9), route_points=2)
    WorkoutRoutePoint.objects.create(workout=w, at=at(date(2026, 10, 2), 9), lat=1, lon=1)
    c = hl.coverage(now=NOW)
    assert c["months"] == ["2026-09", "2026-10", "2026-11"] and c["days"] == [30, 31, 9]
    rows = {r["label"]: r for r in c["rows"]}
    assert rows["Steps"]["days_with_data"] == [0, 2, 0]
    assert rows["Sleep"]["days_with_data"] == [0, 1, 0]
    assert rows["Routes"]["days_with_data"] == [0, 1, 0]
    assert rows["Steps"]["share"][1] == round(2 / 31, 3)


def test_workout_detail_and_pages(client):
    w = Workout.objects.create(external_id="x", name="Hiking", start=at(date(2026, 11, 8), 11), duration_seconds=7020,
                               route_points=3, raw={"heartRateData": [
                                   {"date": "2026-11-08 11:05:00 -0800", "Avg": 95.9, "Min": 90, "Max": 100}]})
    for i in range(3):
        WorkoutRoutePoint.objects.create(workout=w, at=w.start + timedelta(minutes=i), lat=38 + i / 100, lon=-123,
                                         altitude=100 + i)
    d = hl.workout_detail(w)
    assert d["route"]["lat"] == [38, 38.01, 38.02] and d["elevation"]["x"] == [0, 1, 2]
    assert d["heart_rate"] == {"x": [5.0], "y": [95.9]}
    rebuild({date(2026, 11, 8)})
    page = client.get("/body/?range=all")
    assert page.status_code == 200 and b"How am I doing physically?" in page.content
    assert b"1h 57m" in page.content
    assert client.get("/body/data/?range=bogus").json()["days"]
    assert client.get("/body/coverage/").json()["rows"]
    assert client.get(f"/body/workouts/{w.pk}/").status_code == 200
    assert client.get(f"/body/workouts/{w.pk}/data/").json()["route"]["lon"] == [-123] * 3
    assert client.get("/body/workouts/999999/").status_code == 404


def test_empty_page(client):
    r = client.get("/body/")
    assert r.status_code == 200 and b"No health data yet" in r.content


def test_decimate():
    assert len(hl._decimate(list(range(10_000)), 2000)) <= 2001
    assert hl._decimate([1, 2], 5) == [1, 2]
