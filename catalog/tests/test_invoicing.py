from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from catalog import invoicing as inv
from core.models import BillingProject
from sources.agentsview.models import Session
from sources.google.models import GCalCalendar, GCalEvent

pytestmark = pytest.mark.django_db


def at(day, hhmm):
    """A Pacific-time datetime on 2026-<day> like '11-10'."""
    return timezone.make_aware(datetime.strptime(f"2026-{day} {hhmm}", "%Y-%m-%d %H:%M"))


NOW = at("12-31", "12:00")  # after the term: everything has happened unless a test says otherwise


@pytest.fixture
def bp():
    BillingProject.objects.all().delete()
    return BillingProject.objects.create(
        slug="t", name="Test", tags=["DFO"], attendee_domains=["ode.partners"], rate_usd=Decimal("200"),
        hours_cap=Decimal("2"), term_start=date(2026, 11, 9), term_end=date(2026, 12, 18),
        invoice_schedule={"2026-11": {"cap": 300, "due": "2026-12-05"}, "2026-12": {"cap": 4000, "due": "2027-01-05"}})


@pytest.fixture
def cal():
    return GCalCalendar.objects.create(calendar_id="me@example.com", primary=True)


_n = 0


def ev(cal, summary, day, start, end, attendees=(), **kw):
    global _n
    _n += 1
    return GCalEvent.objects.create(
        calendar=cal, event_id=f"e{_n}", summary=summary, start=at(day, start), end=at(day, end),
        attendees=[{"email": e, **extra} for e, extra in attendees], **{"status": "confirmed", **kw})


def test_title_tag_is_a_whole_word_and_case_insensitive(bp, cal):
    ev(cal, "DFO: API keys", "11-10", "09:00", "10:00")
    ev(cal, "dfo planning", "11-10", "11:00", "11:30")
    ev(cal, "Sef/Judson lunch", "11-10", "12:00", "13:00")  # "dso" inside a word
    ev(cal, "DFOX launch", "11-10", "14:00", "15:00")
    rows = inv.entries(bp, NOW)
    assert [r["description"] for r in rows] == ["API keys", "planning"]
    assert [r["hours"] for r in rows] == [Decimal(1), Decimal("0.5")]
    assert rows[0]["rule"] == "title: DFO"


def test_attendee_domain_counts_but_not_me(bp, cal):
    ev(cal, "Weekly sync", "11-10", "09:00", "09:30", [("ramya@ode.partners", {})])
    ev(cal, "Mine", "11-10", "10:00", "11:00", [("me@ode.partners", {"self": True})])
    ev(cal, "Other", "11-10", "12:00", "13:00", [("x@example.com", {})])
    rows = inv.entries(bp, NOW)
    assert [r["title"] for r in rows] == ["Weekly sync"]
    assert rows[0]["rule"] == "attendee: ode.partners" and rows[0]["description"] == "Weekly sync"


def test_ignored_events(bp, cal):
    other = GCalCalendar.objects.create(calendar_id="family", primary=False)
    ev(other, "DFO elsewhere", "11-10", "09:00", "10:00")
    ev(cal, "DFO cancelled", "11-10", "09:00", "10:00", status="cancelled")
    ev(cal, "DFO declined", "11-10", "10:00", "11:00", [("me@x.com", {"self": True, "responseStatus": "declined"})])
    ev(cal, "DFO before term", "11-03", "09:00", "10:00")
    ev(cal, "DFO after term", "12-21", "09:00", "10:00")
    GCalEvent.objects.create(calendar=cal, event_id="allday", summary="DFO offsite", all_day=True,
                             start=at("11-10", "00:00"), end=at("11-11", "00:00"))
    ev(cal, "DFO counted", "12-18", "09:00", "10:00")  # last day of the term counts
    assert [r["title"] for r in inv.entries(bp, NOW)] == ["DFO counted"]


def test_overlap_counts_once_and_rounding(bp, cal):
    ev(cal, "DFO block", "11-10", "09:00", "11:00")
    ev(cal, "Sync", "11-10", "10:00", "11:30", [("r@ode.partners", {})])  # 90 min, 60 shared
    ev(cal, "DFO inside", "11-10", "10:15", "10:45")  # entirely covered
    ev(cal, "DFO odd", "11-11", "09:00", "09:08")  # 8 min -> 0.25 h
    ev(cal, "DFO short", "11-11", "10:00", "10:05")  # 5 min -> 0
    rows = inv.entries(bp, NOW)
    assert [r["hours"] for r in rows] == [Decimal(2), Decimal("0.5"), Decimal(0), Decimal("0.25"), Decimal(0)]
    assert [r["overlap"] for r in rows] == [False, True, True, False, False]


def test_summary_caps_and_months(bp, cal):
    ev(cal, "DFO a", "11-10", "09:00", "10:30")  # 1.5 h
    ev(cal, "DFO b", "11-11", "09:00", "10:00")  # 1 h: crosses the 2 h cap
    ev(cal, "DFO c", "12-01", "09:00", "10:00")
    s = inv.summary(bp, NOW)
    assert s["hours"] == Decimal("3.5") and s["amount"] == 700 and s["over"] and s["remaining"] == Decimal("-1.5")
    assert [r["over_cap"] for r in s["rows"]] == [False, True, True]
    nov, dec = s["months"]
    assert (nov["month"], nov["hours"], nov["amount"], nov["over_cap"], nov["due"]) == ("2026-11", Decimal("2.5"), 500, True, "2026-12-05")
    assert dec["hours"] == 1 and not dec["over_cap"]


def test_warns_near_cap_and_lists_empty_scheduled_months(bp, cal):
    bp.hours_cap = Decimal("10")
    bp.save()
    ev(cal, "DFO a", "11-10", "09:00", "17:30")  # 8.5 h
    s = inv.summary(bp, NOW)
    assert s["warn"] and not s["over"] and s["pct"] == 85
    assert [m["month"] for m in s["months"]] == ["2026-11", "2026-12"] and s["months"][1]["count"] == 0


def test_pages(client, bp, cal):
    ev(cal, "DFO: API keys", "11-10", "09:00", "10:00")
    page = client.get(reverse("catalog:invoicing"))
    assert page.status_code == 200
    body = page.content.decode()
    assert "API keys" in body and "$200.00" in body and "Invoicing" in body
    page = client.get(reverse("catalog:invoice", args=["t", "2026-11"]))
    assert page.status_code == 200 and "November 2026" in page.content.decode()
    assert client.get(reverse("catalog:invoice", args=["t", "garbage"])).status_code == 404
    assert client.get(reverse("catalog:invoice", args=["nope", "2026-11"])).status_code == 404


def test_seeded_dfo_terms():
    BillingProject.objects.all().delete()
    from importlib import import_module

    from django.apps import apps
    import_module("core.migrations.0003_seed_dfo_billing").seed(apps, None)
    b = BillingProject.objects.get(slug="dfo")
    assert (b.rate_usd, b.hours_cap, b.amount_cap_usd) == (Decimal("200"), Decimal("55"), Decimal("11000"))
    assert b.tags == ["DFO"] and "ode.partners" in b.attendee_domains
    assert b.invoice_schedule["2026-11"] == {"cap": 7000, "due": "2026-12-05"}


def series(cal, summary, rule_lines, first="10-27", start="10:00", end="11:00", **kw):
    """A recurring master whose first occurrence is before the term (Tue Oct 27, 2026)."""
    kw.setdefault("raw", {"start": {"dateTime": at(first, start).isoformat(), "timeZone": "America/Los_Angeles"}})
    return ev(cal, summary, first, start, end, recurrence=rule_lines, **kw)


def days(rows):
    return [r["day"].strftime("%m-%d") for r in rows]


def test_recurring_series_is_expanded_across_the_term(bp, cal):
    series(cal, "DFO: engineering sync", ["RRULE:FREQ=WEEKLY;BYDAY=TU"])
    rows = inv.entries(bp, NOW)
    assert days(rows) == ["11-10", "11-17", "11-24", "12-01", "12-08", "12-15"]  # nothing before Nov 9
    assert {r["hours"] for r in rows} == {Decimal(1)} and rows[0]["description"] == "engineering sync"
    assert all(r["recurring"] for r in rows)


def test_recurring_attendee_match_count_and_until(bp, cal):
    series(cal, "Weekly sync", ["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"], first="11-10",
           attendees=[("r@ode.partners", {})])
    series(cal, "DFO standup", ["RRULE:FREQ=WEEKLY;UNTIL=20261117T210000Z;BYDAY=TU"], start="12:00", end="12:30")
    rows = inv.entries(bp, NOW)
    assert [(r["day"].strftime("%m-%d"), r["title"]) for r in rows] == [
        ("11-10", "Weekly sync"), ("11-10", "DFO standup"), ("11-17", "Weekly sync"), ("11-17", "DFO standup"),
        ("11-24", "Weekly sync")]


def test_exdate_and_exception_instances(bp, cal):
    m = series(cal, "DFO sync", ["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=America/Los_Angeles:20261201T100000",
                                  "EXDATE;VALUE=DATE:20261208"])
    # Nov 17 cancelled; Nov 24 moved to Wed Nov 25 at 11:00 (a stand-alone event pointing back at the series)
    ev(cal, "DFO sync", "11-17", "10:00", "11:00", status="cancelled", recurring_event_id=m.event_id,
       raw={"originalStartTime": {"dateTime": at("11-17", "10:00").isoformat()}})
    ev(cal, "DFO sync (moved)", "11-25", "11:00", "11:30", recurring_event_id=m.event_id,
       raw={"originalStartTime": {"dateTime": at("11-24", "10:00").isoformat()}})
    rows = inv.entries(bp, NOW)
    assert [(r["day"].strftime("%m-%d"), r["hours"]) for r in rows] == [
        ("11-10", Decimal(1)), ("11-25", Decimal("0.5")), ("12-15", Decimal(1))]


def test_recurring_ignored_when_cancelled_all_day_or_declined(bp, cal):
    series(cal, "DFO cancelled series", ["RRULE:FREQ=WEEKLY"], status="cancelled")
    series(cal, "DFO declined series", ["RRULE:FREQ=WEEKLY"],
           attendees=[("me@x.com", {"self": True, "responseStatus": "declined"})])
    series(cal, "DFO all-day series", ["RRULE:FREQ=WEEKLY"], all_day=True)
    assert inv.entries(bp, NOW) == []


def test_recurring_totals_reach_the_summary(bp, cal):
    series(cal, "DFO sync", ["RRULE:FREQ=WEEKLY;BYDAY=TU"])
    s = inv.summary(bp, NOW)
    assert s["hours"] == 6 and s["over"] and s["months"][0]["count"] == 3 and s["months"][1]["count"] == 3


def test_only_ended_events_bill(bp, cal):
    ev(cal, "DFO done", "11-10", "09:00", "10:00")
    ev(cal, "DFO running", "11-11", "09:00", "10:00")
    ev(cal, "DFO later", "11-12", "09:00", "10:30")
    now = at("11-11", "09:30")  # mid-way through "running"
    rows = inv.entries(bp, now)
    assert [(r["title"], r["upcoming"]) for r in rows] == [("DFO done", False), ("DFO running", True), ("DFO later", True)]
    s = inv.summary(bp, now)
    assert (s["hours"], s["scheduled"], s["amount"]) == (Decimal(1), Decimal("2.5"), 200)
    assert s["months"][0]["count"] == 1 and s["months"][0]["hours"] == 1
    assert [r["title"] for r in inv.invoice(bp, "2026-11", now)["rows"]] == ["DFO done"]
    assert inv.invoice(bp, "2026-11", now)["hours"] == 1


def test_future_series_occurrences_do_not_bill_or_use_the_cap(bp, cal):
    series(cal, "DFO sync", ["RRULE:FREQ=WEEKLY;BYDAY=TU"])
    s = inv.summary(bp, at("11-18", "00:00"))  # Nov 10 and 17 are done; 6 occurrences in all
    assert s["hours"] == 2 and s["scheduled"] == 4 and not s["over"]
    assert not any(r["over_cap"] for r in s["rows"] if r["upcoming"])


def test_unfinished_event_does_not_swallow_finished_time(bp, cal):
    ev(cal, "DFO block", "11-11", "09:00", "12:00")
    ev(cal, "Sync", "11-11", "10:00", "10:30", [("r@ode.partners", {})])
    rows = inv.entries(bp, at("11-11", "10:30"))
    assert [(r["title"], r["hours"], r["upcoming"]) for r in rows] == [
        ("DFO block", Decimal(3), True), ("Sync", Decimal("0.5"), False)]
    assert inv.summary(bp, at("11-11", "10:30"))["hours"] == Decimal("0.5")
    # once the block has ended it comes first and keeps the shared time
    assert [r["hours"] for r in inv.entries(bp, at("11-11", "13:00"))] == [Decimal(3), Decimal(0)]


def test_events_straddling_the_term_bill_only_the_part_inside(bp, cal):
    for title, start, end in [("DFO late", at("12-18", "23:00"), at("12-19", "02:00")),
                              ("DFO early", at("11-08", "22:00"), at("11-09", "01:00")),
                              ("DFO outside", at("11-08", "20:00"), at("11-08", "21:00"))]:
        GCalEvent.objects.create(calendar=cal, event_id=title, summary=title, start=start, end=end, status="confirmed")
    rows = inv.entries(bp, NOW)
    assert [(r["title"], r["hours"]) for r in rows] == [("DFO early", Decimal(1)), ("DFO late", Decimal(1))]
    assert [r["day"].strftime("%m-%d") for r in rows] == ["11-09", "12-18"]  # clipped, so the day is inside the term


def test_recurring_occurrence_straddling_the_term_start(bp, cal):
    series(cal, "DFO overnight", ["RRULE:FREQ=WEEKLY;BYDAY=SU"], first="10-25", start="23:00", end="23:30")
    m = GCalEvent.objects.get(summary="DFO overnight")
    m.end = at("10-26", "01:00")  # Sun 23:00 -> Mon 01:00, so Nov 8 23:00 -> Nov 9 01:00 straddles the start
    m.save()
    rows = inv.entries(bp, NOW)
    assert rows[0]["day"].strftime("%m-%d") == "11-09" and rows[0]["hours"] == 1


# --- agent activity -------------------------------------------------------------------------------------------------

_s = 0


def agent_session(day, times, cwd="/agent/dfo", **kw):
    """A session whose person-typed prompts landed at Pacific `times` ("HH:MM") on `day`."""
    global _s
    _s += 1
    iso = [at(day, t).astimezone(dt_timezone.utc).isoformat() for t in times]
    return Session.objects.create(session_id=f"s{_s}", agent="claude", cwd=cwd, typed_prompts=iso,
                                  last_activity_at=at(day, times[-1]) if times else None, **kw)


@pytest.fixture
def abp(bp):
    bp.use_agent_activity, bp.agent_paths = True, ["/agent/dfo"]
    bp.hours_cap = None
    bp.save()
    return bp


def test_prompts_within_the_gap_make_one_block_with_a_tail(abp):
    agent_session("11-16", ["07:52", "07:53", "08:03"])  # last prompt + 5 min tail = 7:52-8:08, 16 min
    [row] = inv.entries(abp, NOW)
    assert row["agent"] and row["rule"] == "agent: prompts you typed" and row["description"] == "Development work"
    assert (row["start"].strftime("%H:%M"), row["end"].strftime("%H:%M")) == ("07:52", "08:08")
    assert row["hours"] == Decimal("0.25") and row["amount"] == Decimal(50)


def test_a_longer_gap_splits_blocks_and_a_lone_prompt_is_too_short(abp):
    agent_session("11-16", ["09:00", "09:15", "09:31", "13:00"])  # 9:15 joins 9:00; 9:31 is 16 min on; 13:00 is alone
    rows = inv.entries(abp, NOW)
    assert [(r["start"].strftime("%H:%M"), r["end"].strftime("%H:%M")) for r in rows] == [("09:00", "09:20")]
    assert rows[0]["hours"] == Decimal("0.25")  # 20 min -> nearest quarter hour
    # the 9:31 prompt (5 min) and the 13:00 prompt (5 min) each round to nothing and don't clutter the list


def test_agent_time_is_not_the_session_span(abp):
    Session.objects.create(session_id="long", agent="claude", cwd="/agent/dfo", started_at=at("11-11", "20:57"),
                           ended_at=at("11-12", "09:13"), typed_prompts=[at("11-11", "21:00").astimezone(dt_timezone.utc).isoformat()])
    assert inv.entries(abp, NOW) == []  # one prompt; twelve hours of agent work bill nothing


def test_concurrent_sessions_share_one_block_and_name_their_repos(abp):
    agent_session("11-16", ["10:00", "10:10"], cwd="/agent/dfo/website")
    agent_session("11-16", ["10:05", "10:20"], cwd="/agent/dfo/apis")
    [row] = inv.entries(abp, NOW)
    assert row["description"] == "Development work (apis, website)"
    assert (row["start"].strftime("%H:%M"), row["end"].strftime("%H:%M"), row["hours"]) == ("10:00", "10:25", Decimal("0.5"))


def test_calendar_wins_over_agent_time(abp, cal):
    ev(cal, "DFO: planning", "11-16", "10:00", "11:00")
    agent_session("11-16", ["09:50", "10:05", "10:20", "10:40"])  # block 9:50-10:45; 10:00-10:45 is on the calendar
    rows = inv.entries(abp, NOW)
    assert [(r["agent"], r["hours"], r["overlap"]) for r in rows] == [(True, Decimal("0.25"), True), (False, Decimal(1), False)]
    assert rows[1]["title"] == "DFO: planning"  # the meeting keeps its full hour
    agent_session("11-17", ["10:00", "10:10"])
    ev(cal, "DFO: all morning", "11-17", "09:00", "12:00")
    assert [r["title"] for r in inv.entries(abp, NOW) if r["day"] == date(2026, 11, 17)] == ["DFO: all morning"]


def test_only_my_dfo_sessions_count(abp):
    agent_session("11-16", ["08:00", "08:10"], cwd="/agent/dfo-other")  # sibling path, not under it
    agent_session("11-16", ["09:00", "09:10"], cwd="/elsewhere")
    agent_session("11-16", ["10:00", "10:10"], is_automated=True)
    Session.objects.create(session_id="unread", agent="claude", cwd="/agent/dfo", typed_prompts=None)
    assert inv.entries(abp, NOW) == []
    abp.use_agent_activity = False
    abp.save()
    agent_session("11-16", ["11:00", "11:10"])
    assert inv.entries(abp, NOW) == []


def test_prompts_outside_the_term_do_not_count(abp):
    agent_session("11-03", ["09:00", "09:10"])
    agent_session("12-21", ["09:00", "09:10"])
    assert inv.entries(abp, NOW) == []


def test_a_block_straddling_the_term_bills_only_the_part_inside(abp):
    agent_session("11-08", ["23:50"])  # the day before the term starts
    agent_session("11-09", ["00:05", "00:15"])  # one block, 23:50-00:20; the 20 minutes inside round to 0.25
    [row] = inv.entries(abp, NOW)
    assert (row["start"].strftime("%m-%d %H:%M"), row["end"].strftime("%m-%d %H:%M")) == ("11-09 00:00", "11-09 00:20")
    assert row["hours"] == Decimal("0.25")
    agent_session("12-18", ["23:50"])
    agent_session("12-19", ["00:05"])  # the tail of this block runs past the term's end
    last = inv.entries(abp, NOW)[-1]
    assert last["end"].strftime("%m-%d %H:%M") == "12-19 00:00" and last["hours"] == Decimal("0.25")


def test_a_tail_longer_than_the_gap_does_not_bill_time_twice(abp):
    abp.agent_gap_minutes, abp.agent_tail_minutes = 15, 30
    abp.save()
    agent_session("11-16", ["09:00", "09:20"])  # 20 apart: separate runs, but the first tail reaches the second
    [row] = inv.entries(abp, NOW)
    assert (row["start"].strftime("%H:%M"), row["end"].strftime("%H:%M"), row["hours"]) == ("09:00", "09:50", Decimal("0.75"))  # 50 min, once (not 60)


def test_an_unfinished_block_does_not_bill_yet(abp):
    agent_session("11-16", ["10:00", "10:10"])  # runs to 10:15
    now = at("11-16", "10:12")
    [row] = inv.entries(abp, now)
    assert row["upcoming"] and not inv.summary(abp, now)["hours"]
    assert inv.summary(abp, now)["scheduled"] == Decimal("0.25")
    assert inv.entries(abp, at("11-16", "10:30"))[0]["upcoming"] is False


def test_the_cap_runs_over_calendar_and_agent_time_in_date_order(abp, cal):
    abp.hours_cap = Decimal("2")
    abp.save()
    ev(cal, "DFO: first", "11-16", "09:00", "10:00")
    agent_session("11-16", ["11:00", "11:14", "11:28", "11:42", "11:56"])  # 11:00-12:01, 1 h
    ev(cal, "DFO: last", "11-16", "14:00", "15:00")
    assert [(r["title"], r["over_cap"]) for r in inv.entries(abp, NOW)] == [
        ("DFO: first", False), ("Agent session (active)", False), ("DFO: last", True)]
    assert inv.summary(abp, NOW)["agent_hours"] == Decimal(1)


def test_pages_show_the_agent_caveat_and_rows(abp, client):
    agent_session("11-16", ["07:52", "07:53", "08:03"])
    page = client.get(reverse("catalog:invoicing")).content.decode()
    assert "Agent time is an estimate" in page and "working on your behalf without you" in page
    assert 'class="tag">agent' in page and "07:52" not in page.replace("7:52", "")  # shown as g:i, not zero padded
    page = client.get(reverse("catalog:invoice", args=[abp.slug, "2026-11"])).content.decode()
    assert "Development work" in page


def test_no_caveat_when_agent_activity_is_off(bp, client):
    assert "Agent time is an estimate" not in client.get(reverse("catalog:invoicing")).content.decode()


def test_dfo_seed_uses_agent_activity():
    BillingProject.objects.all().delete()
    from importlib import import_module
    from django.apps import apps
    import_module("core.migrations.0003_seed_dfo_billing").seed(apps, None)
    import_module("core.migrations.0005_dfo_agent_activity").enable(apps, None)
    dfo = BillingProject.objects.get(slug="dfo")
    assert dfo.use_agent_activity and dfo.agent_dirs()[0].endswith("/src/dfo")
    assert (dfo.agent_gap_minutes, dfo.agent_tail_minutes) == (15, 5)
