"""Seed the DFO engagement with Ode, PB LLC (agreement signed 9 Nov 2026)."""

from django.db import migrations


def seed(apps, schema_editor):
    Project = apps.get_model("core", "Project")
    BillingProject = apps.get_model("core", "BillingProject")
    BillingProject.objects.update_or_create(
        slug="dfo",
        defaults={
            "name": "DFO for The ONE Campaign (Ode, PB LLC)",
            "project": Project.objects.filter(slug__in=["dfo", "ode"]).first(),
            "tags": ["DFO"],
            "attendee_domains": ["ode.partners", "data.one.org"],
            "rate_usd": "200.00",
            "hours_cap": "55",
            "amount_cap_usd": "11000",
            "term_start": "2026-11-09",
            "term_end": "2026-12-18",
            "invoice_schedule": {
                "2026-11": {"cap": 7000, "due": "2026-12-05"},
                "2026-12": {"cap": 4000, "due": "2027-01-05"},
            },
            "bill_from": "John Josef Kloninger, Independent Contractor",
            "bill_to": "Ode, PB LLC\nAttn: Megan Ahearn",
        },
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_billingproject")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
