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

## Tests

```sh
uv run pytest -q
```

[prd]: docs/prd.md
