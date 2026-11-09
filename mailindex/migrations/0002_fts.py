"""FTS5 indexes over MailBody.text and AttachmentBlob.text.

External-content tables: the text lives once, in the model tables, and
triggers keep the index in step with every insert, update and delete.
"""

from django.db import migrations

TOKENIZE = "porter unicode61 remove_diacritics 2"


def fts(table, index, key):
    return [
        f"CREATE VIRTUAL TABLE {index} USING fts5(text, content='{table}', content_rowid='{key}',"
        f" tokenize='{TOKENIZE}')",
        f"CREATE TRIGGER {index}_ai AFTER INSERT ON {table} BEGIN"
        f" INSERT INTO {index}(rowid, text) VALUES (new.{key}, new.text); END",
        f"CREATE TRIGGER {index}_ad AFTER DELETE ON {table} BEGIN"
        f" INSERT INTO {index}({index}, rowid, text) VALUES ('delete', old.{key}, old.text); END",
        f"CREATE TRIGGER {index}_au AFTER UPDATE OF text ON {table} BEGIN"
        f" INSERT INTO {index}({index}, rowid, text) VALUES ('delete', old.{key}, old.text);"
        f" INSERT INTO {index}(rowid, text) VALUES (new.{key}, new.text); END",
    ]


def drop(index):
    return [f"DROP TRIGGER IF EXISTS {index}_{t}" for t in ("ai", "ad", "au")] + [f"DROP TABLE IF EXISTS {index}"]


class Migration(migrations.Migration):
    dependencies = [("mailindex", "0001_initial")]

    operations = [
        migrations.RunSQL(fts("mailindex_mailbody", "mailindex_body_fts", "message_id"),
                          drop("mailindex_body_fts")),
        migrations.RunSQL(fts("mailindex_attachmentblob", "mailindex_blob_fts", "id"),
                          drop("mailindex_blob_fts")),
    ]
