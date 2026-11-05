"""RFC 822 header parsing for Gmail raw messages, and the on-disk store.

Mail in the wild has broken headers. Every field is read on its own and a
failure lands in `parse_error` instead of failing the message (or run).
"""

from __future__ import annotations

import gzip
import os
import tempfile
from datetime import UTC
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from pathlib import Path


def raw_relpath(gmail_id: str) -> str:
    return f"gmail/{gmail_id[:2]}/{gmail_id}.eml.gz"


def write_raw(root: Path, gmail_id: str, raw: bytes) -> str:
    """gzip `raw` to root/gmail/<xx>/<id>.eml.gz atomically; return the relative path."""
    rel = raw_relpath(gmail_id)
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{gmail_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(raw, compresslevel=6))
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return rel


def read_raw(root: Path, rel: str) -> bytes:
    return gzip.decompress((root / rel).read_bytes())


def _addresses(header) -> list[tuple[str, str]]:
    """(display name, address) pairs from a parsed address header."""
    try:
        pairs = [(a.display_name or "", a.addr_spec or "") for a in header.addresses]
        return [(n, "" if a == "<>" else a) for n, a in pairs]  # "<>" = unparseable
    except Exception:
        return [(n, a) for n, a in getaddresses([str(header)]) if a or n]


def parse_headers(raw: bytes) -> dict:
    """Parsed header fields for a GmailMessage row, plus `parse_error`."""
    errors: list[str] = []
    out = dict(from_name="", from_address="", to=[], cc=[], subject="", date=None,
               message_id="", in_reply_to="", list_id="", has_attachments=False, attachment_count=0)
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # the parser is lenient; this is rare
        return out | {"parse_error": f"message: {type(exc).__name__}: {exc}"[:500]}

    def get(name):
        try:
            h = msg[name]
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            return None
        for d in getattr(h, "defects", ()):
            errors.append(f"{name}: {type(d).__name__}")
        return h

    if (h := get("From")) is not None:
        pairs = _addresses(h)
        if pairs:
            out["from_name"], out["from_address"] = pairs[0]
        else:
            out["from_name"], out["from_address"] = parseaddr(str(h))
    for name, key in (("To", "to"), ("Cc", "cc")):
        if (h := get(name)) is not None:
            out[key] = [a for _, a in _addresses(h) if a]
    for name, key in (("Subject", "subject"), ("Message-ID", "message_id"),
                      ("In-Reply-To", "in_reply_to"), ("List-Id", "list_id")):
        if (h := get(name)) is not None:
            out[key] = " ".join(str(h).split())
    if (h := get("Date")) is not None:
        try:
            d = h.datetime
            if d is not None and d.tzinfo is None:
                d = d.replace(tzinfo=UTC)  # "-0000": time zone unknown
            out["date"] = d
            if d is None and str(h).strip():
                errors.append("Date: unparseable")
        except Exception as exc:
            errors.append(f"Date: {type(exc).__name__}")
    for key, limit in (("from_name", 500), ("from_address", 500), ("list_id", 500)):
        out[key] = out[key][:limit]

    try:
        count = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = part.get_content_disposition()
            if disp == "attachment" or (disp != "inline" and part.get_filename()):
                count += 1
        out["attachment_count"], out["has_attachments"] = count, count > 0
    except Exception as exc:
        errors.append(f"attachments: {type(exc).__name__}")

    # Message-level defects (bad MIME structure) are common and harmless to
    # the header row; they're only noted alongside a header problem.
    defects = [type(d).__name__ for d in getattr(msg, "defects", [])]
    out["parse_error"] = "; ".join(dict.fromkeys(errors + defects))[:500] if errors else ""
    return out
