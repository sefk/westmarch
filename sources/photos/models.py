"""Photos library metadata (never pixels), read through osxphotos.

One row per asset; people are Photos' own named faces. Assets removed from
the library keep their row with deleted_at set.
"""

from django.db import models


class Person(models.Model):
    uuid = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Photo(models.Model):
    uuid = models.CharField(max_length=64, unique=True)
    taken_at = models.DateTimeField(db_index=True)
    tz_offset_s = models.IntegerField(null=True)  # local offset where it was taken
    added_at = models.DateTimeField(null=True)
    modified_at = models.DateTimeField(null=True)
    kind = models.CharField(max_length=10)  # photo | video
    screenshot = models.BooleanField(default=False)
    selfie = models.BooleanField(default=False)
    favorite = models.BooleanField(default=False)
    hidden = models.BooleanField(default=False)
    in_trash = models.BooleanField(default=False)
    shared = models.BooleanField(default=False)  # from a shared album
    syndicated = models.BooleanField(default=False)  # shared with me via Messages
    lat = models.FloatField(null=True)
    lon = models.FloatField(null=True)
    place = models.CharField(max_length=300, blank=True)
    address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=10, blank=True)
    original_filename = models.CharField(max_length=300, blank=True)
    uti = models.CharField(max_length=100, blank=True)
    width = models.IntegerField(null=True)
    height = models.IntegerField(null=True)
    camera = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    albums = models.JSONField(default=list, blank=True)
    keywords = models.JSONField(default=list, blank=True)
    labels = models.JSONField(default=list, blank=True)  # Photos' own ML labels
    faces = models.IntegerField(default=0)  # all detected faces, named or not
    people = models.ManyToManyField(Person, related_name="photos", blank=True)
    deleted_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["taken_at"]

    def __str__(self):
        return f"{self.taken_at:%Y-%m-%d %H:%M} {self.original_filename}"
