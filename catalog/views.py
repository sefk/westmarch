"""Catalog views: the opinionated analyses, each stating the question it
answers. Pages render HTML; chart data comes from JSON endpoints the page
fetches and draws with plotly."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render

from catalog import health as hl
from catalog import home as home_tabs
from catalog import projects as pj
from catalog import subscriptions as subs
from core.models import Project
from sources.health.models import Workout

latest_loader_runs = home_tabs.latest_runs


def home(request):
    ctx = home_tabs.tab_context(request.GET.get("tab", home_tabs.DEFAULT_TAB))
    return render(request, "catalog/index.html", {"section": "home", **ctx})


def _range(request):
    r = request.GET.get("range", subs.DEFAULT_RANGE)
    return r if r in subs.RANGES else subs.DEFAULT_RANGE


def subscriptions(request):
    return render(
        request,
        "catalog/subscriptions.html",
        {
            "section": "subscriptions",
            "cards": subs.cards(),
            "range": _range(request),
            "ranges": list(subs.RANGES),
        },
    )


def subscriptions_data(request):
    return JsonResponse(subs.traces(_range(request)))


def subscriptions_local(request):
    return JsonResponse(subs.local_share(_range(request)))


def _health_range(request):
    r = request.GET.get("range", hl.DEFAULT_RANGE)
    return r if r in hl.RANGES else hl.DEFAULT_RANGE


def health(request):
    r = _health_range(request)
    return render(
        request,
        "catalog/health.html",
        {"section": "health", "range": r, "ranges": list(hl.RANGES), "summary": hl.summary(),
         "workouts": hl.workouts(r)},
    )


def health_data(request):
    return JsonResponse(hl.series(_health_range(request)))


def workout(request, pk):
    w = get_object_or_404(Workout.objects.defer("raw"), pk=pk)
    return render(request, "catalog/workout.html", {"section": "health", "w": w})


def workout_data(request, pk):
    return JsonResponse(hl.workout_detail(get_object_or_404(Workout, pk=pk)))


def health_coverage(request):
    return JsonResponse(hl.coverage())


def _projects_range(request):
    r = request.GET.get("range", pj.DEFAULT_RANGE)
    return r if r in pj.RANGES else pj.DEFAULT_RANGE


def projects(request):
    r = _projects_range(request)
    return render(request, "catalog/projects.html",
                  {"section": "projects", "range": r, "ranges": list(pj.RANGES), "t": pj.table(r)})


def projects_data(request):
    return JsonResponse(pj.series(_projects_range(request)))


def project(request, pk):
    p = get_object_or_404(Project, pk=pk)
    r = _projects_range(request)
    d = pj.project_detail(p, r, request_host=request.get_host())
    return render(request, "catalog/project.html",
                  {"section": "projects", "p": p, "range": r, "ranges": list(pj.RANGES), "d": d,
                   "chart": {"x": d["x"], "cost": d["cost"], "commits": d["commits"]}})
