"""Social Worker interactive management console (spec section 15).

Run it with ``python main.py``.  The console is asynchronous: while the menu
waits for input the controller keeps running the test in the background, so
Live Monitoring, Pause/Resume and STOP ALL all work on a live run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from cli import console
from cli.dashboard import live_monitor, render
from app.core.controller import AuthorizationError, Controller, ControllerState
from app.core.content_manager import ContentManager
from app.core.worker_manager import WorkerManager
from app.database.database import get_database
from app.database.migrations import upgrade
from app.database.repository import Repository
from app.locations.location_manager import LocationManager
from app.reporting.comparison import compare
from app.reporting.report_manager import REPORT_CATEGORIES, ReportManager
from app.scheduler.scenarios import ScenarioLibrary
from app.utils.config import (
    ConfigPaths, ContentSelectionMode, Environment, ForumSelectionMode,
    ReplyTargetMode, Settings, config_exists, load_settings, save_settings,
)
from app.utils.logger import configure_logging, get_logger
from app.utils.time_utils import timestamp_slug
from app.utils.validation import ValidationError, validate_url, validate_weights

LOGGER = get_logger("cli")

MENU = [
    "Configure Website", "Configure Locations", "Configure Workers", "Import Posts",
    "Import Replies", "Configure Forums", "Configure Posting Behavior",
    "Configure Reply Behavior", "Configure Session Behavior", "Configure Concurrency",
    "Test Single Worker", "Run Dry Test", "Start Load Test", "Pause Load Test",
    "Resume Load Test", "Stop Load Test", "Live Monitoring", "View Reports",
    "Export Results", "Backup", "Exit",
]

BANNER_TITLE = "SOCIAL WORKER"
BANNER_SUB = "LOAD TEST MANAGEMENT CONSOLE"


class ConsoleApp:
    """The interactive console."""

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self.paths = paths or ConfigPaths()
        self.settings: Settings = (load_settings(self.paths.settings)
                                   if config_exists(self.paths) else Settings())
        configure_logging(self.settings.logging.directory,
                          level=self.settings.logging.level,
                          console=False)
        self.locations = LocationManager.load(self.paths.locations, self.paths.schedules)
        self.workers = WorkerManager(self.settings, self.locations)
        self.workers.load(self.paths.workers)
        self.content = ContentManager(self.settings.content, self.settings.posting,
                                      self.settings.replies)
        self.scenarios = ScenarioLibrary.load(self.paths.scenarios)
        self.controller: Controller | None = None
        self._first_run = not config_exists(self.paths)

    # ------------------------------------------------------------------- loop
    async def run(self) -> int:
        """Main menu loop.  Returns the process exit code."""
        console.enable_color()
        console.clear()
        print(console.paint(console.banner(BANNER_TITLE, BANNER_SUB), "cyan"))

        if self._first_run:
            console.warn("No configuration found - starting the setup wizard.")
            await self._to_thread(self._setup_wizard)

        try:
            while True:
                self._print_menu()
                choice = await self._to_thread(
                    input, console.paint("Select an option (or 'stop' for STOP ALL)\n> ",
                                         "cyan"))
                choice = choice.strip().lower()
                if choice in {"stop", "stopall", "stop all", "0"}:
                    await self._emergency_stop()
                    continue
                if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
                    console.error(f"Choose 1-{len(MENU)}.")
                    continue
                option = int(choice)
                if option == len(MENU):
                    if await self._exit():
                        return 0
                    continue
                await self._dispatch(option)
        except KeyboardInterrupt:
            print()
            console.warn("Interrupted - stopping any running test.")
            await self._shutdown()
            return 130

    def _print_menu(self) -> None:
        print()
        print(console.paint(console.rule("=", console.WIDTH), "cyan"))
        print(console.paint(BANNER_TITLE.center(console.WIDTH), "bold"))
        print(console.paint(BANNER_SUB.center(console.WIDTH), "bold"))
        print(console.paint(console.rule("=", console.WIDTH), "cyan"))
        state = self.controller.state if self.controller else ControllerState.IDLE
        auth = ("AUTHORIZED" if self.settings.website.authorized_test_mode
                else "NOT AUTHORIZED")
        print(f" Target : {self.settings.website.url}  "
              f"({self.settings.website.environment.value})")
        print(f" State  : {state}   Mode: {auth}"
              f"   Workers: {self.settings.concurrency.max_global_workers}")
        print(console.paint(console.rule("-", console.WIDTH), "grey"))
        half = (len(MENU) + 1) // 2
        for index in range(half):
            left = f"{index + 1:2d}. {MENU[index]}"
            right_index = index + half
            right = (f"{right_index + 1:2d}. {MENU[right_index]}"
                     if right_index < len(MENU) else "")
            print(f" {left:<32}{right}")
        print(console.paint(console.rule("=", console.WIDTH), "cyan"))

    async def _dispatch(self, option: int) -> None:
        handlers = {
            1: self._configure_website, 2: self._configure_locations,
            3: self._configure_workers, 4: self._import_posts,
            5: self._import_replies, 6: self._configure_forums,
            7: self._configure_posting, 8: self._configure_replies,
            9: self._configure_session, 10: self._configure_concurrency,
            11: self._test_single_worker, 12: self._run_dry_test,
            13: self._start_test, 14: self._pause_test, 15: self._resume_test,
            16: self._stop_test, 17: self._live_monitor, 18: self._view_reports,
            19: self._export_results, 20: self._backup,
        }
        handler = handlers[option]
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                # Synchronous handlers block on input(); run them on a worker
                # thread so a live test run keeps dispatching, flushing metrics
                # and answering the heartbeat while a wizard is open.
                await asyncio.to_thread(handler)
        except (ValidationError, ValueError, KeyError) as exc:
            console.error(str(exc))
            await self._to_thread(console.pause)
        except AuthorizationError as exc:
            console.error(str(exc))
            await self._to_thread(console.pause)
        except Exception as exc:  # noqa: BLE001 - console must not crash
            LOGGER.exception("Menu action %s failed", option)
            console.error(f"{type(exc).__name__}: {exc}")
            await self._to_thread(console.pause)

    @staticmethod
    async def _to_thread(function: Any, *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)

    # -------------------------------------------------------------- 1 website
    def _setup_wizard(self) -> None:
        from cli.setup_wizard import run_setup_wizard

        self.settings = run_setup_wizard(self.settings, self.paths)
        self._reload_managers()
        self._first_run = False

    def _configure_website(self) -> None:
        console.header("CONFIGURE WEBSITE")
        website = self.settings.website
        print(console.keyvalues({
            "URL": website.url,
            "Environment": website.environment.value,
            "Authorized test mode": website.authorized_test_mode,
            "Authorization reference": website.authorization_reference or "-",
            "Operator contact": website.operator_contact or "-",
            "Identifies as": website.identify_as,
        }))
        print()
        website.url = validate_url(console.ask("Website URL", website.url, required=True))
        environment = console.choose("Testing environment:",
                                     ["Local", "Staging", "Production"],
                                     default=["local", "staging", "production"].index(
                                         website.environment.value) + 1)
        website.environment = Environment(environment.lower())
        website.authorized_test_mode = console.ask_bool(
            "Authorized test mode (you own this site or hold written permission)",
            website.authorized_test_mode)
        if website.authorized_test_mode:
            website.authorization_reference = console.ask(
                "Authorization reference", website.authorization_reference,
                required=website.environment is Environment.PRODUCTION)
            website.operator_contact = console.ask("Operator contact",
                                                   website.operator_contact)
        website.identify_as = console.ask(
            "Identify test traffic as (sent in X-Load-Test-Client)",
            website.identify_as)
        self._save_settings()

    # ------------------------------------------------------------ 2 locations
    def _configure_locations(self) -> None:
        from cli.location_wizard import run_location_wizard

        self.locations = run_location_wizard(self.settings, paths=self.paths,
                                             manager=self.locations)
        self.workers.locations = self.locations

    # -------------------------------------------------------------- 3 workers
    def _configure_workers(self) -> None:
        from cli.worker_wizard import run_worker_wizard

        run_worker_wizard(self.settings, self.workers, paths=self.paths)

    # -------------------------------------------------------------- 4/5 content
    def _import_posts(self) -> None:
        from cli.content_wizard import run_content_wizard

        run_content_wizard(self.settings, manager=self.content, library="posts")
        self._save_settings(quiet=True)

    def _import_replies(self) -> None:
        from cli.content_wizard import run_content_wizard

        run_content_wizard(self.settings, manager=self.content, library="replies")
        self._save_settings(quiet=True)

    # --------------------------------------------------------------- 6 forums
    def _configure_forums(self) -> None:
        console.header("CONFIGURE FORUMS")
        forums = self.settings.forums
        print(console.keyvalues({
            "Mode": forums.mode.value,
            "Specific forum": forums.specific_forum or "-",
            "Weights": ", ".join(f"{key}={value:g}"
                                 for key, value in forums.weights.items()) or "-",
            "Allow-list": ", ".join(forums.allowed_forums) or "(all discovered forums)",
        }))
        mode = console.choose("Forum selection mode:",
                              [item.value for item in ForumSelectionMode],
                              default=[item.value for item in ForumSelectionMode]
                              .index(forums.mode.value) + 1)
        forums.mode = ForumSelectionMode(mode)

        if forums.mode is ForumSelectionMode.SPECIFIC:
            forums.specific_forum = console.ask("Forum key", forums.specific_forum,
                                                required=True)
        elif forums.mode is ForumSelectionMode.WEIGHTED:
            console.info("Enter weights as percentages, e.g. general=40,questions=15")
            raw = console.ask("Weights",
                              ",".join(f"{key}={value:g}"
                                       for key, value in forums.weights.items()))
            weights: dict[str, float] = {}
            for part in raw.split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                weights[key.strip().lower()] = float(value)
            validate_weights(weights)
            forums.weights = weights
        elif forums.mode is ForumSelectionMode.ROTATE:
            raw = console.ask("Rotation order (comma separated forum keys)",
                              ",".join(forums.rotation_order))
            forums.rotation_order = [item.strip().lower()
                                     for item in raw.split(",") if item.strip()]

        allow = console.ask("Restrict to these forums (comma separated, blank = all)",
                            ",".join(forums.allowed_forums))
        forums.allowed_forums = [item.strip().lower()
                                 for item in allow.split(",") if item.strip()]
        self._save_settings()

    # -------------------------------------------------------------- 7 posting
    def _configure_posting(self) -> None:
        console.header("CONFIGURE POSTING BEHAVIOR")
        posting = self.settings.posting
        print(console.keyvalues({
            "Enabled": posting.enabled,
            "Interval": f"{posting.min_interval_seconds / 60:g}-"
                        f"{posting.max_interval_seconds / 60:g} minutes",
            "Max posts/session": posting.max_posts_per_session,
            "Max posts/day": posting.max_posts_per_day,
            "Probability": f"{posting.probability:.0%}",
            "Content selection": posting.content_selection_mode.value,
        }))
        posting.enabled = console.ask_bool("Enable posting", posting.enabled)
        if posting.enabled:
            minimum = console.ask_float("Minimum interval (minutes)",
                                        posting.min_interval_seconds / 60, minimum=0)
            maximum = console.ask_float("Maximum interval (minutes)",
                                        posting.max_interval_seconds / 60,
                                        minimum=minimum)
            posting.apply(min_interval_seconds=minimum * 60,
                          max_interval_seconds=maximum * 60)
            posting.max_posts_per_session = console.ask_int(
                "Maximum posts/session", posting.max_posts_per_session, minimum=0)
            posting.max_posts_per_day = console.ask_int(
                "Maximum posts/day", posting.max_posts_per_day, minimum=0)
            posting.probability = console.ask_float(
                "Probability a session posts (0-1)", posting.probability,
                minimum=0.0, maximum=1.0)
            posting.content_selection_mode = ContentSelectionMode(console.choose(
                "Content selection mode:", [item.value for item in ContentSelectionMode],
                default=3))
            posting.randomized_delay = console.ask_bool("Randomize the delay",
                                                        posting.randomized_delay)
        self.content.posting = posting
        self.content.build_rotators()
        self._save_settings()

    # -------------------------------------------------------------- 8 replies
    def _configure_replies(self) -> None:
        console.header("CONFIGURE REPLY BEHAVIOR")
        replies = self.settings.replies
        print(console.keyvalues({
            "Enabled": replies.enabled,
            "Interval": f"{replies.min_interval_seconds / 60:g}-"
                        f"{replies.max_interval_seconds / 60:g} minutes",
            "Max replies/session": replies.max_replies_per_session,
            "Max replies/day": replies.max_replies_per_day,
            "Probability": f"{replies.probability:.0%}",
            "Target selection": replies.target_selection_mode.value,
        }))
        replies.enabled = console.ask_bool("Enable replying", replies.enabled)
        if replies.enabled:
            minimum = console.ask_float("Minimum interval (minutes)",
                                        replies.min_interval_seconds / 60, minimum=0)
            maximum = console.ask_float("Maximum interval (minutes)",
                                        replies.max_interval_seconds / 60,
                                        minimum=minimum)
            replies.apply(min_interval_seconds=minimum * 60,
                          max_interval_seconds=maximum * 60)
            replies.max_replies_per_session = console.ask_int(
                "Maximum replies/session", replies.max_replies_per_session, minimum=0)
            replies.max_replies_per_day = console.ask_int(
                "Maximum replies/day", replies.max_replies_per_day, minimum=0)
            replies.probability = console.ask_float(
                "Reply probability (0-1)", replies.probability, minimum=0.0, maximum=1.0)
            modes = [item.value for item in ReplyTargetMode]
            replies.target_selection_mode = ReplyTargetMode(console.choose(
                "Reply target selection:", modes,
                default=modes.index(replies.target_selection_mode.value) + 1))
            if replies.target_selection_mode is ReplyTargetMode.FILTERED:
                raw = console.ask("Filter keywords (comma separated)",
                                  ",".join(replies.target_filter_keywords))
                replies.target_filter_keywords = [item.strip()
                                                  for item in raw.split(",") if item.strip()]
            if replies.target_selection_mode is ReplyTargetMode.SPECIFIC:
                replies.specific_post_id = console.ask("Post id", replies.specific_post_id,
                                                       required=True)
        self.content.replies_config = replies
        self.content.build_rotators()
        self._save_settings()

    # -------------------------------------------------------------- 9 session
    def _configure_session(self) -> None:
        console.header("CONFIGURE SESSION BEHAVIOR")
        session = self.settings.session
        print(console.keyvalues({
            "Default scenario": session.default_scenario,
            "Session duration": f"{session.min_duration_seconds:g}-"
                                f"{session.max_duration_seconds:g} s",
            "Think time": f"{session.min_think_time_seconds:g}-"
                          f"{session.max_think_time_seconds:g} s",
            "Sessions per worker": session.sessions_per_worker,
        }))
        console.section("Available scenarios")
        print(console.table([{"name": item.name, "description": item.description,
                              "steps": len(item.steps)}
                             for item in self.scenarios.all()]))
        session.default_scenario = console.choose("Default scenario:",
                                                  self.scenarios.names(),
                                                  default=6)
        minimum = console.ask_float("Minimum session duration (seconds)",
                                    session.min_duration_seconds, minimum=1)
        maximum = console.ask_float("Maximum session duration (seconds)",
                                    session.max_duration_seconds, minimum=minimum)
        session.apply(min_duration_seconds=minimum, max_duration_seconds=maximum)
        think_min = console.ask_float("Minimum think time (seconds)",
                                      session.min_think_time_seconds, minimum=0)
        think_max = console.ask_float("Maximum think time (seconds)",
                                      session.max_think_time_seconds, minimum=think_min)
        session.apply(min_think_time_seconds=think_min,
                      max_think_time_seconds=think_max)
        session.sessions_per_worker = console.ask_int("Sessions per worker",
                                                      session.sessions_per_worker,
                                                      minimum=1)
        self._save_settings()

    # ---------------------------------------------------------- 10 concurrency
    def _configure_concurrency(self) -> None:
        console.header("CONFIGURE CONCURRENCY AND RATE LIMITS")
        concurrency = self.settings.concurrency
        safety = self.settings.safety
        print(console.keyvalues({
            "Global workers": concurrency.max_global_workers,
            "Default per-location ceiling": concurrency.default_max_workers_per_location,
            "Sessions per worker at once": concurrency.max_concurrent_sessions_per_worker,
            "Ramp-up": f"{concurrency.ramp_up_seconds:g} s",
            "Requests/minute": concurrency.max_requests_per_minute,
            "Sessions/hour": concurrency.max_sessions_per_hour,
            "Sessions/day": concurrency.max_sessions_per_day,
            "Abort at error rate": f"{safety.abort_on_error_rate:.0%}",
        }))
        concurrency.max_global_workers = console.ask_int(
            "Maximum global workers", concurrency.max_global_workers, minimum=1)
        concurrency.default_max_workers_per_location = console.ask_int(
            "Default maximum workers per location",
            concurrency.default_max_workers_per_location, minimum=1)
        concurrency.max_concurrent_sessions_per_worker = console.ask_int(
            "Maximum concurrent sessions per worker",
            concurrency.max_concurrent_sessions_per_worker, minimum=1, maximum=8)
        concurrency.ramp_up_seconds = console.ask_float(
            "Ramp-up window (seconds)", concurrency.ramp_up_seconds, minimum=0)
        concurrency.max_requests_per_minute = console.ask_int(
            "Maximum sessions dispatched per minute",
            concurrency.max_requests_per_minute, minimum=1)
        concurrency.max_sessions_per_hour = console.ask_int(
            "Maximum sessions per hour", concurrency.max_sessions_per_hour, minimum=1)
        concurrency.max_sessions_per_day = console.ask_int(
            "Maximum sessions per day", concurrency.max_sessions_per_day, minimum=1)
        safety.abort_on_error_rate = console.ask_float(
            "Abort the run at this error rate (0-1)", safety.abort_on_error_rate,
            minimum=0.0, maximum=1.0)
        safety.max_consecutive_worker_errors = console.ask_int(
            "Maximum consecutive errors per worker",
            safety.max_consecutive_worker_errors, minimum=1)
        console.info("Rate-limit responses (HTTP 429) are always respected: workers "
                     "back off and the run stops if throttling persists.")

        if console.ask_bool("Configure acceptance thresholds (pass/fail criteria)?",
                            True):
            self._configure_thresholds()
        self._save_settings()

    def _configure_thresholds(self) -> None:
        """Acceptance criteria that turn a run into a PASS/FAIL verdict."""
        console.section("ACCEPTANCE THRESHOLDS")
        thresholds = self.settings.thresholds
        print(console.keyvalues({
            "Enabled": thresholds.enabled,
            "Max error rate": f"{thresholds.max_error_rate:.1%}",
            "Max p95 response": f"{thresholds.max_p95_response_ms:g} ms",
            "Max p99 response": f"{thresholds.max_p99_response_ms:g} ms",
            "Max rate-limit events": thresholds.max_rate_limit_events,
            "Max failed sessions": thresholds.max_failed_sessions,
        }))
        console.info("A limit of 0 disables that check.")
        thresholds.enabled = console.ask_bool("Evaluate acceptance thresholds",
                                              thresholds.enabled)
        if not thresholds.enabled:
            return
        thresholds.max_error_rate = console.ask_float(
            "Maximum error rate (0-1)", thresholds.max_error_rate,
            minimum=0.0, maximum=1.0)
        thresholds.max_p95_response_ms = console.ask_float(
            "Maximum p95 response time (ms)", thresholds.max_p95_response_ms, minimum=0)
        thresholds.max_p99_response_ms = console.ask_float(
            "Maximum p99 response time (ms)", thresholds.max_p99_response_ms, minimum=0)
        thresholds.max_avg_response_ms = console.ask_float(
            "Maximum average response time (ms, 0 = off)",
            thresholds.max_avg_response_ms, minimum=0)
        thresholds.max_rate_limit_events = console.ask_int(
            "Maximum rate-limit events", thresholds.max_rate_limit_events, minimum=0)
        thresholds.max_failed_sessions = console.ask_int(
            "Maximum failed sessions", thresholds.max_failed_sessions, minimum=0)
        thresholds.min_completed_sessions = console.ask_int(
            "Minimum completed sessions (0 = off)",
            thresholds.min_completed_sessions, minimum=0)

    # ------------------------------------------------------- 11/12 test modes
    async def _test_single_worker(self) -> None:
        console.header("TEST SINGLE WORKER")
        location = console.choose("Location:", self.locations.keys(), default=1)
        scenario = console.choose("Scenario:", self.scenarios.names(), default=6)
        dry_run = console.ask_bool("Dry run (do not submit posts or replies)?", True)
        controller = self._new_controller()
        console.info("Running one worker for one session...")
        result = await controller.run_single_worker(location_key=location,
                                                    scenario=scenario, dry_run=dry_run)
        self._print_run_summary(result)
        if result.get("findings"):
            self._print_dry_run(result["findings"])
        await controller.shutdown()
        await self._to_thread(console.pause)

    async def _run_dry_test(self) -> None:
        console.header("RUN DRY TEST")
        console.info("A dry run walks the site and reports what it finds. "
                     "No posts or replies are submitted.")
        workers = console.ask_int("How many workers?", 1, minimum=1,
                                  maximum=self.settings.concurrency.max_global_workers)
        controller = self._new_controller()
        result = await controller.run_dry(workers=workers)
        self._print_dry_run(result["findings"])
        self._print_run_summary(result["status"])
        await controller.shutdown()
        await self._to_thread(console.pause)

    def _print_dry_run(self, findings: list[dict[str, Any]]) -> None:
        console.section("DRY RUN")
        if not findings:
            console.warn("No findings were recorded.")
            return
        for finding in findings:
            print(console.keyvalues({
                "Worker": finding.get("worker_id", "-"),
                "Location": finding.get("location", "-"),
                "Avatars detected": len(finding.get("avatars_detected", []) or []),
                "Avatar selected": finding.get("avatar_selected", "-"),
                "Usernames detected": len(finding.get("usernames_detected", []) or []),
                "Username selected": finding.get("username_selected", "-"),
                "Forums detected": ", ".join(finding.get("forums_detected", []) or []) or "-",
                "Forum entered": finding.get("forum_entered", "-"),
                "Initial posts detected": finding.get("initial_posts_detected", 0),
                "Reply target": finding.get("reply_target", "-"),
                "Would create post": finding.get("would_create_post", False),
                "Post probability": f"{finding.get('post_probability', 0):.0%}",
                "Would reply": finding.get("would_reply", False),
                "Reply probability": f"{finding.get('reply_probability', 0):.0%}",
                "Actual submission": "DISABLED",
            }))
            print()

    # ------------------------------------------------------------ 13-16 control
    async def _start_test(self) -> None:
        if self.controller is not None and self.controller.is_running:
            console.warn("A test run is already in progress.")
            return
        console.header("START LOAD TEST")
        problems = self._preflight_report()
        if problems:
            console.error("The run cannot start until these are resolved:")
            print(console.bullet_list(problems))
            if not console.ask_bool("Override and start anyway?", False):
                return
            force = True
        else:
            force = False

        label = console.ask("Label for this test run", "load-test")
        if not console.ask_bool(
                f"Start the load test against {self.settings.website.url}?", True):
            return

        self.controller = self._new_controller()
        test_run_id = await self.controller.start(label=label, force=force)
        console.success(f"Test run {test_run_id} started")
        if console.ask_bool("Open the live monitor now?", True):
            await self._live_monitor()

    async def _pause_test(self) -> None:
        if self.controller is None or not self.controller.is_running:
            console.warn("No test run is in progress.")
            return
        await self.controller.pause()
        console.success("Test paused - running sessions will finish their current step.")

    async def _resume_test(self) -> None:
        if self.controller is None:
            console.warn("No test run is in progress.")
            return
        await self.controller.resume()
        console.success("Test resumed.")

    async def _stop_test(self) -> None:
        if self.controller is None:
            console.warn("No test run is in progress.")
            return
        graceful = console.choose(
            "How should the test stop?",
            ["Graceful (let running sessions finish)", "STOP ALL (cancel immediately)",
             "Stop one location", "Stop one worker", "Cancel"],
            default=1)
        if graceful.startswith("Graceful"):
            console.info("Stopping...")
            await self.controller.stop()
            console.success("Test stopped.")
        elif graceful.startswith("STOP ALL"):
            await self._emergency_stop()
        elif graceful.startswith("Stop one location"):
            key = console.choose("Location:", self.locations.keys(), default=1)
            result = await self.controller.stop_location(key)
            console.success(f"Stopped {key}: {result}")
        elif graceful.startswith("Stop one worker"):
            worker_id = console.ask("Worker ID", required=True).upper()
            result = await self.controller.stop_worker(worker_id)
            console.success(f"Stopped {worker_id}: {result}")

    async def _emergency_stop(self) -> None:
        if self.controller is None or not self.controller.is_running:
            console.warn("Nothing is running.")
            return
        console.warn("STOP ALL - cancelling every queued and running session...")
        result = await self.controller.stop_all(reason="operator STOP ALL")
        console.success(f"All workers stopped ({result}).")

    # ----------------------------------------------------------- 17 monitoring
    async def _live_monitor(self) -> None:
        if self.controller is None:
            console.warn("No test run to monitor. Start a load test first.")
            return
        stop = asyncio.Event()
        refresher = asyncio.create_task(
            live_monitor(self.controller.status, interval=1.0, stop_event=stop),
            name="live-monitor")
        # Leaving on Enter rather than Ctrl+C: Ctrl+C would tear down the whole
        # console (and the running test) instead of returning to the menu.
        await self._to_thread(input, "")
        stop.set()
        await refresher
        console.info("Left the live monitor. The test run continues.")

    # ------------------------------------------------------------- 18 reports
    def _view_reports(self) -> None:
        console.header("VIEW REPORTS")
        database = get_database(self.settings.database)
        upgrade(database)
        repository = Repository(database)
        runs = repository.list_test_runs(limit=20)
        if not runs:
            console.warn("No test runs recorded yet.")
            return
        print(console.table(runs, columns=["test_run_id", "label", "status",
                                           "environment", "dry_run", "started_at",
                                           "duration_seconds"]))
        run_id = console.ask("Test run id (blank = latest)", runs[0]["test_run_id"])
        manager = ReportManager(database,
                                output_directory=self.settings.reporting.output_directory)
        category = console.choose(
            "Report:", list(REPORT_CATEGORIES) + ["acceptance verdict",
                                                  "compare with another run",
                                                  "all files"], default=1)
        if category == "acceptance verdict":
            from app.core.thresholds import evaluate

            verdict = evaluate(repository, run_id, self.settings.thresholds)
            console.section(f"ACCEPTANCE VERDICT - {run_id}")
            print(console.table([check.to_row() for check in verdict.checks],
                                columns=["check", "result", "observed", "limit",
                                         "unit", "detail"]))
            print()
            (console.success if verdict.passed else console.error)(
                verdict.summary_line())
            console.pause()
            return
        if category == "compare with another run":
            baseline = console.ask("Baseline test run id",
                                   runs[1]["test_run_id"] if len(runs) > 1 else "",
                                   required=True)
            tolerance = console.ask_float("Regression tolerance (0-1)", 0.10,
                                          minimum=0.0, maximum=1.0)
            result = compare(repository, baseline, run_id, tolerance=tolerance)
            console.section(f"{baseline} -> {run_id}")
            print(console.table([metric.to_row() for metric in result.metrics]))
            print()
            (console.success if result.passed else console.error)(result.summary_line())
            console.pause()
            return
        if category == "all files":
            files = manager.list_reports(run_id)
            print(console.table([{"file": path.name,
                                  "size_bytes": path.stat().st_size}
                                 for path in files]))
            console.pause()
            return
        payload = manager.build(category, run_id)
        console.section(f"{category.upper()} REPORT - {run_id}")
        self._print_payload(payload)
        console.pause()

    def _print_payload(self, payload: Any) -> None:
        if isinstance(payload, list):
            print(console.table(payload, max_rows=40))
        elif isinstance(payload, dict):
            for key, value in payload.items():
                console.section(key.replace("_", " ").title())
                if isinstance(value, list):
                    print(console.table(value, max_rows=25))
                elif isinstance(value, dict):
                    if value and all(isinstance(item, dict) for item in value.values()):
                        rows = [{"metric": name, **item} for name, item in value.items()]
                        print(console.table(rows, max_rows=25))
                    else:
                        print(console.keyvalues(value))
                else:
                    print(f"  {value}")
        else:
            print(json.dumps(payload, indent=2, default=str))

    def _export_results(self) -> None:
        console.header("EXPORT RESULTS")
        database = get_database(self.settings.database)
        upgrade(database)
        repository = Repository(database)
        runs = repository.list_test_runs(limit=20)
        if not runs:
            console.warn("No test runs recorded yet.")
            return
        print(console.table(runs, columns=["test_run_id", "label", "status",
                                           "started_at"]))
        run_id = console.ask("Test run id", runs[0]["test_run_id"], required=True)
        formats = []
        for fmt in ("json", "csv", "html"):
            if console.ask_bool(f"Export {fmt.upper()}?", True):
                formats.append(fmt)
        if not formats:
            console.warn("No formats selected.")
            return
        manager = ReportManager(database,
                                output_directory=self.settings.reporting.output_directory)
        written = manager.export_all(run_id, formats=formats)
        console.success(f"Wrote {len(written)} file(s)")
        print(console.bullet_list(str(path) for path in written[:20]))
        console.pause()

    # -------------------------------------------------------------- 20 backup
    def _backup(self) -> None:
        console.header("BACKUP")
        destination = Path(console.ask("Backup directory",
                                       f"data/backups/{timestamp_slug()}"))
        destination.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        config_target = destination / "config"
        config_target.mkdir(exist_ok=True)
        for path in Path(self.paths.root).glob("*.yaml"):
            shutil.copy2(path, config_target / path.name)
            copied.append(str(config_target / path.name))

        content_target = destination / "content"
        content_target.mkdir(exist_ok=True)
        for path in (Path(self.settings.content.posts_path),
                     Path(self.settings.content.replies_path)):
            if path.exists():
                shutil.copy2(path, content_target / path.name)
                copied.append(str(content_target / path.name))

        database_url = self.settings.database.url
        if database_url.startswith("sqlite:///"):
            source = Path(database_url.split("///", 1)[1])
            if source.exists():
                shutil.copy2(source, destination / source.name)
                copied.append(str(destination / source.name))
        else:
            console.warn("Non-SQLite database: back it up with your database tooling "
                         "(pg_dump for PostgreSQL).")

        console.success(f"Backed up {len(copied)} file(s) to {destination}")
        print(console.bullet_list(copied[:20]))
        console.pause()

    # ----------------------------------------------------------------- 21 exit
    async def _exit(self) -> bool:
        if self.controller is not None and self.controller.is_running:
            if not console.ask_bool("A test run is in progress. Stop it and exit?",
                                    False):
                return False
        await self._shutdown()
        console.success("Goodbye.")
        return True

    async def _shutdown(self) -> None:
        if self.controller is not None:
            await self.controller.shutdown()
            self.controller = None

    # ---------------------------------------------------------------- helpers
    def _new_controller(self) -> Controller:
        """Persist the current configuration and build a fresh controller.

        The controller reloads everything from disk, so unsaved edits made in
        the wizards (content items in particular) are written out first -
        otherwise they would silently not take part in the run.
        """
        self._save_settings(quiet=True)
        self.locations.save(self.paths.locations)
        self.locations.save_schedules(self.paths.schedules)
        self.workers.save(self.paths.workers)
        if len(self.content.post_library) or len(self.content.reply_library):
            self.content.save()
        return Controller(load_settings(self.paths.settings), config_paths=self.paths)

    def _save_settings(self, *, quiet: bool = False) -> None:
        save_settings(self.settings, self.paths.settings)
        if not quiet:
            console.success(f"Saved {self.paths.settings}")

    def _reload_managers(self) -> None:
        self.locations = LocationManager.load(self.paths.locations, self.paths.schedules)
        self.workers = WorkerManager(self.settings, self.locations)
        self.workers.load(self.paths.workers)
        self.content = ContentManager(self.settings.content, self.settings.posting,
                                      self.settings.replies)

    def _preflight_report(self) -> list[str]:
        controller = Controller(self.settings, config_paths=self.paths)
        controller.content.load()
        problems = controller.preflight()
        controller.database.dispose()
        return problems

    def _print_run_summary(self, status: dict[str, Any]) -> None:
        stats = status.get("global", {})
        verdict = status.get("verdict") or {}
        console.section("RUN SUMMARY")
        print(console.keyvalues({
            "Test run": status.get("test_run_id", "-"),
            "State": status.get("state", "-"),
            "Verdict": verdict.get("verdict", "-"),
            "Sessions completed": stats.get("completed", 0),
            "Sessions failed": stats.get("failed", 0),
            "Posts": stats.get("posts", 0),
            "Replies": stats.get("replies", 0),
            "Errors": stats.get("errors", 0),
            "Average response": f"{stats.get('avg_response_ms', 0)} ms",
        }))
        if verdict.get("checks"):
            console.section("ACCEPTANCE CHECKS")
            print(console.table(verdict["checks"],
                                columns=["check", "result", "observed", "limit",
                                         "unit", "detail"]))
            if verdict.get("failed_checks"):
                console.error("Breached: " + ", ".join(verdict["failed_checks"]))
            else:
                console.success("Every acceptance check was met.")
        locations = status.get("locations", [])
        if locations:
            print()
            print(console.table(locations))


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``main.py``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = ConfigPaths()
    if "--config-dir" in argv:
        index = argv.index("--config-dir")
        paths = ConfigPaths.under(argv[index + 1])
    app = ConsoleApp(paths)
    try:
        return asyncio.run(app.run())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
