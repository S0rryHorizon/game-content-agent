"""Typed quest payloads and rule-driven static validation."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from game_content_agent.domain.design import (
    BriefConstraints,
    CandidateVersion,
    Condition,
    DesignBrief,
    QuestDraft,
    ValidationReport,
    canonical_hash,
)
from game_content_agent.domain.world import LoreEntry, WorldSpecification
from game_content_agent.validation.quest import validate_design

ROOT = Path(__file__).resolve().parents[2]


def load_json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def inputs() -> tuple[WorldSpecification, list[LoreEntry], dict]:
    world = WorldSpecification.model_validate(load_json("data/world/v1/world.json"))
    lore = [
        LoreEntry.model_validate(json.loads(line))
        for line in (ROOT / "data/world/v1/lore_entries.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    return world, lore, load_json("data/world/v1/rules.json")


def sample(name: str = "normal") -> QuestDraft:
    return QuestDraft.model_validate(load_json(f"data/examples/{name}.json"))


def report(draft: QuestDraft, brief: DesignBrief | None = None) -> ValidationReport:
    return validate_design(draft, *inputs(), brief)


def codes(result: ValidationReport) -> set[str]:
    return {finding.code for finding in result.findings}


def changed(**updates: object) -> QuestDraft:
    payload = sample().model_dump()
    payload.update(updates)
    return QuestDraft.model_validate(payload)


def test_examples_and_content_hash_are_stable() -> None:
    for name, expected_valid in (
        ("normal", True),
        ("revision", True),
        ("missing_key", False),
    ):
        draft = sample(name)
        result = report(draft)
        assert result.valid is expected_valid, result.findings
        assert result.content_hash == canonical_hash(draft)
        assert result.rules_version == "rules-v1"
        assert result.manual_review
    assert "missing_action_condition" in codes(report(sample("missing_key")))
    original = sample()
    same_data_different_key_order = json.loads(
        json.dumps(original.model_dump(), sort_keys=True)
    )
    assert canonical_hash(original) == canonical_hash(
        QuestDraft.model_validate(same_data_different_key_order)
    )
    assert canonical_hash(original) != canonical_hash(sample("revision"))


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, None])
def test_condition_value_rejects_invalid_numbers(value: object) -> None:
    with pytest.raises(ValidationError):
        Condition.model_validate(
            {"kind": "has_item", "entity_id": "blue_salt_key", "value": value}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "at_location", "entity_id": "lantern_harbor", "value": 2},
        {"kind": "at_location", "entity_id": "  "},
        {"kind": "has_item", "entity_id": 123},
        {"kind": "has_item", "entity_id": "blue_salt_key", "extra": True},
    ],
)
def test_conditions_reject_invalid_shape(payload: dict) -> None:
    with pytest.raises(ValidationError):
        Condition.model_validate(payload)


def test_boolean_and_version_models_reject_coercion() -> None:
    base = {
        "version": 1,
        "original_input": "一次任务",
        "goal": "开门",
        "constraints": {},
    }
    with pytest.raises(ValidationError):
        DesignBrief.model_validate({**base, "confirmed": "true"})
    with pytest.raises(ValidationError):
        DesignBrief.model_validate({**base, "version": True})
    brief = DesignBrief.model_validate(base)
    assert brief.constraints == BriefConstraints()
    candidate = {
        "candidate_id": "c",
        "session_id": "s",
        "version": 1,
        "parent_version": None,
        "brief_version": 1,
        "world_version": "v1",
        "rules_version": "rules-v1",
        "content_hash": canonical_hash(sample()),
        "draft": sample(),
        "created_at": "2026-09-17T00:00:00Z",
    }
    assert CandidateVersion.model_validate(candidate).schema_version == "quest-v1"
    with pytest.raises(ValidationError):
        CandidateVersion.model_validate({**candidate, "version": 1.0})
    with pytest.raises(ValidationError):
        QuestDraft.model_validate(
            {
                **sample().model_dump(),
                "nodes": [{**sample().nodes[0].model_dump(), "text": " "}],
            }
        )


def test_unknown_entities_actions_conditions_and_citation_version() -> None:
    payload = sample().model_dump()
    payload["nodes"][0]["character_ids"] = ["invented"]
    payload["nodes"][0]["actions"][0]["target_id"] = "invented"
    payload["nodes"][3]["actions"][1]["conditions"][0]["entity_id"] = "invented"
    payload["citations"][0]["world_version"] = "v2"
    result = report(QuestDraft.model_validate(payload))
    assert {
        "unknown_entity",
        "unknown_action_target",
        "citation_version_mismatch",
        "missing_action_condition",
    } <= codes(result)


def test_citation_must_exist_and_lore_source_must_exist() -> None:
    world, lore, rules = inputs()
    payload = sample().model_dump()
    payload["citations"][0]["lore_id"] = "invented"
    assert "invalid_citation" in codes(
        validate_design(QuestDraft.model_validate(payload), world, lore, rules, None)
    )
    broken_lore = [
        entry.model_copy(update={"source": "rules.invented"})
        if entry.lore_id == "lore-rule-gate-key-v1"
        else entry
        for entry in lore
    ]
    assert "invalid_citation_source" in codes(
        validate_design(sample(), world, broken_lore, rules, None)
    )


@pytest.mark.parametrize(
    ("node_index", "action_index", "target_id"),
    [
        (
            3,
            1,
            "lantern_harbor",
        ),  # a real location, but not the gate allowed by this rule
        (3, 0, "mira"),  # a real character, but not the route keeper
        (1, 0, "blue_salt_key"),  # a real item, but not the recording shell
    ],
)
def test_restricted_actions_reject_known_but_unmatched_targets(
    node_index: int,
    action_index: int,
    target_id: str,
) -> None:
    payload = sample().model_dump()
    payload["nodes"][node_index]["actions"][action_index]["target_id"] = target_id
    result = report(QuestDraft.model_validate(payload))
    assert not result.valid
    assert "invalid_action_target" in codes(result)


@pytest.mark.parametrize("reverse", [False, True])
def test_citation_resolves_pinned_version_independent_of_lore_order(
    reverse: bool,
) -> None:
    world, lore, rules = inputs()
    original = next(entry for entry in lore if entry.lore_id == "lore-rule-gate-key-v1")
    other_version = original.model_copy(
        update={"world_version": "v2", "source": "rules.invented"}
    )
    entries = [entry for entry in lore if entry is not original]
    pair = [original, other_version]
    if reverse:
        pair.reverse()
    assert validate_design(sample(), world, entries + pair, rules, None).valid
    result = validate_design(sample(), world, entries + [other_version], rules, None)
    assert "citation_version_mismatch" in codes(result)


@pytest.mark.parametrize("reverse", [False, True])
def test_citation_rejects_duplicate_same_version_key_in_both_orders(
    reverse: bool,
) -> None:
    world, lore, rules = inputs()
    original = next(entry for entry in lore if entry.lore_id == "lore-rule-gate-key-v1")
    ambiguous = original.model_copy(update={"source": "rules.invented"})
    entries = [entry for entry in lore if entry is not original]
    pair = [original, ambiguous]
    if reverse:
        pair.reverse()
    result = validate_design(sample(), world, entries + pair, rules, None)
    assert not result.valid
    assert "ambiguous_citation" in codes(result)


def test_graph_catches_dangling_cycle_unreachable_and_bad_branches() -> None:
    payload = sample().model_dump()
    payload["edges"][1]["target"] = "invented"
    payload["edges"][0]["label"] = payload["edges"][1]["label"]
    payload["edges"].append(
        {"source": "gate", "target": "harbor_start", "label": "回头"}
    )
    result = report(QuestDraft.model_validate(payload))
    assert {"dangling_edge", "invalid_branch", "cycle", "unreachable_node"} <= codes(
        result
    )


def test_branch_outlets_need_distinct_targets_even_with_distinct_labels() -> None:
    payload = sample().model_dump()
    payload["edges"][1]["target"] = payload["edges"][0]["target"]
    assert "invalid_branch" in codes(report(QuestDraft.model_validate(payload)))


def test_each_node_needs_path_to_ending_and_endings_have_no_outgoing_edges() -> None:
    payload = sample().model_dump()
    payload["edges"][-1] = {"source": "gate", "target": "gate", "label": "原地等待"}
    result = report(QuestDraft.model_validate(payload))
    assert {"no_ending_path", "cycle", "unreachable_node"} <= codes(result)
    payload = sample().model_dump()
    payload["edges"].append({"source": "safe_route", "target": "gate", "label": "返程"})
    assert "ending_has_edges" in codes(report(QuestDraft.model_validate(payload)))


def test_conditions_are_scoped_to_rule_action_and_node_location() -> None:
    payload = sample().model_dump()
    payload["nodes"][1]["location_id"] = "north_tide_gate"
    payload["nodes"][3]["actions"][0]["conditions"][0]["value"] = 1
    result = report(QuestDraft.model_validate(payload))
    assert {"location_condition_conflict", "missing_action_condition"} <= codes(result)


def test_required_conditions_use_rules_data_without_task_special_cases() -> None:
    world, lore, rules = inputs()
    swapped = deepcopy(rules)
    swapped["action_requirements"][0]["conditions"][0]["value"] = 2
    result = validate_design(sample(), world, lore, swapped, None)
    assert "missing_action_condition" in codes(result)
    assert "blue_salt_key" in " ".join(result.findings[-1].evidence)
    assert report(sample()).valid


def test_rule_data_version_and_references_fail_closed() -> None:
    world, lore, rules = inputs()
    mismatched = {**rules, "world_version": "v2"}
    assert "rules_world_version_mismatch" in codes(
        validate_design(sample(), world, lore, mismatched, None)
    )
    unsupported = {**rules, "version": "rules-v99"}
    assert "unsupported_rules_version" in codes(
        validate_design(sample(), world, lore, unsupported, None)
    )
    malformed = {**rules, "action_requirements": "not a list"}
    assert "invalid_rules" in codes(
        validate_design(sample(), world, lore, malformed, None)
    )
    incomplete = {**rules, "action_requirements": rules["action_requirements"][:-1]}
    assert "rules_incomplete" in codes(
        validate_design(sample(), world, lore, incomplete, None)
    )
    unknown_rule = deepcopy(rules)
    unknown_rule["action_requirements"][0]["rule_id"] = "invented"
    assert "invalid_rule_reference" in codes(
        validate_design(sample(), world, lore, unknown_rule, None)
    )
    missing_allowed = deepcopy(rules)
    missing_allowed["allowed_action_targets"]["open_gate"] = []
    assert "invalid_rules" in codes(
        validate_design(sample(), world, lore, missing_allowed, None)
    )


def test_structural_brief_constraints_and_manual_text_review() -> None:
    brief = DesignBrief(
        version=1,
        original_input="保留米拉并开门",
        goal="进入航路",
        must_keep=["米拉得到帮助"],
        constraints=BriefConstraints(
            required_character_ids=["mira"],
            required_location_ids=["north_tide_gate"],
            required_item_ids=["blue_salt_key"],
            required_ending="安全启航",
            protected_conditions=[
                Condition(kind="has_item", entity_id="blue_salt_key", value=1)
            ],
        ),
        confirmed=True,
    )
    valid = report(sample(), brief)
    assert valid.valid
    assert any("米拉得到帮助" in item for item in valid.manual_review)
    invalid = report(sample("missing_key"), brief)
    assert "protected_condition_missing" in codes(invalid)
    missing_ending = brief.model_copy(
        update={
            "constraints": brief.constraints.model_copy(
                update={"required_ending": "另外的结局"}
            )
        }
    )
    assert "brief_constraint_missing" in codes(report(sample(), missing_ending))


def test_protected_condition_only_applies_to_related_action_scope() -> None:
    draft = sample()
    brief = DesignBrief(
        version=1,
        original_input="保留钥匙条件",
        goal="开门",
        constraints=BriefConstraints(
            protected_conditions=[
                Condition(kind="has_item", entity_id="blue_salt_key", value=1)
            ]
        ),
    )
    assert report(draft, brief).valid  # talk and record_tide need no key
    payload = draft.model_dump()
    payload["nodes"][3]["actions"] = [payload["nodes"][3]["actions"][0]]
    assert "protected_condition_missing" in codes(
        report(QuestDraft.model_validate(payload), brief)
    )
