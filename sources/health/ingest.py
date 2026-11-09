"""Parse a Health Auto Export payload and upsert it. Shared by the HTTP
endpoint and the `health_import` command."""

import gzip
import hashlib
import json
import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import LoaderRun

from .models import IngestBatch, Metric, SleepNight, Workout, WorkoutRoutePoint

log = logging.getLogger(__name__)
CHUNK = 500
KJ_PER_KCAL = 4.184


def parse_date(value):
    """'2026-11-01 07:00:00 -0800' or ISO 8601 -> aware datetime; None if bad."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    dt = None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return num(v.get("qty"))
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def units_of(v, default=""):
    return v.get("units", default) if isinstance(v, dict) else default


def save_raw(body: bytes, sha: str, now) -> str:
    """Write the gzipped payload to RAW_DIR/health/YYYY/MM, else to
    VAR_DIR/health-raw if the raw volume is missing. Returns the path."""
    prior = IngestBatch.objects.filter(sha256=sha).exclude(raw_path="").first()
    if prior and Path(prior.raw_path).exists():
        return prior.raw_path
    utc = now.astimezone(dt_timezone.utc)
    rel = Path(f"{utc:%Y}") / f"{utc:%m}" / f"{utc:%Y%m%dT%H%M%S}Z-{sha[:16]}.json.gz"
    roots = []
    if Path(settings.RAW_DIR).is_dir():  # unmounted volume: don't mkdir /Volumes/...
        roots.append(Path(settings.RAW_DIR) / "health")
    roots.append(Path(settings.VAR_DIR) / "health-raw")
    for root in roots:
        dest = root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(gzip.compress(body))
            tmp.replace(dest)
            return str(dest)
        except OSError as e:
            log.warning("health raw write to %s failed: %s", root, e)
    return ""


def _metric_row(name, units, pt):
    at = parse_date(pt.get("date"))
    if at is None:
        return None
    source = pt.get("source")
    source = source if isinstance(source, str) else (json.dumps(source) if source else "")
    extra = {k: v for k, v in pt.items() if k not in ("date", "qty", "source")}
    qty = num(pt.get("qty"))
    if qty is None and "Avg" in pt:
        qty = num(pt.get("Avg"))
    if qty is None and not extra:
        return None
    return Metric(name=name, units=units, at=at, qty=qty, source=source, fields=extra)


def _sleep_row(pt):
    night = parse_date(pt.get("date"))
    if night is None:
        return None
    return SleepNight(
        night=night.date(),
        source=pt.get("source") if isinstance(pt.get("source"), str) else "",
        total_sleep_hr=num(pt.get("totalSleep")),
        in_bed_hr=num(pt.get("inBed")),
        asleep_hr=num(pt.get("asleep")),
        core_hr=num(pt.get("core")),
        deep_hr=num(pt.get("deep")),
        rem_hr=num(pt.get("rem")),
        awake_hr=num(pt.get("awake")),
        sleep_start=parse_date(pt.get("sleepStart")),
        sleep_end=parse_date(pt.get("sleepEnd")),
        raw=pt,
    )


def _energy_kcal(v):
    q = num(v)
    if q is not None and str(units_of(v)).lower() in ("kj", "kilojoules"):
        q /= KJ_PER_KCAL
    return q


def _route_points(route):
    """Health Auto Export route list -> unsaved WorkoutRoutePoints (no
    workout yet), deduped by time. v2 uses latitude/longitude/timestamp,
    v1 lat/lon. Returns (points, skipped)."""
    points, skipped = {}, 0
    for pt in route:
        if not isinstance(pt, dict):
            skipped += 1
            continue
        at = parse_date(pt.get("timestamp") or pt.get("date"))
        lat = num(pt.get("latitude", pt.get("lat")))
        lon = num(pt.get("longitude", pt.get("lon")))
        if at is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            skipped += 1
            continue
        points[at] = WorkoutRoutePoint(
            at=at, lat=lat, lon=lon,
            altitude=num(pt.get("altitude")),
            speed=num(pt.get("speed")),
            course=num(pt.get("course")),
            h_accuracy=num(pt.get("horizontalAccuracy")),
            v_accuracy=num(pt.get("verticalAccuracy")),
        )
    return list(points.values()), skipped


def _workout_row(w):
    start = parse_date(w.get("start"))
    if start is None:
        return None
    ext = w.get("id") or hashlib.sha1(f"{w.get('name')}|{w.get('start')}".encode()).hexdigest()
    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    dur = num(w.get("duration"))
    end = parse_date(w.get("end"))
    if dur is None and end:
        dur = (end - start).total_seconds()
    energy = w.get("activeEnergyBurned", w.get("activeEnergy"))
    if isinstance(energy, list):
        energy = None  # v1 time series; kept in raw
    loc = w.get("location")
    indoor = w.get("isIndoor")
    return Workout(
        external_id=str(ext),
        name=str(w.get("name") or "")[:100],
        start=start,
        end=end,
        duration_seconds=dur,
        active_energy_kcal=_energy_kcal(energy),
        distance=num(w.get("distance")),
        distance_units=str(units_of(w.get("distance")))[:20],
        avg_heart_rate=num(w.get("avgHeartRate", hr.get("avg"))),
        max_heart_rate=num(w.get("maxHeartRate", hr.get("max"))),
        is_indoor=indoor if isinstance(indoor, bool) else None,
        location=loc if isinstance(loc, str) else "",
        raw={k: v for k, v in w.items() if k != "route"},
    )


def _flush(model, rows, unique, update):
    for i in range(0, len(rows), CHUNK):
        model.objects.bulk_create(
            rows[i : i + CHUNK], update_conflicts=True, unique_fields=unique, update_fields=update
        )


@transaction.atomic
def ingest_payload(payload, *, sha256="", size=0, raw_path="") -> dict:
    """Upsert a decoded payload. Returns {"metrics", "workouts", "skipped"}."""
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    metrics, sleeps, workouts, routes, skipped = {}, {}, {}, {}, 0
    for m in data.get("metrics") or []:
        if not isinstance(m, dict) or not m.get("name"):
            skipped += 1
            continue
        name, units = str(m["name"]), str(m.get("units") or "")
        for pt in m.get("data") or []:
            try:
                if not isinstance(pt, dict):
                    raise ValueError
                if name == "sleep_analysis" and ("totalSleep" in pt or "sleepStart" in pt):
                    row = _sleep_row(pt)
                    key = (row.night, row.source) if row else None
                    target = sleeps
                else:
                    row = _metric_row(name, units, pt)
                    key = (row.name, row.at, row.source) if row else None
                    target = metrics
                if row is None:
                    raise ValueError
                target[key] = row
            except Exception:
                skipped += 1
    for w in data.get("workouts") or []:
        try:
            row = _workout_row(w) if isinstance(w, dict) else None
            if row is None:
                raise ValueError
            workouts[row.external_id] = row
        except Exception:
            skipped += 1
            continue
        if isinstance(w.get("route"), list) and w["route"]:
            pts, bad = _route_points(w["route"])
            skipped += bad
            if pts:
                routes[row.external_id] = pts
                row.route_points = len(pts)

    # A re-send without a route must not zero a count already stored.
    have = dict(Workout.objects.filter(external_id__in=workouts).values_list("external_id", "route_points"))
    for ext, row in workouts.items():
        if ext not in routes:
            row.route_points = have.get(ext, 0)

    _flush(Metric, list(metrics.values()), ["name", "at", "source"], ["units", "qty", "fields"])
    _flush(
        SleepNight,
        list(sleeps.values()),
        ["night", "source"],
        ["total_sleep_hr", "in_bed_hr", "asleep_hr", "core_hr", "deep_hr", "rem_hr", "awake_hr",
         "sleep_start", "sleep_end", "raw"],
    )
    _flush(
        Workout,
        list(workouts.values()),
        ["external_id"],
        ["name", "start", "end", "duration_seconds", "active_energy_kcal", "distance", "distance_units",
         "avg_heart_rate", "max_heart_rate", "is_indoor", "location", "route_points", "raw"],
    )
    n_route = 0
    if routes:
        ids = dict(Workout.objects.filter(external_id__in=routes).values_list("external_id", "id"))
        WorkoutRoutePoint.objects.filter(workout_id__in=ids.values()).delete()
        rows = []
        for ext, pts in routes.items():
            for p in pts:
                p.workout_id = ids[ext]
            rows += pts
        for i in range(0, len(rows), CHUNK):
            WorkoutRoutePoint.objects.bulk_create(rows[i : i + CHUNK])
        n_route = len(rows)
    n_metrics = len(metrics) + len(sleeps)
    IngestBatch.objects.create(
        received_at=timezone.now(), sha256=sha256, bytes=size, metric_points=n_metrics,
        workouts=len(workouts), route_points=n_route, skipped=skipped, raw_path=raw_path,
    )
    return {"metrics": n_metrics, "workouts": len(workouts), "route_points": n_route, "skipped": skipped}


def ingest_body(body: bytes, *, store_raw=True, raw_path="") -> dict:
    """Decode bytes (JSON, optionally gzip), store raw, upsert, log a
    LoaderRun. Raises ValueError on undecodable input."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    payload = json.loads(body)  # ValueError (JSONDecodeError) on bad JSON
    now = timezone.now()
    sha = hashlib.sha256(body).hexdigest()
    run = LoaderRun.objects.create(loader="health", started=now)
    try:
        if store_raw:
            raw_path = save_raw(body, sha, now)
        result = ingest_payload(payload, sha256=sha, size=len(body), raw_path=raw_path)
    except Exception as e:
        run.ok, run.finished, run.message = False, timezone.now(), f"{type(e).__name__}: {e}"[:500]
        run.save()
        raise
    run.ok, run.finished = True, timezone.now()
    run.rows = result["metrics"] + result["workouts"] + result["route_points"]
    run.message = (f"{result['metrics']} points, {result['workouts']} workouts, "
                   f"{result['route_points']} route points, {result['skipped']} skipped")
    run.save()
    return result
