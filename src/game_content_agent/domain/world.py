"""Versioned facts that define the game world."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorldCharacter(BaseModel):
    """A character with facts that remain stable within a world version."""

    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    description: str = Field(min_length=1)


class WorldLocation(BaseModel):
    """A location that can be referenced by content and GameState."""

    model_config = ConfigDict(extra="forbid")

    location_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class WorldItem(BaseModel):
    """An item that can appear in a player's inventory."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class WorldRule(BaseModel):
    """A deterministic fact or constraint in the world."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class WorldSpecification(BaseModel):
    """The immutable, versioned fact handbook for one game world."""

    model_config = ConfigDict(extra="forbid")

    world_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    characters: list[WorldCharacter] = Field(min_length=1)
    locations: list[WorldLocation] = Field(min_length=1)
    items: list[WorldItem] = Field(min_length=1)
    rules: list[WorldRule] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_are_unique_within_each_collection(self) -> "WorldSpecification":
        collections = {
            "characters": [character.character_id for character in self.characters],
            "locations": [location.location_id for location in self.locations],
            "items": [item.item_id for item in self.items],
            "rules": [rule.rule_id for rule in self.rules],
        }
        for collection_name, ids in collections.items():
            if len(ids) != len(set(ids)):
                raise ValueError(f"{collection_name} IDs must be unique")
        return self


class LoreEntry(BaseModel):
    """One versioned fact that retrieval can return and content can cite."""

    model_config = ConfigDict(extra="forbid")

    lore_id: str = Field(min_length=1)
    world_version: str = Field(min_length=1)
    text: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    source: str = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def source_is_a_stable_reference(cls, value: str) -> str:
        collection, separator, object_id = value.partition(".")
        allowed_collections = {"characters", "locations", "items", "rules"}
        if not separator or collection not in allowed_collections or not object_id:
            raise ValueError(
                "source must look like characters.<id>, locations.<id>, "
                "items.<id>, or rules.<id>"
            )
        return value

    @model_validator(mode="after")
    def tags_are_not_blank(self) -> "LoreEntry":
        if any(not tag.strip() for tag in self.tags):
            raise ValueError("tags must not contain blank values")
        return self


__all__ = [
    "LoreEntry",
    "WorldCharacter",
    "WorldItem",
    "WorldLocation",
    "WorldRule",
    "WorldSpecification",
]
