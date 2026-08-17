# Workers

A worker is one simulated visitor. Its profile is the operator-facing knob set;
its runtime state is what the live monitor shows.

## Profile

```yaml
workers:
  - worker_id: SW-00001
    location_key: canada
    scenario: scenario_06
    forum_mode: WEIGHTED_FORUM_SELECTION
    forum: ""                       # set for SPECIFIC_FORUM
    posting_enabled: true
    replying_enabled: true
    min_post_interval_seconds: 900
    max_post_interval_seconds: 2400
    min_reply_interval_seconds: 600
    max_reply_interval_seconds: 1800
    max_posts_per_session: 5
    max_replies_per_session: 10
    max_posts_per_day: 50
    max_replies_per_day: 100
    post_probability: 1.0
    reply_probability: 0.65
    sessions: 3
```

Leave `config/workers.yaml` empty (`workers: []`) and the fleet is generated
from the location allocation instead, using the global behaviour settings as the
template.

The avatar and username are **never** part of the profile: they are read from
the site during the session and recorded on the session row.

## Lifecycle

```
CREATED -> READY -> STARTING -> ACTIVE -> (WAITING | BACKOFF | PAUSED) -> COMPLETED
                                     \-> ERROR / STOPPED
```

| Status | Meaning |
|---|---|
| `ACTIVE` | executing a scenario step |
| `WAITING` | inside a post/reply interval |
| `BACKOFF` | the target signalled throttling; the worker is standing down |
| `PAUSED` | the operator paused the run |
| `STOPPING`/`STOPPED` | stop requested for this worker, its location, or globally |

The current action (`OPENING`, `IDENTITY`, `FORUM`, `READING`, `POSTING`,
`REPLYING`, `SLEEP`, `ENDING`) is shown next to the status.

## Quotas and intervals

* `max_posts_per_session` / `max_replies_per_session` cap one session.
* `max_posts_per_day` / `max_replies_per_day` cap the worker across sessions;
  `0` disables the activity entirely.
* Between two posts (or two replies) the worker sleeps for a random duration
  drawn from its configured interval range. The sleep is interruptible — a stop
  takes effect immediately.
* `post_probability` / `reply_probability` decide whether an eligible session
  actually writes. In a dry run the probability is reported rather than applied,
  so the operator sees full capability.

## Error handling

A failed step is recorded (execution row, event, error row) with a screenshot,
the page HTML and the failing selector under
`data/debug/<TEST-ID>/<worker-id>/`. The session continues until
`safety.max_consecutive_worker_errors` consecutive failures, then aborts.

## Heartbeats

Every state change beats the heartbeat monitor. Workers whose heartbeat lapses
past `controller.heartbeat_timeout_seconds` are reported as stale — the signal
that matters once worker pools run on separate machines.
