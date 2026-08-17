# Geographic testing

The point of the location system is to answer *how does the site behave for
visitors arriving from each region we serve?* — not to disguise where traffic
comes from. Every request carries `X-Load-Test-Location`.

## Defining locations

```yaml
locations:
  - key: nigeria
    name: Nigeria
    region_group: africa
    country: Nigeria
    city: Lagos
    timezone: Africa/Lagos
    locale: en-NG
    workers: 10                 # requested allocation
    max_concurrent_workers: 30  # hard ceiling for this location
    runner: local               # local | remote | proxy
    schedule: business_hours    # optional
    enabled: true
```

`timezone` and `locale` are applied to the browser context, so the site sees a
plausible regional client without any fingerprint spoofing.

## Where the traffic actually originates

| `runner` | Meaning | Extra field |
|---|---|---|
| `local` | this machine — development and mock-site rehearsals | – |
| `remote` | a worker agent you operate in that region | `runner_endpoint` |
| `proxy` | an explicitly configured, authorized test proxy | `proxy_url` |

Use real regional infrastructure (cloud runners, VMs, corporate egress) for
meaningful geographic numbers. Social Worker will not help hide the origin or
nature of the traffic.

## Allocation

The global budget is split across enabled locations by their requested
`workers`, using largest-remainder scaling when the total exceeds
`concurrency.max_global_workers`. A location never receives more than its own
`max_concurrent_workers`.

```
requested: usa 20, canada 10, uk 10, nigeria 10   global limit: 25
allocated: usa 10, canada 5,  uk 5,  nigeria 5    total 25
```

Preview it in menu option 2 → *Preview allocation*.

## Concurrency

Three independent ceilings are enforced before any session starts:

1. **Global** — `concurrency.max_global_workers`
2. **Per location** — `max_concurrent_workers`
3. **Per worker** — `concurrency.max_concurrent_sessions_per_worker`

Plus sliding-window session limits per minute, hour and day. Live and peak
utilisation for each are visible in the dashboard and stored with the run.

## Schedules

```yaml
schedules:
  - name: business_hours
    days: [mon, tue, wed, thu, fri]
    windows:
      - {start: "08:00", end: "18:00"}
  - name: off_peak
    windows:
      - {start: "22:00", end: "04:00"}     # wrapping past midnight is supported
```

Windows are evaluated in UTC. A location bound to a schedule is skipped by the
scheduler outside its window — the jobs are marked `SKIPPED`, not failed.

## Reading the results

The geographic report gives one row per location:

```
LOCATION       | SESSIONS | POSTS | REPLIES | AVG RESPONSE | ERRORS
canada         |       31 |    31 |      42 |     141.5 ms |      0
nigeria        |       41 |    41 |      52 |     210.8 ms |      2
united_states  |       65 |    65 |      81 |     132.4 ms |      1
```

Compare `avg_response_ms` across regions to see where latency or error rates
diverge, and `allocated_workers` versus `sessions` to confirm the plan ran as
intended.
