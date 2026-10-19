from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from game_content_agent.domain.state import GameState
from game_content_agent.domain.validation import (
    validate_completed_quest_prerequisites,
    validate_session_world_version,
    validate_state_version_transition,
)


def validate_model[ModelT: BaseModel](model_type: type[ModelT], **data: Any) -> ModelT:
    return model_type.model_validate(data)


def test_game_state_accepts_valid_data() -> None:
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[{"item_id": "key", "quantity": 1}],
        quests=[
            {
                "quest_id": "secret-room",
                "status": "active",
                "prerequisites": ["key"],
            }
        ],
        npcs=[{"npc_id": "bridge_keeper", "trust_level": 2}],
    )

    assert state.state_id == "state-001"
    assert state.version == 1
    assert state.player.player_id == "player-001"
    assert state.inventory[0].item_id == "key"
    assert state.quests[0].status.value == "active"
    assert state.quests[0].prerequisites == ["key"]
    assert state.npcs[0].trust_level == 2


def test_game_state_rejects_non_integer_version() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version="第一版",
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": 10,
            },
            inventory=[],
            quests=[],
            npcs=[],
        )


def test_game_state_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=1,
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": 10,
            },
            inventory=[],
            quests=[],
            npcs=[],
            weather="rain",
        )


def test_game_state_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=1,
            inventory=[],
            quests=[],
            npcs=[],
        )


def test_game_state_rejects_negative_coins() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=1,
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": -5,
            },
            inventory=[],
            quests=[],
            npcs=[],
        )


def test_game_state_rejects_negative_inventory_quantity() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=1,
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": 10,
            },
            inventory=[{"item_id": "key", "quantity": -1}],
            quests=[],
            npcs=[],
        )


def test_game_state_rejects_version_zero() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=0,
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": 10,
            },
            inventory=[],
            quests=[],
            npcs=[],
        )


def test_game_state_rejects_unknown_quest_status() -> None:
    with pytest.raises(ValidationError):
        validate_model(
            GameState,
            state_id="state-001",
            world_version="v1",
            version=1,
            player={
                "player_id": "player-001",
                "location_id": "village",
                "coins": 10,
            },
            inventory=[],
            quests=[{"quest_id": "secret-room", "status": "finished"}],
            npcs=[],
        )


def test_completed_quest_with_required_item_has_no_finding() -> None:
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[{"item_id": "key", "quantity": 1}],
        quests=[
            {
                "quest_id": "secret-room",
                "status": "completed",
                "prerequisites": ["key"],
            }
        ],
        npcs=[],
    )

    assert validate_completed_quest_prerequisites(state) == []


def test_completed_quest_without_required_item_has_finding() -> None:
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[
            {
                "quest_id": "secret-room",
                "status": "completed",
                "prerequisites": ["key"],
            }
        ],
        npcs=[],
    )

    findings = validate_completed_quest_prerequisites(state)

    assert len(findings) == 1
    assert findings[0].code == "missing_prerequisite"
    assert findings[0].location == "quests.secret-room"
    assert findings[0].evidence == ["key"]


def test_active_quest_without_required_item_has_no_finding() -> None:
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[
            {
                "quest_id": "secret-room",
                "status": "active",
                "prerequisites": ["key"],
            }
        ],
        npcs=[],
    )

    assert validate_completed_quest_prerequisites(state) == []


def test_state_transition_accepts_a_greater_version() -> None:
    previous = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )
    current = validate_model(
        GameState,
        state_id="state-002",
        world_version="v1",
        version=2,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )

    assert validate_state_version_transition(previous, current) == []


def test_session_and_state_with_same_world_version_have_no_finding() -> None:
    from game_content_agent.domain.agent import AgentSession

    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="active",
    )
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )

    assert validate_session_world_version(session, state) == []


def test_session_and_state_with_different_world_versions_have_finding() -> None:
    from game_content_agent.domain.agent import AgentSession

    session = validate_model(
        AgentSession,
        session_id="session-001",
        world_version="v1",
        current_state_id="state-001",
        status="active",
    )
    state = validate_model(
        GameState,
        state_id="state-001",
        world_version="v2",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )

    findings = validate_session_world_version(session, state)

    assert len(findings) == 1
    assert findings[0].code == "world_version_mismatch"
    assert findings[0].location == "world_version"


def test_state_transition_rejects_a_version_that_does_not_advance() -> None:
    previous = validate_model(
        GameState,
        state_id="state-002",
        world_version="v1",
        version=2,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )
    current = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=1,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )

    findings = validate_state_version_transition(previous, current)

    assert len(findings) == 1
    assert findings[0].code == "invalid_state_version"
    assert findings[0].location == "version"


def test_state_transition_rejects_an_equal_version() -> None:
    previous = validate_model(
        GameState,
        state_id="state-001",
        world_version="v1",
        version=2,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )
    current = validate_model(
        GameState,
        state_id="state-002",
        world_version="v1",
        version=2,
        player={
            "player_id": "player-001",
            "location_id": "village",
            "coins": 10,
        },
        inventory=[],
        quests=[],
        npcs=[],
    )

    findings = validate_state_version_transition(previous, current)

    assert len(findings) == 1
    assert findings[0].code == "invalid_state_version"
    assert findings[0].location == "version"
