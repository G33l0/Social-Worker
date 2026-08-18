# Screenshots

Every image here is a capture of a real run against the bundled mock site — the
console output is verbatim, rendered in a terminal frame; the report pages are
browser screenshots of generated HTML.

| File | What it shows |
|---|---|
| `01-console-menu.png` | the 21-option management console |
| `02-live-monitor.png` | the live monitor during an 8-worker, 3-location run |
| `03-dry-run.png` | a dry run reporting what it detected, submitting nothing |
| `04-acceptance-verdict.png` | `python main.py --verdict <TEST-ID>` |
| `05-run-comparison.png` | `python main.py --compare <baseline> <candidate>` |
| `06-html-report.png` / `-dark.png` | the generated HTML report, both themes |
| `07-mock-site.png` | the mock forum's identity chooser |
| `08-mock-forum.png` | a mock forum page with the post composer |

Regenerate them after a UI change by running a test against the mock site and
re-capturing; nothing here is hand-drawn or mocked up.
