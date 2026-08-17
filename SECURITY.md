# Security and Responsible Use

## What Social Worker is for

Social Worker generates realistic visitor traffic so an operator can measure how
their own website behaves under load: response times, error rates, database
load, concurrency limits and geographic traffic distribution.

## Authorized use only

Run it only against:

* a website you own or operate, or
* a website whose operator has given you explicit written permission to
  generate automated test traffic.

Before a load test can start, Social Worker requires:

1. `authorized_test_mode: true` in `config/settings.yaml`, and
2. an authorization reference (ticket, contract or approval note) when the
   target environment is `production`.

The reference is stored with the test run so every run is auditable.

## What this project deliberately does not do

Social Worker does not implement, and pull requests adding them will not be
accepted:

* CAPTCHA solving or bypassing
* anti-bot or WAF evasion
* browser-fingerprint spoofing intended to defeat security controls
* credential theft, credential stuffing or account takeover
* unauthorized account creation
* techniques for disguising the origin or nature of the traffic

Every request Social Worker makes is labelled: contexts send
`X-Load-Test: social-worker`, `X-Load-Test-Client: <identify_as>/<version>` and
`X-Load-Test-Location: <location>`, so the operator can identify, throttle or
exclude synthetic traffic in their own logs.

## Rate limiting is respected, never circumvented

If the target answers HTTP 429 (or any status listed in
`safety.rate_limit_status_codes`), the worker records the event, backs off with
exponential delay, and the run stops entirely once the configured streak is
reached. There is no retry-with-different-identity path.

## Secrets

Credentials are never written to ordinary logs: a redaction filter strips
passwords, tokens, API keys, cookies and session identifiers from every log
record. The controller API token is stripped from the configuration snapshot
stored with each test run. Keep secrets in `.env` or your platform's secret
store, not in the YAML configuration.

## Reporting a vulnerability

Open an issue describing the problem and the affected version. Do not include
credentials or data captured from a live system in the report.
