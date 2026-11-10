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


class Trip(models.Model):
    """A run of consecutive days away from home (gaps of a day or two
    without location data don't break it). Rebuilt by the geo loader."""

    start = models.DateField()
    end = models.DateField()
    days = models.IntegerField()
    label = models.CharField(max_length=200, blank=True)  # most-photographed city, else coordinates
    countries = models.JSONField(default=list)
    max_km = models.FloatField(default=0)  # furthest point from home

    class Meta:
        ordering = ["start"]

    def __str__(self):
        return f"{self.label} {self.start}..{self.end}"


class PlaceDay(models.Model):
    """Where I was on one local day: the weighted median of every located
    thing that day (photos, workout routes, Timeline visits, Overland
    points). Rebuilt by the geo loader."""

    day = models.DateField(unique=True)
    lat = models.FloatField()
    lon = models.FloatField()
    city = models.CharField(max_length=100, blank=True)  # from photo places; blank if none that day
    country = models.CharField(max_length=10, blank=True)
    home_km = models.FloatField()  # primary position's distance from home
    furthest_km = models.FloatField()  # furthest located point from home
    away = models.BooleanField(default=False)
    sources = models.JSONField(default=dict)  # {"photos": 12, "routes": 1, ...}
    trip = models.ForeignKey(Trip, null=True, blank=True, on_delete=models.SET_NULL, related_name="place_days")

    class Meta:
        ordering = ["day"]

    def __str__(self):
        return f"{self.day} {self.city or f'{self.lat:.2f},{self.lon:.2f}'}"
