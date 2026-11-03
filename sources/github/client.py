"""Minimal GitHub REST client: auth, pagination, rate-limit sleeping."""

import subprocess
import time

import requests
from django.conf import settings

from core.loaders import Disabled

API = "https://api.github.com"


def get_token() -> str:
    if settings.GITHUB_TOKEN:
        return settings.GITHUB_TOKEN
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        out = None
    if not out or out.returncode or not out.stdout.strip():
        raise Disabled("no GITHUB_TOKEN and `gh auth token` failed")
    return out.stdout.strip()


class NotAccessible(Exception):
    """404/403/409 on one resource (gone, no access, empty repo)."""

    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


class Client:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.calls = 0
        self.remaining = None

    def _request(self, url, params):
        for _ in range(5):
            resp = self.session.get(url if url.startswith("http") else API + url, params=params, timeout=60)
            self.calls += 1
            rem = resp.headers.get("X-RateLimit-Remaining")
            self.remaining = int(rem) if rem is not None else self.remaining
            limited = resp.status_code in (403, 429) and (
                rem == "0" or "retry-after" in resp.headers or "rate limit" in resp.text.lower()
            )
            if limited:  # sleep until reset, then retry
                wait = float(resp.headers.get("retry-after") or
                             max(0, int(resp.headers.get("X-RateLimit-Reset", 0)) - time.time()) + 1)
                time.sleep(min(wait, 3700))
                continue
            if resp.status_code >= 500:
                time.sleep(2)
                continue
            if resp.status_code in (403, 404, 409, 451):
                raise NotAccessible(resp.status_code)
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"GitHub request kept failing: {url}")

    def get(self, path, **params):
        return self._request(path, params).json()

    def pages(self, path, **params):
        """Yield items across pages; handles search's {"items": [...]} shape."""
        params.setdefault("per_page", 100)
        url = path
        while url:
            resp = self._request(url, params)
            data = resp.json()
            yield from (data["items"] if isinstance(data, dict) else data)
            url = resp.links.get("next", {}).get("url")
            params = None  # the next URL carries its own query
