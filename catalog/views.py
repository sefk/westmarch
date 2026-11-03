"""Catalog views: the opinionated analyses, each stating the question it
answers. Pages render HTML; chart data comes from JSON endpoints the page
fetches and draws with plotly."""

from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import render

from catalog import subscriptions as subs
from core.models import LoaderRun

CATALOG = [
    {
        "title": "Subscriptions",
        "url_name": "catalog:subscriptions",
        "question": "Am I making good use of my AI subscriptions?",
    },
]
WAITING = [
    {
        "title": "Projects",
        "question": "What have I been working on, and how?",
        "why": "Waiting on the derive step that maps sessions, commits and tabs to projects.",
    },
    {
        "title": "Data health",
        "question": "Is every loader running, and where are the gaps?",
        "why": "Placeholder; the loader table below is the start of it.",
    },
]


def latest_loader_runs():
    latest = LoaderRun.objects.values("loader").annotate(at=Max("started"))
    q = Q()
    for row in latest:
        q |= Q(loader=row["loader"], started=row["at"])
    if not q:
        return []
    runs = []
    for run in LoaderRun.objects.filter(q).order_by("loader"):
        first = (run.message or "").strip().splitlines()
        run.first_line = first[0] if first else ""
        runs.append(run)
    return runs


def home(request):
    return render(
        request,
        "catalog/index.html",
        {"section": "home", "entries": CATALOG, "waiting": WAITING, "runs": latest_loader_runs()},
    )


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
