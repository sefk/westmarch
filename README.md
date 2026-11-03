# life-metrics

A personal data warehouse and dashboard: what I work on and how, whether
my AI subscriptions earn their keep, and later health, social, fun and
money. See the [PRD][prd].

One Django process, SQLite, Dogsheep-style loaders: one per source, raw
rows kept, idempotent and incremental. agentsview and Grafana stay
untouched installed packages; this project talks to them over their
APIs only.

## Setup

```sh
uv sync
uv run manage.py migrate
cp .env.example .env
```

## Loaders

```sh
uv run manage.py load              # list loaders and their last run
uv run manage.py load --all        # run every loader
uv run manage.py load agentsview   # run one
uv run manage.py load --all --full # ignore incremental cursors, reload everything
```

| loader | source | cadence |
|--------|--------|---------|
| projects | git checkouts under `~/src` plus friendly names from local-redirector | daily |
| gitlocal | commits in those checkouts | daily |
| agentsview | AI coding sessions, per-model tokens and cost, via the agentsview CLI (`export sessions` + `session list`); subagents are their own rows | hourly |
| claude_limits | Claude subscription rate-limit windows, via the Claude Code OAuth token in the Keychain (read-only, never refreshed) | hourly |
| openrouter_limits | OpenRouter credits and budgets | hourly |
| codex_limits | Codex weekly limit, read from the CLI's local session logs | hourly |
| github | commits, issues, PRs for my repos | daily |
| herdr | herdr workspaces, tabs and focus events | hourly |

Each loader lives in `sources/<name>/` with its own models, migrations and
tests. The framework (`core/loaders.py`) records every run in
`LoaderRun` and keeps incremental cursors in `LoaderState`.

## Running locally

```sh
DJANGO_DEBUG=true uv run manage.py runserver 8520
```

## Deployment (always-on Mac)

Three system LaunchDaemons, rendered from `ops/launchd/*.plist.in` and run
as this user so they start at boot with nobody logged in:

| job | does | when |
|-----|------|------|
| `com.sefk.life-metrics.web` | gunicorn on `:8520` (`LIFE_PORT`) | always, restarted on crash |
| `com.sefk.life-metrics.load-hourly` | `bin/load hourly`: the hourly loaders | every hour |
| `com.sefk.life-metrics.load-daily` | `bin/load daily`: the daily loaders, then a full agentsview reload | 04:15 |

```sh
bin/install-launchd     # render and load the three jobs (sudo)
bin/deploy              # uv sync, tests, migrate, restart web, wait for /health/
bin/status              # job state, web check, log tails, last run of every loader
bin/uninstall-launchd   # sudo
```

Logs land in `var/log/`. A loader job ends with a `LOAD hourly OK` or
`LOAD hourly FAILED` line. The loader jobs need no restart on deploy:
launchd starts a fresh process each tick.

Caveat: the Claude limits loader reads the Claude Code token from the
login Keychain. Under a system daemon that works while you are logged in
to the Mac; if the Keychain is locked the loader records itself disabled
for that run and tries again next hour.

## Tests

```sh
uv run pytest -q
```

[prd]: docs/prd.md
