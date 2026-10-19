"""Agent execution boundary models."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    """Allowed lifecycle states for an AgentSession."""

    ACTIVE = "active"
    CLOSED = "closed"


class AgentSession(BaseModel):
    """A multi-turn interaction boundary for one player and world version."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    world_version: str
    current_state_id: str
    status: SessionStatus


class ToolCall(BaseModel):
    """A recorded request to one controlled AgentTool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    call_id: str
    arguments: dict[str, Any]


class ObservationStatus(str, Enum):
    """Allowed outcomes for an AgentTool call."""

    SUCCESS = "success"
    ERROR = "error"


class Observation(BaseModel):
    """Structured output returned by an AgentTool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    call_id: str
    status: ObservationStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AgentStep(BaseModel):
    """One publicly recorded decision inside an AgentRun."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    step_number: int
    goal_summary: str
    tool_call: ToolCall | None = None
    observation: Observation | None = None


class TerminationReason(str, Enum):
    """Allowed reasons for an AgentRun to finish."""

    SUCCESS = "success"
    UNRECOVERABLE_FAILURE = "unrecoverable_failure"
    TOOL_FAILURE = "tool_failure"
    VALIDATION_FAILURE = "validation_failure"
    LOOP_LIMIT = "loop_limit"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"


class AgentRun(BaseModel):
    """One execution triggered by one user input."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    goal: str
    steps: list[AgentStep] = Field(default_factory=list)
    termination_reason: TerminationReason | None = None
