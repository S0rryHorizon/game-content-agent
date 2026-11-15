"""SQLite persistence for immutable design versions and review decisions."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _brief(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    payload = _decode(row["payload_json"])
    payload["confirmed"] = bool(row["confirmed"])
    return payload


class RepositoryError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file: Any = None

    def acquire_instance_lock(self) -> None:
        """Prevent a second process from recovering or writing this database."""
        lock_file = open(f"{self.path}.lock", "a+b")  # noqa: SIM115 - held for app lifespan
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RepositoryError(
                "instance_running", "数据库已有运行中的服务实例"
            ) from exc
        self._lock_file = lock_file

    def release_instance_lock(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        with self.read() as connection:
            connection.execute("BEGIN DEFERRED")
            yield connection

    def initialize(self, interrupted_at: str) -> None:
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, world_version TEXT NOT NULL,
                    state_json TEXT NOT NULL, candidate_id TEXT UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS briefs (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    version INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, version)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    version INTEGER NOT NULL, brief_version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    PRIMARY KEY (candidate_id, version),
                    UNIQUE (session_id, version)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    candidate_id TEXT NOT NULL, version INTEGER NOT NULL,
                    decision TEXT NOT NULL, reason TEXT NOT NULL,
                    content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    PRIMARY KEY (candidate_id, version),
                    FOREIGN KEY (candidate_id, version) REFERENCES candidates(candidate_id, version)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    purpose TEXT NOT NULL, instruction TEXT NOT NULL,
                    mode TEXT NOT NULL, scenario TEXT NOT NULL,
                    base_version INTEGER, status TEXT NOT NULL,
                    termination_reason TEXT, message TEXT,
                    result_json TEXT, created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_run ON runs(status)
                    WHERE status='running';
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
            """)
            fields = {row["name"] for row in db.execute("PRAGMA table_info(briefs)")}
            if "confirmed" not in fields:
                db.execute(
                    "ALTER TABLE briefs ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0"
                )
                for row in db.execute(
                    "SELECT session_id,version,payload_json FROM briefs"
                ):
                    if _decode(row["payload_json"]).get("confirmed"):
                        db.execute(
                            "UPDATE briefs SET confirmed=1 WHERE session_id=? AND version=?",
                            (row["session_id"], row["version"]),
                        )
            db.execute(
                "UPDATE runs SET status='interrupted', termination_reason='server_restart', "
                "message='服务重启，运行未自动重放', finished_at=? WHERE status='running'",
                (interrupted_at,),
            )

    def create_session(
        self, session_id: str, world_version: str, state: dict, now: str
    ) -> dict:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, NULL, ?)",
                (session_id, world_version, _json(state), now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        with self.read_snapshot() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError("session_not_found", "会话不存在", 404)
            brief = db.execute(
                "SELECT payload_json,confirmed FROM briefs WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            versions = db.execute(
                "SELECT payload_json FROM candidates WHERE session_id=? ORDER BY version",
                (session_id,),
            ).fetchall()
            runs = db.execute(
                "SELECT run_id, session_id, purpose, status, termination_reason, message, "
                "created_at, finished_at FROM runs WHERE session_id=? ORDER BY created_at, run_id",
                (session_id,),
            ).fetchall()
        return {
            "session_id": row["session_id"],
            "world_version": row["world_version"],
            "state_snapshot": _decode(row["state_json"]),
            "brief": _brief(brief),
            "candidate_id": row["candidate_id"],
            "versions": [_decode(item["payload_json"]) for item in versions],
            "runs": [dict(item) for item in runs],
        }

    def get_brief(self, session_id: str, version: int | None = None) -> dict | None:
        with self.read() as db:
            if version is None:
                row = db.execute(
                    "SELECT payload_json,confirmed FROM briefs WHERE session_id=? ORDER BY version DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT payload_json,confirmed FROM briefs WHERE session_id=? AND version=?",
                    (session_id, version),
                ).fetchone()
        return _brief(row)

    def confirm_brief(self, session_id: str, version: int) -> dict:
        with self.transaction() as db:
            row = db.execute(
                "SELECT version, payload_json, confirmed FROM briefs WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None or row["version"] != version:
                raise RepositoryError("stale_brief", "需求版本已过期")
            brief = _brief(row)
            assert brief is not None
            if not brief["confirmed"]:
                brief["confirmed"] = True
                db.execute(
                    "UPDATE briefs SET confirmed=1 WHERE session_id=? AND version=?",
                    (session_id, version),
                )
        return brief

    def create_run(self, run: dict) -> None:
        with self.transaction() as db:
            session = db.execute(
                "SELECT candidate_id FROM sessions WHERE session_id=?",
                (run["session_id"],),
            ).fetchone()
            if session is None:
                raise RepositoryError("session_not_found", "会话不存在", 404)
            if db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise RepositoryError("run_busy", "已有运行中的任务")
            brief_row = db.execute(
                "SELECT payload_json,confirmed FROM briefs WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (run["session_id"],),
            ).fetchone()
            brief = _brief(brief_row)
            if run["purpose"] in {"generate", "revise"} and (
                not brief or not brief["confirmed"]
            ):
                raise RepositoryError("brief_unconfirmed", "请先确认当前需求摘要")
            if run["purpose"] == "revise":
                row = db.execute(
                    "SELECT MAX(version) AS version FROM candidates WHERE session_id=?",
                    (run["session_id"],),
                ).fetchone()
                if row["version"] is None or run["base_version"] != row["version"]:
                    raise RepositoryError(
                        "stale_base_version", "修订基准版本不是最新版本"
                    )
            try:
                db.execute(
                    "INSERT INTO runs (run_id,session_id,purpose,instruction,mode,scenario,base_version,"
                    "status,created_at) VALUES (?,?,?,?,?,?,?,'running',?)",
                    (
                        run["run_id"],
                        run["session_id"],
                        run["purpose"],
                        run["instruction"],
                        run["mode"],
                        run["scenario"],
                        run["base_version"],
                        run["created_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryError("run_busy", "已有运行中的任务") from exc

    def get_run(self, run_id: str) -> dict:
        with self.read() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RepositoryError("run_not_found", "运行记录不存在", 404)
        return {
            key: row[key]
            for key in (
                "run_id",
                "session_id",
                "purpose",
                "status",
                "termination_reason",
                "message",
                "created_at",
                "finished_at",
            )
        } | {"result": _decode(row["result_json"])}

    def get_run_context(self, run_id: str) -> dict:
        with self.read() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RepositoryError("run_not_found", "运行记录不存在", 404)
        return dict(row)

    def append_event(self, run_id: str, event: dict) -> None:
        with self.transaction() as db:
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO events VALUES (?,?,?)", (run_id, sequence, _json(event))
            )

    def get_events(self, run_id: str) -> list[dict]:
        with self.read_snapshot() as db:
            if not db.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone():
                raise RepositoryError("run_not_found", "运行记录不存在", 404)
            rows = db.execute(
                "SELECT payload_json FROM events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_decode(row["payload_json"]) for row in rows]

    def finish_run(
        self,
        run_id: str,
        status: str,
        reason: str,
        message: str,
        result: dict | None,
        finished_at: str,
        *,
        brief: dict | None = None,
        candidate: dict | None = None,
        report: dict | None = None,
    ) -> None:
        with self.transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] != "running":
                return
            session_id = run["session_id"]
            if brief is not None:
                current = db.execute(
                    "SELECT COALESCE(MAX(version),0) FROM briefs WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                if brief["version"] != current + 1:
                    raise RepositoryError("stale_brief", "需求版本已改变")
                db.execute(
                    "INSERT INTO briefs (session_id,version,payload_json,created_at,confirmed) VALUES (?,?,?,?,0)",
                    (session_id, brief["version"], _json(brief), finished_at),
                )
            if candidate is not None:
                assert report is not None
                current = db.execute(
                    "SELECT COALESCE(MAX(version),0) FROM candidates WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                if candidate["version"] != current + 1:
                    raise RepositoryError("stale_candidate", "候选版本已改变")
                db.execute(
                    "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
                    (
                        candidate["candidate_id"],
                        session_id,
                        candidate["version"],
                        candidate["brief_version"],
                        candidate["content_hash"],
                        _json(candidate),
                        _json(report),
                    ),
                )
                db.execute(
                    "UPDATE sessions SET candidate_id=? WHERE session_id=? AND candidate_id IS NULL",
                    (candidate["candidate_id"], session_id),
                )
            db.execute(
                "UPDATE runs SET status=?, termination_reason=?, message=?, result_json=?, finished_at=? "
                "WHERE run_id=?",
                (
                    status,
                    reason,
                    message,
                    _json(result) if result is not None else None,
                    finished_at,
                    run_id,
                ),
            )

    def get_candidate(self, candidate_id: str, version: int) -> dict:
        with self.read() as db:
            row = db.execute(
                "SELECT c.*, r.decision, r.reason, r.created_at AS reviewed_at "
                "FROM candidates c LEFT JOIN reviews r ON r.candidate_id=c.candidate_id "
                "AND r.version=c.version WHERE c.candidate_id=? AND c.version=?",
                (candidate_id, version),
            ).fetchone()
        if row is None:
            raise RepositoryError("candidate_not_found", "候选版本不存在", 404)
        review = None
        if row["decision"] is not None:
            review = {
                "decision": row["decision"],
                "reason": row["reason"],
                "candidate_version": version,
                "content_hash": row["content_hash"],
                "created_at": row["reviewed_at"],
            }
        return {
            "candidate": _decode(row["payload_json"]),
            "report": _decode(row["report_json"]),
            "review": review,
            "session_id": row["session_id"],
        }

    def review(
        self,
        candidate_id: str,
        version: int,
        decision: str,
        reason: str,
        content_hash: str,
        now: str,
        revalidate: Callable[[dict, dict], dict],
    ) -> dict:
        """Check freshness, revalidate, and write report plus decision atomically."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM candidates WHERE candidate_id=? AND version=?",
                (candidate_id, version),
            ).fetchone()
            if row is None:
                raise RepositoryError("candidate_not_found", "候选版本不存在", 404)
            if row["content_hash"] != content_hash:
                raise RepositoryError("hash_mismatch", "内容摘要不匹配")
            candidate = _decode(row["payload_json"])
            existing = db.execute(
                "SELECT * FROM reviews WHERE candidate_id=? AND version=?",
                (candidate_id, version),
            ).fetchone()
            if existing is not None:
                if existing["decision"] != decision or existing["reason"] != reason:
                    raise RepositoryError("already_reviewed", "该版本已有不同审核决定")
                return self._review_response(
                    candidate, _decode(existing["report_json"]), existing
                )
            latest = db.execute(
                "SELECT MAX(version) FROM candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()[0]
            brief_row = db.execute(
                "SELECT payload_json,confirmed FROM briefs WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (row["session_id"],),
            ).fetchone()
            brief = _brief(brief_row)
            if (
                latest != version
                or brief is None
                or not brief["confirmed"]
                or brief["version"] != row["brief_version"]
            ):
                raise RepositoryError("stale_review", "候选或需求版本已过期")
            report = revalidate(candidate, brief)
            if report != _decode(row["report_json"]):
                raise RepositoryError(
                    "validation_changed", "复核结果与该版本原始报告不一致"
                )
            if decision == "accept" and not report["valid"]:
                raise RepositoryError("invalid_candidate", "候选未通过确定性检查")
            db.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    version,
                    decision,
                    reason,
                    content_hash,
                    now,
                    _json(report),
                ),
            )
            review = {
                "decision": decision,
                "reason": reason,
                "candidate_version": version,
                "content_hash": content_hash,
                "created_at": now,
            }
            return {"review": review, "candidate": candidate, "report": report}

    @staticmethod
    def _review_response(candidate: dict, report: dict, review: sqlite3.Row) -> dict:
        return {
            "review": {
                "decision": review["decision"],
                "reason": review["reason"],
                "candidate_version": review["version"],
                "content_hash": review["content_hash"],
                "created_at": review["created_at"],
            },
            "candidate": candidate,
            "report": report,
        }
