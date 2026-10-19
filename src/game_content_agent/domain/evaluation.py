"""Fixed evaluation contracts for repeatable game-agent checks."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_content_agent.domain.state import GameState


class ScenarioType(str, Enum):
    """Whether a scenario describes an accepted path or a deliberate failure."""

    NORMAL = "normal"
    FAILURE = "failure"


class ExpectedOutcome(str, Enum):
    """The outcome a deterministic scenario contract expects."""

    SUCCESS = "success"
    REJECTED = "rejected"


class EvaluationScenario(BaseModel):
    """Fixed inputs, state, and expectations for one repeatable evaluation."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scenario_type: ScenarioType
    world_version: str = Field(min_length=1)
    inputs: list[str] = Field(min_length=1)
    initial_state: GameState
    expected_outcome: ExpectedOutcome
    expected_state_version: int = Field(ge=1)
    expected_failure_type: str | None = None
    expected_finding_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def matches_declared_scenario_type(self) -> "EvaluationScenario":
        if self.world_version != self.initial_state.world_version:
            raise ValueError(
                "scenario and initial state must use the same world version"
            )
        if self.expected_state_version != self.initial_state.version:
            raise ValueError(
                "expected state version must match the initial state version"
            )
        if any(not value.strip() for value in self.inputs):
            raise ValueError("inputs must not contain blank values")

        if self.scenario_type is ScenarioType.NORMAL:
            if self.expected_outcome is not ExpectedOutcome.SUCCESS:
                raise ValueError("normal scenarios must expect success")
            if self.expected_failure_type is not None:
                raise ValueError("normal scenarios cannot declare a failure type")
            if self.expected_finding_codes:
                raise ValueError("normal scenarios cannot declare findings")
        else:
            if self.expected_outcome is not ExpectedOutcome.REJECTED:
                raise ValueError("failure scenarios must expect rejection")
            if not self.expected_failure_type:
                raise ValueError("failure scenarios must declare a failure type")
            if not self.expected_finding_codes:
                raise ValueError("failure scenarios must declare a finding code")
        return self


__all__ = [
    "EvaluationScenario",
    "ExpectedOutcome",
    "ScenarioType",
]
