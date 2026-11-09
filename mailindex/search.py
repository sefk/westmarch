"""Full-text search over mail bodies and attachment text."""

from __future__ import annotations

import re

from django.db import connection


def fts_query(text: str) -> str:
    """Free text to an FTS5 query: every word must match, as a prefix if
    it ends in '*'. Quoting each word keeps FTS syntax characters inert."""
    words = re.findall(r"[\w'*-]+", text)
    terms = []
    for w in words:
        star = w.endswith("*")
        w = w.strip("*").replace('"', "")
        if w:
            terms.append(f'"{w}"' + ("*" if star else ""))
    return " ".join(terms)


BODY_SQL = """
SELECT m.gmail_id, m.date, m.from_address, m.subject, snippet(mailindex_body_fts, 0, '[', ']', ' … ', 12)
FROM mailindex_body_fts f JOIN src_google_gmailmessage m ON m.id = f.rowid
WHERE mailindex_body_fts MATCH %s ORDER BY m.internal_date DESC LIMIT %s
"""

ATTACHMENT_SQL = """
SELECT m.gmail_id, m.date, m.from_address, m.subject, a.part, a.filename,
       snippet(mailindex_blob_fts, 0, '[', ']', ' … ', 12)
FROM mailindex_blob_fts f
JOIN mailindex_mailattachment a ON a.blob_id = f.rowid
JOIN src_google_gmailmessage m ON m.id = a.message_id
WHERE mailindex_blob_fts MATCH %s ORDER BY m.internal_date DESC LIMIT %s
"""


def search(text: str, limit: int = 20) -> dict:
    q = fts_query(text)
    if not q:
        return {"bodies": [], "attachments": []}
    with connection.cursor() as c:
        c.execute(BODY_SQL, [q, limit])
        bodies = [dict(zip(("gmail_id", "date", "from", "subject", "snippet"), r)) for r in c.fetchall()]
        c.execute(ATTACHMENT_SQL, [q, limit])
        atts = [dict(zip(("gmail_id", "date", "from", "subject", "part", "filename", "snippet"), r))
                for r in c.fetchall()]
    return {"bodies": bodies, "attachments": atts}
