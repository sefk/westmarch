"""Django settings for westmarch.

One process on the always-on Mac, SQLite, no auth (it sits behind
Tailscale / local-redirector). Secrets come from `.env` (gitignored).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Runtime state that survives restarts but stays out of git.
VAR_DIR = BASE_DIR / "var"
LOG_DIR = VAR_DIR / "log"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "westmarch-lan-only-not-a-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
    "sources.agentsview",
    "sources.limits",
    "sources.github",
    "sources.herdr",
    "sources.gitlocal",
    "sources.google",
    "sources.imessage",
    "sources.health",
    "sources.location",
    "derive",
    "mailindex",
    "catalog",
]

# Every loader, in the order `manage.py load --all` runs them. A sources
# app adds its loader class here; nothing else registers them.
LOADERS = [
    "sources.gitlocal.loader.ProjectsLoader",
    "sources.gitlocal.loader.GitLocalLoader",
    "sources.agentsview.loader.AgentsviewLoader",
    "sources.limits.loader.ClaudeLimitsLoader",
    "sources.limits.loader.OpenRouterLimitsLoader",
    "sources.limits.loader.CodexLimitsLoader",
    "sources.github.loader.GitHubLoader",
    "sources.herdr.loader.HerdrLoader",
    "sources.google.loader.CalendarLoader",
    "sources.google.loader.TasksLoader",
    "sources.imessage.loader.IMessageLoader",
    "derive.loader.DeriveLoader",  # joins the sources onto core.Project
    # Last: the first Gmail backfill works for up to 50 minutes per run,
    # and nothing else should wait behind it.
    "sources.google.loader.GmailLoader",
    "mailindex.loader.MailIndexLoader",  # text of what gmail just stored
]

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "westmarch.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "westmarch.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # WAL lets web reads and loader writes coexist.
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

# Raw rows are stored UTC; analysis happens in local time.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Los_Angeles"
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = VAR_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- westmarch ---

SERVE_HOST = os.environ.get("WESTMARCH_HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("WESTMARCH_PORT", "8520"))
GRAFANA_PORT = int(os.environ.get("GRAFANA_PORT", "3520"))

# Where my code lives; the projects loader scans it for git checkouts.
SRC_DIR = Path(os.environ.get("WESTMARCH_SRC_DIR", Path.home() / "src"))
# local-redirector's shares.toml supplies friendly names for projects.
REDIRECTOR_SHARES = Path(
    os.environ.get("WESTMARCH_REDIRECTOR_SHARES", SRC_DIR / "local-redirector" / "shares.toml")
)

# agentsview: installed package, running as a LaunchDaemon. Integrate via
# its HTTP API / CLI only; never read its SQLite.
AGENTSVIEW_URL = os.environ.get("AGENTSVIEW_URL", "http://127.0.0.1:8089")
AGENTSVIEW_BIN = os.environ.get("AGENTSVIEW_BIN", "/opt/homebrew/bin/agentsview")

# herdr keeps its state under ~/.config/herdr.
HERDR_DIR = Path(os.environ.get("HERDR_DIR", Path.home() / ".config" / "herdr"))

# GitHub: blank token means "ask `gh auth token`".
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# OpenRouter: blank disables the credits loader.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# --- phase 2 ---

# Bulk raw data (mail, health payloads) lives off the boot disk. A loader
# that needs it reports itself disabled when the volume isn't mounted.
RAW_DIR = Path(os.environ.get("WESTMARCH_RAW_DIR", "/Volumes/ext1/westmarch"))
# First month the health coverage chart shows (YYYY-MM); earlier data widens it.
HEALTH_HISTORY_START = os.environ.get("HEALTH_HISTORY_START", "2015-01")

# Google: a Desktop OAuth client and the token `manage.py google_auth`
# writes. Read-only scopes; this project never sends or modifies anything.
SECRETS_DIR = BASE_DIR / "secrets"
GOOGLE_CLIENT_FILE = Path(os.environ.get("GOOGLE_CLIENT_FILE", SECRETS_DIR / "google-client.json"))
GOOGLE_TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", SECRETS_DIR / "google-token.json"))
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]

# Gmail API quota for this Cloud project. Defaults are the limits for
# projects created on or after 2026-05-01 (100 raw messages a minute).
GMAIL_UNITS_PER_MINUTE = int(os.environ.get("GMAIL_UNITS_PER_MINUTE", "6000"))
GMAIL_GET_RAW_UNITS = int(os.environ.get("GMAIL_GET_RAW_UNITS", "60"))

# iMessage: bin/build-imessage-snapshot builds a small helper, the only thing
# granted Full Disk Access. The loader runs it to copy chat.db to
# IMESSAGE_SNAPSHOT, then reads the copy. IMESSAGE_DB, when set, means "read
# this file directly and skip the helper" (tests, or FDA granted to Python).
IMESSAGE_SNAPSHOT_BIN = Path(os.environ.get("IMESSAGE_SNAPSHOT_BIN") or BASE_DIR / "var" / "bin" / "imessage-snapshot")
IMESSAGE_SNAPSHOT = Path(os.environ.get("IMESSAGE_SNAPSHOT") or RAW_DIR / "imessage" / "chat.db")
IMESSAGE_DB = Path(os.environ["IMESSAGE_DB"]) if os.environ.get("IMESSAGE_DB") else None

# mailindex: searchable text of mail bodies and attachments, derived from
# the raw files. OCR (images, scanned PDFs) runs on the local model only;
# a non-loopback URL is refused. Hourly runs are time-boxed; the first
# backfill is `manage.py mailindex --budget-minutes 0`; hourly runs are off
# until MAILINDEX_ENABLED=1.
MAILINDEX_ENABLED = os.environ.get("MAILINDEX_ENABLED", "") == "1"
MAILINDEX_OCR_URL = os.environ.get("MAILINDEX_OCR_URL", "http://127.0.0.1:8765/v1")
MAILINDEX_OCR_MODEL = os.environ.get("MAILINDEX_OCR_MODEL", "qwen3.8-flash-next")
MAILINDEX_RUN_SECONDS = int(os.environ.get("MAILINDEX_RUN_SECONDS", str(20 * 60)))
MAILINDEX_WORKERS = int(os.environ.get("MAILINDEX_WORKERS", str(max((os.cpu_count() or 4) - 2, 1))))

# Apple Health: Health Auto Export POSTs to /ingest/health/ with this
# token in the `X-Ingest-Token` header. Blank disables the endpoint.
HEALTH_INGEST_TOKEN = os.environ.get("HEALTH_INGEST_TOKEN", "")
LOCATION_INGEST_TOKEN = os.environ.get("LOCATION_INGEST_TOKEN", "")

GITHUB_OWNERS = [s for s in os.environ.get("GITHUB_OWNERS", "sefk,biglocalnews").split(",") if s]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
