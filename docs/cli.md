# The management console

```bash
python main.py                     # interactive console
python main.py --check             # validate environment and configuration
python main.py --init-db           # create/upgrade the schema
python main.py --dry-run --workers 3
python main.py --headless-run --label nightly
python main.py --config-dir /etc/social-worker
```

## Menu map

| # | Option | What it does |
|---|---|---|
| 1 | Configure Website | target URL, environment, authorization, identity header |
| 2 | Configure Locations | add/edit locations, allocation, ceilings, allocation preview |
| 3 | Configure Workers | worker wizard, bulk creation, generation from the location plan |
| 4 | Import Posts | import/edit/enable/disable post library items |
| 5 | Import Replies | the same for the reply library |
| 6 | Configure Forums | selection mode, weights, rotation order, allow-list |
| 7 | Configure Posting Behavior | intervals, quotas, probability, content mode |
| 8 | Configure Reply Behavior | intervals, quotas, probability, target selection |
| 9 | Configure Session Behavior | scenario, session duration, think time |
| 10 | Configure Concurrency | global/location ceilings, rate limits, abort thresholds |
| 11 | Test Single Worker | one worker, one session, optionally dry |
| 12 | Run Dry Test | detect avatars/usernames/forums/posts, submit nothing |
| 13 | Start Load Test | preflight, then ramped dispatch |
| 14/15 | Pause / Resume | stop and restart dispatch; running sessions are unaffected |
| 16 | Stop Load Test | graceful stop, STOP ALL, stop a location, stop a worker |
| 17 | Live Monitoring | the dashboard; Ctrl+C returns to the menu, the run continues |
| 18 | View Reports | render any of the eight report categories in the console |
| 19 | Export Results | write JSON/CSV/HTML for a run |
| 20 | Backup | copy configuration, content and the SQLite database |
| 21 | Exit | stops a running test if you confirm |

Typing `stop` at the menu prompt is the emergency **STOP ALL**.

## Live monitor

```
TEST STATUS: RUNNING    RUN: TEST-2026-08-17-0001

GLOBAL
Workers: 100   Active: 82   Completed: 153   Failed: 0   Errors: 3
Posts: 184   Replies: 233   Requests: 4210   Avg response: 148.2 ms
Queued: 47   Running: 82   Dispatched: 200   Skipped: 0   Rate-limit events: 0

LOCATION        ACTIVE  SESSIONS  POSTS  REPLIES  ERRORS  AVG RESPONSE MS
canada              15        31     31       42       0            141.5
...

WORKER     LOCATION  STATUS  ACTION    POSTS  REPLIES  ERRORS  USERNAME
SW-00001   canada    ACTIVE  READING       2        3       0  QuietPine
SW-00002   canada    ACTIVE  POSTING      1        2       0  AmberFrost

CONCURRENCY  global 82/100 (peak 84)
  canada: 15/20   nigeria: 20/30   united_states: 30/50
```

The console is asynchronous: the run keeps going while a menu waits for input,
so pause, resume, stop and monitoring all act on a live test.

## Wizards

* **Setup wizard** runs on first launch and writes `config/settings.yaml`.
* **Location wizard** (option 2) manages the location catalogue and can preview
  how the global worker budget will be split.
* **Worker wizard** (option 3) creates workers one at a time or in bulk, and can
  generate an entire fleet from the location allocation.
* **Content wizard** (options 4/5) imports TXT/CSV/JSON and edits items in place.
* **Schedule wizard** creates named windows and binds them to locations.
