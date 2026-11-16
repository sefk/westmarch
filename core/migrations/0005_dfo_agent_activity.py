"""Count the hours I type prompts to agents under ~/src/dfo toward the DFO engagement."""

from django.db import migrations


def enable(apps, schema_editor):
    apps.get_model("core", "BillingProject").objects.filter(slug="dfo").update(
        use_agent_activity=True, agent_paths=["~/src/dfo"], agent_gap_minutes=15, agent_tail_minutes=5)


class Migration(migrations.Migration):
    dependencies = [("core", "0004_agent_activity")]
    operations = [migrations.RunPython(enable, migrations.RunPython.noop)]
