"""Embeddings of mail, one SQLite file per embedding model.

    $MAILINDEX_VEC_DIR/vec-<model-slug>.sqlite3   (default RAW_DIR/mailindex)

Kept out of db.sqlite3 on purpose: it is large, entirely rebuildable from
the warehouse, and a second model's index can sit beside the first for
comparison. Tables:

    meta      model, dims, doc/query prefixes
    chunk     one row per embedded piece of text: kind ('body' = a
              message, ref = GmailMessage.id; 'att' = an attachment's text,
              ref = AttachmentBlob.id; 'digest' = a thread digest, ref =
              ThreadDigest.id), its thread, day and the text itself
    vec_chunk sqlite-vec vec0 table, rowid = chunk.id, cosine distance,
              with `day` (yyyymmdd) as a metadata column for date filters
    done      what has been embedded (kind, ref, stamp), including items
              that produced no chunks, so they aren't retried

Readers (westmarch's mailsearch, Samwise) open it read-only; the
writer uses WAL so they don't block each other.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from .llm import embed_prefixes, model_slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunk (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    message_id INTEGER,
    day INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunk_ref ON chunk (kind, ref);
CREATE TABLE IF NOT EXISTS done (
    kind TEXT NOT NULL,
    ref INTEGER NOT NULL,
    stamp TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, ref)
) WITHOUT ROWID;
"""


def store_path(vec_dir: Path, model: str) -> Path:
    return Path(vec_dir) / f"vec-{model_slug(model)}.sqlite3"


def day_of(value) -> int:
    """yyyymmdd from a datetime or a stored 'YYYY-MM-DD ...' string; 0 if unknown."""
    if not value:
        return 0
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    digits = text[:10].replace("-", "")
    return int(digits) if digits.isdigit() and len(digits) == 8 else 0


class VecStore:
    def __init__(self, path: Path, model: str | None = None, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            self.con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(self.path, timeout=30)
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.executescript(SCHEMA)
        self.con.enable_load_extension(True)
        sqlite_vec.load(self.con)
        self.con.enable_load_extension(False)
        if not readonly:
            stored = self.meta("model")
            if model and stored and stored != model:
                raise ValueError(f"{self.path} holds {stored!r}, not {model!r}")
            if model and not stored:
                doc, query = embed_prefixes(model)
                self.set_meta(model=model, doc_prefix=doc, query_prefix=query)
                self.con.commit()
        self.model = self.meta("model")

    def close(self):
        self.con.close()

    # -- meta --

    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, **values):
        self.con.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             [(k, str(v)) for k, v in values.items()])

    @property
    def dims(self) -> int | None:
        d = self.meta("dims")
        return int(d) if d else None

    def _ensure_vec(self, dims: int):
        if self.dims is None:
            self.con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunk USING vec0("
                             f"embedding float[{dims}] distance_metric=cosine, day integer)")
            self.set_meta(dims=dims)
        elif self.dims != dims:
            raise ValueError(f"model returned {dims} dimensions; {self.path} holds {self.dims}")

    # -- writing --

    def done_stamps(self, kind: str) -> dict[int, str]:
        return dict(self.con.execute("SELECT ref, stamp FROM done WHERE kind = ?", (kind,)))

    def replace(self, kind: str, ref: int, stamp: str, chunks: list[dict], vectors: list[list[float]]):
        """Store one item's chunks (dicts with seq, thread_id, message_id, day,
        text) and their vectors, replacing whatever it had. No commit."""
        if vectors:
            self._ensure_vec(len(vectors[0]))
        old = [r[0] for r in self.con.execute("SELECT id FROM chunk WHERE kind = ? AND ref = ?", (kind, ref))]
        if old:
            marks = ",".join("?" * len(old))
            self.con.execute(f"DELETE FROM vec_chunk WHERE rowid IN ({marks})", old)
            self.con.execute(f"DELETE FROM chunk WHERE id IN ({marks})", old)
        for c, vec in zip(chunks, vectors):
            cid = self.con.execute(
                "INSERT INTO chunk (kind, ref, seq, thread_id, message_id, day, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, ref, c["seq"], c.get("thread_id", ""), c.get("message_id"), c.get("day", 0), c["text"]),
            ).lastrowid
            self.con.execute("INSERT INTO vec_chunk (rowid, embedding, day) VALUES (?, ?, ?)",
                             (cid, sqlite_vec.serialize_float32(vec), c.get("day", 0)))
        self.con.execute("INSERT OR REPLACE INTO done (kind, ref, stamp) VALUES (?, ?, ?)", (kind, ref, stamp))

    def forget(self, kind: str, ref: int):
        """Drop an item's chunks and its done row, as if never embedded. No commit."""
        old = [r[0] for r in self.con.execute("SELECT id FROM chunk WHERE kind = ? AND ref = ?", (kind, ref))]
        if old:
            marks = ",".join("?" * len(old))
            self.con.execute(f"DELETE FROM vec_chunk WHERE rowid IN ({marks})", old)
            self.con.execute(f"DELETE FROM chunk WHERE id IN ({marks})", old)
        self.con.execute("DELETE FROM done WHERE kind = ? AND ref = ?", (kind, ref))

    def refs_by_message(self, kind: str) -> dict[int, set[int]]:
        """message_id -> refs of `kind` whose chunks belong to that message."""
        out: dict[int, set[int]] = {}
        for ref, mid in self.con.execute(
                "SELECT DISTINCT ref, message_id FROM chunk WHERE kind = ? AND message_id IS NOT NULL", (kind,)):
            out.setdefault(mid, set()).add(ref)
        return out

    def commit(self):
        self.con.commit()

    # -- reading --

    def counts(self) -> dict:
        out = {f"done_{k}": n for k, n in self.con.execute("SELECT kind, COUNT(*) FROM done GROUP BY kind")}
        out.update({f"chunks_{k}": n for k, n in self.con.execute("SELECT kind, COUNT(*) FROM chunk GROUP BY kind")})
        return out

    def knn(self, vector: list[float], k: int = 50, after: int | None = None, before: int | None = None) -> list[dict]:
        """Nearest chunks to `vector`, closest first. after/before are yyyymmdd
        bounds (inclusive, exclusive) applied inside the search."""
        if self.dims is None:
            return []
        where, params = ["embedding MATCH ?", "k = ?"], [sqlite_vec.serialize_float32(vector), int(k)]
        if after:
            where.append("day >= ?")
            params.append(int(after))
        if before:
            where.append("day < ?")
            params.append(int(before))
        sql = (f"SELECT c.id, c.kind, c.ref, c.seq, c.thread_id, c.message_id, c.day, c.text, v.distance"
               f" FROM (SELECT rowid, distance FROM vec_chunk WHERE {' AND '.join(where)}) v"
               f" JOIN chunk c ON c.id = v.rowid ORDER BY v.distance")
        cols = ("id", "kind", "ref", "seq", "thread_id", "message_id", "day", "text", "distance")
        return [dict(zip(cols, r)) for r in self.con.execute(sql, params)]
