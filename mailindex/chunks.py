"""Which mail is worth the model's time, and how text is cut up for it.

"Personal" mail is any thread I wrote in, plus whatever is neither a
mailing list nor in one of Gmail's bulk tabs. It is the default scope of
the embedding and digest passes: about a fifth of the mailbox, and the
part that keyword search serves worst. Spam and Trash are never indexed.
"""

from __future__ import annotations

import re

BULK_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS")
NEVER_LABELS = ("SPAM", "TRASH")

# A message I sent: the API labels it SENT; a Takeout row not yet relabelled
# carries the display name "Sent" in takeout_labels.
IS_SENT = (
    "(EXISTS (SELECT 1 FROM json_each(m.label_ids) WHERE value = 'SENT')"
    " OR EXISTS (SELECT 1 FROM json_each(m.takeout_labels) WHERE value = 'Sent'))"
)


def _labels_in(labels) -> str:
    quoted = ",".join(f"'{label}'" for label in labels)
    return f"EXISTS (SELECT 1 FROM json_each(m.label_ids) WHERE value IN ({quoted}))"


# SQL over src_google_gmailmessage aliased `m`. MY_THREADS is a CTE body.
MY_THREADS = f"SELECT DISTINCT m.thread_id FROM src_google_gmailmessage m WHERE {IS_SENT}"
NOT_NEVER = f"NOT {_labels_in(NEVER_LABELS)}"
PERSONAL = (f"(m.thread_id IN (SELECT thread_id FROM my_threads)"
            f" OR (m.list_id = '' AND NOT {_labels_in(BULK_LABELS)}))")


# Where the quoted copy of earlier mail starts in a reply.
_QUOTE_START = re.compile(
    r"^(On\b[^\n]{0,200}(?:\n[^\n]{0,200})?\bwrote:[ \t]*$"
    r"|-{2,}[ \t]*Original Message[ \t]*-{2,}"
    r"|_{10,}[ \t]*\nFrom:[^\n]*\n(?:Sent|Date):"
    r"|From:[^\n]*\n(?:Sent|Date):[^\n]*\n(?:To|Subject):)",
    re.M | re.I,
)


def own_text(text: str) -> str:
    """A message's own words: everything before the quoted earlier mail.
    Forwarded mail is kept, since in a forward the quoted part is the news.
    If nothing comes before the quote (bottom-posting), keep it all."""
    m = _QUOTE_START.search(text)
    if m and text[:m.start()].strip():
        return text[:m.start()].rstrip()
    return text


def chunk(text: str, size: int = 1500, overlap: int = 200, max_chunks: int = 6) -> list[str]:
    """Split on whitespace into pieces of about `size` characters that
    overlap a little, so a sentence cut at one edge is whole in the next."""
    text = text.strip()
    if not text:
        return []
    out, start = [], 0
    while start < len(text) and len(out) < max_chunks:
        end = start + size
        if end < len(text):
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(" ", start + size // 2, end))
            end = cut if cut > start else end
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        while start < end and not text[start - 1].isspace():  # begin on a word boundary
            start += 1
    return out
