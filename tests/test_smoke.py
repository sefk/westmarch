import pytest
from django.core.management import call_command

from core.loaders import registry


@pytest.mark.django_db
def test_registry_resolves_every_configured_loader():
    names = list(registry())
    assert names == sorted(set(names), key=names.index)  # no duplicates
    assert "agentsview" in names


@pytest.mark.django_db
def test_load_lists_without_running(capsys):
    call_command("load")
    assert "agentsview" in capsys.readouterr().out
