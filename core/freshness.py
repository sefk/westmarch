"""Staleness for loaders that nothing runs on a schedule.

Health data is pushed by the phone, so its LoaderRun rows are all "ok" even
when the phone has been silent for days. For those loaders, "ok" means the
latest data is recent: `STALE_AFTER` says how recent, `check` says whether it
is, and the loader table, the home status line, `manage.py load` and the
health pages' banner all read it from here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

STALE_AFTER = {"health": timedelta(days=1)}


@dataclass(frozen=True)
class Freshness:
    loader: str
    last_at: datetime | None
    age: timedelta | None
    stale: bool
    text: str  # "no data for 2d 5h (last: Nov 10, 15:59)"


def last_data_at(name):
    """When the loader's source last delivered anything (not when something last ran)."""
    if name == "health":
        from sources.health.models import IngestBatch

        return IngestBatch.objects.order_by("-received_at").values_list("received_at", flat=True).first()
    return None


def duration(delta):
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h"


def check(name, now=None) -> Freshness | None:
    """None when the loader has no staleness rule."""
    limit = STALE_AFTER.get(name)
    if limit is None:
        return None
    now = now or timezone.now()
    last = last_data_at(name)
    if last is None:
        return Freshness(name, None, None, True, "no data has ever arrived")
    age = now - last
    stamp = f"{timezone.localtime(last):%b %-d, %H:%M}"
    stale = age > limit
    return Freshness(name, last, age, stale, f"no data for {duration(age)} (last: {stamp})" if stale
                     else f"last data {duration(age)} ago")


def stale(name, now=None) -> Freshness | None:
    f = check(name, now)
    return f if f and f.stale else None
