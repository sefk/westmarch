"""Shared tables: the project registry and the loader run log.

Raw per-source tables live in their own apps under `sources/`. Nothing in
core knows about any particular source.
"""

from django.db import models


class Project(models.Model):
    """A thing I work on. Phase 1: a git checkout under ~/src.

    `slug` is the canonical key other tables and the derive step map to.
    The projects loader fills this from the filesystem and
    local-redirector; hand edits (tab assignments) survive reloads
    because the loader only updates the fields it owns.
    """

    class Tab(models.TextChoices):
        WORK = "work"
        TECH = "tech"
        PROJECTS = "projects"
        HEALTH = "health"
        SOCIAL = "social"
        FUN = "fun"
        MONEY = "money"
        UNSORTED = "unsorted"

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)  # friendly name if known, else slug
    path = models.CharField(max_length=500, blank=True)  # checkout, if any
    repo = models.CharField(max_length=200, blank=True)  # GitHub owner/name, if any
    tab = models.CharField(max_length=20, choices=Tab.choices, default=Tab.UNSORTED)
    active = models.BooleanField(default=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class ProjectAlias(models.Model):
    """How a source names a project: agentsview says `leafletter_app`,
    GitHub says `sefk/leafletter-app`, herdr says a path."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="aliases")
    source = models.CharField(max_length=30)  # loader name
    name = models.CharField(max_length=300)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "name"], name="uniq_project_alias")
        ]

    def __str__(self):
        return f"{self.source}:{self.name} -> {self.project_id}"


class LoaderRun(models.Model):
    """One execution of one loader. Failures are rows, not exceptions."""

    loader = models.CharField(max_length=50, db_index=True)
    started = models.DateTimeField()
    finished = models.DateTimeField(null=True)
    ok = models.BooleanField(null=True)  # null while running
    rows = models.IntegerField(default=0)  # rows written or updated
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started"]

    def __str__(self):
        state = "running" if self.ok is None else ("ok" if self.ok else "FAILED")
        return f"{self.loader} {self.started:%Y-%m-%d %H:%M} {state}"


class LoaderState(models.Model):
    """Per-loader cursors for incremental runs (last id, last timestamp,
    an ETag). A loader owns its keys; `full=True` runs ignore them."""

    loader = models.CharField(max_length=50)
    key = models.CharField(max_length=100)
    value = models.TextField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["loader", "key"], name="uniq_loader_state")
        ]

    @classmethod
    def get(cls, loader: str, key: str, default: str | None = None) -> str | None:
        row = cls.objects.filter(loader=loader, key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, loader: str, key: str, value: str) -> None:
        cls.objects.update_or_create(loader=loader, key=key, defaults={"value": value})


class BillingProject(models.Model):
    """A project whose billable time is read off the primary calendar.

    An event counts when its title contains one of `tags` as a whole word,
    or it has an attendee at one of `attendee_domains`. `invoice_schedule`
    is {"YYYY-MM": {"cap": dollars, "due": "YYYY-MM-DD"}} from the contract.
    The catalog invoicing page does the matching; this is just the terms.
    """

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="billing")
    tags = models.JSONField(default=list)
    attendee_domains = models.JSONField(default=list)
    rate_usd = models.DecimalField(max_digits=8, decimal_places=2)
    hours_cap = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    amount_cap_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    term_start = models.DateField()
    term_end = models.DateField()
    increment_minutes = models.PositiveSmallIntegerField(default=15)  # each entry rounds to nearest
    invoice_schedule = models.JSONField(default=dict)
    bill_from = models.TextField(blank=True)
    bill_to = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug
