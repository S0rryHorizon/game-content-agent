"""Aggregate reads must keep one snapshot across concurrent commits."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from game_content_agent.infrastructure.repository import Repository


@pytest.mark.parametrize("aggregate", ["session", "events"])
def test_aggregate_read_keeps_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: str
) -> None:
    repo = Repository(tmp_path / "snapshot.db")
    repo.initialize("before")
    # WAL lets the writer commit while the reader's snapshot remains open.
    with repo.read() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    repo.create_session("s1", "world", {}, "before")
    repo.create_run(
        {
            "run_id": "r1",
            "session_id": "s1",
            "purpose": "prepare_brief",
            "instruction": "brief",
            "mode": "fake",
            "scenario": "normal",
            "base_version": None,
            "created_at": "before",
        }
    )
    repo.append_event("r1", {"kind": "start"})
    writer = Repository(repo.path)

    def read_aggregate():
        return (
            repo.get_session("s1") if aggregate == "session" else repo.get_events("r1")
        )

    before = read_aggregate()
    original_read = repo.read
    committed = False

    @contextmanager
    def interleaved_read():
        with original_read() as db:
            selects = 0

            def before_query(sql: str) -> None:
                nonlocal selects, committed
                if not sql.startswith("SELECT"):
                    return
                selects += 1
                if selects != 2:
                    return
                writer.finish_run(
                    "r1",
                    "succeeded",
                    "completed",
                    "done",
                    {},
                    "after",
                    brief={"version": 1},
                    candidate={
                        "candidate_id": "c1",
                        "version": 1,
                        "brief_version": 1,
                        "content_hash": "hash",
                    },
                    report={"valid": True},
                )
                writer.append_event("r1", {"kind": "termination"})
                committed = True

            db.set_trace_callback(before_query)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(repo, "read", interleaved_read)
        snapshot = read_aggregate()
    assert committed, "writer must commit between the first and second SELECT"
    assert snapshot == before
    after = read_aggregate()
    assert after != before
    session = repo.get_session("s1")
    assert session["candidate_id"] == "c1"
    assert len(session["versions"]) == 1
    assert session["runs"][0]["status"] == "succeeded"
    assert len(repo.get_events("r1")) == 2
