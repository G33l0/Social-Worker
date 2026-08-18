# Architecture

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

## Layers

| Package | Responsibility |
|---|---|
| `app/core` | controller, worker/session/content/forum/metrics managers, acceptance thresholds |
| `app/scheduler` | job queue, dispatch loop, scenarios |
| `app/workers` | worker, worker pool, runtime state, heartbeats |
| `app/site` | site adapters — the only code that knows the target's markup |
| `app/browser` | Playwright pool, navigation, interaction, selectors, diagnostics |
| `app/locations` | location model, allocation, concurrency and rate limiting |
| `app/content` | import, libraries, rotation |
| `app/database` | engine, models, migrations, read-side repository |
| `app/reporting` | report building, run comparison, JSON/CSV/HTML writers |
| `app/api` | optional FastAPI control plane (run control + agent registration) |
| `cli` | console, wizards, live dashboard |

## Request path

1. The **controller** validates authorization, initialises the database, loads
   content and locations, plans the worker fleet and creates the test-run row.
2. The **scheduler** pulls session jobs, checking — in order — pause/stop state,
   the location schedule, the rate limiter, then the global, per-location and
   per-worker concurrency slots.
3. A **worker** runs its scenario. Each step calls the **site adapter**, which
   uses the **browser** layer with selectors from configuration.
4. Every step emits telemetry to the **metrics manager**, which buffers records
   and flushes them to the database on a background task, off the event loop.
5. On stop, counters are persisted, the run is scored against the acceptance
   thresholds, and the **report manager** writes the eight report categories
   with the verdict attached.

## The adapter contract

```python
class BaseSiteAdapter(ABC):
    async def initialize_session(self) -> ActionResult
    async def get_available_avatars(self) -> list[Avatar]
    async def select_avatar(self, avatar=None) -> ActionResult
    async def get_available_usernames(self) -> list[Username]
    async def select_username(self, username=None) -> ActionResult
    async def get_forums(self) -> list[ForumRef]
    async def enter_forum(self, forum) -> ActionResult
    async def browse_forum(self, depth=1) -> ActionResult
    async def get_initial_posts(self, limit=50) -> list[PostRef]
    async def read_post(self, post) -> ActionResult
    async def create_post(self, body, *, title="") -> ActionResult
    async def create_reply(self, post, body) -> ActionResult
    async def end_session(self) -> ActionResult
```

Supporting another authorized site means writing one adapter. The test suite
uses a fake adapter, which is why the unit suite needs no browser.

## Concurrency model

Everything is `asyncio`. Blocking work is kept off the loop: database writes go
through `asyncio.to_thread`, telemetry is buffered and flushed periodically, and
sleeps are interruptible so a stop takes effect immediately.

Browsers are pooled (`max_browsers` × `contexts_per_browser`); each session gets
its own context, so cookies and storage never leak between simulated visitors.

## Scaling out

The design is distribution-ready and the control plane is implemented:
`python main.py --serve-api` exposes run control (`/test/start`, `/test/pause`,
`/test/stop-all`, `/locations/{key}/stop`, `/workers/{id}/stop`), results
(`/status`, `/verdict`, `/runs`, `/reports/...`) and agent lifecycle
(`/workers/register`, `/workers/{id}/heartbeat`). Point every node at one
PostgreSQL `database.url` and the controller sees the whole fleet. The API binds
loopback by default and refuses any other address without an API token.
