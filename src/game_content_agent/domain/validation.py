"""Deterministic validation rules for domain objects."""

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict, Field

from game_content_agent.domain.agent import AgentRun, AgentSession, SessionStatus
from game_content_agent.domain.content import CandidateContent
from game_content_agent.domain.state import GameState, QuestStatus

DEFAULT_MAX_AGENT_STEPS = 6


class ValidationFinding(BaseModel):
    """A precise, reproducible result from a deterministic validator."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: str
    location: str
    message: str
    evidence: list[str] = Field(default_factory=list)


def validate_completed_quest_prerequisites(
    state: GameState,
) -> list[ValidationFinding]:
    """Report completed quests whose required items are missing."""

    owned_item_ids = {item.item_id for item in state.inventory if item.quantity > 0}
    findings: list[ValidationFinding] = []

    for quest in state.quests:
        if quest.status is not QuestStatus.COMPLETED:
            continue

        missing_item_ids = [
            item_id for item_id in quest.prerequisites if item_id not in owned_item_ids
        ]
        if missing_item_ids:
            findings.append(
                ValidationFinding(
                    code="missing_prerequisite",
                    severity="error",
                    location=f"quests.{quest.quest_id}",
                    message="completed quest has missing item prerequisites",
                    evidence=missing_item_ids,
                )
            )

    return findings


def validate_state_version_transition(
    previous: GameState,
    current: GameState,
) -> list[ValidationFinding]:
    """Report a state transition that does not advance the version."""

    if current.version > previous.version:
        return []

    return [
        ValidationFinding(
            code="invalid_state_version",
            severity="error",
            location="version",
            message="current state version must be greater than previous version",
            evidence=[
                f"previous={previous.version}",
                f"current={current.version}",
            ],
        )
    ]


def validate_agent_run_step_limit(
    run: AgentRun,
    max_steps: int = DEFAULT_MAX_AGENT_STEPS,
) -> list[ValidationFinding]:
    """Report an AgentRun that contains more steps than allowed."""

    if len(run.steps) <= max_steps:
        return []

    return [
        ValidationFinding(
            code="step_limit_exceeded",
            severity="error",
            location="steps",
            message="AgentRun contains more steps than the configured limit",
            evidence=[
                f"step_count={len(run.steps)}",
                f"max_steps={max_steps}",
            ],
        )
    ]


def validate_agent_run_step_order(
    run: AgentRun,
) -> list[ValidationFinding]:
    """Report steps that are not numbered consecutively from one."""

    actual_numbers = [step.step_number for step in run.steps]
    expected_numbers = list(range(1, len(run.steps) + 1))

    if actual_numbers == expected_numbers:
        return []

    return [
        ValidationFinding(
            code="invalid_step_order",
            severity="error",
            location="steps",
            message="AgentRun steps must be numbered consecutively from 1",
            evidence=[
                f"actual={actual_numbers}",
                f"expected={expected_numbers}",
            ],
        )
    ]


def validate_session_can_start_run(
    session: AgentSession,
) -> list[ValidationFinding]:
    """Report an attempt to start a run in a closed session."""

    if session.status is SessionStatus.ACTIVE:
        return []

    return [
        ValidationFinding(
            code="session_closed",
            severity="error",
            location="status",
            message="closed AgentSession cannot create a new AgentRun",
            evidence=[
                f"session_id={session.session_id}",
                f"status={session.status.value}",
            ],
        )
    ]


def validate_session_state_reference(
    session: AgentSession,
    available_state_ids: Collection[str],
) -> list[ValidationFinding]:
    """Report a session that points to a non-existent GameState."""

    if session.current_state_id in available_state_ids:
        return []

    return [
        ValidationFinding(
            code="invalid_state_reference",
            severity="error",
            location="current_state_id",
            message="AgentSession must reference an existing GameState",
            evidence=[f"current_state_id={session.current_state_id}"],
        )
    ]


def validate_session_world_version(
    session: AgentSession,
    state: GameState,
) -> list[ValidationFinding]:
    """Report a session and state that use different world versions."""

    if session.world_version == state.world_version:
        return []

    return [
        ValidationFinding(
            code="world_version_mismatch",
            severity="error",
            location="world_version",
            message="AgentSession and GameState must use the same world version",
            evidence=[
                f"session={session.world_version}",
                f"state={state.world_version}",
            ],
        )
    ]


def validate_candidate_citations(
    candidate: CandidateContent,
    available_lore_ids: Collection[str],
) -> list[ValidationFinding]:
    """Report citations that do not point to known Lore Entries."""

    missing_lore_ids = [
        lore_id for lore_id in candidate.citations if lore_id not in available_lore_ids
    ]
    if not missing_lore_ids:
        return []

    return [
        ValidationFinding(
            code="invalid_citation",
            severity="error",
            location="citations",
            message="Candidate cites Lore Entries that do not exist",
            evidence=missing_lore_ids,
        )
    ]


def validate_candidate_base_state_version(
    candidate: CandidateContent,
    current_state: GameState,
) -> list[ValidationFinding]:
    """Report a Candidate that was built from a stale GameState."""

    if candidate.base_state_version == current_state.version:
        return []

    return [
        ValidationFinding(
            code="state_version_conflict",
            severity="error",
            location="base_state_version",
            message="Candidate base state version must match current GameState version",
            evidence=[
                f"candidate_base={candidate.base_state_version}",
                f"current_state={current_state.version}",
            ],
        )
    ]
