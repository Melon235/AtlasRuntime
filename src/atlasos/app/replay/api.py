"""Read-only replay API namespace.

Every route here is a GET. There is deliberately no POST, no execution entry point, no
approval resolution and no recovery call: the workbench projects evidence, it never acts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from atlasos.app.replay.service import ReplayNotConfigured, ReplayService


def create_replay_router(service: ReplayService) -> APIRouter:
    router = APIRouter(prefix="/api/replay", tags=["replay"])

    def _service() -> ReplayService:
        if not service.roots:
            raise ReplayNotConfigured(
                "no evidence root is configured; start the workbench with --replay-roots "
                "or set ATLASOS_REPLAY_ROOTS"
            )
        return service

    @router.get("/sources")
    def sources() -> dict[str, Any]:
        return _service().sources()

    @router.get("/runs")
    def runs(
        q: str | None = Query(default=None, max_length=200),
        stage: str | None = Query(default=None, max_length=120),
        source: str | None = Query(default=None, max_length=40),
        mode: str | None = Query(default=None, max_length=60),
        status: str | None = Query(default=None, max_length=60),
        provider: str | None = Query(default=None, max_length=40),
        completeness: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return _service().list_runs(
            query=q,
            stage=stage,
            source_kind=source,
            mode=mode,
            status=status,
            provider_kind=provider,
            completeness=completeness,
            limit=limit,
            offset=offset,
        )

    @router.get("/runs/{run_key}")
    def overview(run_key: str) -> dict[str, Any]:
        return _service().overview(run_key)

    @router.get("/runs/{run_key}/agents")
    def agents(run_key: str) -> dict[str, Any]:
        return _service().agents(run_key)

    @router.get("/runs/{run_key}/timeline")
    def timeline(
        run_key: str,
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return _service().timeline(run_key, limit=limit, offset=offset)

    @router.get("/runs/{run_key}/delegation")
    def delegation(run_key: str) -> dict[str, Any]:
        return _service().delegation(run_key)

    @router.get("/runs/{run_key}/context")
    def context(run_key: str) -> dict[str, Any]:
        return _service().resources(run_key)

    @router.get("/runs/{run_key}/tools")
    def tools(run_key: str) -> dict[str, Any]:
        return _service().tools(run_key)

    @router.get("/runs/{run_key}/artifacts")
    def artifacts(run_key: str) -> dict[str, Any]:
        return _service().artifacts(run_key)

    @router.get("/runs/{run_key}/verification")
    def verification(run_key: str) -> dict[str, Any]:
        return _service().verification(run_key)

    @router.get("/runs/{run_key}/requests")
    def requests(run_key: str) -> dict[str, Any]:
        return _service().requests(run_key)

    return router
