"""Application orchestration for one versioned quest design per session."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from game_content_agent.config import Settings, live_readiness
from game_content_agent.domain.design import (
    CandidateVersion,
    DesignBrief,
    QuestDraft,
    canonical_hash,
)
from game_content_agent.domain.state import GameState
from game_content_agent.domain.world import LoreEntry, WorldSpecification
from game_content_agent.infrastructure.repository import Repository, RepositoryError
from game_content_agent.runtime.gateways import FAKE_CONFLICT, FAKE_GOAL, FAKE_REVISION
from game_content_agent.security import sanitize
from game_content_agent.validation.quest import validate_design


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _diff(before: Any, after: Any, path: str = "") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict] = []
        for key in sorted(before.keys() | after.keys()):
            changes.extend(_diff(before.get(key), after.get(key), f"{path}/{key}"))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for index in range(max(len(before), len(after))):
            old = before[index] if index < len(before) else None
            new = after[index] if index < len(after) else None
            changes.extend(_diff(old, new, f"{path}/{index}"))
        return changes
    if before != after:
        return [{"path": path or "/", "before": before, "after": after}]
    return []


class Service:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.root = Path(settings.project_root)
        directory = self.root / "data" / "world" / "v1"
        self.world = WorldSpecification.model_validate_json(
            (directory / "world.json").read_text()
        )
        self.state = GameState.model_validate_json(
            (directory / "initial_state.json").read_text()
        )
        self.lore_entries = [
            LoreEntry.model_validate_json(line)
            for line in (directory / "lore_entries.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.rules = json.loads((directory / "rules.json").read_text())
        if self.rules.get("world_version") != self.world.version:
            raise ValueError("规则与世界版本不一致")

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(value for value in (self.settings.openai_api_key,) if value)

    def clean(self, value: Any) -> Any:
        return sanitize(value, secrets=self.secrets)

    def world_info(self) -> dict:
        ready, reason = live_readiness(self.settings)
        return {
            "world": self.world.model_dump(),
            "state": self.state.model_dump(),
            "lore_entries": [entry.model_dump() for entry in self.lore_entries],
            "mode_options": ["fake", "live"],
            "fake_prompts": {
                "goal": FAKE_GOAL,
                "revision": FAKE_REVISION,
                "conflict": FAKE_CONFLICT,
            },
            "capabilities": {
                "live_enabled": self.settings.live_api_enabled,
                "live_ready": ready,
                "live_reason": reason,
            },
        }

    def create_session(self, world_version: str = "v1") -> dict:
        if world_version != self.world.version:
            raise RepositoryError("unsupported_world_version", "不支持该世界版本", 400)
        session = self.repository.create_session(
            uuid4().hex, world_version, self.state.model_dump(), utc_now()
        )
        return {
            key: session[key]
            for key in (
                "session_id",
                "world_version",
                "state_snapshot",
                "brief",
                "candidate_id",
            )
        }

    def get_session(self, session_id: str) -> dict:
        return self.repository.get_session(session_id)

    def start_run(
        self,
        session_id: str,
        purpose: str,
        instruction: str,
        mode: str,
        scenario: str,
        base_version: int | None,
    ) -> dict:
        if purpose not in {"prepare_brief", "generate", "revise"}:
            raise RepositoryError("invalid_purpose", "运行目的无效", 400)
        if mode not in {"fake", "live"}:
            raise RepositoryError("invalid_mode", "运行模式无效", 400)
        if not instruction.strip():
            raise RepositoryError("empty_instruction", "请填写设计要求", 400)
        if mode == "live":
            ready, reason = live_readiness(self.settings)
            if not ready:
                raise RepositoryError("live_unavailable", reason, 409)
        run_id = uuid4().hex
        self.repository.create_run(
            {
                "run_id": run_id,
                "session_id": session_id,
                "purpose": purpose,
                "instruction": self.clean(instruction),
                "mode": mode,
                "scenario": scenario,
                "base_version": base_version,
                "created_at": utc_now(),
            }
        )
        return {"run_id": run_id, "status": "running"}

    async def execute_run(self, run_id: str) -> None:
        """Always release a claimed run slot, including setup failures."""
        try:
            await self._execute_run(run_id)
        except asyncio.CancelledError:
            self.repository.finish_run(
                run_id,
                "interrupted",
                "server_shutdown",
                "服务停止，运行未完成",
                None,
                utc_now(),
            )
            raise
        except Exception:  # noqa: BLE001 - background failures must not strand a running row
            self.repository.finish_run(
                run_id,
                "failed",
                "application_error",
                "应用处理失败，请查看本次运行记录后重试",
                None,
                utc_now(),
            )

    async def _execute_run(self, run_id: str) -> None:
        from game_content_agent.runtime import engine

        run = self.repository.get_run_context(run_id)
        session = self.repository.get_session(run["session_id"])
        brief = (
            DesignBrief.model_validate(session["brief"]) if session["brief"] else None
        )
        previous = None
        if run["purpose"] == "revise":
            previous_row = self.repository.get_candidate(
                session["candidate_id"], run["base_version"]
            )
            previous = QuestDraft.model_validate(previous_row["candidate"]["draft"])

        def event_sink(event: dict[str, Any]) -> None:
            allowed = {
                "kind",
                "step_number",
                "tool_name",
                "arguments",
                "observation",
                "message",
                "usage",
                "duration_ms",
                "call_id",
                "termination_reason",
                "purpose",
                "instruction",
                "brief_version",
                "world_version",
            }
            self.repository.append_event(
                run_id, self.clean({k: v for k, v in event.items() if k in allowed})
            )

        try:
            raw = await asyncio.wait_for(
                engine.execute(
                    purpose=run["purpose"],
                    instruction=run["instruction"],
                    world=self.world,
                    lore_entries=self.lore_entries,
                    state=GameState.model_validate(session["state_snapshot"]),
                    rules=self.rules,
                    brief=brief,
                    previous=previous,
                    mode=run["mode"],
                    scenario=run["scenario"],
                    event_sink=event_sink,
                    settings=self.settings,
                ),
                timeout=self.settings.run_timeout_seconds,
            )
            result = self.clean(raw)
            if result.get("status") not in {"succeeded", "needs_decision", "failed"}:
                raise ValueError("runtime returned invalid status")
            next_brief: dict | None = None
            candidate: dict | None = None
            report: dict | None = None
            if result["status"] == "succeeded" and run["purpose"] == "prepare_brief":
                proposed = result.get("brief")
                if proposed is None:
                    raise ValueError("runtime omitted brief")
                proposed = dict(proposed)
                proposed["version"] = (brief.version if brief else 0) + 1
                proposed["confirmed"] = False
                next_brief = DesignBrief.model_validate(proposed).model_dump()
                result["brief"] = next_brief
            elif result["status"] == "succeeded" and run["purpose"] in {
                "generate",
                "revise",
            }:
                if brief is None or not brief.confirmed or result.get("draft") is None:
                    raise ValueError("runtime omitted confirmed brief or draft")
                draft = QuestDraft.model_validate(result["draft"])
                candidate_id = session["candidate_id"] or uuid4().hex
                version = len(session["versions"]) + 1
                model = CandidateVersion(
                    candidate_id=candidate_id,
                    session_id=run["session_id"],
                    version=version,
                    parent_version=version - 1 if version > 1 else None,
                    brief_version=brief.version,
                    world_version=self.world.version,
                    rules_version=self.rules["version"],
                    content_hash=canonical_hash(draft),
                    draft=draft,
                    created_at=utc_now(),
                )
                candidate = model.model_dump()
                report = validate_design(
                    draft, self.world, self.lore_entries, self.rules, brief
                ).model_dump()
                result["draft"] = draft.model_dump()
                result["report"] = report
            result["candidate"] = candidate
            result["brief"] = next_brief or (brief.model_dump() if brief else None)
            status = result["status"]
            self.repository.finish_run(
                run_id,
                status,
                str(result.get("termination_reason") or status),
                str(result.get("message") or ""),
                result,
                utc_now(),
                brief=next_brief,
                candidate=candidate,
                report=report,
            )
        except asyncio.CancelledError:
            self.repository.finish_run(
                run_id,
                "interrupted",
                "server_shutdown",
                "服务停止，运行未完成",
                None,
                utc_now(),
            )
            raise
        except TimeoutError:
            self.repository.finish_run(
                run_id, "failed", "run_timeout", "运行超过时间上限", None, utc_now()
            )
        except Exception:  # noqa: BLE001 - a failed background task must free the global run slot
            self.repository.finish_run(
                run_id,
                "failed",
                "application_error",
                "应用处理失败，请查看本次运行记录后重试",
                None,
                utc_now(),
            )

    def get_run(self, run_id: str) -> dict:
        return self.repository.get_run(run_id)

    def get_replay(self, run_id: str) -> dict:
        run = self.repository.get_run_context(run_id)
        events = self.repository.get_events(run_id)
        if not any(
            event.get("kind") == "start" and "instruction" in event for event in events
        ):
            # Old start events omitted the input, and early exits may have no
            # start event at all. Supplement the response without rewriting history.
            events.insert(
                0,
                {
                    "kind": "input_supplement",
                    "message": "补充公开输入：从已保存的运行记录读取；原始事件未修改。",
                    "instruction": self.clean(run["instruction"]),
                },
            )
        return {"run_id": run_id, "events": events}

    def confirm_brief(self, session_id: str, version: int) -> dict:
        self.repository.get_session(session_id)
        return {"brief": self.repository.confirm_brief(session_id, version)}

    def get_candidate_version(self, candidate_id: str, version: int) -> dict:
        row = self.repository.get_candidate(candidate_id, version)
        parent = row["candidate"]["parent_version"]
        previous_draft = None
        if parent is not None:
            previous_draft = self.repository.get_candidate(candidate_id, parent)[
                "candidate"
            ]["draft"]
        return {
            "candidate": row["candidate"],
            "report": row["report"],
            "review": row["review"],
            "diff": _diff(previous_draft, row["candidate"]["draft"]) if parent else [],
        }

    def review(
        self,
        candidate_id: str,
        version: int,
        decision: str,
        reason: str,
        content_hash: str,
    ) -> dict:
        if decision not in {"accept", "reject"}:
            raise RepositoryError("invalid_decision", "审核决定无效", 400)
        if not reason.strip():
            raise RepositoryError("empty_reason", "请填写审核理由", 400)

        def revalidate(candidate_data: dict, brief_data: dict) -> dict:
            candidate = CandidateVersion.model_validate(candidate_data)
            if candidate.content_hash != canonical_hash(candidate.draft):
                raise RepositoryError("hash_mismatch", "持久化内容摘要不匹配")
            return validate_design(
                candidate.draft,
                self.world,
                self.lore_entries,
                self.rules,
                DesignBrief.model_validate(brief_data),
            ).model_dump()

        return self.repository.review(
            candidate_id,
            version,
            decision,
            self.clean(reason),
            content_hash,
            utc_now(),
            revalidate,
        )

    def export(
        self, candidate_id: str, version: int, format: str
    ) -> tuple[str, str, str]:
        if format not in {"json", "markdown"}:
            raise RepositoryError("invalid_format", "导出格式无效", 400)
        row = self.repository.get_candidate(candidate_id, version)
        if not row["review"] or row["review"]["decision"] != "accept":
            raise RepositoryError("not_accepted", "只有已接受的版本可以正式导出", 403)
        candidate = row["candidate"]
        document = {
            "candidate": candidate,
            "report": row["report"],
            "review": row["review"],
        }
        if format == "json":
            return (
                json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2),
                "application/json",
                f"quest-{candidate_id}-v{version}.json",
            )
        draft = candidate["draft"]
        lines = [
            f"# {draft['title']}",
            "",
            draft["summary"],
            "",
            f"版本：{version}；需求版本：{candidate['brief_version']}；世界版本：{candidate['world_version']}",
            f"内容摘要：{candidate['content_hash']}",
            "",
            "## 节点",
            "",
        ]
        for node in draft["nodes"]:
            lines.extend(
                [
                    f"### {node['node_id']} · {node['title']}",
                    "",
                    node["text"],
                    "",
                    f"地点：{node['location_id']}",
                    f"角色：{', '.join(node['character_ids']) or '无'}",
                    f"道具：{', '.join(node['item_ids']) or '无'}",
                ]
            )
            for action in node["actions"]:
                conditions = json.dumps(action["conditions"], ensure_ascii=False)
                lines.append(
                    f"动作：{action['kind']} → {action['target_id']}；条件：{conditions}"
                )
            if node["effects"]:
                lines.append(f"效果：{'；'.join(node['effects'])}")
            if node["ending"]:
                lines.append(f"结局：{node['ending']}")
            lines.append("")
        lines.extend(["## 路径", ""])
        lines.extend(
            f"- {edge['source']} → {edge['target']}：{edge['label']}"
            for edge in draft["edges"]
        )
        lines.extend(["", "## 设定引用", ""])
        lines.extend(
            f"- {cite['lore_id']} ({cite['world_version']})"
            for cite in draft["citations"]
        )
        lines.extend(
            [
                "",
                "## 检查与审核",
                "",
                "确定性检查：" + ("通过" if row["report"]["valid"] else "未通过"),
                "检查范围：结构、引用、图路径与已编码动作条件；对白语义和剧情质量需人工审核。",
                "人工审核：接受",
                f"审核理由：{row['review']['reason']}",
                f"审核时间：{row['review']['created_at']}",
                "",
            ]
        )
        return (
            "\n".join(lines),
            "text/markdown; charset=utf-8",
            f"quest-{candidate_id}-v{version}.md",
        )
