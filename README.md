# Social Worker

**Authorized load and behaviour testing for anonymous/temporary-identity forum
websites.**

Social Worker simulates realistic visitors against a website you own or are
explicitly authorized to test. Each simulated visitor opens the site, takes the
avatar and temporary username **the site itself offers**, enters a forum,
browses, and — when the operator enables it — publishes posts and replies. Every
step is measured, so you get answers to questions like *how does the forum page
behave with 80 concurrent visitors from five regions?* and *what does the
database do when 40 posts land in a minute?*

> **Authorized use only.** This tool generates automated traffic. Use it only
> against systems you own or have written permission to test. See
> [SECURITY.md](SECURITY.md). A load test will not start until
> `authorized_test_mode` is enabled, and a `production` target additionally
> requires a recorded authorization reference.

---

## Table of contents

1. [What it measures](#what-it-measures)
2. [Installation](#1-installation)
3. [Playwright installation](#2-playwright-installation)
4. [Database initialization](#3-database-initialization)
5. [First-run setup](#4-first-run-setup)
6. [Mock website](#5-mock-website)
7. [Configuration examples](#6-configuration-examples)
8. [Running the tests](#7-running-the-tests)
9. [Running a load test](#8-running-a-load-test)
10. [Reporting](#9-reporting)
11. [Troubleshooting](#10-troubleshooting)
12. [Architecture](#architecture)
13. [Project layout](#project-layout)

---

## What it measures

| Area | Collected |
|---|---|
| Traffic | requests, pages visited, sessions started/completed |
| Geography | per-location sessions, posts, replies, errors, response time |
| Concurrency | live and peak concurrent workers, globally and per location |
| Forum activity | visits, posts and replies per forum, weight vs. observed share |
| Timing | page load, DNS, connect, TTFB, response, post/reply submission time |
| Sessions | duration, pages visited, successful vs. failed actions |
| Errors | HTTP 4xx/5xx, JS/page errors, automation failures, rate limiting |
| Resources | browser/context pool utilisation, worker utilisation |

Timings come from the browser's Navigation Timing API plus per-request
instrumentation, so the numbers reflect what a real visitor's browser saw.

---

## 1. Installation

Requires **Python 3.11 or newer** (3.12+ recommended).

### Windows

```bat
git clone <your-fork-or-copy> Social-Worker
cd Social-Worker
scripts\setup_windows.bat
```

The script creates `.venv`, installs the dependencies, installs Chromium and
initialises the database.

### Windows / Linux / macOS — manual

```bash
python -m venv .venv
# Windows:            .venv\Scripts\activate.bat
# Linux / macOS:      source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify the environment at any time:

```bash
python main.py --check
```

---

## 2. Playwright installation

Social Worker drives Chromium through Playwright. The Python package alone is
not enough — the browser binary must be downloaded once:

```bash
python -m playwright install chromium
```

On a bare Linux server, also install the shared libraries Chromium needs:

```bash
python -m playwright install-deps chromium
# or: sudo apt-get install libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
#         libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
#         libxrandr2 libgbm1 libasound2
```

Windows helper:

```bat
scripts\install_playwright.bat
```

If your organisation stores browsers centrally, set `PLAYWRIGHT_BROWSERS_PATH`,
or point `browser.executable_path` in `config/settings.yaml` at a specific
Chromium build.

---

## 3. Database initialization

SQLite is the default and needs no server:

```bash
python main.py --init-db
```

This creates `data/social_worker.db` with all twelve tables (`test_runs`,
`locations`, `workers`, `sessions`, `forums`, `content`, `replies`,
`executions`, `events`, `metrics`, `errors`, `settings`) and stamps the schema
version.

For production volumes use PostgreSQL — set the URL in `config/settings.yaml`:

```yaml
database:
  url: postgresql+psycopg://social_worker:secret@db.internal:5432/social_worker
  pool_size: 20
  max_overflow: 40
```

…then run `python main.py --init-db` again. Keep the password in `.env` and
reference it as `${DATABASE_URL}` — environment variables in the URL are
expanded at load time.

---

## 4. First-run setup

```bash
python main.py
```

With no configuration present the setup wizard runs first:

```
==================================================
SOCIAL WORKER INITIAL SETUP
==================================================

Website URL:
> http://127.0.0.1:8099

Testing environment:
  1. Local
  2. Staging
  3. Production
> 1

Authorized test mode - do you own this site or hold written
permission to load test it? (y/N)
> y

Authorization reference (ticket, contract or approval note)
> OPS-4821

Maximum global workers [50]
> 40

Configure geographic locations now? (Y/n)
Import content now? (Y/n)
Configure schedules now? (y/N)
Save configuration? (Y/n)
```

Afterwards the management console appears:

```
==============================================================
                      SOCIAL WORKER
                LOAD TEST MANAGEMENT CONSOLE
==============================================================
 Target : http://127.0.0.1:8099  (local)
 State  : IDLE   Mode: AUTHORIZED   Workers: 40
--------------------------------------------------------------
  1. Configure Website              12. Run Dry Test
  2. Configure Locations            13. Start Load Test
  3. Configure Workers              14. Pause Load Test
  4. Import Posts                   15. Resume Load Test
  5. Import Replies                 16. Stop Load Test
  6. Configure Forums               17. Live Monitoring
  7. Configure Posting Behavior     18. View Reports
  8. Configure Reply Behavior       19. Export Results
  9. Configure Session Behavior     20. Backup
 10. Configure Concurrency          21. Exit
 11. Test Single Worker
==============================================================
Select an option (or 'stop' for STOP ALL)
>
```

Typing `stop` at the menu is the emergency **STOP ALL** — it cancels every
queued and running session immediately.

---

## 5. Mock website

A local mock forum ships with the project so the whole platform can be
exercised before it is ever pointed at your real site. It offers avatars,
temporary usernames, four forums, seeded initial posts, post creation and reply
creation — matching the default selector catalogue.

```bash
python mock_site/test_site.py --port 8099
# Windows: scripts\run_mock_site.bat
```

Failure paths can be simulated:

```bash
python mock_site/test_site.py --port 8099 --rate-limit-after 25   # HTTP 429 after 25 writes
python mock_site/test_site.py --port 8099 --latency-ms 250        # slow server
python mock_site/test_site.py --port 8099 --error-rate 0.05       # 5% of writes fail
```

Useful endpoints: `GET /api/stats` (counters), `POST /api/reset` (reseed),
`GET /healthz`.

Point Social Worker at `http://127.0.0.1:8099` and run option **12 (Run Dry
Test)** followed by **11 (Test Single Worker)**.

---

## 6. Configuration examples

Configuration lives in `config/` and is validated on load — a typo fails fast
with an actionable message.

### `config/settings.yaml` (excerpt)

```yaml
website:
  url: https://forum.example.com
  environment: staging
  authorized_test_mode: true
  authorization_reference: OPS-4821
  operator_contact: ops@example.com
  identify_as: SocialWorker-LoadTest      # sent as X-Load-Test-Client

browser:
  headless: true
  viewport_width: 1366
  viewport_height: 768
  screenshot_on_failure: true
  max_browsers: 8
  contexts_per_browser: 8

concurrency:
  max_global_workers: 100
  default_max_workers_per_location: 20
  max_concurrent_sessions_per_worker: 1
  ramp_up_seconds: 60
  max_requests_per_minute: 600
  max_sessions_per_hour: 500
  max_sessions_per_day: 5000

posting:
  enabled: true
  min_interval_seconds: 900        # 15 minutes
  max_interval_seconds: 2700       # 45 minutes
  max_posts_per_session: 5
  max_posts_per_day: 50
  probability: 0.8
  content_selection_mode: RANDOM_WITHOUT_REPETITION

replies:
  enabled: true
  min_interval_seconds: 600
  max_interval_seconds: 1800
  max_replies_per_session: 10
  probability: 0.65                # ~65% of eligible sessions reply
  target_selection_mode: RANDOM_INITIAL_POST

forums:
  mode: WEIGHTED_FORUM_SELECTION
  weights: {general: 40, confessions: 20, questions: 15, random: 25}

safety:
  respect_rate_limits: true
  backoff_initial_seconds: 30
  backoff_max_seconds: 600
  rate_limit_status_codes: [429, 503]
  abort_on_error_rate: 0.5
  stop_on_rate_limit_streak: 10
```

### `config/locations.yaml`

```yaml
locations:
  - key: united_states
    name: United States
    region_group: north_america
    country: United States
    city: Ashburn
    timezone: America/New_York
    workers: 20
    max_concurrent_workers: 50
    runner: local            # local | remote | proxy
  - key: canada
    country: Canada
    timezone: America/Toronto
    workers: 10
    max_concurrent_workers: 20
  - key: nigeria
    region_group: africa
    country: Nigeria
    city: Lagos
    timezone: Africa/Lagos
    workers: 10
    max_concurrent_workers: 30
    schedule: business_hours
```

`runner` describes how that region's traffic is produced:

* `local` — this machine (development and mock-site testing)
* `remote` — a worker agent you run in that region (`runner_endpoint` required)
* `proxy` — an explicitly configured, authorized test proxy (`proxy_url` required)

Requested worker counts are scaled down proportionally when they exceed
`max_global_workers`; a location never exceeds its own
`max_concurrent_workers`.

### `config/workers.yaml`

Leave `workers: []` to derive workers automatically from the location
allocation, or define them explicitly (menu option 3 writes this file):

```yaml
workers:
  - worker_id: SW-00001
    location_key: canada
    scenario: scenario_06
    posting_enabled: true
    replying_enabled: true
    min_post_interval_seconds: 900
    max_post_interval_seconds: 2400
    max_posts_per_session: 5
    max_replies_per_session: 10
    sessions: 3
```

### `config/scenarios.yaml`

Seven scenarios ship built in (`scenario_01` visit-only … `scenario_07` heavy
forum activity). Custom scenarios are just a list of steps:

```yaml
scenarios:
  - name: scenario_peak_reader
    description: Long browse, no writes
    allow_posting: false
    allow_replying: false
    steps:
      - {action: OPEN_SITE}
      - {action: SELECT_AVATAR}
      - {action: SELECT_USERNAME}
      - {action: ENTER_FORUM}
      - {action: BROWSE_FORUM, repeat: 5}
      - {action: THINK, min_seconds: 5, max_seconds: 20}
      - {action: SWITCH_FORUM}
      - {action: BROWSE_FORUM, repeat: 3}
      - {action: END_SESSION}
```

### `config/selectors.yaml`

All site-specific selectors live here — pointing Social Worker at a different
authorized site is configuration, not code. Each element takes a list of
candidates that are tried in order:

```yaml
selectors:
  identity:
    avatar_options: ["[data-testid='avatar-option']", ".avatar-option"]
    username_options: ["[data-testid='username-option']", ".username-option"]
    enter_button: ["[data-testid='enter-site']", "#enter"]
  posts:
    post_items: ["[data-testid='post-item']", "article.post"]
    post_id_attribute: data-post-id
    new_post_input: ["[data-testid='new-post-body']", "textarea[name='body']"]
    new_post_submit: ["[data-testid='new-post-submit']", "#create-post"]
```

### Content libraries

`data/content/posts.txt` and `data/content/replies.txt` ship with 100 items
each. TXT (one per line, `#` comments), CSV (`id,category,title,body,enabled`)
and JSON (array of strings or objects) are all accepted, via menu options 4 and
5 or programmatically.

---

## 7. Running the tests

```bash
python -m pytest -q                       # unit + integration (no browser)
python -m pytest -m browser -q            # real Chromium against the mock site
python -m pytest tests/test_content.py -q # one module
# Windows: scripts\run_tests.bat
```

The browser suite starts the mock site on a free port, runs real sessions and
asserts against the site's own counters. It skips itself automatically when
Playwright is not installed.

---

## 8. Running a load test

1. **Start the target.** For a rehearsal: `python mock_site/test_site.py --port 8099`.
2. **Confirm the configuration** — `python main.py --check`.
3. **Dry run first** (menu **12**). Nothing is submitted; you get a report of
   what the workers detected:

   ```
   DRY RUN
     Worker                 : SW-00001
     Location               : canada
     Avatars detected       : 6
     Avatar selected        : Avatar 3
     Usernames detected     : 5
     Username selected      : Anonymous_17
     Forums detected        : General, Confessions, Questions, Random
     Initial posts detected : 24
     Would create post      : yes
     Would reply            : yes
     Actual submission      : DISABLED
   ```

4. **Test a single worker** (menu **11**) to confirm posts and replies land.
5. **Start the load test** (menu **13**). Workers ramp up over
   `concurrency.ramp_up_seconds`, never exceeding the global, per-location and
   per-worker ceilings.
6. **Watch it** (menu **17**):

   ```
   ==============================================================
   SOCIAL WORKER LIVE MONITOR
   ==============================================================
   TEST STATUS: RUNNING    RUN: TEST-2026-08-17-0001

   GLOBAL
   Workers: 100   Active: 82   Completed: 153   Failed: 0   Errors: 3
   Posts: 184   Replies: 233   Requests: 4210   Avg response: 148.2 ms

   LOCATION        ACTIVE  SESSIONS  POSTS  REPLIES  ERRORS  AVG RESPONSE MS
   ---------------------------------------------------------------------
   canada              15        31     31       42       0            141.5
   nigeria             20        41     41       52       2            210.8
   united_states       30        65     65       81       1            132.4
   ```

7. **Pause / resume / stop** with menu options 14–16, or type `stop` for an
   immediate STOP ALL.

Headless (no menu):

```bash
python main.py --headless-run --label nightly-capacity
python main.py --dry-run --workers 3
```

### Safety controls

* global, per-location and per-worker concurrency ceilings
* sessions per minute / hour / day
* posts and replies per session and per day, per worker
* abort when the observed error rate crosses `abort_on_error_rate`
* HTTP 429 (and any configured status) → record, exponential back-off, and stop
  the run once `stop_on_rate_limit_streak` is reached — the limit is never
  circumvented
* `STOP ALL` cancels queued and in-flight sessions immediately

---

## 9. Reporting

Every run gets a unique id (`TEST-2026-08-17-0001`) and all activity is linked
to it. Reports are written to `data/reports/<TEST-ID>/` automatically when a run
stops (`reporting.auto_export_on_stop`), and on demand from menu **18 (View
Reports)** / **19 (Export Results)**.

Eight categories, each in JSON, CSV and HTML:

| Report | Contents |
|---|---|
| Overall | run metadata, sessions, posts, replies, errors, avg response |
| Geographic | `LOCATION \| SESSIONS \| POSTS \| REPLIES \| AVG RESPONSE \| ERRORS` |
| Worker | per-worker sessions, posts, replies, errors, last heartbeat |
| Forum | visits, posts, replies, errors, avg load time, configured weight |
| Post | every post submission plus content-rotation usage |
| Reply | every reply submission plus reply-library usage |
| Error | errors with screenshot/HTML paths, plus a breakdown by type |
| Performance | count/min/avg/p50/p90/p95/p99/max for every timing metric |

`index.html` in the run folder links everything together. The HTML is
self-contained (no CDN, no scripts) and readable in light and dark themes.

```bash
# programmatic export
python - <<'PY'
from app.database.database import Database
from app.reporting.report_manager import ReportManager
from app.utils.config import load_settings

database = Database(load_settings().database)
manager = ReportManager(database)
print(manager.export_all("TEST-2026-08-17-0001", formats=["json", "csv", "html"]))
PY
```

Logs land in `logs/application.log`, `logs/workers.log`, `logs/browser.log` and
`logs/errors.log`; each record carries the test id, worker id, location, action,
result and duration. Credentials, cookies and tokens are redacted before they
reach disk.

Automation failures write `data/debug/<TEST-ID>/<worker-id>/<timestamp>.png`,
`.html` and `.txt` (URL, selector, action, error) so a broken selector can be
diagnosed after the fact.

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Authorized test mode is OFF` and the run refuses to start | Menu 1 → enable authorized test mode (and add a reference for production targets). This is deliberate. |
| `BrowserUnavailableError: Could not launch Chromium` | Run `python -m playwright install chromium`. On Linux add `python -m playwright install-deps chromium`. |
| `Executable doesn't exist at .../chromium-<rev>` | The Playwright package and the installed browser revision disagree. Re-run `python -m playwright install chromium`, or set `browser.executable_path`. |
| `The post library is empty` | Menu 4 → import posts. A dry run works without content; a real run does not. |
| `No forums were discovered on the site` | The forum selectors do not match your markup. Update `config/selectors.yaml` (`forums.forum_links`) and re-run a dry test. |
| `No post composer found in this forum` | The site requires an identity before posting, or `posts.new_post_input` is wrong. Check the screenshot in `data/debug/`. |
| Workers stuck in `WAITING` | They are inside a post/reply interval. Lower `posting.min_interval_seconds` for short tests. |
| Run stops with "Target reported rate limiting" | The site is throttling. That is a result, not a bug — reduce `max_global_workers` or `max_requests_per_minute`. |
| `database is locked` (SQLite) | Concurrency beyond what SQLite likes. Move to PostgreSQL for large runs. |
| Sessions end immediately, `pages_visited: 1` | `session.max_duration_seconds` is too small for the scenario. |
| Console colours look like `←[36m` in cmd.exe | Old console host — set `NO_COLOR=1`, or use Windows Terminal. |
| Timings look impossibly fast | Confirm you are not pointed at the mock site (`python main.py --check` prints the target). |

Still stuck? `python main.py --check` validates configuration, database,
locations, content and Playwright in one pass, and `logs/errors.log` holds every
warning and error with its context.

---

## Architecture

```
                     SOCIAL WORKER CONTROLLER
                              |
        +---------------------+---------------------+
        |                     |                     |
   Worker Pool           Worker Pool           Worker Pool
     Canada                  USA                    UK
        |                     |                     |
   Browser sessions     Browser sessions      Browser sessions
```

The controller owns configuration, the database, content, locations, workers,
the scheduler, metrics and reporting. The scheduler dispatches session jobs
under the pause/stop state, location schedules, rate limits and the three
concurrency ceilings. Each worker runs a scenario through a **site adapter** —
the only component that knows anything about the target's markup — and every
step emits telemetry that is buffered and flushed to the database off the event
loop.

`BaseSiteAdapter` defines the contract (`initialize_session`,
`get_available_avatars`, `select_avatar`, `get_available_usernames`,
`select_username`, `get_forums`, `enter_forum`, `get_initial_posts`,
`create_post`, `create_reply`, `end_session`), so supporting another authorized
site means writing one adapter, not touching the engine.

The architecture is distribution-ready: workers are addressed by location, the
controller tracks registration and heartbeats, and `controller.api_enabled`,
`redis_url` and a PostgreSQL `database.url` are the switches for running worker
pools on separate machines.

---

## Project layout

```
Social-Worker/
├── app/
│   ├── core/          controller, scheduler facade, worker/session/content/forum/metrics managers
│   ├── browser/       Playwright manager, navigator, interaction, selectors, session
│   ├── site/          base adapter + avatar/username/forum/post/reply managers
│   ├── workers/       worker, worker pool, worker state, heartbeat
│   ├── scheduler/     dispatch scheduler, job queue, scenarios
│   ├── locations/     location model, manager, allocation and concurrency control
│   ├── content/       importer, post/reply libraries, rotation
│   ├── database/      engine, models, migrations, repository
│   ├── reporting/     report manager and JSON/CSV/HTML writers
│   └── utils/         config, logger, validation, time helpers, ids
├── cli/               console, menu, setup/worker/location/content/schedule wizards, dashboard
├── config/            settings, locations, workers, schedules, scenarios, selectors
├── data/              content, reports, screenshots, debug artefacts, SQLite database
├── mock_site/         local mock forum website
├── scripts/           Windows/Linux setup and helper scripts
├── tests/             unit, integration and browser test suites
└── main.py            entry point
```

## License

MIT, with an authorized-use notice — see [LICENSE](LICENSE) and
[SECURITY.md](SECURITY.md).
