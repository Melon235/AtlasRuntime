"""Read-only replay service: evidence discovery plus the projection facade.

The service owns no execution path, no router, no budget and no approval authority. It reads
evidence that already exists on disk and returns a projection. Every method is a pure read.
"""

from __future__ import annotations

from typing import Any

from atlasos.app.replay import projection
from atlasos.app.replay.sources import (
    EvidenceReader,
    EvidenceRoot,
    ReplayError,
    RunArtifacts,
    ScanResult,
    scan,
)

EVIDENCE_ROOT_ENV = "ATLASOS_REPLAY_ROOTS"


class ReplayNotConfigured(ReplayError):
    """No evidence root was configured, so there is nothing to replay."""


class ReplayRunNotFound(ReplayError):
    """The requested run key is not in the discovered evidence."""


class ReplayService:
    """Lazy, in-memory index over one or more read-only evidence roots."""

    def __init__(self, roots: list[EvidenceRoot], *, live_root: EvidenceRoot | None = None) -> None:
        #: Roots the operator named. They make the workbench "configured" even when empty.
        self._pinned = tuple(roots)
        #: The application's own run directory. It is indexed like any other root, so a finished
        #: live run is the same run here and never a second copy of itself -- but an empty run
        #: directory does not make the workbench configured.
        self._live = live_root
        self._roots = tuple(roots) + ((live_root,) if live_root is not None else ())
        self._readers: dict[str, EvidenceReader] = {
            root.label: EvidenceReader(root) for root in self._roots
        }
        self._scan: ScanResult | None = None
        self._models: dict[str, projection.RunModel] = {}
        self._summaries: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ lifecycle
    @property
    def roots(self) -> tuple[EvidenceRoot, ...]:
        return self._roots

    @property
    def configured(self) -> bool:
        """True when there is something to replay: a named root, or a run in the live root."""

        if self._pinned:
            return True
        return bool(self.scan().runs)

    def refresh(self) -> None:
        self._scan = None
        self._models.clear()
        self._summaries.clear()

    def scan(self) -> ScanResult:
        if self._scan is None:
            self._scan = scan(list(self._roots))
        return self._scan

    # ------------------------------------------------------------------ lookup
    def _run(self, run_key: str) -> RunArtifacts:
        for run in self.scan().runs:
            if run.run_key == run_key:
                return run
        raise ReplayRunNotFound(f"unknown replay run: {run_key}")

    def find_run_key(self, root_label: str, run_dir: str) -> str | None:
        """The replay key for one directory, so a live run links to its own historical record.

        Returns ``None`` when the directory is not part of the discovered evidence (for example a
        run that has not written a session yet), so the caller reports "not yet replayable" rather
        than inventing a key.
        """

        wanted = run_dir.replace("\\", "/").strip("/")
        for run in self.scan().runs:
            if run.root_label != root_label:
                continue
            if run.run_dir.replace("\\", "/").strip("/") == wanted:
                return run.run_key
        return None

    def model(self, run_key: str) -> projection.RunModel:
        cached = self._models.get(run_key)
        if cached is not None:
            return cached
        run = self._run(run_key)
        reader = self._readers.get(run.root_label)
        if reader is None:
            raise ReplayRunNotFound(f"evidence root is not configured for {run_key}")
        model = projection.RunModel(run, reader)
        self._models[run_key] = model
        return model

    # ------------------------------------------------------------------ listing
    def summary_of(self, run_key: str) -> dict[str, Any]:
        cached = self._summaries.get(run_key)
        if cached is None:
            cached = projection.summary(self.model(run_key))
            self._summaries[run_key] = cached
        return cached

    def summaries(self) -> list[dict[str, Any]]:
        return [self.summary_of(run.run_key) for run in self.scan().runs]

    def list_runs(
        self,
        *,
        query: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
        mode: str | None = None,
        status: str | None = None,
        provider_kind: str | None = None,
        completeness: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        items = list(self.summaries())
        if query:
            needle = query.strip().lower()
            items = [item for item in items if needle in _search_text(item)]
        if stage:
            items = [item for item in items if item["experiment"]["stage"] == stage]
        if source_kind:
            items = [item for item in items if item["source_kind"] == source_kind]
        if mode:
            items = [item for item in items if str(item["execution"]["mode"]) == mode]
        if status:
            items = [item for item in items if str(item["status"]["recorded"]) == status]
        if provider_kind:
            items = [item for item in items if item["model"]["provider_kind"] == provider_kind]
        if completeness:
            items = [item for item in items if item["completeness"]["level"] == completeness]

        items = sort_runs_desc(items)
        total = len(items)
        window = items[offset : offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "truncated": offset + limit < total,
            "runs": window,
            "facets": self._facets(items),
        }

    def _facets(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        stages = sorted({item["experiment"]["stage"] for item in items})
        kinds = sorted({item["source_kind"] for item in items})
        modes = sorted({str(item["execution"]["mode"]) for item in items})
        statuses = sorted({str(item["status"]["recorded"]) for item in items})
        return {
            "stages": stages,
            "source_kinds": kinds,
            "modes": modes,
            "statuses": statuses,
            "provider_kinds": sorted({item["model"]["provider_kind"] for item in items}),
            "completeness": sorted({item["completeness"]["level"] for item in items}),
        }

    # ------------------------------------------------------------------ views
    def overview(self, run_key: str) -> dict[str, Any]:
        return projection.overview(self.model(run_key))

    def agents(self, run_key: str) -> dict[str, Any]:
        model = self.model(run_key)
        agents = projection.agent_views(model)
        return {
            "run_key": run_key,
            "total": len(agents),
            "agents": agents,
            "topology": projection.overview(model)["topology"],
        }

    def timeline(self, run_key: str, *, limit: int = 500, offset: int = 0) -> dict[str, Any]:
        return projection.timeline(self.model(run_key), limit=limit, offset=offset)

    def delegation(self, run_key: str) -> dict[str, Any]:
        return projection.delegation(self.model(run_key))

    def resources(self, run_key: str) -> dict[str, Any]:
        return projection.resources(self.model(run_key))

    def tools(self, run_key: str) -> dict[str, Any]:
        model = self.model(run_key)
        payload = projection.tools(model)
        payload["public_tests"] = _public_test_rows(model)
        return payload

    def artifacts(self, run_key: str) -> dict[str, Any]:
        return projection.artifacts(self.model(run_key))

    def verification(self, run_key: str) -> dict[str, Any]:
        return projection.verification(self.model(run_key))

    def requests(self, run_key: str) -> dict[str, Any]:
        return projection.requests_view(self.model(run_key))

    # ------------------------------------------------------------------ evidence sources
    def sources(self) -> dict[str, Any]:
        result = self.scan()
        return {
            "roots": [root.as_dict() for root in result.roots] or [],
            "configured": self.configured,
            "mode": "READ_ONLY_HISTORICAL_REPLAY",
            "stats": result.stats,
            "problems": result.problems,
            "matrices": [
                {
                    "source": matrix.reference(),
                    "cells": len(matrix.rows()),
                    "stage": matrix.data.get("stage"),
                    "plan_id": matrix.data.get("plan_id"),
                    "started_at": matrix.data.get("started_at"),
                    "finished_at": matrix.data.get("finished_at"),
                    "stop_reason": matrix.data.get("stop_reason"),
                    "usage_completeness": matrix.data.get("usage_completeness"),
                }
                for matrix in result.matrices
            ],
            "kinds": _kind_counts(result),
            "duplicates": result.duplicates[:50],
            "duplicate_note": (
                "these locations carry the same cell identity as a projected run; they are "
                "listed here instead of being shown as separate runs"
            ),
        }


def _kind_counts(result: ScanResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in result.runs:
        counts[run.kind] = counts.get(run.kind, 0) + 1
    return counts


def _search_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("run_key"),
        item.get("cell_id"),
        item.get("run_dir"),
        item["experiment"].get("plan_id"),
        item["experiment"].get("scope"),
        item["task"].get("task_id"),
        item["status"].get("failure_kind"),
        item["model"].get("requested"),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def sort_runs_desc(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first by recorded start; runs without a recorded start follow, key-sorted.

    A run with no timestamp is never given one, so it cannot be interleaved with dated runs.
    """

    dated = [item for item in items if item["timing"].get("started_at")]
    undated = [item for item in items if not item["timing"].get("started_at")]
    dated.sort(
        key=lambda item: (str(item["timing"]["started_at"]), str(item["run_key"])),
        reverse=True,
    )
    undated.sort(key=lambda item: str(item["run_key"]))
    return dated + undated


def _public_test_rows(model: projection.RunModel) -> list[dict[str, Any]]:
    session = model.session
    runs = ((session.get("public_tests") or {}).get("runs")) or []
    return [
        {
            "call_index": row.get("call_index"),
            "status": row.get("status"),
            "exit_code": row.get("exit_code"),
            "duration_ms": row.get("duration_ms"),
        }
        for row in runs
        if isinstance(row, dict)
    ]
