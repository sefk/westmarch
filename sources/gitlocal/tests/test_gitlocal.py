import os
import subprocess

import pytest

from core.models import LoaderState, Project, ProjectAlias
from sources.gitlocal.loader import GitLocalLoader, ProjectsLoader, parse_github
from sources.gitlocal.models import Commit

pytestmark = pytest.mark.django_db


def commit(repo, name, date, content=None, msg=None):
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_AUTHOR_NAME": "Sef", "GIT_AUTHOR_EMAIL": "s@x.org",
           "GIT_COMMITTER_NAME": "Sef", "GIT_COMMITTER_EMAIL": "s@x.org"}
    if content is not None:
        (repo / name).write_text(content)
        subprocess.run(["git", "-C", repo, "add", name], check=True, env=env)
    subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", msg or name],
                   check=True, env=env)


@pytest.fixture
def world(tmp_path, settings):
    src = tmp_path / "src"
    alpha = src / "alpha"
    alpha.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", alpha], check=True)
    subprocess.run(["git", "-C", alpha, "remote", "add", "origin",
                    "git@github.com:me/alpha-repo.git"], check=True)
    commit(alpha, "a.txt", "2024-01-01T10:00:00", "1\n2\n3\n")
    commit(alpha, "a.txt", "2024-01-02T10:00:00", "1\n2\n")
    commit(alpha, "b.txt", "2024-01-03T10:00:00", None, "empty one")
    # a scanned-only repo, a worktree-style .git file, and junk dirs
    (src / "Beta_Thing").mkdir()
    subprocess.run(["git", "init", "-q", src / "Beta_Thing"], check=True)
    (src / "wt").mkdir()
    (src / "wt" / ".git").write_text("gitdir: /nowhere")
    (src / "x-worktrees" / "y").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", src / "x-worktrees" / "y"], check=True)
    (src / "AGENTS.md").write_text(
        f"## alpha\n\npath = {alpha}\nrepo = me/alpha\nworkspace = alp\naliases = a, al\n\n"
        "## gone\npath = /nonexistent/gone\n")
    (src / "local-redirector").mkdir()
    (src / "local-redirector" / "shares.toml").write_text(
        '[shares.al]\nport = 1\n[shares.beta-thing]\nport = 2\n'
        '[shares.solar]\nport = 3\n[shares.mystery]\nport = 4\n')
    settings.SRC_DIR = src
    settings.REDIRECTOR_SHARES = src / "local-redirector" / "shares.toml"
    return alpha


def alias(source, name):
    return ProjectAlias.objects.get(source=source, name=name).project.slug


def test_parse_github():
    assert parse_github("https://github.com/o/n.git") == "o/n"
    assert parse_github("git@github.com:o/n.git") == "o/n"
    assert parse_github("https://gitlab.com/o/n") == ""


def test_projects(world):
    ProjectsLoader().load()
    slugs = set(Project.objects.values_list("slug", flat=True))
    assert slugs == {"alpha", "gone", "beta_thing".replace("_", "-")}  # no wt, no x-worktrees
    alpha = Project.objects.get(slug="alpha")
    assert alpha.repo == "me/alpha" and alpha.active  # registry wins over origin
    assert not Project.objects.get(slug="gone").active
    assert alias("registry", "alp") == "alpha" and alias("registry", "a") == "alpha"
    assert alias("path", str(world)) == "alpha"
    assert alias("redirector", "al") == "alpha"
    assert alias("redirector", "beta-thing") == "beta-thing"
    assert not ProjectAlias.objects.filter(source="redirector", name="solar").exists()
    assert LoaderState.get("projects", "unmapped_shares") == "solar,mystery"


def test_tab_survives(world):
    ProjectsLoader().load()
    Project.objects.filter(slug="alpha").update(tab="tech")
    ProjectsLoader().load()
    assert Project.objects.get(slug="alpha").tab == "tech"


def test_commits_and_incremental(world):
    ProjectsLoader().load()
    loader = GitLocalLoader()
    assert loader.load() == 3
    c = Commit.objects.get(project__slug="alpha", subject="a.txt", insertions=3)
    assert c.files_changed == 1 and c.deletions == 0
    c2 = Commit.objects.get(subject="empty one")
    assert (c2.files_changed, c2.insertions, c2.deletions) == (0, 0, 0)
    assert Commit.objects.filter(insertions=0, deletions=1).count() == 1
    assert loader.load() == 0  # overlap re-reads but adds nothing
    commit(world, "c.txt", "2024-01-04T10:00:00", "x\n")
    assert loader.load() == 1
    assert loader.load(full=True) == 0
    assert Commit.objects.count() == 4
