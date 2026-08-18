# Configuration

All configuration lives in `config/` and is validated on load, so a typo fails
immediately with a message naming the field.

| File | Contents |
|---|---|
| `settings.yaml` | website, browser, concurrency, posting, replies, session, forums, content, safety, database, logging, reporting, controller |
| `locations.yaml` | geographic test locations |
| `workers.yaml` | explicit worker definitions (optional) |
| `schedules.yaml` | named time windows |
| `scenarios.yaml` | session scenarios |
| `selectors.yaml` | site-specific CSS selectors |

`*.example.yaml` files are shipped as known-good starting points.

## settings.yaml

### website

| Key | Meaning |
|---|---|
| `url` | the target under authorized test |
| `environment` | `local`, `staging` or `production` |
| `authorized_test_mode` | must be `true` before any run starts |
| `authorization_reference` | ticket/contract id; required for `production` |
| `operator_contact` | recorded with the run |
| `identify_as` | value sent in `X-Load-Test-Client` |

### browser

Headless toggle, viewport, timeouts, `screenshot_on_failure`,
`save_html_on_failure`, console/network capture, `max_browsers` and
`contexts_per_browser` (the browser pool), and typing behaviour
(`human_typing`, `typing_delay_min_ms`, `typing_delay_max_ms`).

### concurrency

`max_global_workers`, `default_max_workers_per_location`,
`max_concurrent_sessions_per_worker`, `ramp_up_seconds`, and the sliding-window
limits `max_requests_per_minute`, `max_sessions_per_hour`,
`max_sessions_per_day`.

### posting / replies

Enable flags, interval ranges (seconds), per-session and per-day maxima,
probability, content selection mode, and — for replies — the target selection
mode (`RANDOM_INITIAL_POST`, `OLDEST_INITIAL_POST`, `NEWEST_INITIAL_POST`,
`RANDOM_WITH_FILTER`, `SPECIFIC_POST`).

A per-day maximum of `0` disables that activity for the worker entirely.

### session

Default scenario, session duration bounds, think-time bounds, browse depth and
`sessions_per_worker`.

### forums

`mode` is one of `RANDOM_FORUM`, `SPECIFIC_FORUM`, `ROTATE_FORUMS` or
`WEIGHTED_FORUM_SELECTION`. Weights are given as percentages and normalised;
`allowed_forums` restricts testing to a subset of the forums discovered on the
site.

### safety

Rate-limit statuses to honour, back-off shape, the consecutive-error limit per
worker, the error-rate abort threshold and `stop_on_rate_limit_streak`.

`honour_robots_txt` (on by default) fetches the target's robots.txt during
preflight and blocks the run if it disallows the path being tested. Turn it off
only for a target you own where the policy is aimed at crawlers rather than your
own test client.

### thresholds

Acceptance criteria evaluated when a run finishes — see
[reporting](reporting.md#acceptance-verdict). A limit of `0` disables that check.

### controller

Settings for the optional HTTP control API (`api_enabled`, `host`, `port`,
`api_token`, `redis_url`) and the heartbeat interval/timeout used to spot a dead
regional runner.

## Editing intervals safely

Minimum/maximum pairs are validated together. When changing both from code, use
the atomic helper so ordering cannot trip the validation:

```python
settings.posting.apply(min_interval_seconds=300, max_interval_seconds=900)
```

## selectors.yaml

Each logical element takes a list of candidate selectors, tried in order, so a
minor markup change does not end a run:

```yaml
selectors:
  posts:
    post_items: ["[data-testid='post-item']", "article.post", "li.post"]
    post_id_attribute: data-post-id
```

Run a dry test (`python main.py --dry-run`) after any selector change: it
reports exactly what was detected without submitting anything.

## Retired keys

Settings removed in a newer version are ignored with a warning rather than
failing the load, so an existing configuration keeps working after an upgrade.
Re-save from any configuration menu to tidy the file.
