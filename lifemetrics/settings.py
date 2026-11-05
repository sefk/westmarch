"""Django settings for life-metrics.

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

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "life-metrics-lan-only-not-a-secret")
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
    "derive",
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
]

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "lifemetrics.urls"

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

WSGI_APPLICATION = "lifemetrics.wsgi.application"

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

# --- life-metrics ---

SERVE_HOST = os.environ.get("LIFE_HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("LIFE_PORT", "8520"))
GRAFANA_PORT = int(os.environ.get("GRAFANA_PORT", "3520"))

# Where my code lives; the projects loader scans it for git checkouts.
SRC_DIR = Path(os.environ.get("LIFE_SRC_DIR", Path.home() / "src"))
# local-redirector's shares.toml supplies friendly names for projects.
REDIRECTOR_SHARES = Path(
    os.environ.get("LIFE_REDIRECTOR_SHARES", SRC_DIR / "local-redirector" / "shares.toml")
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
RAW_DIR = Path(os.environ.get("LIFE_RAW_DIR", "/Volumes/ext1/life-metrics"))

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

# iMessage: needs Full Disk Access for whatever process runs the loader.
IMESSAGE_DB = Path(os.environ.get("IMESSAGE_DB", Path.home() / "Library" / "Messages" / "chat.db"))

# Apple Health: Health Auto Export POSTs to /ingest/health/ with this
# token in the `X-Ingest-Token` header. Blank disables the endpoint.
HEALTH_INGEST_TOKEN = os.environ.get("HEALTH_INGEST_TOKEN", "")

GITHUB_OWNERS = [s for s in os.environ.get("GITHUB_OWNERS", "sefk,biglocalnews").split(",") if s]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
