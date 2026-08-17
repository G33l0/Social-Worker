# Contributing

Thanks for helping improve Social Worker.

## Ground rules

Social Worker is a load- and behaviour-testing tool for systems the operator
owns or is authorized to test. Contributions that add CAPTCHA solving, anti-bot
or WAF evasion, fingerprint spoofing intended to defeat security controls,
credential attacks, or ways to disguise the origin of the traffic will be
declined. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
python -m playwright install chromium
python main.py --init-db
```

## Before opening a pull request

```bash
python -m pytest -q                # unit + integration suites
python -m pytest -m browser -q     # real-browser suite against the mock site
python -m compileall -q app cli mock_site main.py
```

## Style

* Python 3.11+ with type hints on public functions and methods.
* Docstrings on every module, class and public function.
* Keep modules small and single-purpose; the package layout mirrors the
  architecture described in the README.
* Site-specific selectors belong in `config/selectors.yaml`, never inline in
  automation code.
* New behaviour needs a test. Prefer the fake site adapter in
  `tests/conftest.py` so the suite stays fast and browser-free; add a
  `@pytest.mark.browser` test when the change touches real browser interaction.

## Adding support for another site

Implement `BaseSiteAdapter` in `app/site/`, keep the selectors in configuration,
and add a mock-site fixture that exercises the new adapter end to end.
