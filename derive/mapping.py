"""Map each source's project naming onto core.Project.

Pure helpers, reusable by the catalog app: a filesystem-path resolver and
alias lookups. The derive loader uses them to write ProjectAlias rows with
source "agentsview", "github" and "herdr".
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from core.models import Project, ProjectAlias

SOURCES = ("agentsview", "github", "herdr")


def _norm(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path)) if path else ""


def project_for_path(path: str, projects: Iterable[Project]) -> Project | None:
    """The project a path belongs to, or None.

    1. Longest Project.path that is the path or an ancestor of it (so
       `.claude/worktrees/...` and subdirectories map to the checkout).
    2. Otherwise a sibling worktree: `<parent>/<basename>-<suffix>[/...]`
       next to a checkout at `<parent>/<basename>`; longest basename wins.
    Anything else (home, ~/src, /private/tmp, /sessions/...) is None.
    """
    path = _norm(path)
    if not path:
        return None
    best, best_len = None, -1
    for proj in projects:
        root = _norm(proj.path)
        if root and (path == root or path.startswith(root + os.sep)) and len(root) > best_len:
            best, best_len = proj, len(root)
    if best is not None:
        return best
    for proj in projects:
        root = _norm(proj.path)
        if not root:
            continue
        parent, base = os.path.split(root)
        if not path.startswith(parent + os.sep):
            continue
        first = path[len(parent) + 1:].split(os.sep, 1)[0]
        if first.startswith(base + "-") and len(first) > len(base) + 1 and len(base) > best_len:
            best, best_len = proj, len(base)
    return best


def project_for_name(name: str, projects: Iterable[Project]) -> Project | None:
    """Fallback for a bare name like `wrgpt_buddy`: match (with `_` -> `-`)
    a Project.slug, then any ProjectAlias name, then a Project.path basename."""
    if not name:
        return None
    projects = list(projects)
    cands = [name, name.replace("_", "-")]
    for c in cands:
        for p in projects:
            if p.slug == c:
                return p
    alias = (ProjectAlias.objects.filter(name__in=cands).select_related("project")
             .order_by("id").first())
    if alias:
        return alias.project
    for c in cands:
        for p in projects:
            if p.path and os.path.basename(_norm(p.path)) == c:
                return p
    return None


def project_for_repo(full_name: str, projects: Iterable[Project]) -> Project | None:
    """GitHub `owner/name` -> the project with that repo, case-insensitive."""
    key = full_name.lower()
    for p in projects:
        if p.repo and p.repo.lower() == key:
            return p
    return None


def alias_map(source: str) -> dict[str, int]:
    """{source name: project_id} for one source's aliases."""
    return dict(ProjectAlias.objects.filter(source=source).values_list("name", "project_id"))


def lookup(source: str, name: str) -> Project | None:
    alias = ProjectAlias.objects.filter(source=source, name=name).select_related("project").first()
    return alias.project if alias else None
