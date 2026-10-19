"""API and durable transaction checks with a substitute runtime, never live calls."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from game_content_agent.config import Settings
from game_content_agent.infrastructure.repository import Repository, RepositoryError
from game_content_agent.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def _finish(client: TestClient, run_id: str) -> dict:
    for _ in range(100):
        result = client.get(f"/runs/{run_id}").json()
        if result["status"] != "running":
            return result
        time.sleep(0.01)
    pytest.fail("background run did not finish")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from game_content_agent.runtime import engine

    async def fake_execute(**kwargs):
        kwargs["event_sink"](
            {
                "kind": "tool_result",
                "tool_name": "read_state",
                "observation": "state unchanged",
            }
        )
        instruction = kwargs["instruction"]
        if kwargs["purpose"] == "prepare_brief":
            return {
                "status": "succeeded",
                "termination_reason": "brief_prepared",
                "message": "摘要待确认",
                "brief": {
                    "version": 99,
                    "original_input": instruction,
                    "goal": "寻找蓝盐钥匙并开启北潮门",
                    "must_keep": [],
                    "constraints": {},
                    "confirmed": False,
                },
                "draft": None,
                "report": None,
                "decision": None,
                "usage": {},
            }
        example = "revision.json" if kwargs["purpose"] == "revise" else "normal.json"
        return {
            "status": "succeeded",
            "termination_reason": "completed",
            "message": "候选已生成",
            "brief": None,
            "draft": json.loads((ROOT / "data/examples" / example).read_text()),
            "report": None,
            "decision": None,
            "usage": {},
        }

    monkeypatch.setattr(engine, "execute", fake_execute)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}", live_api_enabled=False
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _create_approved_brief(client: TestClient) -> tuple[str, dict]:
    session = client.post("/sessions", json={}).json()
    session_id = session["session_id"]
    run = client.post(
        f"/sessions/{session_id}/runs",
        json={"purpose": "prepare_brief", "instruction": "请设计寻找蓝盐钥匙的任务"},
    )
    assert run.status_code == 202
    finished = _finish(client, run.json()["run_id"])
    assert finished["status"] == "succeeded"
    brief = finished["result"]["brief"]
    assert brief["version"] == 1 and not brief["confirmed"]
    confirm = client.post(f"/sessions/{session_id}/brief/confirm", json={"version": 1})
    assert confirm.status_code == 200 and confirm.json()["brief"]["confirmed"]
    return session_id, session


def test_versioned_review_and_historical_export(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/world").json()["capabilities"]["live_ready"] is False
    session_id, initial = _create_approved_brief(client)
    run = client.post(
        f"/sessions/{session_id}/runs",
        json={"purpose": "generate", "instruction": "生成任务"},
    )
    assert run.status_code == 202
    finished = _finish(client, run.json()["run_id"])
    assert finished["status"] == "succeeded"
    candidate = finished["result"]["candidate"]
    assert candidate["version"] == 1
    assert finished["result"]["report"]["valid"]
    candidate_id = candidate["candidate_id"]
    assert (
        client.get(
            f"/candidates/{candidate_id}/versions/1/export?format=json"
        ).status_code
        == 403
    )
    review_url = f"/candidates/{candidate_id}/versions/1/review"
    body = {
        "decision": "accept",
        "reason": "人工核对完成",
        "content_hash": candidate["content_hash"],
    }
    accepted = client.post(review_url, json=body)
    assert accepted.status_code == 200
    assert client.post(review_url, json=body).json() == accepted.json()
    assert client.post(review_url, json=body | {"reason": "changed"}).status_code == 409
    assert (
        client.post(review_url, json=body | {"content_hash": "bad"}).status_code == 409
    )
    exported = client.get(f"/candidates/{candidate_id}/versions/1/export?format=json")
    assert exported.status_code == 200
    assert exported.json()["review"]["decision"] == "accept"
    markdown = client.get(
        f"/candidates/{candidate_id}/versions/1/export?format=markdown"
    )
    assert markdown.status_code == 200 and "检查范围" in markdown.text
    assert client.get(f"/runs/{run.json()['run_id']}/replay").json()["events"]

    revision = client.post(
        f"/sessions/{session_id}/runs",
        json={
            "purpose": "revise",
            "instruction": "保留结局，把第二个分支改成帮助米拉",
            "base_version": 1,
        },
    )
    assert revision.status_code == 202
    revised = _finish(client, revision.json()["run_id"])
    assert revised["status"] == "succeeded"
    assert revised["result"]["candidate"]["version"] == 2
    details = client.get(f"/candidates/{candidate_id}/versions/2").json()
    assert details["diff"]
    assert (
        client.get(
            f"/candidates/{candidate_id}/versions/1/export?format=json"
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/candidates/{candidate_id}/versions/2/export?format=json"
        ).status_code
        == 403
    )
    assert client.post(review_url, json=body).status_code == 200
    assert (
        client.get(f"/sessions/{session_id}").json()["state_snapshot"]
        == initial["state_snapshot"]
    )


def test_freshness_and_new_brief_block_old_review(client: TestClient):
    session_id, _ = _create_approved_brief(client)
    assert (
        client.post(
            f"/sessions/{session_id}/runs",
            json={"purpose": "revise", "instruction": "revise", "base_version": 1},
        ).status_code
        == 409
    )
    generate = client.post(
        f"/sessions/{session_id}/runs",
        json={"purpose": "generate", "instruction": "generate"},
    )
    candidate = _finish(client, generate.json()["run_id"])["result"]["candidate"]
    new_brief = client.post(
        f"/sessions/{session_id}/runs",
        json={"purpose": "prepare_brief", "instruction": "另一个目标"},
    )
    assert (
        _finish(client, new_brief.json()["run_id"])["result"]["brief"]["version"] == 2
    )
    response = client.post(
        f"/candidates/{candidate['candidate_id']}/versions/1/review",
        json={
            "decision": "accept",
            "reason": "人工复核通过",
            "content_hash": candidate["content_hash"],
        },
    )
    assert response.status_code == 409


def test_single_run_and_restart_recovery(tmp_path: Path):
    repo = Repository(tmp_path / "runtime.db")
    repo.acquire_instance_lock()
    repo.initialize("2026-09-17T00:00:00Z")
    other = Repository(tmp_path / "runtime.db")
    with pytest.raises(RepositoryError, match="已有运行中的服务实例"):
        other.acquire_instance_lock()
    repo.create_session("s1", "v1", {"untouched": True}, "2026-09-17T00:00:00Z")
    run = {
        "run_id": "r1",
        "session_id": "s1",
        "purpose": "prepare_brief",
        "instruction": "brief",
        "mode": "fake",
        "scenario": "normal",
        "base_version": None,
        "created_at": "2026-09-17T00:00:00Z",
    }
    repo.create_run(run)
    with pytest.raises(RepositoryError) as busy:
        repo.create_run(run | {"run_id": "r2"})
    assert busy.value.code == "run_busy"
    repo.release_instance_lock()
    other.acquire_instance_lock()
    other.initialize("2026-09-17T00:01:00Z")
    assert other.get_run("r1")["status"] == "interrupted"
    assert other.get_run("r1")["termination_reason"] == "server_restart"
    other.release_instance_lock()


def test_real_fake_prompt_and_request_boundaries(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'fake.db'}", live_api_enabled=False
    )
    with TestClient(create_app(settings)) as client:
        world = client.get("/api/world").json()
        prompt = world["fake_prompts"]["goal"]
        session = client.post("/sessions", json={}).json()
        sid = session["session_id"]
        assert (
            client.post(
                f"/sessions/{sid}/runs",
                json={"purpose": "generate", "instruction": "generate"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/sessions/{sid}/runs",
                json={
                    "purpose": "prepare_brief",
                    "instruction": prompt,
                    "scenario": "unknown",
                },
            ).status_code
            == 422
        )
        prepared = client.post(
            f"/sessions/{sid}/runs",
            json={"purpose": "prepare_brief", "instruction": prompt},
        )
        assert prepared.status_code == 202
        brief_run = _finish(client, prepared.json()["run_id"])
        assert brief_run["status"] == "succeeded"
        assert brief_run["result"]["brief"]["original_input"] == prompt
        assert (
            client.post(
                f"/sessions/{sid}/brief/confirm", json={"version": True}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/sessions/{sid}/brief/confirm", json={"version": 1}
            ).status_code
            == 200
        )
        generated = client.post(
            f"/sessions/{sid}/runs",
            json={"purpose": "generate", "instruction": "生成任务"},
        )
        assert generated.status_code == 202
        candidate_run = _finish(client, generated.json()["run_id"])
        assert candidate_run["status"] == "succeeded"
        assert candidate_run["result"]["report"]["valid"]
        assert (
            client.post(
                f"/sessions/{sid}/runs",
                json={
                    "purpose": "revise",
                    "instruction": world["fake_prompts"]["revision"],
                    "base_version": True,
                },
            ).status_code
            == 422
        )
        candidate = candidate_run["result"]["candidate"]
        assert (
            client.post(
                f"/candidates/{candidate['candidate_id']}/versions/1/review",
                json={
                    "decision": "accept",
                    "reason": "   ",
                    "content_hash": candidate["content_hash"],
                },
            ).status_code
            == 422
        )
        assert (
            client.get(f"/sessions/{sid}").json()["state_snapshot"]
            == session["state_snapshot"]
        )


def test_public_inputs_and_events_are_redacted(tmp_path: Path):
    from game_content_agent.runtime.gateways import FAKE_GOAL

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'redacted.db'}", live_api_enabled=False
    )
    secret = "sk-test-secret-1234567890"
    with TestClient(create_app(settings)) as client:
        sid = client.post("/sessions", json={}).json()["session_id"]
        response = client.post(
            f"/sessions/{sid}/runs",
            json={"purpose": "prepare_brief", "instruction": FAKE_GOAL + " " + secret},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        run = _finish(client, run_id)
        assert run["status"] == "needs_decision"
        assert secret not in json.dumps(run, ensure_ascii=False)
        assert secret not in json.dumps(
            client.get(f"/runs/{run_id}/replay").json(), ensure_ascii=False
        )
    assert secret.encode() not in (tmp_path / "redacted.db").read_bytes()
