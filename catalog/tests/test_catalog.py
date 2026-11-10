from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone

from catalog import subscriptions as subs
from catalog.views import latest_loader_runs
from core.models import LoaderRun
from sources.agentsview.models import Session
from sources.limits.models import LimitSnapshot, Subscription

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=dt_timezone.utc)


def snap(vendor, window, at, pct=None, **kw):
    return LimitSnapshot.objects.create(vendor=vendor, window=window, taken_at=at, utilization_pct=pct, **kw)


@pytest.fixture
def plans(db):
    # The seed migration may already have created these; make it exact.
    Subscription.objects.all().delete()
    Subscription.objects.create(vendor="claude", plan="Max 5x", monthly_usd=Decimal(100))
    Subscription.objects.create(vendor="codex", plan="ChatGPT Plus", monthly_usd=Decimal(20))
    Subscription.objects.create(vendor="gemini", plan="AI Pro", monthly_usd=Decimal(50), shared=True)
    Subscription.objects.create(vendor="openrouter", plan="pay-as-you-go", monthly_usd=None)


@pytest.fixture
def snaps(plans):
    now = timezone.now()
    for h in range(48, 0, -1):
        snap("claude", "session_5h", now - timedelta(hours=h), pct=h % 50, resets_at=now + timedelta(hours=2))
        snap("claude", "weekly_all", now - timedelta(hours=h), pct=75)
    snap("claude", "weekly_fable", now - timedelta(minutes=5), pct=95)
    snap("claude", "iguana_necktie", now - timedelta(minutes=5), pct=0, used_usd=0, limit_usd=250)
    snap("codex", "weekly_all", now - timedelta(days=20), pct=10)
    snap("codex", "weekly_all", now - timedelta(hours=3), pct=40)
    snap("openrouter", "credits", now - timedelta(hours=1), pct=96.9, used_usd=4.84, limit_usd=5)
    snap("openrouter", "spend_month", now - timedelta(hours=2), used_usd=0.004)
    snap("openrouter", "spend_month", now - timedelta(hours=1), used_usd=0.0086)


@pytest.mark.django_db
def test_home_lists_entries_and_loaders(client):
    t = timezone.now()
    LoaderRun.objects.create(loader="agentsview", started=t - timedelta(hours=2), ok=True, message="old")
    LoaderRun.objects.create(loader="agentsview", started=t, ok=False, message="boom happened\ntraceback...")
    LoaderRun.objects.create(loader="herdr", started=t, ok=True, rows=3, message="3 rows")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Am I making good use of my AI subscriptions?" in body
    assert "What have I been working on, and how?" in body
    assert ">boom happened</td>" in body  # first line only; full message in the title
    assert "FAILED" in body and "herdr" in body
    assert [r.loader for r in latest_loader_runs()] == ["agentsview", "herdr"]


@pytest.mark.django_db
def test_subscriptions_page(client, snaps):
    Session.objects.create(session_id="a", agent="claude", project="p", started_at=timezone.now(),
                           estimated_cost_usd=Decimal("300"))
    Session.objects.create(session_id="b", agent="claude", project="p", started_at=timezone.now(),
                           relationship_type="subagent", estimated_cost_usd=Decimal("112"))
    resp = client.get("/subscriptions/")
    assert resp.status_code == 200
    body = resp.content.decode()
    order = [body.index(f'id="{v}"') for v in ("claude", "codex", "openrouter", "gemini")]
    assert order == sorted(order)
    for text in ("5-hour session", "Weekly, all models", "Weekly, fable", "Extra usage ($)",
                 "No programmatic limits; chat only.", "pay-as-you-go", "$412", "4.1x", "1 session"):
        assert text in body, text
    assert 'class="critical"' in body  # weekly_fable at 95%
    assert 'class="warn"' in body  # weekly_all at 75%
    assert "resets in 1h" in body


@pytest.mark.django_db
def test_data_endpoint_traces_and_range(client, snaps):
    week = client.get("/subscriptions/data/").json()  # default 7d
    assert set(week) == {"claude", "codex", "openrouter"}
    names = [t["name"] for t in week["claude"]["traces"]]
    assert names[:2] == ["5-hour session", "Weekly, all models"]
    codex = week["codex"]["traces"][0]
    assert len(codex["x"]) == 1  # the 20-day-old sample is outside 7d
    kinds = {t["window"]: t["kind"] for t in week["openrouter"]["traces"]}
    assert kinds == {"credits": "pct", "spend_month": "usd"}

    all_ = client.get("/subscriptions/data/?range=all").json()
    codex_all = all_["codex"]["traces"][0]
    assert codex_all["y"] == [10, None, 40]  # long gap becomes a null

    day = client.get("/subscriptions/data/?range=24h").json()
    s5 = next(t for t in day["claude"]["traces"] if t["window"] == "session_5h")
    assert 23 <= len(s5["x"]) <= 24
    assert client.get("/subscriptions/data/?range=bogus").json() == week


def test_resets_in():
    assert subs.resets_in(None, NOW) is None
    assert subs.resets_in(NOW + timedelta(hours=3, minutes=52, seconds=30), NOW) == "resets in 3h 52m"
    assert subs.resets_in(NOW + timedelta(days=6, hours=15, minutes=5), NOW) == "resets in 6d 15h"
    assert subs.resets_in(NOW + timedelta(minutes=12), NOW) == "resets in 12m"
    assert subs.resets_in(NOW - timedelta(minutes=1), NOW) == "reset due"


def test_window_labels():
    assert subs.window_label("claude", "iguana_necktie", 250) == "Extra usage ($)"
    assert subs.window_label("claude", "mystery_bucket", 100) == "Extra usage ($)"
    assert subs.window_label("claude", "mystery_bucket", None) == "mystery_bucket"
    assert subs.window_label("claude", "weekly_opus") == "Weekly, opus"
    assert subs.window_label("codex", "weekly_all") == "Weekly, all models"


def test_bar_level():
    assert [subs.bar_level(p) for p in (None, 10, 70, 71, 90, 91)] == ["", "ok", "ok", "warn", "warn", "critical"]


def test_local_share(db):
    from sources.agentsview.models import ModelUsage

    now = timezone.now()
    s1 = Session.objects.create(session_id="l1", agent="pi", project="x", started_at=now - timedelta(days=1))
    s2 = Session.objects.create(session_id="h1", agent="claude", project="x", started_at=now - timedelta(days=1))
    ModelUsage.objects.create(session=s1, model="qwen/qwen3.6-35b-a3b", output_tokens=300)
    ModelUsage.objects.create(session=s2, model="claude-opus-5-5", output_tokens=700)
    ModelUsage.objects.create(session=s2, model="gpt-5.6-sol", output_tokens=0)
    assert subs.is_local("qwen3.8-flash-next") and subs.is_local("openai/gpt-oss-20b")
    assert not subs.is_local("claude-haiku-4-5") and not subs.is_local("gpt-6-astra") and not subs.is_local("")
    d = subs.local_share("7d", now)
    assert d["bucket"] == "day" and sum(d["local"]) == 300 and sum(d["hosted"]) == 700
    assert d["share"] == 0.3 and d["models"] == [("qwen/qwen3.6-35b-a3b", 300)]
    assert subs.local_share("all", now)["bucket"] == "day"  # data spans < 31 days


def test_local_share_empty(db, client):
    assert subs.local_share("7d")["share"] is None
    assert client.get("/subscriptions/local/?range=30d").json()["x"] == []
