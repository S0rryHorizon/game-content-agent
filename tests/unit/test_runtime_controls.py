import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from game_content_agent.config import Settings, live_readiness
from game_content_agent.domain.design import BriefConstraints, DesignBrief, QuestDraft
from game_content_agent.domain.state import GameState
from game_content_agent.domain.world import LoreEntry, WorldSpecification
from game_content_agent.retrieval.lore import cosine, fake_vector
from game_content_agent.runtime import engine, gateways
from game_content_agent.runtime.budget import BudgetExceeded, CostLedger
from game_content_agent.runtime.gateways import FAKE_GOAL, FakeGateway, strict_schema
from game_content_agent.security import sanitize


def test_redaction_recursive_but_usage_counts_remain():
    value = {
        "api_key": "private",
        "input_tokens": 45,
        "nested": ["secret-value", "Bearer abc123", "sk-abcdef01234567890"],
    }
    result = sanitize(value, secrets=("secret-value",))
    assert result["api_key"] == "[REDACTED]"
    assert result["input_tokens"] == 45
    assert "secret-value" not in str(result) and "abcdef012" not in str(result)


def test_live_requires_enabled_key_models_and_verified_prices():
    assert not live_readiness(Settings(live_api_enabled=False))[0]
    config = Settings(
        live_api_enabled=True,
        openai_api_key="test",
        openai_embedding_model="embed",
        price_model="gpt-5.6-terra",
        price_embedding_model="embed",
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        price_embedding_usd_per_million=1,
        usd_to_sgd=1.3,
        pricing_verified_on=datetime.now(UTC).date().isoformat(),
        pricing_source="https://developers.openai.com/api/docs/pricing",
    )
    assert live_readiness(config)[0]
    assert not live_readiness(config.model_copy(update={"price_model": "other"}))[0]


def test_conservative_budget_reservation_survives_restart(tmp_path):
    settings = Settings(
        cost_ledger_path=str(tmp_path / "cost.db"),
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        usd_to_sgd=1,
        openai_model="test",
        mvp_api_budget_sgd=0.01,
        api_warning_sgd=0.008,
    )
    ledger = CostLedger(settings)
    ledger.reserve("test", 5000)
    assert ledger.total() == 0.005
    ledger.close()
    recovered = CostLedger(settings)
    with pytest.raises(BudgetExceeded):
        recovered.reserve("test", 6000)
    assert recovered.total() == 0.005
    recovered.close()


def test_vectors_are_deterministic_and_cosine_handles_zero():
    assert fake_vector("钥匙") == fake_vector("钥匙")
    assert cosine([1, 0], [1, 0]) == 1
    assert cosine([0, 0], [1, 0]) == 0
    with pytest.raises(ValueError):
        cosine([1], [1, 2])


def test_strict_provider_schema_requires_nested_fields():
    schema = strict_schema(QuestDraft.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "default" not in json.dumps(schema)


def test_model_timeout_is_bounded_without_changing_state(monkeypatch):
    settings = Settings(model_timeout_seconds=0.001, run_timeout_seconds=0.2)
    folder = settings.project_root / "data/world/v1"
    world = WorldSpecification.model_validate_json((folder / "world.json").read_text())
    state = GameState.model_validate_json((folder / "initial_state.json").read_text())
    original = state.model_dump_json()
    lore = [
        LoreEntry.model_validate_json(line)
        for line in (folder / "lore_entries.jsonl").read_text().splitlines()
    ]

    async def slow(self):
        await asyncio.sleep(0.2)
        return {}

    monkeypatch.setattr(FakeGateway, "next_turn", slow)
    events = []
    result = asyncio.run(
        engine.execute(
            purpose="generate",
            instruction=FAKE_GOAL,
            world=world,
            lore_entries=lore,
            state=state,
            rules=json.loads((folder / "rules.json").read_text()),
            brief=DesignBrief(
                version=1,
                original_input=FAKE_GOAL,
                goal=FAKE_GOAL,
                constraints=BriefConstraints(),
                confirmed=True,
            ),
            previous=None,
            mode="fake",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["termination_reason"] == "timeout"
    assert state.model_dump_json() == original


@pytest.mark.parametrize("valid_response", [False, True])
def test_paid_usage_is_recorded_once_with_or_without_parse_failure(
    tmp_path,
    monkeypatch,
    valid_response,
):
    settings = Settings(
        cost_ledger_path=str(tmp_path / "costs.db"),
        openai_api_key="stub-key",
        openai_model="stub-model",
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        usd_to_sgd=1,
        max_candidate_repairs=0,
    )
    folder = settings.project_root / "data/world/v1"
    world = WorldSpecification.model_validate_json((folder / "world.json").read_text())
    state = GameState.model_validate_json((folder / "initial_state.json").read_text())
    lore = [
        LoreEntry.model_validate_json(line)
        for line in (folder / "lore_entries.jsonl").read_text().splitlines()
    ]
    calls = []
    output_text = (
        json.dumps(
            {
                "kind": "brief",
                "brief": {
                    "version": 1,
                    "original_input": FAKE_GOAL,
                    "goal": FAKE_GOAL,
                    "must_keep": [],
                    "constraints": {},
                    "confirmed": False,
                },
            },
            ensure_ascii=False,
        )
        if valid_response
        else "{"
    )

    class StubResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=11, output_tokens=3),
                output=[],
                status="completed",
                output_text=output_text,
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
            rules=json.loads((folder / "rules.json").read_text()),
            brief=None,
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    expected = 17 / 1_000_000
    assert len(calls) == 1
    assert result["status"] == ("succeeded" if valid_response else "failed")
    assert result["termination_reason"] == (
        "brief_prepared" if valid_response else "repair_limit"
    )
    assert result["usage"]["input_tokens"] == 11
    assert result["usage"]["output_tokens"] == 3
    assert result["usage"]["estimated_sgd"] == pytest.approx(expected)
    assert events[-1]["usage"]["estimated_sgd"] == pytest.approx(expected)
    with sqlite3.connect(settings.ledger_path) as db:
        row = db.execute("SELECT status,estimated FROM costs").fetchone()
    assert row[0] == "usage_estimate"
    assert row[1] == pytest.approx(expected)


def test_paid_format_error_can_repair_with_known_usage_and_bounded_second_call(
    tmp_path, monkeypatch
):
    settings = Settings(
        cost_ledger_path=str(tmp_path / "costs.db"),
        openai_api_key="stub-key",
        openai_model="stub-model",
        price_input_usd_per_million=1,
        price_output_usd_per_million=2,
        usd_to_sgd=1,
        max_candidate_repairs=1,
    )
    folder = settings.project_root / "data/world/v1"
    world = WorldSpecification.model_validate_json((folder / "world.json").read_text())
    state = GameState.model_validate_json((folder / "initial_state.json").read_text())
    original_state = state.model_dump_json()
    lore = [
        LoreEntry.model_validate_json(line)
        for line in (folder / "lore_entries.jsonl").read_text().splitlines()
    ]
    requests = []

    class StubResponses:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=11, output_tokens=3),
                output=[],
                status="completed",
                output_text="{"
                if len(requests) == 1
                else json.dumps(
                    {
                        "kind": "brief",
                        "brief": {
                            "version": 1,
                            "original_input": FAKE_GOAL,
                            "goal": FAKE_GOAL,
                            "must_keep": [],
                            "constraints": {},
                            "confirmed": False,
                        },
                    },
                    ensure_ascii=False,
                ),
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
            rules=json.loads((folder / "rules.json").read_text()),
            brief=None,
            previous=None,
            mode="live",
            scenario="normal",
            event_sink=events.append,
            settings=settings,
        )
    )
    assert result["status"] == "succeeded"
    assert result["termination_reason"] == "brief_prepared"
    assert len(requests) == 2
    assert [event["kind"] for event in events] == [
        "start",
        "format_repair",
        "termination",
    ]
    assert result["usage"]["input_tokens"] == 22
    assert result["usage"]["output_tokens"] == 6
    assert result["usage"]["estimated_sgd"] == pytest.approx(34 / 1_000_000)
    assert result["usage"]["unsettled_reservation_count"] == 0
    with sqlite3.connect(settings.ledger_path) as db:
        rows = db.execute("SELECT status,estimated FROM costs").fetchall()
    assert len(rows) == 2
    assert all(row[0] == "usage_estimate" for row in rows)
    assert sum(row[1] for row in rows) == pytest.approx(34 / 1_000_000)
    assert state.model_dump_json() == original_state
