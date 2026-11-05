from django.http import JsonResponse
from django.urls import include, path


def health(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("health/", health),
    path("ingest/health/", include("sources.health.urls")),
    path("", include("catalog.urls")),
]
