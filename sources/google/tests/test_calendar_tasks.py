from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from core.loaders import Disabled
from core.models import LoaderState
from sources.google.client import ApiDisabled
from sources.google.loader import CalendarLoader, TasksLoader
from sources.google.models import GCalCalendar, GCalEvent, GTask, GTaskList

from .fakes import FakeClient

pytestmark = pytest.mark.django_db

TIMED = {"id": "ev1", "status": "confirmed", "summary": "Standup", "location": "Zoom",
         "start": {"dateTime": "2026-02-03T09:00:00-08:00"}, "end": {"dateTime": "2026-02-03T09:15:00-08:00"},
         "recurrence": ["RRULE:FREQ=DAILY"], "organizer": {"email": "boss@example.com"},
         "attendees": [{"email": "me@example.com", "responseStatus": "accepted", "self": True},
                       {"email": "x@example.com", "responseStatus": "needsAction", "displayName": "X"}],
         "created": "2026-01-01T00:00:00.000Z", "updated": "2026-01-02T00:00:00.000Z"}
ALL_DAY = {"id": "ev2", "status": "confirmed", "summary": "Holiday",
           "start": {"date": "2026-07-04"}, "end": {"date": "2026-07-05"},
           "updated": "2026-01-02T00:00:00Z"}


def cal_client():
    c = FakeClient()
    c.calendar_list = [{"id": "me@example.com", "summary": "Me", "timeZone": "America/Los_Angeles",
                        "accessRole": "owner", "primary": True}]
    return c


def run_cal(client, full=False):
    ld = CalendarLoader()
    ld.client = client
    return ld.load(full=full)


def test_calendar_full_then_incremental_then_410():
    client = cal_client()
    tokens = []

    def events(token):
        tokens.append(token)
        if token is None:
            return [TIMED, ALL_DAY], "sync-1"
        if token == "sync-1":
            return [{"id": "ev2", "status": "cancelled", "updated": "2026-03-01T00:00:00Z"}], "sync-2"
        return 410

    client.events["me@example.com"] = events
    assert run_cal(client) == 3  # calendar + 2 events
    assert LoaderState.get("gcal", "sync_token:me@example.com") == "sync-1"

    ev = GCalEvent.objects.get(event_id="ev1")
    assert ev.start == datetime(2026, 2, 3, 17, 0, tzinfo=UTC) and not ev.all_day
    assert ev.start_raw == "2026-02-03T09:00:00-08:00"
    assert ev.recurrence == ["RRULE:FREQ=DAILY"] and ev.organizer_email == "boss@example.com"
    assert ev.attendees[1] == {"email": "x@example.com", "responseStatus": "needsAction", "self": False}
    day = GCalEvent.objects.get(event_id="ev2")
    assert day.all_day and day.start_raw == "2026-07-04" and day.end_raw == "2026-07-05"
    assert day.start == datetime(2026, 7, 4, tzinfo=ZoneInfo("America/Los_Angeles"))

    # Incremental: a cancellation stub keeps the event's details.
    assert run_cal(client) == 2
    day.refresh_from_db()
    assert day.status == "cancelled" and day.summary == "Holiday"
    assert LoaderState.get("gcal", "sync_token:me@example.com") == "sync-2"

    # sync-2 is expired (410): reset and do a full sync.
    run_cal(client)
    assert tokens[-2:] == ["sync-2", None]
    assert LoaderState.get("gcal", "sync_token:me@example.com") == "sync-1"
    assert GCalEvent.objects.count() == 2
    assert GCalCalendar.objects.get().primary


def test_calendar_full_ignores_token():
    client = cal_client()
    client.events["me@example.com"] = lambda token: ([TIMED], "s")
    run_cal(client)
    run_cal(client, full=True)
    assert [t for _, t in client.event_calls] == [None, None]


def test_calendar_api_disabled():
    client = cal_client()

    def boom():
        raise ApiDisabled(403, "accessNotConfigured", "", "https://console.cloud.google.com/apis/x")

    client.calendars = boom
    with pytest.raises(Disabled, match="console.cloud.google.com"):
        run_cal(client)


def test_no_token_disables_all(settings, tmp_path):
    settings.GOOGLE_TOKEN_FILE = tmp_path / "missing.json"
    for cls in (CalendarLoader, TasksLoader):
        with pytest.raises(Disabled, match="no Google token; run: manage.py google_auth"):
            cls().load()


def test_tasks_upsert_and_updated_min():
    client = FakeClient()
    client.lists = [{"id": "L1", "title": "My Tasks", "updated": "2026-01-01T00:00:00Z"}]
    client.task_items["L1"] = [
        {"id": "t1", "title": "Buy milk", "status": "needsAction", "due": "2026-02-01T00:00:00.000Z",
         "updated": "2026-01-05T00:00:00.000Z", "position": "00000000000000000001"},
        {"id": "t2", "title": "Sub", "parent": "t1", "status": "completed", "notes": "n",
         "completed": "2026-01-06T12:00:00.000Z", "updated": "2026-01-06T12:00:00.000Z", "hidden": True},
    ]
    ld = TasksLoader()
    ld.client = client
    assert ld.load() == 3
    t2 = GTask.objects.get(task_id="t2")
    assert t2.parent == "t1" and t2.hidden and t2.completed == datetime(2026, 1, 6, 12, tzinfo=UTC)
    assert GTask.objects.get(task_id="t1").due == datetime(2026, 2, 1, tzinfo=UTC)
    assert client.task_calls[0] == ("L1", None)

    client.task_items["L1"] = [{"id": "t1", "title": "Buy oat milk", "status": "needsAction", "deleted": True,
                                "updated": "2026-01-07T00:00:00Z"}]
    ld.load()
    assert client.task_calls[1][1] is not None  # incremental: updatedMin from the last run
    t1 = GTask.objects.get(task_id="t1")
    assert t1.title == "Buy oat milk" and t1.deleted
    assert GTask.objects.count() == 2 and GTaskList.objects.count() == 1

    ld.load(full=True)
    assert client.task_calls[2][1] is None
