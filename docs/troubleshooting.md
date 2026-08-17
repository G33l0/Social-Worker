# Troubleshooting

Start with `python main.py --check` — it validates configuration, the database,
locations, content and Playwright in one pass. Then read `logs/errors.log`,
which holds every warning and error with its test id, worker id, location,
action and duration.

## Startup

**"Authorized test mode is OFF" and the run refuses to start.**
Deliberate. Menu 1 → enable authorized test mode. Production targets also need
an authorization reference, which is stored with the run.

**`BrowserUnavailableError: Could not launch Chromium`**
`python -m playwright install chromium`. On Linux add
`python -m playwright install-deps chromium`.

**`Executable doesn't exist at .../chromium-<rev>/chrome-linux/chrome`**
The Playwright package and the installed browser revision disagree — reinstall
the browser after upgrading the package, or set `browser.executable_path`.

**`The post library is empty`**
Menu 4 → import posts. Dry runs work without content; real runs do not.

## Automation

**`No forums were discovered on the site`**
`selectors.forums.forum_links` does not match your markup. Update
`config/selectors.yaml` and re-run a dry test.

**`No post composer found in this forum`**
Either the site requires an identity before showing the composer (check that
`SELECT_USERNAME` succeeded earlier in the session) or
`posts.new_post_input` is wrong. The screenshot and HTML in
`data/debug/<TEST-ID>/<worker-id>/` show exactly what the worker saw.

**Sessions end after one page, `pages_visited: 1`.**
`session.max_duration_seconds` is smaller than the scenario needs; raise it.

**Workers sit in `WAITING`.**
They are inside a post or reply interval. That is the configured behaviour —
lower `posting.min_interval_seconds` for short rehearsals.

## Load and safety

**Run stops with "Target reported rate limiting".**
The site returned HTTP 429/503 more times than `stop_on_rate_limit_streak`.
That is a result, not a bug: reduce `max_global_workers` or
`max_requests_per_minute`. Social Worker will not work around the limit.

**Run aborts on the error-rate threshold.**
More than `safety.abort_on_error_rate` of actions failed. Check the error report
for the dominant `error_type` before raising the threshold.

**`database is locked` (SQLite).**
Too much write concurrency for SQLite. Move to PostgreSQL for large runs.

**Memory or CPU climbing with many workers.**
Each context is a real browser context. Lower `contexts_per_browser`, raise
`max_browsers` across more machines, or reduce the global worker count. Headless
mode uses noticeably less memory than headed.

## Console

**Colours render as `←[36m` in cmd.exe.**
An old console host. Set `NO_COLOR=1` or use Windows Terminal.

**The menu seems frozen during a run.**
It is not — the console is asynchronous. If a wizard is open, finish or cancel
it; the test keeps running underneath.

## Results look wrong

**Timings are impossibly fast.** Confirm the target: `python main.py --check`
prints it. You may still be pointed at the mock site.

**Posts appear in the wrong forums.** Check `forums.mode` and the weights; the
forum report shows the configured weight next to the observed visit share.

**Content repeats.** With `RANDOM` selection repeats are expected. Use
`RANDOM_WITHOUT_REPETITION`, and set `content.prevent_duplicates_per_worker`.
