"""Authoritative game state models."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlayerState(BaseModel):
    """The player's current state."""

    model_config = ConfigDict(extra="forbid")

    player_id: str
    location_id: str
    coins: int

    @field_validator("coins")
    @classmethod
    def coins_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("coins must not be negative")
        return value


class InventoryItem(BaseModel):
    """One item in the player's inventory."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    quantity: int

    @field_validator("quantity")
    @classmethod
    def quantity_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("quantity must not be negative")
        return value


class QuestStatus(str, Enum):
    """Allowed lifecycle states for a quest."""

    NOT_STARTED = "not_started"
    ACTIVE = "active"
    COMPLETED = "completed"


class QuestState(BaseModel):
    """One quest and its current status."""

    model_config = ConfigDict(extra="forbid")

    quest_id: str
    status: QuestStatus
    prerequisites: list[str] = Field(default_factory=list)


class NPCState(BaseModel):
    """One non-player character and its current relationship state."""

    model_config = ConfigDict(extra="forbid")

    npc_id: str
    trust_level: int


class GameState(BaseModel):
    """The authoritative state of the game at one point in time."""

    model_config = ConfigDict(extra="forbid")

    state_id: str
    world_version: str
    version: int
    player: PlayerState
    inventory: list[InventoryItem]
    quests: list[QuestState]
    npcs: list[NPCState]

    @field_validator("version")
    @classmethod
    def version_must_start_at_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be at least 1")
        return value
