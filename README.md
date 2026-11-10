# westmarch

A personal data warehouse and dashboard: what I work on and how, whether
my AI subscriptions earn their keep, and later health, social, fun and
money. Named for the Red Book of Westmarch, the record Sam keeps. See the
[PRD][prd].

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
| mailindex | full-text index of mail bodies and attachments; OCR on the local model | hourly |
| health | not a loader: Apple Health pushed to `/ingest/health/` by Health Auto Export | on push |
| location | not a loader: GPS points pushed to `/ingest/location/` by Overland | on push |

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
gzip-compressed at `$WESTMARCH_RAW_DIR/gmail/<xx>/<id>.eml.gz` (default
`/Volumes/ext1/westmarch`) with parsed headers in SQLite. The loader
reports disabled when the external volume isn't mounted.

The first backfill is slow by Google's design. A Cloud project created on
or after 2026-05-01 gets 6,000 quota units per user per minute and a raw
message costs 60, so the ceiling is 100 messages a minute: about five days
for 700,000 messages. The loader paces itself just under that, works up to
50 minutes of each hourly run, and resumes where it left off; a lock keeps
a second run from starting. Progress is in LoaderState (`messages_stored`
against `messages_total`, `backfill_done`). A project that predates the
change keeps the old limits (15,000 units a minute, 5 per message); for one
of those set `GMAIL_UNITS_PER_MINUTE=15000` and `GMAIL_GET_RAW_UNITS=5`.

**Seeding Gmail from Takeout.** At 100 messages a minute an API backfill
of a large mailbox takes days. Instead, export Mail from
[Google Takeout][takeout] and run
`uv run manage.py gmail_import_takeout <takeout.zip>` (a bare `.mbox` works
too). It streams the mbox straight out of the zip, so nothing is extracted
to disk. The message and thread ids in a Takeout are the API's own, so it
writes the same gzipped raw files and header rows, marked
`source="takeout"`, with Takeout's label names in `takeout_labels`. Spam
and Trash are skipped unless you pass `--include-spam-trash`, and anything
already stored is skipped, so it is safe to rerun or stop at any point;
`--limit N` does a trial run. No API quota is used. Then run
`uv run manage.py gmail_sync_labels`: it uses only cheap id listings to set
`label_ids` on every row and mark messages deleted since the export. The
hourly backfill then fetches only what the Takeout lacks.

After that each run applies Gmail's change history: new
mail, label changes and deletions. Deleted messages keep their row and
file, with `deleted_at` set. `manage.py load gmail --full` re-walks the
mailbox and fetches anything missing.

**Calendar** syncs every calendar's full history with per-calendar sync
tokens; recurring events are stored once, not expanded. **Tasks** syncs
every list, including completed, hidden and deleted tasks.

### Mail text index (`mailindex`)

`mailindex` (in `mailindex/`, run after gmail) makes every stored message
searchable: body text (plain part, or the HTML part stripped) and the text
of each attachment. It reads only the raw files, so everything it writes
can be rebuilt.

- **Documents** (PDF, Word, Excel, PowerPoint, old .doc/.rtf/.odt via
  macOS `textutil`, text, CSV, HTML, attached emails) are read in worker
  processes with ordinary libraries.
- **Images and scanned PDFs** (first five pages) go to the local model for
  OCR, `MAILINDEX_OCR_URL` / `MAILINDEX_OCR_MODEL` (default ds4 and
  `qwen3.8-flash-next`). A non-loopback URL is refused, so mail never
  leaves this machine. Images with no text get a one-line description.
  Tiny images (logos, spacers) are skipped. If the server is down the OCR
  pass stops and the next run retries; a file that fails three times is
  marked `error`.
- An attachment's text is stored once per distinct file (`AttachmentBlob`,
  by sha256); `MailAttachment` is one row per attachment per message.
  `mailindex_body_fts` and `mailindex_blob_fts` are FTS5 indexes beside the
  text, kept in sync by triggers.

Hourly runs are time-boxed (`MAILINDEX_RUN_SECONDS`, 20 minutes), newest
mail first, and only once `MAILINDEX_ENABLED=1` is in `.env`. For the
first backfill run it by hand without a limit, then turn it on:

```sh
uv run manage.py mailindex --no-ocr --budget-minutes 0   # all text, no OCR
uv run manage.py mailindex --budget-minutes 0            # then OCR until done
uv run manage.py mailsearch quarterly invoice            # try it
```

### iMessage

Mirrors `~/Library/Messages/chat.db` into Handle, Chat, Message and
Attachment tables. Attachment files are never touched, only their metadata.
Message text is stored but never logged; loader runs record counts only.

Setup: macOS protects chat.db, so a tiny C helper
(`ops/imessage-snapshot/imessage-snapshot.c`, about 80 lines) is the only
thing granted Full Disk Access. It can read just that one database, copies
it with the SQLite backup API into `$WESTMARCH_RAW_DIR/imessage/chat.db`
(`IMESSAGE_SNAPSHOT`), and the loader reads the copy with no special
permission.

1. `bin/build-imessage-snapshot` compiles it to `var/bin/imessage-snapshot`
   (`IMESSAGE_SNAPSHOT_BIN`) and prints the path.
2. Add that binary under System Settings, Privacy & Security, Full Disk
   Access.
3. Rebuilding changes the binary's ad hoc signature, so remove and re-add
   the grant after every rebuild.

macOS normally charges a file read to the app at the top of the process
tree (the terminal, or the launchd job), not to the binary doing it, so the
loader starts the helper as its own "responsible process"
(`sources/imessage/spawn.py`). Run by hand from a shell, the helper is
denied unless the terminal itself has Full Disk Access.

Until then `uv run manage.py load imessage` reports itself disabled and says
which step is missing. Setting `IMESSAGE_DB` instead reads that file
directly and skips the helper (useful for tests, or if you grant Full Disk
Access to the Python binary).

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

Each accepted body is kept gzipped under `$WESTMARCH_RAW_DIR/health/YYYY/MM/`
(falling back to `var/health-raw/` when the external SSD is not mounted)
and upserted into `Metric` (one row per metric, time and source; re-sent
windows replace rather than duplicate), `SleepNight`, `Workout`,
`WorkoutRoutePoint` (the GPS track of an outdoor workout, one row per fix)
and `IngestBatch`. A re-sent workout with a route replaces its points; one
sent without a route keeps them. Each ingest also shows up as a `health` row in the loader
table.

Every ingest also rebuilds `HealthDay`, a per-day rollup (steps, energy,
daylight, resting HR, HRV, sleep stages, workouts) for the days it touched;
`uv run manage.py health_rollup --full` rebuilds all of it.

Check that data is landing at `/ingest/health/status/` (counts and the
latest point per metric), or at the coverage chart at the bottom of
`/body/`, which shows per month and data type which exports are still
missing (from `HEALTH_HISTORY_START`, default `2015-01`). Replay a saved payload, or backfill from a manual
JSON export, with `uv run manage.py health_import file.json[.gz]`.

Phone setup: two automations of type REST API (one for Health Metrics, one
for Workouts, with Include Route Data on), format JSON, export version 2,
Batch Requests on, date range "Since Last Sync", Background App Refresh on,
Low Power Mode off. Syncs
only run while the phone is unlocked, and iOS may delay them.

### Location (Overland)

Continuous location arrives by push from the iOS app [Overland][overland],
which batches GPS points and POSTs them as GeoJSON to
`http://<host>:8520/ingest/location/`. It authenticates with
`LOCATION_INGEST_TOKEN`, sent as Overland's access token (a Bearer header;
`?access_token=` and `X-Ingest-Token` also work). A blank token disables the
endpoint (503).

Each batch is kept gzipped under `$WESTMARCH_RAW_DIR/location/YYYY/MM/`
(falling back to `var/location-raw/`) and upserted into `LocationPoint`, one
row per device and timestamp, so re-sent batches replace rather than
duplicate. iOS's -1 for an unknown speed, course or accuracy is stored as
null; properties the model doesn't name (activity, visit arrival and
departure, ...) are kept in `props`. The endpoint answers
`{"result": "ok"}`, which is what tells Overland to drop its queue.

Check it at `/ingest/location/status/`; replay a saved payload with
`uv run manage.py location_import file.json[.gz]`.

**Google location history.** `uv run manage.py timeline_import <path>`
loads any of Google's formats, streamed so size doesn't matter: the
on-device Timeline export from Google Maps (iOS: Settings, Location,
Location services, Timeline, Export; Android's variant too), or an old
Takeout's `Records.json` and Semantic Location History. It takes a
`.json`, `.json.gz`, a directory, or the Takeout `.zip` itself. Stays go to
`Visit`, movement to `TripSegment` (mode, distance), and GPS fixes to
`LocationPoint` under device ids `google-timeline`, `google-raw` and
`google-records`, beside Overland's. Re-importing is idempotent, and each
run is a `timeline` row in the loader table.

Phone setup, in Overland's settings: Receiver Endpoint
`http://<tailscale-host>:8520/ingest/location/`, Access Token the value of
`LOCATION_INGEST_TOKEN`, then start tracking. Tailscale must be connected
on the phone; Overland queues points while it can't reach the server and
sends them later.

## UI

Plain Django templates (`templates/`), plotly vendored at
`static/vendor/plotly.min.js`, no build step and no CDN. Pages render
HTML; each chart fetches a JSON endpoint and draws it.

| URL | what |
|-----|------|
| `/` | the catalog: each analysis and the question it answers, plus the last run of every loader |
| `/subscriptions/?range=7d` | "Am I making good use of my AI subscriptions?": per plan, the latest reading of every limit window, its history (`range` = `24h`, `7d`, `30d`, `all`), and agentsview's API-equivalent spend against the monthly price |
| `/body/?range=90d` | "How am I doing physically?": last-7-day tiles, steps, sleep stages, resting HR, HRV, daylight, exercise, workouts per week by type, the workout list, and data coverage by month (`range` = `30d`, `90d`, `1y`, `all`) |
| `/body/workouts/<id>/` | one workout: route map, elevation, heart rate. Map tiles load from CARTO; nothing else leaves the machine |
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
| `com.sefk.westmarch.web` | system | gunicorn on `:8520` (`WESTMARCH_PORT`) | always, restarted on crash |
| `com.sefk.westmarch.load-hourly` | system | `bin/load hourly`: loaders with cadence `hourly` | every hour |
| `com.sefk.westmarch.load-daily` | system | `bin/load daily`: cadence `daily`, then a full agentsview reload | 04:15 |
| `com.sefk.westmarch.load-session` | gui/uid | `bin/load session`: cadence `session` (Claude limits) | every hour, while logged in |

```sh
bin/install-launchd          # render and load all four; asks for sudo itself, do not prefix it
bin/install-launchd --agent  # just the LaunchAgent, no sudo
bin/deploy                   # uv sync, tests, migrate, restart web, wait for /health/
bin/status                   # job state, web check, log tails, last run of every loader
bin/uninstall-launchd        # asks for sudo itself
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
[takeout]: https://takeout.google.com/
[overland]: https://overland.p3k.app/
