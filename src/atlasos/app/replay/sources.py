"""Read-only evidence roots for the Stage H1 historical replay workbench.

This module is the only place in the replay layer that touches the filesystem. It never
writes, every read is bounded, and every path is resolved inside an explicitly configured
evidence root. Nothing here is runtime authority: the projections built on top of it are
presentation only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSONL_BYTES = 16 * 1024 * 1024
MAX_JSONL_LINES = 20_000
MAX_TEXT_BYTES = 256 * 1024
#: A per-root traversal budget. Pruned directories do not consume it, so a root holding
#: hundreds of thousands of materialised workspace files still walks in a bounded time.
MAX_WALK_ENTRIES = 400_000
MAX_RUNS = 800
MAX_PROBLEMS = 100

#: Directories that never contain run-level evidence but do contain large trees.
PRUNED_DIRECTORIES = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "workspace",
        "workspaces",
        "fixtures",
        "fixture",
        "checkpoints",
        "oracle-sandbox",
        "private",
    }
)

#: Directory-name prefixes that unambiguously denote scratch space rather than evidence. A
#: pruned directory is reported in the scan stats, never silently forgotten.
PRUNED_PREFIXES = ("tmp-", "tmp_", "scratch-", ".pytest")

_LABEL_PATTERN = re.compile(r"[^a-z0-9_.-]+")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class ReplayError(RuntimeError):
    """Base class for replay failures mapped to explicit HTTP responses."""


class ReplayConfigError(ReplayError):
    """An evidence root spec is invalid."""


class PathOutsideRoot(ReplayError):
    """A relative path escaped its evidence root."""


class EvidenceTooLarge(ReplayError):
    """An evidence file exceeded the bounded read limit."""


@dataclass(frozen=True)
class EvidenceRoot:
    label: str
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "path": str(self.path), "exists": self.path.is_dir()}


def normalize_label(value: str, *, fallback: str = "root") -> str:
    cleaned = _LABEL_PATTERN.sub("-", value.strip().lower()).strip("-._")
    return cleaned[:48] or fallback


def parse_root_specs(
    specs: Iterable[str], *, env_value: str | None = None, base: Path | None = None
) -> list[EvidenceRoot]:
    """Parse ``label=path`` or ``path`` specs, falling back to ``ATLASOS_REPLAY_ROOTS``.

    The environment variable is a path list (``;`` on Windows, ``:`` elsewhere). A directory
    that does not exist is reported, not silently created: reading evidence must never
    mutate the filesystem.
    """

    raw: list[str] = [spec for spec in specs if spec.strip()]
    if not raw and env_value:
        raw = [part for part in env_value.split(os.pathsep) if part.strip()]
    roots: list[EvidenceRoot] = []
    seen: set[str] = set()
    for index, spec in enumerate(raw):
        alias, raw_path = _split_spec(spec)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() and base is not None:
            candidate = base / candidate
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise ReplayConfigError(f"evidence root is not a directory: {resolved}")
        marker = str(resolved).lower()
        if marker in seen:
            continue
        seen.add(marker)
        roots.append(
            EvidenceRoot(
                label=normalize_label(alias or resolved.name, fallback=f"root{index + 1}"),
                path=resolved,
            )
        )
    return roots


def _split_spec(spec: str) -> tuple[str | None, str]:
    if "=" in spec:
        left, right = spec.split("=", 1)
        if left and not _DRIVE_PATTERN.match(left) and not left.startswith(("/", "\\")):
            return left, right
    return None, spec


def is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:  # different drives on Windows
        return False


def _safe_parts(relative: str) -> tuple[str, ...]:
    if not relative or relative.startswith(("/", "\\")) or _DRIVE_PATTERN.match(relative):
        raise PathOutsideRoot(f"absolute evidence path is not allowed: {relative!r}")
    normalized = relative.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise PathOutsideRoot(f"evidence path escapes its root: {relative!r}")
    return parts


_JSON_CACHE: dict[tuple[str, int, int], Any] = {}


class EvidenceReader:
    """Bounded, read-only access to one evidence root."""

    def __init__(self, root: EvidenceRoot) -> None:
        self.root = root

    # ---------------------------------------------------------------- resolution
    def resolve(self, relative: str) -> Path:
        parts = _safe_parts(relative)
        candidate = self.root.path.joinpath(*parts)
        resolved = candidate.resolve()
        if not is_within(resolved, self.root.path):
            raise PathOutsideRoot(f"evidence path escapes its root: {relative!r}")
        return resolved

    def display(self, relative: str) -> str:
        return f"{self.root.label}/{relative.replace(os.sep, '/')}"

    def reference(self, relative: str) -> dict[str, Any]:
        """A source pointer that survives a moved checkout inside the same root."""
        return {"root": self.root.label, "path": relative.replace(os.sep, "/")}

    # ---------------------------------------------------------------- reads
    def read_json(self, relative: str) -> Any:
        path = self.resolve(relative)
        stat = path.stat()
        if stat.st_size > MAX_JSON_BYTES:
            raise EvidenceTooLarge(f"{relative} exceeds {MAX_JSON_BYTES} bytes")
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _JSON_CACHE.get(key)
        if cached is not None:
            return cached
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        if len(_JSON_CACHE) > 512:
            _JSON_CACHE.clear()
        _JSON_CACHE[key] = value
        return value

    def read_jsonl(self, relative: str, *, limit: int = MAX_JSONL_LINES) -> list[Any]:
        path = self.resolve(relative)
        if path.stat().st_size > MAX_JSONL_BYTES:
            raise EvidenceTooLarge(f"{relative} exceeds {MAX_JSONL_BYTES} bytes")
        rows: list[Any] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def read_head(self, relative: str, *, limit: int = MAX_TEXT_BYTES) -> str:
        path = self.resolve(relative)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)

    def file_facts(self, relative: str) -> dict[str, Any]:
        path = self.resolve(relative)
        if not path.is_file():
            return {"available": False, "reason": "NOT_FOUND"}
        stat = path.stat()
        return {"available": True, "bytes": stat.st_size}


# -------------------------------------------------------------------- discovery


@dataclass
class Found:
    role: str
    relative: str
    size: int


@dataclass
class WalkResult:
    files: list[tuple[str, int]]
    truncated: bool = False
    visited: int = 0
    pruned: int = 0
    pruned_names: list[str] = field(default_factory=list)


def walk_root(root: EvidenceRoot, *, max_depth: int = 9) -> WalkResult:
    """Walk a root once, breadth-first, pruning materialised workspaces and payload corpora."""

    files: list[tuple[str, int]] = []
    visited = 0
    pruned = 0
    pruned_names: list[str] = []
    truncated = False
    queue: deque[tuple[Path, int]] = deque([(root.path, 0)])
    while queue:
        directory, depth = queue.popleft()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            visited += 1
            if visited > MAX_WALK_ENTRIES:
                truncated = True
                queue.clear()
                break
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name.startswith(".") or name in PRUNED_DIRECTORIES:
                        continue
                    if name.startswith(PRUNED_PREFIXES):
                        pruned += 1
                        if len(pruned_names) < 20:
                            pruned_names.append(Path(entry.path).relative_to(root.path).as_posix())
                        continue
                    if depth >= max_depth:
                        continue
                    queue.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            relative = Path(entry.path).relative_to(root.path).as_posix()
            files.append((relative, size))
    files.sort()
    return WalkResult(
        files=files,
        truncated=truncated,
        visited=visited,
        pruned=pruned,
        pruned_names=pruned_names,
    )


_SESSION_KEYS = frozenset({"main", "delegation", "metrics", "status"})


def _classify(reader: EvidenceReader, relative: str, size: int) -> tuple[str, Any] | None:
    name = relative.rsplit("/", 1)[-1]
    if size > MAX_JSON_BYTES:
        return None
    try:
        if name == "analysis.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and "cell_id" in value and "outcome" in value:
                return "analysis", value
            return None
        if name == "evidence.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and str(value.get("schema_version", "")).startswith(
                "stage-f-cell-evidence"
            ):
                return "evidence", value
            return None
        if name == "session.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and _SESSION_KEYS.issubset(value.keys()):
                return "session", value
            return None
        if name == "result.json":
            value = reader.read_json(relative)
            if not isinstance(value, dict):
                return None
            if {"worker_results", "identity"}.issubset(value.keys()):
                return "native_result", value
            if {"run_id", "status"}.issubset(value.keys()) and "tasks" in value:
                return "atlas_result", value
            return None
        if name == "run.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and {"run_id", "mode", "task"}.issubset(value.keys()):
                return "app_record", value
            return None
        if name == "matrix-report.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and isinstance(value.get("cells"), list):
                return "matrix_report", value
            return None
        if name == "attempt.json":
            value = reader.read_json(relative)
            if isinstance(value, dict) and "cell_id" in value:
                return "attempt", value
            return None
        if name == "provider_requests.json":
            value = reader.read_json(relative)
            return "provider_requests", value
        if name == "workers.json":
            # Only the scoped-worker layout under ``agent/`` carries a manifest we can join to a
            # run. Any other copy is app-run internals, not a run record of its own.
            if "agent" in relative.split("/")[:-1]:
                return "worker_manifest", None
            return None
        if name == "handoffs.json" and relative.rsplit("/", 2)[-2:] == [
            "delegation",
            "handoffs.json",
        ]:
            return "handoff_index", reader.read_json(relative)
        if name == "workspace.patch":
            return "patch", None
        if name == "transcript.jsonl":
            return "transcript", None
        if name == "evidence-index.json":
            return "evidence_index", None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, EvidenceTooLarge):
        return "malformed", None
    return None


def _cell_role(rest: list[str]) -> str:
    head = rest[0] if rest else ""
    if head == "agent":
        return _agent_role(rest[2:]) if len(rest) > 2 else "session"
    if head == "native-runs":
        return "native_result"
    if head == "run":
        # The Stage F preflight harness writes a synthetic session here. It stays visible as a
        # source file but is never treated as the run's main-driven session.
        return "preflight_session"
    return {
        "analysis.json": "analysis",
        "evidence.json": "evidence",
        "attempt.json": "attempt",
    }.get(head, "auxiliary")


def _agent_role(rest: list[str]) -> str:
    if not rest:
        return "session"
    head = rest[0]
    if head == "trajectory":
        return "transcript"
    if head == "handoffs":
        return "handoff_record"
    if head == "delegation":
        return "handoff_index"
    return {
        "session.json": "session",
        "workers.json": "worker_manifest",
        "workspace.patch": "patch",
        "provider_requests.json": "provider_requests",
        "tool_conversations.json": "tool_conversations",
    }.get(head, "auxiliary")


def _owner(parts: list[str]) -> tuple[list[str], str] | None:
    """Return the run directory (as path parts) and the artifact role."""

    for index, part in enumerate(parts[:-1]):
        if part == "cells" and index + 1 < len(parts):
            return parts[: index + 2], _cell_role(parts[index + 2 :])
    name = parts[-1]
    if "trajectory" in parts[:-1] and name == "transcript.jsonl":
        cut = parts.index("trajectory")
        if cut:
            return parts[:cut], "transcript"
        return None
    if len(parts) >= 2 and parts[-2] == "delegation" and name == "handoffs.json":
        return parts[:-2], "handoff_index"
    if len(parts) >= 2 and parts[-2] == "handoffs" and name.endswith(".json"):
        return parts[:-2], "handoff_record"
    if name in {
        "session.json",
        "result.json",
        "run.json",
        "analysis.json",
        "evidence.json",
        "attempt.json",
        "workspace.patch",
        "provider_requests.json",
    }:
        return parts[:-1], {
            "session.json": "session",
            "result.json": "atlas_result",
            "run.json": "app_record",
            "analysis.json": "analysis",
            "evidence.json": "evidence",
            "attempt.json": "attempt",
            "workspace.patch": "patch",
            "provider_requests.json": "provider_requests",
        }[name]
    return None


@dataclass
class RunArtifacts:
    run_key: str
    root_label: str
    run_dir: str
    kind: str
    cell_id: str | None = None
    cell_identity: tuple[str, str] | None = None
    files: dict[str, list[str]] = field(default_factory=dict)
    matrix_row: dict[str, Any] | None = None
    matrix_ref: dict[str, Any] | None = None
    #: What actually executed here: provider, model, declared mode, an execution id when one was
    #: recorded, and a digest of the record itself. Two directories are copies only when this
    #: agrees; a scripted proof and a live run of the same planned cell never do.
    execution: dict[str, Any] = field(default_factory=dict)

    def first(self, role: str) -> str | None:
        values = self.files.get(role) or []
        return values[0] if values else None

    def has(self, role: str) -> bool:
        return bool(self.files.get(role))

    def source_list(self) -> list[dict[str, Any]]:
        return [
            {"role": role, "path": path, "count": len(paths)}
            for role, paths in sorted(self.files.items())
            for path in paths
        ]


@dataclass
class MatrixReport:
    relative: str
    root_label: str
    data: dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.root_label}/{self.relative}"

    def reference(self) -> dict[str, Any]:
        return {"root": self.root_label, "path": self.relative}

    def rows(self) -> list[dict[str, Any]]:
        cells = self.data.get("cells")
        return [row for row in cells if isinstance(row, dict)] if isinstance(cells, list) else []


@dataclass
class ScanResult:
    roots: list[EvidenceRoot]
    runs: list[RunArtifacts]
    matrices: list[MatrixReport]
    problems: list[dict[str, Any]]
    stats: dict[str, Any]
    duplicates: list[dict[str, Any]] = field(default_factory=list)

    def index_by_key(self) -> dict[str, RunArtifacts]:
        return {run.run_key: run for run in self.runs}


def derive_run_key(root_label: str, kind: str, slug: str, run_dir: str) -> str:
    digest = hashlib.sha256(run_dir.encode("utf-8")).hexdigest()[:8]
    return f"{root_label}.{kind}.{normalize_label(slug, fallback='run')}.{digest}"


_KIND_PRIORITY = ("analysis", "evidence", "app_record", "session", "native_result", "atlas_result")

#: Ordering used only when the run budget is exhausted. It never changes what a run contains.
_KIND_ORDER = {
    "unified_cell": 0,
    "cell_analysis": 1,
    "main_session": 2,
    "native_run": 3,
    "app_run": 4,
    "atlas_run": 5,
}

#: Per-kind budget. A rehearsal cell with an analysis file but no session must not push an
#: executed run out of the index, so each kind gets its own share.
_KIND_QUOTA = {
    "unified_cell": 350,
    "cell_analysis": 120,
    "main_session": 180,
    "native_run": 60,
    "app_run": 60,
    "atlas_run": 60,
}


def _evidence_weight(roles: Mapping[str, Sequence[Any]]) -> int:
    """Cheap richness hint used only to decide which candidates survive the budget."""

    return (
        (4 if roles.get("session") else 0)
        + (3 if roles.get("native_result") else 0)
        + (2 if roles.get("analysis") else 0)
        + (1 if roles.get("evidence") else 0)
    )


#: Provider or model names that mean no external model answered. A record carrying one of these is
#: a scripted/deterministic proof: it may be replayed, but it must never be merged with a live run.
SCRIPTED_MARKERS = (
    "scripted",
    "fake",
    "stub",
    "deterministic",
    "offline",
    "rehearsal",
)

#: Ordered by how specific the answer is. The first entry that is recorded names the execution.
_PRIMARY_ROLES = ("evidence", "session", "native_result", "app_record", "atlas_result", "analysis")


def provider_band(*values: Any) -> str:
    """``SCRIPTED``, ``LIVE_MODEL`` or ``UNKNOWN`` for a set of provider/model labels."""

    texts = [str(value).strip().lower() for value in values if value not in (None, "")]
    if not texts:
        return "UNKNOWN"
    if any(marker in text for marker in SCRIPTED_MARKERS for text in texts):
        return "SCRIPTED"
    return "LIVE_MODEL"


def _content_digest(payload: Any) -> str | None:
    """A digest of the record's own content: a copy keeps it, any real difference does not."""

    if not isinstance(payload, (dict, list)):
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _execution_record(payloads: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Derive the observable execution identity of one evidence directory.

    Only recorded values are used. ``provider``/``model`` come from whichever artifact states
    them; ``execution_id`` is an identity the runtime itself wrote (a session run id, a native
    identity, an app run id); ``source_digest`` is the digest the evidence records of the artifact
    it was built from. Nothing here is inferred from a directory name or a plan.
    """

    evidence = payloads.get("evidence") or {}
    analysis = payloads.get("analysis") or {}
    session = payloads.get("session") or {}
    native = payloads.get("native_result") or {}
    app = payloads.get("app_record") or {}
    atlas = payloads.get("atlas_result") or {}
    provenance = evidence.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    sandbox_plan = analysis.get("sandbox_plan")
    sandbox_plan = sandbox_plan if isinstance(sandbox_plan, dict) else {}
    native_identity = native.get("identity")
    native_identity = native_identity if isinstance(native_identity, dict) else {}

    provider = _first_text(
        provenance.get("provider"),
        session.get("provider"),
        sandbox_plan.get("provider_name"),
        native_identity.get("provider"),
        atlas.get("provider"),
    )
    model = _first_text(
        provenance.get("model"),
        session.get("model"),
        native_identity.get("model"),
        atlas.get("model"),
    )
    experiment_mode = _first_text(
        evidence.get("execution_mode"),
        (analysis.get("outcome") or {}).get("execution_mode")
        if isinstance(analysis.get("outcome"), dict)
        else None,
    )
    execution_id = _first_text(
        session.get("run_id"),
        native_identity.get("run_id"),
        app.get("run_id"),
        atlas.get("run_id"),
    )
    primary = next((role for role in _PRIMARY_ROLES if role in payloads), None)
    # Only a record with no execution evidence at all may be collapsed by the verdict it claims.
    rehearsal = primary == "analysis"
    # A rehearsal's own bytes carry timings and costs, so they never prove two copies are the same
    # result; its verdict digest does.
    content_digest: str | None = None
    if not rehearsal and primary:
        content_digest = _content_digest(payloads.get(primary))
    return {
        "provider": provider,
        "model": model,
        "band": provider_band(provider, model, provenance.get("provider")),
        "experiment_mode": experiment_mode,
        "execution_id": execution_id,
        "source_digest": _first_text(provenance.get("source_digest")),
        "content_digest": content_digest,
        "verdict_digest": _verdict_digest(payloads) if rehearsal else None,
        "primary_role": primary,
    }


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


#: Fields that state what a rehearsal *concluded*. Timing, cost and note text are excluded on
#: purpose: they differ between two runs of the same rehearsal and say nothing about the result.
_VERDICT_FIELDS = (
    "status",
    "verdict",
    "executor_verdict",
    "oracle_status",
    "oracle_passed",
    "execution_integrity",
    "goal_correctness",
    "delivered_success",
    "failure_kind",
    "agent_success",
    "static_acceptance",
    "public_test_status",
)


def _verdict_digest(payloads: Mapping[str, dict[str, Any]]) -> str | None:
    analysis = payloads.get("analysis") or {}
    outcome = analysis.get("outcome")
    if not isinstance(outcome, dict):
        return None
    oracle = analysis.get("oracle")
    claimed = {name: outcome.get(name) for name in _VERDICT_FIELDS}
    claimed["oracle_kind"] = oracle.get("kind") if isinstance(oracle, dict) else None
    canonical = json.dumps(claimed, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execution_token(execution: Mapping[str, Any]) -> str:
    """The part of the identity that separates two executions of the same planned cell.

    An execution id the runtime wrote is the strongest evidence; the digest of the artifact the
    evidence was built from is next; the digest of the record itself is last. A rehearsal that
    carries no execution evidence at all falls back to the verdict it claims, so two copies of one
    rehearsal collapse while a rehearsal with a different conclusion stays its own row.

    An empty answer means nothing distinguishes the two directories, and they are then kept apart
    rather than merged.
    """

    for key in ("execution_id", "source_digest", "content_digest", "verdict_digest"):
        value = execution.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    return ""


def _kind_for(roles: Mapping[str, Sequence[Any]]) -> str:
    if roles.get("analysis") or roles.get("evidence"):
        # An analysis without a session, a native result or cell evidence is a rehearsal record,
        # not an executed run. It stays visible under its own kind.
        executed = roles.get("session") or roles.get("native_result") or roles.get("evidence")
        return "unified_cell" if executed else "cell_analysis"
    if roles.get("app_record"):
        return "app_run"
    if roles.get("session"):
        return "main_session"
    if roles.get("native_result"):
        return "native_run"
    return "atlas_run"


def scan(roots: list[EvidenceRoot], *, max_runs: int = MAX_RUNS) -> ScanResult:
    problems: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], dict[str, list[Found]]] = {}
    declared_cell_ids: dict[tuple[str, str], dict[str, str]] = {}
    execution_payloads: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    matrices: list[MatrixReport] = []
    totals = {"files": 0, "candidates": 0, "truncated_roots": 0, "roots_scanned": 0}
    maximum_depth = 0

    for root in roots:
        if not root.path.is_dir():
            problems.append({"root": root.label, "reason": "ROOT_MISSING", "path": str(root.path)})
            continue
        totals["roots_scanned"] += 1
        walk = walk_root(root)
        totals["files"] += len(walk.files)
        totals["pruned_scratch_dirs"] = totals.get("pruned_scratch_dirs", 0) + walk.pruned
        if walk.pruned and walk.pruned_names:
            problems.append(
                {
                    "root": root.label,
                    "reason": "SCRATCH_DIRECTORIES_PRUNED",
                    "detail": f"{walk.pruned} scratch director(ies) skipped, e.g. "
                    + ", ".join(walk.pruned_names[:5]),
                    "count": walk.pruned,
                }
            )
        if walk.truncated:
            totals["truncated_roots"] += 1
            problems.append(
                {
                    "root": root.label,
                    "reason": "WALK_TRUNCATED",
                    "detail": f"stopped after {walk.visited} entries",
                }
            )
        reader = EvidenceReader(root)
        for relative, size in walk.files:
            classified = _classify(reader, relative, size)
            if classified is None:
                continue
            role, payload = classified
            totals["candidates"] += 1
            if role == "malformed":
                problems.append(
                    {"root": root.label, "reason": "MALFORMED_EVIDENCE", "path": relative}
                )
                continue
            if role == "matrix_report":
                assert isinstance(payload, dict)
                matrices.append(
                    MatrixReport(relative=relative, root_label=root.label, data=payload)
                )
                continue
            if role == "evidence_index":
                # A recognised root-level integrity index. It is not a run artifact, so it is
                # not projected as one; the run list stays free of duplicate rows.
                continue
            owner = _owner(relative.split("/"))
            if owner is None:
                problems.append(
                    {"root": root.label, "reason": "UNSUPPORTED_LAYOUT", "path": relative}
                )
                continue
            parts, artifact_role = owner
            if role in {"native_result", "atlas_result"}:
                # The classifier distinguishes a native result from a workflow DAG result;
                # the path layout alone cannot.
                artifact_role = role
            run_dir = "/".join(parts)
            maximum_depth = max(maximum_depth, len(parts))
            key = (root.label, run_dir)
            bucket = grouped.setdefault(key, {})
            bucket.setdefault(artifact_role, []).append(Found(artifact_role, relative, size))
            if isinstance(payload, dict):
                payloads = execution_payloads.setdefault(key, {})
                payloads.setdefault(artifact_role, payload)
            if artifact_role in {"analysis", "evidence"} and isinstance(payload, dict):
                declared = declared_cell_ids.setdefault(key, {})
                for field_name in ("cell_id", "plan_id", "batch_id"):
                    value = payload.get(field_name)
                    if isinstance(value, str) and value and not declared.get(field_name):
                        declared[field_name] = value

    runs: list[RunArtifacts] = []
    weights: dict[tuple[str, str], int] = {}
    for (root_label, run_dir), roles in grouped.items():
        primary = [role for role in _KIND_PRIORITY if roles.get(role)]
        if not primary:
            if set(roles) <= {"patch"}:
                # A lone workspace export is an artifact of a run recorded elsewhere, not a
                # directory that is missing its evidence.
                continue
            problems.append(
                {
                    "root": root_label,
                    "reason": "INCOMPLETE_EVIDENCE",
                    "path": run_dir,
                    "detail": "supporting evidence without a run identity or an analysis file",
                }
            )
            continue
        kind = _kind_for(roles)
        weights[(root_label, run_dir)] = _evidence_weight(roles)
        declared = declared_cell_ids.get((root_label, run_dir)) or {}
        cell_id = declared.get("cell_id")
        execution = _execution_record(execution_payloads.get((root_label, run_dir), {}))
        # A planned cell is one logical unit, but the plan is not the execution: the same
        # (cell_id, plan_id) can be a scripted proof, an offline rehearsal and several live runs.
        # The pair therefore names the *logical* cell only; the execution token below decides
        # whether two directories are the same piece of evidence.
        identity = (cell_id, declared.get("plan_id", "")) if cell_id else None
        runs.append(
            RunArtifacts(
                run_key="",
                root_label=root_label,
                run_dir=run_dir,
                kind=kind,
                cell_id=cell_id,
                cell_identity=identity,
                files={
                    role: [item.relative for item in entries] for role, entries in roles.items()
                },
                execution=execution,
            )
        )

    runs = _merge_descendants(runs)

    cell_rows: dict[str, tuple[dict[str, Any], MatrixReport]] = {}
    for matrix in matrices:
        for row in matrix.rows():
            cell_id = row.get("cell_id")
            if isinstance(cell_id, str) and cell_id:
                cell_rows.setdefault(cell_id, (row, matrix))

    matched_rows: set[str] = set()
    for run in runs:
        cell_id = run.cell_id
        if cell_id and cell_id in cell_rows:
            row, matrix = cell_rows[cell_id]
            run.matrix_row = row
            run.matrix_ref = matrix.reference()
            matched_rows.add(cell_id)
        slug = normalize_label(cell_id or Path(run.run_dir).name, fallback="run")
        run.run_key = derive_run_key(run.root_label, run.kind, slug, run.run_dir)

    # One logical cell, one run row: a rehearsal directory and an execution directory that carry
    # the same cell identity are copies, not two runs. The richest copy is projected and every
    # other location is published under /api/replay/sources, so nothing is silently dropped.
    duplicates: list[dict[str, Any]] = []
    canonical: dict[tuple[str, ...], RunArtifacts] = {}
    kept: list[RunArtifacts] = []
    for run in sorted(
        runs,
        key=lambda item: (
            -weights.get((item.root_label, item.run_dir), 0),
            len(item.run_dir),
            item.root_label,
            item.run_dir,
        ),
    ):
        identity = run.cell_identity
        if identity is None:
            kept.append(run)
            continue
        token = execution_token(run.execution)
        if not token:
            # Nothing distinguishes this directory from any other copy of the cell. Two records
            # that cannot be proven to be the same execution stay separate.
            kept.append(run)
            continue
        key = (identity[0], identity[1], str(run.execution.get("band") or "UNKNOWN"), token)
        first = canonical.get(key)
        if first is None:
            canonical[key] = run
            kept.append(run)
            continue
        duplicates.append(
            {
                "cell_id": run.cell_id,
                "canonical": f"{first.root_label}/{first.run_dir}",
                "copy": f"{run.root_label}/{run.run_dir}",
                "proof": token.split(":", 1)[0],
                "provider": run.execution.get("provider"),
            }
        )
    runs = kept

    # Richer evidence wins the budget: a cell that actually executed beats a preflight rehearsal
    # cell that only wrote an analysis file. The per-kind quota keeps every source visible.
    runs.sort(
        key=lambda item: (
            _KIND_ORDER.get(item.kind, 9),
            -weights.get((item.root_label, item.run_dir), 0),
            item.root_label,
            item.run_dir,
        )
    )
    considered = len(runs)
    selected: list[RunArtifacts] = []
    used: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for run in runs:
        quota = _KIND_QUOTA.get(run.kind, 50)
        if used.get(run.kind, 0) < quota and len(selected) < max_runs:
            selected.append(run)
            used[run.kind] = used.get(run.kind, 0) + 1
        else:
            dropped[run.kind] = dropped.get(run.kind, 0) + 1
    runs = selected
    if dropped:
        problems.append(
            {
                "reason": "RUN_LIMIT",
                "detail": (
                    f"{considered} run candidates; "
                    + ", ".join(f"{kind}: {count} not projected" for kind, count in dropped.items())
                ),
                "count": sum(dropped.values()),
            }
        )

    unmatched = [
        {"matrix": matrix.label, "cell_id": row.get("cell_id")}
        for matrix in matrices
        for row in matrix.rows()
        if isinstance(row.get("cell_id"), str) and row["cell_id"] not in matched_rows
    ]
    if unmatched:
        problems.append(
            {
                "reason": "MATRIX_ROW_WITHOUT_EVIDENCE",
                "detail": f"{len(unmatched)} matrix rows have no cell evidence directory",
                "count": len(unmatched),
            }
        )

    problem_count = len(problems)
    if problem_count > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS]
        problems.append(
            {
                "reason": "PROBLEMS_TRUNCATED",
                "detail": f"{problem_count} discovery problems were recorded",
            }
        )
    stats = {
        **totals,
        "runs": len(runs),
        "runs_considered": considered,
        "matrices": len(matrices),
        "matrix_rows": sum(len(matrix.rows()) for matrix in matrices),
        "matrix_rows_matched": len(matched_rows),
        "max_run_depth": maximum_depth,
        "problem_count": problem_count,
        "duplicate_cells": len(duplicates),
    }
    return ScanResult(
        roots=roots,
        runs=runs,
        matrices=matrices,
        problems=problems,
        stats=stats,
        duplicates=duplicates,
    )


def _merge_descendants(runs: list[RunArtifacts]) -> list[RunArtifacts]:
    """A native result under a cell, or a DAG result under an app run, is one run."""

    ordered = sorted(runs, key=lambda item: (item.root_label, len(item.run_dir), item.run_dir))
    kept: list[RunArtifacts] = []
    for candidate in ordered:
        parent = _ancestor(candidate, kept)
        if parent is None:
            kept.append(candidate)
            continue
        for role, paths in candidate.files.items():
            bucket = parent.files.setdefault(role, [])
            for path in paths:
                if path not in bucket:
                    bucket.append(path)
        if parent.kind == "atlas_run":
            parent.kind = candidate.kind
    return kept


def _ancestor(candidate: RunArtifacts, kept: list[RunArtifacts]) -> RunArtifacts | None:
    for other in kept:
        if other.root_label != candidate.root_label:
            continue
        if other.run_dir == candidate.run_dir:
            continue
        if candidate.run_dir.startswith(other.run_dir + "/"):
            return other
    return None


def iter_roots(roots: list[EvidenceRoot]) -> Iterator[tuple[EvidenceRoot, EvidenceReader]]:
    for root in roots:
        yield root, EvidenceReader(root)
