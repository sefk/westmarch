"""Messages (chat.db) mirrored into our own tables.

Message text is private: nothing here is ever logged. Attachments are
metadata only; the files themselves are never read or copied.
"""

from django.db import models


class Handle(models.Model):
    identifier = models.CharField(max_length=200)  # phone number or email
    service = models.CharField(max_length=30, blank=True)  # iMessage / SMS / RCS

    class Meta:
        constraints = [models.UniqueConstraint(fields=["identifier", "service"], name="uniq_imessage_handle")]

    def __str__(self):
        return f"{self.identifier} ({self.service})"


class Chat(models.Model):
    guid = models.CharField(max_length=200, unique=True)
    identifier = models.CharField(max_length=200, blank=True)  # chat_identifier
    display_name = models.CharField(max_length=300, blank=True)
    is_group = models.BooleanField(default=False)  # style 43; 45 is 1:1
    service = models.CharField(max_length=30, blank=True)
    participants = models.JSONField(default=list)  # handle identifiers

    def __str__(self):
        return self.display_name or self.identifier or self.guid


class Message(models.Model):
    rowid = models.BigIntegerField(unique=True)  # message.ROWID in chat.db; the incremental cursor
    guid = models.CharField(max_length=200, unique=True)
    chat = models.ForeignKey(Chat, null=True, on_delete=models.SET_NULL, related_name="messages")
    handle = models.ForeignKey(Handle, null=True, on_delete=models.SET_NULL, related_name="messages")
    is_from_me = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True)
    read_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)
    service = models.CharField(max_length=30, blank=True)
    text = models.TextField(blank=True)  # from `text`, else decoded from attributedBody
    has_attachments = models.BooleanField(default=False)
    attachment_count = models.IntegerField(default=0)
    reaction = models.CharField(max_length=20, blank=True)  # "love"; "-love" = tapback removed
    reply_to_guid = models.CharField(max_length=200, blank=True)  # thread parent, or tapback target
    edited_at = models.DateTimeField(null=True)
    retracted_at = models.DateTimeField(null=True)
    item_type = models.IntegerField(default=0)  # 0 = ordinary message; others are group events

    class Meta:
        indexes = [
            models.Index(fields=["sent_at"]),
            models.Index(fields=["chat", "sent_at"]),
        ]


class Attachment(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    guid = models.CharField(max_length=200)
    filename = models.CharField(max_length=500, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    transfer_name = models.CharField(max_length=300, blank=True)
    total_bytes = models.BigIntegerField(null=True)
