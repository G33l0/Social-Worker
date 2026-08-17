# Scenarios

A scenario is the ordered list of steps a simulated visitor performs.

## Built-ins

| Scenario | Shape |
|---|---|
| `scenario_01` | visit only |
| `scenario_02` | visit + take the site-provided identity |
| `scenario_03` | visit + identity + browse a forum |
| `scenario_04` | visit + create a post |
| `scenario_05` | visit + reply to an existing initial post |
| `scenario_06` | full session: browse, post, read, reply, switch forum |
| `scenario_07` | heavy forum activity: multiple posts and replies |

`scenario_01`–`scenario_03` and `scenario_05` disable posting; `scenario_02`,
`scenario_03` and `scenario_04` disable replying — the flags are on the
scenario, so a scenario can never write more than it claims.

## Steps

| Action | Effect |
|---|---|
| `OPEN_SITE` | navigate to the target and wait for the identity chooser |
| `WAIT_FOR_LOAD` | wait for network idle |
| `SELECT_AVATAR` | read the offered avatars, pick one |
| `SELECT_USERNAME` | read the offered usernames, pick one, confirm |
| `ENTER_FORUM` | pick a forum per the selection mode and enter it |
| `BROWSE_FORUM` | scroll the feed like a reader |
| `READ_POST` | open an initial post so its replies are visible |
| `CREATE_POST` | publish a post from the content library |
| `CREATE_REPLY` | reply to the selected initial post |
| `SWITCH_FORUM` | return to the forum list and enter a different forum |
| `THINK` | pause, bounded by the session think-time window |
| `END_SESSION` | finish the visit |

Each step accepts `repeat`, `probability`, `min_seconds`, `max_seconds`,
`forum` and `category`.

## Writing your own

```yaml
scenarios:
  - name: scenario_lurker
    description: Long browse, occasional reply
    allow_posting: false
    steps:
      - {action: OPEN_SITE}
      - {action: WAIT_FOR_LOAD}
      - {action: SELECT_AVATAR}
      - {action: SELECT_USERNAME}
      - {action: ENTER_FORUM}
      - {action: BROWSE_FORUM, repeat: 4}
      - {action: THINK, min_seconds: 10, max_seconds: 30}
      - {action: READ_POST}
      - {action: CREATE_REPLY, probability: 0.3}
      - {action: END_SESSION}
```

Scenario think times are clamped into the session's configured think-time
window, so one global setting can slow a whole run down or speed a rehearsal up
without editing every scenario.

Select the default in menu option 9, or per worker with the `scenario` field.
