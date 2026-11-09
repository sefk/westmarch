"""Text extraction on generated messages and documents."""

import pytest

from mailindex import extract
from mailindex.ocr import LocalOcr, OcrUnavailable

from .fixtures import docx_bytes, email, picture, pptx_bytes, scanned_pdf, text_pdf, xlsx_bytes


def one(raw):
    r = extract.index_message(raw)
    assert r["body_error"] == ""
    return r


def test_plain_body_preferred_over_html():
    r = one(email(1, body="plain words here", html="<p>html words</p>"))
    assert r["body"] == "plain words here"
    assert r["attachments"] == []


def test_html_only_body_is_stripped():
    r = one(email(2, html="<html><head><style>p{}</style></head><body><p>Hello&nbsp;there</p>"
                          "<script>x()</script><div>second line</div></body></html>"))
    assert r["body"] == "Hello there\n\nsecond line"


def test_document_attachments():
    r = one(email(3, body="see attached", attachments=[
        ("report.pdf", "application", "pdf", text_pdf("Quarterly walrus report")),
        ("notes.docx", "application", "vnd.openxmlformats-officedocument.wordprocessingml.document",
         docx_bytes("docx narwhal paragraph")),
        ("budget.xlsx", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         xlsx_bytes("xlsx pelican row")),
        ("deck.pptx", "application", "vnd.openxmlformats-officedocument.presentationml.presentation",
         pptx_bytes("pptx heron title")),
        ("data.csv", "text", "csv", b"a,b\nplatypus,1\n"),
        ("page.html", "text", "html", b"<b>bold</b> ocelot"),
        ("archive.zip", "application", "zip", b"PK\x03\x04junk"),
    ]))
    by_name = {a["filename"]: a for a in r["attachments"]}
    assert [a["part"] for a in r["attachments"]] == list(range(7))
    assert "walrus" in by_name["report.pdf"]["text"] and by_name["report.pdf"]["method"] == "pdf"
    assert "narwhal" in by_name["notes.docx"]["text"] and "cellB" in by_name["notes.docx"]["text"]
    assert "# Budget" in by_name["budget.xlsx"]["text"] and "pelican row 42" in by_name["budget.xlsx"]["text"]
    assert "heron" in by_name["deck.pptx"]["text"]
    assert "platypus" in by_name["data.csv"]["text"]
    assert by_name["page.html"]["text"] == "bold ocelot"
    assert by_name["archive.zip"]["status"] == "skipped" and "unsupported" in by_name["archive.zip"]["error"]
    for name in ("report.pdf", "notes.docx", "budget.xlsx", "deck.pptx", "data.csv", "page.html"):
        assert by_name[name]["status"] == "text", name
        assert len(by_name[name]["sha256"]) == 64


def test_rtf_through_textutil():
    rtf = rb"{\rtf1\ansi {\fonttbl\f0 Helvetica;}\f0 rtf armadillo text}"
    r = one(email(4, body="x", attachments=[("old.rtf", "application", "rtf", rtf)]))
    assert "armadillo" in r["attachments"][0]["text"]


def test_attached_email_is_read():
    inner = email(99, body="inner capybara message", attachments=[("x.txt", "text", "plain", b"hi")])
    r = one(email(5, body="fwd", attachments=[("fwd.eml", "application", "octet-stream", inner)]))
    a = r["attachments"][0]
    assert a["method"] == "eml" and "capybara" in a["text"] and "x.txt" in a["text"]


def test_images_and_scans_wait_for_ocr():
    r = one(email(6, body="pics", attachments=[
        ("photo.png", "image", "png", picture()),
        ("logo.png", "image", "png", picture(size=(60, 20))),
        ("scan.pdf", "application", "pdf", scanned_pdf()),
        ("broken.jpg", "image", "jpeg", b"\xff\xd8" + b"x" * 20000),
    ]))
    status = {a["filename"]: (a["status"], a["error"]) for a in r["attachments"]}
    assert status["photo.png"] == ("ocr", "")
    assert status["logo.png"][0] == "skipped"
    assert status["scan.pdf"] == ("ocr", "scanned pdf, 1 pages")
    assert status["broken.jpg"][0] == "error"


def test_find_attachment_returns_same_bytes():
    pic = picture()
    raw = email(7, body="x", attachments=[("a.txt", "text", "plain", b"aa"), ("p.png", "image", "png", pic)])
    assert extract.find_attachment(raw, 1) == ("image/png", "p.png", pic)
    with pytest.raises(LookupError):
        extract.find_attachment(raw, 2)


def test_garbage_never_raises():
    r = extract.index_message(b"\x00\x01 not an email at all")
    assert r["attachments"] == []


def test_clean_strips_nul_and_surrogates():
    assert extract.clean("a\x00b \udcff  c\n\n\n\nd") == "ab � c\n\nd"


def test_ocr_refuses_remote_servers():
    with pytest.raises(OcrUnavailable, match="non-local"):
        LocalOcr("https://api.example.com/v1", "m")
    assert LocalOcr("http://127.0.0.1:8765/v1/", "m").url == "http://127.0.0.1:8765/v1"


def test_ocr_request_shape():
    from PIL import Image

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "<think>hmm</think>\nLINE ONE"}}]}

    class Session:
        def post(self, url, json, timeout):
            self.url, self.body = url, json
            return Resp()

    s = Session()
    ocr = LocalOcr("http://localhost:1234/v1", "vis", session=s)
    assert ocr.image_text(Image.new("RGB", (3000, 100))) == "LINE ONE"
    assert s.url == "http://localhost:1234/v1/chat/completions"
    assert s.body["model"] == "vis"
    assert s.body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
