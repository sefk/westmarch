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
| derive | maps source names onto projects, rolls up per-project daily totals (`ProjectDay`) | hourly |

Each loader lives in `sources/<name>/` with its own models, migrations and
tests. The framework (`core/loaders.py`) records every run in
`LoaderRun` and keeps incremental cursors in `LoaderState`.

### Derive

`derive` (in `derive/`, run last) joins the sources onto `core.Project`.
It writes `ProjectAlias` rows for agentsview project names, GitHub repos
and herdr tab cwds (paths resolve by longest checkout prefix, or as a
sibling worktree `<checkout>-<suffix>`); it only creates aliases, so hand
edits win. It then rebuilds `ProjectDay`, one row per project per local
day (sessions, cost, tokens, commits, GitHub activity, herdr focus), and
`UnmappedDay` for agentsview usage no project claimed. Incremental runs
rebuild from three days before the last run; `--full` rebuilds all.
`uv run manage.py unmapped` lists the names that map nowhere: add them as
`aliases` in `~/src/AGENTS.md`.

## UI

Plain Django templates (`templates/`), plotly vendored at
`static/vendor/plotly.min.js`, no build step and no CDN. Pages render
HTML; each chart fetches a JSON endpoint and draws it.

| URL | what |
|-----|------|
| `/` | the catalog: each analysis and the question it answers, plus the last run of every loader |
| `/subscriptions/?range=7d` | "Am I making good use of my AI subscriptions?": per plan, the latest reading of every limit window, its history (`range` = `24h`, `7d`, `30d`, `all`), and agentsview's API-equivalent spend against the monthly price |
| `/subscriptions/data/?range=7d` | the chart data: `{vendor: {traces: [{name, window, x, y, kind}]}}`, `kind` `pct` or `usd` |

## Running locally

```sh
DJANGO_DEBUG=true uv run manage.py runserver 8520
```

## Deployment (always-on Mac)

Four launchd jobs, rendered from `ops/launchd/*.plist.in`. Three are
system LaunchDaemons that run as this user so they start at boot with
nobody logged in. The fourth is a per-user LaunchAgent in
`~/Library/LaunchAgents`, because the Claude limits loader reads the Claude
Code token from the login Keychain, which a system daemon cannot see:

| job | domain | does | when |
|-----|--------|------|------|
| `com.sefk.life-metrics.web` | system | gunicorn on `:8520` (`LIFE_PORT`) | always, restarted on crash |
| `com.sefk.life-metrics.load-hourly` | system | `bin/load hourly`: loaders with cadence `hourly` | every hour |
| `com.sefk.life-metrics.load-daily` | system | `bin/load daily`: cadence `daily`, then a full agentsview reload | 04:15 |
| `com.sefk.life-metrics.load-session` | gui/uid | `bin/load session`: cadence `session` (Claude limits) | every hour, while logged in |

```sh
bin/install-launchd          # render and load all four (sudo for the daemons)
bin/install-launchd --agent  # just the LaunchAgent, no sudo
bin/deploy                   # uv sync, tests, migrate, restart web, wait for /health/
bin/status                   # job state, web check, log tails, last run of every loader
bin/uninstall-launchd        # sudo
```

Logs land in `var/log/`. A loader job ends with a `LOAD hourly OK` or
`LOAD hourly FAILED` line. The loader jobs need no restart on deploy:
launchd starts a fresh process each tick.

The LaunchAgent only exists while you have a login session, so after a
reboot the Claude limits series has a gap until you log in. The screen
being locked is fine; the Keychain stays unlocked. There is deliberately no
auto-login.

## Tests

```sh
uv run pytest -q
```

[prd]: docs/prd.md
