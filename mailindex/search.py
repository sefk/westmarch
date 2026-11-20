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


# -- search by meaning, and both together --


def open_store():
    """The current embedding store, read-only, or None if there isn't one yet."""
    from django.conf import settings

    from .vecstore import VecStore, store_path

    path = store_path(settings.MAILINDEX_VEC_DIR, settings.MAILVEC_MODEL)
    try:
        return VecStore(path, readonly=True)
    except FileNotFoundError:
        return None


def meaning(text: str, k: int = 50, store=None, client=None, after=None, before=None) -> list[dict]:
    """Chunks nearest to `text` by meaning; after/before are yyyymmdd ints."""
    from django.conf import settings

    from .llm import LocalLlm

    store = store or open_store()
    if store is None:
        return []
    client = client or LocalLlm(settings.MAILVEC_URL, store.model)
    vector = client.embed([store.meta("query_prefix", "") + text])[0]
    return store.knn(vector, k=k, after=after, before=before)


RRF_K = 60  # reciprocal rank fusion: a hit at rank r adds 1/(RRF_K + r)
MAX_DISTANCE = 0.95  # cosine; nearer 1 shares nothing with the query


def hybrid(text: str, limit: int = 10, store=None, client=None) -> list[dict]:
    """Threads ranked by keyword and meaning together, best first. Each:
    thread_id, score, how ('keyword', 'meaning' or both), a snippet."""
    scores: dict[str, float] = {}
    how: dict[str, set] = {}
    snippet: dict[str, str] = {}

    def add(thread_id, rank, kind, snip):
        scores[thread_id] = scores.get(thread_id, 0) + 1 / (RRF_K + rank)
        how.setdefault(thread_id, set()).add(kind)
        snippet.setdefault(thread_id, snip)

    kw = search(text, limit=50)
    ids = [h["gmail_id"] for h in kw["bodies"] + kw["attachments"]]
    with connection.cursor() as c:
        if ids:
            c.execute(f"SELECT gmail_id, thread_id FROM src_google_gmailmessage WHERE gmail_id IN "
                      f"({','.join(['%s'] * len(ids))})", ids)
        threads = dict(c.fetchall()) if ids else {}
    seen = []
    for h in kw["bodies"] + kw["attachments"]:
        t = threads.get(h["gmail_id"])
        if t and t not in seen:
            seen.append(t)
            add(t, len(seen), "keyword", h["snippet"])
    seen = []
    for h in meaning(text, k=100, store=store, client=client):
        t = h["thread_id"]
        if h["distance"] >= MAX_DISTANCE:  # nothing in common with the query
            continue
        if t and t not in seen:
            seen.append(t)
            add(t, len(seen), "meaning" if h["kind"] != "digest" else "summary", h["text"][:300])
    ranked = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [{"thread_id": t, "score": round(scores[t], 4), "how": sorted(how[t]), "snippet": snippet[t]}
            for t in ranked]
