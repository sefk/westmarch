"""Local git: the project registry (`projects`) and commit history (`gitlocal`).

ProjectsLoader is the one writer of core.Project/ProjectAlias. It merges the
~/src/AGENTS.md registry, a depth-2 scan for git checkouts, and
local-redirector share names. GitLocalLoader then reads `git log` for every
active project.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings

from core.loaders import Loader
from core.models import Project, ProjectAlias

from .models import Commit

SKIP_DIRS = {".claude", ".worktrees", "node_modules", ".venv", ".git"}
# Redirector shares whose name doesn't match a slug or registry alias.
SHARE_HINTS = {
    "solar": "homepower", "solar-dash": "homepower", "poker": "wrgpt-buddy",
    "pilfer": "pilfer-game", "halloween": "halloween-stats",
    "datatalk-dev": "datatalk", "sefk-dev": "sefk.github.io", "peninsula": "peninsula",
}


def git(path: str, *args: str) -> str:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          check=True).stdout


def parse_github(url: str) -> str:
    """owner/name for a GitHub remote URL (https or ssh), else ''."""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{m[1]}/{m[2]}" if m else ""


def origin_repo(path: str) -> str:
    try:
        return parse_github(git(path, "remote", "get-url", "origin"))
    except (subprocess.CalledProcessError, OSError):
        return ""


def parse_registry(agents_md: Path) -> list[dict]:
    """`## name` sections with `key = value` lines directly beneath."""
    out, cur = [], None
    for line in agents_md.read_text().splitlines():
        if line.startswith("## "):
            cur = {"name": line[3:].strip()}
            out.append(cur)
        elif cur is not None and (m := re.match(r"^(\w+)\s*=\s*(.*)$", line)):
            cur[m[1]] = m[2].strip()
    return [e for e in out if "path" in e]


def scan_checkouts(root: Path) -> list[str]:
    """Git checkouts at depth 1-2 under root; worktrees (.git file) and
    junk directories are skipped, and we don't descend into checkouts."""
    found = []

    def skip(p: Path) -> bool:
        return p.name in SKIP_DIRS or p.name.endswith("-worktrees") or not p.is_dir()

    for d1 in sorted(root.iterdir()) if root.is_dir() else []:
        if skip(d1):
            continue
        if (d1 / ".git").exists():
            if (d1 / ".git").is_dir():
                found.append(str(d1))
            continue
        for d2 in sorted(d1.iterdir()):
            if not skip(d2) and (d2 / ".git").is_dir():
                found.append(str(d2))
    return found


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")


class ProjectsLoader(Loader):
    name = "projects"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        src = Path(settings.SRC_DIR)
        entries: dict[str, dict] = {}  # slug -> fields
        by_path: dict[str, str] = {}  # normalized path -> slug
        registry = src / "AGENTS.md"
        if registry.exists():
            for e in parse_registry(registry):
                path = os.path.normpath(os.path.expanduser(e["path"]))
                aliases = [a.strip() for a in e.get("aliases", "").split(",") if a.strip()]
                if e.get("workspace"):
                    aliases.append(e["workspace"])
                entries[e["name"]] = {"path": path, "repo": e.get("repo", ""),
                                      "aliases": aliases}
                by_path[path] = e["name"]
        for path in scan_checkouts(src):
            path = os.path.normpath(path)
            slug = by_path.get(path)
            if slug is None:
                slug = slugify(Path(path).name)
                if slug in entries:  # name clash with a different checkout
                    slug = slugify(Path(path).parent.name + "-" + Path(path).name)
                entries[slug] = {"path": path, "repo": "", "aliases": []}
                by_path[path] = slug
            if not entries[slug]["repo"]:
                entries[slug]["repo"] = origin_repo(path)

        rows = 0
        for slug, e in entries.items():
            exists = os.path.isdir(os.path.join(e["path"], ".git"))
            proj, created = Project.objects.get_or_create(
                slug=slug, defaults={"name": slug, "path": e["path"], "repo": e["repo"],
                                     "active": exists})
            proj.path, proj.repo, proj.active = e["path"], e["repo"], exists
            proj.save()  # tab is never touched; name stays as a human left it
            rows += 1
            self._alias(proj, "path", e["path"])
            for a in e["aliases"]:
                self._alias(proj, "registry", a)
        # Checkouts that vanished from both registry and disk.
        Project.objects.exclude(path="").exclude(slug__in=entries).update(active=False)
        self._redirector()
        return rows

    @staticmethod
    def _alias(proj: Project, source: str, name: str) -> None:
        ProjectAlias.objects.update_or_create(source=source, name=name,
                                              defaults={"project": proj})

    def _redirector(self) -> None:
        shares = Path(settings.REDIRECTOR_SHARES)
        if not shares.exists():
            return
        names = tomllib.loads(shares.read_text()).get("shares", {})
        unmapped = []
        for share in names:
            proj = (Project.objects.filter(slug=share).first()
                    or Project.objects.filter(
                        aliases__source="registry", aliases__name=share).first()
                    or Project.objects.filter(slug=SHARE_HINTS.get(share, "")).first())
            if proj:
                self._alias(proj, "redirector", share)
            else:
                unmapped.append(share)
        self.state_set("unmapped_shares", ",".join(unmapped))


SEP = "\x1e"  # record separator before each commit; fields split on \x1f
FORMAT = SEP + "%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%s"
STAT = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")


def parse_log(out: str) -> list[dict]:
    """Each record is a header line, then (if files changed) a blank line and
    a shortstat line. Commits with no file changes have no stat line."""
    commits = []
    for rec in out.split(SEP)[1:]:
        head, _, rest = rec.partition("\n")
        sha, an, ae, ad, cd, subject = head.split("\x1f", 5)
        m = STAT.search(rest)
        commits.append(dict(
            sha=sha, author_name=an, author_email=ae,
            authored_at=datetime.fromisoformat(ad), committed_at=datetime.fromisoformat(cd),
            subject=subject, files_changed=int(m[1]) if m else 0,
            insertions=int(m[2] or 0) if m else 0, deletions=int(m[3] or 0) if m else 0))
    return commits


class GitLocalLoader(Loader):
    name = "gitlocal"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        total = 0
        for proj in Project.objects.filter(active=True).exclude(path=""):
            if os.path.isdir(os.path.join(proj.path, ".git")):
                total += self._load_project(proj, full)
        return total

    def _load_project(self, proj: Project, full: bool) -> int:
        key = f"cursor:{proj.slug}"
        cmd = ["log", "--all", "--no-merges", f"--format={FORMAT}", "--shortstat"]
        cursor = None if full else self.state_get(key)
        if cursor:
            since = datetime.fromisoformat(cursor) - timedelta(days=2)
            cmd.append(f"--since={since.isoformat()}")
        try:
            commits = parse_log(git(proj.path, *cmd))
        except subprocess.CalledProcessError:  # e.g. a repo with no commits yet
            return 0
        if not commits:
            return 0
        shas = [c["sha"] for c in commits]
        have = set()
        for i in range(0, len(shas), 500):  # stay under SQLite's variable limit
            have |= set(Commit.objects.filter(project=proj, sha__in=shas[i:i + 500])
                        .values_list("sha", flat=True))
        new = [Commit(project=proj, **c) for c in commits if c["sha"] not in have]
        Commit.objects.bulk_create(new, ignore_conflicts=True, batch_size=500)
        latest = max(c["authored_at"] for c in commits)
        if cursor:
            latest = max(latest, datetime.fromisoformat(cursor))
        self.state_set(key, latest.isoformat())
        return len(new)
