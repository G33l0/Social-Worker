# Reporting

Every run gets an id like `TEST-2026-08-17-0001`, and every session, execution,
event, metric and error is tied to it.

## Where reports go

`data/reports/<TEST-ID>/`, written automatically when a run stops (unless
`reporting.auto_export_on_stop` is off), and on demand from menu options 18 and
19. `index.html` links the run together.

## Categories

| Report | Contents |
|---|---|
| Overall | run metadata plus sessions, posts, replies, errors, avg response, error rate |
| Geographic | per-location sessions, posts, replies, avg response, avg session duration, errors |
| Worker | per-worker sessions, posts, replies, errors, last heartbeat |
| Forum | visits, posts, replies, errors, avg load time, configured weight |
| Post | every post submission plus content-library usage counts |
| Reply | every reply submission plus reply-library usage counts |
| Error | error rows with screenshot/HTML paths, and a breakdown by error type |
| Performance | count/min/avg/p50/p90/p95/p99/max for every timing metric |

Each is written as JSON (full fidelity), CSV (one file per table) and
self-contained HTML (no scripts, no CDN, light and dark friendly).

## Acceptance verdict

Every finished run is scored against `settings.thresholds`, and the verdict
(PASS / FAIL / NOT EVALUATED) is shown on the live monitor, in the run summary,
as a badge on the HTML report, and over the control API.

```bash
python main.py --verdict TEST-2026-08-18-0003     # exits 2 when a check failed
```

| Check | Setting |
|---|---|
| `error_rate` | `max_error_rate` |
| `avg_response_ms` | `max_avg_response_ms` |
| `p95_response_ms` / `p99_response_ms` | `max_p95_response_ms` / `max_p99_response_ms` |
| `post_submit_p95_ms` / `reply_submit_p95_ms` | `max_post_submit_p95_ms` / `max_reply_submit_p95_ms` |
| `rate_limit_events` | `max_rate_limit_events` |
| `failed_sessions` | `max_failed_sessions` |
| `completed_sessions` | `min_completed_sessions` |

A limit of `0` disables that check (except the count-based ones, where 0 means
"none tolerated").

## Comparing two runs

```bash
python main.py --compare TEST-2026-08-17-0004 TEST-2026-08-18-0003 --tolerance 0.10
```

Compares response percentiles, error rate, throughput and activity counts, and
exits non-zero when the candidate regressed beyond the tolerance. The same view
is available from menu option 18 → *compare with another run*.

## Charts

The HTML index embeds four inline-SVG charts (no scripts, no CDN): average
response time by location, activity by location, response time over the run, and
the percentile distribution of the dominant page metric. Colours are validated
for colour-vision deficiency and are defined for both light and dark themes.

## Metrics collected

Page load, DNS lookup, connection, TLS, TTFB, response and DOM-content-loaded
come from the browser's Navigation Timing API. Post and reply submission times,
forum entry time and per-action durations are measured around each interaction.
Session duration, pages visited, forums visited, successful and failed actions
are recorded per session.

## Programmatic export

```python
from app.database.database import Database
from app.reporting.report_manager import ReportManager
from app.utils.config import load_settings

database = Database(load_settings().database)
manager = ReportManager(database)
manager.export_all("TEST-2026-08-17-0001", formats=["json", "csv", "html"])
manager.build("geographic", "TEST-2026-08-17-0001")   # raw rows
```

## Logs and debug artefacts

```
logs/application.log   controller, scheduler and CLI activity
logs/workers.log       per-worker action records
logs/browser.log       navigation, console and network diagnostics
logs/errors.log        every warning and error, from all of the above
```

Records carry the test id, worker id, location, action, result and duration.
Passwords, tokens, API keys, cookies and session identifiers are redacted before
anything is written.

Automation failures also produce:

```
data/debug/<TEST-ID>/<worker-id>/<timestamp>.png    screenshot
data/debug/<TEST-ID>/<worker-id>/<timestamp>.html   page HTML
data/debug/<TEST-ID>/<worker-id>/<timestamp>.txt    URL, selector, action, error
```

## Querying the database directly

```sql
SELECT location_key, COUNT(*) AS sessions, AVG(duration_ms) AS avg_ms
FROM sessions WHERE test_run_id = 'TEST-2026-08-17-0001'
GROUP BY location_key;

SELECT action, COUNT(*), AVG(duration_ms)
FROM executions WHERE test_run_id = 'TEST-2026-08-17-0001'
GROUP BY action;
```
