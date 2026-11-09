"""Text out of a raw RFC 822 message: the body, and each attachment.

No Django here: `index_message` runs in worker processes. Everything is
local; images and scanned PDFs are only flagged here (status "ocr") and
handed to the local model by ocr.py later.

An attachment is a leaf part the same rule as parse.parse_headers counts:
disposition "attachment", or a filename without disposition "inline".
`part` is its position among those in walk() order, so ocr.py can find
the same bytes again.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import subprocess
import tempfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

MAX_TEXT = 200_000  # chars kept per body or attachment
MAX_EXTRACT_BYTES = 50 * 1024 * 1024  # bigger attachments are listed, not read
MIN_IMAGE_BYTES = 8 * 1024  # smaller images are logos, spacers, signatures
MIN_IMAGE_SIDE = 120  # pixels
PDF_TEXT_PER_PAGE = 10  # fewer chars per page than this: a scan, so OCR it
MAX_OCR_PAGES = 5

# Statuses of an attachment's text.
TEXT, OCR, SKIPPED, ERROR = "text", "ocr", "skipped", "error"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
PLAIN_EXT = {".txt", ".csv", ".tsv", ".md", ".ics", ".vcf", ".json", ".xml", ".log", ".eml"}
TEXTUTIL_EXT = {".doc", ".rtf", ".odt", ".rtfd", ".wordml"}  # macOS textutil reads these


def read_raw(root: Path, rel: str) -> bytes:
    return gzip.decompress((root / rel).read_bytes())


def index_file(root: str, rel: str) -> dict:
    """index_message for a stored raw file; never raises."""
    try:
        raw = read_raw(Path(root), rel)
    except FileNotFoundError:
        return {"body": "", "body_error": "raw file missing", "attachments": []}
    except Exception as exc:
        return {"body": "", "body_error": f"read: {type(exc).__name__}: {exc}"[:500], "attachments": []}
    return index_message(raw)


def index_message(raw: bytes) -> dict:
    """{"body", "body_error", "attachments": [{part, filename, mime_type,
    size, sha256, status, method, text, error}]}. Never raises."""
    out = {"body": "", "body_error": "", "attachments": []}
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        out["body_error"] = f"parse: {type(exc).__name__}: {exc}"[:500]
        return out
    try:
        out["body"] = clean(body_text(msg))[:MAX_TEXT]
    except Exception as exc:
        out["body_error"] = f"body: {type(exc).__name__}: {exc}"[:500]
    try:
        for i, part in enumerate(attachment_parts(msg)):
            out["attachments"].append(describe(i, part))
    except Exception as exc:
        out["body_error"] = (out["body_error"] + f"; attachments: {type(exc).__name__}: {exc}").strip("; ")[:500]
    return out


def attachment_parts(msg):
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        if disp == "attachment" or (disp != "inline" and part.get_filename()):
            yield part


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        text = part_text(part)
        if text.strip():
            return text
    part = msg.get_body(preferencelist=("html",))
    return html_to_text(part_text(part)) if part is not None else ""


def part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")


def part_bytes(part) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:  # message/rfc822 and friends
        payload = part.as_bytes()
    return payload


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return "".join(p.out)


def clean(text: str) -> str:
    """Collapse whitespace runs, drop NULs and lone surrogates (SQLite
    rejects both)."""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n[ \n]*\n", "\n\n", text)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return text.strip()


def describe(i: int, part) -> dict:
    filename = clean(str(part.get_filename() or ""))[:500]
    mime = part.get_content_type()
    data = part_bytes(part)
    att = {"part": i, "filename": filename, "mime_type": mime[:200], "size": len(data),
           "sha256": hashlib.sha256(data).hexdigest(), "status": SKIPPED, "method": "", "text": "", "error": ""}
    try:
        status, method, text, note = extract(mime, filename, data)
    except Exception as exc:
        status, method, text, note = ERROR, "", "", f"{type(exc).__name__}: {exc}"
    att.update(status=status, method=method, text=clean(text)[:MAX_TEXT], error=note[:500])
    return att


def kind(mime: str, filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if mime == "application/pdf" or ext == ".pdf":
        return "pdf"
    if mime.startswith("image/") or ext in IMAGE_EXT:
        return "image"
    if ext == ".docx" or "wordprocessingml" in mime:
        return "docx"
    if ext in (".xlsx", ".xlsm") or "spreadsheetml" in mime:
        return "xlsx"
    if ext == ".pptx" or "presentationml" in mime:
        return "pptx"
    if ext in TEXTUTIL_EXT or mime in ("application/msword", "application/rtf", "text/rtf",
                                        "application/vnd.oasis.opendocument.text"):
        return "textutil"
    if mime == "message/rfc822" or ext == ".eml":
        return "eml"
    if mime == "text/html" or ext in (".html", ".htm"):
        return "html"
    if mime.startswith("text/") or ext in PLAIN_EXT:
        return "plain"
    return "other"


def extract(mime: str, filename: str, data: bytes) -> tuple[str, str, str, str]:
    """(status, method, text, note) for one attachment's bytes."""
    k = kind(mime, filename)
    if k == "other":
        return SKIPPED, "", "", f"unsupported type {mime}"
    if len(data) > MAX_EXTRACT_BYTES:
        return SKIPPED, "", "", f"larger than {MAX_EXTRACT_BYTES // 2**20} MB"
    if k == "image":
        return image_status(data)
    if k == "pdf":
        text, pages = pdf_text(data)
        if pages and len(text.strip()) < PDF_TEXT_PER_PAGE * min(pages, MAX_OCR_PAGES):
            return OCR, "", text, f"scanned pdf, {pages} pages"
        return TEXT, "pdf", text, ""
    if k == "plain":
        return TEXT, "plain", decode(data), ""
    if k == "html":
        return TEXT, "html", html_to_text(decode(data)), ""
    if k == "eml":
        inner = index_message(data)
        names = [a["filename"] for a in inner["attachments"] if a["filename"]]
        text = inner["body"] + ("\n\nAttachments: " + ", ".join(names) if names else "")
        return TEXT, "eml", text, ""
    return TEXT, k, {"docx": docx_text, "xlsx": xlsx_text, "pptx": pptx_text, "textutil": textutil_text}[k](
        data, filename), ""


def decode(data: bytes) -> str:
    for enc in ("utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def image_status(data: bytes) -> tuple[str, str, str, str]:
    if len(data) < MIN_IMAGE_BYTES:
        return SKIPPED, "", "", "image too small"
    try:
        img = open_image(data)
    except Exception as exc:
        return ERROR, "", "", f"unreadable image: {type(exc).__name__}"
    if min(img.size) < MIN_IMAGE_SIDE:
        return SKIPPED, "", "", f"image too small ({img.size[0]}x{img.size[1]})"
    return OCR, "", "", ""


def open_image(data: bytes):
    from PIL import Image

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def pdf_text(data: bytes) -> tuple[str, int]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        pages = []
        for page in pdf:
            tp = page.get_textpage()
            pages.append(tp.get_text_bounded())
            tp.close()
            page.close()
            if sum(map(len, pages)) > MAX_TEXT:
                break
        return "\n\n".join(pages), len(pdf)
    finally:
        pdf.close()


def pdf_page_images(data: bytes, max_pages: int = MAX_OCR_PAGES, scale: float = 150 / 72) -> list:
    """PIL images of the first `max_pages` pages, for OCR."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        return [pdf[i].render(scale=scale).to_pil() for i in range(min(len(pdf), max_pages))]
    finally:
        pdf.close()


def docx_text(data: bytes, filename: str = "") -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            lines.append("\t".join(c.text for c in row.cells))
    return "\n".join(lines)


def xlsx_text(data: bytes, filename: str = "") -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines, size = [], 0
    try:
        for ws in wb.worksheets:
            lines.append(f"# {ws.title}")
            for row in ws.iter_rows(values_only=True):
                line = "\t".join("" if v is None else str(v) for v in row).rstrip()
                if line:
                    lines.append(line)
                    size += len(line)
                if size > MAX_TEXT:
                    return "\n".join(lines)
    finally:
        wb.close()
    return "\n".join(lines)


def pptx_text(data: bytes, filename: str = "") -> str:
    import pptx

    deck = pptx.Presentation(io.BytesIO(data))
    lines = []
    for n, slide in enumerate(deck.slides, 1):
        lines.append(f"# Slide {n}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines.append(shape.text_frame.text)
    return "\n".join(lines)


def textutil_text(data: bytes, filename: str = "") -> str:
    """Old Word, RTF and ODT through macOS textutil."""
    ext = os.path.splitext(filename.lower())[1] or ".doc"
    with tempfile.TemporaryDirectory(prefix="mailindex-") as d:
        src = Path(d) / f"in{ext}"
        src.write_bytes(data)
        done = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(src)],
                              capture_output=True, timeout=60)
        if done.returncode:
            raise RuntimeError(f"textutil exit {done.returncode}: {done.stderr.decode()[:200]}")
        return decode(done.stdout)


def find_attachment(raw: bytes, part: int) -> tuple[str, str, bytes]:
    """(mime type, filename, bytes) of attachment number `part`."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    for i, p in enumerate(attachment_parts(msg)):
        if i == part:
            return p.get_content_type(), str(p.get_filename() or ""), part_bytes(p)
    raise LookupError(f"no attachment {part}")
