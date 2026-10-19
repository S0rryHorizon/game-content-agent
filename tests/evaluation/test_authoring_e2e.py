"""Actual HTTP/service/runtime/tools/SQLite flow, with only the model scripted."""

import time

import pytest
from fastapi.testclient import TestClient

from game_content_agent.config import Settings
from game_content_agent.runtime.gateways import FAKE_CONFLICT, FAKE_GOAL, FAKE_REVISION
from game_content_agent.web.app import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        cost_ledger_path=str(tmp_path / "costs.db"),
        live_api_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def run(
    client,
    session,
    purpose,
    instruction=FAKE_GOAL,
    scenario="normal",
    base_version=None,
):
    response = client.post(
        f"/sessions/{session}/runs",
        json={
            "purpose": purpose,
            "instruction": instruction,
            "scenario": scenario,
            "base_version": base_version,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(300):
        record = client.get(f"/runs/{run_id}").json()
        if record["status"] != "running":
            return record
        time.sleep(0.005)
    pytest.fail("bounded local run failed to finish")


def prepared(client, goal=FAKE_GOAL):
    session = client.post("/sessions", json={}).json()
    sid = session["session_id"]
    first = run(client, sid, "prepare_brief", goal)
    assert first["status"] == "succeeded", first
    brief = first["result"]["brief"]
    assert not brief["confirmed"]
    confirmed = client.post(
        f"/sessions/{sid}/brief/confirm", json={"version": brief["version"]}
    )
    assert confirmed.status_code == 200
    return sid, session["state_snapshot"]


def test_actual_offline_authoring_review_revision_export_preserves_state(client):
    sid, initial = prepared(client)
    result = run(client, sid, "generate")
    assert result["status"] == "succeeded", result
    candidate = result["result"]["candidate"]
    prefix = f"/candidates/{candidate['candidate_id']}/versions/1"
    assert client.get(prefix + "/export?format=json").status_code == 403
    trace = client.get(f"/runs/{result['run_id']}/replay").json()["events"]
    assert [e["tool_name"] for e in trace if e["kind"] == "tool"] == [
        "read_state",
        "retrieve_lore",
        "validate_content",
    ]
    assert all(
        e["observation"]["status"] == "success" for e in trace if e["kind"] == "tool"
    )
    accepted = client.post(
        prefix + "/review",
        json={
            "decision": "accept",
            "reason": "人工检查目标与剧情，认可静态验证的范围。",
            "content_hash": candidate["content_hash"],
        },
    )
    assert accepted.status_code == 200, accepted.text
    exported = client.get(prefix + "/export?format=json")
    assert exported.status_code == 200
    assert exported.json()["candidate"] == candidate
    assert client.get(prefix + "/export?format=markdown").status_code == 200
    revision = run(client, sid, "revise", FAKE_REVISION, base_version=1)
    assert revision["status"] == "succeeded", revision
    second = revision["result"]["candidate"]
    assert second["version"] == 2 and second["parent_version"] == 1
    assert second["content_hash"] != candidate["content_hash"]
    current = f"/candidates/{candidate['candidate_id']}/versions/2"
    assert client.get(current).json()["diff"]
    assert client.get(current).json()["review"] is None
    assert client.get(current + "/export?format=json").status_code == 403
    assert client.get(prefix + "/export?format=json").status_code == 200
    assert (
        client.post(
            current + "/review",
            json={
                "decision": "reject",
                "reason": "暂不采用修订版",
                "content_hash": second["content_hash"],
            },
        ).status_code
        == 200
    )
    assert client.get(f"/sessions/{sid}").json()["state_snapshot"] == initial


@pytest.mark.parametrize(
    ("scenario", "status", "reason"),
    [
        ("invalid_entity", "needs_decision", "validation_failure"),
        ("invalid_citation", "needs_decision", "validation_failure"),
        ("invalid_graph", "needs_decision", "validation_failure"),
        ("missing_condition", "needs_decision", "validation_failure"),
        ("conflict", "needs_decision", "needs_decision"),
        ("tool_failure", "failed", "tool_failure"),
        ("loop_limit", "failed", "loop_limit"),
        ("provider_error", "failed", "provider_error"),
        ("repair_format", "succeeded", "candidate_ready"),
    ],
)
def test_scripted_failure_classification_through_real_stack(
    client, scenario, status, reason
):
    sid, initial = prepared(client)
    record = run(client, sid, "generate", scenario=scenario)
    assert record["status"] == status, record
    assert record["termination_reason"] == reason
    assert client.get(f"/sessions/{sid}").json()["state_snapshot"] == initial


def test_confirmed_impossible_goal_never_silently_changes(client):
    sid, initial = prepared(client, FAKE_CONFLICT)
    record = run(client, sid, "generate", FAKE_CONFLICT)
    assert record["status"] == "needs_decision"
    assert record["termination_reason"] == "core_conflict"
    session = client.get(f"/sessions/{sid}").json()
    assert session["brief"]["original_input"] == FAKE_CONFLICT
    assert session["candidate_id"] is None
    assert session["state_snapshot"] == initial


def test_fake_unknown_prompt_reports_limit_instead_of_inventing_understanding(client):
    sid = client.post("/sessions", json={}).json()["session_id"]
    record = run(client, sid, "prepare_brief", "写一个完全不同的故事")
    assert record["status"] == "needs_decision"
    assert "脚本" in record["message"]


@pytest.mark.parametrize("instruction", ["加入不存在的黄金钥匙并开门", FAKE_CONFLICT])
def test_direct_generate_instruction_cannot_replace_confirmed_goal(client, instruction):
    sid, initial = prepared(client)
    record = run(client, sid, "generate", instruction)
    assert record["status"] == "needs_decision"
    assert record["termination_reason"] == "instruction_mismatch"
    session = client.get(f"/sessions/{sid}").json()
    assert session["candidate_id"] is None
    assert session["state_snapshot"] == initial
