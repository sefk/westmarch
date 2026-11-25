"""Address DFO invoices to both Ramya Mahalingam and Megan Ahearn."""

from django.db import migrations

OLD = "Ode, PB LLC\nAttn: Megan Ahearn"
NEW = "Ode, PB LLC\nAttn: Ramya Mahalingam\nAttn: Megan Ahearn"


def forward(apps, schema_editor):
    BillingProject = apps.get_model("core", "BillingProject")
    BillingProject.objects.filter(slug="dfo", bill_to=OLD).update(bill_to=NEW)


def backward(apps, schema_editor):
    BillingProject = apps.get_model("core", "BillingProject")
    BillingProject.objects.filter(slug="dfo", bill_to=NEW).update(bill_to=OLD)


class Migration(migrations.Migration):
    dependencies = [("core", "0006_billingnote")]
    operations = [migrations.RunPython(forward, backward)]
