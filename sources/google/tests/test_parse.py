from datetime import UTC, datetime

from sources.google.parse import parse_headers, raw_relpath, read_raw, write_raw

from .fakes import eml


def test_headers():
    h = parse_headers(eml(3))
    assert h["from_name"] == "Alice Example"
    assert h["from_address"] == "alice3@example.com"
    assert h["to"] == ["bob@example.org", "carol@example.net"]
    assert h["cc"] == ["dee@example.com"]
    assert h["subject"] == "Test message 3"
    assert h["date"] == datetime(2026, 2, 3, 18, 3, tzinfo=UTC)
    assert h["message_id"] == "<m3@example.com>"
    assert h["list_id"] == "Example List <list.example.com>"
    assert h["in_reply_to"] == ""
    assert not h["has_attachments"] and h["attachment_count"] == 0
    assert h["parse_error"] == ""


def test_attachments():
    h = parse_headers(eml(4, attach=True))
    assert h["has_attachments"] and h["attachment_count"] == 2


def test_encoded_subject():
    h = parse_headers(eml(5, subject="=?utf-8?q?Caf=C3=A9_menu?="))
    assert h["subject"] == "Café menu"


def test_malformed_message_never_raises():
    raw = (b"From: <<<not an address\r\nTo: ,,,@@\r\nDate: yesterday-ish\r\n"
           b"Subject: =?bogus?x?zzz?=\r\nContent-Type: multipart/mixed\r\n\r\nno boundary\r\n")
    h = parse_headers(raw)
    assert h["date"] is None
    assert h["parse_error"]  # the bad Date is reported
    assert isinstance(h["to"], list)


def test_garbage_bytes():
    h = parse_headers(b"\xff\xfe\x00 not mail at all")
    assert isinstance(h, dict) and "parse_error" in h


def test_naive_date_gets_utc():
    raw = b"From: a@example.com\r\nDate: Tue, 03 Feb 2026 10:00:00 -0000\r\n\r\nx"
    assert parse_headers(raw)["date"] == datetime(2026, 2, 3, 10, 0, tzinfo=UTC)


def test_raw_round_trip(tmp_path):
    rel = write_raw(tmp_path, "18c0ffee00000001", b"hello raw")
    assert rel == raw_relpath("18c0ffee00000001") == "gmail/18/18c0ffee00000001.eml.gz"
    assert read_raw(tmp_path, rel) == b"hello raw"
    assert (tmp_path / rel).read_bytes()[:2] == b"\x1f\x8b"  # gzip magic
    assert [p.name for p in (tmp_path / "gmail" / "18").iterdir()] == ["18c0ffee00000001.eml.gz"]  # no temp left
    write_raw(tmp_path, "18c0ffee00000001", b"replaced")
    assert read_raw(tmp_path, rel) == b"replaced"
