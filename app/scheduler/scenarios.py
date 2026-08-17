"""Reusable session scenarios.

A scenario is an ordered list of steps a simulated visitor performs.  The seven
built-in scenarios cover the standard progression from "visit only" to "heavy
forum activity"; operators can define their own in ``config/scenarios.yaml``.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.config import ConfigPaths, load_yaml_document, save_yaml_document
from app.utils.logger import get_logger

LOGGER = get_logger("scheduler.scenarios")


class StepAction(str, Enum):
    """Every action a scenario step may request."""

    OPEN_SITE = "OPEN_SITE"
    WAIT_FOR_LOAD = "WAIT_FOR_LOAD"
    SELECT_AVATAR = "SELECT_AVATAR"
    SELECT_USERNAME = "SELECT_USERNAME"
    ENTER_FORUM = "ENTER_FORUM"
    BROWSE_FORUM = "BROWSE_FORUM"
    READ_POST = "READ_POST"
    CREATE_POST = "CREATE_POST"
    CREATE_REPLY = "CREATE_REPLY"
    SWITCH_FORUM = "SWITCH_FORUM"
    THINK = "THINK"
    END_SESSION = "END_SESSION"


class Step(BaseModel):
    """One step of a scenario."""

    model_config = ConfigDict(extra="forbid")

    action: StepAction
    repeat: int = Field(1, ge=1, le=100)
    probability: float = Field(1.0, ge=0.0, le=1.0)
    min_seconds: float = Field(0.0, ge=0.0)
    max_seconds: float = Field(0.0, ge=0.0)
    forum: str = ""
    category: str = ""
    note: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    def describe(self) -> str:
        """Short label used in dry-run output."""
        parts = [self.action.value]
        if self.repeat > 1:
            parts.append(f"x{self.repeat}")
        if self.probability < 1.0:
            parts.append(f"p={self.probability:.0%}")
        if self.forum:
            parts.append(f"forum={self.forum}")
        return " ".join(parts)


class Scenario(BaseModel):
    """A named, reusable visitor workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    steps: list[Step] = Field(default_factory=list)
    allow_posting: bool = True
    allow_replying: bool = True
    loop: bool = False
    max_duration_seconds: float = Field(0.0, ge=0.0)

    def actions(self) -> list[StepAction]:
        """Flat list of the actions this scenario may perform."""
        return [step.action for step in self.steps]

    def uses(self, action: StepAction) -> bool:
        """True when the scenario contains *action*."""
        return any(step.action == action for step in self.steps)

    def describe(self) -> str:
        """Human-readable one-line summary."""
        return " -> ".join(step.describe() for step in self.steps)


def _step(action: StepAction, **kwargs: Any) -> Step:
    return Step(action=action, **kwargs)


def builtin_scenarios() -> dict[str, Scenario]:
    """Return the seven built-in scenarios keyed by name."""
    think = lambda lo, hi: _step(StepAction.THINK, min_seconds=lo, max_seconds=hi)  # noqa: E731

    scenarios = [
        Scenario(
            name="scenario_01",
            description="Visit only - open the site and leave.",
            allow_posting=False, allow_replying=False,
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                think(2, 6),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_02",
            description="Visit and select the site-provided identity.",
            allow_posting=False, allow_replying=False,
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                think(2, 8),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_03",
            description="Visit, take an identity and browse a forum.",
            allow_posting=False, allow_replying=False,
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                _step(StepAction.ENTER_FORUM),
                _step(StepAction.BROWSE_FORUM, repeat=2),
                think(3, 10),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_04",
            description="Visit and create a post.",
            allow_replying=False,
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                _step(StepAction.ENTER_FORUM),
                _step(StepAction.BROWSE_FORUM),
                think(3, 9),
                _step(StepAction.CREATE_POST),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_05",
            description="Visit and reply to an existing initial post.",
            allow_posting=False,
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                _step(StepAction.ENTER_FORUM),
                _step(StepAction.BROWSE_FORUM),
                _step(StepAction.READ_POST),
                think(4, 12),
                _step(StepAction.CREATE_REPLY),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_06",
            description="Full session - browse, post, read and reply.",
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                _step(StepAction.ENTER_FORUM),
                _step(StepAction.BROWSE_FORUM),
                think(3, 10),
                _step(StepAction.CREATE_POST),
                think(3, 10),
                _step(StepAction.READ_POST),
                _step(StepAction.CREATE_REPLY),
                think(2, 8),
                _step(StepAction.SWITCH_FORUM),
                _step(StepAction.BROWSE_FORUM),
                _step(StepAction.END_SESSION),
            ],
        ),
        Scenario(
            name="scenario_07",
            description="Heavy forum activity - multiple posts and replies.",
            steps=[
                _step(StepAction.OPEN_SITE),
                _step(StepAction.WAIT_FOR_LOAD),
                _step(StepAction.SELECT_AVATAR),
                _step(StepAction.SELECT_USERNAME),
                _step(StepAction.ENTER_FORUM),
                _step(StepAction.BROWSE_FORUM, repeat=2),
                _step(StepAction.CREATE_POST, repeat=2),
                _step(StepAction.READ_POST),
                _step(StepAction.CREATE_REPLY, repeat=3),
                _step(StepAction.SWITCH_FORUM),
                _step(StepAction.BROWSE_FORUM),
                _step(StepAction.CREATE_POST),
                _step(StepAction.CREATE_REPLY, repeat=2),
                _step(StepAction.END_SESSION),
            ],
        ),
    ]
    return {scenario.name: scenario for scenario in scenarios}


class ScenarioLibrary:
    """Built-in plus operator-defined scenarios."""

    def __init__(self, scenarios: Iterable[Scenario] | None = None) -> None:
        self._scenarios: dict[str, Scenario] = builtin_scenarios()
        for scenario in scenarios or []:
            self._scenarios[scenario.name] = scenario

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ScenarioLibrary":
        """Load ``config/scenarios.yaml`` on top of the built-ins."""
        target = Path(path or ConfigPaths().scenarios)
        document = load_yaml_document(target)
        raw = document.get("scenarios", [])
        if isinstance(raw, dict):
            raw = [{"name": key, **(value or {})} for key, value in raw.items()]
        custom = []
        for item in raw:
            try:
                custom.append(Scenario.model_validate(item))
            except Exception as exc:
                LOGGER.error("Invalid scenario in %s: %s", target, exc)
                raise
        return cls(custom)

    def save(self, path: str | Path | None = None, *, include_builtin: bool = True) -> Path:
        """Write the scenario catalogue to YAML."""
        target = Path(path or ConfigPaths().scenarios)
        builtin = set(builtin_scenarios())
        scenarios = [
            scenario.model_dump(mode="json")
            for name, scenario in sorted(self._scenarios.items())
            if include_builtin or name not in builtin
        ]
        save_yaml_document(target, {"scenarios": scenarios})
        return target

    def get(self, name: str) -> Scenario:
        """Return a scenario by name."""
        try:
            return self._scenarios[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown scenario {name!r}; available: {', '.join(sorted(self._scenarios))}"
            ) from exc

    def add(self, scenario: Scenario) -> Scenario:
        """Register (or replace) a scenario."""
        self._scenarios[scenario.name] = scenario
        return scenario

    def remove(self, name: str) -> None:
        """Delete a custom scenario."""
        if name in builtin_scenarios():
            raise ValueError(f"Built-in scenario {name!r} cannot be removed")
        self._scenarios.pop(name, None)

    def names(self) -> list[str]:
        """Sorted scenario names."""
        return sorted(self._scenarios)

    def all(self) -> list[Scenario]:
        """Every scenario, sorted by name."""
        return [self._scenarios[name] for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return name in self._scenarios

    def __len__(self) -> int:
        return len(self._scenarios)
