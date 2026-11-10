"""Continuous location, pushed by the Overland iOS app as GeoJSON batches.

Raw and idempotent: a point is unique per device and timestamp, so a
re-sent batch replaces rather than duplicates. Unknown properties are kept
in LocationPoint.props.
"""

from django.db import models


class LocationBatch(models.Model):
    """One accepted upload (or replayed file)."""

    received_at = models.DateTimeField()
    sha256 = models.CharField(max_length=64, db_index=True)
    bytes = models.BigIntegerField(default=0)
    points = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    raw_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.received_at:%Y-%m-%d %H:%M} {self.points} points"


class LocationPoint(models.Model):
    device_id = models.CharField(max_length=100, blank=True)
    at = models.DateTimeField()
    lat = models.FloatField()
    lon = models.FloatField()
    altitude = models.FloatField(null=True)  # metres
    speed = models.FloatField(null=True)  # m/s; negative from iOS means unknown, stored as null
    course = models.FloatField(null=True)
    h_accuracy = models.FloatField(null=True)
    v_accuracy = models.FloatField(null=True)
    motion = models.JSONField(default=list, blank=True)  # ["walking"], ["driving", "stationary"], ...
    battery_level = models.FloatField(null=True)
    battery_state = models.CharField(max_length=20, blank=True)
    wifi = models.CharField(max_length=100, blank=True)
    props = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at"]
        constraints = [models.UniqueConstraint(fields=["device_id", "at"], name="uniq_location_point")]
        indexes = [models.Index(fields=["at"])]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.lat:.5f},{self.lon:.5f}"


class Visit(models.Model):
    """A stay at a place, from Google Timeline (on-device export or old
    Takeout Semantic Location History)."""

    source = models.CharField(max_length=30)  # timeline-ios, timeline-android, takeout-semantic
    start = models.DateTimeField()
    end = models.DateTimeField(null=True)
    lat = models.FloatField(null=True)
    lon = models.FloatField(null=True)
    place_id = models.CharField(max_length=200, blank=True)
    semantic_type = models.CharField(max_length=50, blank=True)  # HOME, WORK, ...
    name = models.CharField(max_length=300, blank=True)
    address = models.CharField(max_length=500, blank=True)
    probability = models.FloatField(null=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["start"]
        constraints = [models.UniqueConstraint(fields=["source", "start"], name="uniq_location_visit")]
        indexes = [models.Index(fields=["start"])]

    def __str__(self):
        return f"visit {self.start:%Y-%m-%d %H:%M} {self.name or self.place_id}"


class TripSegment(models.Model):
    """Movement between places, from Google Timeline."""

    source = models.CharField(max_length=30)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True)
    start_lat = models.FloatField(null=True)
    start_lon = models.FloatField(null=True)
    end_lat = models.FloatField(null=True)
    end_lon = models.FloatField(null=True)
    distance_m = models.FloatField(null=True)
    mode = models.CharField(max_length=50, blank=True)  # walking, in passenger vehicle, flying, ...
    probability = models.FloatField(null=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["start"]
        constraints = [models.UniqueConstraint(fields=["source", "start"], name="uniq_location_trip")]
        indexes = [models.Index(fields=["start"])]

    def __str__(self):
        return f"{self.mode} {self.start:%Y-%m-%d %H:%M}"
