import gzip
import json
import zipfile

import pytest
from django.core.management import call_command

from core.models import LoaderRun
from sources.location.models import LocationPoint, TripSegment, Visit
from sources.location.timeline import detect, import_path, latlng

pytestmark = pytest.mark.django_db

IOS = [
    {"startTime": "2024-03-01T08:00:00.000-08:00", "endTime": "2024-03-01T09:00:00.000-08:00",
     "visit": {"hierarchyLevel": "0", "probability": "0.9",
               "topCandidate": {"probability": "0.8", "semanticType": "Home", "placeID": "ChIJhome",
                                "placeLocation": "geo:37.440000,-122.160000"}}},
    {"startTime": "2024-03-01T09:00:00.000-08:00", "endTime": "2024-03-01T09:20:00.000-08:00",
     "activity": {"start": "geo:37.44,-122.16", "end": "geo:37.45,-122.14", "distanceMeters": "2100.5",
                  "topCandidate": {"type": "walking", "probability": "0.7"}}},
    {"startTime": "2024-03-01T09:00:00.000-08:00", "endTime": "2024-03-01T11:00:00.000-08:00",
     "timelinePath": [{"point": "geo:37.441,-122.159", "durationMinutesOffsetFromStartTime": "5"},
                      {"point": "geo:37.442,-122.158", "durationMinutesOffsetFromStartTime": "10"}]},
]

ANDROID = {
    "semanticSegments": [
        {"startTime": "2024-03-02T08:00:00.000-08:00", "endTime": "2024-03-02T09:00:00.000-08:00",
         "visit": {"topCandidate": {"placeId": "ChIJwork", "semanticType": "WORK", "probability": 0.6,
                                    "placeLocation": {"latLng": "37.4°, -122.1°"}}}},
        {"startTime": "2024-03-02T09:00:00.000-08:00", "endTime": "2024-03-02T10:00:00.000-08:00",
         "timelinePath": [{"point": "37.41°, -122.11°", "time": "2024-03-02T09:05:00.000-08:00"}]},
    ],
    "rawSignals": [
        {"position": {"LatLng": "37.42°, -122.12°", "accuracyMeters": 12, "altitudeMeters": 30.5,
                      "source": "WIFI", "timestamp": "2024-03-02T09:30:00.000-08:00"}},
        {"wifiScan": {"deliveryTime": "2024-03-02T09:31:00.000-08:00"}},
    ],
    "userLocationProfile": {"frequentPlaces": []},
}

RECORDS = {"locations": [
    {"latitudeE7": 374400000, "longitudeE7": -1221600000, "accuracy": 20, "altitude": 15, "velocity": 0,
     "timestamp": "2015-06-01T12:00:00.123Z", "source": "GPS",
     "activity": [{"activity": [{"type": "STILL", "confidence": 90}, {"type": "WALKING", "confidence": 10}]}]},
    {"latitudeE7": 374410000, "longitudeE7": -1221610000, "accuracy": 25, "timestampMs": "1433160060000"},
]}

SEMANTIC = {"timelineObjects": [
    {"placeVisit": {"location": {"latitudeE7": 374400000, "longitudeE7": -1221600000, "placeId": "ChIJcafe",
                                 "name": "Cafe", "address": "1 Main St"},
                    "duration": {"startTimestamp": "2015-06-01T15:00:00Z", "endTimestamp": "2015-06-01T16:00:00Z"},
                    "visitConfidence": 85}},
    {"activitySegment": {"startLocation": {"latitudeE7": 374400000, "longitudeE7": -1221600000},
                         "endLocation": {"latitudeE7": 377700000, "longitudeE7": -1224100000},
                         "duration": {"startTimestampMs": "1433174400000", "endTimestampMs": "1433178000000"},
                         "distance": 45000, "activityType": "IN_PASSENGER_VEHICLE", "confidence": "HIGH",
                         "simplifiedRawPath": {"points": [{"latE7": 375000000, "lngE7": -1222000000,
                                                             "timestamp": "2015-06-01T17:30:00Z"}]}}},
]}


def write(tmp_path, name, data, gz=False):
    p = tmp_path / name
    raw = json.dumps(data).encode()
    p.write_bytes(gzip.compress(raw) if gz else raw)
    return p


def test_latlng_forms():
    assert latlng("geo:37.44,-122.16") == (37.44, -122.16)
    assert latlng("37.4°, -122.1°") == (37.4, -122.1)
    assert latlng({"latLng": "1°, 2°"}) == (1, 2)
    assert latlng({"latitudeE7": 374400000, "longitudeE7": -1221600000}) == (37.44, -122.16)
    assert latlng({"latE7": 374400000, "lngE7": -1221600000}) == (37.44, -122.16)
    assert latlng("geo:99,0") == (None, None) and latlng(None) == (None, None)


def test_detect():
    assert detect(b"  [{") == "timeline-ios"
    assert detect(b'{"semanticSegments": [') == "timeline-android"
    assert detect(b'{"locations": [') == "takeout-records"
    assert detect(b'{"timelineObjects": [') == "takeout-semantic"
    assert detect(b'{"other": 1}') is None


def test_ios(tmp_path):
    c = import_path(write(tmp_path, "location-history.json", IOS), log=lambda m: None)
    assert c == {"points": 2, "visits": 1, "trips": 1}
    v = Visit.objects.get()
    assert (v.source, v.lat, v.lon, v.place_id, v.semantic_type, v.probability) == (
        "timeline-ios", 37.44, -122.16, "ChIJhome", "Home", 0.8)
    t = TripSegment.objects.get()
    assert (t.mode, t.distance_m, t.end_lat) == ("walking", 2100.5, 37.45)
    pts = list(LocationPoint.objects.order_by("at"))
    assert pts[0].device_id == "google-timeline" and pts[0].at.isoformat() == "2024-03-01T17:05:00+00:00"


def test_android(tmp_path):
    c = import_path(write(tmp_path, "Timeline.json", ANDROID), log=lambda m: None)
    assert c == {"points": 2, "visits": 1, "trips": 0}
    assert Visit.objects.get().semantic_type == "WORK"
    raw = LocationPoint.objects.get(device_id="google-raw")
    assert (raw.h_accuracy, raw.altitude, raw.props) == (12, 30.5, {"source": "WIFI"})


def test_records_gz(tmp_path):
    c = import_path(write(tmp_path, "Records.json.gz", RECORDS, gz=True), log=lambda m: None)
    assert c["points"] == 2
    p = LocationPoint.objects.order_by("at").first()
    assert (p.lat, p.lon, p.h_accuracy, p.motion, p.device_id) == (37.44, -122.16, 20, ["still"], "google-records")
    assert LocationPoint.objects.order_by("at").last().at.isoformat() == "2015-06-01T12:01:00+00:00"


def test_semantic_in_takeout_zip_and_idempotent(tmp_path):
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Location History/Semantic Location History/2015/2015_JUNE.json", json.dumps(SEMANTIC))
        zf.writestr("Takeout/Location History/Records.json", json.dumps(RECORDS))
        zf.writestr("Takeout/Mail/readme.json", "{}")
    import_path(z, log=lambda m: None)
    import_path(z, log=lambda m: None)
    assert Visit.objects.count() == 1 and TripSegment.objects.count() == 1 and LocationPoint.objects.count() == 3
    assert LocationPoint.objects.filter(device_id="google-timeline", lat=37.5, lon=-122.2).exists()  # latE7 path point
    v = Visit.objects.get()
    assert (v.name, v.address, v.probability) == ("Cafe", "1 Main St", 85)
    t = TripSegment.objects.get()
    assert t.mode == "in passenger vehicle" and t.distance_m == 45000 and t.end_lon == -122.41


def test_command_and_unknown_file(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    write(d, "a.json", IOS)
    write(d, "junk.json", {"hello": 1})
    call_command("timeline_import", str(d))
    run = LoaderRun.objects.get(loader="timeline")
    assert run.ok and run.rows == 4
