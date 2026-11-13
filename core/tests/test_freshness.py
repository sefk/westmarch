from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from core import freshness
from core.models import LoaderRun
from sources.health.models import IngestBatch

pytestmark = pytest.mark.django_db


def batch(ago):
    return IngestBatch.objects.create(received_at=timezone.now() - ago, sha256="x")


def test_no_rule_for_scheduled_loaders():
    assert freshness.check("agentsview") is None and freshness.stale("agentsview") is None


def test_health_is_fresh_within_a_day_and_stale_after():
    batch(timedelta(hours=23))
    f = freshness.check("health")
    assert not f.stale and f.text == "last data 23h ago"
    IngestBatch.objects.all().delete()
    batch(timedelta(days=2, hours=5, minutes=10))
    f = freshness.stale("health")
    assert f.stale and f.text.startswith("no data for 2d 5h (last: ") and f.age > timedelta(days=2)


def test_health_stale_when_nothing_ever_arrived():
    assert freshness.stale("health").text == "no data has ever arrived"


def test_a_day_exactly_is_not_yet_stale():
    now = timezone.now()
    IngestBatch.objects.create(received_at=now - timedelta(days=1), sha256="x")
    assert freshness.check("health", now).stale is False
    assert freshness.check("health", now + timedelta(seconds=1)).stale is True


def test_load_listing_flags_stale():
    LoaderRun.objects.create(loader="health", started=timezone.now(), ok=True)
    batch(timedelta(days=3))
    out = StringIO()
    call_command("load", stdout=out)
    health = next(l for l in out.getvalue().splitlines() if l.startswith("health"))
    assert "push" in health and "STALE: no data for 3d" in health
    IngestBatch.objects.all().delete()
    batch(timedelta(hours=1))
    out = StringIO()
    call_command("load", stdout=out)
    assert "STALE" not in out.getvalue()
