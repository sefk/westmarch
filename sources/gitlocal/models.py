from django.db import models

from core.models import Project


class Commit(models.Model):
    """One non-merge commit from a local checkout (any branch)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="commits")
    sha = models.CharField(max_length=40)
    author_name = models.CharField(max_length=200)
    author_email = models.CharField(max_length=200)
    authored_at = models.DateTimeField(db_index=True)
    committed_at = models.DateTimeField()
    subject = models.TextField()
    insertions = models.IntegerField(default=0)
    deletions = models.IntegerField(default=0)
    files_changed = models.IntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "sha"], name="uniq_commit")]

    def __str__(self):
        return f"{self.sha[:8]} {self.subject[:60]}"
