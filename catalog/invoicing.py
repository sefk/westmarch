"""Invoicing analysis: "what hours can I bill, and where am I against the contract?"

The primary Google calendar is the record of time. For each active
core.BillingProject an event is billable when

- its title contains one of the project's tags as a whole word ("DFO"), or
- someone other than me on the guest list is at one of its attendee domains.

Agent activity is a second input. When the project has `use_agent_activity`,
the prompts I typed to an AI agent in a session under its `agent_paths` mark
time I was at work even if I never put a block on the calendar. Prompts less
than `agent_gap_minutes` apart make one block, which runs from the first prompt
to the last plus `agent_tail_minutes` (reading the answer). Time agents work
alone, before the first prompt or after the tail, never bills. A calendar
entry always wins: the part of an agent block inside one is dropped.

Recurring meetings are expanded: the mirror keeps one master per series
(with its RRULE/EXDATE/RDATE), so its occurrences inside the term are
generated here, less any occurrence replaced or cancelled by an exception
instance (those arrive as their own events). Cancelled, declined and
all-day events are ignored, and an event straddling the term's start or end
bills only the part inside. Only events that have ended bill: upcoming ones
are listed and totalled separately as "scheduled", never in hours, caps or
invoices. Time that two billable events share counts once (the earlier event
keeps it). Each entry rounds to the nearest `increment_minutes`. Nothing is
stored: the entries are recomputed from GCalEvent on every request, so a
calendar edit shows on the next load.
"""

import logging
import re
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from django.db.models import Q
from django.utils import timezone

from core.models import BillingProject
from sources.agentsview.models import Session
from sources.google.models import GCalEvent

logger = logging.getLogger(__name__)
WARN_AT = Decimal("0.8")  # share of the hours cap that starts a warning
ZERO = Decimal(0)


def tag_pattern(tags):
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in tags) + r")\b", re.IGNORECASE) if tags else None


def attendee_match(event, domains):
    """First guest (not me) at one of `domains`, or None."""
    wanted = {d.lower() for d in domains}
    for a in event.attendees or []:
        email = (a.get("email") or "").lower()
        if not a.get("self") and email.rpartition("@")[2] in wanted:
            return email
    return None


def declined(event):
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in event.attendees or [])


def describe(title, pattern):
    """The title less its leading tag and separator: "DFO: API keys" -> "API keys"."""
    if pattern:
        rest = re.sub(pattern.pattern + r"[\s:\-\u2013\u2014]*", "", title, count=1, flags=re.IGNORECASE).strip()
        if rest:
            return rest
    return title.strip()


def hours_for(minutes, increment):
    """Whole minutes -> hours, rounded to the nearest `increment` minutes."""
    steps = (Decimal(minutes) / increment).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return steps * increment / 60


def term_bounds(bp):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(bp.term_start, time.min), tz)
    end = timezone.make_aware(datetime.combine(bp.term_end + timedelta(days=1), time.min), tz)
    return start, end


def series_zone(master):
    name = (master.raw.get("start") or {}).get("timeZone") or master.calendar.time_zone
    try:
        return ZoneInfo(name) if name else timezone.get_current_timezone()
    except Exception:
        return timezone.get_current_timezone()


def as_utc(value, zone):
    """An RRULE-style or ISO date / date-time, or an API {dateTime|date}, as an aware UTC datetime.
    Naive values (and bare dates, at midnight) are read in `zone`."""
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    try:
        parsed = datetime.fromisoformat(value.strip()) if value else None  # 20261117T100000Z, 2026-11-17, ...
    except ValueError:
        return None
    if parsed is None:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).astimezone(dt_timezone.utc)


def date_lines(lines, kind, zone, anchor):
    """Instants from the master's EXDATE / RDATE lines. A bare date means the master's time that day."""
    out = set()
    for line in lines:
        head, _, body = line.partition(":")
        params = head.split(";")
        if params[0] != kind:
            continue
        tz = next((ZoneInfo(p[5:]) for p in params[1:] if p.startswith("TZID=")), zone)
        for v in body.split(","):
            when = as_utc(v, tz)
            if when is not None and "T" not in v:
                day = when.astimezone(tz).date()
                when = datetime.combine(day, anchor.astimezone(tz).time(), tzinfo=tz).astimezone(dt_timezone.utc)
            if when is not None:
                out.add(when)
    return out


def occurrences(master, lo, hi, replaced):
    """Start instants of a recurring master inside [lo, hi), less exceptions."""
    zone = series_zone(master)
    first = master.start
    wall = timezone.localtime(first, zone)
    rules = [line[len("RRULE:"):] for line in master.recurrence if line.startswith("RRULE:")]
    found = set()
    for rule in rules:
        try:
            found.update(rrulestr(rule, dtstart=wall).between(lo.astimezone(zone), hi.astimezone(zone), inc=True))
        except (ValueError, TypeError) as exc:
            logger.warning("invoicing: can't expand %r for %r: %s", rule, master.summary, exc)
            found.add(wall)
    found = {f.astimezone(dt_timezone.utc) for f in found}
    found |= {r for r in date_lines(master.recurrence, "RDATE", zone, first) if lo <= r < hi}
    found -= date_lines(master.recurrence, "EXDATE", zone, first)
    return sorted(f for f in found if lo <= f < hi and f not in replaced)


def candidates(bp, lo, hi):
    """(event, start, end) for each event or series occurrence overlapping [lo, hi), in time order."""
    base = (GCalEvent.objects.filter(calendar__primary=True, all_day=False, end__isnull=False)
            .exclude(status="cancelled").select_related("calendar"))
    out = [(e, e.start, e.end) for e in base.filter(recurrence=[], end__gt=lo, start__lt=hi)]
    masters = list(base.exclude(recurrence=[]).filter(start__lt=hi))
    replaced = {}  # master event_id -> original start instants an exception instance replaced or cancelled
    for r in (GCalEvent.objects.filter(calendar__primary=True, recurring_event_id__in=[m.event_id for m in masters])
              .values("recurring_event_id", "raw")):
        when = as_utc((r["raw"] or {}).get("originalStartTime"), dt_timezone.utc)
        if when:
            replaced.setdefault(r["recurring_event_id"], set()).add(when)
    for m in masters:
        length = m.end - m.start
        out += [(m, s, s + length) for s in occurrences(m, lo - length, hi, replaced.get(m.event_id, set()))]
    return sorted(out, key=lambda c: (c[1], c[2]))


def typed_prompts(bp, lo, hi):
    """(instant, repo) for each prompt I typed to an agent inside [lo, hi), time-ordered.
    `repo` is the first directory under the agent path ("" for the path itself)."""
    dirs = bp.agent_dirs()
    if not (bp.use_agent_activity and dirs):
        return []
    under = Q()
    for d in dirs:
        under |= Q(cwd=d) | Q(cwd__startswith=d + "/")
    out = set()
    for cwd, times in (Session.objects.filter(under, is_automated=False, typed_prompts__isnull=False)
                       .filter(Q(last_activity_at__gte=lo) | Q(last_activity_at__isnull=True))
                       .values_list("cwd", "typed_prompts")):
        base = next(d for d in dirs if cwd == d or cwd.startswith(d + "/"))
        repo = cwd[len(base):].strip("/").partition("/")[0]
        for t in times:
            when = as_utc(t, dt_timezone.utc)
            if when and lo <= when < hi:
                out.add((when, repo))
    return sorted(out)


def agent_blocks(bp, lo, hi):
    """(start, end, repos) work blocks from typed prompts; concurrent sessions share a block."""
    gap, tail = timedelta(minutes=bp.agent_gap_minutes), timedelta(minutes=bp.agent_tail_minutes)
    blocks = []  # [first, last, repos]
    for when, repo in typed_prompts(bp, lo, hi):
        if blocks and when - blocks[-1][1] <= gap:
            blocks[-1][1] = when
            blocks[-1][2].add(repo)
        else:
            blocks.append([when, when, {repo}])
    return [(a, min(b + tail, hi), repos) for a, b, repos in blocks]


def minutes_inside(start, end, spans):
    """Whole minutes of [start, end) that fall within the union of `spans`."""
    total, edge = timedelta(), start
    for a, b in sorted(spans):
        a, b = max(a, edge), min(b, end)
        if b > a:
            total += b - a
            edge = b
    return int(total.total_seconds() // 60)


def entries(bp, now=None):
    """Entries in date order, each a dict; hours is 0 where an overlap ate the event.
    `upcoming` marks those that haven't ended yet: they don't bill and don't count toward the cap."""
    now = now or timezone.now()
    lo, hi = term_bounds(bp)
    pattern = tag_pattern(bp.tags)
    out, covered = [], {False: None, True: None}  # covered: end of claimed time, by upcoming?
    claimed = []  # every calendar entry's span: agent time inside it is already accounted for
    for e, e_start, e_end in candidates(bp, lo, hi):
        e_start, e_end = max(e_start, lo), min(e_end, hi)  # only the part inside the term bills
        if declined(e):
            continue
        guest = attendee_match(e, bp.attendee_domains)
        hit = pattern.search(e.summary) if pattern else None
        if hit:
            rule = f"title: {hit.group(1)}"
        elif guest:
            rule = f"attendee: {guest.rpartition('@')[2]}"
        else:
            continue
        upcoming = e_end > now
        # Time already claimed by an earlier event. Unfinished events never claim time from
        # finished ones (they don't bill yet); they do yield to finished ones.
        seen = [c for k, c in covered.items() if c and (upcoming or not k)]
        begin = max(e_start, *seen) if seen else e_start
        covered[upcoming] = max(e_end, covered[upcoming]) if covered[upcoming] else e_end
        claimed.append((e_start, e_end))
        minutes = max(0, int((e_end - begin).total_seconds() // 60))
        out.append(entry(bp, e_start, e_end, minutes, upcoming, begin > e_start, rule=rule, title=e.summary,
                         description=describe(e.summary, pattern), event_id=e.pk, recurring=bool(e.recurrence)))
    for a_start, a_end, repos in agent_blocks(bp, lo, hi):
        minutes = int((a_end - a_start).total_seconds() // 60) - minutes_inside(a_start, a_end, claimed)
        if hours_for(minutes, bp.increment_minutes) == 0:  # too short to bill, or all on the calendar already
            continue
        named = ", ".join(sorted(r for r in repos if r))
        out.append(entry(bp, a_start, a_end, minutes, a_end > now, minutes < (a_end - a_start).total_seconds() // 60,
                         rule="agent: prompts you typed", title="Agent session (active)",
                         description=f"Development work ({named})" if named else "Development work", agent=True))
    out.sort(key=lambda r: (r["start"], r["end"]))
    running = ZERO
    for r in out:  # the cap runs over finished time in date order, calendar and agent together
        if not r["upcoming"]:
            running += r["hours"]
            r["over_cap"] = bp.hours_cap is not None and running > bp.hours_cap
    return out


def entry(bp, start, end, minutes, upcoming, overlap, *, rule, title, description, event_id=None, recurring=False,
          agent=False):
    hours = hours_for(minutes, bp.increment_minutes)
    local = timezone.localtime(start)
    return {
        "event_id": event_id, "day": local.date(), "month": f"{local:%Y-%m}",
        "start": local, "end": timezone.localtime(end), "title": title, "description": description,
        "rule": rule, "hours": hours, "amount": hours * bp.rate_usd, "overlap": overlap, "over_cap": False,
        "recurring": recurring, "upcoming": upcoming, "agent": agent,
    }


def month_rows(bp, rows):
    """Per-month totals, including scheduled months that have no time yet."""
    months = {m: {"month": m, "hours": ZERO, "count": 0} for m in bp.invoice_schedule}
    for r in rows:
        m = months.setdefault(r["month"], {"month": r["month"], "hours": ZERO, "count": 0})
        m["hours"] += r["hours"]
        m["count"] += 1
    result = []
    for key in sorted(months):
        m, sched = months[key], bp.invoice_schedule.get(key, {})
        m["label"] = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
        m["amount"] = m["hours"] * bp.rate_usd
        m["cap"] = Decimal(str(sched["cap"])) if "cap" in sched else None
        m["due"] = sched.get("due")
        m["over_cap"] = m["cap"] is not None and m["amount"] > m["cap"]
        result.append(m)
    return result


def summary(bp, now=None):
    rows = entries(bp, now)
    done = [r for r in rows if not r["upcoming"]]
    hours = sum((r["hours"] for r in done), ZERO)
    cap = bp.hours_cap
    return {
        "bp": bp, "rows": rows, "months": month_rows(bp, done), "hours": hours,
        "agent_hours": sum((r["hours"] for r in done if r["agent"]), ZERO),
        "scheduled": sum((r["hours"] for r in rows if r["upcoming"]), ZERO), "amount": hours * bp.rate_usd,
        "hours_cap": cap, "remaining": None if cap is None else cap - hours,
        "pct": None if not cap else min(100, int(hours / cap * 100)),
        "over": cap is not None and hours > cap,
        "warn": cap is not None and hours >= cap * WARN_AT,
        "overlaps": sum(1 for r in rows if r["overlap"]),
    }


def invoice(bp, month, now=None):
    """One month's invoice: its entries, total and terms. None for a malformed month."""
    try:
        label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return None
    rows = [r for r in entries(bp, now) if r["month"] == month and not r["upcoming"]]
    hours = sum((r["hours"] for r in rows), ZERO)
    sched = bp.invoice_schedule.get(month, {})
    amount = hours * bp.rate_usd
    cap = Decimal(str(sched["cap"])) if "cap" in sched else None
    return {"bp": bp, "month": month, "label": label, "rows": rows, "hours": hours, "amount": amount,
            "due": sched.get("due"), "cap": cap, "over_cap": cap is not None and amount > cap,
            "issued": timezone.localdate()}


def active_projects():
    return BillingProject.objects.filter(active=True)
