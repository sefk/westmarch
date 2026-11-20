"""The local model server, for embeddings and structured chat.

Anything OpenAI-compatible on this machine (LM Studio, ds4). Mail never
leaves the machine: a non-loopback URL is refused outright, here and in
ocr.py, which shares the check.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import requests

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class LlmUnavailable(Exception):
    """The model server is down or misconfigured; stop this pass, retry next run."""


def check_local(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise LlmUnavailable(f"refusing non-local model server {url!r}; mail stays on this machine")
    return url.rstrip("/")


def model_slug(model: str) -> str:
    """A model id as a file-name part: 'text-embedding-nomic-embed-text-v1.5' -> same, '/' -> '-'."""
    return re.sub(r"[^a-z0-9.]+", "-", model.lower()).strip("-")


def embed_prefixes(model: str) -> tuple[str, str]:
    """(document prefix, query prefix). nomic-embed was trained with these task
    prefixes and does noticeably worse without them; other models get none."""
    if "nomic" in model.lower():
        return "search_document: ", "search_query: "
    return "", ""


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, tolerating code fences and chatter."""
    text = strip_thinking(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object in the model's reply")


class LocalLlm:
    def __init__(self, url: str, model: str, timeout: float = 600, session=None):
        self.url = check_local(url)
        self.model = model
        self.timeout = timeout
        self.http = session or requests.Session()

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self.http.post(f"{self.url}{path}", json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LlmUnavailable(f"{self.url}: {type(exc).__name__}") from None
        if r.status_code in (404, 502, 503):
            raise LlmUnavailable(f"{self.url}: HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", {"model": self.model, "input": texts})["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]

    def chat_json(self, system: str, user: str, schema: dict, max_tokens: int = 1500) -> tuple[dict, dict]:
        """(parsed JSON reply, usage). Thinking is switched off: these are
        extraction jobs, and a thinking model spends its whole budget musing."""
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "digest", "strict": True, "schema": schema}},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        reply = self._post("/chat/completions", body)
        text = reply["choices"][0]["message"].get("content") or ""
        return parse_json(text), reply.get("usage") or {}
