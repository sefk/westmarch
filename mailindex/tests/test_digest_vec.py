"""Thread digests, the embedding store and search by meaning, over made-up
mail and a fake local model. No real mail, no model server."""

import hashlib
import math
import re
from datetime import UTC, datetime

import pytest
import requests
from django.core.management import call_command

from core.loaders import Disabled
from core.models import LoaderState
from mailindex import chunks
from mailindex.digest import MailDigestLoader, pending_threads, render_thread, thread_messages
from mailindex.llm import LlmUnavailable, LocalLlm, embed_prefixes, model_slug, parse_json
from mailindex.loader import MailIndexLoader
from mailindex.models import ThreadDigest
from mailindex.search import hybrid, meaning
from mailindex.vecloader import MailVecLoader
from mailindex.vecstore import VecStore, store_path

from .fixtures import FakeOcr, email, stored, text_pdf

pytestmark = pytest.mark.django_db

EMBED = "text-embedding-nomic-embed-text-v1.5"
DIGEST = "fake-digester"


class FakeLlm:
    """Bag-of-words vectors (so shared words mean nearness) and a canned digest."""

    def __init__(self, dims=64, down=False, refuse=None):
        self.dims, self.down, self.refuse = dims, down, refuse
        self.embedded, self.prompts = [], []

    def embed(self, texts):
        if self.down:
            raise LlmUnavailable("down")
        if self.refuse and any(self.refuse in t for t in texts):
            raise requests.HTTPError("400 too long")
        self.embedded.extend(texts)
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for w in re.findall(r"[a-z]+", t.lower()):
                if w in ("search", "document", "query", "subject", "from", "attachment", "in", "email"):
                    continue
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dims] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1
            out.append([x / n for x in v])
        return out

    def chat_json(self, system, user, schema, max_tokens=1500):
        if self.down:
            raise LlmUnavailable("down")
        self.prompts.append((system, user))
        if "EXPLODE" in user:
            raise ValueError("no JSON object in the model's reply")
        subject = re.search(r"Subject: (.*)", user).group(1)
        return ({"summary": f"About {subject}", "topic": "home", "people": ["Alice Example"],
                 "organizations": [], "dates": [], "amounts": [], "commitments": [],
                 "open_question": "", "keywords": ["porcupine"]},
                {"prompt_tokens": len(user) // 4, "completion_tokens": 40})


@pytest.fixture
def root(settings, tmp_path):
    settings.RAW_DIR = tmp_path / "raw"
    settings.RAW_DIR.mkdir()
    settings.MAILINDEX_VEC_DIR = tmp_path / "raw" / "mailindex"
    settings.BASE_DIR = tmp_path
    settings.MAILDIGEST_MODEL = DIGEST
    settings.MAILVEC_MODEL = EMBED
    return settings.RAW_DIR


def msg(root, n, body, thread=None, labels=(), sent=False, list_id="", minute=None, attachments=()):
    m = stored(root, n, email(n, body=body, attachments=attachments))
    m.thread_id = thread or m.thread_id
    m.label_ids = list(labels) + (["SENT"] if sent else [])
    m.list_id = list_id
    m.from_address = "me@example.org" if sent else f"alice{n}@example.com"
    m.from_name = "Me Myself" if sent else "Alice Example"
    m.internal_date = datetime(2026, 2, 1, 10, minute if minute is not None else n, tzinfo=UTC)
    m.subject = f"Subject {n}"
    m.save()
    return m


@pytest.fixture
def mailbox(root):
    """t-roof: a thread I wrote in. t-news: a newsletter. t-promo: a promotion.
    t-friend: personal mail I never answered."""
    msg(root, 1, "the roof is leaking near the chimney, can you come look", thread="t-roof")
    msg(root, 2, "I can come Tuesday.\n\nOn Mon, Feb 2, 2026 at 9:00 AM Alice <a@example.com> wrote:\n> the roof"
        " is leaking", thread="t-roof", sent=True)
    msg(root, 3, "weekly newsletter about gardening tomatoes", thread="t-news", list_id="<news.example.com>")
    msg(root, 4, "fifty percent off kayaks", thread="t-promo", labels=["CATEGORY_PROMOTIONS"])
    msg(root, 5, "want to go birdwatching saturday", thread="t-friend", attachments=[
        ("map.pdf", "application", "pdf", text_pdf("trailhead parking ostrich"))])
    msg(root, 6, "spam about kayaks", thread="t-spam", labels=["SPAM"])
    MailIndexLoader(budget_seconds=0, workers=0, ocr_client=FakeOcr(), enabled=True).load()
    return root


# -- text helpers --


def test_own_text_cuts_quoted_mail():
    reply = "Sounds good, see you then.\n\nOn Tue, Feb 3, 2026 at 10:00 AM Alice Example <\na@x.com> wrote:\n> old"
    assert chunks.own_text(reply) == "Sounds good, see you then."
    outlook = "Approved.\n\n-----Original Message-----\nFrom: Bob\nSent: Monday\nold text"
    assert chunks.own_text(outlook) == "Approved."
    bottom = "On Tue, Bob wrote:\n> question?\nanswer"  # nothing above the quote: keep it all
    assert chunks.own_text(bottom) == bottom


def test_chunk_overlaps_and_caps():
    text = " ".join(f"word{i}" for i in range(2000))
    pieces = chunks.chunk(text, size=500, overlap=100, max_chunks=4)
    assert len(pieces) == 4 and all(len(p) <= 500 for p in pieces)
    assert pieces[1].split()[0] in pieces[0]  # overlap
    assert chunks.chunk("  ") == [] and chunks.chunk("short") == ["short"]


def test_llm_helpers():
    with pytest.raises(LlmUnavailable, match="non-local"):
        LocalLlm("http://192.168.1.5:1234/v1", "m")
    assert parse_json('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert embed_prefixes(EMBED) == ("search_document: ", "search_query: ")
    assert embed_prefixes("qwen3-embedding") == ("", "")
    assert model_slug("qwen/qwen3.6-35b-a3b") == "qwen-qwen3.6-35b-a3b"


# -- digests --


def test_digests_only_my_threads_and_redigest_on_new_mail(mailbox):
    assert [t for t, _, _ in pending_threads(DIGEST)] == ["t-roof"]
    llm = FakeLlm()
    assert MailDigestLoader(budget_seconds=0, client=llm, enabled=True).load() == 1
    d = ThreadDigest.objects.get(thread_id="t-roof", model=DIGEST)
    assert d.summary == "About Subject 1" and d.topic == "home" and d.message_count == 2
    assert d.prompt_tokens and d.digest["keywords"] == ["porcupine"]
    system, user = llm.prompts[0]
    assert "Me Myself <me@example.org>" in system
    assert "(me)" in user and "I can come Tuesday." in user and "> the roof" not in user  # quote cut off
    assert pending_threads(DIGEST) == []

    msg(mailbox, 7, "Tuesday works, thanks", thread="t-roof", minute=30)
    assert [t for t, _, _ in pending_threads(DIGEST)] == ["t-roof"]
    MailDigestLoader(budget_seconds=0, client=llm, enabled=True).load()
    assert ThreadDigest.objects.get(thread_id="t-roof").message_count == 3
    assert LoaderState.get("maildigest", "digests") == "1"


def test_digest_failures_retry_then_stop(mailbox):
    msg(mailbox, 8, "EXPLODE", thread="t-boom", sent=True)
    MailIndexLoader(budget_seconds=0, workers=0, ocr=False, enabled=True).load()
    for _ in range(3):
        MailDigestLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    bad = ThreadDigest.objects.get(thread_id="t-boom")
    assert bad.attempts == 3 and "no JSON" in bad.error
    assert pending_threads(DIGEST) == []

    # New mail in the thread gives it a fresh set of tries.
    msg(mailbox, 9, "all sorted now", thread="t-boom", minute=50)
    assert [t for t, _, _ in pending_threads(DIGEST)] == ["t-boom"]
    MailDigestLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    assert ThreadDigest.objects.get(thread_id="t-boom").attempts == 1


def test_digest_server_down_and_switch(settings, mailbox):
    MailDigestLoader(budget_seconds=0, client=FakeLlm(down=True), enabled=True).load()
    assert LoaderState.get("maildigest", "stopped") == "down" and not ThreadDigest.objects.exists()
    settings.MAILDIGEST_ENABLED = False
    with pytest.raises(Disabled, match="MAILDIGEST_ENABLED"):
        MailDigestLoader(client=FakeLlm()).load()


def test_long_thread_keeps_first_and_newest(root):
    for n in range(1, 9):
        msg(root, n, f"message number {n} " + "x" * 900, thread="t-long", sent=n % 2 == 0)
    MailIndexLoader(budget_seconds=0, workers=0, ocr=False, enabled=True).load()
    text = render_thread(thread_messages("t-long"), {"me@example.org"}, max_chars=3500)
    assert "message number 1" in text and "message number 8" in text
    assert "messages left out" in text and "message number 3" not in text
    assert len(text) <= 3500


# -- embeddings --


def test_vec_embeds_personal_mail_digests_and_attachments(settings, mailbox):
    MailDigestLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    llm = FakeLlm()
    MailVecLoader(budget_seconds=0, client=llm, enabled=True).load()
    store = VecStore(store_path(settings.MAILINDEX_VEC_DIR, EMBED), readonly=True)
    counts = store.counts()
    # Personal: t-roof (2) and t-friend (1); not the newsletter, promotion or spam.
    assert counts["done_body"] == 3 and counts["done_digest"] == 1 and counts["done_att"] == 1
    assert all(t.startswith("search_document: ") for t in llm.embedded)
    assert not any("kayaks" in t or "tomatoes" in t for t in llm.embedded)
    assert not any("> the roof" in t for t in llm.embedded)  # quoted copy cut off
    assert LoaderState.get("mailvec", "store") == str(store.path)

    # Nothing left: a rerun embeds nothing.
    again = FakeLlm()
    MailVecLoader(budget_seconds=0, client=again, enabled=True).load()
    assert again.embedded == []

    # A rewritten digest is re-embedded, replacing its old chunk.
    d = ThreadDigest.objects.get()
    d.summary = "About the chimney repair"
    d.save()
    MailVecLoader(budget_seconds=0, client=again, enabled=True).load()
    assert len(again.embedded) == 1 and "chimney repair" in again.embedded[0]
    assert VecStore(store.path, readonly=True).counts()["chunks_digest"] == 1


def test_vec_forgets_deleted_and_trashed_mail(settings, mailbox):
    from django.utils import timezone

    from sources.google.models import GmailMessage

    MailVecLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    path = store_path(settings.MAILINDEX_VEC_DIR, EMBED)
    assert VecStore(path, readonly=True).counts()["done_body"] == 3
    GmailMessage.objects.filter(thread_id="t-roof", label_ids=[]).update(deleted_at=timezone.now())
    friend = GmailMessage.objects.get(thread_id="t-friend")
    friend.label_ids = ["TRASH"]
    friend.save()
    MailVecLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    counts = VecStore(path, readonly=True).counts()
    assert counts["done_body"] == 1 and "done_att" not in counts  # the map was only in the trashed mail
    assert LoaderState.get("mailvec", "pruned") == "3"
    assert [h["thread_id"] for h in meaning("birdwatching chimney", k=10, client=FakeLlm())] == ["t-roof"]


def test_attachment_belongs_to_the_message_in_scope(settings, root):
    pdf = text_pdf("lighthouse tickets")
    msg(root, 1, "promo", thread="t-promo", labels=["CATEGORY_PROMOTIONS"], attachments=[
        ("a.pdf", "application", "pdf", pdf)])
    msg(root, 2, "for you", thread="t-mine", sent=True, attachments=[("a.pdf", "application", "pdf", pdf)])
    MailIndexLoader(budget_seconds=0, workers=0, ocr=False, enabled=True).load()
    MailVecLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    hit = meaning("lighthouse tickets", k=1, client=FakeLlm())[0]
    assert hit["kind"] == "att" and hit["thread_id"] == "t-mine"  # not the promo it was first extracted from


def test_vec_scope_all_and_refused_text(settings, mailbox):
    MailVecLoader(budget_seconds=0, client=FakeLlm(refuse="birdwatching"), enabled=True, scope="all").load()
    counts = VecStore(store_path(settings.MAILINDEX_VEC_DIR, EMBED), readonly=True).counts()
    assert counts["done_body"] == 5  # everything but spam, one of them refused
    assert LoaderState.get("mailvec", "skipped") == "1"


def test_vec_stops_when_server_is_down(settings, mailbox):
    MailVecLoader(budget_seconds=0, client=FakeLlm(down=True), enabled=True).load()
    assert LoaderState.get("mailvec", "stopped") == "down"
    settings.MAILVEC_ENABLED = False
    with pytest.raises(Disabled, match="MAILVEC_ENABLED"):
        MailVecLoader(client=FakeLlm()).load()


def test_store_refuses_a_different_model(settings, tmp_path):
    VecStore(tmp_path / "v.sqlite3", "model-a").close()
    with pytest.raises(ValueError, match="model-a"):
        VecStore(tmp_path / "v.sqlite3", "model-b")


# -- search --


def test_meaning_and_hybrid_search(settings, mailbox):
    MailDigestLoader(budget_seconds=0, client=FakeLlm(), enabled=True).load()
    llm = FakeLlm()
    MailVecLoader(budget_seconds=0, client=llm, enabled=True).load()

    hits = meaning("chimney leaking", k=3, client=llm)
    assert hits[0]["thread_id"] == "t-roof"
    assert llm.embedded[-1] == "search_query: chimney leaking"
    assert meaning("ostrich parking", k=1, client=llm)[0]["kind"] == "att"
    assert meaning("chimney", k=5, client=llm, before=20260101) == []  # date filter inside the search

    ranked = hybrid("roof leaking", limit=5, client=llm)
    assert ranked[0]["thread_id"] == "t-roof" and "keyword" in ranked[0]["how"]
    assert ranked[0]["how"] != ["keyword"]  # meaning agreed


def test_commands_run(settings, mailbox, capsys, monkeypatch):
    llm = FakeLlm()
    monkeypatch.setattr("mailindex.digest.LocalLlm", lambda *a, **k: llm)
    monkeypatch.setattr("mailindex.vecloader.LocalLlm", lambda *a, **k: llm)
    monkeypatch.setattr("mailindex.llm.LocalLlm", lambda *a, **k: llm)
    call_command("maildigest", "--thread", "t-roof")
    call_command("maildigest", "--model", "other-model", "--limit", "5")
    call_command("mailvec", "--limit", "10")
    call_command("maildigest", "--stats")
    call_command("maildigest", "--compare", DIGEST, "other-model", "--sample", "1")
    call_command("mailsearch", "--meaning", "roof")
    out = capsys.readouterr().out
    assert "About Subject 1" in out and "both: 1" in out and "t-roof" in out
