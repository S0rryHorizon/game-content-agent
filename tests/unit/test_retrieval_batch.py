"""Batch embedding, cache, and budget checks using only in-process stubs."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_content_agent.config import Settings
from game_content_agent.domain.world import LoreEntry
from game_content_agent.retrieval.lore import LoreRetriever
from game_content_agent.runtime.budget import CostLedger


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        cost_ledger_path=str(tmp_path / "costs.db"),
        openai_embedding_model="stub-embedding",
        price_embedding_usd_per_million=1,
        usd_to_sgd=1,
    )


def _entry() -> LoreEntry:
    return LoreEntry(
        lore_id="lore-one",
        world_version="v1",
        text="蓝盐钥匙开启北潮门",
        tags=["rule"],
        source="rules.gate_requires_key",
    )


class EmbeddingsStub:
    def __init__(self, response_factory=None):
        self.calls: list[list[str]] = []
        self.response_factory = response_factory

    async def create(self, *, model: str, input: list[str]):
        assert model == "stub-embedding"
        self.calls.append(input)
        if self.response_factory is not None:
            return self.response_factory(input)
        # Reverse provider order to prove results are mapped using each index.
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1), 1.0])
            for index in reversed(range(len(input)))
        ]
        return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=31))


def test_cold_retrieval_batches_all_texts_and_warm_retrieval_embeds_only_query(
    tmp_path,
):
    settings = _settings(tmp_path)
    ledger = CostLedger(settings)
    embeddings = EmbeddingsStub()
    client = SimpleNamespace(embeddings=embeddings)
    lore_file = settings.project_root / "data/world/v1/lore_entries.jsonl"
    entries = [
        LoreEntry.model_validate_json(line)
        for line in lore_file.read_text().splitlines()
        if line.strip()
    ]
    assert len(entries) == 7
    retriever = LoreRetriever(entries, "v1", settings, "live", client, ledger)
    try:
        first = asyncio.run(retriever.search("第一次查询"))
        assert first["method"] == "embedding-cosine"
        assert len(embeddings.calls) == 1
        assert embeddings.calls[0] == ["第一次查询", *(entry.text for entry in entries)]
        vectors = asyncio.run(retriever.vectors(embeddings.calls[0]))
        assert vectors["第一次查询"] == [1.0, 1.0]
        assert vectors[entries[0].text] == [2.0, 1.0]
        assert len(embeddings.calls) == 1
        assert retriever.usage["embedding_tokens"] == 31
        assert retriever.usage["embedding_estimated_sgd"] == pytest.approx(
            31 / 1_000_000
        )

        warm = LoreRetriever(entries, "v1", settings, "live", client, ledger)
        asyncio.run(warm.search("第二次查询"))
        assert embeddings.calls[1] == ["第二次查询"]
        assert len(embeddings.calls) == 2
        assert warm.usage["embedding_tokens"] == 31
        asyncio.run(warm.search("第二次查询"))
        assert len(embeddings.calls) == 2
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "bad_case",
    [
        "index",
        "duplicate_index",
        "empty",
        "non_finite",
        "dimension",
        "missing_usage",
        "bad_usage",
    ],
)
def test_invalid_batch_fails_closed_and_keeps_reservation(tmp_path, bad_case):
    def response_factory(inputs):
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1), 1.0])
            for index in range(len(inputs))
        ]
        usage = SimpleNamespace(total_tokens=13)
        if bad_case == "index":
            data[0].index = len(inputs)
        elif bad_case == "duplicate_index":
            data[1].index = 0
        elif bad_case == "empty":
            data[0].embedding = []
        elif bad_case == "non_finite":
            data[0].embedding = [float("nan"), 1.0]
        elif bad_case == "dimension":
            data[0].embedding = [1.0]
        elif bad_case == "missing_usage":
            usage = None
        elif bad_case == "bad_usage":
            usage = SimpleNamespace(total_tokens=-1)
        return SimpleNamespace(data=data, usage=usage)

    settings = _settings(tmp_path)
    ledger = CostLedger(settings)
    embeddings = EmbeddingsStub(response_factory)
    retriever = LoreRetriever(
        [_entry()],
        "v1",
        settings,
        "live",
        SimpleNamespace(embeddings=embeddings),
        ledger,
    )
    try:
        with pytest.raises(ValueError):
            asyncio.run(retriever.search("查询"))
        assert len(embeddings.calls) == 1
        assert ledger.total() > 0
        with sqlite3.connect(settings.ledger_path) as db:
            status = db.execute("SELECT status FROM costs").fetchone()[0]
        if bad_case in {"missing_usage", "bad_usage"}:
            assert status == "reserved"
            assert retriever.usage["embedding_tokens"] == 0
        else:
            assert status == "usage_estimate"
            assert retriever.usage["embedding_tokens"] == 13
    finally:
        ledger.close()
