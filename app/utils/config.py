"""Typed configuration model and YAML loader for Social Worker.

Configuration is split across a handful of YAML documents under ``config/``:

``settings.yaml``    website, browser, concurrency, behaviour and safety
``locations.yaml``   geographic test locations and their worker allocation
``workers.yaml``     explicit worker definitions created by the worker wizard
``scenarios.yaml``   reusable session scenarios
``schedules.yaml``   named time windows a location may be bound to
``selectors.yaml``   site-specific CSS/XPath selectors used by the adapter

Everything is validated with Pydantic so a malformed file fails fast with an
actionable message instead of surfacing as a mid-test crash.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_DIR = Path("config")


# --------------------------------------------------------------------------- enums
class Environment(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class ContentSelectionMode(str, Enum):
    RANDOM = "RANDOM"
    SEQUENTIAL = "SEQUENTIAL"
    RANDOM_WITHOUT_REPETITION = "RANDOM_WITHOUT_REPETITION"
    CATEGORY_BASED = "CATEGORY_BASED"


class ForumSelectionMode(str, Enum):
    RANDOM = "RANDOM_FORUM"
    SPECIFIC = "SPECIFIC_FORUM"
    ROTATE = "ROTATE_FORUMS"
    WEIGHTED = "WEIGHTED_FORUM_SELECTION"


class ReplyTargetMode(str, Enum):
    RANDOM = "RANDOM_INITIAL_POST"
    OLDEST = "OLDEST_INITIAL_POST"
    NEWEST = "NEWEST_INITIAL_POST"
    FILTERED = "RANDOM_WITH_FILTER"
    SPECIFIC = "SPECIFIC_POST"


class Base(BaseModel):
    """Common base: reject unknown keys so typos in YAML are caught early."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False,
                              validate_assignment=True)

    def apply(self, **changes: Any) -> "Base":
        """Apply several fields at once, validating them as a set.

        Assigning one field at a time trips cross-field rules (``min`` must not
        exceed ``max``) purely because of ordering.  ``apply`` validates the new
        values together and only then writes them back.
        """
        validated = type(self).model_validate({**self.model_dump(), **changes})
        self.__dict__.update(validated.__dict__)
        self.__pydantic_fields_set__.update(changes.keys())
        return self


# --------------------------------------------------------------------------- sections
class WebsiteConfig(Base):
    """The website under authorized test."""

    url: str = "http://127.0.0.1:8099"
    environment: Environment = Environment.LOCAL
    authorized_test_mode: bool = False
    authorization_reference: str = ""
    operator_contact: str = ""
    identify_as: str = "SocialWorker-LoadTest"
    verify_tls: bool = True
    adapter: str = "generic"

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        from app.utils.validation import validate_url

        return validate_url(value)

    @model_validator(mode="after")
    def _authorization_required_for_production(self) -> "WebsiteConfig":
        if self.environment == Environment.PRODUCTION and not self.authorized_test_mode:
            raise ValueError(
                "Production targets require authorized_test_mode: true and a written "
                "authorization reference before any load test may run."
            )
        return self


class BrowserConfig(Base):
    """Playwright browser settings."""

    engine: Literal["chromium"] = "chromium"
    headless: bool = True
    viewport_width: int = Field(1366, ge=320, le=3840)
    viewport_height: int = Field(768, ge=240, le=2160)
    default_timeout_ms: int = Field(30_000, ge=1_000, le=300_000)
    navigation_timeout_ms: int = Field(45_000, ge=1_000, le=300_000)
    slow_mo_ms: int = Field(0, ge=0, le=5_000)
    screenshot_on_failure: bool = True
    save_html_on_failure: bool = True
    capture_console: bool = True
    capture_network: bool = True
    ignore_https_errors: bool = False
    executable_path: str = ""
    launch_args: list[str] = Field(default_factory=list)
    human_typing: bool = True
    typing_delay_min_ms: int = Field(5, ge=0, le=500)
    typing_delay_max_ms: int = Field(25, ge=0, le=1_000)
    max_browsers: int = Field(8, ge=1, le=64)
    contexts_per_browser: int = Field(8, ge=1, le=64)


class ConcurrencyConfig(Base):
    """Global and per-location concurrency ceilings."""

    max_global_workers: int = Field(50, ge=1, le=10_000)
    default_max_workers_per_location: int = Field(20, ge=1, le=10_000)
    max_concurrent_sessions_per_worker: int = Field(1, ge=1, le=8)
    ramp_up_seconds: float = Field(30.0, ge=0.0, le=3_600.0)
    ramp_down_seconds: float = Field(5.0, ge=0.0, le=3_600.0)
    max_requests_per_minute: int = Field(600, ge=1, le=1_000_000)
    max_sessions_per_hour: int = Field(500, ge=1, le=1_000_000)
    max_sessions_per_day: int = Field(5_000, ge=1, le=10_000_000)


class PostingConfig(Base):
    """Post creation behaviour."""

    enabled: bool = True
    min_interval_seconds: float = Field(900.0, ge=0.0)
    max_interval_seconds: float = Field(2_700.0, ge=0.0)
    max_posts_per_session: int = Field(5, ge=0, le=1_000)
    max_posts_per_day: int = Field(50, ge=0, le=100_000)
    probability: float = Field(0.8, ge=0.0, le=1.0)
    randomized_delay: bool = True
    content_selection_mode: ContentSelectionMode = ContentSelectionMode.RANDOM_WITHOUT_REPETITION
    forum_selection_mode: ForumSelectionMode = ForumSelectionMode.WEIGHTED
    categories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_interval(self) -> "PostingConfig":
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError("posting.min_interval_seconds exceeds max_interval_seconds")
        return self


class ReplyConfig(Base):
    """Reply behaviour."""

    enabled: bool = True
    min_interval_seconds: float = Field(600.0, ge=0.0)
    max_interval_seconds: float = Field(1_800.0, ge=0.0)
    max_replies_per_session: int = Field(10, ge=0, le=1_000)
    max_replies_per_day: int = Field(100, ge=0, le=100_000)
    probability: float = Field(0.65, ge=0.0, le=1.0)
    randomized_delay: bool = True
    target_selection_mode: ReplyTargetMode = ReplyTargetMode.RANDOM
    target_filter_keywords: list[str] = Field(default_factory=list)
    specific_post_id: str = ""
    content_selection_mode: ContentSelectionMode = ContentSelectionMode.RANDOM_WITHOUT_REPETITION
    categories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_interval(self) -> "ReplyConfig":
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError("replies.min_interval_seconds exceeds max_interval_seconds")
        return self


class SessionConfig(Base):
    """Visitor session shape."""

    default_scenario: str = "scenario_06"
    min_duration_seconds: float = Field(60.0, ge=0.0)
    max_duration_seconds: float = Field(900.0, ge=0.0)
    min_think_time_seconds: float = Field(2.0, ge=0.0)
    max_think_time_seconds: float = Field(12.0, ge=0.0)
    browse_pages_min: int = Field(1, ge=0, le=100)
    browse_pages_max: int = Field(4, ge=0, le=100)
    sessions_per_worker: int = Field(1, ge=1, le=10_000)
    loop_forever: bool = False

    @model_validator(mode="after")
    def _check_bounds(self) -> "SessionConfig":
        if self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("session.min_duration_seconds exceeds max_duration_seconds")
        if self.min_think_time_seconds > self.max_think_time_seconds:
            raise ValueError("session.min_think_time_seconds exceeds max_think_time_seconds")
        if self.browse_pages_min > self.browse_pages_max:
            raise ValueError("session.browse_pages_min exceeds browse_pages_max")
        return self


class ForumConfig(Base):
    """Which forums a test targets."""

    mode: ForumSelectionMode = ForumSelectionMode.WEIGHTED
    specific_forum: str = ""
    weights: dict[str, float] = Field(default_factory=lambda: {
        "general": 40.0, "confessions": 20.0, "questions": 15.0, "random": 25.0,
    })
    allowed_forums: list[str] = Field(default_factory=list)
    rotation_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_mode(self) -> "ForumConfig":
        if self.mode == ForumSelectionMode.SPECIFIC and not self.specific_forum:
            raise ValueError("forums.specific_forum must be set for SPECIFIC_FORUM mode")
        if self.mode == ForumSelectionMode.WEIGHTED and not self.weights:
            raise ValueError("forums.weights must be set for WEIGHTED_FORUM_SELECTION mode")
        return self


class ContentConfig(Base):
    """Where post/reply libraries live."""

    posts_path: str = "data/content/posts.txt"
    replies_path: str = "data/content/replies.txt"
    prevent_duplicates_per_worker: bool = True
    prevent_global_duplicates: bool = False
    reload_on_start: bool = True
    max_library_size: int = Field(10_000, ge=1)


class SafetyConfig(Base):
    """Rate-limit handling and abort thresholds.

    Social Worker always *respects* a target's rate limiting: on HTTP 429 (or
    any configured rate-limit signal) it records the event and backs off.  It
    never attempts to circumvent the restriction.
    """

    respect_rate_limits: bool = True
    backoff_initial_seconds: float = Field(30.0, ge=1.0)
    backoff_max_seconds: float = Field(600.0, ge=1.0)
    backoff_multiplier: float = Field(2.0, ge=1.0, le=10.0)
    rate_limit_status_codes: list[int] = Field(default_factory=lambda: [429, 503])
    max_consecutive_worker_errors: int = Field(5, ge=1, le=1_000)
    abort_on_error_rate: float = Field(0.5, ge=0.0, le=1.0)
    abort_error_rate_min_samples: int = Field(20, ge=1)
    stop_on_rate_limit_streak: int = Field(10, ge=1)
    honour_robots_txt: bool = True


class ThresholdConfig(Base):
    """Pass/fail criteria evaluated against a finished run.

    A load test without acceptance criteria only produces numbers; these turn a
    run into a verdict the operator can gate a release on.  A threshold of 0
    disables that particular check.
    """

    enabled: bool = True
    max_error_rate: float = Field(0.02, ge=0.0, le=1.0)
    max_avg_response_ms: float = Field(0.0, ge=0.0)
    max_p95_response_ms: float = Field(2_000.0, ge=0.0)
    max_p99_response_ms: float = Field(5_000.0, ge=0.0)
    max_post_submit_p95_ms: float = Field(0.0, ge=0.0)
    max_reply_submit_p95_ms: float = Field(0.0, ge=0.0)
    min_completed_sessions: int = Field(0, ge=0)
    max_rate_limit_events: int = Field(0, ge=0)
    max_failed_sessions: int = Field(0, ge=0)
    fail_on_javascript_errors: bool = False


class DatabaseConfig(Base):
    """Persistence settings.  SQLite by default; PostgreSQL for production."""

    url: str = "sqlite:///data/social_worker.db"
    echo: bool = False
    pool_size: int = Field(10, ge=1, le=200)
    max_overflow: int = Field(20, ge=0, le=200)

    @field_validator("url")
    @classmethod
    def _expand_env(cls, value: str) -> str:
        return os.path.expandvars(value)


class LoggingConfig(Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    directory: str = "logs"
    console: bool = True
    console_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    max_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    backups: int = Field(5, ge=0, le=100)


class ReportingConfig(Base):
    output_directory: str = "data/reports"
    debug_directory: str = "data/debug"
    screenshot_directory: str = "data/screenshots"
    formats: list[Literal["json", "csv", "html"]] = Field(
        default_factory=lambda: ["json", "csv", "html"]
    )
    auto_export_on_stop: bool = True


class ControllerConfig(Base):
    """Distributed controller (FastAPI) options."""

    api_enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(8800, ge=1, le=65_535)
    heartbeat_interval_seconds: float = Field(10.0, ge=1.0, le=600.0)
    heartbeat_timeout_seconds: float = Field(45.0, ge=2.0, le=3_600.0)
    redis_url: str = ""
    api_token: str = ""


class Settings(Base):
    """Root configuration document (``config/settings.yaml``)."""

    website: WebsiteConfig = Field(default_factory=WebsiteConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    posting: PostingConfig = Field(default_factory=PostingConfig)
    replies: ReplyConfig = Field(default_factory=ReplyConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    forums: ForumConfig = Field(default_factory=ForumConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    dry_run: bool = False

    def summary(self) -> dict[str, Any]:
        """Compact human-readable snapshot used by the CLI."""
        return {
            "website": self.website.url,
            "environment": self.website.environment.value,
            "authorized": self.website.authorized_test_mode,
            "global_workers": self.concurrency.max_global_workers,
            "posting": self.posting.enabled,
            "replying": self.replies.enabled,
            "scenario": self.session.default_scenario,
            "database": self.database.url,
            "dry_run": self.dry_run,
        }


# --------------------------------------------------------------------------- IO
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True,
                       default_flow_style=False)


def _jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


#: Keys removed in later versions, dropped on load instead of failing the run.
DEPRECATED_KEYS: dict[str, tuple[str, ...]] = {
    "session": ("reuse_identity_across_sessions",),
}


def _drop_deprecated(document: dict[str, Any]) -> list[str]:
    """Remove retired keys from a loaded document; returns what was dropped."""
    dropped: list[str] = []
    for section, keys in DEPRECATED_KEYS.items():
        block = document.get(section)
        if not isinstance(block, dict):
            continue
        for key in keys:
            if key in block:
                block.pop(key)
                dropped.append(f"{section}.{key}")
    return dropped


def load_settings(path: str | Path = CONFIG_DIR / "settings.yaml") -> Settings:
    """Load and validate ``settings.yaml`` (returns defaults when absent).

    Keys retired in a newer version are dropped with a warning rather than
    failing the load, so an existing configuration keeps working after an
    upgrade.
    """
    document = _read_yaml(Path(path))
    dropped = _drop_deprecated(document)
    if dropped:
        import logging

        logging.getLogger("social_worker.config").warning(
            "Ignoring setting(s) removed in this version: %s. Re-save the "
            "configuration to tidy the file.", ", ".join(dropped))
    return Settings.model_validate(document)


def save_settings(settings: Settings,
                  path: str | Path = CONFIG_DIR / "settings.yaml") -> Path:
    """Persist *settings* to YAML and return the path written."""
    target = Path(path)
    _write_yaml(target, _jsonable(settings))
    return target


def load_yaml_document(path: str | Path) -> dict[str, Any]:
    """Load an arbitrary Social Worker YAML document."""
    return _read_yaml(Path(path))


def save_yaml_document(path: str | Path, data: dict[str, Any]) -> Path:
    """Write an arbitrary Social Worker YAML document."""
    target = Path(path)
    _write_yaml(target, data)
    return target


class ConfigPaths(Base):
    """Resolved locations of every configuration document."""

    root: str = str(CONFIG_DIR)
    settings: str = str(CONFIG_DIR / "settings.yaml")
    locations: str = str(CONFIG_DIR / "locations.yaml")
    workers: str = str(CONFIG_DIR / "workers.yaml")
    scenarios: str = str(CONFIG_DIR / "scenarios.yaml")
    schedules: str = str(CONFIG_DIR / "schedules.yaml")
    selectors: str = str(CONFIG_DIR / "selectors.yaml")

    @classmethod
    def under(cls, root: str | Path) -> "ConfigPaths":
        base = Path(root)
        return cls(
            root=str(base),
            settings=str(base / "settings.yaml"),
            locations=str(base / "locations.yaml"),
            workers=str(base / "workers.yaml"),
            scenarios=str(base / "scenarios.yaml"),
            schedules=str(base / "schedules.yaml"),
            selectors=str(base / "selectors.yaml"),
        )


def config_exists(paths: ConfigPaths | None = None) -> bool:
    """True when a settings document already exists (first-run detection)."""
    paths = paths or ConfigPaths()
    return Path(paths.settings).exists()
