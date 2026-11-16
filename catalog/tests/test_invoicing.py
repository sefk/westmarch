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
    assert [(r["start"].strftime("%H:%M"), r["end"].strftime("%H:%M"), r["hours"]) for r in rows] == [
        ("09:00", "09:20", Decimal("0.25")), ("09:31", "09:36", 0), ("13:00", "13:05", 0)]  # 20 min -> nearest quarter
    [line] = inv.lines(abp, rows, {})  # the two 5-minute stretches round to nothing: they show only in the detail
    assert line["hours"] == Decimal("0.25") and len(line["parts"]) == 3


def test_agent_time_is_not_the_session_span(abp):
    Session.objects.create(session_id="long", agent="claude", cwd="/agent/dfo", started_at=at("11-11", "20:57"),
                           ended_at=at("11-12", "09:13"), typed_prompts=[at("11-11", "21:00").astimezone(dt_timezone.utc).isoformat()])
    assert inv.lines(abp, inv.entries(abp, NOW), {}) == []  # one prompt; twelve hours of agent work bill nothing


def test_concurrent_sessions_share_one_block_and_name_their_repos(abp):
    agent_session("11-16", ["10:00", "10:10"], cwd="/agent/dfo/website")
    agent_session("11-16", ["10:05", "10:20"], cwd="/agent/dfo/apis")
    [row] = inv.entries(abp, NOW)
    assert row["description"] == "Development work (apis, website)"
    assert (row["start"].strftime("%H:%M"), row["end"].strftime("%H:%M"), row["hours"]) == ("10:00", "10:25", Decimal("0.5"))


def test_calendar_wins_over_agent_time(abp, cal):
    ev(cal, "DFO: planning", "11-16", "10:00", "11:00")
    agent_session("11-16", ["09:50", "10:05", "10:20", "10:40"])  # block 9:50-10:45; 10:00-10:45 is on the calendar
    rows = inv.lines(abp, inv.entries(abp, NOW), {})
    assert [(r["agent"], r["hours"], r["overlap"]) for r in rows] == [(True, Decimal("0.25"), True), (False, Decimal(1), False)]
    assert rows[1]["title"] == "DFO: planning"  # the meeting keeps its full hour
    agent_session("11-17", ["10:00", "10:10"])
    ev(cal, "DFO: all morning", "11-17", "09:00", "12:00")
    day = [r for r in inv.lines(abp, inv.entries(abp, NOW), {}) if r["day"] == date(2026, 11, 17)]
    assert [r["title"] for r in day] == ["DFO: all morning"]


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


# --- rescan ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def loaders(monkeypatch):
    """Stand-ins for the gcal and agentsview loaders; records which ran and in what mode."""
    from catalog import views
    from core.models import LoaderRun
    ran, fail, off = [], set(), set()

    class Fake:
        def __init__(self, name):
            self.name = name

    def run(loader, full=False):
        ran.append((loader.name, full))
        return LoaderRun(loader=loader.name, ok=loader.name not in fail and loader.name not in off,
                         message="disabled: no credentials" if loader.name in off else "")

    monkeypatch.setattr(views, "registry", lambda: {n: Fake(n) for n in ("gcal", "agentsview")})
    monkeypatch.setattr(views, "run_loader", run)
    from types import SimpleNamespace
    return SimpleNamespace(ran=ran, fail=fail, off=off)


def test_rescan_reloads_calendar_and_agent_sessions(abp, client, loaders):
    ran = loaders.ran
    resp = client.post(reverse("catalog:rescan"))
    assert ran == [("gcal", False), ("agentsview", False)]
    assert resp.status_code == 302 and resp.url == reverse("catalog:invoicing") + "?rescan=ok"
    assert "Rescanned" in client.get(resp.url).content.decode()


def test_rescan_says_which_source_failed(abp, client, loaders):
    ran = loaders.ran
    loaders.fail.add("agentsview")
    resp = client.post(reverse("catalog:rescan"))
    assert ran == [("gcal", False), ("agentsview", False)]  # one failing doesn't skip the other
    page = client.get(resp.url).content.decode()
    assert "Rescan failed for Agent sessions" in page and "Rescanned" not in page


def test_rescan_is_post_only_and_csrf_protected(abp, loaders):
    from django.test import Client
    ran = loaders.ran
    strict = Client(enforce_csrf_checks=True)
    assert strict.get(reverse("catalog:rescan")).status_code == 405
    assert strict.post(reverse("catalog:rescan")).status_code == 403 and ran == []
    page = strict.get(reverse("catalog:invoicing"))  # the page hands out the token its form carries
    token = page.cookies["csrftoken"].value
    assert 'name="csrfmiddlewaretoken"' in page.content.decode()
    assert strict.post(reverse("catalog:rescan"), {"csrfmiddlewaretoken": token}).status_code == 302
    assert len(ran) == 2


def test_a_second_rescan_while_one_runs_is_refused(abp, client, loaders):
    import fcntl
    from catalog import views
    with open(views.RESCAN_LOCK, "a") as other:  # another worker mid-rescan
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        resp = client.post(reverse("catalog:rescan"))
    assert loaders.ran == [] and resp.url.endswith("?rescan=busy")
    assert "already running" in client.get(resp.url).content.decode()
    client.post(reverse("catalog:rescan"))  # the lock is free again
    assert len(loaders.ran) == 2


def test_a_disabled_loader_is_not_a_refresh(abp, client, loaders):
    loaders.off.add("gcal")  # no Google credentials: loads nothing but records ok
    page = client.get(client.post(reverse("catalog:rescan")).url).content.decode()
    assert "Rescan failed for Calendar" in page and "Rescanned" not in page
    from core.models import LoaderRun
    now = timezone.now()
    LoaderRun.objects.create(loader="gcal", ok=True, message="disabled: no credentials", started=now, finished=now)
    assert "Calendar never loaded" in client.get(reverse("catalog:invoicing")).content.decode()


def test_page_shows_when_each_source_last_loaded(abp, client):
    from core.models import LoaderRun
    now = timezone.now()
    LoaderRun.objects.create(loader="gcal", ok=True, started=now, finished=now)
    page = client.get(reverse("catalog:invoicing")).content.decode()
    assert "Calendar loaded" in page and "Agent sessions never loaded" in page
    assert ">Rescan<" in page
    assert "bogus" not in client.get(reverse("catalog:invoicing") + "?rescan=failed&failed=bogus").content.decode()


# --- daily roll-up and notes ----------------------------------------------------------------------------------------

def test_a_days_agent_stretches_roll_up_into_one_line_with_the_working_shown(abp, cal):
    agent_session("11-16", ["07:52", "07:53", "08:03"], cwd="/agent/dfo/website")  # 7:52-8:08, 16 min -> 0.25
    agent_session("11-16", ["14:00", "14:10", "14:25"], cwd="/agent/dfo/website")  # 14:00-14:30, 30 min -> 0.5
    ev(cal, "DFO: planning", "11-16", "10:00", "11:00")
    rows = inv.entries(abp, NOW)
    assert len(rows) == 3
    a, meeting, b = rows
    assert a["calc"] == {"prompts": 3, "first": at("11-16", "07:52"), "last": at("11-16", "08:03"), "tail": 5,
                         "gross": 16, "calendar": 0, "net": 16, "increment": 15}
    lines = inv.lines(abp, rows, {})
    assert [(ln["agent"], ln["hours"]) for ln in lines] == [(True, Decimal("0.75")), (False, Decimal(1))]
    day = lines[0]
    assert (day["parts"], day["amount"], day["rule"]) == ([a, b], Decimal(150), "agent: 2 stretches")
    assert (day["start"], day["end"]) == (a["start"], b["end"])
    assert day["description"] == "Development work (website)" and day["note"] == ""
    assert lines[1]["parts"] == [] and lines[1]["title"] == "DFO: planning"  # meetings are not rolled up


def test_the_note_describes_the_day(abp):
    from core.models import BillingNote
    agent_session("11-16", ["07:52", "07:53", "08:03"])
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 16), text="API Keys (website/142)")
    [day] = inv.summary(abp, NOW)["lines"]
    assert day["description"] == "API Keys (website/142)" and day["note"] == "API Keys (website/142)"


def test_calendar_overlap_shows_in_the_arithmetic(abp, cal):
    ev(cal, "DFO: planning", "11-16", "10:00", "11:00")
    agent_session("11-16", ["09:50", "10:05"])  # 9:50-10:10: 10 min before the meeting, 10 inside it
    [agent, _] = inv.entries(abp, NOW)
    assert (agent["calc"]["gross"], agent["calc"]["calendar"], agent["calc"]["net"]) == (20, 10, 10)
    assert agent["overlap"] and agent["hours"] == Decimal("0.25")


def test_a_day_with_only_unbillable_stretches_has_no_line(abp):
    agent_session("11-16", ["09:00"])
    assert len(inv.entries(abp, NOW)) == 1 and inv.lines(abp, inv.entries(abp, NOW), {}) == []


def test_finished_and_upcoming_stretches_of_a_day_are_separate_lines(abp):
    agent_session("11-16", ["09:00", "09:10"])
    agent_session("11-16", ["15:00", "15:10"])
    lines = inv.summary(abp, at("11-16", "15:12"))["lines"]
    assert [(ln["upcoming"], ln["hours"]) for ln in lines] == [(False, Decimal("0.25")), (True, Decimal("0.25"))]


def test_invoice_rolls_up_by_day_and_flags_undescribed_days(abp):
    from core.models import BillingNote
    agent_session("11-16", ["09:00", "09:10"])
    agent_session("11-16", ["14:00", "14:10", "14:25"])
    agent_session("11-17", ["09:00", "09:10"])
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 16), text="API Keys (website/142)")
    data = inv.invoice(abp, "2026-11", NOW)
    assert [(r["day"].day, r["description"], r["hours"]) for r in data["rows"]] == [
        (16, "API Keys (website/142)", Decimal("0.75")), (17, "Development work", Decimal("0.25"))]
    assert data["hours"] == Decimal(1) and data["undescribed"] == [date(2026, 11, 17)]


def test_entries_page_expands_a_day_and_the_invoice_page_does_not(abp, client):
    from core.models import BillingNote
    agent_session("11-16", ["07:52", "07:53", "08:03"])
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 16), text="API Keys (website/142)")
    page = client.get(reverse("catalog:invoicing")).content.decode()
    assert "API Keys (website/142)" in page and '<details id="day-t-2026-11-16">' in page
    assert "3 prompts, 7:52 to 8:03" in page and "= 16 min" in page and "nearest 15 = 0.25 h" in page
    assert 'name="text"' in page and 'name="csrfmiddlewaretoken"' in page
    inv_page = client.get(reverse("catalog:invoice", args=[abp.slug, "2026-11"])).content.decode()
    assert "API Keys (website/142)" in inv_page and "<details" not in inv_page and "prompts" not in inv_page


def test_invoice_page_names_the_undescribed_days(abp, client):
    agent_session("11-16", ["07:52", "07:53", "08:03"])
    page = " ".join(client.get(reverse("catalog:invoice", args=[abp.slug, "2026-11"])).content.decode().split())
    assert "1 day of development work has no description yet (Nov 16)" in page


def test_saving_a_note(abp, client):
    from core.models import BillingNote
    url = reverse("catalog:note")
    resp = client.post(url, {"slug": "t", "day": "2026-11-16", "text": "  API Keys   (website/142) "})
    assert resp.status_code == 302 and resp.url == reverse("catalog:invoicing") + "#day-t-2026-11-16"
    assert BillingNote.objects.get().text == "API Keys (website/142)"
    client.post(url, {"slug": "t", "day": "2026-11-16", "text": "Edited"})
    assert BillingNote.objects.get().text == "Edited"
    client.post(url, {"slug": "t", "day": "2026-11-16", "text": "  "})  # emptied: cleared
    assert BillingNote.objects.count() == 0
    assert client.post(url, {"slug": "t", "day": "nope", "text": "x"}).status_code == 404
    assert client.post(url, {"slug": "zzz", "day": "2026-11-16", "text": "x"}).status_code == 404
    assert client.get(url).status_code == 405


def test_saving_a_note_needs_the_csrf_token(abp):
    from django.test import Client
    strict = Client(enforce_csrf_checks=True)
    assert strict.post(reverse("catalog:note"), {"slug": "t", "day": "2026-11-16", "text": "x"}).status_code == 403


def test_one_description_applies_to_every_ticked_day(abp, client):
    from core.models import BillingNote
    url = reverse("catalog:notes")
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 12), text="Old")
    resp = client.post(url, {"slug": "t", "day": ["2026-11-12", "2026-11-13", "2026-11-13", "2026-11-16"],
                             "text": "  API Keys   (website/142) "})
    assert resp.status_code == 302 and resp.url == reverse("catalog:invoicing") + "#entries-t"
    assert dict(BillingNote.objects.values_list("day", "text")) == {
        date(2026, 11, d): "API Keys (website/142)" for d in (12, 13, 16)}  # replaced Old, no duplicates


def test_bulk_describe_never_clears_and_needs_valid_input(abp, client):
    from core.models import BillingNote
    url = reverse("catalog:notes")
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 12), text="Keep")
    client.post(url, {"slug": "t", "day": ["2026-11-12"], "text": " "})  # empty text
    client.post(url, {"slug": "t", "text": "x"})  # nothing ticked
    assert dict(BillingNote.objects.values_list("day", "text")) == {date(2026, 11, 12): "Keep"}
    assert client.post(url, {"slug": "t", "day": ["2026-11-12", "nope"], "text": "x"}).status_code == 404
    assert client.post(url, {"slug": "zzz", "day": "2026-11-12", "text": "x"}).status_code == 404
    assert client.get(url).status_code == 405
    from django.test import Client
    assert Client(enforce_csrf_checks=True).post(url, {"slug": "t", "day": "2026-11-12", "text": "x"}).status_code == 403


def test_page_offers_ticking_days_and_reusing_descriptions(abp, client):
    from core.models import BillingNote
    agent_session("11-13", ["09:00", "09:10"])
    agent_session("11-16", ["07:52", "07:53", "08:03"])
    BillingNote.objects.create(billing_project=abp, day=date(2026, 11, 13), text="API Keys (website/142)")
    page = client.get(reverse("catalog:invoicing")).content.decode()
    assert page.count('type="checkbox" class="pick"') == 2 and 'form="bulk-t"' in page
    assert '<option value="API Keys (website/142)">' in page
    assert page.count(" data-undescribed ") == 1  # only Nov 16 is undescribed
    assert page.count("<script>") == 1


def test_note_choices_are_distinct_and_latest_first(abp):
    from core.models import BillingNote
    for d, t in [(10, "A"), (11, "B"), (12, "A")]:
        BillingNote.objects.create(billing_project=abp, day=date(2026, 11, d), text=t)
    assert inv.note_choices(abp) == ["A", "B"]


def test_invoicing_page_title_and_script_placement(abp, client):
    page = client.get(reverse("catalog:invoicing")).content.decode()
    assert "<title>Invoicing" in page and "<script>" not in page.split("</title>")[0]
    assert page.count("<script>") == 1  # the one that reopens a saved day, in the body
