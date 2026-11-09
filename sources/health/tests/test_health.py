import gzip
import json

import pytest
from django.core.management import call_command

from core.models import LoaderRun
from sources.health.models import IngestBatch, Metric, SleepNight, Workout, WorkoutRoutePoint

pytestmark = pytest.mark.django_db
URL = "/ingest/health/"
TOKEN = "test-token-not-real"


@pytest.fixture(autouse=True)
def cfg(settings, tmp_path):
    settings.HEALTH_INGEST_TOKEN = TOKEN
    settings.RAW_DIR = tmp_path / "raw"
    settings.RAW_DIR.mkdir()
    settings.VAR_DIR = tmp_path / "var"


def post(client, payload, token=TOKEN, raw=None):
    headers = {"HTTP_X_INGEST_TOKEN": token} if token is not None else {}
    body = raw if raw is not None else json.dumps(payload)
    return client.post(URL, data=body, content_type="application/json", **headers)


def steps(qty=100, date="2026-11-01 07:00:00 -0800"):
    return {"data": {"metrics": [{"name": "step_count", "units": "count",
                                  "data": [{"date": date, "qty": qty, "source": "iPhone"}]}]}}


def test_auth(client, settings):
    assert post(client, steps(), token=None).status_code == 401
    assert post(client, steps(), token="wrong").status_code == 401
    assert client.get(URL).status_code == 405
    r = post(client, steps())
    assert r.status_code == 200 and r.json() == {"ok": True, "metrics": 1, "workouts": 0, "route_points": 0, "skipped": 0}
    assert TOKEN not in r.content.decode()
    settings.HEALTH_INGEST_TOKEN = ""
    assert post(client, steps()).status_code == 503


def test_bad_json(client):
    assert post(client, None, raw="{nope").status_code == 400


def test_quantity_and_idempotent_and_overlap(client):
    post(client, steps(100))
    post(client, steps(100))
    assert Metric.objects.count() == 1
    post(client, steps(250))
    m = Metric.objects.get()
    assert m.qty == 250 and m.at.isoformat() == "2026-11-01T15:00:00+00:00"
    assert LoaderRun.objects.filter(loader="health", ok=True).count() == 3
    assert IngestBatch.objects.count() == 3


def test_heart_rate_and_unknown(client):
    payload = {"data": {"metrics": [
        {"name": "heart_rate", "units": "count/min",
         "data": [{"date": "2026-11-01 07:00:00 -0800", "Min": 60, "Avg": 70, "Max": 90, "source": "Watch"}]},
        {"name": "blood_pressure", "units": "mmHg",
         "data": [{"date": "2026-11-01T08:00:00-08:00", "systolic": 118, "diastolic": 76}]},
        {"name": "brand_new_metric", "units": "x", "data": [{"date": "2026-11-01 07:00:00 -0800", "qty": 1, "extra": "y"}]},
    ]}}
    assert post(client, payload).json()["metrics"] == 3
    hr = Metric.objects.get(name="heart_rate")
    assert hr.qty == 70 and hr.fields == {"Min": 60, "Max": 90, "Avg": 70}
    assert Metric.objects.get(name="blood_pressure").fields["systolic"] == 118
    assert Metric.objects.get(name="brand_new_metric").fields == {"extra": "y"}


def test_sleep(client):
    payload = {"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [
        {"date": "2026-11-02 00:00:00 -0800", "totalSleep": 6.8, "core": 3.9, "deep": 1.2, "rem": 1.7,
         "awake": 0.3, "inBed": 7.4, "sleepStart": "2026-11-01 23:20:00 -0800",
         "sleepEnd": "2026-11-02 06:50:00 -0800", "source": "Watch"},
        {"date": "2026-11-02 01:00:00 -0800", "start": "2026-11-02 01:00:00 -0800",
         "end": "2026-11-02 01:30:00 -0800", "qty": 0.5, "value": "Deep", "source": "Watch"},
    ]}]}}
    assert post(client, payload).json()["metrics"] == 2
    n = SleepNight.objects.get()
    assert str(n.night) == "2026-11-02" and n.total_sleep_hr == 6.8 and n.rem_hr == 1.7
    assert Metric.objects.get(name="sleep_analysis").fields["value"] == "Deep"


def test_workout(client):
    w = {"id": "abc", "name": "Running", "start": "2026-11-01 07:00:00 -0800", "end": "2026-11-01 07:30:00 -0800",
         "duration": 1800, "isIndoor": False, "location": "Outdoor",
         "activeEnergyBurned": {"qty": 350, "units": "kcal"}, "distance": {"qty": 3.5, "units": "mi"},
         "heartRate": {"min": {"qty": 120}, "avg": {"qty": 150}, "max": {"qty": 175}}}
    assert post(client, {"data": {"workouts": [w]}}).json()["workouts"] == 1
    w["distance"]["qty"] = 4.0
    post(client, {"data": {"workouts": [w]}})
    o = Workout.objects.get()
    assert (o.distance, o.distance_units, o.avg_heart_rate, o.max_heart_rate, o.active_energy_kcal) == (4.0, "mi", 150, 175, 350)
    assert o.is_indoor is False and o.duration_seconds == 1800


def test_malformed_point_skipped(client):
    payload = steps()
    payload["data"]["metrics"][0]["data"] += [{"date": "garbage", "qty": 1}, "str", {"qty": 5}]
    r = post(client, payload).json()
    assert r["metrics"] == 1 and r["skipped"] == 3


def test_raw_written_and_deduped(client, settings):
    post(client, steps())
    post(client, steps())
    files = list((settings.RAW_DIR / "health").rglob("*.json.gz"))
    assert len(files) == 1
    assert json.loads(gzip.decompress(files[0].read_bytes())) == steps()
    assert IngestBatch.objects.first().raw_path == str(files[0])


def test_raw_fallback_when_volume_missing(client, settings, tmp_path):
    settings.RAW_DIR = tmp_path / "not-mounted"
    assert post(client, steps()).status_code == 200
    assert len(list((settings.VAR_DIR / "health-raw").rglob("*.json.gz"))) == 1


def test_status(client):
    post(client, steps(42))
    d = client.get("/ingest/health/status/").json()
    assert d["metric_points"] == 1 and d["metrics"][0]["name"] == "step_count" and d["metrics"][0]["latest_qty"] == 42


def test_import_command(tmp_path):
    f = tmp_path / "p.json.gz"
    f.write_bytes(gzip.compress(json.dumps(steps(7)).encode()))
    call_command("health_import", str(f))
    assert Metric.objects.get().qty == 7
    assert IngestBatch.objects.get().raw_path == str(f.resolve())


def route(n=3, start_min=0, v1=False):
    out = []
    for i in range(n):
        ts = f"2026-11-01 07:{start_min + i:02d}:00 -0800"
        if v1:
            out.append({"lat": 37.44 + i / 1000, "lon": -122.16, "altitude": 20, "timestamp": ts})
        else:
            out.append({"latitude": 37.44 + i / 1000, "longitude": -122.16, "altitude": 20 + i, "speed": 2.5,
                        "course": 90, "horizontalAccuracy": 4, "verticalAccuracy": 3, "timestamp": ts})
    return out


def run_workout(**extra):
    return {"id": "r1", "name": "Outdoor Walk", "start": "2026-11-01 07:00:00 -0800",
            "end": "2026-11-01 07:30:00 -0800", **extra}


def test_workout_route(client):
    r = post(client, {"data": {"workouts": [run_workout(route=route(3) + [{"latitude": 999, "longitude": 0,
                                                                          "timestamp": "2026-11-01 07:09:00 -0800"}])]}})
    assert r.json()["route_points"] == 3 and r.json()["skipped"] == 1
    w = Workout.objects.get()
    assert w.route_points == 3 and "route" not in w.raw
    p = w.route.first()
    assert (p.lat, p.lon, p.altitude, p.speed, p.h_accuracy) == (37.44, -122.16, 20, 2.5, 4)
    assert p.at.isoformat() == "2026-11-01T15:00:00+00:00"
    assert IngestBatch.objects.get().route_points == 3


def test_route_resend_replaces_and_routeless_resend_keeps(client):
    post(client, {"data": {"workouts": [run_workout(route=route(3))]}})
    post(client, {"data": {"workouts": [run_workout(route=route(5, start_min=10))]}})
    w = Workout.objects.get()
    assert WorkoutRoutePoint.objects.count() == 5 and w.route_points == 5
    assert w.route.first().at.minute == 10
    post(client, {"data": {"workouts": [run_workout(distance={"qty": 1.2, "units": "mi"})]}})
    w.refresh_from_db()
    assert WorkoutRoutePoint.objects.count() == 5 and w.route_points == 5 and w.distance == 1.2


def test_route_v1_keys(client):
    post(client, {"data": {"workouts": [run_workout(route=route(2, v1=True))]}})
    assert WorkoutRoutePoint.objects.count() == 2


def test_status_counts_routes(client):
    post(client, {"data": {"workouts": [run_workout(route=route(4))]}})
    d = client.get("/ingest/health/status/").json()
    assert d["route_points"] == 4 and d["workouts_with_route"] == 1
