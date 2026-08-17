# Installation

## Requirements

* Python 3.11 or newer (3.12+ recommended)
* ~500 MB of disk for the Chromium build
* SQLite (bundled) for development, PostgreSQL for production volumes

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py --init-db
python main.py --check
```

Windows users can run `scripts\setup_windows.bat`, which performs all of the
above; Linux/macOS users can run `scripts/setup_linux.sh`.

## Playwright notes

The Python package and the browser binary are versioned together. If you see

```
Executable doesn't exist at .../chromium-<revision>/chrome-linux/chrome
```

re-run `python -m playwright install chromium` after any Playwright upgrade, or
pin a specific build with `browser.executable_path` in `config/settings.yaml`.

On a bare Linux host also install the system libraries Chromium needs:

```bash
python -m playwright install-deps chromium
```

Air-gapped installs can pre-seed the browsers directory and point
`PLAYWRIGHT_BROWSERS_PATH` at it.

## Database

SQLite is the default and needs no setup:

```bash
python main.py --init-db          # creates data/social_worker.db
```

For a large run, switch to PostgreSQL in `config/settings.yaml`:

```yaml
database:
  url: postgresql+psycopg://social_worker:${DB_PASSWORD}@db.internal:5432/social_worker
  pool_size: 20
  max_overflow: 40
```

Install the driver with `pip install "psycopg[binary]"` and run
`python main.py --init-db` again. Environment variables inside the URL are
expanded at load time, so credentials can stay in `.env`.

## Verifying the install

```bash
python main.py --check            # config, database, locations, content, Playwright
python -m pytest -q               # unit + integration suites
python -m pytest -m browser -q    # real Chromium against the mock site
```
