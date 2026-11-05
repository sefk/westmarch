"""Google OAuth: a Desktop client, one consent, a refreshable token file.

`manage.py google_auth` runs the browser consent once and writes
GOOGLE_TOKEN_FILE (mode 0600, under the gitignored secrets/). Loaders call
`load_credentials()` non-interactively; it refreshes an expired access
token and rewrites the file, and raises GoogleAuthError (which loaders
turn into Disabled) when there is no usable token.

Tokens and client secrets are never printed, logged, or put in an
exception message. Read-only scopes only (settings.GOOGLE_SCOPES).
"""

from __future__ import annotations

import json
import os
import time

from django.conf import settings

AUTH_HINT = "run: manage.py google_auth"


class GoogleAuthError(RuntimeError):
    pass


class NoToken(GoogleAuthError):
    pass


def token_age() -> str:
    """How long ago the refresh token was issued, for failure messages."""
    try:
        issued = json.loads(settings.GOOGLE_TOKEN_FILE.read_text()).get("issued_at")
    except (OSError, ValueError):
        issued = None
    if not issued:
        return "token age unknown"
    return f"token issued {(time.time() - issued) / 86400:.1f} days ago"


def load_credentials(interactive: bool = False, force: bool = False):
    """Valid credentials, refreshing (or, if interactive, re-consenting) as needed.
    `force` skips the stored token and always runs the consent flow."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = settings.GOOGLE_TOKEN_FILE
    scopes = settings.GOOGLE_SCOPES
    creds = None
    if token_file.exists() and not force:
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except (ValueError, KeyError):
            creds = None
            if not interactive:
                raise GoogleAuthError(f"unreadable Google token file; {AUTH_HINT}")
    if creds and creds.valid and creds.has_scopes(scopes):
        return creds
    if creds and creds.expired and creds.refresh_token and creds.has_scopes(scopes):
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except RefreshError as exc:
            if not interactive:
                # args[0] is Google's short reason (e.g. "invalid_grant: ..."); no secrets.
                reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else type(exc).__name__
                raise GoogleAuthError(f"Google token refresh failed ({reason}; {token_age()}); {AUTH_HINT}")
    if not interactive:
        if creds is None:
            raise NoToken(f"no Google token; {AUTH_HINT}")
        raise GoogleAuthError(f"Google token missing scopes or unusable; {AUTH_HINT}")

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = settings.GOOGLE_CLIENT_FILE
    if not client_file.exists():
        raise GoogleAuthError(f"no OAuth client file at {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save(creds, issued_at=time.time())
    return creds


def _save(creds, issued_at: float | None = None) -> None:
    """Write the token, 0600. `issued_at` stamps a fresh consent; a refresh keeps the old stamp."""
    path = settings.GOOGLE_TOKEN_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.loads(creds.to_json())
    if issued_at is None:
        try:
            issued_at = json.loads(path.read_text()).get("issued_at")
        except (OSError, ValueError):
            pass
    if issued_at:
        data["issued_at"] = issued_at
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def service(name: str, version: str, creds=None):
    """A discovery client (e.g. service("gmail", "v1"))."""
    from googleapiclient.discovery import build

    return build(name, version, credentials=creds or load_credentials(), cache_discovery=False)
