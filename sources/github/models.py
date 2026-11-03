"""Raw GitHub tables, Dogsheep style: a few typed columns for querying plus
the full API payload in `raw`. Issues and pull requests are separate tables."""

from django.db import models


class Repo(models.Model):
    full_name = models.CharField(max_length=200, unique=True)  # owner/name
    owner = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    private = models.BooleanField(default=False)
    default_branch = models.CharField(max_length=100, blank=True)
    pushed_at = models.DateTimeField(null=True)
    raw = models.JSONField(default=dict)

    def __str__(self):
        return self.full_name


class Commit(models.Model):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE, related_name="commits")
    sha = models.CharField(max_length=40, unique=True)
    author_login = models.CharField(max_length=100, null=True, blank=True)
    author_name = models.CharField(max_length=200, blank=True)
    author_email = models.CharField(max_length=200, blank=True)
    authored_at = models.DateTimeField(db_index=True)
    committed_at = models.DateTimeField(null=True)
    message = models.TextField(blank=True)
    # Per-commit stats cost one API call each; not fetched in phase 1.
    additions = models.IntegerField(null=True)
    deletions = models.IntegerField(null=True)
    raw = models.JSONField(default=dict)


class Issue(models.Model):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE, related_name="issues")
    number = models.IntegerField()
    title = models.TextField(blank=True)
    state = models.CharField(max_length=20)
    author_login = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(null=True)
    closed_at = models.DateTimeField(null=True)
    labels = models.JSONField(default=list)
    comments = models.IntegerField(default=0)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repo", "number"], name="uniq_gh_issue")]


class PullRequest(models.Model):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE, related_name="pulls")
    number = models.IntegerField()
    title = models.TextField(blank=True)
    state = models.CharField(max_length=20)
    author_login = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(null=True)
    closed_at = models.DateTimeField(null=True)
    merged_at = models.DateTimeField(null=True)
    draft = models.BooleanField(default=False)
    additions = models.IntegerField(null=True)
    deletions = models.IntegerField(null=True)
    changed_files = models.IntegerField(null=True)
    head_ref = models.CharField(max_length=300, blank=True)
    base_ref = models.CharField(max_length=300, blank=True)
    labels = models.JSONField(default=list)
    comments = models.IntegerField(default=0)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repo", "number"], name="uniq_gh_pr")]
