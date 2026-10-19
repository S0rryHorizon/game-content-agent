"""Native function-call protocol checks with in-process provider stubs only."""

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_content_agent.config import Settings
from game_content_agent.domain.design import BriefConstraints, DesignBrief, QuestDraft
from game_content_agent.domain.state import GameState
from game_content_agent.domain.world import LoreEntry, WorldSpecification
from game_content_agent.runtime import engine, gateways
from game_content_agent.runtime.budget import CostLedger
from game_content_agent.runtime.gateways import FAKE_GOAL, FAKE_REVISION


class OutputItem:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, mode, exclude_none):
        assert mode == "json" and exclude_none
        return self.payload


@pytest.mark.parametrize(
    ("purpose", "draft_file", "instruction"),
    [
        ("generate", "normal.json", FAKE_GOAL),
        ("generate", "normal.json", "生成任务"),
        ("revise", "revision.json", FAKE_REVISION),
    ],
)
def test_live_adapter_round_trips_three_native_tools_and_final_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    draft_file: str,
    instruction: str,
) -> None:
    settings = Settings(
        cost_ledger_path=str(tmp_path / "costs.db"),
        openai_api_key="stub-only",
        openai_model="stub-model",
        openai_embedding_model="stub-embed",
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        price_embedding_usd_per_million=1,
        usd_to_sgd=1,
    )
    world_dir = settings.project_root / "data/world/v1"
    world = WorldSpecification.model_validate_json(
        (world_dir / "world.json").read_text()
    )
    state = GameState.model_validate_json(
        (world_dir / "initial_state.json").read_text()
    )
    state_before = state.model_dump_json()
    lore = [
        LoreEntry.model_validate_json(line)
        for line in (world_dir / "lore_entries.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rules = json.loads((world_dir / "rules.json").read_text())
    draft = json.loads(
        (settings.project_root / "data/examples" / draft_file).read_text()
    )
    normal = QuestDraft.model_validate_json(
        (settings.project_root / "data/examples/normal.json").read_text()
    )
    requests = []
    embedding_calls = []

    class StubResponses:
        async def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs["input"]))
            step = len(requests)
            if step <= 3:
                name, arguments = [
                    ("read_state", {}),
                    ("retrieve_lore", {"query": "米拉 北潮门 蓝盐钥匙"}),
                    ("validate_content", {"draft": draft}),
                ][step - 1]
                output = [
                    OutputItem(
                        {
                            "type": "function_call",
                            "name": name,
                            "call_id": f"call-{step}",
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        }
                    )
                ]
                output_text = ""
            else:
                output = []
                output_text = json.dumps(
                    {"kind": "draft", "draft": draft}, ensure_ascii=False
                )
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                output=output,
                status="completed",
                output_text=output_text,
            )

    class StubEmbeddings:
        async def create(self, **kwargs):
            embedding_calls.append(kwargs)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[1.0, float(index + 1)])
                    for index in range(len(kwargs["input"]))
                ],
                usage=SimpleNamespace(total_tokens=31),
            )

    class StubClient:
        responses = StubResponses()
        embeddings = StubEmbeddings()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []
    result = asyncio.run(
        engine.execute(
            purpose=purpose,
            instruction=instruction,
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=DesignBrief(
                version=1,
                original_input=FAKE_GOAL,
                goal=FAKE_GOAL,
                constraints=BriefConstraints(),
                confirmed=True,
            ),
            previous=normal if purpose == "revise" else None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["status"] == "succeeded", result
    assert result["draft"] == draft
    assert len(requests) == 4
    assert len(embedding_calls) == 1
    assert len(embedding_calls[0]["input"]) == len(lore) + 1
    for step in range(1, 4):
        assert any(
            item.get("type") == "function_call_output"
            and item.get("call_id") == f"call-{step}"
            and json.loads(item["output"])["status"] == "success"
            for item in requests[step]
        )
    assert [event["tool_name"] for event in events if event["kind"] == "tool"] == [
        "read_state",
        "retrieve_lore",
        "validate_content",
    ]
    assert result["usage"]["input_tokens"] == 40
    assert result["usage"]["output_tokens"] == 8
    assert result["usage"]["embedding_tokens"] == 31
    assert result["usage"]["estimated_sgd"] > 0
    assert result["usage"]["unknown_reserved"] is False
    assert result["usage"]["unsettled_reserved_sgd"] == 0
    assert state.model_dump_json() == state_before


def _live_context(tmp_path: Path):
    settings = Settings(
        cost_ledger_path=str(tmp_path / "costs.db"),
        openai_api_key="stub-only",
        openai_model="stub-model",
        openai_embedding_model="stub-embed",
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        price_embedding_usd_per_million=1,
        usd_to_sgd=1,
        model_timeout_seconds=0.01,
        run_timeout_seconds=0.2,
    )
    folder = settings.project_root / "data/world/v1"
    return (
        settings,
        WorldSpecification.model_validate_json((folder / "world.json").read_text()),
        GameState.model_validate_json((folder / "initial_state.json").read_text()),
        [
            LoreEntry.model_validate_json(line)
            for line in (folder / "lore_entries.jsonl").read_text().splitlines()
            if line.strip()
        ],
        json.loads((folder / "rules.json").read_text()),
    )


@pytest.mark.parametrize("mode", ["fake", "live"])
def test_changed_generate_instruction_stops_before_any_provider_or_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)
    original_state = state.model_dump_json()
    provider_calls = []

    def unexpected_provider(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("mismatched instruction reached the provider")

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", unexpected_provider)
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="generate",
            instruction="把结局改为沉船",
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=DesignBrief(
                version=1,
                original_input=FAKE_GOAL,
                goal=FAKE_GOAL,
                constraints=BriefConstraints(),
                confirmed=True,
            ),
            previous=None,
            mode=mode,
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["status"] == "needs_decision"
    assert result["termination_reason"] == "instruction_mismatch"
    assert result["draft"] is None and result["decision"]["suggestions"]
    assert result["usage"]["unsettled_reservation_count"] == 0
    assert not provider_calls and not settings.ledger_path.exists()
    assert [event["kind"] for event in events] == ["termination"]
    assert state.model_dump_json() == original_state


@pytest.mark.parametrize(
    ("provider_usage", "detail"),
    [
        ("missing_attribute", "缺失"),
        (None, "缺失"),
        (SimpleNamespace(input_tokens=1), "缺失"),
        (SimpleNamespace(output_tokens=1), "缺失"),
        (SimpleNamespace(input_tokens=None, output_tokens=1), "缺失"),
        (SimpleNamespace(input_tokens="1", output_tokens=1), "无效"),
        (SimpleNamespace(input_tokens=1.5, output_tokens=1), "无效"),
        (SimpleNamespace(input_tokens=True, output_tokens=1), "无效"),
        (SimpleNamespace(input_tokens=1, output_tokens=False), "无效"),
        (SimpleNamespace(input_tokens=-1, output_tokens=1), "无效"),
        (SimpleNamespace(input_tokens=1, output_tokens=-1), "无效"),
    ],
)
def test_invalid_provider_usage_is_single_request_and_keeps_unknown_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_usage,
    detail: str,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)
    original_state = state.model_dump_json()
    requests = []

    class StubResponses:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                **(
                    {}
                    if provider_usage == "missing_attribute"
                    else {"usage": provider_usage}
                ),
                output=[],
                status="completed",
                output_text="{}",
            )

    class StubClient:
        responses = StubResponses()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="prepare_brief",
            instruction=FAKE_GOAL,
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=None,
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["status"] == "failed"
    assert result["termination_reason"] == "provider_usage_error"
    assert detail in result["message"] and "不会自动重试" in result["message"]
    assert result["draft"] is None and result["brief"] is None
    assert len(requests) == 1
    assert [event["kind"] for event in events] == ["start", "termination"]
    usage = result["usage"]
    assert events[-1]["usage"] == usage
    assert usage["input_tokens"] == usage["output_tokens"] == 0
    assert usage["estimated_sgd"] == 0
    assert usage["unknown_reserved"] is True
    assert usage["unsettled_reservation_count"] == 1
    assert usage["unsettled_model_reserved_sgd"] > 0
    with sqlite3.connect(settings.ledger_path) as db:
        rows = db.execute("SELECT status,estimated,reserved FROM costs").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "reserved" and rows[0][1] is None
    assert rows[0][2] == pytest.approx(usage["unsettled_reserved_sgd"])
    assert state.model_dump_json() == original_state


@pytest.mark.parametrize("with_tool_call", [False, True])
@pytest.mark.parametrize(
    ("status", "has_usage", "expected_reason"),
    [
        ("incomplete", True, "provider_incomplete"),
        ("failed", True, "provider_error"),
        ("cancelled", True, "provider_error"),
        ("incomplete", False, "provider_usage_error"),
    ],
)
def test_noncompleted_response_never_repairs_or_executes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_tool_call: bool,
    status: str,
    has_usage: bool,
    expected_reason: str,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)
    original_state = state.model_dump_json()
    requests = []
    embedding_calls = []

    class StubResponses:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=2)
                if has_usage
                else None,
                status=status,
                output=[
                    OutputItem(
                        {
                            "type": "function_call",
                            "name": "retrieve_lore",
                            "call_id": "partial-call",
                            "arguments": json.dumps({"query": "北潮门"}),
                        }
                    )
                ]
                if with_tool_call
                else [],
                output_text="{",
            )

    class StubEmbeddings:
        async def create(self, **kwargs):
            embedding_calls.append(kwargs)
            raise AssertionError("partial output must not execute retrieval")

    class StubClient:
        responses = StubResponses()
        embeddings = StubEmbeddings()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="generate",
            instruction=FAKE_GOAL,
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=DesignBrief(
                version=1,
                original_input=FAKE_GOAL,
                goal=FAKE_GOAL,
                constraints=BriefConstraints(),
                confirmed=True,
            ),
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["status"] == "failed"
    assert result["termination_reason"] == expected_reason
    assert result["draft"] is None and result["report"] is None
    assert len(requests) == 1 and not embedding_calls
    assert [event["kind"] for event in events] == ["start", "termination"]
    usage = result["usage"]
    assert events[-1]["usage"] == usage
    with sqlite3.connect(settings.ledger_path) as db:
        rows = db.execute("SELECT status,estimated,reserved FROM costs").fetchall()
    assert len(rows) == 1
    if has_usage:
        assert usage["input_tokens"] == 10 and usage["output_tokens"] == 2
        assert usage["estimated_sgd"] == pytest.approx(14 / 1_000_000)
        assert usage["unknown_reserved"] is False
        assert usage["unsettled_reservation_count"] == 0
        assert rows[0][0] == "usage_estimate"
        assert rows[0][1] == pytest.approx(usage["estimated_sgd"])
    else:
        assert usage["input_tokens"] == usage["output_tokens"] == 0
        assert usage["estimated_sgd"] == 0
        assert usage["unknown_reserved"] is True
        assert usage["unsettled_reservation_count"] == 1
        assert rows[0][0] == "reserved" and rows[0][1] is None
        assert rows[0][2] == pytest.approx(usage["unsettled_reserved_sgd"])
    assert state.model_dump_json() == original_state


def test_model_timeout_reports_unknown_reservation_in_run_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)

    class SlowResponses:
        async def create(self, **_kwargs):
            await asyncio.sleep(0.1)

    class StubClient:
        responses = SlowResponses()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="prepare_brief",
            instruction=FAKE_GOAL,
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=None,
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    usage = result["usage"]
    assert result["termination_reason"] == "timeout"
    assert usage["input_tokens"] == usage["output_tokens"] == 0
    assert usage["unknown_reserved"] is True
    assert usage["unsettled_reservation_count"] == 1
    assert usage["unsettled_model_reserved_sgd"] > 0
    assert usage["unsettled_embedding_reserved_sgd"] == 0
    assert usage["estimated_sgd"] == 0
    assert "非账单" in usage["cost_basis"]
    assert events[-1]["usage"] == usage
    ledger = CostLedger(settings)
    try:
        assert ledger.total() == pytest.approx(usage["unsettled_reserved_sgd"])
    finally:
        ledger.close()


def test_interrupted_call_keeps_reservation_visible_in_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)
    started = asyncio.Event()

    class PausedResponses:
        async def create(self, **_kwargs):
            started.set()
            await asyncio.Future()

    class StubClient:
        responses = PausedResponses()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []

    async def exercise():
        task = asyncio.create_task(
            engine.execute(
                purpose="prepare_brief",
                instruction=FAKE_GOAL,
                world=world,
                lore_entries=lore,
                state=state,
                rules=rules,
                brief=None,
                previous=None,
                mode="live",
                scenario="normal",
                event_sink=events.append,
                settings=settings,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert events[-1]["kind"] == "interrupted"
    usage = events[-1]["usage"]
    assert usage["unknown_reserved"] is True
    assert usage["unsettled_model_reserved_sgd"] > 0
    assert usage["input_tokens"] == 0
    ledger = CostLedger(settings)
    try:
        assert ledger.total() == pytest.approx(usage["unsettled_reserved_sgd"])
    finally:
        ledger.close()


@pytest.mark.parametrize("embedding_case", ["bad_vector", "missing_usage"])
def test_embedding_failure_reports_known_or_unknown_cost_in_run_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedding_case: str,
) -> None:
    settings, world, state, lore, rules = _live_context(tmp_path)
    settings = settings.model_copy(
        update={"model_timeout_seconds": 1.0, "run_timeout_seconds": 2.0}
    )

    class StubResponses:
        def __init__(self):
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            name, arguments = [
                ("read_state", {}),
                ("retrieve_lore", {"query": "米拉 北潮门"}),
            ][self.calls - 1]
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                output=[
                    OutputItem(
                        {
                            "type": "function_call",
                            "name": name,
                            "call_id": f"call-{self.calls}",
                            "arguments": json.dumps(arguments),
                        }
                    )
                ],
                status="completed",
                output_text="",
            )

    class StubEmbeddings:
        async def create(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        index=index, embedding=[] if index == 0 else [1.0, 1.0]
                    )
                    for index in range(len(kwargs["input"]))
                ],
                usage=SimpleNamespace(total_tokens=99)
                if embedding_case == "bad_vector"
                else None,
            )

    class StubClient:
        responses = StubResponses()
        embeddings = StubEmbeddings()

        async def close(self):
            pass

    monkeypatch.setattr(engine, "live_readiness", lambda _settings: (True, "stub only"))
    monkeypatch.setattr(gateways, "AsyncOpenAI", lambda **_kwargs: StubClient())
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="generate",
            instruction=FAKE_GOAL,
            world=world,
            lore_entries=lore,
            state=state,
            rules=rules,
            brief=DesignBrief(
                version=1,
                original_input=FAKE_GOAL,
                goal=FAKE_GOAL,
                constraints=BriefConstraints(),
                confirmed=True,
            ),
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    usage = result["usage"]
    assert result["termination_reason"] == "tool_failure"
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 4
    assert usage["model_estimated_sgd"] == pytest.approx(28 / 1_000_000)
    if embedding_case == "bad_vector":
        assert usage["embedding_tokens"] == 99
        assert usage["embedding_estimated_sgd"] == pytest.approx(99 / 1_000_000)
        assert usage["estimated_sgd"] == pytest.approx(127 / 1_000_000)
        assert usage["unknown_reserved"] is False
        assert usage["unsettled_reserved_sgd"] == 0
    else:
        assert usage["embedding_tokens"] == 0
        assert usage["estimated_sgd"] == pytest.approx(28 / 1_000_000)
        assert usage["unknown_reserved"] is True
        assert usage["unsettled_embedding_reserved_sgd"] > 0
    assert events[-1]["usage"] == usage
    ledger = CostLedger(settings)
    try:
        assert ledger.total() == pytest.approx(
            usage["estimated_sgd"] + usage["unsettled_reserved_sgd"]
        )
    finally:
        ledger.close()
