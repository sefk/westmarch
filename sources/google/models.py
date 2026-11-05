"""Raw Google tables: Gmail, Calendar, Tasks.

Gmail keeps the full RFC 822 message on disk (gzip, under RAW_DIR) and a
row of parsed headers here. Calendar events and tasks are small, so the
whole API payload goes in `raw` next to the typed columns. Nothing is
ever deleted: a message or event that goes away gets a `deleted_at` or a
status, and keeps its row.
"""

from django.db import models


class GmailLabel(models.Model):
    label_id = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=500)
    type = models.CharField(max_length=20, blank=True)  # system | user
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class GmailMessage(models.Model):
    gmail_id = models.CharField(max_length=32, unique=True)
    thread_id = models.CharField(max_length=32, db_index=True)
    history_id = models.BigIntegerField(null=True)
    internal_date = models.DateTimeField(null=True, db_index=True)
    label_ids = models.JSONField(default=list)
    size_estimate = models.IntegerField(null=True)
    snippet = models.TextField(blank=True)

    # Parsed headers. Address lists are JSON lists of bare addresses.
    from_name = models.CharField(max_length=500, blank=True)
    from_address = models.CharField(max_length=500, blank=True, db_index=True)
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    subject = models.TextField(blank=True)
    date = models.DateTimeField(null=True)  # the Date: header, if parseable
    message_id = models.TextField(blank=True)  # Message-ID header
    in_reply_to = models.TextField(blank=True)
    list_id = models.CharField(max_length=500, blank=True)
    has_attachments = models.BooleanField(default=False)
    attachment_count = models.IntegerField(default=0)
    parse_error = models.TextField(blank=True)  # non-empty: some header was unreadable

    raw_path = models.CharField(max_length=200)  # relative to settings.RAW_DIR
    fetched_at = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True)  # gone from Gmail; row and file kept

    def __str__(self):
        return f"{self.gmail_id} {self.subject[:60]}"


class GCalCalendar(models.Model):
    calendar_id = models.CharField(max_length=500, unique=True)
    summary = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    time_zone = models.CharField(max_length=100, blank=True)
    access_role = models.CharField(max_length=30, blank=True)
    primary = models.BooleanField(default=False)
    raw = models.JSONField(default=dict)

    def __str__(self):
        return self.summary or self.calendar_id


class GCalEvent(models.Model):
    calendar = models.ForeignKey(GCalCalendar, on_delete=models.CASCADE, related_name="events")
    event_id = models.CharField(max_length=1100)
    status = models.CharField(max_length=20, blank=True)  # confirmed | tentative | cancelled
    summary = models.TextField(blank=True)
    description = models.TextField(blank=True)
    location = models.TextField(blank=True)
    start = models.DateTimeField(null=True, db_index=True)
    end = models.DateTimeField(null=True)
    all_day = models.BooleanField(default=False)
    start_raw = models.CharField(max_length=50, blank=True)  # the API's date or dateTime string
    end_raw = models.CharField(max_length=50, blank=True)
    recurrence = models.JSONField(default=list)
    recurring_event_id = models.CharField(max_length=1100, blank=True)
    organizer_email = models.CharField(max_length=500, blank=True)
    attendees = models.JSONField(default=list)  # [{email, responseStatus, self}]
    created = models.DateTimeField(null=True)
    updated = models.DateTimeField(null=True)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["calendar", "event_id"], name="uniq_gcal_event")]

    def __str__(self):
        return f"{self.start:%Y-%m-%d} {self.summary[:60]}" if self.start else self.summary[:60]


class GTaskList(models.Model):
    list_id = models.CharField(max_length=200, unique=True)
    title = models.CharField(max_length=500, blank=True)
    updated = models.DateTimeField(null=True)
    raw = models.JSONField(default=dict)

    def __str__(self):
        return self.title


class GTask(models.Model):
    tasklist = models.ForeignKey(GTaskList, on_delete=models.CASCADE, related_name="tasks")
    task_id = models.CharField(max_length=200)
    title = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, blank=True)  # needsAction | completed
    due = models.DateTimeField(null=True)
    completed = models.DateTimeField(null=True)
    updated = models.DateTimeField(null=True)
    parent = models.CharField(max_length=200, blank=True)
    position = models.CharField(max_length=100, blank=True)
    deleted = models.BooleanField(default=False)
    hidden = models.BooleanField(default=False)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tasklist", "task_id"], name="uniq_gtask")]

    def __str__(self):
        return self.title[:60]
