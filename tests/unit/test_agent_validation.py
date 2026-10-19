from typing import Any

from pydantic import BaseModel

from game_content_agent.domain.agent import AgentRun, AgentSession
from game_content_agent.domain.content import CandidateContent
from game_content_agent.domain.state import GameState
from game_content_agent.domain.validation import (
    validate_agent_run_step_limit,
    validate_agent_run_step_order,
    validate_candidate_base_state_version,
    validate_candidate_citations,
    validate_session_can_start_run,
    validate_session_state_reference,
)


def validate_model[ModelT: BaseModel](model_type: type[ModelT], **data: Any) -> ModelT:
    return model_type.model_validate(data)


def make_run_with_steps(step_count: int) -> AgentRun:
    return validate_model(
        AgentRun,
        run_id="run-001",
        session_id="session-001",
        goal="design a side quest",
        steps=[
            {
                "step_id": f"step-{number:03d}",
                "step_number": number,
                "goal_summary": "perform one step",
            }
            for number in range(1, step_count + 1)
        ],
    )


def make_state(version: int) -> GameState:
    return validate_model(
        GameState,
        state_id=f"state-{version:03d}",
        world_version="v1",
        version=version,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )


def test_agent_run_at_step_limit_has_no_finding() -> None:
    run = make_run_with_steps(6)

    assert validate_agent_run_step_limit(run) == []


def test_agent_run_over_step_limit_has_finding() -> None:
    run = make_run_with_steps(7)

    findings = validate_agent_run_step_limit(run)

    assert len(findings) == 1
    assert findings[0].code == "step_limit_exceeded"
    assert findings[0].location == "steps"


def test_agent_run_with_consecutive_steps_has_no_order_finding() -> None:
    run = make_run_with_steps(3)

    assert validate_agent_run_step_order(run) == []


def test_agent_run_with_missing_step_number_has_order_finding() -> None:
    run = validate_model(
        AgentRun,
        run_id="run-001",
        session_id="session-001",
        goal="design a side quest",
        steps=[
            {
                "step_id": "step-001",
                "step_number": 1,
                "goal_summary": "perform one step",
            },
            {
                "step_id": "step-003",
                "step_number": 3,
                "goal_summary": "perform one step",
            },
        ],
    )

    findings = validate_agent_run_step_order(run)

    assert len(findings) == 1
    assert findings[0].code == "invalid_step_order"
    assert findings[0].location == "steps"


def test_active_session_can_start_a_run() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="active",
    )

    assert validate_session_can_start_run(session) == []


def test_closed_session_cannot_start_a_run() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="closed",
    )

    findings = validate_session_can_start_run(session)

    assert len(findings) == 1
    assert findings[0].code == "session_closed"
    assert findings[0].location == "status"


def test_session_with_existing_state_reference_has_no_finding() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="active",
    )

    assert validate_session_state_reference(session, {"state-001"}) == []


def test_session_with_missing_state_reference_has_finding() -> None:
    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-999",
        status="active",
    )

    findings = validate_session_state_reference(session, {"state-001"})

    assert len(findings) == 1
    assert findings[0].code == "invalid_state_reference"
    assert findings[0].location == "current_state_id"


def test_candidate_with_existing_citation_has_no_finding() -> None:
    candidate = validate_model(
        CandidateContent,
        candidate_id="candidate-001",
        content_type="quest",
        schema_version="v1",
        base_state_version=1,
        payload={"quest_id": "secret-room"},
        citations=["lore-key-001"],
    )

    assert validate_candidate_citations(candidate, {"lore-key-001"}) == []


def test_candidate_with_missing_citation_has_finding() -> None:
    candidate = validate_model(
        CandidateContent,
        candidate_id="candidate-001",
        content_type="quest",
        schema_version="v1",
        base_state_version=1,
        payload={"quest_id": "secret-room"},
        citations=["lore-999"],
    )

    findings = validate_candidate_citations(candidate, {"lore-key-001"})

    assert len(findings) == 1
    assert findings[0].code == "invalid_citation"
    assert findings[0].location == "citations"
    assert findings[0].evidence == ["lore-999"]


def test_candidate_based_on_current_state_has_no_version_finding() -> None:
    candidate = validate_model(
        CandidateContent,
        candidate_id="candidate-001",
        content_type="quest",
        schema_version="v1",
        base_state_version=2,
        payload={"quest_id": "secret-room"},
        citations=[],
    )

    assert validate_candidate_base_state_version(candidate, make_state(2)) == []


def test_candidate_based_on_old_state_has_version_conflict_finding() -> None:
    candidate = validate_model(
        CandidateContent,
        candidate_id="candidate-001",
        content_type="quest",
        schema_version="v1",
        base_state_version=2,
        payload={"quest_id": "secret-room"},
        citations=[],
    )

    findings = validate_candidate_base_state_version(candidate, make_state(3))

    assert len(findings) == 1
    assert findings[0].code == "state_version_conflict"
    assert findings[0].location == "base_state_version"
    assert findings[0].evidence == ["candidate_base=2", "current_state=3"]
