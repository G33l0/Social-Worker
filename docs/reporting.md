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
