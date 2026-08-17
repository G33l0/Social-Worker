#!/usr/bin/env python3
"""Social Worker - authorized load and behaviour testing platform.

Entry point.  Start the interactive management console with::

    python main.py

Other entry points::

    python main.py --version
    python main.py --check           validate configuration and dependencies
    python main.py --init-db         create or upgrade the database schema
    python main.py --dry-run         run a one-worker dry test and exit
    python main.py --headless-run    start a load test without the menu
    python main.py --config-dir DIR  use a different configuration directory
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.utils.config import ConfigPaths, config_exists, load_settings  # noqa: E402
from app.utils.logger import configure_logging  # noqa: E402

VERSION = "1.0.0"

AUTHORIZATION_BANNER = """
Social Worker generates automated traffic for load and behaviour testing.
Use it only against systems you own or are explicitly authorized to test.
""".strip()


def _check(paths: ConfigPaths) -> int:
    """Validate the environment and configuration."""
    from app.browser.browser_manager import BrowserManager
    from app.core.content_manager import ContentManager
    from app.database.database import Database
    from app.locations.location_manager import LocationManager

    print(f"Social Worker {VERSION}")
    print(f"Python {sys.version.split()[0]}")
    ok = True

    if not config_exists(paths):
        print(f"[!] No configuration at {paths.settings} - run 'python main.py' "
              "to start the setup wizard")
        return 1
    settings = load_settings(paths.settings)
    print(f"[OK] Configuration loaded from {paths.settings}")
    print(f"     target={settings.website.url} environment="
          f"{settings.website.environment.value} "
          f"authorized={settings.website.authorized_test_mode}")

    database = Database(settings.database)
    if database.healthcheck():
        print(f"[OK] Database reachable ({settings.database.url})")
    else:
        print(f"[X] Database unreachable ({settings.database.url})")
        ok = False

    locations = LocationManager.load(paths.locations, paths.schedules)
    warnings = locations.validate(global_limit=settings.concurrency.max_global_workers)
    print(f"[OK] {len(locations.all())} location(s), "
          f"{len(locations.enabled())} enabled")
    for warning in warnings:
        print(f"     [!] {warning}")

    content = ContentManager(settings.content, settings.posting, settings.replies)
    loaded = content.load()
    print(f"[OK] Content: {loaded['posts']} post(s), {loaded['replies']} reply/replies")
    ready, problems = content.is_ready()
    for problem in problems:
        print(f"     [!] {problem}")
    ok = ok and (ready or settings.dry_run)

    if BrowserManager.is_available():
        print("[OK] Playwright is installed")
    else:
        print("[X] Playwright is not installed - run: pip install playwright && "
              "python -m playwright install chromium")
        ok = False

    if not settings.website.authorized_test_mode:
        print("[!] Authorized test mode is OFF - load tests will refuse to start")

    return 0 if ok else 1


def _init_db(paths: ConfigPaths) -> int:
    """Create or upgrade the database schema."""
    from app.database.database import Database
    from app.database.migrations import describe, upgrade

    settings = load_settings(paths.settings) if config_exists(paths) else None
    from app.utils.config import Settings

    settings = settings or Settings()
    database = Database(settings.database)
    version = upgrade(database)
    tables = describe(database)
    print(f"Schema version {version} at {settings.database.url}")
    for table, columns in tables.items():
        print(f"  {table:<12} {len(columns)} column(s)")
    return 0


async def _dry_run(paths: ConfigPaths, workers: int) -> int:
    """Run a dry test without the menu."""
    from app.core.controller import Controller

    controller = Controller(load_settings(paths.settings), config_paths=paths)
    result = await controller.run_dry(workers=workers)
    import json

    print(json.dumps(result["findings"], indent=2, default=str))
    await controller.shutdown()
    return 0


async def _headless_run(paths: ConfigPaths, label: str) -> int:
    """Start a load test and wait for it to finish, without the menu."""
    from app.core.controller import Controller

    controller = Controller(load_settings(paths.settings), config_paths=paths)
    try:
        await controller.start(label=label)
        await controller.wait()
        status = controller.status()
        stats = status.get("global", {})
        print(f"Test run {status.get('test_run_id')} finished: "
              f"{stats.get('completed', 0)} session(s), {stats.get('posts', 0)} post(s), "
              f"{stats.get('replies', 0)} reply/replies, {stats.get('errors', 0)} error(s)")
        return 0
    finally:
        await controller.shutdown()


def main() -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(
        prog="social-worker",
        description="Social Worker - authorized website load and behaviour testing")
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("--check", action="store_true",
                        help="validate configuration and dependencies")
    parser.add_argument("--init-db", action="store_true",
                        help="create or upgrade the database schema")
    parser.add_argument("--dry-run", action="store_true",
                        help="run a dry test (no posts or replies submitted)")
    parser.add_argument("--workers", type=int, default=1,
                        help="worker count for --dry-run")
    parser.add_argument("--headless-run", action="store_true",
                        help="start a load test without the interactive menu")
    parser.add_argument("--label", default="load-test", help="label for --headless-run")
    parser.add_argument("--config-dir", default="config",
                        help="configuration directory (default: config)")
    arguments = parser.parse_args()

    paths = ConfigPaths.under(arguments.config_dir)

    if arguments.version:
        print(f"Social Worker {VERSION}")
        return 0

    configure_logging(console=False)

    if arguments.check:
        return _check(paths)
    if arguments.init_db:
        return _init_db(paths)
    if arguments.dry_run:
        print(AUTHORIZATION_BANNER)
        return asyncio.run(_dry_run(paths, arguments.workers))
    if arguments.headless_run:
        print(AUTHORIZATION_BANNER)
        return asyncio.run(_headless_run(paths, arguments.label))

    from cli.main import main as console_main

    return console_main(["--config-dir", arguments.config_dir])


if __name__ == "__main__":
    raise SystemExit(main())
