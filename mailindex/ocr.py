"""OCR through the local model (ds4 by default; any OpenAI-compatible
server on this machine). Mail never leaves the machine: a non-loopback
URL is refused outright.
"""

from __future__ import annotations

import base64
import io
import re
from urllib.parse import urlparse

import requests

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_SIDE = 1600  # px; larger images are scaled down before sending

PROMPT = (
    "Transcribe all legible text in this image exactly, keeping line breaks. "
    "Output only the transcription, with no commentary. "
    "If the image contains no text, output one short sentence describing it, starting with 'Image: '."
)


class OcrUnavailable(Exception):
    """The model server is down or misconfigured; stop the OCR pass."""


def check_local(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise OcrUnavailable(f"refusing non-local OCR server {url!r}; mail stays on this machine")
    return url.rstrip("/")


def png_b64(img) -> str:
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


class LocalOcr:
    def __init__(self, url: str, model: str, timeout: float = 300, session=None):
        self.url = check_local(url)
        self.model = model
        self.timeout = timeout
        self.http = session or requests.Session()

    @property
    def method(self) -> str:
        return f"ocr:{self.model}"

    def image_text(self, img) -> str:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4096,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64(img)}"}},
            ]}],
        }
        try:
            r = self.http.post(f"{self.url}/chat/completions", json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OcrUnavailable(f"{self.url}: {type(exc).__name__}") from None
        if r.status_code in (404, 502, 503):
            raise OcrUnavailable(f"{self.url}: HTTP {r.status_code}")
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
