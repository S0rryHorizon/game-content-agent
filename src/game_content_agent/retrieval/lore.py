import hashlib
import json
import math
import sqlite3
from collections import Counter
from typing import Any

import numpy as np

from game_content_agent.config import Settings
from game_content_agent.domain.world import LoreEntry
from game_content_agent.runtime.budget import CostLedger


def cosine(a: list[float], b: list[float]) -> float:
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("embedding shape or values invalid")
    denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
    return float(np.dot(x, y) / denominator) if denominator else 0.0


def fake_vector(text: str) -> list[float]:
    """Deterministic character features, explicitly not a semantic embedding."""
    result = [0.0] * 256
    for token, count in Counter(text.lower()).items():
        if token.isspace():
            continue
        index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:2], "big") % 256
        result[index] += count
    return result


class LoreRetriever:
    def __init__(
        self,
        entries: list[LoreEntry],
        version: str,
        settings: Settings,
        mode: str,
        client: Any = None,
        ledger: CostLedger | None = None,
    ):
        if any(entry.world_version != version for entry in entries):
            raise ValueError("lore world version mismatch")
        self.entries, self.version, self.settings, self.mode = (
            entries,
            version,
            settings,
            mode,
        )
        self.client, self.ledger = client, ledger
        self.usage: dict[str, Any] = {
            "embedding_tokens": 0,
            "embedding_estimated_sgd": 0.0,
        }

    def _key(self, text: str) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.settings.openai_embedding_model, self.version, text],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    @staticmethod
    def _checked_vector(value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("embedding provider returned an empty vector")
        if any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            for number in value
        ):
            raise ValueError(
                "embedding provider returned non-finite or non-numeric values"
            )
        return [float(number) for number in value]

    async def vectors(self, texts: list[str]) -> dict[str, list[float]]:
        unique = list(dict.fromkeys(texts))
        if self.mode == "fake":
            return {text: fake_vector(text) for text in unique}
        assert self.ledger is not None and self.client is not None
        model = self.settings.openai_embedding_model
        path = self.settings.ledger_path.parent / "embeddings.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        cached_vectors: dict[str, list[float]] = {}
        db = sqlite3.connect(path)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
            )
            for text in unique:
                cached = db.execute(
                    "SELECT vector FROM vectors WHERE key=?", (self._key(text),)
                ).fetchone()
                if cached is not None:
                    cached_vectors[text] = self._checked_vector(json.loads(cached[0]))
            db.commit()
        finally:
            db.close()

        missing = [text for text in unique if text not in cached_vectors]
        if not missing:
            self._check_dimensions(cached_vectors)
            return cached_vectors

        # UTF-8 bytes plus per-input overhead are a conservative token bound.
        total_bound = sum(len(text.encode("utf-8")) + 1024 for text in missing)
        reservation = self.ledger.reserve(model, total_bound)
        response = await self.client.embeddings.create(model=model, input=missing)
        tokens = getattr(getattr(response, "usage", None), "total_tokens", None)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("embedding provider usage is missing or invalid")
        # The provider has reported usage even if a later vector is malformed.
        estimate = self.ledger.settle(reservation, model, tokens)
        self.usage["embedding_tokens"] += tokens
        self.usage["embedding_estimated_sgd"] += estimate
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != len(missing):
            raise ValueError("embedding provider returned an incomplete batch")
        fresh_vectors: dict[str, list[float]] = {}
        seen_indices: set[int] = set()
        for item in data:
            index = getattr(item, "index", None)
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(missing)
            ):
                raise ValueError("embedding provider returned an invalid index")
            if index in seen_indices:
                raise ValueError("embedding provider returned duplicate indices")
            seen_indices.add(index)
            fresh_vectors[missing[index]] = self._checked_vector(
                getattr(item, "embedding", None)
            )
        all_vectors = cached_vectors | fresh_vectors
        self._check_dimensions(all_vectors)
        db = sqlite3.connect(path)
        try:
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES (?,?)",
                    [
                        (self._key(text), json.dumps(vector))
                        for text, vector in fresh_vectors.items()
                    ],
                )
        finally:
            db.close()
        return all_vectors

    @staticmethod
    def _check_dimensions(vectors: dict[str, list[float]]) -> None:
        if len({len(vector) for vector in vectors.values()}) > 1:
            raise ValueError("embedding dimensions do not match")

    async def vector(self, text: str) -> list[float]:
        return (await self.vectors([text]))[text]

    async def search(self, query: str) -> dict[str, Any]:
        vectors = await self.vectors([query, *(entry.text for entry in self.entries)])
        query_vector = vectors[query]
        hits = []
        for entry in self.entries:
            vector = vectors[entry.text]
            score = cosine(query_vector, vector)
            if score >= 0.12:
                hits.append({**entry.model_dump(mode="json"), "score": round(score, 5)})
        hits.sort(key=lambda hit: (-hit["score"], hit["lore_id"]))
        return {
            "entries": hits[:5],
            "world_version": self.version,
            "method": "scripted-character-vector"
            if self.mode == "fake"
            else "embedding-cosine",
            "empty": not hits,
        }
