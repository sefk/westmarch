"""Raw agentsview tables, Dogsheep style: typed columns for querying plus
the full CLI payloads in `raw`.

Every agentsview session is a row, subagent and automated ones included:
agentsview reports each transcript's own usage only (a parent's tokens
exclude its subagents'), so summing all rows gives true spend. Count
"sessions" with `relationship_type != 'subagent'`, not by parent id:
many subagents (mostly codex) have no parent recorded.
"""

from django.db import models


class Session(models.Model):
    session_id = models.CharField(max_length=200, unique=True)  # agentsview id
    parent_session_id = models.CharField(max_length=200, null=True, blank=True, db_index=True)
    relationship_type = models.CharField(max_length=30, blank=True)  # subagent | continuation | ""
    agent = models.CharField(max_length=50, db_index=True)  # claude, codex, pi, ...
    # agentsview's project name as its UI shows it (worktrees folded into
    # the main checkout, e.g. `datatalk`), and the per-checkout label from
    # `export sessions` (e.g. `datatalk_1134_activity_tui`).
    project = models.CharField(max_length=300, db_index=True)
    project_label = models.CharField(max_length=300, blank=True)
    cwd = models.CharField(max_length=1000, blank=True)
    git_branch = models.CharField(max_length=300, blank=True)
    machine = models.CharField(max_length=100, blank=True)
    display_name = models.TextField(blank=True)
    classification = models.CharField(max_length=30, blank=True)  # interactive | automated
    is_automated = models.BooleanField(default=False)
    entrypoint = models.CharField(max_length=50, blank=True)  # cli | sdk-cli (claude -p) | ...
    session_kind = models.CharField(max_length=50, blank=True)  # bg | non-interactive | ""
    started_at = models.DateTimeField(null=True, db_index=True)
    ended_at = models.DateTimeField(null=True)
    last_activity_at = models.DateTimeField(null=True)
    duration_seconds = models.IntegerField(null=True)
    message_count = models.IntegerField(default=0)
    user_message_count = models.IntegerField(default=0)
    assistant_message_count = models.IntegerField(default=0)
    turn_count = models.IntegerField(default=0)
    primary_model = models.CharField(max_length=200, blank=True)  # most tokens
    model_names = models.JSONField(default=list)  # every model used
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cache_read_tokens = models.BigIntegerField(default=0)
    cache_write_tokens = models.BigIntegerField(default=0)
    reasoning_tokens = models.BigIntegerField(default=0)
    peak_context_tokens = models.BigIntegerField(null=True)
    estimated_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    has_cost = models.BooleanField(default=False)  # false if any model was unpriced
    raw = models.JSONField(default=dict)  # {"export": {...}, "list": {...}}
    loaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["project", "started_at"])]

    def __str__(self):
        return f"{self.agent} {self.project} {self.session_id}"


class ModelUsage(models.Model):
    """One session's usage of one model (agentsview's `by_model`)."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="usage")
    model = models.CharField(max_length=200, db_index=True)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cache_read_tokens = models.BigIntegerField(default=0)
    cache_write_tokens = models.BigIntegerField(default=0)
    reasoning_tokens = models.BigIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    has_cost = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "model"], name="uniq_agentsview_session_model")
        ]

    def __str__(self):
        return f"{self.session_id} {self.model}"
