"""End-to-end browser tests: real Chromium against the bundled mock site.

Skipped automatically when Playwright or its Chromium build is unavailable, so
the unit suite still runs in a bare environment.  Run them explicitly with::

    pytest tests/test_browser_integration.py -m browser
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from app.browser.browser_manager import BrowserManager
from app.core.controller import Controller
from app.database.repository import Repository
from app.locations.location import Location
from app.locations.location_manager import LocationManager
from app.utils.config import ConfigPaths, save_settings, save_yaml_document

pytestmark = pytest.mark.browser

playwright_missing = not BrowserManager.is_available()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def mock_site() -> str:
    """Run the mock site in a background thread for the whole module."""
    import uvicorn

    from mock_site.test_site import STATE, app

    STATE.reset()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:  # pragma: no cover - environment dependent
        pytest.skip("mock site did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser_paths(tmp_path, settings, mock_site) -> ConfigPaths:
    """Configuration pointing a single local location at the mock site."""
    settings.website.url = mock_site
    settings.browser.headless = True
    settings.browser.typing_delay_max_ms = 0
    settings.browser.human_typing = False
    settings.concurrency.max_global_workers = 2
    settings.session.max_think_time_seconds = 0.2
    settings.session.min_think_time_seconds = 0.1
    settings.posting.max_posts_per_session = 1
    settings.replies.max_replies_per_session = 1

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    paths = ConfigPaths.under(config_dir)
    save_settings(settings, paths.settings)
    LocationManager([
        Location(key="canada", country="Canada", workers=1, max_concurrent_workers=1),
        Location(key="nigeria", country="Nigeria", workers=1, max_concurrent_workers=1),
    ]).save(paths.locations)
    save_yaml_document(paths.workers, {"workers": []})
    (tmp_path / "posts.txt").write_text(
        "\n".join(f"Browser test post {index:03d}" for index in range(1, 21)),
        encoding="utf-8")
    (tmp_path / "replies.txt").write_text(
        "\n".join(f"Browser test reply {index:03d}" for index in range(1, 21)),
        encoding="utf-8")
    return paths


@pytest.mark.skipif(playwright_missing, reason="Playwright is not installed")
async def test_single_worker_against_the_mock_site(settings, browser_paths, mock_site):
    """One real browser session: identity, forum, post and reply."""
    import httpx

    settings.concurrency.max_global_workers = 1
    save_settings(settings, browser_paths.settings)

    async with httpx.AsyncClient(trust_env=False) as client:
        before = (await client.get(f"{mock_site}/api/stats")).json()

    controller = Controller(settings, config_paths=browser_paths)
    try:
        await controller.start(label="browser-single", wait=True)
        status = controller.status()
        assert status["global"]["completed"] == 1
        assert status["global"]["errors"] == 0
        assert status["global"]["posts"] == 1
        assert status["global"]["replies"] == 1

        async with httpx.AsyncClient(trust_env=False) as client:
            after = (await client.get(f"{mock_site}/api/stats")).json()
        assert after["created_posts"] == before["created_posts"] + 1
        assert after["replies"] == before["replies"] + 1

        sessions = Repository(controller.database).sessions(controller.test_run_id)
        assert sessions[0]["username"]
        assert sessions[0]["avatar"]
        assert sessions[0]["pages_visited"] >= 3
    finally:
        await controller.shutdown()


@pytest.mark.skipif(playwright_missing, reason="Playwright is not installed")
async def test_multiple_workers_respect_concurrency(settings, browser_paths, mock_site):
    """Two workers in two locations, each capped at one concurrent session."""
    controller = Controller(settings, config_paths=browser_paths)
    try:
        await controller.start(label="browser-multi", wait=True)
        status = controller.status()
        assert status["global"]["completed"] == 2
        concurrency = status["scheduler"]["concurrency"]
        assert concurrency["global_peak"] <= 2
        for location, values in concurrency["locations"].items():
            assert values["peak"] <= values["limit"], location

        locations = {row["location"] for row in status["locations"]}
        assert {"canada", "nigeria"} <= locations
    finally:
        await controller.shutdown()


@pytest.mark.skipif(playwright_missing, reason="Playwright is not installed")
async def test_dry_run_against_the_mock_site(settings, browser_paths, mock_site):
    """A dry run detects avatars, usernames, forums and posts, and submits nothing."""
    import httpx

    async with httpx.AsyncClient(trust_env=False) as client:
        before = (await client.get(f"{mock_site}/api/stats")).json()

    controller = Controller(settings, config_paths=browser_paths)
    try:
        result = await controller.run_dry(workers=1)
        finding = result["findings"][0]
        assert len(finding["avatars_detected"]) == 6
        assert len(finding["usernames_detected"]) == 5
        assert len(finding["forums_detected"]) == 4
        assert finding["initial_posts_detected"] >= 1
        assert finding["would_create_post"] is True

        async with httpx.AsyncClient(trust_env=False) as client:
            after = (await client.get(f"{mock_site}/api/stats")).json()
        assert after["created_posts"] == before["created_posts"]
        assert after["replies"] == before["replies"]
    finally:
        await controller.shutdown()


@pytest.mark.skipif(playwright_missing, reason="Playwright is not installed")
async def test_failure_capture_writes_debug_artefacts(settings, browser_paths, tmp_path,
                                                      mock_site):
    """A missing selector produces a screenshot, page HTML and an error row."""
    from app.browser.selectors import SelectorSet

    selectors = SelectorSet()
    selectors.identity.avatar_options = ["#no-such-avatar-element"]
    selectors.save(browser_paths.selectors)

    settings.concurrency.max_global_workers = 1
    settings.safety.max_consecutive_worker_errors = 1
    save_settings(settings, browser_paths.settings)

    controller = Controller(settings, config_paths=browser_paths)
    controller.settings.browser.screenshot_on_failure = True
    try:
        await controller.start(label="browser-failure", wait=True)
        errors = Repository(controller.database).errors(controller.test_run_id)
        assert errors
        assert any(error["screenshot_path"] for error in errors)
        debug_root = tmp_path / "debug" / controller.test_run_id
        assert debug_root.exists()
        assert list(debug_root.rglob("*.png"))
        assert list(debug_root.rglob("*.html"))
    finally:
        await controller.shutdown()
