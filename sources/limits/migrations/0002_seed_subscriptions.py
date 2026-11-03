"""Seed the plans in scope (PRD Phase 1). Edit prices here or in admin."""

from decimal import Decimal

from django.db import migrations

PLANS = [
    ("claude", "Max 5x", Decimal("100"), False, "primary coding driver"),
    ("codex", "ChatGPT Plus", Decimal("20"), False, "reviews; candidate for more primary work"),
    ("gemini", "AI Pro", Decimal("50"), True, "chat only, shared with family"),
    ("openrouter", "pay-as-you-go", None, False, "experimentation; wrgpt-buddy analyses"),
]


def seed(apps, schema_editor):
    Subscription = apps.get_model("src_limits", "Subscription")
    for vendor, plan, usd, shared, notes in PLANS:
        Subscription.objects.update_or_create(
            vendor=vendor, defaults={"plan": plan, "monthly_usd": usd, "shared": shared, "notes": notes}
        )


def unseed(apps, schema_editor):
    apps.get_model("src_limits", "Subscription").objects.filter(vendor__in=[p[0] for p in PLANS]).delete()


class Migration(migrations.Migration):
    dependencies = [("src_limits", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
