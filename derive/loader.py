"""The derive loader: map source naming onto core.Project, then roll up
per-project daily totals into ProjectDay (and UnmappedDay).

Runs last in settings.LOADERS. Aliases are only ever created, never
changed, so a hand edit to a ProjectAlias wins. The rollup is
delete-and-rebuild over a day range: everything with `full`, otherwise
from three days before the last run (agentsview rewrites recent sessions).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from core.loaders import Loader
from core.models import Project, ProjectAlias
from sources.agentsview.models import Session
from sources.github.models import Commit as GhCommit
from sources.github.models import Issue, PullRequest, Repo
from sources.gitlocal.models import Commit
from sources.herdr.models import Tab, TabEvent

from . import mapping
from .models import ProjectDay, UnmappedDay

LOOKBACK_DAYS = 3
COUNTERS = ("sessions", "subagent_sessions", "agent_seconds", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "commits", "insertions", "deletions",
            "gh_commits", "gh_issues_opened", "gh_issues_closed", "gh_prs_opened",
            "gh_prs_merged", "herdr_focus_events", "herdr_tabs")


def local_day(dt: datetime) -> date:
    return timezone.localtime(dt).date()


def day_start(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


class DeriveLoader(Loader):
    name = "derive"
    cadence = "hourly"

    def load(self, full: bool = False) -> int:
        projects = list(Project.objects.all())
        rows = self.map_agentsview(projects) + self.map_github(projects) + self.map_herdr(projects)
        start = None
        last = None if full else self.state_get("last_run")
        if last:
            start = date.fromisoformat(last) - timedelta(days=LOOKBACK_DAYS)
        rows += self.rollup(start)
        self.state_set("last_run", timezone.localdate().isoformat())
        return rows

    # -- mapping -----------------------------------------------------------

    def _write(self, source: str, resolved: dict[str, Project | None]) -> int:
        """Create missing aliases; record names that resolve nowhere."""
        have = set(ProjectAlias.objects.filter(source=source).values_list("name", flat=True))
        new = [ProjectAlias(project=p, source=source, name=n)
               for n, p in resolved.items() if p is not None and n not in have]
        ProjectAlias.objects.bulk_create(new, ignore_conflicts=True)
        unmapped = sorted(n for n, p in resolved.items() if p is None and n not in have)
        self.state_set(f"unmapped:{source}", "\n".join(unmapped))
        return len(new)

    def map_agentsview(self, projects: list[Project]) -> int:
        cwds: dict[str, Counter] = defaultdict(Counter)
        for row in (Session.objects.exclude(project="").values("project", "cwd")
                    .annotate(n=Count("id"))):
            cwds[row["project"]][row["cwd"]] += row["n"]
        resolved = {}
        for name, counter in cwds.items():
            counter.pop("", None)
            proj = None
            if counter:
                proj = mapping.project_for_path(counter.most_common(1)[0][0], projects)
            resolved[name] = proj or mapping.project_for_name(name, projects)
        return self._write("agentsview", resolved)

    def map_github(self, projects: list[Project]) -> int:
        return self._write("github", {
            fn: mapping.project_for_repo(fn, projects)
            for fn in Repo.objects.values_list("full_name", flat=True)})

    def map_herdr(self, projects: list[Project]) -> int:
        return self._write("herdr", {
            cwd: mapping.project_for_path(cwd, projects)
            for cwd in Tab.objects.exclude(cwd="").values_list("cwd", flat=True).distinct()})

    # -- rollup ------------------------------------------------------------

    def rollup(self, start: date | None) -> int:
        """Rebuild ProjectDay/UnmappedDay for days >= start (all if None)."""
        since = day_start(start) if start else None

        def window(qs, field):
            return qs.filter(**{f"{field}__gte": since}) if since else qs

        days: dict[tuple[int, date], dict] = {}

        def bucket(pid: int, d: date) -> dict:
            if (pid, d) not in days:
                days[(pid, d)] = {**dict.fromkeys(COUNTERS, 0), "cost_usd": Decimal(0),
                                  "cost_by_agent": defaultdict(Decimal)}
            return days[(pid, d)]

        unmapped: dict[date, dict] = defaultdict(lambda: {"sessions": 0, "cost_usd": Decimal(0)})

        # agentsview
        amap = mapping.alias_map("agentsview")
        for s in window(Session.objects.exclude(started_at=None), "started_at").values(
                "project", "agent", "relationship_type", "started_at", "duration_seconds",
                "estimated_cost_usd", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens"):
            d = local_day(s["started_at"])
            sub = s["relationship_type"] == "subagent"
            cost = s["estimated_cost_usd"] or Decimal(0)
            pid = amap.get(s["project"])
            if pid is None:
                u = unmapped[d]
                u["sessions"] += 0 if sub else 1
                u["cost_usd"] += cost
                continue
            b = bucket(pid, d)
            b["subagent_sessions" if sub else "sessions"] += 1
            b["agent_seconds"] += s["duration_seconds"] or 0
            b["cost_usd"] += cost
            b["cost_by_agent"][s["agent"]] += cost
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                b[k] += s[k]

        # gitlocal
        for c in window(Commit.objects, "authored_at").values(
                "project_id", "authored_at", "insertions", "deletions"):
            b = bucket(c["project_id"], local_day(c["authored_at"]))
            b["commits"] += 1
            b["insertions"] += c["insertions"]
            b["deletions"] += c["deletions"]

        # github, via the github alias of each repo
        gmap = mapping.alias_map("github")
        repo_pid = {rid: gmap[fn] for rid, fn in Repo.objects.values_list("id", "full_name")
                    if fn in gmap}
        for model, field, key in ((GhCommit, "authored_at", "gh_commits"),
                                  (Issue, "created_at", "gh_issues_opened"),
                                  (Issue, "closed_at", "gh_issues_closed"),
                                  (PullRequest, "created_at", "gh_prs_opened"),
                                  (PullRequest, "merged_at", "gh_prs_merged")):
            qs = model.objects.filter(repo_id__in=repo_pid).exclude(**{field: None})
            for rid, at in window(qs, field).values_list("repo_id", field):
                bucket(repo_pid[rid], local_day(at))[key] += 1

        # herdr, via the herdr alias of each tab's cwd
        hmap = mapping.alias_map("herdr")
        tab_pid = {tid: hmap[cwd] for tid, cwd in Tab.objects.values_list("id", "cwd")
                   if cwd in hmap}
        events = TabEvent.objects.filter(kind=TabEvent.Kind.FOCUS, tab_id__in=tab_pid)
        for tid, at in window(events, "at").values_list("tab_id", "at"):
            bucket(tab_pid[tid], local_day(at))["herdr_focus_events"] += 1
        tabs = Tab.objects.filter(id__in=tab_pid).exclude(first_seen_at=None)
        for tid, first, last in tabs.values_list("id", "first_seen_at", "last_seen_at"):
            lo, hi = local_day(first), local_day(last or first)
            if start:
                lo = max(lo, start)
            while lo <= hi:
                bucket(tab_pid[tid], lo)["herdr_tabs"] += 1
                lo += timedelta(days=1)

        new = []
        for (pid, d), b in days.items():
            b["cost_by_agent"] = {a: round(float(v), 4)
                                  for a, v in sorted(b["cost_by_agent"].items())}
            new.append(ProjectDay(project_id=pid, day=d, **b))
        new_unmapped = [UnmappedDay(day=d, **u) for d, u in unmapped.items()]
        with transaction.atomic():
            old = ProjectDay.objects.all()
            old_u = UnmappedDay.objects.all()
            if start:
                old, old_u = old.filter(day__gte=start), old_u.filter(day__gte=start)
            old.delete()
            old_u.delete()
            ProjectDay.objects.bulk_create(new, batch_size=500)
            UnmappedDay.objects.bulk_create(new_unmapped, batch_size=500)
        return len(new) + len(new_unmapped)
