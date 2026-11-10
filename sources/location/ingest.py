"""Parse an Overland payload and upsert it. Shared by the HTTP endpoint and
`location_import`.

Overland POSTs {"locations": [GeoJSON Feature, ...], "current": ..., "trip": ...};
each Feature is a Point with coordinates [lon, lat] and properties such as
timestamp, altitude, speed, horizontal_accuracy, motion, battery_level,
device_id. iOS reports -1 for unknown speed, course and accuracy.
"""

import gzip
import hashlib
import json
import logging
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import LoaderRun
from sources.health.ingest import num, parse_date

from .models import LocationBatch, LocationPoint

log = logging.getLogger(__name__)
CHUNK = 500
KNOWN = {"timestamp", "altitude", "speed", "course", "horizontal_accuracy", "vertical_accuracy", "motion",
         "battery_level", "battery_state", "device_id", "wifi"}
UPDATE = ["lat", "lon", "altitude", "speed", "course", "h_accuracy", "v_accuracy", "motion", "battery_level",
          "battery_state", "wifi", "props"]


def save_raw(body: bytes, sha: str, now) -> str:
    """Gzip the payload to RAW_DIR/location/YYYY/MM, falling back to
    VAR_DIR/location-raw when the raw volume isn't mounted."""
    prior = LocationBatch.objects.filter(sha256=sha).exclude(raw_path="").first()
    if prior and Path(prior.raw_path).exists():
        return prior.raw_path
    utc = now.astimezone(dt_timezone.utc)
    rel = Path(f"{utc:%Y}") / f"{utc:%m}" / f"{utc:%Y%m%dT%H%M%S}Z-{sha[:16]}.json.gz"
    roots = []
    if Path(settings.RAW_DIR).is_dir():
        roots.append(Path(settings.RAW_DIR) / "location")
    roots.append(Path(settings.VAR_DIR) / "location-raw")
    for root in roots:
        dest = root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(gzip.compress(body))
            tmp.replace(dest)
            return str(dest)
        except OSError as e:
            log.warning("location raw write to %s failed: %s", root, e)
    return ""


def _known(v):
    """None for missing or iOS's negative 'unknown'."""
    q = num(v)
    return None if q is None or q < 0 else q


def point_row(feature):
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry") or {}
    props = feature.get("properties") or {}
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if not isinstance(props, dict) or not isinstance(coords, list) or len(coords) < 2:
        return None
    lon, lat = num(coords[0]), num(coords[1])
    at = parse_date(props.get("timestamp"))
    if at is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    motion = props.get("motion")
    alt = num(props.get("altitude"))
    if alt is None and len(coords) > 2:
        alt = num(coords[2])
    return LocationPoint(
        device_id=str(props.get("device_id") or "")[:100],
        at=at,
        lat=lat,
        lon=lon,
        altitude=alt,
        speed=_known(props.get("speed")),
        course=_known(props.get("course")),
        h_accuracy=_known(props.get("horizontal_accuracy")),
        v_accuracy=_known(props.get("vertical_accuracy")),
        motion=[str(m) for m in motion] if isinstance(motion, list) else [],
        battery_level=_known(props.get("battery_level")),
        battery_state=str(props.get("battery_state") or "")[:20],
        wifi=str(props.get("wifi") or "")[:100],
        props={k: v for k, v in props.items() if k not in KNOWN},
    )


@transaction.atomic
def ingest_payload(payload, *, sha256="", size=0, raw_path="") -> dict:
    features = payload.get("locations") if isinstance(payload, dict) else None
    rows, skipped = {}, 0
    for f in features if isinstance(features, list) else []:
        row = point_row(f)
        if row is None:
            skipped += 1
            continue
        rows[(row.device_id, row.at)] = row
    pts = list(rows.values())
    for i in range(0, len(pts), CHUNK):
        LocationPoint.objects.bulk_create(pts[i : i + CHUNK], update_conflicts=True,
                                          unique_fields=["device_id", "at"], update_fields=UPDATE)
    LocationBatch.objects.create(received_at=timezone.now(), sha256=sha256, bytes=size, points=len(pts),
                                 skipped=skipped, raw_path=raw_path)
    return {"points": len(pts), "skipped": skipped}


def ingest_body(body: bytes, *, store_raw=True, raw_path="") -> dict:
    """Decode (JSON, optionally gzip), store raw, upsert, log a LoaderRun.
    Raises ValueError on undecodable input."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    payload = json.loads(body)
    now = timezone.now()
    sha = hashlib.sha256(body).hexdigest()
    run = LoaderRun.objects.create(loader="location", started=now)
    try:
        if store_raw:
            raw_path = save_raw(body, sha, now)
        result = ingest_payload(payload, sha256=sha, size=len(body), raw_path=raw_path)
    except Exception as e:
        run.ok, run.finished, run.message = False, timezone.now(), f"{type(e).__name__}: {e}"[:500]
        run.save()
        raise
    run.ok, run.finished, run.rows = True, timezone.now(), result["points"]
    run.message = f"{result['points']} points, {result['skipped']} skipped"
    run.save()
    return result
