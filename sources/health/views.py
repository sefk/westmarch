import hmac

from django.conf import settings
from django.db.models import Count, Max
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .ingest import ingest_body
from .models import IngestBatch, Metric, SleepNight, Workout

MAX_BODY = 100 * 1024 * 1024


@csrf_exempt
def ingest(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405, headers={"Allow": "POST"})
    expected = settings.HEALTH_INGEST_TOKEN
    if not expected:
        return JsonResponse({"ok": False, "error": "ingest disabled"}, status=503)
    given = request.headers.get("X-Ingest-Token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return JsonResponse({"ok": False, "error": "payload too large"}, status=413)
    # Read the stream directly: request.body enforces DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB).
    body = request.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        return JsonResponse({"ok": False, "error": "payload too large"}, status=413)
    try:
        result = ingest_body(body)
    except ValueError:
        return JsonResponse({"ok": False, "error": "bad json"}, status=400)
    return JsonResponse({"ok": True, **result})


def status(request):
    latest = {r["name"]: r for r in Metric.objects.values("name").annotate(n=Count("id"), latest=Max("at"))}
    out = []
    for name, r in sorted(latest.items()):
        last = Metric.objects.filter(name=name, at=r["latest"]).first()
        out.append({"name": name, "points": r["n"], "latest_at": r["latest"].isoformat(),
                    "latest_qty": last.qty if last else None, "units": last.units if last else ""})
    batch = IngestBatch.objects.first()
    return JsonResponse({
        "metric_points": Metric.objects.count(),
        "sleep_nights": SleepNight.objects.count(),
        "workouts": Workout.objects.count(),
        "batches": IngestBatch.objects.count(),
        "last_batch_at": batch.received_at.isoformat() if batch else None,
        "metrics": out,
    })
