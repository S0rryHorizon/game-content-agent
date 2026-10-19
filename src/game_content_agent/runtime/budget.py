"""Durable conservative reservations; estimates are never presented as invoices."""

import sqlite3
import uuid
from datetime import UTC, datetime

from game_content_agent.config import Settings


class BudgetExceeded(RuntimeError):
    pass


class CostLedger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._issued_ids: list[str] = []
        settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.ledger_path, timeout=10)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS costs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "model TEXT NOT NULL, reserved REAL NOT NULL, estimated REAL, status TEXT NOT NULL)"
        )
        self.db.commit()

    def total(self) -> float:
        return float(
            self.db.execute(
                "SELECT COALESCE(SUM(COALESCE(estimated,reserved)),0) FROM costs"
            ).fetchone()[0]
        )

    def reserve(self, model: str, input_bound: int, output_bound: int = 0) -> str:
        s = self.settings
        input_rate = (
            s.price_embedding_usd_per_million
            if model == s.openai_embedding_model
            else s.price_input_usd_per_million
        )
        amount = (
            (input_bound * input_rate + output_bound * s.price_output_usd_per_million)
            * s.usd_to_sgd
            / 1_000_000
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            spent = self.total()
            if (
                spent >= min(s.api_warning_sgd, s.mvp_api_budget_sgd)
                or spent + amount > s.mvp_api_budget_sgd
            ):
                raise BudgetExceeded("累计用量估算或预留已触及预算门槛；停止真实请求。")
            key = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO costs VALUES (?,?,?,?,NULL,'reserved')",
                (key, datetime.now(UTC).isoformat(), model, amount),
            )
            self.db.commit()
            self._issued_ids.append(key)
            return key
        except Exception:
            self.db.rollback()
            raise

    def settle(
        self, key: str, model: str, input_tokens: int, output_tokens: int = 0
    ) -> float:
        s = self.settings
        rate = (
            s.price_embedding_usd_per_million
            if model == s.openai_embedding_model
            else s.price_input_usd_per_million
        )
        estimate = (
            (input_tokens * rate + output_tokens * s.price_output_usd_per_million)
            * s.usd_to_sgd
            / 1_000_000
        )
        with self.db:
            self.db.execute(
                "UPDATE costs SET estimated=?,status='usage_estimate' WHERE id=?",
                (estimate, key),
            )
        return estimate

    def run_snapshot(self) -> dict[str, float | int | bool]:
        """Summarize only reservations issued by this run's ledger instance."""
        summary: dict[str, float | int | bool] = {
            "unknown_reserved": False,
            "unsettled_reservation_count": 0,
            "unsettled_reserved_sgd": 0.0,
            "unsettled_model_reserved_sgd": 0.0,
            "unsettled_embedding_reserved_sgd": 0.0,
            "settled_estimated_sgd": 0.0,
        }
        if not self._issued_ids:
            return summary
        placeholders = ",".join("?" for _ in self._issued_ids)
        rows = self.db.execute(
            f"SELECT model,reserved,estimated,status FROM costs WHERE id IN ({placeholders})",
            self._issued_ids,
        ).fetchall()
        for model, reserved, estimated, status in rows:
            if status == "reserved":
                summary["unsettled_reservation_count"] += 1
                summary["unsettled_reserved_sgd"] += float(reserved)
                field = (
                    "unsettled_embedding_reserved_sgd"
                    if model == self.settings.openai_embedding_model
                    else "unsettled_model_reserved_sgd"
                )
                summary[field] += float(reserved)
            elif status == "usage_estimate" and estimated is not None:
                summary["settled_estimated_sgd"] += float(estimated)
        summary["unknown_reserved"] = summary["unsettled_reservation_count"] > 0
        return summary

    def close(self) -> None:
        self.db.close()
