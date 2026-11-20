"""Searchable text for the Gmail mirror: bodies and attachments.

Derived entirely from the raw files under RAW_DIR, so every row here can
be rebuilt. Attachment text is kept once per distinct file (sha256) in
AttachmentBlob; MailAttachment is one row per attachment per message.
Both text tables have an FTS5 index beside them (mailindex_body_fts,
mailindex_blob_fts), kept in sync by triggers (see migration 0001).
"""

from django.db import models

from sources.google.models import GmailMessage


class MailBody(models.Model):
    message = models.OneToOneField(GmailMessage, on_delete=models.CASCADE, primary_key=True,
                                   related_name="body")
    text = models.TextField(blank=True)
    error = models.TextField(blank=True)
    indexed_at = models.DateTimeField(auto_now=True)


class AttachmentBlob(models.Model):
    STATUSES = ["text", "ocr", "skipped", "error"]  # "ocr": waiting for the local model

    sha256 = models.CharField(max_length=64, unique=True)
    mime_type = models.CharField(max_length=200, blank=True)
    size = models.IntegerField()
    status = models.CharField(max_length=10, db_index=True)
    method = models.CharField(max_length=100, blank=True)  # pdf, docx, ..., ocr:<model>
    text = models.TextField(blank=True)
    error = models.TextField(blank=True)
    ocr_attempts = models.IntegerField(default=0)
    # Where to find the bytes again: the first message seen carrying them.
    source_message = models.ForeignKey(GmailMessage, on_delete=models.CASCADE, related_name="+")
    source_part = models.IntegerField()
    extracted_at = models.DateTimeField(auto_now=True)


class ThreadDigest(models.Model):
    """A local model's index entry for one thread I wrote in: a summary and
    the people, dates, amounts and commitments in it (see digest.py). One
    row per thread per model, so a better model's digests sit beside an
    older one's for comparison. Rewritten when the thread gets new mail."""

    thread_id = models.CharField(max_length=32, db_index=True)
    model = models.CharField(max_length=200)
    prompt_version = models.CharField(max_length=20)
    message_count = models.IntegerField()
    last_message_at = models.DateTimeField(null=True)  # newest message the digest saw
    summary = models.TextField(blank=True)
    topic = models.CharField(max_length=40, blank=True)
    digest = models.JSONField(default=dict)  # the model's whole JSON reply
    error = models.TextField(blank=True)
    attempts = models.IntegerField(default=0)
    input_chars = models.IntegerField(default=0)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    seconds = models.FloatField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread_id", "model"], name="uniq_thread_digest")]


class MailAttachment(models.Model):
    message = models.ForeignKey(GmailMessage, on_delete=models.CASCADE, related_name="attachments")
    part = models.IntegerField()  # position among the message's attachments
    filename = models.CharField(max_length=500, blank=True)
    mime_type = models.CharField(max_length=200, blank=True)
    size = models.IntegerField()
    blob = models.ForeignKey(AttachmentBlob, on_delete=models.CASCADE, related_name="attachments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message", "part"], name="uniq_mail_attachment")]
