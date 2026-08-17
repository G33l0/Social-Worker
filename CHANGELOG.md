# Changelog

All notable changes to Social Worker are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
