"""A failed review write must leave the candidate, report, and state untouched."""

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from game_content_agent.config import Settings
from game_content_agent.web.app import create_app


def _finish(client: TestClient, run_id: str) -> dict:
    for _ in range(100):
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("Fake run did not finish")


def _stored_rows(database: Path, candidate_id: str, version: int) -> tuple:
    """Read stored bytes, so a partial report rewrite cannot hide behind API output."""
    with sqlite3.connect(database) as db:
        state = db.execute("SELECT state_json FROM sessions").fetchone()
        candidate = db.execute(
            "SELECT payload_json, report_json FROM candidates "
            "WHERE candidate_id=? AND version=?",
            (candidate_id, version),
        ).fetchone()
        review_count = db.execute(
            "SELECT COUNT(*) FROM reviews WHERE candidate_id=? AND version=?",
            (candidate_id, version),
        ).fetchone()
        version_count = db.execute(
            "SELECT COUNT(*) FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
    assert state is not None and candidate is not None
    assert review_count is not None and version_count is not None
    return (
        state[0],
        candidate[0],
        candidate[1],
        review_count[0],
        version_count[0],
    )


def test_review_write_failure_rolls_back_and_hash_conflict_is_atomic(
    tmp_path: Path,
) -> None:
    database = tmp_path / "review-atomicity.db"
    settings = Settings(database_url=f"sqlite:///{database}", live_api_enabled=False)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        world = client.get("/api/world").json()
        session_response = client.post("/sessions", json={})
        assert session_response.status_code == 201
        session = session_response.json()
        session_id = session["session_id"]
        original_state = session["state_snapshot"]

        prepared = client.post(
            f"/sessions/{session_id}/runs",
            json={
                "purpose": "prepare_brief",
                "instruction": world["fake_prompts"]["goal"],
                "mode": "fake",
                "scenario": "normal",
            },
        )
        assert prepared.status_code == 202
        assert _finish(client, prepared.json()["run_id"])["status"] == "succeeded"
        confirmed = client.post(
            f"/sessions/{session_id}/brief/confirm", json={"version": 1}
        )
        assert confirmed.status_code == 200

        generated = client.post(
            f"/sessions/{session_id}/runs",
            json={
                "purpose": "generate",
                "instruction": world["fake_prompts"]["goal"],
                "mode": "fake",
                "scenario": "normal",
            },
        )
        assert generated.status_code == 202
        generation = _finish(client, generated.json()["run_id"])
        assert generation["status"] == "succeeded"
        candidate = generation["result"]["candidate"]
        assert generation["result"]["report"]["valid"]
        candidate_id = candidate["candidate_id"]
        v1_url = f"/candidates/{candidate_id}/versions/1"
        before_detail = client.get(v1_url).json()
        before_rows = _stored_rows(database, candidate_id, 1)
        assert before_rows[3:] == (0, 1)

        review_body = {
            "decision": "accept",
            "reason": "人工核对目标与路径",
            "content_hash": candidate["content_hash"],
        }
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TRIGGER fail_review_insert BEFORE INSERT ON reviews "
                "BEGIN SELECT RAISE(ABORT, 'injected review write failure'); END"
            )

        failed = client.post(f"{v1_url}/review", json=review_body)
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        assert _stored_rows(database, candidate_id, 1) == before_rows
        after_failure = client.get(v1_url).json()
        assert after_failure["candidate"] == before_detail["candidate"]
        assert after_failure["report"] == before_detail["report"]
        assert after_failure["review"] is None
        assert client.get(f"{v1_url}/export?format=json").status_code == 403
        assert (
            client.get(f"/sessions/{session_id}").json()["state_snapshot"]
            == original_state
        )

        with sqlite3.connect(database) as db:
            db.execute("DROP TRIGGER fail_review_insert")
        accepted = client.post(f"{v1_url}/review", json=review_body)
        assert accepted.status_code == 200
        assert accepted.json()["review"]["decision"] == "accept"
        repeated = client.post(f"{v1_url}/review", json=review_body)
        assert repeated.status_code == 200
        assert repeated.json() == accepted.json()
        after_accept = _stored_rows(database, candidate_id, 1)
        assert after_accept[:3] == before_rows[:3]
        assert after_accept[3:] == (1, 1)

        revision = client.post(
            f"/sessions/{session_id}/runs",
            json={
                "purpose": "revise",
                "instruction": world["fake_prompts"]["revision"],
                "mode": "fake",
                "scenario": "normal",
                "base_version": 1,
            },
        )
        assert revision.status_code == 202
        revised = _finish(client, revision.json()["run_id"])
        assert revised["status"] == "succeeded"
        v2_candidate = revised["result"]["candidate"]
        assert v2_candidate["version"] == 2
        assert v2_candidate["content_hash"] != candidate["content_hash"]
        v2_url = f"/candidates/{candidate_id}/versions/2"
        v2_before = _stored_rows(database, candidate_id, 2)
        wrong_hash = client.post(f"{v2_url}/review", json=review_body)
        assert wrong_hash.status_code == 409
        assert wrong_hash.json()["error"]["code"] == "hash_mismatch"
        assert _stored_rows(database, candidate_id, 2) == v2_before
        assert client.get(v2_url).json()["review"] is None
        assert (
            client.get(f"/sessions/{session_id}").json()["state_snapshot"]
            == original_state
        )
