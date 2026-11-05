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
| gcal | every Google calendar, full history, read-only | hourly |
| gtasks | every Google Tasks list, read-only | daily |
| imessage | the Messages database, read-only | hourly |
| gmail | full mailbox mirror: raw messages on the external SSD, headers in SQLite, read-only | hourly |
| health | not a loader: Apple Health pushed to `/ingest/health/` by Health Auto Export | on push |

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

## Phase 2 sources

Each needs one unlock step from a human. Until then its loader reports
"disabled" and everything else keeps running.

### Google (Gmail, Calendar, Tasks)

Read-only: scopes are gmail.readonly, calendar.readonly and
tasks.readonly. Nothing is ever sent, labelled, moved or deleted in Google.

One-time setup, with a Google Cloud project and OAuth client of its own
(not shared with any other project):

1. In the [Google Cloud console][gcp], create a project and enable the
   Gmail API, Google Calendar API and Google Tasks API.
2. Configure the consent screen: audience **External**. Then **publish**
   it (Audience, Publish app). In Testing, refresh tokens expire after 7
   days and the loaders go "disabled" weekly. The Publish button stays
   greyed out until the branding page has a home page, privacy policy and
   terms of service URL; any URL on a domain you own will do. Do not
   submit for verification and do not upload a logo; an unverified app
   works for its owner.
3. Clients, Create client, type **Desktop app**. Download the JSON at
   creation time to `secrets/google-client.json` (or set
   `GOOGLE_CLIENT_FILE`).
4. Run `uv run manage.py google_auth`. A browser opens; click through the
   "Google hasn't verified this app" warning (Advanced, Go to ...) and tick
   all three permission boxes. The token is saved to
   `secrets/google-token.json` (mode 0600, gitignored). The command prints
   the account, the message count, and whether Calendar and Tasks respond;
   if an API isn't enabled it prints the URL that enables it.
5. `manage.py google_auth --check` re-checks without a browser; `--force`
   consents again.

**Gmail** mirrors the whole mailbox. Each raw message is stored
gzip-compressed at `$LIFE_RAW_DIR/gmail/<xx>/<id>.eml.gz` (default
`/Volumes/ext1/life-metrics`) with parsed headers in SQLite. The loader
reports disabled when the external volume isn't mounted. The first backfill
works 20 minutes per hourly run and resumes where it left off; progress is
in LoaderState (`messages_stored` against `messages_total`,
`backfill_done`). After that each run applies Gmail's change history: new
mail, label changes and deletions. Deleted messages keep their row and
file, with `deleted_at` set. `manage.py load gmail --full` re-walks the
mailbox and fetches anything missing.

**Calendar** syncs every calendar's full history with per-calendar sync
tokens; recurring events are stored once, not expanded. **Tasks** syncs
every list, including completed, hidden and deleted tasks.

### iMessage

Mirrors `~/Library/Messages/chat.db` (override with `IMESSAGE_DB`) into
Handle, Chat, Message and Attachment tables. The database is opened
read-only; attachment files are never touched, only their metadata. Message
text is stored but never logged; loader runs record counts only.

Setup: macOS protects chat.db, so grant Full Disk Access (System Settings,
Privacy & Security, Full Disk Access) to the Python binary that runs the
loader. `uv run manage.py load imessage` names the exact path in its
"disabled" message. A uv Python upgrade changes that path and the grant has
to be redone.

Incremental by message ROWID, plus a re-read of the trailing two days so
edits, unsends and read receipts update; `load imessage --full` rereads
everything. Newer macOS stores the body only in `attributedBody`; a small
typedstream extractor decodes it, and undecodable bodies are counted in
the loader state `body_decode_failures`. Tapbacks are rows with `reaction`
set ("love", or "-love" when removed) and `reply_to_guid` pointing at the
target.

### Apple Health

Health data arrives by push from the iOS app [Health Auto Export][hae]
(Premium tier, REST API automations), never by manual export. The app
POSTs JSON to `http://<host>:8520/ingest/health/` with the header
`X-Ingest-Token: $HEALTH_INGEST_TOKEN`. A blank token disables the endpoint
(503).

Each accepted body is kept gzipped under `$LIFE_RAW_DIR/health/YYYY/MM/`
(falling back to `var/health-raw/` when the external SSD is not mounted)
and upserted into `Metric` (one row per metric, time and source; re-sent
windows replace rather than duplicate), `SleepNight`, `Workout` and
`IngestBatch`. Each ingest also shows up as a `health` row in the loader
table.

Check that data is landing at `/ingest/health/status/` (counts and the
latest point per metric). Replay a saved payload, or backfill from a manual
JSON export, with `uv run manage.py health_import file.json[.gz]`.

Phone setup: two automations of type REST API (one for Health Metrics, one
for Workouts), format JSON, export version 2, Batch Requests on, date range
"Since Last Sync", Background App Refresh on, Low Power Mode off. Syncs
only run while the phone is unlocked, and iOS may delay them.

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
[gcp]: https://console.cloud.google.com/
[hae]: https://www.healthyapps.dev/
