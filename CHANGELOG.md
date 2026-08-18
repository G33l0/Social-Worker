# Changelog

All notable changes to Social Worker are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] - 2026-08-18

### Added

* **Acceptance thresholds** — every finished run is scored against configurable
  criteria (error rate, p95/p99 response time, submission latency, rate-limit
  events, failed sessions, minimum completed sessions) and gets a PASS/FAIL
  verdict on the monitor, in the run summary, on the HTML report and over the
  API. `python main.py --verdict <run>` exits non-zero on failure.
* **Run comparison** — `python main.py --compare <baseline> <candidate>`
  reports per-metric deltas and exits non-zero when the candidate regressed
  beyond the tolerance; also available from menu option 18.
* **HTTP control API** — `python main.py --serve-api` exposes run control,
  results and regional worker-agent registration/heartbeats. Off by default,
  loopback-only, and refuses a public bind without an API token.
* **Charts in the HTML report** — inline-SVG response time by location, activity
  by location, response time over the run and percentile distribution, with a
  colour-vision-validated palette in both light and dark themes.
* **robots.txt compliance** — `safety.honour_robots_txt` is now enforced during
  preflight instead of being an unused setting.
* Screenshots of the console, monitor, dry run, verdict, comparison and report
  under `docs/screenshots/`.

### Fixed

* Synchronous menu wizards ran on the event loop and stalled a live run's
  dispatch, metric flushing and heartbeats; they now run on a worker thread.
* Leaving the live monitor required Ctrl+C, which tore down the whole console
  and the running test. It now exits on Enter.
* Content added or edited in the wizard was silently dropped when a run started,
  because the controller reloads libraries from disk; libraries are now saved
  first.
* An all-zero forum weight set raised out of the forum selector and killed the
  step; zero-weighted forums are now correctly treated as "do not use".
* The buffered-telemetry flush task had no strong reference and could be
  garbage-collected mid-flight, losing records.
* `_finalise` could run twice (run task plus operator stop), double-exporting
  reports and re-stamping the run's end time.
* Completed workers were reported as stale heartbeats after a run finished.
* Browser response timing used `asyncio.get_event_loop().time()` from a sync
  callback; it now uses a monotonic clock.
* Settings that were configurable but never read are now honoured:
  `concurrency.default_max_workers_per_location`, `concurrency.ramp_down_seconds`,
  `posting.randomized_delay` (and a new matching `replies.randomized_delay`),
  `posting.categories` / `replies.categories`, `session.browse_pages_min/max`,
  `browser.save_html_on_failure`, `website.verify_tls`, `safety.honour_robots_txt`.
  `session.reuse_identity_across_sessions`, which cannot be honoured with
  per-session browser contexts, was removed — retired keys are now ignored with
  a warning instead of failing the load.

## [1.0.0] - 2026-08-17

### Added

* **Controller** — one class owning a test run: configuration, database,
  content, locations, workers, scheduling, metrics and reporting, with
  START / PAUSE / RESUME / STOP / STOP LOCATION / STOP WORKER / STOP ALL.
* **Geographic test locations** — location catalogue with country/region/city,
  timezone, locale, worker allocation, per-location concurrency ceilings,
  enable/disable, named schedules and `local` / `remote` / `proxy` runners.
* **Concurrency control** — global, per-location and per-worker ceilings plus
  sliding-window rate limits per minute, hour and day.
* **Workers** — profile-driven simulated visitors with posting/replying toggles,
  interval ranges, per-session and per-day quotas, probabilities and scenarios.
* **Site adapter layer** — `BaseSiteAdapter` contract with avatar, username,
  forum, post and reply managers; avatars and usernames are always taken from
  the site rather than invented.
* **Browser layer** — Playwright Chromium pool, navigator with Navigation-Timing
  capture, interaction helpers with candidate-selector fallback, console/page
  error capture, network monitoring, and screenshot + HTML capture on failure.
* **Content libraries** — 100+ posts and replies from TXT/CSV/JSON with add,
  edit, delete, enable/disable, categories, usage tracking, duplicate
  prevention and four rotation modes.
* **Forum behaviour** — random, specific, rotating and weighted forum selection.
* **Scenarios** — seven built-in scenarios plus operator-defined ones.
* **Scheduler** — priority job queue, ramp-up, schedule-aware dispatch and
  cancellation by worker, by location or globally.
* **Telemetry** — buffered events, metrics, executions and errors written off
  the event loop into twelve SQLAlchemy tables, every row tied to a test-run id.
* **Reporting** — eight report categories in JSON, CSV and self-contained HTML,
  with percentile performance summaries.
* **Interactive console** — 21-option Windows CMD management console, setup /
  worker / location / content / schedule wizards and a live dashboard.
* **Dry-run mode** — walks the site and reports what it detects without
  submitting posts or replies.
* **Mock website** — local FastAPI forum with avatars, usernames, forums,
  seeded posts, and switches for latency, error injection and HTTP 429.
* **Safety** — authorization gate before any run, rate-limit back-off that is
  never circumvented, error-rate abort, and credential redaction in logs.
