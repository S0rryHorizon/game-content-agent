"""Static quest validation; structural paths do not establish playability."""

from collections import defaultdict, deque
from typing import Any, Literal

from pydantic import Field, ValidationError

from game_content_agent.domain.design import (
    BriefConstraints,
    Condition,
    DesignBrief,
    NonBlank,
    QuestDraft,
    StrictDesignModel,
    ValidationReport,
    canonical_hash,
)
from game_content_agent.domain.validation import ValidationFinding
from game_content_agent.domain.world import LoreEntry, WorldSpecification

RESTRICTED_KINDS = ("open_gate", "reveal_route", "record_tide")


class _ActionRequirement(StrictDesignModel):
    rule_id: NonBlank
    kind: Literal["talk", "open_gate", "reveal_route", "record_tide"]
    target_id: NonBlank
    conditions: list[Condition] = Field(min_length=1)


class _RuleSet(StrictDesignModel):
    version: NonBlank
    world_version: NonBlank
    action_targets: dict[str, Literal["characters", "locations", "items"]]
    allowed_action_targets: dict[
        Literal["open_gate", "reveal_route", "record_tide"], list[NonBlank]
    ]
    action_requirements: list[_ActionRequirement]


def _satisfies(actual: Condition, required: Condition) -> bool:
    return (
        actual.kind == required.kind
        and actual.entity_id == required.entity_id
        and actual.value >= required.value
    )


def validate_design(
    draft: QuestDraft,
    world: WorldSpecification,
    lore_entries: list[LoreEntry],
    rules: dict[str, Any],
    brief: DesignBrief | None,
) -> ValidationReport:
    """Validate declared references, graph and encoded action prerequisites.

    The checker does not interpret natural language or simulate acquisition,
    player movement, trust changes, or the Game State.
    """
    findings: list[ValidationFinding] = []
    manual_review = [
        "人工核对剧情、对白与引用事实的语义是否一致；引用合法不等于文字正确。",
        "结构上通往结局不保证玩家在当前状态能取得物品、信任或执行动作。",
    ]

    def add(code: str, location: str, message: str, *evidence: str) -> None:
        findings.append(
            ValidationFinding(
                code=code,
                severity="error",
                location=location,
                message=message,
                evidence=list(evidence),
            )
        )

    rules_version = rules.get("version") if isinstance(rules, dict) else None
    if not isinstance(rules_version, str) or not rules_version.strip():
        rules_version = "unknown"
    parsed_rules: _RuleSet | None = None
    try:
        parsed_rules = _RuleSet.model_validate(rules)
    except ValidationError as exc:
        add("invalid_rules", "rules", "rules data is malformed", str(exc))
    if parsed_rules is not None:
        if parsed_rules.version != "rules-v1":
            add(
                "unsupported_rules_version",
                "rules.version",
                "rules schema version is unsupported",
                parsed_rules.version,
            )
        if parsed_rules.world_version != world.version:
            add(
                "rules_world_version_mismatch",
                "rules.world_version",
                "rules refer to another world version",
                parsed_rules.world_version,
                world.version,
            )
        world_rule_ids = {rule.rule_id for rule in world.rules}
        missing_rule_ids = world_rule_ids - {
            requirement.rule_id for requirement in parsed_rules.action_requirements
        }
        if missing_rule_ids:
            add(
                "rules_incomplete",
                "rules.action_requirements",
                "world rules have no encoded action requirements",
                *sorted(missing_rule_ids),
            )
        target_kinds = {
            "characters": {character.character_id for character in world.characters},
            "locations": {location.location_id for location in world.locations},
            "items": {item.item_id for item in world.items},
        }
        for kind in ("talk", "open_gate", "reveal_route", "record_tide"):
            if kind not in parsed_rules.action_targets:
                add(
                    "invalid_rules",
                    f"rules.action_targets.{kind}",
                    "action target category is missing",
                )
        required_pairs = [
            (rule.kind, rule.target_id) for rule in parsed_rules.action_requirements
        ]
        if len(required_pairs) != len(set(required_pairs)):
            add(
                "invalid_rules",
                "rules.action_requirements",
                "action requirements contain duplicate action targets",
            )
        for kind in RESTRICTED_KINDS:
            allowed = parsed_rules.allowed_action_targets.get(kind)
            if not allowed:
                add(
                    "invalid_rules",
                    f"rules.allowed_action_targets.{kind}",
                    "restricted action needs explicitly allowed targets",
                )
                continue
            if len(allowed) != len(set(allowed)):
                add(
                    "invalid_rules",
                    f"rules.allowed_action_targets.{kind}",
                    "allowed target IDs must be unique",
                )
            category = parsed_rules.action_targets.get(kind)
            for target_id in allowed:
                if category is None or target_id not in target_kinds[category]:
                    add(
                        "invalid_rule_reference",
                        f"rules.allowed_action_targets.{kind}",
                        "allowed target is not a world entity of the declared category",
                        target_id,
                    )
                if (kind, target_id) not in required_pairs:
                    add(
                        "rules_incomplete",
                        f"rules.allowed_action_targets.{kind}",
                        "allowed action target has no encoded requirement",
                        target_id,
                    )
        for index, requirement in enumerate(parsed_rules.action_requirements):
            path = f"rules.action_requirements.{index}"
            if requirement.rule_id not in world_rule_ids:
                add(
                    "invalid_rule_reference",
                    f"{path}.rule_id",
                    "rule ID is not defined in this world",
                    requirement.rule_id,
                )
            category = parsed_rules.action_targets.get(requirement.kind)
            if category is None or requirement.target_id not in target_kinds[category]:
                add(
                    "invalid_rule_reference",
                    f"{path}.target_id",
                    "rule target is not a world entity of the declared category",
                    requirement.target_id,
                )
            if (
                requirement.kind in RESTRICTED_KINDS
                and requirement.target_id
                not in parsed_rules.allowed_action_targets.get(requirement.kind, [])
            ):
                add(
                    "invalid_rule_reference",
                    f"{path}.target_id",
                    "rule target is not explicitly allowed for this action",
                    requirement.target_id,
                )
            for condition_index, condition in enumerate(requirement.conditions):
                if (
                    condition.entity_id
                    not in target_kinds[_condition_category(condition)]
                ):
                    add(
                        "invalid_rule_reference",
                        f"{path}.conditions.{condition_index}",
                        "rule condition references an unknown world entity",
                        condition.entity_id,
                    )

    characters = {character.character_id for character in world.characters}
    locations = {location.location_id for location in world.locations}
    items = {item.item_id for item in world.items}
    known = {"characters": characters, "locations": locations, "items": items}
    nodes_by_id = {node.node_id: node for node in draft.nodes}
    node_ids = [node.node_id for node in draft.nodes]
    if not 3 <= len(draft.nodes) <= 8:
        add(
            "node_count",
            "nodes",
            "quest must contain 3 to 8 nodes",
            str(len(draft.nodes)),
        )
    if len(nodes_by_id) != len(draft.nodes):
        add("duplicate_node_id", "nodes", "node IDs must be unique")
    if draft.entry_node not in nodes_by_id:
        add(
            "invalid_entry", "entry_node", "entry node does not exist", draft.entry_node
        )

    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    declared_outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    incoming: dict[str, set[str]] = defaultdict(set)
    for index, edge in enumerate(draft.edges):
        path = f"edges.{index}"
        if edge.source not in nodes_by_id:
            add(
                "dangling_edge", f"{path}.source", "edge source is missing", edge.source
            )
        if edge.target not in nodes_by_id:
            add(
                "dangling_edge", f"{path}.target", "edge target is missing", edge.target
            )
        if edge.source in nodes_by_id:
            declared_outgoing[edge.source].append((edge.target, edge.label))
        if edge.source in nodes_by_id and edge.target in nodes_by_id:
            outgoing[edge.source].append((edge.target, edge.label))
            incoming[edge.target].add(edge.source)
    branch_count = 0
    for index, node in enumerate(draft.nodes):
        path = f"nodes.{index}"
        _check_reference(node.location_id, locations, f"{path}.location_id", add)
        for field_name, ids, collection in (
            ("character_ids", node.character_ids, characters),
            ("item_ids", node.item_ids, items),
        ):
            for pos, entity_id in enumerate(ids):
                _check_reference(
                    entity_id, collection, f"{path}.{field_name}.{pos}", add
                )
        edges = declared_outgoing[node.node_id]
        if node.ending is not None and edges:
            add(
                "ending_has_edges",
                path,
                "ending node must not have outgoing edges",
                node.node_id,
            )
        if node.ending is None and not edges:
            add(
                "dead_end",
                path,
                "non-ending node must have an outgoing edge",
                node.node_id,
            )
        if len(edges) >= 2:
            branch_count += 1
            if (
                len(edges) != 2
                or len({label for _, label in edges}) != 2
                or len({target for target, _ in edges}) != 2
            ):
                add(
                    "invalid_branch",
                    path,
                    "branch must have exactly two outgoing edges with distinct labels and targets",
                    node.node_id,
                )
        if node.node_id != draft.entry_node and not incoming[node.node_id]:
            add(
                "isolated_node",
                path,
                "non-entry node has no incoming edge",
                node.node_id,
            )
        for action_index, action in enumerate(node.actions):
            action_path = f"{path}.actions.{action_index}"
            if parsed_rules is not None:
                category = parsed_rules.action_targets.get(action.kind)
                if category is None:
                    add(
                        "invalid_rules",
                        f"rules.action_targets.{action.kind}",
                        "action target category is missing",
                    )
                elif action.target_id not in known[category]:
                    add(
                        "unknown_action_target",
                        f"{action_path}.target_id",
                        "action target is not a world entity of the declared category",
                        action.target_id,
                    )
                if action.kind in RESTRICTED_KINDS and (
                    action.target_id
                    not in parsed_rules.allowed_action_targets.get(action.kind, [])
                    or not any(
                        (action.kind, action.target_id) == (rule.kind, rule.target_id)
                        for rule in parsed_rules.action_requirements
                    )
                ):
                    add(
                        "invalid_action_target",
                        f"{action_path}.target_id",
                        "action and target have no matching encoded world rule",
                        action.kind,
                        action.target_id,
                    )
                if category == "locations" and action.target_id != node.location_id:
                    add(
                        "action_location_conflict",
                        action_path,
                        "location-target action must occur at its target location",
                        action.target_id,
                        node.location_id,
                    )
            for condition_index, condition in enumerate(action.conditions):
                condition_path = f"{action_path}.conditions.{condition_index}"
                _check_reference(
                    condition.entity_id,
                    known[_condition_category(condition)],
                    condition_path,
                    add,
                )
                if (
                    condition.kind == "at_location"
                    and condition.entity_id != node.location_id
                ):
                    add(
                        "location_condition_conflict",
                        condition_path,
                        "node location contradicts its location condition",
                        condition.entity_id,
                        node.location_id,
                    )
            if parsed_rules is not None:
                for requirement in parsed_rules.action_requirements:
                    if (action.kind, action.target_id) != (
                        requirement.kind,
                        requirement.target_id,
                    ):
                        continue
                    for required in requirement.conditions:
                        if not any(
                            _satisfies(actual, required) for actual in action.conditions
                        ):
                            add(
                                "missing_action_condition",
                                action_path,
                                "action omits a required rule condition",
                                requirement.rule_id,
                                f"{required.kind}:{required.entity_id}>={required.value}",
                            )
    if not 1 <= branch_count <= 2:
        add(
            "branch_count",
            "nodes",
            "quest needs one or two branching nodes",
            str(branch_count),
        )
    ending_ids = {node.node_id for node in draft.nodes if node.ending is not None}
    if not ending_ids:
        add("missing_ending", "nodes", "quest needs an explicit ending")
    if _has_cycle(nodes_by_id, outgoing):
        add("cycle", "edges", "quest graph must have no cycle")
    reachable = (
        _reachable({draft.entry_node}, outgoing)
        if draft.entry_node in nodes_by_id
        else set()
    )
    for node_id in node_ids:
        if node_id not in reachable:
            add(
                "unreachable_node",
                f"nodes.{node_id}",
                "node cannot be reached from entry",
                node_id,
            )
    can_end = _reachable(
        ending_ids,
        {
            node: [(parent, "") for parent in parents]
            for node, parents in incoming.items()
        },
    )
    for node_id in node_ids:
        if node_id not in can_end:
            add(
                "no_ending_path",
                f"nodes.{node_id}",
                "node has no structural path to an ending",
                node_id,
            )

    lore_by_key: dict[tuple[str, str], list[LoreEntry]] = defaultdict(list)
    lore_versions: dict[str, set[str]] = defaultdict(set)
    for entry in lore_entries:
        lore_by_key[(entry.lore_id, entry.world_version)].append(entry)
        lore_versions[entry.lore_id].add(entry.world_version)
    for index, citation in enumerate(draft.citations):
        path = f"citations.{index}"
        matches = lore_by_key.get((citation.lore_id, citation.world_version), [])
        if citation.world_version != world.version:
            add(
                "citation_version_mismatch",
                path,
                "citation must match the pinned world version",
                citation.lore_id,
                citation.world_version,
                world.version,
            )
        elif len(matches) > 1:
            add(
                "ambiguous_citation",
                path,
                "multiple lore entries share the cited ID and world version",
                citation.lore_id,
                citation.world_version,
            )
        elif not matches:
            if citation.lore_id in lore_versions:
                add(
                    "citation_version_mismatch",
                    path,
                    "cited lore has no entry in the pinned world version",
                    citation.lore_id,
                    *sorted(lore_versions[citation.lore_id]),
                    world.version,
                )
            else:
                add(
                    "invalid_citation",
                    path,
                    "cited lore does not exist",
                    citation.lore_id,
                )
        elif not _lore_source_exists(matches[0], world):
            add(
                "invalid_citation_source",
                path,
                "lore source does not exist in the pinned world",
                matches[0].source,
            )

    if brief is not None:
        _check_brief(brief.constraints, draft, parsed_rules, add)
        for item in brief.must_keep:
            manual_review.append(f"人工核对必须保留的文字要求：{item}")
        manual_review.append(f"人工核对目标与原始输入是否一致：{brief.goal}")
    return ValidationReport(
        content_hash=canonical_hash(draft),
        rules_version=rules_version,
        findings=findings,
        valid=not findings,
        manual_review=manual_review,
    )


def _condition_category(condition: Condition) -> str:
    return {
        "has_item": "items",
        "trust_at_least": "characters",
        "at_location": "locations",
    }[condition.kind]


def _check_reference(entity_id: str, known: set[str], location: str, add: Any) -> None:
    if entity_id not in known:
        add("unknown_entity", location, "entity is not in the pinned world", entity_id)


def _reachable(starts: set[str], edges: dict[str, list[tuple[str, str]]]) -> set[str]:
    seen = set(starts)
    queue = deque(starts)
    while queue:
        for next_id, _ in edges.get(queue.popleft(), []):
            if next_id not in seen:
                seen.add(next_id)
                queue.append(next_id)
    return seen


def _has_cycle(nodes: dict[str, Any], edges: dict[str, list[tuple[str, str]]]) -> bool:
    active: set[str] = set()
    done: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in active:
            return True
        if node_id in done:
            return False
        active.add(node_id)
        if any(visit(next_id) for next_id, _ in edges.get(node_id, [])):
            return True
        active.remove(node_id)
        done.add(node_id)
        return False

    return any(visit(node_id) for node_id in nodes if node_id not in done)


def _lore_source_exists(entry: LoreEntry, world: WorldSpecification) -> bool:
    category, _, entity_id = entry.source.partition(".")
    collections = {
        "characters": {character.character_id for character in world.characters},
        "locations": {location.location_id for location in world.locations},
        "items": {item.item_id for item in world.items},
        "rules": {rule.rule_id for rule in world.rules},
    }
    return entity_id in collections.get(category, set())


def _check_brief(
    constraints: BriefConstraints,
    draft: QuestDraft,
    rules: _RuleSet | None,
    add: Any,
) -> None:
    uses = {
        "characters": {
            entity_id for node in draft.nodes for entity_id in node.character_ids
        },
        "locations": {node.location_id for node in draft.nodes},
        "items": {entity_id for node in draft.nodes for entity_id in node.item_ids},
    }
    for field_name, category in (
        ("required_character_ids", "characters"),
        ("required_location_ids", "locations"),
        ("required_item_ids", "items"),
    ):
        for entity_id in getattr(constraints, field_name):
            if entity_id not in uses[category]:
                add(
                    "brief_constraint_missing",
                    f"brief.constraints.{field_name}",
                    "required entity is absent from quest nodes",
                    entity_id,
                )
    if constraints.required_ending is not None and constraints.required_ending not in {
        node.ending for node in draft.nodes
    }:
        add(
            "brief_constraint_missing",
            "brief.constraints.required_ending",
            "required ending is absent",
            constraints.required_ending,
        )
    for index, condition in enumerate(constraints.protected_conditions):
        location = f"brief.constraints.protected_conditions.{index}"
        matching_rules = (
            []
            if rules is None
            else [
                rule
                for rule in rules.action_requirements
                if any(
                    required.kind == condition.kind
                    and required.entity_id == condition.entity_id
                    for required in rule.conditions
                )
            ]
        )
        scoped_actions = [
            action
            for node in draft.nodes
            for action in node.actions
            if any(
                (action.kind, action.target_id) == (rule.kind, rule.target_id)
                for rule in matching_rules
            )
        ]
        if matching_rules and not scoped_actions:
            add(
                "protected_condition_missing",
                location,
                "protected condition has no action in its rule scope",
                f"{condition.kind}:{condition.entity_id}>={condition.value}",
            )
        elif scoped_actions:
            if any(
                not any(_satisfies(actual, condition) for actual in action.conditions)
                for action in scoped_actions
            ):
                add(
                    "protected_condition_missing",
                    location,
                    "protected condition is missing from an action in its rule scope",
                    f"{condition.kind}:{condition.entity_id}>={condition.value}",
                )
        elif not any(
            _satisfies(actual, condition)
            for node in draft.nodes
            for action in node.actions
            for actual in action.conditions
        ):
            add(
                "protected_condition_missing",
                location,
                "protected condition is absent from quest actions",
                f"{condition.kind}:{condition.entity_id}>={condition.value}",
            )
