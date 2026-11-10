from django.http import JsonResponse
from django.urls import include, path

from sources.health import views as health_views
from sources.location import views as location_views


def health(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("health/", health),
    path("ingest/health/", include("sources.health.urls")),
    # Without the slash too: APPEND_SLASH would answer a POST with a 301,
    # and clients drop the body when they follow it.
    path("ingest/health", health_views.ingest),
    path("ingest/location/", include("sources.location.urls")),
    path("ingest/location", location_views.ingest),
    path("", include("catalog.urls")),
]
