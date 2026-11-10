"""Import Google location history in any of the shapes Google has shipped.

- iOS on-device Timeline export: a top-level list of segments with
  "visit" / "activity" / "timelinePath", coordinates as "geo:lat,lon",
  numbers as strings, path points as minute offsets from startTime.
- Android on-device export: {"semanticSegments": [...], "rawSignals": [...]},
  coordinates as "37.4°, -122.1°".
- Old Takeout Records.json: {"locations": [{latitudeE7, longitudeE7, ...}]}.
- Old Takeout Semantic Location History (YYYY_MONTH.json):
  {"timelineObjects": [{"placeVisit": ...}, {"activitySegment": ...}]}.

Visits go to Visit, movement to TripSegment, GPS fixes to LocationPoint
under a device id naming the source, so they sit beside Overland's points
without colliding. Files are streamed with ijson; nothing is loaded whole.
Re-importing is idempotent (unique on source + start, device + time).
"""

import gzip
import io
import zipfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import ijson
from django.db import transaction

from sources.health.ingest import num, parse_date

from .models import LocationPoint, TripSegment, Visit

FLUSH = 2000
DEVICE = {"timeline-ios": "google-timeline", "timeline-android": "google-timeline",
          "takeout-records": "google-records", "takeout-semantic": "google-timeline"}


# ---- small parsers -------------------------------------------------------

def latlng(v):
    """'geo:37.4,-122.1', '37.4°, -122.1°', {'latLng': ...}, {'latitudeE7', 'longitudeE7'} -> (lat, lon)."""
    if isinstance(v, dict):
        if "latitudeE7" in v and "longitudeE7" in v:
            lat, lon = num(v["latitudeE7"]), num(v["longitudeE7"])
            return (lat / 1e7, lon / 1e7) if lat is not None and lon is not None else (None, None)
        for k in ("latLng", "LatLng", "point"):
            if k in v:
                return latlng(v[k])
        return None, None
    if not isinstance(v, str):
        return None, None
    s = v.strip().removeprefix("geo:").replace("°", "")
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 2:
        return None, None
    lat, lon = num(parts[0]), num(parts[1])
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def when(v):
    """ISO string or epoch-milliseconds string -> aware datetime."""
    if isinstance(v, str) and v.isdigit():
        return datetime.fromtimestamp(int(v) / 1000, tz=dt_timezone.utc)
    return parse_date(v)


def mode_name(v):
    return str(v or "").replace("_", " ").lower()[:50]


def plain(o):
    """ijson yields Decimal; make the raw JSON-storable."""
    if isinstance(o, dict):
        return {k: plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [plain(v) for v in o]
    if hasattr(o, "as_integer_ratio") and not isinstance(o, (int, float, bool)):
        return float(o)
    return o


def _point(device, at, lat, lon, **kw):
    if at is None or lat is None or lon is None:
        return None
    return LocationPoint(device_id=device, at=at, lat=lat, lon=lon, **kw)


# ---- per-format record handlers -----------------------------------------

def _segment(source, seg):
    """iOS or Android semantic segment -> records."""
    start, end = when(seg.get("startTime")), when(seg.get("endTime"))
    if start is None:
        return
    if isinstance(seg.get("visit"), dict):
        v = seg["visit"]
        top = v.get("topCandidate") or {}
        lat, lon = latlng(top.get("placeLocation"))
        yield Visit(source=source, start=start, end=end, lat=lat, lon=lon,
                    place_id=str(top.get("placeID") or top.get("placeId") or "")[:200],
                    semantic_type=str(top.get("semanticType") or "")[:50],
                    probability=num(top.get("probability", v.get("probability"))), raw=plain(seg))
    if isinstance(seg.get("activity"), dict):
        a = seg["activity"]
        top = a.get("topCandidate") or {}
        slat, slon = latlng(a.get("start"))
        elat, elon = latlng(a.get("end"))
        yield TripSegment(source=source, start=start, end=end, start_lat=slat, start_lon=slon, end_lat=elat,
                          end_lon=elon, distance_m=num(a.get("distanceMeters")), mode=mode_name(top.get("type")),
                          probability=num(top.get("probability")), raw=plain(seg))
    for p in seg.get("timelinePath") or []:
        if not isinstance(p, dict):
            continue
        lat, lon = latlng(p.get("point"))
        at = when(p.get("time"))
        if at is None and num(p.get("durationMinutesOffsetFromStartTime")) is not None:
            at = start + timedelta(minutes=num(p["durationMinutesOffsetFromStartTime"]))
        pt = _point(DEVICE[source], at, lat, lon)
        if pt:
            yield pt


def _raw_signal(sig):
    pos = sig.get("position") if isinstance(sig, dict) else None
    if not isinstance(pos, dict):
        return
    lat, lon = latlng(pos.get("LatLng") or pos.get("latLng"))
    pt = _point("google-raw", when(pos.get("timestamp")), lat, lon, altitude=num(pos.get("altitudeMeters")),
                speed=num(pos.get("speedMetersPerSecond")), h_accuracy=num(pos.get("accuracyMeters")),
                props={"source": pos.get("source")} if pos.get("source") else {})
    if pt:
        yield pt


def _record(loc):
    lat, lon = latlng(loc)
    at = when(loc.get("timestamp") or loc.get("timestampMs"))
    acts = loc.get("activity") or []
    top = None
    if acts and isinstance(acts[0], dict):
        best = max(acts[0].get("activity") or [{}], key=lambda a: num(a.get("confidence")) or 0)
        top = mode_name(best.get("type")) if best.get("type") else None
    pt = _point("google-records", at, lat, lon, altitude=num(loc.get("altitude")),
                speed=num(loc.get("velocity")), course=num(loc.get("heading")),
                h_accuracy=num(loc.get("accuracy")), v_accuracy=num(loc.get("verticalAccuracy")),
                motion=[top] if top else [], props={"source": loc["source"]} if loc.get("source") else {})
    if pt:
        yield pt


def _semantic(obj):
    if isinstance(obj.get("placeVisit"), dict):
        v = obj["placeVisit"]
        loc, dur = v.get("location") or {}, v.get("duration") or {}
        start = when(dur.get("startTimestamp") or dur.get("startTimestampMs"))
        if start is None:
            return
        lat, lon = latlng(loc)
        yield Visit(source="takeout-semantic", start=start,
                    end=when(dur.get("endTimestamp") or dur.get("endTimestampMs")), lat=lat, lon=lon,
                    place_id=str(loc.get("placeId") or "")[:200], semantic_type=str(loc.get("semanticType") or "")[:50],
                    name=str(loc.get("name") or "")[:300], address=str(loc.get("address") or "")[:500],
                    probability=num(v.get("visitConfidence")), raw=plain(obj))
    if isinstance(obj.get("activitySegment"), dict):
        a = obj["activitySegment"]
        dur = a.get("duration") or {}
        start = when(dur.get("startTimestamp") or dur.get("startTimestampMs"))
        if start is None:
            return
        slat, slon = latlng(a.get("startLocation") or {})
        elat, elon = latlng(a.get("endLocation") or {})
        yield TripSegment(source="takeout-semantic", start=start,
                          end=when(dur.get("endTimestamp") or dur.get("endTimestampMs")),
                          start_lat=slat, start_lon=slon, end_lat=elat, end_lon=elon,
                          distance_m=num(a.get("distance")), mode=mode_name(a.get("activityType")),
                          probability=num(a.get("confidence")) if isinstance(a.get("confidence"), (int, float))
                          else None, raw=plain(obj))
        for p in (a.get("simplifiedRawPath") or {}).get("points") or []:
            lat, lon = latlng(p)
            pt = _point("google-timeline", when(p.get("timestamp") or p.get("timestampMs")), lat, lon)
            if pt:
                yield pt


# ---- format detection and streaming -------------------------------------

def detect(head: bytes) -> str | None:
    text = head.lstrip()[:65536].decode("utf-8", "ignore")
    if text.startswith("["):
        return "timeline-ios"
    for key, fmt in (('"semanticSegments"', "timeline-android"), ('"timelineObjects"', "takeout-semantic"),
                     ('"locations"', "takeout-records")):
        if key in text:
            return fmt
    return None


def records(fh, fmt):
    """Yield model instances from an open binary file of format `fmt`."""
    if fmt == "timeline-ios":
        for seg in ijson.items(fh, "item", use_float=True):
            yield from _segment(fmt, seg)
    elif fmt == "timeline-android":
        for prefix, seg in _multi(fh, ("semanticSegments.item", "rawSignals.item")):
            yield from (_segment(fmt, seg) if prefix == "semanticSegments.item" else _raw_signal(seg))
    elif fmt == "takeout-records":
        for loc in ijson.items(fh, "locations.item", use_float=True):
            yield from _record(loc)
    elif fmt == "takeout-semantic":
        for obj in ijson.items(fh, "timelineObjects.item", use_float=True):
            yield from _semantic(obj)


def _multi(fh, prefixes):
    """Stream objects under several top-level array prefixes in one pass."""
    builders = {}
    for prefix, event, value in ijson.parse(fh, use_float=True):
        for p in prefixes:
            if prefix == p and event in ("start_map", "start_array"):
                builders[p] = ijson.ObjectBuilder()
            if p in builders:
                if prefix.startswith(p):
                    builders[p].event(event, value)
                if prefix == p and event in ("end_map", "end_array"):
                    yield p, builders.pop(p).value


def open_sources(path: Path):
    """Yield (name, binary file) for a .json, .json.gz, directory, or Takeout .zip."""
    if path.is_dir():
        for f in sorted(path.rglob("*.json*")):
            if f.suffix in (".json", ".gz"):
                yield from open_sources(f)
    elif path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                n = info.filename
                if n.endswith(".json") and ("Location History" in n or "Timeline" in n or "location" in n.lower()):
                    with z.open(info) as fh:
                        yield n, fh
    elif path.suffix == ".gz":
        with gzip.open(path, "rb") as fh:
            yield path.name, fh
    else:
        with path.open("rb") as fh:
            yield path.name, fh


# ---- writing --------------------------------------------------------------

POINT_UPDATE = ["lat", "lon", "altitude", "speed", "course", "h_accuracy", "v_accuracy", "motion", "props"]
VISIT_UPDATE = ["end", "lat", "lon", "place_id", "semantic_type", "name", "address", "probability", "raw"]
TRIP_UPDATE = ["end", "start_lat", "start_lon", "end_lat", "end_lon", "distance_m", "mode", "probability", "raw"]


class Writer:
    def __init__(self):
        self.buf = {LocationPoint: {}, Visit: {}, TripSegment: {}}
        self.counts = {"points": 0, "visits": 0, "trips": 0}

    def add(self, obj):
        key = (obj.device_id, obj.at) if isinstance(obj, LocationPoint) else (obj.source, obj.start)
        self.buf[type(obj)][key] = obj
        if sum(len(b) for b in self.buf.values()) >= FLUSH:
            self.flush()

    @transaction.atomic
    def flush(self):
        for model, unique, update, name in (
            (LocationPoint, ["device_id", "at"], POINT_UPDATE, "points"),
            (Visit, ["source", "start"], VISIT_UPDATE, "visits"),
            (TripSegment, ["source", "start"], TRIP_UPDATE, "trips"),
        ):
            rows = list(self.buf[model].values())
            if rows:
                model.objects.bulk_create(rows, update_conflicts=True, unique_fields=unique, update_fields=update)
                self.counts[name] += len(rows)
            self.buf[model] = {}


def import_path(path: Path, log=print) -> dict:
    w = Writer()
    for name, fh in open_sources(path):
        head = fh.read(65536)
        fmt = detect(head)
        if fmt is None:
            log(f"{name}: not a recognised location history file, skipped")
            continue
        before = dict(w.counts)
        for obj in records(io.BufferedReader(_Chain(head, fh)), fmt):
            w.add(obj)
        w.flush()
        log(f"{name} ({fmt}): " + ", ".join(f"{w.counts[k] - before[k]} {k}" for k in w.counts))
    return w.counts


class _Chain(io.RawIOBase):
    """The bytes already peeked, then the rest of the stream."""

    def __init__(self, head, rest):
        self.head, self.rest = head, rest

    def readable(self):
        return True

    def readinto(self, b):
        if self.head:
            n = min(len(b), len(self.head))
            b[:n], self.head = self.head[:n], self.head[n:]
            return n
        data = self.rest.read(len(b))
        b[: len(data)] = data
        return len(data)
