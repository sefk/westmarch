import sqlite3
import sys
from datetime import datetime, timezone

import pytest
from django.test import override_settings

from core.loaders import Disabled, run_loader
from core.models import LoaderState
from sources.imessage.loader import IMessageLoader, apple_time, decode_attributed_body
from sources.imessage.models import Attachment, Chat, Message

pytestmark = pytest.mark.django_db

EPOCH = 978307200


def ns(dt):
    return int((dt.timestamp() - EPOCH) * 1e9)


def blob(text: str, form="short") -> bytes:
    raw = text.encode()
    pre = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
    if len(raw) < 0x80:
        ln = bytes([len(raw)])
    elif len(raw) < 0x10000:
        ln = b"\x81" + len(raw).to_bytes(2, "little")
    else:
        ln = b"\x82" + len(raw).to_bytes(4, "little")
    return pre + ln + raw + b"\x86\x84\x02iI\x01"


def make_db(path, old=False):
    con = sqlite3.connect(path)
    extra = "" if old else ", attributedBody BLOB, date_edited INT, date_retracted INT, thread_originator_guid TEXT"
    con.executescript(f"""
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT, style INT, service_name TEXT);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, handle_id INT, service TEXT,
      date INT, date_read INT, date_delivered INT, is_from_me INT, is_read INT, cache_has_attachments INT,
      associated_message_guid TEXT, associated_message_type INT, item_type INT, group_title TEXT{extra});
    CREATE TABLE chat_message_join (chat_id INT, message_id INT);
    CREATE TABLE chat_handle_join (chat_id INT, handle_id INT);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT, transfer_name TEXT, total_bytes INT);
    CREATE TABLE message_attachment_join (message_id INT, attachment_id INT);
    INSERT INTO handle VALUES (1,'+15550001','iMessage'),(2,'a@example.com','iMessage');
    INSERT INTO chat VALUES (1,'chat-1to1','+15550001','',45,'iMessage'),(2,'chat-grp','chat123','Hikers',43,'iMessage');
    INSERT INTO chat_handle_join VALUES (1,1),(2,1),(2,2);
    """)
    return con


def add(con, rowid, chat, text="hi", body=None, date=None, handle=1, from_me=0, assoc=None, atype=0, att=0, **kw):
    date = date if date is not None else ns(datetime(2020, 1, rowid, tzinfo=timezone.utc))
    cols = {"ROWID": rowid, "guid": f"g{rowid}", "text": text, "handle_id": handle, "service": "iMessage",
            "date": date, "is_from_me": from_me, "cache_has_attachments": att,
            "associated_message_guid": assoc, "associated_message_type": atype, "item_type": 0, **kw}
    if body is not None:
        cols["attributedBody"] = body
    con.execute(f"INSERT INTO message ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
    con.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, rowid))
    con.commit()


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "chat.db"
    with override_settings(IMESSAGE_DB=p):
        yield make_db(p)


def test_decode_blobs():
    assert decode_attributed_body(blob("hello")) == "hello"
    assert decode_attributed_body(blob("é" * 100)) == "é" * 100
    assert decode_attributed_body(blob("x" * 70000)) == "x" * 70000
    assert decode_attributed_body(b"garbage") == ""
    assert decode_attributed_body(None) == ""
    assert decode_attributed_body(blob("hello")[:-6]) == "hello"  # trailer irrelevant
    assert decode_attributed_body(blob("hello")[:-10]) == ""  # truncated


def test_apple_time():
    assert apple_time(0) is None
    assert apple_time(86400) == datetime(2001, 1, 2, tzinfo=timezone.utc)
    assert apple_time(86400 * 10**9) == datetime(2001, 1, 2, tzinfo=timezone.utc)


def test_load_everything(db):
    add(db, 1, 1, text="plain", date=ns(datetime(2020, 1, 1, tzinfo=timezone.utc)))
    add(db, 2, 1, text=None, body=blob("only in body"), date=86400 * 6940)  # seconds
    add(db, 3, 2, text="group msg", handle=2)
    add(db, 4, 2, text="Loved it", assoc="p:0/g3", atype=2000, from_me=1, handle=None)
    add(db, 5, 2, text="undo", assoc="p:0/g3", atype=3001)
    add(db, 6, 1, text="pic", att=1)
    add(db, 7, 1, text=None, body=b"junk")
    db.execute("INSERT INTO attachment VALUES (1,'ag','~/x.jpg','image/jpeg','x.jpg',123)")
    db.execute("INSERT INTO message_attachment_join VALUES (6,1)")
    db.commit()
    loader = IMessageLoader()
    assert loader.load() == 7
    m = {x.rowid: x for x in Message.objects.all()}
    assert m[1].sent_at == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert m[2].text == "only in body" and m[2].sent_at.year == 2020
    assert m[3].chat.is_group and m[3].chat.participants == ["+15550001", "a@example.com"]
    assert not m[1].chat.is_group
    assert m[4].reaction == "love" and m[4].text == "" and m[4].reply_to_guid == "g3" and m[4].handle is None
    assert m[5].reaction == "-like"
    assert m[6].has_attachments and m[6].attachment_count == 1
    assert Attachment.objects.get().mime_type == "image/jpeg"
    assert m[7].text == "" and loader.state_get("body_decode_failures") == "1"
    assert loader.state_get("last_rowid") == "7"


def test_incremental_and_edit(db):
    now = datetime.now(timezone.utc)
    add(db, 1, 1, text="old", date=ns(datetime(2020, 1, 1, tzinfo=timezone.utc)))
    add(db, 2, 1, text="recent", date=ns(now))
    loader = IMessageLoader()
    loader.load()
    # old row changes outside the window: ignored; recent row edited: picked up; new row: added
    db.execute("UPDATE message SET text='old edited' WHERE ROWID=1")
    db.execute("UPDATE message SET text='recent edited', date_edited=? WHERE ROWID=2", (ns(now),))
    add(db, 3, 1, text="new", date=ns(now))
    assert loader.load() == 2
    assert Message.objects.count() == 3
    assert Message.objects.get(rowid=1).text == "old"
    r = Message.objects.get(rowid=2)
    assert r.text == "recent edited" and r.edited_at is not None
    assert loader.load(full=True) == 3
    assert Message.objects.get(rowid=1).text == "old edited"
    assert Chat.objects.count() == 2


def test_old_schema(tmp_path):
    p = tmp_path / "old.db"
    with override_settings(IMESSAGE_DB=p):
        con = make_db(p, old=True)
        add(con, 1, 1, text="legacy", date=86400)
        assert IMessageLoader().load() == 1
    m = Message.objects.get()
    assert m.text == "legacy" and m.edited_at is None and m.reply_to_guid == ""


def test_disabled(tmp_path):
    with override_settings(IMESSAGE_DB=tmp_path / "nope" / "chat.db"):
        with pytest.raises(Disabled, match="Full Disk Access"):
            IMessageLoader().load()
        run = run_loader(IMessageLoader())
    assert run.ok and run.message.startswith("disabled:")
    import os
    assert os.path.realpath(sys.executable) in run.message
