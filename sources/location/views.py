import hmac

from django.conf import settings
from django.db.models import Count, Max, Min
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .ingest import ingest_body
from .models import LocationBatch, LocationPoint

MAX_BODY = 50 * 1024 * 1024


def _token(request):
    """Overland sends its access token as a Bearer header (older builds as
    ?access_token=); X-Ingest-Token matches the health endpoint."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Ingest-Token") or request.GET.get("access_token", "")


def _err(msg, status):
    return JsonResponse({"result": "error", "error": msg}, status=status)


@csrf_exempt
def ingest(request):
    if request.method != "POST":
        return JsonResponse({"result": "error", "error": "method not allowed"}, status=405, headers={"Allow": "POST"})
    expected = settings.LOCATION_INGEST_TOKEN
    if not expected:
        return _err("ingest disabled", 503)
    if not hmac.compare_digest(_token(request).encode(), expected.encode()):
        return _err("unauthorized", 401)
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return _err("payload too large", 413)
    body = request.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        return _err("payload too large", 413)
    try:
        result = ingest_body(body)
    except ValueError:
        return _err("bad json", 400)
    # Overland deletes its queued points only on exactly {"result": "ok"}.
    return JsonResponse({"result": "ok", **result})


def status(request):
    agg = LocationPoint.objects.aggregate(n=Count("id"), first=Min("at"), last=Max("at"))
    devices = list(LocationPoint.objects.values("device_id").annotate(n=Count("id"), last=Max("at")).order_by("-last"))
    batch = LocationBatch.objects.first()
    return JsonResponse({
        "points": agg["n"],
        "first_at": agg["first"].isoformat() if agg["first"] else None,
        "last_at": agg["last"].isoformat() if agg["last"] else None,
        "batches": LocationBatch.objects.count(),
        "last_batch_at": batch.received_at.isoformat() if batch else None,
        "devices": [{"device_id": d["device_id"], "points": d["n"], "last_at": d["last"].isoformat()} for d in devices],
    })
