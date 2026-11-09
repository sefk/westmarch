"""Made-up messages and documents, generated on the fly. No real mail."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from email.message import EmailMessage

from sources.google.models import GmailMessage
from sources.google.parse import write_raw


def text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, written by hand."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def picture(text: str = "HELLO", size=(800, 400), fmt="PNG") -> bytes:
    """A noisy image with some text drawn on it; noisy so it's not 'too small'."""
    from PIL import Image, ImageDraw

    img = Image.effect_noise(size, 60).convert("RGB")
    ImageDraw.Draw(img).text((40, 40), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def scanned_pdf() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(picture("SCANNED"))).save(buf, format="PDF")
    return buf.getvalue()


def docx_bytes(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "cellA", "cellB"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx_bytes(text: str) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Budget"
    wb.active.append(["item", "amount"])
    wb.active.append([text, 42])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pptx_bytes(text: str) -> bytes:
    import pptx

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = text
    buf = io.BytesIO()
    deck.save(buf)
    return buf.getvalue()


def email(n: int, body: str = "", html: str = "", attachments=()) -> bytes:
    """attachments: (filename, maintype, subtype, bytes)."""
    m = EmailMessage()
    m["From"] = f"Alice Example <alice{n}@example.com>"
    m["To"] = "bob@example.org"
    m["Subject"] = f"Test message {n}"
    m["Date"] = f"Tue, 03 Feb 2026 10:{n % 60:02d}:00 -0800"
    if body:
        m.set_content(body)
        if html:
            m.add_alternative(html, subtype="html")
    elif html:
        m.set_content(html, subtype="html")
    else:
        m.set_content("")
    for filename, maintype, subtype, data in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


def stored(root, n: int, raw: bytes) -> GmailMessage:
    gid = f"{n:016x}"
    rel = write_raw(root, gid, raw)
    return GmailMessage.objects.create(gmail_id=gid, thread_id=gid, raw_path=rel, subject=f"Test message {n}",
                                       internal_date=datetime(2026, 2, 1, tzinfo=UTC).replace(minute=n % 60))


class FakeOcr:
    method = "ocr:fake"

    def __init__(self, fail_with=None):
        self.calls = 0
        self.fail_with = fail_with

    def image_text(self, img):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return f"transcribed zebra {img.size[0]}x{img.size[1]}"
