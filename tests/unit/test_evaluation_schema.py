import pytest
from pydantic import ValidationError

from game_content_agent.domain.evaluation import EvaluationScenario


def minimal_state() -> dict[str, object]:
    return {
        "state_id": "state-001",
        "world_version": "v1",
        "version": 1,
        "player": {
            "player_id": "player-001",
            "location_id": "harbor",
            "coins": 3,
        },
        "inventory": [],
        "quests": [],
        "npcs": [],
    }


def minimal_scenario(**overrides: object) -> dict[str, object]:
    scenario: dict[str, object] = {
        "scenario_id": "SC01",
        "name": "正常场景",
        "scenario_type": "normal",
        "world_version": "v1",
        "inputs": ["设计一条任务。"],
        "initial_state": minimal_state(),
        "expected_outcome": "success",
        "expected_state_version": 1,
        "expected_failure_type": None,
        "expected_finding_codes": [],
    }
    scenario.update(overrides)
    return scenario


def test_normal_scenario_accepts_a_success_contract() -> None:
    scenario = EvaluationScenario.model_validate(minimal_scenario())

    assert scenario.scenario_type.value == "normal"
    assert scenario.expected_outcome.value == "success"
    assert scenario.expected_failure_type is None
    assert scenario.expected_finding_codes == []


def test_failure_scenario_requires_a_failure_type_and_finding() -> None:
    scenario = EvaluationScenario.model_validate(
        minimal_scenario(
            scenario_id="SC03",
            name="失败场景",
            scenario_type="failure",
            expected_outcome="rejected",
            expected_failure_type="invalid_citation",
            expected_finding_codes=["invalid_citation"],
        )
    )

    assert scenario.scenario_type.value == "failure"
    assert scenario.expected_outcome.value == "rejected"


def test_normal_scenario_rejects_a_failure_expectation() -> None:
    with pytest.raises(ValidationError, match="normal scenarios must expect success"):
        EvaluationScenario.model_validate(minimal_scenario(expected_outcome="rejected"))


def test_failure_scenario_rejects_missing_finding_code() -> None:
    with pytest.raises(ValidationError, match="must declare a finding code"):
        EvaluationScenario.model_validate(
            minimal_scenario(
                scenario_type="failure",
                expected_outcome="rejected",
                expected_failure_type="invalid_citation",
            )
        )


def test_scenario_rejects_a_world_version_mismatch() -> None:
    with pytest.raises(ValidationError, match="same world version"):
        EvaluationScenario.model_validate(minimal_scenario(world_version="v2"))
