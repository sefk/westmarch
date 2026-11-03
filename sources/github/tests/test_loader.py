"""GitHub loader against a fake client serving small canned payloads."""

import pytest

from core.models import LoaderState
from sources.github.client import NotAccessible
from sources.github.loader import GitHubLoader
from sources.github.models import Commit, Issue, PullRequest, Repo

pytestmark = pytest.mark.django_db


def repo(full, fork=False, archived=False):
    owner, name = full.split("/")
    return {"full_name": full, "name": name, "owner": {"login": owner}, "private": False,
            "fork": fork, "archived": archived, "default_branch": "main", "pushed_at": "2026-01-02T00:00:00Z"}


def commit(sha, date, login="sefk"):
    who = {"name": "Sef", "email": "s@x", "date": date}
    return {"sha": sha, "author": {"login": login}, "commit": {"author": who, "committer": who, "message": "m " + sha}}


def issue(n, updated, pr=False, user="sefk"):
    d = {"number": n, "title": f"t{n}", "state": "open", "user": {"login": user}, "created_at": "2026-01-01T00:00:00Z",
         "updated_at": updated, "closed_at": None, "labels": [{"name": "bug"}], "comments": 2}
    if pr:
        d["pull_request"] = {}
    return d


def pull(n, updated, user="sefk"):
    return {"number": n, "title": f"p{n}", "state": "closed", "user": {"login": user}, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": updated, "closed_at": updated, "merged_at": updated, "draft": False, "additions": 5,
            "deletions": 2, "changed_files": 1, "head": {"ref": "f"}, "base": {"ref": "main"}, "labels": [], "comments": 0}


class Fake:
    """Serves canned data by path; records calls and params."""

    calls = 0
    remaining = 4000

    def __init__(self, data):
        self.data, self.log = data, []

    def lookup(self, path, params):
        self.calls += 1
        self.log.append((path, params))
        v = self.data[path]
        if isinstance(v, int):
            raise NotAccessible(v)
        return v

    def get(self, path, **params):
        return self.lookup(path, params)

    def pages(self, path, **params):
        yield from self.lookup(path, params)


def fake():
    return Fake({
        "/user": {"login": "sefk"},
        "/user/repos": [repo("sefk/a"), repo("sefk/forked", fork=True), repo("sefk/old", archived=True),
                        repo("sefk/empty")],
        "/search/issues": [{"repository_url": "https://api.github.com/repos/biglocalnews/datatalk"}],
        "/search/commits": [{"repository": {"full_name": "biglocalnews/product"}}],
        "/repos/biglocalnews/datatalk": repo("biglocalnews/datatalk"),
        "/repos/biglocalnews/product": repo("biglocalnews/product"),
        "/repos/sefk/a/commits": [commit("a1", "2026-01-02T10:00:00Z"), commit("a2", "2026-01-03T10:00:00Z")],
        "/repos/sefk/a/issues": [issue(1, "2026-01-02T00:00:00Z"), issue(2, "2026-01-03T00:00:00Z", pr=True)],
        "/repos/sefk/a/pulls": [pull(2, "2026-01-03T00:00:00Z")],
        "/repos/sefk/a/pulls/2": pull(2, "2026-01-03T00:00:00Z"),
        "/repos/sefk/empty/commits": 409,
        "/repos/sefk/empty/issues": [], "/repos/sefk/empty/pulls": [],
        "/repos/biglocalnews/datatalk/commits": [commit("d1", "2026-01-04T10:00:00Z")],
        "/repos/biglocalnews/datatalk/issues": [],
        "/repos/biglocalnews/datatalk/pulls": [pull(7, "2026-01-04T00:00:00Z"), pull(8, "2026-01-04T00:00:00Z", user="other")],
        "/repos/biglocalnews/datatalk/pulls/7": pull(7, "2026-01-04T00:00:00Z"),
        "/repos/biglocalnews/product/commits": 404,
        "/repos/biglocalnews/product/issues": [], "/repos/biglocalnews/product/pulls": [],
    })


def run(client, full=False):
    loader = GitHubLoader()
    loader.client = client
    return loader, loader.load(full=full)


def test_scope_and_tables():
    loader, rows = run(fake())
    assert set(Repo.objects.values_list("full_name", flat=True)) == {
        "sefk/a", "sefk/empty", "biglocalnews/datatalk", "biglocalnews/product"}
    assert Commit.objects.count() == 3
    assert Issue.objects.count() == 1  # the PR in the issues feed is filtered out
    assert Issue.objects.get().labels == ["bug"]
    # org repos keep only my PRs
    assert sorted(PullRequest.objects.values_list("number", flat=True)) == [2, 7]
    pr = PullRequest.objects.get(number=2)
    assert (pr.additions, pr.head_ref, pr.merged_at is not None) == (5, "f", True)
    assert loader.skipped == ["biglocalnews/product: HTTP 404"]
    assert rows > 0


def test_idempotent_and_cursors():
    run(fake())
    run(fake())
    assert Commit.objects.count() == 3 and Issue.objects.count() == 1 and Repo.objects.count() == 4
    assert LoaderState.get("github", "sefk/a:commits").startswith("2026-01-03T10:00:00")


def test_incremental_uses_since_with_overlap_and_full_ignores():
    run(fake())
    c = fake()
    run(c)
    commits = [p for path, p in c.log if path == "/repos/sefk/a/commits"][0]
    assert commits["since"] == "2026-01-02T10:00:00Z"
    issues = [p for path, p in c.log if path == "/repos/sefk/a/issues"][0]
    assert issues["since"] == "2026-01-02T00:00:00Z"
    c = fake()
    run(c, full=True)
    assert "since" not in [p for path, p in c.log if path == "/repos/sefk/a/commits"][0]


def test_org_repos_filter_by_author():
    c = fake()
    run(c)
    assert [p for path, p in c.log if path == "/repos/biglocalnews/datatalk/commits"][0]["author"] == "sefk"
    assert [p for path, p in c.log if path == "/repos/biglocalnews/datatalk/issues"][0]["creator"] == "sefk"


def test_pulls_stop_at_cursor():
    run(fake())
    c = fake()
    c.data["/repos/sefk/a/pulls"] = [pull(3, "2026-01-05T00:00:00Z"), pull(2, "2025-12-01T00:00:00Z")]
    c.data["/repos/sefk/a/pulls/3"] = pull(3, "2026-01-05T00:00:00Z")
    run(c)
    assert PullRequest.objects.filter(repo__full_name="sefk/a").count() == 2
    assert "/repos/sefk/a/pulls/2" not in [p for p, _ in c.log]
