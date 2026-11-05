"""Apple Health, pushed by the Health Auto Export iOS app.

Raw and idempotent: re-sent windows overwrite rows with the same natural
key. Unknown metrics and fields are kept (Metric.fields, Workout.raw).
"""

from django.db import models


class IngestBatch(models.Model):
    """One accepted upload (or replayed file): provenance for the rows."""

    received_at = models.DateTimeField()
    sha256 = models.CharField(max_length=64, db_index=True)
    bytes = models.BigIntegerField(default=0)
    metric_points = models.IntegerField(default=0)
    workouts = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    raw_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.received_at:%Y-%m-%d %H:%M} {self.sha256[:8]}"


class Metric(models.Model):
    """One data point of one named metric (step_count, heart_rate, ...)."""

    name = models.CharField(max_length=100)
    units = models.CharField(max_length=40, blank=True)
    at = models.DateTimeField()
    qty = models.FloatField(null=True)
    source = models.CharField(max_length=200, blank=True)
    # Everything else on the point: heart-rate Min/Avg/Max, sleep segment
    # stage and end, blood-pressure systolic/diastolic, unknown keys.
    fields = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name", "at"]
        constraints = [models.UniqueConstraint(fields=["name", "at", "source"], name="uniq_health_metric")]
        indexes = [models.Index(fields=["name", "at"])]

    def __str__(self):
        return f"{self.name} {self.at:%Y-%m-%d %H:%M} {self.qty}"


class SleepNight(models.Model):
    """Summarized sleep_analysis: one entry per night per source."""

    night = models.DateField()  # the morning date the app assigns
    source = models.CharField(max_length=200, blank=True)
    total_sleep_hr = models.FloatField(null=True)
    in_bed_hr = models.FloatField(null=True)
    asleep_hr = models.FloatField(null=True)
    core_hr = models.FloatField(null=True)
    deep_hr = models.FloatField(null=True)
    rem_hr = models.FloatField(null=True)
    awake_hr = models.FloatField(null=True)
    sleep_start = models.DateTimeField(null=True)
    sleep_end = models.DateTimeField(null=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-night"]
        constraints = [models.UniqueConstraint(fields=["night", "source"], name="uniq_health_sleep")]

    def __str__(self):
        return f"sleep {self.night} {self.total_sleep_hr}h"


class Workout(models.Model):
    external_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=100)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True)
    duration_seconds = models.FloatField(null=True)
    active_energy_kcal = models.FloatField(null=True)
    distance = models.FloatField(null=True)
    distance_units = models.CharField(max_length=20, blank=True)
    avg_heart_rate = models.FloatField(null=True)
    max_heart_rate = models.FloatField(null=True)
    is_indoor = models.BooleanField(null=True)
    location = models.CharField(max_length=50, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-start"]
        indexes = [models.Index(fields=["start"])]

    def __str__(self):
        return f"{self.name} {self.start:%Y-%m-%d %H:%M}"
