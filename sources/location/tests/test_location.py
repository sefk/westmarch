import gzip
import json

import pytest
from django.core.management import call_command

from core.models import LoaderRun
from sources.location.models import LocationBatch, LocationPoint

pytestmark = pytest.mark.django_db
URL = "/ingest/location/"
TOKEN = "loc-token-not-real"


@pytest.fixture(autouse=True)
def cfg(settings, tmp_path):
    settings.LOCATION_INGEST_TOKEN = TOKEN
    settings.RAW_DIR = tmp_path / "raw"
    settings.RAW_DIR.mkdir()
    settings.VAR_DIR = tmp_path / "var"


def feature(ts="2026-11-09T18:00:00Z", lon=-122.16, lat=37.44, **props):
    p = {"timestamp": ts, "altitude": 20, "speed": 1.4, "course": -1, "horizontal_accuracy": 5,
         "vertical_accuracy": -1, "motion": ["walking"], "battery_level": 0.8, "battery_state": "unplugged",
         "device_id": "phone", "wifi": "home", "activity": "fitness", **props}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": p}


def post(client, payload, auth=("bearer", TOKEN), raw=None, url=URL):
    kind, tok = auth if auth else (None, None)
    headers = {}
    if kind == "bearer":
        headers["HTTP_AUTHORIZATION"] = f"Bearer {tok}"
    elif kind == "header":
        headers["HTTP_X_INGEST_TOKEN"] = tok
    elif kind == "query":
        url = f"{url}?access_token={tok}"
    body = raw if raw is not None else json.dumps(payload)
    return client.post(url, data=body, content_type="application/json", **headers)


def test_auth_variants(client, settings):
    payload = {"locations": [feature()]}
    assert post(client, payload, auth=None).status_code == 401
    assert post(client, payload, auth=("bearer", "wrong")).status_code == 401
    for auth in [("bearer", TOKEN), ("header", TOKEN), ("query", TOKEN)]:
        r = post(client, payload, auth=auth)
        assert r.status_code == 200 and r.json()["result"] == "ok"
    assert client.get(URL).status_code == 405
    settings.LOCATION_INGEST_TOKEN = ""
    assert post(client, payload).status_code == 503


def test_point_fields_and_unknowns(client):
    r = post(client, {"locations": [feature()], "current": {}, "trip": {}}).json()
    assert r == {"result": "ok", "points": 1, "skipped": 0}
    p = LocationPoint.objects.get()
    assert (p.lat, p.lon, p.altitude, p.speed, p.h_accuracy) == (37.44, -122.16, 20, 1.4, 5)
    assert p.course is None and p.v_accuracy is None  # iOS -1 means unknown
    assert p.motion == ["walking"] and p.device_id == "phone" and p.battery_state == "unplugged"
    assert p.props == {"activity": "fitness"}
    assert p.at.isoformat() == "2026-11-09T18:00:00+00:00"


def test_idempotent_and_dedup(client):
    payload = {"locations": [feature(), feature()]}
    post(client, payload)
    post(client, {"locations": [feature(speed=3.0)]})
    assert LocationPoint.objects.count() == 1 and LocationPoint.objects.get().speed == 3.0
    assert LoaderRun.objects.filter(loader="location", ok=True).count() == 2


def test_skips_bad_features(client):
    bad = [feature(ts="nope"), feature(lat=95), {"type": "Feature"}, "str",
           {"geometry": {"coordinates": [1]}, "properties": {}}]
    r = post(client, {"locations": [feature()] + bad}).json()
    assert r["points"] == 1 and r["skipped"] == 5


def test_visit_kept(client):
    visit = feature(ts="2026-11-09T19:00:00Z", type="visit", arrival_date="2026-11-09T18:30:00Z",
                    departure_date="2026-11-09T19:00:00Z")
    post(client, {"locations": [visit]})
    assert LocationPoint.objects.get().props["type"] == "visit"


def test_bad_json_and_no_slash(client):
    assert post(client, None, raw="{nope").status_code == 400
    assert post(client, {"locations": [feature()]}, url="/ingest/location").status_code == 200


def test_raw_written(client, settings):
    payload = {"locations": [feature()]}
    post(client, payload)
    post(client, payload)
    files = list((settings.RAW_DIR / "location").rglob("*.json.gz"))
    assert len(files) == 1 and json.loads(gzip.decompress(files[0].read_bytes())) == payload
    assert LocationBatch.objects.first().raw_path == str(files[0])


def test_status(client):
    post(client, {"locations": [feature(), feature(ts="2026-11-09T18:01:00Z")]})
    d = client.get("/ingest/location/status/").json()
    assert d["points"] == 2 and d["devices"][0]["device_id"] == "phone" and d["batches"] == 1


def test_import_command(tmp_path):
    f = tmp_path / "o.json.gz"
    f.write_bytes(gzip.compress(json.dumps({"locations": [feature()]}).encode()))
    call_command("location_import", str(f))
    assert LocationPoint.objects.count() == 1 and LocationBatch.objects.get().raw_path == str(f.resolve())
