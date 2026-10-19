"""Candidate content models."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class CandidateContentType(str, Enum):
    """Kinds of content the Agent may propose."""

    QUEST = "quest"
    DIALOGUE = "dialogue"
    CONFIG = "config"


class CandidateContent(BaseModel):
    """Structured content proposed by the Agent before review."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    content_type: CandidateContentType
    schema_version: str
    base_state_version: int
    payload: dict[str, Any]
    citations: list[str]


class ReviewDecisionType(str, Enum):
    """Allowed human review decisions for a CandidateContent."""

    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"


class ReviewDecision(BaseModel):
    """A versioned human decision about a candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    decision: ReviewDecisionType
    reason: str
    reviewer: str
    candidate_version: int
