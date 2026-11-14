"""Local FastAPI interface for quest drafting and human review."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator

from game_content_agent.application.service import Service, utc_now
from game_content_agent.config import Settings
from game_content_agent.infrastructure.repository import Repository, RepositoryError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBody(StrictModel):
    world_version: str = "v1"


class RunBody(StrictModel):
    purpose: Literal["prepare_brief", "generate", "revise"]
    instruction: str = Field(min_length=1, max_length=6000)
    mode: Literal["fake", "live"] = "fake"
    scenario: Literal[
        "normal",
        "conflict",
        "invalid_entity",
        "invalid_citation",
        "invalid_graph",
        "missing_condition",
        "tool_failure",
        "loop_limit",
        "provider_error",
        "repair_format",
    ] = "normal"
    base_version: int | None = Field(default=None, strict=True, ge=1)


class ConfirmBody(StrictModel):
    version: int = Field(strict=True, ge=1)


class ReviewBody(StrictModel):
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=6000)
    content_hash: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("审核理由不能为空")
        return value.strip()


def _database_path(settings: Settings) -> Path:
    prefix = "sqlite:///"
    url = settings.database_url
    if not url.startswith(prefix):
        raise ValueError("仅支持 SQLite 数据库 URL")
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        raise ValueError("持久化数据库必须使用文件路径")
    path = Path(raw)
    return path if path.is_absolute() else Path(settings.project_root) / path


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(_database_path(settings))
    service = Service(settings, repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository.acquire_instance_lock()
        try:
            repository.initialize(utc_now())
            app.state.service = service
            app.state.tasks = set()
            yield
        finally:
            tasks = list(getattr(app.state, "tasks", set()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            repository.release_instance_lock()

    app = FastAPI(lifespan=lifespan)
    static_dir = (
        Path(settings.project_root) / "src" / "game_content_agent" / "web" / "static"
    )
    template_dir = (
        Path(settings.project_root) / "src" / "game_content_agent" / "web" / "templates"
    )
    static_dir.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(template_dir))
    asset_versions = {
        name: hashlib.sha256((static_dir / name).read_bytes()).hexdigest()[:12]
        for name in ("app.js", "styles.css")
    }
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(RepositoryError)
    async def repository_error(_request: Request, error: RepositoryError):
        return JSONResponse(
            {"error": {"code": error.code, "message": str(error)}},
            status_code=error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "请求字段无效"}},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _error: Exception):
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "服务暂时无法处理请求"}},
            status_code=500,
        )

    def current_service() -> Service:
        return app.state.service

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"asset_versions": asset_versions},
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/world")
    def world():
        return current_service().world_info()

    @app.post("/sessions", status_code=201)
    def create_session(body: SessionBody):
        return current_service().create_session(body.world_version)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str):
        return current_service().get_session(session_id)

    @app.post("/sessions/{session_id}/runs", status_code=202)
    async def start_run(session_id: str, body: RunBody):
        # Stays async to schedule the run on the event loop; the blocking part runs
        # in the threadpool like the plain `def` routes below.
        result = await run_in_threadpool(
            current_service().start_run,
            session_id,
            body.purpose,
            body.instruction,
            body.mode,
            body.scenario,
            body.base_version,
        )
        task = asyncio.create_task(current_service().execute_run(result["run_id"]))
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return result

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return current_service().get_run(run_id)

    @app.get("/runs/{run_id}/replay")
    def replay(run_id: str):
        return current_service().get_replay(run_id)

    @app.post("/sessions/{session_id}/brief/confirm")
    def confirm_brief(session_id: str, body: ConfirmBody):
        return current_service().confirm_brief(session_id, body.version)

    @app.get("/candidates/{candidate_id}/versions/{version}")
    def candidate_version(candidate_id: str, version: int):
        return current_service().get_candidate_version(candidate_id, version)

    @app.post("/candidates/{candidate_id}/versions/{version}/review")
    def review(candidate_id: str, version: int, body: ReviewBody):
        return current_service().review(
            candidate_id, version, body.decision, body.reason, body.content_hash
        )

    @app.get("/candidates/{candidate_id}/versions/{version}/export")
    def export(candidate_id: str, version: int, format: Literal["json", "markdown"]):
        content, media_type, filename = current_service().export(
            candidate_id, version, format
        )
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app
