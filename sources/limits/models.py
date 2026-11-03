"""Subscription rate-limit snapshots, one row per vendor window per run.

Raw and append-only, Dogsheep-style: each hourly run records what the
vendor reported, and graphs of "how close to my limits am I" read
straight from LimitSnapshot. Never store credentials here.
"""

from django.db import models


class Subscription(models.Model):
    """A paid AI plan and its monthly price, seeded by a data migration so
    cost-vs-usage views have prices without hand entry elsewhere."""

    vendor = models.CharField(max_length=30, unique=True)  # matches LimitSnapshot.vendor
    plan = models.CharField(max_length=50)
    monthly_usd = models.DecimalField(max_digits=8, decimal_places=2, null=True)  # null: pay-as-you-go
    shared = models.BooleanField(default=False)  # shared with family
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["vendor"]

    def __str__(self):
        return f"{self.vendor} {self.plan}"


class LimitSnapshot(models.Model):
    """One vendor-reported limit window at one moment.

    `window` is our stable name: session_5h, weekly_all, weekly_sonnet,
    weekly_<model>, credits, budget_monthly, ... Unknown vendor buckets
    keep the vendor's key so nothing is dropped.
    """

    vendor = models.CharField(max_length=30)  # claude | codex | openrouter
    window = models.CharField(max_length=50)
    taken_at = models.DateTimeField()
    utilization_pct = models.FloatField(null=True)  # 0-100
    resets_at = models.DateTimeField(null=True)
    used_usd = models.FloatField(null=True)  # dollar-denominated windows only
    limit_usd = models.FloatField(null=True)
    source = models.CharField(max_length=30, default="api")  # api | claude-monitor | codex-sessions
    raw = models.JSONField(default=dict)

    class Meta:
        ordering = ["vendor", "window", "taken_at"]
        constraints = [
            models.UniqueConstraint(fields=["vendor", "window", "taken_at"], name="uniq_limit_snapshot")
        ]
        indexes = [models.Index(fields=["vendor", "taken_at"])]

    def __str__(self):
        return f"{self.vendor}/{self.window} {self.taken_at:%Y-%m-%d %H:%M} {self.utilization_pct}%"
