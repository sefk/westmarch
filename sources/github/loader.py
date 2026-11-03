"""GitHub loader: repos, default-branch commits, issues and pull requests.

Scope: non-fork, non-archived repos owned by the user (public and private),
plus repos in the other GITHUB_OWNERS (orgs) that the user has authored
commits, issues or PRs in. In org repos we keep only the user's own
activity. Commits are default branch only (phase 1).

Incremental, per repo, via LoaderState: commits use `since` (last commit
date, 1-day overlap); issues use `since`/`sort=updated`; PRs have no
`since`, so we page newest-updated first and stop at the cursor.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils.dateparse import parse_datetime

from core.loaders import Loader

from .client import Client, NotAccessible, get_token
from .models import Commit, Issue, PullRequest, Repo

logger = logging.getLogger(__name__)
OVERLAP = timedelta(days=1)


def dt(value):
    return parse_datetime(value) if value else None


def login_of(obj):
    return (obj or {}).get("login")


class GitHubLoader(Loader):
    name = "github"
    cadence = "daily"
    client = None  # tests inject a fake
    skipped: list

    def load(self, full: bool = False) -> int:
        client = self.client or Client(get_token())
        self.skipped, rows = [], 0
        me = client.get("/user")["login"]
        repos = self.discover(client, me)
        for info in repos:
            repo = self.save_repo(info)
            mine_only = info["owner"]["login"].lower() != me.lower()
            try:
                rows += 1
                rows += self.load_commits(client, repo, me, mine_only, full)
                rows += self.load_issues(client, repo, me, mine_only, full)
                rows += self.load_pulls(client, repo, me, mine_only, full)
            except NotAccessible as exc:
                self.skipped.append(f"{repo.full_name}: HTTP {exc.status}")
        for s in self.skipped:
            logger.warning("github skipped %s", s)
        logger.info("github: %d repos, %d API calls, %s rate limit left",
                    len(repos), getattr(client, "calls", 0), getattr(client, "remaining", "?"))
        return rows

    # -- discovery ---------------------------------------------------------

    def discover(self, client, me):
        """Repo payloads in scope: own repos, then org repos I touched."""
        found = {}
        for r in client.pages("/user/repos", affiliation="owner", sort="pushed"):
            if r["owner"]["login"].lower() == me.lower() and not r["fork"] and not r["archived"]:
                found[r["full_name"]] = r
        orgs = [o for o in settings.GITHUB_OWNERS if o.lower() != me.lower()]
        for org in orgs:
            names = set()
            for kind in ("issues", "commits"):
                q = f"author:{me} org:{org}"
                try:
                    for item in client.pages(f"/search/{kind}", q=q):
                        names.add(item["repository"]["full_name"] if kind == "commits"
                                  else item["repository_url"].split("/repos/")[1])
                except NotAccessible:
                    self.skipped.append(f"search {kind} org:{org}")
            for full_name in sorted(names):
                if full_name in found:
                    continue
                try:
                    r = client.get(f"/repos/{full_name}")
                except NotAccessible as exc:
                    self.skipped.append(f"{full_name}: HTTP {exc.status}")
                    continue
                if not r["fork"] and not r["archived"]:
                    found[full_name] = r
                else:
                    self.skipped.append(f"{full_name}: fork/archived")
        return list(found.values())

    def save_repo(self, r):
        repo, _ = Repo.objects.update_or_create(full_name=r["full_name"], defaults=dict(
            owner=r["owner"]["login"], name=r["name"], private=r["private"],
            default_branch=r.get("default_branch") or "", pushed_at=dt(r.get("pushed_at")), raw=r))
        return repo

    # -- cursors -----------------------------------------------------------

    def since(self, repo, key, full, overlap=True):
        value = None if full else self.state_get(f"{repo.full_name}:{key}")
        if not value:
            return None
        ts = dt(value)
        return (ts - OVERLAP if overlap else ts).isoformat().replace("+00:00", "Z")

    # -- commits -----------------------------------------------------------

    def load_commits(self, client, repo, me, mine_only, full):
        params = {}
        if (s := self.since(repo, "commits", full)):
            params["since"] = s
        if mine_only:
            params["author"] = me
        newest, n = None, 0
        try:
            for c in client.pages(f"/repos/{repo.full_name}/commits", **params):
                git = c["commit"]
                authored = dt(git["author"]["date"])
                committed = dt(git["committer"]["date"])
                Commit.objects.update_or_create(sha=c["sha"], defaults=dict(
                    repo=repo, author_login=login_of(c.get("author")),
                    author_name=git["author"].get("name", ""), author_email=git["author"].get("email", ""),
                    authored_at=authored, committed_at=committed, message=git.get("message", ""), raw=c))
                newest = max(newest or committed, committed)
                n += 1
        except NotAccessible as exc:
            if exc.status != 409:  # 409 = empty repository
                raise
        if newest:
            self.state_set(f"{repo.full_name}:commits", newest.isoformat())
        return n

    # -- issues ------------------------------------------------------------

    def load_issues(self, client, repo, me, mine_only, full):
        params = dict(state="all", sort="updated", direction="asc")
        if (s := self.since(repo, "issues", full)):
            params["since"] = s
        if mine_only:
            params["creator"] = me
        newest, n = None, 0
        for i in client.pages(f"/repos/{repo.full_name}/issues", **params):
            updated = dt(i["updated_at"])
            newest = max(newest or updated, updated)
            if "pull_request" in i:  # the issues endpoint also lists PRs
                continue
            Issue.objects.update_or_create(repo=repo, number=i["number"], defaults=dict(
                title=i["title"], state=i["state"], author_login=login_of(i.get("user")),
                created_at=dt(i["created_at"]), updated_at=updated, closed_at=dt(i.get("closed_at")),
                labels=[lb["name"] for lb in i.get("labels", [])], comments=i.get("comments", 0), raw=i))
            n += 1
        if newest:
            self.state_set(f"{repo.full_name}:issues", newest.isoformat())
        return n

    # -- pull requests -----------------------------------------------------

    def load_pulls(self, client, repo, me, mine_only, full):
        cursor = None if full else dt(self.state_get(f"{repo.full_name}:pulls"))
        newest, n = None, 0
        # Newest-updated first; stop once we reach PRs older than the cursor.
        for p in client.pages(f"/repos/{repo.full_name}/pulls", state="all", sort="updated", direction="desc"):
            updated = dt(p["updated_at"])
            if cursor and updated < cursor - OVERLAP:
                break
            newest = max(newest or updated, updated)
            if mine_only and login_of(p.get("user")) != me:
                continue
            # The list payload lacks additions/deletions/changed_files; one call each.
            d = client.get(f"/repos/{repo.full_name}/pulls/{p['number']}")
            PullRequest.objects.update_or_create(repo=repo, number=d["number"], defaults=dict(
                title=d["title"], state=d["state"], author_login=login_of(d.get("user")),
                created_at=dt(d["created_at"]), updated_at=updated, closed_at=dt(d.get("closed_at")),
                merged_at=dt(d.get("merged_at")), draft=bool(d.get("draft")),
                additions=d.get("additions"), deletions=d.get("deletions"), changed_files=d.get("changed_files"),
                head_ref=d["head"]["ref"], base_ref=d["base"]["ref"],
                labels=[lb["name"] for lb in d.get("labels", [])],
                comments=d.get("comments", 0), raw=d))
            n += 1
        if newest:
            self.state_set(f"{repo.full_name}:pulls", newest.isoformat())
        return n
