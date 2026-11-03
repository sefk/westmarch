import pytest

from core.loaders import Disabled, Loader, run_loader
from core.models import LoaderRun, LoaderState

pytestmark = pytest.mark.django_db


class Counting(Loader):
    name = "counting"

    def load(self, full=False):
        n = int(self.state_get("n", "0")) + 1
        self.state_set("n", str(n))
        return n


class Broken(Loader):
    name = "broken"

    def load(self, full=False):
        raise RuntimeError("boom")


class Off(Loader):
    name = "off"

    def load(self, full=False):
        raise Disabled("no token")


def test_run_records_rows_and_state():
    run = run_loader(Counting())
    assert run.ok and run.rows == 1
    run = run_loader(Counting())
    assert run.rows == 2
    assert LoaderState.get("counting", "n") == "2"


def test_failure_is_a_row_not_an_exception():
    run = run_loader(Broken())
    assert run.ok is False and "boom" in run.message
    assert LoaderRun.objects.count() == 1


def test_disabled_is_ok_with_message():
    run = run_loader(Off())
    assert run.ok is True and run.message.startswith("disabled")
