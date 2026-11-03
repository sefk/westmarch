"""Derived tables, rebuilt by the derive loader from the raw source tables.

ProjectDay is one row per project per local (America/Los_Angeles) day with
any activity; UnmappedDay holds the agentsview usage that no project
claimed, so totals still add up.
"""

from django.db import models

from core.models import Project


class ProjectDay(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="days")
    day = models.DateField(db_index=True)
    # agentsview. `sessions` excludes subagents; cost and tokens include
    # them, since a parent's usage excludes its subagents'.
    sessions = models.IntegerField(default=0)
    subagent_sessions = models.IntegerField(default=0)
    agent_seconds = models.BigIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cache_read_tokens = models.BigIntegerField(default=0)
    cache_write_tokens = models.BigIntegerField(default=0)
    cost_by_agent = models.JSONField(default=dict)  # {"claude": 12.3, "codex": 0.4}
    # gitlocal
    commits = models.IntegerField(default=0)
    insertions = models.IntegerField(default=0)
    deletions = models.IntegerField(default=0)
    # github
    gh_commits = models.IntegerField(default=0)
    gh_issues_opened = models.IntegerField(default=0)
    gh_issues_closed = models.IntegerField(default=0)
    gh_prs_opened = models.IntegerField(default=0)
    gh_prs_merged = models.IntegerField(default=0)
    # herdr
    herdr_focus_events = models.IntegerField(default=0)
    herdr_tabs = models.IntegerField(default=0)

    class Meta:
        ordering = ["day", "project"]
        constraints = [models.UniqueConstraint(fields=["project", "day"], name="uniq_project_day")]

    def __str__(self):
        return f"{self.project_id} {self.day}"


class UnmappedDay(models.Model):
    """agentsview usage whose project name maps to no Project."""

    day = models.DateField(unique=True)
    sessions = models.IntegerField(default=0)  # excludes subagents
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)

    class Meta:
        ordering = ["day"]

    def __str__(self):
        return f"unmapped {self.day}"
