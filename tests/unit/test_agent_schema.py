from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from game_content_agent.domain.agent import AgentRun, AgentSession, AgentStep


def validate_model[ModelT: BaseModel](model_type: type[ModelT], **data: Any) -> ModelT:
    return model_type.model_validate(data)


def test_agent_session_accepts_valid_data() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="active",
    )

    assert session.session_id == "session-001"
    assert session.world_version == "v1"
    assert session.current_state_id == "state-001"
    assert session.status.value == "active"


def test_agent_session_accepts_closed_status() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="closed",
    )

    assert session.status.value == "closed"


def test_agent_session_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            AgentSession,
            session_id="session-001",
            world_version="v1",
            current_state_id="state-001",
            status="paused",
        )


def test_agent_session_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            AgentSession,
            session_id="session-001",
            world_version="v1",
            current_state_id="state-001",
            status="active",
            player_name="Lin",
        )


def test_agent_run_accepts_identifiers() -> None:
    run = validate_model(
        AgentRun,
        run_id="run-001",
        session_id="session-001",
        goal="design a side quest",
        steps=[
            {
                "step_id": "step-001",
                "step_number": 1,
                "goal_summary": "read the current game state",
            }
        ],
        termination_reason="success",
    )

    assert run.run_id == "run-001"
    assert run.session_id == "session-001"
    assert run.goal == "design a side quest"
    assert len(run.steps) == 1
    assert run.steps[0].step_number == 1
    assert run.termination_reason is not None
    assert run.termination_reason.value == "success"


def test_agent_run_without_termination_reason_is_in_progress() -> None:
    run = validate_model(
        AgentRun,
        run_id="run-001",
        session_id="session-001",
        goal="design a side quest",
    )

    assert run.steps == []
    assert run.termination_reason is None


def test_agent_step_accepts_a_public_goal_summary() -> None:
    step = validate_model(
        AgentStep,
        step_id="step-001",
        step_number=1,
        goal_summary="read the current game state",
        tool_call={
            "tool_name": "read_state",
            "call_id": "call-001",
            "arguments": {"session_id": "session-001"},
        },
        observation={
            "tool_name": "read_state",
            "call_id": "call-001",
            "status": "success",
            "data": {"state_version": 1},
        },
    )

    assert step.step_id == "step-001"
    assert step.step_number == 1
    assert step.goal_summary == "read the current game state"
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_state"
    assert step.tool_call.arguments == {"session_id": "session-001"}
    assert step.observation is not None
    assert step.observation.status.value == "success"
    assert step.observation.data == {"state_version": 1}
