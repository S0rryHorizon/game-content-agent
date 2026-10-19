from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from game_content_agent.domain.content import CandidateContent, ReviewDecision


def validate_model[ModelT: BaseModel](model_type: type[ModelT], **data: Any) -> ModelT:
    return model_type.model_validate(data)


def test_candidate_content_accepts_a_quest_type() -> None:
    candidate = validate_model(
        CandidateContent,
        candidate_id="candidate-001",
        content_type="quest",
        schema_version="v1",
        base_state_version=1,
        payload={
            "quest_id": "secret-room",
            "status": "not_started",
        },
        citations=["lore-key-001"],
    )

    assert candidate.candidate_id == "candidate-001"
    assert candidate.content_type.value == "quest"
    assert candidate.schema_version == "v1"
    assert candidate.base_state_version == 1
    assert candidate.payload["quest_id"] == "secret-room"
    assert candidate.citations == ["lore-key-001"]


def test_candidate_content_rejects_unknown_content_type() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            CandidateContent,
            candidate_id="candidate-001",
            content_type="cutscene",
            schema_version="v1",
            base_state_version=1,
            payload={},
            citations=[],
        )


def test_candidate_content_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            CandidateContent,
            candidate_id="candidate-001",
            content_type="quest",
            schema_version="v1",
            base_state_version=1,
            payload={},
            citations=[],
            approved=True,
        )


def test_review_decision_accepts_a_versioned_decision() -> None:
    review = validate_model(
        ReviewDecision,
        candidate_id="candidate-001",
        decision="accept",
        reason="validated and ready",
        reviewer="human-reviewer-001",
        candidate_version=1,
    )

    assert review.candidate_id == "candidate-001"
    assert review.decision.value == "accept"
    assert review.candidate_version == 1


def test_review_decision_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            ReviewDecision,
            candidate_id="candidate-001",
            decision="maybe",
            reason="uncertain",
            reviewer="human-reviewer-001",
            candidate_version=1,
        )
