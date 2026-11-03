"""herdr tabs: workspaces and tabs from session.json, its snapshots, and
the server log (the only source of ids and timestamps). Read-only.

Incremental: the log by byte offset (reset if the file shrank), snapshots
by the newest timestamp in their file names. session.json is replayed
every run, last, stamped with its mtime.
"""

import json
import re
from datetime import datetime, timezone as tz
from pathlib import Path

from django.conf import settings

from core.loaders import Disabled, Loader

from .models import Tab, TabEvent, Workspace

LOG_RE = re.compile(r'^(\S+Z)\s.*?event="(tab\.focus|tab\.rename|workspace\.create|workspace\.close)".*?'
                    r'workspace_id="([^"]+)"(?: tab_id="([^"]+)")?')
SNAP_RE = re.compile(r"^session-(\d+)-")


def _bump(obj, field, value, later):
    """Set a first-seen (later=False) or last-seen (later=True) style field."""
    cur = getattr(obj, field)
    if cur is None or (value > cur if later else value < cur):
        setattr(obj, field, value)


class HerdrLoader(Loader):
    name = "herdr"
    cadence = "hourly"  # snapshots are pruned after ~4 days; poll often or lose tab names

    def load(self, full: bool = False) -> int:
        root = Path(settings.HERDR_DIR).expanduser()
        if not (root / "session.json").exists():
            raise Disabled(f"no session.json under {root}")
        rows = self.load_log(root / "herdr-server.log", full)
        snaps = sorted(
            (int(m.group(1)), p)
            for p in (root / "session-snapshots").glob("session-*.json")
            if (m := SNAP_RE.match(p.name))
        )
        done = 0 if full else int(self.state_get("snapshot_ns", "0"))
        for ns, path in snaps:
            if ns > done:
                rows += self.apply_session(json.loads(path.read_text()), datetime.fromtimestamp(ns / 1e9, tz.utc))
                self.state_set("snapshot_ns", str(ns))
        cur = root / "session.json"
        mtime = datetime.fromtimestamp(cur.stat().st_mtime, tz.utc)
        return rows + self.apply_session(json.loads(cur.read_text()), mtime)

    # -- server log: exact times for tab focus/rename and workspace create/close
    def load_log(self, path: Path, full: bool) -> int:
        if not path.exists():
            return 0
        offset = 0 if full else int(self.state_get("log_offset", "0"))
        if path.stat().st_size < offset:  # rotated or truncated
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        data = data[: data.rfind(b"\n") + 1]  # never consume a half-written line
        wss: dict[str, Workspace] = {}
        tabs: dict[str, Tab] = {}
        events = []
        for line in data.decode(errors="replace").splitlines():
            m = LOG_RE.match(line)
            if not m:
                continue
            ts, event, wid, tid = m.groups()
            at = datetime.fromisoformat(ts)
            ws = wss.get(wid) or wss.setdefault(wid, Workspace.objects.get_or_create(herdr_id=wid)[0])
            _bump(ws, "first_seen", at, False)
            _bump(ws, "last_seen", at, True)
            if event == "workspace.create":
                ws.created_at = at
            elif event == "workspace.close":
                ws.closed_at = at
            else:
                tab = tabs.get(tid) or tabs.setdefault(
                    tid, Tab.objects.get_or_create(herdr_id=tid, defaults={"workspace": ws})[0])
                _bump(tab, "first_seen_at", at, False)
                _bump(tab, "last_seen_at", at, True)
                events.append((tab, event.split(".")[1], at))
        for ws in wss.values():
            ws.save()
        for tab in tabs.values():
            tab.save()
        before = TabEvent.objects.count()
        TabEvent.objects.bulk_create([TabEvent(tab=t, kind=k, at=a) for t, k, a in events], ignore_conflicts=True)
        self.state_set("log_offset", str(offset + len(data)))
        return len(wss) + len(tabs) + TabEvent.objects.count() - before

    # -- a session.json-shaped state at time `at`
    def apply_session(self, state: dict, at: datetime) -> int:
        rows = 0
        seen_ws, seen_tabs = set(), set()
        for w in state.get("workspaces", []):
            ws, _ = Workspace.objects.get_or_create(herdr_id=w["id"])
            ws.name, ws.cwd = w.get("custom_name") or "", w.get("identity_cwd", "")
            _bump(ws, "first_seen", at, False)
            _bump(ws, "last_seen", at, True)
            ws.save()
            seen_ws.add(ws.pk)
            nums = w.get("public_tab_numbers", [])
            for i, t in enumerate(w.get("tabs", [])):
                if i >= len(nums):
                    continue
                tab, _ = Tab.objects.get_or_create(herdr_id=f"{w['id']}:t{nums[i]}", defaults={"workspace": ws})
                panes = t.get("panes", {})
                root = panes.get(str(t.get("root_pane"))) or next(iter(panes.values()), {})
                agents = [p for p in panes.values() if p.get("managed_agent_kind") or p.get("agent_session")]
                a = agents[0] if agents else {}
                tab.title = t.get("custom_name") or ""
                tab.cwd = root.get("cwd", "")
                tab.agent = a.get("managed_agent_kind") or a.get("agent_session", {}).get("agent") or "shell"
                tab.agent_session_id = a.get("agent_session", {}).get("value", "")
                tab.pane_count = len(panes)
                tab.raw = {k: v for k, v in t.items() if k != "layout"}
                tab.closed_at = None
                _bump(tab, "first_seen_at", at, False)
                _bump(tab, "last_seen_at", at, True)
                tab.save()
                seen_tabs.add(tab.pk)
                rows += 1
        # Anything we knew about before `at` that this state lacks was closed by `at`.
        gone = Workspace.objects.filter(closed_at__isnull=True, first_seen__lt=at).exclude(pk__in=seen_ws)
        for ws in gone:
            ws.closed_at = at
            ws.save()
            rows += 1
        Tab.objects.filter(closed_at__isnull=True, first_seen_at__lt=at).exclude(pk__in=seen_tabs).filter(
            workspace__in=seen_ws).update(closed_at=at)
        # Tabs of closed workspaces close with them.
        for tab in Tab.objects.filter(closed_at__isnull=True, workspace__closed_at__isnull=False).select_related("workspace"):
            tab.closed_at = tab.workspace.closed_at
            tab.save()
        return rows
