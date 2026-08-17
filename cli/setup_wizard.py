"""First-run setup wizard (spec section 16)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli import console
from app.utils.config import (
    ConfigPaths, Environment, Settings, WebsiteConfig, save_settings,
)
from app.utils.validation import ValidationError, validate_url

AUTHORIZATION_NOTICE = """
Social Worker generates automated traffic. Use it only against a website you
own, or one where you hold explicit written permission to run load tests.
Confirm the target, the time window and the traffic volume with whoever
operates the system before you start.
""".strip()


def run_setup_wizard(settings: Settings | None = None,
                     paths: ConfigPaths | None = None) -> Settings:
    """Walk the operator through the initial configuration."""
    paths = paths or ConfigPaths()
    settings = settings or Settings()

    console.header("SOCIAL WORKER INITIAL SETUP")
    print(console.paint(AUTHORIZATION_NOTICE, "yellow"))
    print()

    url = _ask_url(settings.website.url)
    environment = console.choose(
        "Testing environment:", ["Local", "Staging", "Production"],
        default=1,
        descriptions={"Production": "requires an authorization reference"})
    env_value = Environment(environment.lower())

    authorized = console.ask_bool(
        "Authorized test mode - do you own this site or hold written permission "
        "to load test it?", default=False)
    reference = ""
    contact = settings.website.operator_contact
    if authorized:
        reference = console.ask(
            "Authorization reference (ticket, contract or approval note)",
            settings.website.authorization_reference,
            required=env_value is Environment.PRODUCTION)
        contact = console.ask("Operator contact e-mail (recorded in reports)", contact)
    else:
        console.warn("Authorized test mode is OFF. You can configure everything now, "
                     "but a load test will not start until it is enabled.")

    max_workers = console.ask_int("Maximum global workers", 
                                  settings.concurrency.max_global_workers,
                                  minimum=1, maximum=10_000)

    settings.website = WebsiteConfig(
        url=url,
        environment=env_value,
        authorized_test_mode=authorized,
        authorization_reference=reference,
        operator_contact=contact,
        identify_as=settings.website.identify_as,
    )
    settings.concurrency.max_global_workers = max_workers

    settings.browser.headless = console.ask_bool("Run the browser headless?", True)
    settings.database.url = console.ask("Database URL", settings.database.url)

    if console.ask_bool("Configure geographic locations now?", True):
        from cli.location_wizard import run_location_wizard

        run_location_wizard(settings, paths=paths)

    if console.ask_bool("Import content now?", True):
        from cli.content_wizard import run_content_wizard

        run_content_wizard(settings)

    if console.ask_bool("Configure schedules now?", False):
        from cli.schedule_wizard import run_schedule_wizard

        run_schedule_wizard(paths=paths)

    if console.ask_bool("Save configuration?", True):
        path = save_settings(settings, paths.settings)
        console.success(f"Configuration written to {path}")
        _ensure_data_directories(settings)
    else:
        console.warn("Configuration not saved.")

    return settings


def _ask_url(default: str) -> str:
    while True:
        raw = console.ask("Website URL", default, required=True)
        try:
            return validate_url(raw)
        except ValidationError as exc:
            console.error(str(exc))


def _ensure_data_directories(settings: Settings) -> dict[str, Any]:
    """Create the directories the run will write to."""
    created = {}
    for label, path in (
        ("reports", settings.reporting.output_directory),
        ("debug", settings.reporting.debug_directory),
        ("screenshots", settings.reporting.screenshot_directory),
        ("logs", settings.logging.directory),
        ("content", str(Path(settings.content.posts_path).parent)),
    ):
        Path(path).mkdir(parents=True, exist_ok=True)
        created[label] = path
    return created
