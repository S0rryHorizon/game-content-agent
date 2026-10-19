"""A bounded, observable authoring loop with independent final validation."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from game_content_agent.config import Settings, live_readiness
from game_content_agent.domain.design import DesignBrief, QuestDraft
from game_content_agent.domain.state import GameState
from game_content_agent.domain.world import LoreEntry, WorldSpecification
from game_content_agent.ports.model_gateway import ModelGateway
from game_content_agent.retrieval.lore import LoreRetriever
from game_content_agent.runtime.budget import BudgetExceeded, CostLedger
from game_content_agent.runtime.gateways import (
    FAKE_CONFLICT,
    FakeGateway,
    OpenAIGateway,
    ProviderResponseError,
    ProviderUsageError,
)
from game_content_agent.security import sanitize
from game_content_agent.validation.quest import validate_design


async def execute(
    *,
    purpose: str,
    instruction: str,
    world: WorldSpecification,
    lore_entries: list[LoreEntry],
    state: GameState,
    rules: dict[str, Any],
    brief: DesignBrief | None,
    previous: QuestDraft | None,
    mode: str,
    scenario: str,
    event_sink: Callable[[dict[str, Any]], None],
    settings: Settings,
) -> dict[str, Any]:
    started = time.monotonic()
    usage: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_sgd": 0.0,
        "mode": mode,
    }
    gateway: ModelGateway | None = None
    ledger: CostLedger | None = None
    retriever: LoreRetriever | None = None
    state_hash = state.model_dump_json()

    def emit(event: dict[str, Any]) -> None:
        event_sink(sanitize(event, secrets=(settings.openai_api_key,)))

    def usage_snapshot() -> dict[str, Any]:
        known_usage = dict(usage)
        if isinstance(gateway, OpenAIGateway):
            # The gateway records paid usage before decoding the response. A
            # malformed response can fail without returning a turn to this loop.
            known_usage.update(gateway.usage)
            known_usage["model_estimated_sgd"] = gateway.usage["estimated_sgd"]
        if retriever:
            known_usage.update(retriever.usage)
        if ledger:
            reservation_state = ledger.run_snapshot()
            known_usage.update(reservation_state)
            known_usage["estimated_sgd"] = reservation_state["settled_estimated_sgd"]
            known_usage["cost_basis"] = (
                "基于已知用量和配置费率的估算，非账单；未结算预留不代表实际 token 用量。"
            )
        else:
            known_usage.update(
                {
                    "unknown_reserved": False,
                    "unsettled_reservation_count": 0,
                    "unsettled_reserved_sgd": 0.0,
                    "unsettled_model_reserved_sgd": 0.0,
                    "unsettled_embedding_reserved_sgd": 0.0,
                    "settled_estimated_sgd": known_usage["estimated_sgd"],
                    "cost_basis": "离线脚本，无真实 API 用量。",
                }
            )
        known_usage["duration_ms"] = round((time.monotonic() - started) * 1000)
        return known_usage

    def result(status: str, reason: str, message: str, **values: Any) -> dict[str, Any]:
        final = {
            "status": status,
            "termination_reason": reason,
            "message": message,
            "brief": None,
            "draft": None,
            "report": None,
            "decision": None,
            "usage": usage_snapshot(),
            **values,
        }
        emit(
            {
                "kind": "termination",
                "message": message,
                "termination_reason": reason,
                "usage": final["usage"],
            }
        )
        if state.model_dump_json() != state_hash:
            raise RuntimeError("immutable input state was modified")
        return sanitize(final, secrets=(settings.openai_api_key,))

    async def loop() -> dict[str, Any]:
        nonlocal gateway, ledger, retriever
        if purpose not in {"prepare_brief", "generate", "revise"} or mode not in {
            "fake",
            "live",
        }:
            return result("failed", "invalid_input", "未知执行目的或模式。")
        if purpose != "prepare_brief" and (brief is None or not brief.confirmed):
            return result("needs_decision", "brief_unconfirmed", "请先确认需求摘要。")
        if purpose == "revise" and previous is None:
            return result("failed", "invalid_input", "修订需要已有任务版本。")
        if world.version != state.world_version:
            return result("failed", "world_version_mismatch", "世界与状态版本不匹配。")
        if (
            purpose == "generate"
            and brief is not None
            and instruction.strip() not in {brief.original_input.strip(), "生成任务"}
        ):
            return result(
                "needs_decision",
                "instruction_mismatch",
                "这次生成指令与已确认需求不一致；请重新整理并确认需求。",
                decision={
                    "message": "新增指令可能改变已确认目标或必须保留项，需要重新确认。",
                    "suggestions": [
                        "使用已确认的设计要求",
                        "修改要求后重新整理并确认摘要",
                    ],
                },
            )
        if mode == "live":
            ready, reason = live_readiness(settings)
            if not ready:
                return result("failed", "live_not_ready", reason)
            ledger = CostLedger(settings)
            context = {
                "purpose": purpose,
                "instruction": instruction,
                "world": world.model_dump(mode="json"),
                "brief": brief.model_dump(mode="json") if brief else None,
                "previous": previous.model_dump(mode="json") if previous else None,
                "rules": rules,
            }
            real = OpenAIGateway(settings, ledger, context)
            gateway = real
            retriever = LoreRetriever(
                lore_entries, world.version, settings, mode, real.client, ledger
            )
        else:
            fake_instruction = instruction
            if purpose == "generate" and brief:
                fake_instruction = brief.original_input
            gateway = FakeGateway(
                purpose=purpose,
                instruction=fake_instruction,
                scenario=scenario,
                settings=settings,
            )
            retriever = LoreRetriever(lore_entries, world.version, settings, mode)
        emit(
            {
                "kind": "start",
                "message": "离线脚本演示：不代表真实模型理解或语言质量。"
                if mode == "fake"
                else "真实模型执行。",
                "purpose": purpose,
                "instruction": instruction,
                "brief_version": brief.version if brief else None,
                "world_version": world.version,
            }
        )
        if (
            purpose != "prepare_brief"
            and brief
            and brief.original_input == FAKE_CONFLICT
        ):
            return result(
                "needs_decision",
                "core_conflict",
                "已确认目标要求始终无钥匙开门，与固定世界规则冲突。",
                decision={
                    "message": "请选择替代目标后重新整理并确认需求。",
                    "suggestions": [
                        "允许取得蓝盐钥匙后开门",
                        "保持无钥匙，将结局改为等待",
                    ],
                },
            )
        repairs = 0
        tools_used: set[str] = set()
        for step in range(1, settings.max_agent_steps + 1):
            step_start = time.monotonic()
            try:
                turn = await asyncio.wait_for(
                    gateway.next_turn(), settings.model_timeout_seconds
                )
            except (ValidationError, ValueError, KeyError, TypeError):
                repairs += 1
                emit(
                    {
                        "kind": "format_repair",
                        "step_number": step,
                        "message": "模型输出结构无效；仅重试格式，不更改目标。",
                    }
                )
                if repairs > settings.max_candidate_repairs:
                    return result(
                        "failed", "repair_limit", "结构输出修复次数达到上限。"
                    )
                gateway.observe("", {"error": "invalid_model_structure"})
                continue
            if mode == "fake":
                for name in ("input_tokens", "output_tokens", "estimated_sgd"):
                    usage[name] += turn.get("usage", {}).get(name, 0)
            kind = turn.get("kind")
            if kind == "decision":
                return result(
                    "needs_decision",
                    "needs_decision",
                    turn.get("message", "需要确认设计目标。"),
                    decision={
                        "message": turn.get("message", ""),
                        "suggestions": turn.get("suggestions", []),
                    },
                )
            if kind == "tool":
                name, arguments = turn.get("tool_name", ""), turn.get("arguments", {})
                call_id = turn.get("call_id", "")
                observation: dict[str, Any]
                try:
                    if not isinstance(arguments, dict):
                        raise TypeError("tool arguments must be an object")
                    if scenario == "tool_failure" and mode == "fake":
                        raise RuntimeError("scripted tool failure")

                    async def call_tool(
                        name: str = name, arguments: dict[str, Any] = arguments
                    ) -> dict[str, Any]:
                        if name == "read_state" and not arguments:
                            return {
                                "state": state.model_dump(mode="json"),
                                "read_only": True,
                            }
                        if (
                            name == "retrieve_lore"
                            and set(arguments) == {"query"}
                            and isinstance(arguments["query"], str)
                            and 0 < len(arguments["query"]) <= 2000
                        ):
                            assert retriever is not None
                            return await retriever.search(arguments["query"])
                        if name == "validate_content" and set(arguments) == {"draft"}:
                            draft = QuestDraft.model_validate(arguments["draft"])
                            return validate_design(
                                draft, world, lore_entries, rules, brief
                            ).model_dump(mode="json")
                        raise ValueError("unknown tool or invalid arguments")

                    data = await asyncio.wait_for(
                        call_tool(), settings.tool_timeout_seconds
                    )
                    observation = {"status": "success", "data": data}
                    tools_used.add(name)
                except BudgetExceeded:
                    raise
                except Exception as error:  # noqa: BLE001 - tool boundary maps failures to safe observations
                    observation = {
                        "status": "error",
                        "error": "tool_timeout"
                        if isinstance(error, TimeoutError)
                        else "tool_execution_failure",
                    }
                emit(
                    {
                        "kind": "tool",
                        "step_number": step,
                        "tool_name": name,
                        "call_id": call_id,
                        "arguments": arguments,
                        "observation": observation,
                        "usage": turn.get("usage", {}),
                        "duration_ms": round((time.monotonic() - step_start) * 1000),
                    }
                )
                gateway.observe(call_id, observation)
                if observation["status"] == "error":
                    return result(
                        "failed", "tool_failure", "工具调用失败，已保存失败记录。"
                    )
                continue
            if kind == "brief" and purpose == "prepare_brief":
                try:
                    proposed = DesignBrief.model_validate(turn["brief"])
                    # Raw user input and confirmation state are application-owned.
                    proposed = proposed.model_copy(
                        update={"original_input": instruction, "confirmed": False}
                    )
                    constraints = proposed.constraints
                    known = [
                        (
                            constraints.required_character_ids,
                            {x.character_id for x in world.characters},
                        ),
                        (
                            constraints.required_location_ids,
                            {x.location_id for x in world.locations},
                        ),
                        (
                            constraints.required_item_ids,
                            {x.item_id for x in world.items},
                        ),
                    ]
                    if any(set(ids) - allowed for ids, allowed in known):
                        return result(
                            "needs_decision",
                            "unknown_entity",
                            "摘要包含固定世界外的实体，请调整要求。",
                        )
                    return result(
                        "succeeded",
                        "brief_prepared",
                        "需求摘要已生成，等待你确认。",
                        brief=proposed.model_dump(mode="json"),
                    )
                except (ValidationError, KeyError):
                    pass
            elif kind == "draft" and purpose != "prepare_brief":
                try:
                    draft = QuestDraft.model_validate(turn["draft"])
                except (ValidationError, KeyError):
                    draft = None
                if draft is not None:
                    report = validate_design(draft, world, lore_entries, rules, brief)
                    emit(
                        {
                            "kind": "validation",
                            "step_number": step,
                            "observation": report.model_dump(mode="json"),
                        }
                    )
                    if not report.valid:
                        return result(
                            "needs_decision",
                            "validation_failure",
                            "候选存在规则或结构问题，需要选择修订方案。",
                            report=report.model_dump(mode="json"),
                            decision={
                                "message": "不能通过静默修改剧情、条件或结局消除这些问题。",
                                "suggestions": [
                                    "调整需求后重新确认",
                                    "保留需求，拒绝当前结果",
                                ],
                            },
                        )
                    if tools_used != {
                        "read_state",
                        "retrieve_lore",
                        "validate_content",
                    }:
                        return result(
                            "failed",
                            "missing_tool_evidence",
                            "尚未完成读取、检索与验证工具调用，不能提交候选。",
                        )
                    return result(
                        "succeeded",
                        "candidate_ready",
                        "静态检查通过，仍需人工审核剧情与设计目标。",
                        draft=draft.model_dump(mode="json"),
                        report=report.model_dump(mode="json"),
                    )
            repairs += 1
            emit(
                {
                    "kind": "format_repair",
                    "step_number": step,
                    "message": "候选结构不符合合同，重试格式。",
                }
            )
            if repairs > settings.max_candidate_repairs:
                return result("failed", "repair_limit", "格式修复次数达到上限。")
            gateway.observe("", {"error": "invalid_output_structure"})
        return result("failed", "loop_limit", "达到模型决策步骤上限。")

    try:
        return await asyncio.wait_for(loop(), settings.run_timeout_seconds)
    except TimeoutError:
        return result("failed", "timeout", "模型或执行超时，已保留执行记录。")
    except BudgetExceeded:
        return result(
            "failed", "budget_exceeded", "预算门槛已触发；未发出被阻止的请求。"
        )
    except ProviderUsageError as error:
        detail = "缺失" if error.reason == "missing" else "无效"
        return result(
            "failed",
            "provider_usage_error",
            f"模型服务返回的 token 用量{detail}；本次请求费用未知，已保留未结算预算预留，不会自动重试。",
        )
    except ProviderResponseError as error:
        if error.status == "incomplete":
            return result(
                "failed",
                "provider_incomplete",
                "模型服务未完成本次响应；已记录已知用量，不会自动重试或执行部分输出。",
            )
        return result(
            "failed",
            "provider_error",
            "模型服务未成功完成本次响应；已记录已知用量，不会自动重试或执行部分输出。",
        )
    except asyncio.CancelledError:
        emit(
            {
                "kind": "interrupted",
                "message": "执行中断，不会自动重放请求。",
                "usage": usage_snapshot(),
            }
        )
        raise
    except Exception:  # noqa: BLE001 - provider errors must not expose credentials
        return result(
            "failed", "provider_error", "模型或执行依赖发生错误，未生成可审核候选。"
        )
    finally:
        if gateway:
            await gateway.close()
        if ledger:
            ledger.close()
