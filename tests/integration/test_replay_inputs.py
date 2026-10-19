"""Public replay inputs survive execution, old traces, and process restarts."""

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from game_content_agent.config import Settings
from game_content_agent.infrastructure.repository import Repository
from game_content_agent.runtime.gateways import FAKE_GOAL, FAKE_REVISION
from game_content_agent.web.app import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'replay.db'}",
        cost_ledger_path=str(tmp_path / "costs.db"),
        live_api_enabled=False,
    )


def _run(client: TestClient, session_id: str, purpose: str, instruction: str, **extra):
    response = client.post(
        f"/sessions/{session_id}/runs",
        json={"purpose": purpose, "instruction": instruction, **extra},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(300):
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.005)
    pytest.fail("offline run did not finish")


def _replay(client: TestClient, run_id: str) -> list[dict]:
    response = client.get(f"/runs/{run_id}/replay")
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    return response.json()["events"]


def test_all_purposes_persist_public_start_input_after_restart(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        session = client.post("/sessions", json={}).json()
        sid = session["session_id"]
        initial_state = session["state_snapshot"]
        prepared = _run(client, sid, "prepare_brief", FAKE_GOAL)
        assert prepared["status"] == "succeeded"
        assert (
            client.post(
                f"/sessions/{sid}/brief/confirm", json={"version": 1}
            ).status_code
            == 200
        )
        generated = _run(client, sid, "generate", "生成任务")
        assert generated["status"] == "succeeded"
        revised = _run(client, sid, "revise", FAKE_REVISION, base_version=1)
        assert revised["status"] == "succeeded"
        expected = {
            prepared["run_id"]: FAKE_GOAL,
            generated["run_id"]: "生成任务",
            revised["run_id"]: FAKE_REVISION,
        }
        for run_id, instruction in expected.items():
            events = _replay(client, run_id)
            assert events[0]["kind"] == "start"
            assert events[0]["instruction"] == instruction
            assert not any(e["kind"] == "input_supplement" for e in events)
        assert client.get(f"/sessions/{sid}").json()["state_snapshot"] == initial_state

    with TestClient(create_app(settings)) as restarted:
        for run_id, instruction in expected.items():
            assert _replay(restarted, run_id)[0]["instruction"] == instruction
        assert (
            restarted.get(f"/sessions/{sid}").json()["state_snapshot"] == initial_state
        )


def test_old_start_and_early_exit_get_non_persistent_input_supplement(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        session = client.post("/sessions", json={}).json()
        sid = session["session_id"]
        initial_state = session["state_snapshot"]
        prepared = _run(client, sid, "prepare_brief", FAKE_GOAL)
        run_id = prepared["run_id"]
        database = tmp_path / "replay.db"
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT sequence,payload_json FROM events WHERE run_id=? "
                "AND json_extract(payload_json,'$.kind')='start'",
                (run_id,),
            ).fetchone()
            old_start = json.loads(row[1])
            del old_start["instruction"]
            db.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND sequence=?",
                (json.dumps(old_start, ensure_ascii=False), run_id, row[0]),
            )
            before = db.execute(
                "SELECT count(*) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        events = _replay(client, run_id)
        assert events[0]["kind"] == "input_supplement"
        assert events[0]["instruction"] == FAKE_GOAL
        assert "原始事件未修改" in events[0]["message"]
        assert events[1] == old_start
        with sqlite3.connect(database) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM events WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                == before
            )

        assert (
            client.post(
                f"/sessions/{sid}/brief/confirm", json={"version": 1}
            ).status_code
            == 200
        )
        early = _run(client, sid, "generate", "另一项未确认目标")
        assert early["status"] == "needs_decision"
        early_events = _replay(client, early["run_id"])
        assert early_events[0]["kind"] == "input_supplement"
        assert early_events[0]["instruction"] == "另一项未确认目标"
        assert not any(e["kind"] == "start" for e in early_events)
        assert early_events[1]["kind"] == "termination"
        assert client.get(f"/sessions/{sid}").json()["state_snapshot"] == initial_state

    # Configuration failures may finish before any runtime event is emitted.
    repository = Repository(tmp_path / "replay.db")
    repository.create_run(
        {
            "run_id": "configuration-blocked",
            "session_id": sid,
            "purpose": "prepare_brief",
            "instruction": "记录原始配置受阻输入",
            "mode": "fake",
            "scenario": "normal",
            "base_version": None,
            "created_at": "2026-09-19T00:00:00Z",
        }
    )
    repository.finish_run(
        "configuration-blocked",
        "failed",
        "configuration_error",
        "配置受阻",
        None,
        "2026-09-19T00:00:01Z",
    )
    with TestClient(create_app(settings)) as restarted:
        blocked = _replay(restarted, "configuration-blocked")
        assert len(blocked) == 1
        assert blocked[0]["kind"] == "input_supplement"
        assert blocked[0]["instruction"] == "记录原始配置受阻输入"
        assert (
            restarted.get(f"/sessions/{sid}").json()["state_snapshot"] == initial_state
        )


def test_public_replay_input_is_redacted_before_persistence(tmp_path: Path):
    settings = _settings(tmp_path)
    secret = "sk-test-secret-1234567890"
    with TestClient(create_app(settings)) as client:
        sid = client.post("/sessions", json={}).json()["session_id"]
        run = _run(client, sid, "prepare_brief", FAKE_GOAL + " " + secret)
        events = _replay(client, run["run_id"])
        assert events[0]["kind"] == "start"
        assert events[0]["instruction"] == FAKE_GOAL + " [REDACTED]"
        assert secret not in json.dumps(events, ensure_ascii=False)
    assert secret.encode() not in (tmp_path / "replay.db").read_bytes()
