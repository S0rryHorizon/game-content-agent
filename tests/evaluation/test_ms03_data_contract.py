import json
from pathlib import Path
from typing import Any

from game_content_agent.domain.content import CandidateContent, CandidateContentType
from game_content_agent.domain.evaluation import EvaluationScenario
from game_content_agent.domain.state import GameState
from game_content_agent.domain.validation import (
    validate_candidate_citations,
    validate_completed_quest_prerequisites,
)
from game_content_agent.domain.world import LoreEntry, WorldSpecification

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORLD_DIR = PROJECT_ROOT / "data" / "world" / "v1"
SCENARIO_FILE = PROJECT_ROOT / "data" / "scenarios" / "seed_scenarios.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_world() -> WorldSpecification:
    return WorldSpecification.model_validate(
        json.loads((WORLD_DIR / "world.json").read_text())
    )


def load_lore_entries() -> list[LoreEntry]:
    return [
        LoreEntry.model_validate(row)
        for row in read_jsonl(WORLD_DIR / "lore_entries.jsonl")
    ]


def load_scenarios() -> list[EvaluationScenario]:
    return [EvaluationScenario.model_validate(row) for row in read_jsonl(SCENARIO_FILE)]


def test_world_lore_and_initial_state_are_readable_and_versioned() -> None:
    world = load_world()
    lore_entries = load_lore_entries()
    state = GameState.model_validate(
        json.loads((WORLD_DIR / "initial_state.json").read_text())
    )

    assert world.version == "v1"
    assert len(world.characters) >= 2
    assert len(world.locations) >= 2
    assert len(world.items) >= 2
    assert len(world.rules) >= 3
    assert len(lore_entries) >= 4
    lore_ids = [entry.lore_id for entry in lore_entries]
    assert len(lore_ids) == len(set(lore_ids))
    assert all(entry.world_version == world.version for entry in lore_entries)
    assert state.world_version == world.version


def test_lore_sources_resolve_to_world_facts() -> None:
    world = load_world()
    known_ids = {
        "characters": {character.character_id for character in world.characters},
        "locations": {location.location_id for location in world.locations},
        "items": {item.item_id for item in world.items},
        "rules": {rule.rule_id for rule in world.rules},
    }

    for entry in load_lore_entries():
        collection, object_id = entry.source.split(".", maxsplit=1)
        assert object_id in known_ids[collection]


def assert_state_references_world(state: GameState, world: WorldSpecification) -> None:
    location_ids = {location.location_id for location in world.locations}
    item_ids = {item.item_id for item in world.items}
    character_ids = {character.character_id for character in world.characters}

    assert state.player.location_id in location_ids
    assert all(item.item_id in item_ids for item in state.inventory)
    assert all(npc.npc_id in character_ids for npc in state.npcs)
    assert all(
        prerequisite in item_ids
        for quest in state.quests
        for prerequisite in quest.prerequisites
    )


def test_initial_state_and_each_scenario_state_reference_world_objects() -> None:
    world = load_world()
    initial_state = GameState.model_validate(
        json.loads((WORLD_DIR / "initial_state.json").read_text())
    )
    assert_state_references_world(initial_state, world)

    for scenario in load_scenarios():
        assert scenario.world_version == world.version
        assert_state_references_world(scenario.initial_state, world)


def test_ms03_has_at_least_two_normal_and_two_failure_scenarios() -> None:
    scenarios = load_scenarios()
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    normal = [
        scenario for scenario in scenarios if scenario.scenario_type.value == "normal"
    ]
    failures = [
        scenario for scenario in scenarios if scenario.scenario_type.value == "failure"
    ]

    assert len(scenario_ids) == len(set(scenario_ids))
    assert len(normal) >= 2
    assert len(failures) >= 2


def test_normal_scenarios_do_not_change_authoritative_state_before_review() -> None:
    for scenario in load_scenarios():
        assert scenario.expected_state_version == scenario.initial_state.version
        if scenario.scenario_type.value == "normal":
            assert scenario.expected_outcome.value == "success"
            assert scenario.expected_failure_type is None
            assert scenario.expected_finding_codes == []


def test_missing_prerequisite_failure_matches_declared_contract() -> None:
    scenario = next(
        scenario for scenario in load_scenarios() if scenario.scenario_id == "SC03"
    )
    findings = validate_completed_quest_prerequisites(scenario.initial_state)

    assert scenario.expected_failure_type == "state_transition_failure"
    assert [finding.code for finding in findings] == scenario.expected_finding_codes


def test_invalid_citation_failure_matches_declared_contract() -> None:
    scenario = next(
        scenario for scenario in load_scenarios() if scenario.scenario_id == "SC04"
    )
    candidate = CandidateContent(
        candidate_id="candidate-sc04",
        content_type=CandidateContentType.DIALOGUE,
        schema_version="v1",
        base_state_version=scenario.initial_state.version,
        payload={"text": "北潮门的潮声正在改变。"},
        citations=["lore-999"],
    )
    known_lore_ids = {entry.lore_id for entry in load_lore_entries()}
    findings = validate_candidate_citations(candidate, known_lore_ids)

    assert scenario.expected_failure_type == "invalid_citation"
    assert [finding.code for finding in findings] == scenario.expected_finding_codes
