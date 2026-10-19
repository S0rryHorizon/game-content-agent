import pytest
from pydantic import ValidationError

from game_content_agent.domain.world import LoreEntry, WorldSpecification


def minimal_world_payload() -> dict[str, object]:
    return {
        "world_id": "world-001",
        "version": "v1",
        "name": "测试世界",
        "summary": "一个用于测试的原创世界。",
        "characters": [
            {
                "character_id": "keeper",
                "name": "守门人",
                "role": "守门人",
                "description": "守护入口。",
            }
        ],
        "locations": [
            {
                "location_id": "harbor",
                "name": "港口",
                "description": "居民交换信息的地方。",
            }
        ],
        "items": [
            {
                "item_id": "key",
                "name": "钥匙",
                "description": "打开一扇门。",
            }
        ],
        "rules": [
            {
                "rule_id": "key_required",
                "description": "没有钥匙就不能开门。",
            }
        ],
    }


def test_world_specification_accepts_a_minimal_world() -> None:
    world = WorldSpecification.model_validate(minimal_world_payload())

    assert world.world_id == "world-001"
    assert world.version == "v1"
    assert world.characters[0].character_id == "keeper"
    assert world.rules[0].rule_id == "key_required"


def test_world_specification_rejects_duplicate_ids_in_one_collection() -> None:
    payload = minimal_world_payload()
    payload["items"] = [
        {
            "item_id": "key",
            "name": "钥匙",
            "description": "打开一扇门。",
        },
        {
            "item_id": "key",
            "name": "另一把钥匙",
            "description": "不应有重复 ID。",
        },
    ]

    with pytest.raises(ValidationError, match="items IDs must be unique"):
        WorldSpecification.model_validate(payload)


def test_world_specification_rejects_extra_fields() -> None:
    payload = minimal_world_payload()
    payload["weather"] = "rain"

    with pytest.raises(ValidationError):
        WorldSpecification.model_validate(payload)


def test_lore_entry_accepts_a_stable_source_reference() -> None:
    entry = LoreEntry.model_validate(
        {
            "lore_id": "lore-001",
            "world_version": "v1",
            "text": "没有钥匙就不能开门。",
            "tags": ["rule", "key"],
            "source": "rules.key_required",
        }
    )

    assert entry.source == "rules.key_required"


def test_lore_entry_rejects_an_unstructured_source_reference() -> None:
    with pytest.raises(ValidationError, match="source must look like"):
        LoreEntry.model_validate(
            {
                "lore_id": "lore-001",
                "world_version": "v1",
                "text": "没有钥匙就不能开门。",
                "tags": ["rule"],
                "source": "unknown-source",
            }
        )
