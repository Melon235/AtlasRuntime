"""Loopback-first FastAPI/SSE interaction layer for AtlasOS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from atlasos.app.replay import (
    ReplayError,
    ReplayNotConfigured,
    ReplayRunNotFound,
    ReplayService,
    create_replay_router,
)
from atlasos.app.replay.sources import EvidenceRoot
from atlasos.app.service import (
    ApplicationError,
    ApprovalAuthorityUnavailable,
    AtlasApplicationService,
    ReadOnlyMode,
    RecoveryAuthorityUnavailable,
)

STATIC = Path(__file__).resolve().parent / "static"
INDEX = STATIC / "index.html"
REPLAY_INDEX = STATIC / "replay.html"

#: The label the application's own run directory gets inside the replay index, so a finished live
#: run is the *same* run in the workbench and never a second copy of itself.
LIVE_ROOT_LABEL = "live"


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    mode: str = "workflow"
    workspace: str | None = None
    max_turns: int = Field(default=12, ge=1, le=40)
    budget_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    interactive: bool = False
    new_paths: list[str] = Field(default_factory=list, max_length=100)


class PatchDecisionRequest(BaseModel):
    run_id: str
    workspace_id: str
    snapshot_id: str
    patch_digest: str
    confirm: bool = Field(strict=True)


def create_app(
    *,
    runs_root: str | Path,
    replay_roots: list[EvidenceRoot] | None = None,
    read_only: bool = False,
    provider: str = "deepseek",
    sandbox_enabled: bool = True,
    sandbox_image: str | None = None,
) -> FastAPI:
    runs_root = Path(runs_root).resolve()
    service = AtlasApplicationService(
        runs_root=runs_root,
        provider=provider,
        sandbox_enabled=sandbox_enabled,
        sandbox_image=sandbox_image,
    )
    pinned = list(replay_roots or [])
    live_root = None
    if not any(root.path.resolve() == runs_root for root in pinned):
        live_root = EvidenceRoot(label=LIVE_ROOT_LABEL, path=runs_root)
    replay_service = ReplayService(pinned, live_root=live_root)
    app = FastAPI(title="AtlasOS", version="0.0.1")
    #: Read-only handles for tests and for a future admin view. Neither grants authority.
    app.state.runs_root = runs_root
    app.state.service = service
    app.state.replay = replay_service

    def refuse_write() -> None:
        if read_only:
            raise ReadOnlyMode(
                "this server runs in read-only replay mode; live submission, approval and "
                "recovery routes are disabled"
            )

    def local_review_request(request: Request) -> None:
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        try:
            local = urlsplit("//" + host).hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            local = False
        if not local or (origin and origin != f"{request.url.scheme}://{host}"):
            raise ReadOnlyMode(
                "interactive patch review is restricted to the local Workbench origin"
            )

    @app.exception_handler(ApplicationError)
    async def application_error(_request: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(error, "status_code", 400),
            content={"error": str(error), "kind": type(error).__name__},
        )

    @app.exception_handler(ApprovalAuthorityUnavailable)
    async def approval_error(
        _request: Request, error: ApprovalAuthorityUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": str(error), "kind": "ApprovalAuthorityUnavailable"},
        )

    @app.exception_handler(RecoveryAuthorityUnavailable)
    async def recovery_error(
        _request: Request, error: RecoveryAuthorityUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": str(error), "kind": "RecoveryAuthorityUnavailable"},
        )

    @app.exception_handler(ReplayNotConfigured)
    async def replay_not_configured(_request: Request, error: ReplayNotConfigured) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"error": str(error), "kind": "ReplayNotConfigured"}
        )

    @app.exception_handler(ReplayRunNotFound)
    async def replay_not_found(_request: Request, error: ReplayRunNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"error": str(error), "kind": "ReplayRunNotFound"}
        )

    @app.exception_handler(ReplayError)
    async def replay_error(_request: Request, error: ReplayError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"error": str(error), "kind": type(error).__name__}
        )

    app.include_router(create_replay_router(replay_service))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "atlasos-app",
            "loopback_only_by_default": True,
            "read_only": read_only,
            "provider": service.provider,
            "sandbox_enabled": service.sandbox_enabled,
            "replay_roots": len(replay_service.roots),
        }

    @app.get("/api/workspaces")
    def workspaces() -> dict[str, Any]:
        return service.workspaces()

    @app.post("/api/runs")
    def submit(body: RunRequest, request: Request) -> dict[str, Any]:
        refuse_write()
        if body.interactive:
            local_review_request(request)
        return service.submit(
            body.task,
            mode=body.mode,
            workspace=body.workspace,
            max_turns=body.max_turns,
            budget_tokens=body.budget_tokens,
            interactive=body.interactive,
            new_paths=tuple(body.new_paths),
        )

    @app.get("/api/runs/{run_id}/patch")
    def patch_review(run_id: str, request: Request) -> dict[str, Any]:
        local_review_request(request)
        result = service.patch_review(run_id)
        result["decisions_enabled"] = not read_only
        return result

    @app.post("/api/runs/{run_id}/patch/{action}")
    def patch_decision(
        run_id: str, action: str, body: PatchDecisionRequest, request: Request
    ) -> JSONResponse:
        refuse_write()
        local_review_request(request)
        result = service.decide_patch(
            run_id, action, body.model_dump(exclude={"confirm"}), confirm=body.confirm
        )
        status = {"CONFLICT": 409, "APPLY_FAILED": 500}.get(result["state"], 200)
        return JSONResponse(status_code=status, content=result)

    @app.get("/api/runs")
    def runs(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return service.list_runs(limit=limit)

    @app.get("/api/runs/{run_id}/replay")
    def replay_link(run_id: str) -> dict[str, Any]:
        """The same run in the replay index, or an explicit "not replayable yet"."""

        run_dir = str(runs_root / run_id)
        key = replay_service.find_run_key(LIVE_ROOT_LABEL, run_id)
        return {
            "run_id": run_id,
            "run_dir": run_dir,
            "replay_run_key": key,
            "available": key is not None,
            "reason": None
            if key is not None
            else "no session or evidence has been written for this run yet",
        }

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        refuse_write()
        return service.cancel(run_id)

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str) -> dict[str, Any]:
        return service.detail(run_id)

    @app.get("/api/runs/{run_id}/result")
    def result(run_id: str) -> dict[str, Any]:
        return service.result(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str) -> list[dict[str, Any]]:
        return service.events(run_id)

    @app.get("/api/runs/{run_id}/stream")
    def stream(
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        def generate() -> Any:
            for event in service.stream_events(run_id, after=start):
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/approvals")
    def approvals() -> list[dict[str, Any]]:
        return service.list_approvals()

    @app.get("/api/runs/{run_id}/approvals")
    def approvals_for_run(run_id: str) -> list[dict[str, Any]]:
        return service.list_approvals(run_id)

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str) -> dict[str, Any]:
        refuse_write()
        return service.approve(approval_id)

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(approval_id: str) -> dict[str, Any]:
        refuse_write()
        return service.reject(approval_id)

    @app.get("/api/runs/{run_id}/recovery")
    def recovery(run_id: str) -> dict[str, Any]:
        return service.recovery(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str) -> dict[str, Any]:
        refuse_write()
        return service.resume(run_id)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/runs/{run_id}")
    def run_page(run_id: str) -> FileResponse:
        del run_id
        return FileResponse(INDEX)

    @app.get("/approvals")
    def approvals_page() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/history")
    def history_page() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/replay")
    def replay_page() -> FileResponse:
        return FileResponse(REPLAY_INDEX)

    @app.get("/workbench-labels.js", include_in_schema=False)
    def workbench_labels() -> FileResponse:
        return FileResponse(STATIC / "workbench-labels.js", media_type="text/javascript")

    return app
