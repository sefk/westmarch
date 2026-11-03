"""Raw herdr tables.

herdr keeps no tab history of its own: tabs have no ids or timestamps in
session.json. Ids ("wEN:t2" = workspace wEN, tab number 2) and times only
exist in herdr-server.log, and names/cwds/agents only in session.json and
its rolling snapshots. So every time here is "first/last evidence", and
`closed_at` is an upper bound (the first snapshot that no longer had it),
unless the log said so exactly (workspace.close).
"""

from django.db import models


class Workspace(models.Model):
    herdr_id = models.CharField(max_length=20, unique=True)  # "wEN"
    name = models.CharField(max_length=200, blank=True)  # custom_name; "" if never seen in a snapshot
    cwd = models.CharField(max_length=500, blank=True)  # identity_cwd
    created_at = models.DateTimeField(null=True)  # exact, from the log; null if older than the log
    closed_at = models.DateTimeField(null=True)
    first_seen = models.DateTimeField(null=True)
    last_seen = models.DateTimeField(null=True)

    def __str__(self):
        return f"{self.herdr_id} {self.name}"


class Tab(models.Model):
    herdr_id = models.CharField(max_length=30, unique=True)  # "wEN:t2"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tabs")
    title = models.CharField(max_length=200, blank=True)  # custom_name
    cwd = models.CharField(max_length=500, blank=True)  # of the root pane, as given
    agent = models.CharField(max_length=30, blank=True)  # claude / codex / ... or "shell"
    agent_session_id = models.CharField(max_length=100, blank=True)  # joins to agentsview sessions
    pane_count = models.IntegerField(default=0)
    first_seen_at = models.DateTimeField(null=True)  # earliest log event or snapshot
    last_seen_at = models.DateTimeField(null=True)
    closed_at = models.DateTimeField(null=True)  # upper bound, see module docstring
    raw = models.JSONField(default=dict)  # tab record from the newest snapshot

    def __str__(self):
        return f"{self.herdr_id} {self.title}"


class TabEvent(models.Model):
    class Kind(models.TextChoices):
        FOCUS = "focus"
        RENAME = "rename"

    tab = models.ForeignKey(Tab, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tab", "kind", "at"], name="uniq_herdr_tab_event")]
