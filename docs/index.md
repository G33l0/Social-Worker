# Social Worker documentation

Social Worker is an authorized load- and behaviour-testing platform for
anonymous/temporary-identity forum websites. It drives real Chromium sessions
that take the avatar and username the site offers, browse forums, and publish
posts and replies, while recording timing, error and concurrency telemetry.

| Guide | Covers |
|---|---|
| [Installation](installation.md) | Python, dependencies, Playwright, database |
| [Configuration](configuration.md) | every YAML document and what each key does |
| [CLI](cli.md) | the 21-option console, wizards and the live monitor |
| [Workers](workers.md) | worker profiles, quotas, intervals, state machine |
| [Scenarios](scenarios.md) | the seven built-ins and how to write your own |
| [Geographic testing](geographic-testing.md) | locations, allocation, concurrency, schedules |
| [Reporting](reporting.md) | the eight report categories and where they land |
| [Architecture](architecture.md) | how the pieces fit together |
| [Troubleshooting](troubleshooting.md) | symptoms, causes, fixes |

Start with the [README](../README.md) for the quick path: install, start the
mock site, run a dry test, run a load test, read the report.

## Authorized use

Every run is gated on `website.authorized_test_mode`, and production targets
additionally require an authorization reference that is stored with the test
run. Rate limiting from the target is always respected and never circumvented.
See [SECURITY.md](../SECURITY.md).
