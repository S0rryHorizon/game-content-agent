"""Strongly typed, immutable-by-version quest design records."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from game_content_agent.domain.validation import ValidationFinding

NonBlank = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class StrictDesignModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(StrictDesignModel):
    kind: Literal["has_item", "trust_at_least", "at_location"]
    entity_id: NonBlank
    value: PositiveInt = 1

    @model_validator(mode="after")
    def location_value_is_one(self) -> "Condition":
        if self.kind == "at_location" and self.value != 1:
            raise ValueError("at_location value must be 1")
        return self


class QuestAction(StrictDesignModel):
    kind: Literal["talk", "open_gate", "reveal_route", "record_tide"]
    target_id: NonBlank
    conditions: list[Condition] = Field(default_factory=list)


class Citation(StrictDesignModel):
    lore_id: NonBlank
    world_version: NonBlank


class QuestNode(StrictDesignModel):
    node_id: NonBlank
    title: NonBlank
    text: NonBlank
    location_id: NonBlank
    character_ids: list[NonBlank] = Field(default_factory=list)
    item_ids: list[NonBlank] = Field(default_factory=list)
    actions: list[QuestAction] = Field(default_factory=list)
    effects: list[NonBlank] = Field(default_factory=list)
    ending: NonBlank | None = None


class QuestEdge(StrictDesignModel):
    source: NonBlank
    target: NonBlank
    label: NonBlank


class QuestDraft(StrictDesignModel):
    title: NonBlank
    summary: NonBlank
    entry_node: NonBlank
    nodes: list[QuestNode]
    edges: list[QuestEdge]
    citations: list[Citation] = Field(default_factory=list)


class BriefConstraints(StrictDesignModel):
    required_character_ids: list[NonBlank] = Field(default_factory=list)
    required_location_ids: list[NonBlank] = Field(default_factory=list)
    required_item_ids: list[NonBlank] = Field(default_factory=list)
    required_ending: NonBlank | None = None
    protected_conditions: list[Condition] = Field(default_factory=list)


class DesignBrief(StrictDesignModel):
    version: PositiveInt
    original_input: NonBlank
    goal: NonBlank
    must_keep: list[NonBlank] = Field(default_factory=list)
    constraints: BriefConstraints
    confirmed: bool = Field(default=False, strict=True)


class CandidateVersion(StrictDesignModel):
    candidate_id: NonBlank
    session_id: NonBlank
    version: PositiveInt
    parent_version: PositiveInt | None
    brief_version: PositiveInt
    world_version: NonBlank
    schema_version: Literal["quest-v1"] = "quest-v1"
    rules_version: NonBlank
    content_hash: NonBlank
    draft: QuestDraft
    created_at: NonBlank


class ValidationReport(StrictDesignModel):
    content_hash: NonBlank
    rules_version: NonBlank
    findings: list[ValidationFinding] = Field(default_factory=list)
    valid: bool = Field(strict=True)
    manual_review: list[NonBlank] = Field(default_factory=list)


def canonical_hash(draft: QuestDraft) -> str:
    """Hash the exact structured content, independent of dict key or JSON spacing."""
    payload = json.dumps(
        draft.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
