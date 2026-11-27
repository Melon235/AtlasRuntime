"""Thin application service shared by the AtlasOS CLI and Web UI.

The service validates inputs, launches existing execution paths, projects their persisted
results, and serves events. It does not schedule tasks, execute tools, approve actions, or own
recovery state. The authoritative stores and runtimes remain below this layer.

Four execution modes share it: ``workflow`` (the static two-agent DAG), ``coding`` (the UAT
composition), and the two Main-driven modes ``main_direct`` and ``main_dynamic``. The last two call
:func:`atlasos.app.main_driven.run_goal` in-process, so the product entry point and the research
entry point are one runtime, not two.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from atlasos.app.deterministic import MODEL_NAME as DETERMINISTIC_MODEL
from atlasos.app.deterministic import PROVIDER_NAME as DETERMINISTIC_PROVIDER
from atlasos.app.deterministic import delegated_script, direct_script
from atlasos.app.deterministic import factory as deterministic_factory
from atlasos.app.workspace import (
    WorkspaceError,
    describe_roots,
    materialise,
    resolve_seed,
)
from atlasos.config import (
    AtlasConfig,
    ContextConfig,
    ContextTransformConfig,
    PlacementConfig,
    Pricing,
    ProviderConfig,
    ToolsConfig,
)
from atlasos.context.requirements import ContextRequirement
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "experiments" / "fixtures"
DEFAULT_CODING_WORKSPACE = DEFAULT_WORKSPACE_ROOT / "uat_repo"

#: The two Main-driven product modes. ``main_direct`` builds no delegation tool at all;
#: ``main_dynamic`` offers ``delegate_tasks`` and the router decides whether a request is admitted.
MAIN_DIRECT = "main_direct"
MAIN_DYNAMIC = "main_dynamic"
MAIN_MODES = frozenset({MAIN_DIRECT, MAIN_DYNAMIC})
EXECUTION_MODES = frozenset({"workflow", "coding", *MAIN_MODES})

DEFAULT_BUDGET_TOKENS = 500_000
MAX_BUDGET_TOKENS = 2_000_000
MAX_MAIN_TURNS = 40
DEFAULT_MAIN_TURNS = 16
DEFAULT_CHILD_MAX_TURNS = 12
DEFAULT_SANDBOX_IMAGE = "atlas-uat-python:3.12"

TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
    "RECOVERY_REQUIRED",
    "RECOVERY_UNCERTAIN",
}


class ApplicationError(RuntimeError):
    """Invalid application request; the API maps this to 4xx."""

    status_code = 400


class WorkspaceNotSupported(ApplicationError):
    status_code = 422


class ApprovalAuthorityUnavailable(ApplicationError):
    status_code = 409


class RecoveryAuthorityUnavailable(ApplicationError):
    status_code = 409


class ReadOnlyMode(ApplicationError):
    """The server was started as a read-only workbench; mutating routes are refused."""

    status_code = 403


class ConcurrentRunRejected(ApplicationError):
    """The first version of the product path runs one Main-driven task at a time."""

    status_code = 409


class CancelUnavailable(ApplicationError):
    """Cancellation would be a UI-only state change; the service refuses to fake it."""

    status_code = 409


class WorkspaceRejected(ApplicationError):
    """The requested workspace is outside every allowed root."""

    status_code = 422


@dataclass
class RunRecord:
    run_id: str
    mode: str
    task: str
    workspace: str | None
    created_at: str
    status: str = "SUBMITTED"
    updated_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    session: dict[str, Any] | None = None
    error: str | None = None
    failure_class: str | None = None
    atlas_run_id: str | None = None
    uat_run_id: str | None = None
    cleanup_error: str | None = None
    recovery_uncertain: bool = False
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: What the Host actually ran: goal, mode, provider, model, budget, limits, seed digest.
    config: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    #: True when the record was read from disk in this process, so no thread of ours owns it.
    orphaned: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_int(value: str | None) -> int | None:
    """Parse an optional positive integer from the environment; anything else means "unset"."""

    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _main_metrics(session: dict[str, Any]) -> dict[str, Any]:
    """The goal session's usage in the same shape the application already projects.

    A component that was not reported stays ``None``: an unknown child count is not a zero.
    """

    raw = _as_dict(session.get("metrics"))
    main_block = _as_dict(session.get("main"))

    def _sum(*values: Any) -> int | None:
        known = [int(value) for value in values if isinstance(value, int)]
        return sum(known) if known else None

    prompt = _sum(raw.get("main_cumulative_input_tokens"), raw.get("child_total_prompt_tokens"))
    completion = _sum(raw.get("main_completion_tokens"), raw.get("child_completion_tokens"))
    return {
        **raw,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": raw.get("main_cache_hit_tokens"),
        "cache_miss_tokens": raw.get("main_cache_miss_tokens"),
        "system_total_tokens": raw.get("system_total_tokens"),
        "main_prompt_tokens": raw.get("main_cumulative_input_tokens"),
        "child_prompt_tokens": raw.get("child_total_prompt_tokens"),
        "tool_calls": len(main_block.get("tool_calls") or []),
        "delegation_rounds": raw.get("delegation_rounds"),
        "max_observed_child_concurrency": raw.get("max_observed_child_concurrency"),
    }


def _goal_session_path(run_dir: Path, config: Any) -> Path | None:
    """Where a Main-driven run wrote its session: the recorded id, else the only ``goal*`` dir."""

    recorded = _as_dict(config).get("runtime_run_id")
    if isinstance(recorded, str) and recorded:
        return run_dir / recorded / "session.json"
    matches = sorted(run_dir.glob("goal*/session.json"))
    return matches[0] if matches else None


def _nested_text(payload: Any, *path: str) -> str:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return str(value or "")


def _describe_failure(error: BaseException) -> str:
    """A message a reader can act on, including ``SystemExit("...")`` raised by ``run_goal``."""

    message = str(error) if not isinstance(error, SystemExit) else str(error.code or "")
    name = type(error).__name__
    return f"{name}: {message}" if message else name


def _runtime_summary(event_type: str, data: dict[str, Any]) -> str:
    """One line for a transcript event, using only fields the recorder actually wrote."""

    if event_type == "action_proposed":
        return f"proposed {data.get('tool_name') or data.get('tool_id') or 'tool'}"
    if event_type == "action_decision":
        return f"{data.get('decision')} {data.get('tool_id')}"
    if event_type == "tool_observation":
        state = "error" if data.get("is_error") else "ok"
        return f"tool observation {state}"
    if event_type == "execution_outcome":
        return f"execution {data.get('state')} for {data.get('tool_id')}"
    if event_type == "sandbox_lifecycle":
        return f"sandbox {data.get('state')}"
    if event_type == "worker_lifecycle":
        return f"worker {data.get('mode')} {data.get('worker_id') or ''}".strip()
    return event_type


class _TranscriptTail(threading.Thread):
    """Project the runtime's own trajectory transcript into application events.

    The recorder appends and flushes one JSON object per line, so this is a real observation seam
    and not a poll of a summary: every event it emits was written by the runtime while the run was
    happening. It reads only the run's own directory, keeps a byte offset per file, and never
    writes anything.
    """

    def __init__(self, *, run_root: Path, emit: Any, interval: float = 0.4) -> None:
        super().__init__(name=f"atlas-tail-{run_root.name}", daemon=True)
        self.run_root = run_root
        self.emit = emit
        self.interval = interval
        self._stop = threading.Event()
        self._offsets: dict[str, int] = {}
        self._seen: set[tuple[str, int]] = set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - thread body, exercised by the E2E run
        while True:
            self._drain()
            if self._stop.wait(self.interval):
                self._drain()
                return

    def _drain(self) -> None:
        if not self.run_root.is_dir():
            return
        # The goal run lives in ``<app>/goal-<app id>/``; the glob keeps this working if the
        # directory naming ever changes again.
        paths = sorted(self.run_root.glob("goal*/trajectory/*/transcript.jsonl"))
        for path in paths:
            try:
                with path.open("rb") as handle:
                    offset = self._offsets.get(str(path), 0)
                    handle.seek(offset)
                    chunk = handle.read()
                    self._offsets[str(path)] = handle.tell()
            except OSError:
                continue
            for raw in chunk.splitlines():
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload")
                envelope = payload.get("atlas_trajectory") if isinstance(payload, dict) else None
                if not isinstance(envelope, dict):
                    continue
                seq = row.get("seq")
                key = (str(path), int(seq) if isinstance(seq, int) else 0)
                if key in self._seen:
                    continue
                self._seen.add(key)
                event_type = str(envelope.get("event_type") or "")
                if not event_type:
                    continue
                correlation = envelope.get("correlation")
                correlation = correlation if isinstance(correlation, dict) else {}
                data = envelope.get("data")
                data = data if isinstance(data, dict) else {}
                self.emit(
                    "runtime_event",
                    {
                        "event_type": event_type,
                        "summary": _runtime_summary(event_type, data),
                        "agent_id": correlation.get("worker_id"),
                        "task_id": correlation.get("task_id"),
                        "seq": seq,
                        "occurred_at": envelope.get("occurred_at") or row.get("ts"),
                    },
                )


def _pricing() -> Pricing:
    return Pricing(
        cache_hit_input_per_million=0.006,
        cache_miss_input_per_million=0.300,
        output_per_million=1.200,
        snapshot_date=datetime(2026, 11, 9).date(),
    )


def _workflow(task: str) -> AtlasWorkflow:
    return AtlasWorkflow(
        name="atlas-app",
        agents=(
            AtlasAgentSpec(
                name="researcher",
                role="researcher",
                system_prompt=(
                    "You are a precise engineering researcher. Produce a compact plan grounded "
                    "only in the request. Do not claim to have run code."
                ),
                capabilities=frozenset({"research"}),
            ),
            AtlasAgentSpec(
                name="reviewer",
                role="reviewer",
                system_prompt=(
                    "You review the researcher's plan and produce the final answer. Surface "
                    "assumptions and unresolved risks. Never invent execution evidence."
                ),
                capabilities=frozenset({"review"}),
            ),
        ),
        tasks=(
            AtlasTaskSpec(
                task_id="research",
                title="research",
                description=(
                    "Analyze the following request and produce a concise plan with concrete "
                    f"steps:\n\n{task}"
                ),
                assignee="researcher",
                required_capabilities=frozenset({"research"}),
            ),
            AtlasTaskSpec(
                task_id="review",
                title="review",
                description=(
                    "Review the dependency output. Produce the final answer for the user, "
                    "including assumptions and risks. Do not invent tool output."
                ),
                assignee="reviewer",
                depends_on=("research",),
                required_capabilities=frozenset({"review"}),
                required_context=(ContextRequirement("dep:research", source_task_id="research"),),
            ),
        ),
    )


class AtlasApplicationService:
    def __init__(
        self,
        *,
        runs_root: str | Path,
        workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
        repo_root: str | Path = REPO_ROOT,
        coding_workspace: str | Path = DEFAULT_CODING_WORKSPACE,
        sandbox_provider: str = "opensandbox",
        provider: str = "deepseek",
        sandbox_enabled: bool = True,
        sandbox_image: str | None = None,
        workspace_roots: tuple[str | Path, ...] | None = None,
        failure_after_request: int | None = None,
    ) -> None:
        self.runs_root = Path(runs_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.coding_workspace = Path(coding_workspace).resolve()
        self.sandbox_provider = sandbox_provider
        #: ``deepseek`` is the only live provider this stage supports. ``deterministic`` is the
        #: offline seam: it builds a scripted adapter instead of a network client, and every record
        #: it produces names that provider. A request body can never choose it.
        self.provider = provider
        self.sandbox_enabled = sandbox_enabled
        self.sandbox_image = (
            sandbox_image or os.environ.get("ATLASOS_SANDBOX_IMAGE") or DEFAULT_SANDBOX_IMAGE
        )
        self._workspace_roots = (
            tuple(Path(root).expanduser().resolve() for root in workspace_roots)
            if workspace_roots
            else None
        )
        #: Test/demo seam for the deterministic provider only: make request N raise. A live
        #: provider never consults it, and the value is recorded in the run configuration.
        self.failure_after_request = failure_after_request
        #: Demo pacing for the deterministic provider (seconds per answer). It changes nothing
        #: about the runtime; without it a scripted run finishes before a human can watch it.
        self.provider_step_delay = float(os.environ.get("ATLASOS_DETERMINISTIC_DELAY") or 0.0)
        #: Optional run-scoped token budget for coding runs. Unset keeps the historical behaviour;
        #: this is the explicit switch required before the shared budget applies to the app path.
        self.coding_token_budget = _optional_int(os.environ.get("ATLASOS_CODING_TOKEN_BUDGET"))
        self._records: dict[str, RunRecord] = {}
        self.runs_root.mkdir(parents=True, exist_ok=True)
        load_dotenv(self.repo_root / ".env", override=False)

    # ---------------------------------------------------------------- submission
    def submit(
        self,
        task: str,
        *,
        mode: str = "workflow",
        workspace: str | None = None,
        max_turns: int = 12,
        budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        task = task.strip()
        if not task:
            raise ApplicationError("task must not be empty")
        if mode not in EXECUTION_MODES:
            raise ApplicationError("mode must be one of " + ", ".join(sorted(EXECUTION_MODES)))
        if mode in MAIN_MODES:
            return self._submit_main(
                task,
                mode=mode,
                workspace=workspace,
                max_turns=max_turns,
                budget_tokens=budget_tokens,
            )
        if not (1 <= max_turns <= 30):
            raise ApplicationError("max_turns must be between 1 and 30")
        selected: str | None = None
        if mode == "coding":
            selected = str(self._resolve_workspace(workspace))
        run_id = f"app-{uuid.uuid4().hex[:12]}"
        record = RunRecord(
            run_id=run_id,
            mode=mode,
            task=task,
            workspace=selected,
            created_at=_now(),
            updated_at=_now(),
            status="RUNNING",
        )
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._records[run_id] = record
        self._persist(record)
        thread = threading.Thread(
            target=self._execute,
            args=(record, max_turns),
            name=f"atlas-app-{run_id}",
            daemon=True,
        )
        record.thread = thread
        thread.start()
        return self.summary(record)

    # ------------------------------------------------------------ Main-driven modes
    def _submit_main(
        self,
        task: str,
        *,
        mode: str,
        workspace: str | None,
        max_turns: int,
        budget_tokens: int | None,
    ) -> dict[str, Any]:
        if not (1 <= max_turns <= MAX_MAIN_TURNS):
            raise ApplicationError(f"max_turns must be between 1 and {MAX_MAIN_TURNS}")
        budget = DEFAULT_BUDGET_TOKENS if budget_tokens is None else int(budget_tokens)
        if not (1 <= budget <= MAX_BUDGET_TOKENS):
            raise ApplicationError(
                f"budget_tokens must be between 1 and {MAX_BUDGET_TOKENS}; "
                "an unbounded run is not offered"
            )
        if not workspace:
            raise ApplicationError("a Main-driven run requires a workspace name")
        try:
            seed = resolve_seed(workspace, roots=self._workspace_roots)
        except WorkspaceError as error:
            raise WorkspaceRejected(str(error)) from error
        active = self.active_main_run()
        if active is not None:
            raise ConcurrentRunRejected(
                f"run {active} is still active; this version runs one Main-driven task at a time"
            )
        provider = self.provider
        deterministic = provider == DETERMINISTIC_PROVIDER
        config: dict[str, Any] = {
            "goal": task,
            "mode": mode,
            "workspace": seed.as_dict(),
            "provider": provider,
            "provider_kind": "DETERMINISTIC" if deterministic else "LIVE_MODEL",
            "model": DETERMINISTIC_MODEL if deterministic else "deepseek-chat",
            "budget_tokens": budget,
            "max_turns": max_turns,
            "delegation": {
                "enabled": mode == MAIN_DYNAMIC,
                "max_parallel_children": 2,
                "max_children_per_call": 2,
                "child_max_turns": DEFAULT_CHILD_MAX_TURNS,
            },
            "sandbox": (
                {"enabled": True, "provider": self.sandbox_provider, "image": self.sandbox_image}
                if self.sandbox_enabled
                else {"enabled": False, "provider": None, "image": None}
            ),
        }
        run_id = f"app-{uuid.uuid4().hex[:12]}"
        record = RunRecord(
            run_id=run_id,
            mode=mode,
            task=task,
            workspace=str(seed.source),
            created_at=_now(),
            updated_at=_now(),
            status="SUBMITTED",
            config=config,
        )
        (self.runs_root / run_id).mkdir(parents=True, exist_ok=False)
        self._records[run_id] = record
        self._persist(record)
        self._event(
            record,
            "run_submitted",
            {
                "mode": mode,
                "workspace": seed.name,
                "budget_tokens": budget,
                "provider": provider,
                "provider_kind": config["provider_kind"],
                "max_turns": max_turns,
            },
        )
        thread = threading.Thread(
            target=self._execute_main,
            args=(record,),
            name=f"atlas-app-{run_id}",
            daemon=True,
        )
        record.thread = thread
        thread.start()
        return self.summary(record)

    def active_main_run(self) -> str | None:
        """The id of the Main-driven run currently holding the single execution slot."""

        for record in list(getattr(self, "_records", {}).values()):
            if record.mode not in MAIN_MODES:
                continue
            if record.status in TERMINAL:
                continue
            if record.thread is not None and record.thread.is_alive():
                return record.run_id
        return None

    def _resolve_workspace(self, workspace: str | None) -> Path:
        if not workspace:
            candidate = self.coding_workspace
        else:
            path = Path(workspace)
            candidate = path if path.is_absolute() else self.workspace_root / path
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise WorkspaceNotSupported(f"workspace is not a directory: {candidate}")
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as error:
            raise WorkspaceNotSupported(f"workspace must be under {self.workspace_root}") from error
        if not (candidate / "csv_parser.py").is_file():
            raise WorkspaceNotSupported(
                "coding mode currently supports UAT-compatible workspaces with csv_parser.py"
            )
        return candidate

    def _execute(self, record: RunRecord, max_turns: int) -> None:
        try:
            self._event(record, "run_submitted", {"mode": record.mode, "task": record.task})
            if record.mode == "workflow":
                self._execute_workflow(record)
            else:
                self._execute_coding(record, max_turns)
        except Exception as error:  # noqa: BLE001 - surfaced as run failure
            record.error = f"{type(error).__name__}: {error}"
            record.status = "FAILED"
            record.failure_class = "APPLICATION_ERROR"
            self._event(record, "run_failed", {"error": record.error})
        finally:
            record.updated_at = _now()
            self._persist(record)

    def _execute_workflow(self, record: RunRecord) -> None:
        atlas_root = self.runs_root / record.run_id / "atlas"
        atlas_root.mkdir(parents=True, exist_ok=True)

        def hook(event: Any) -> None:
            data = getattr(event, "data", None)
            payload: dict[str, Any] = {"event_type": str(event.type)}
            if data is not None:
                payload["title"] = getattr(data, "title", None)
                payload["assignee"] = getattr(data, "assignee", None)
            self._event(record, "runtime_event", payload)

        config = AtlasConfig(
            provider=ProviderConfig(model="deepseek-chat"),
            context=ContextConfig(
                policy="task_scoped",
                renderer="stable_deterministic",
                transform=ContextTransformConfig(name="identity"),
            ),
            placement=PlacementConfig(policy="bypass"),
            pricing=_pricing(),
        )
        runtime = AtlasRuntime(
            config,
            runs_root=atlas_root,
            event_hook=hook,
            repo_root=self.repo_root,
        )
        result = asyncio.run(
            runtime.run(_workflow(record.task), experiment="atlas-app", persist=True)
        )
        record.atlas_run_id = result.run_id
        record.result = result.model_dump(mode="json")
        record.status = "COMPLETED" if result.status == "succeeded" else "FAILED"
        if result.status != "succeeded":
            record.failure_class = "RUNTIME_FAILED"
        self._event(record, "run_completed" if result.status == "succeeded" else "run_failed", {})
        self._persist(record)

    def _execute_coding(self, record: RunRecord, max_turns: int) -> None:
        coding_root = self.runs_root / record.run_id / "coding"
        coding_root.mkdir(parents=True, exist_ok=True)
        self._event(record, "coding_started", {"workspace": record.workspace})
        # The coding composition lives in the runtime package; it is called in-process so the
        # application never depends on the evaluation harness.
        from atlasos.app.coding import CodingRunOptions, run_coding_session

        if record.workspace is None:
            raise WorkspaceNotSupported("coding mode requires a workspace directory")
        options = CodingRunOptions(
            task=record.task,
            fixture=Path(record.workspace),
            runs_root=coding_root,
            provider="deepseek",
            model="deepseek-chat",
            max_turns=max_turns,
            token_budget=self.coding_token_budget,
            sandbox_provider=self.sandbox_provider,
            require_real_sandbox=True,
        )
        session, _exit_code = asyncio.run(run_coding_session(options))
        record.session = session
        record.uat_run_id = str(session.get("run_id") or "")
        record.cleanup_error = (session.get("sandbox") or {}).get("cleanup_error")
        failure_class = str(session.get("failure_class") or "")
        if "SANDBOX_LOST" in failure_class.upper():
            record.status = "SANDBOX_LOST"
        elif record.cleanup_error:
            record.status = "CLEANUP_UNRESOLVED"
        elif session.get("status") == "completed":
            record.status = "COMPLETED"
        else:
            record.status = "FAILED"
        record.failure_class = failure_class or None
        record.error = session.get("failure_message")
        self._event(
            record,
            "run_completed" if record.status == "COMPLETED" else "run_failed",
            {"status": record.status, "failure_class": record.failure_class},
        )
        self._persist(record)

    # --------------------------------------------------------- Main-driven execution
    def _execute_main(self, record: RunRecord) -> None:
        """Run one goal through ``run_goal`` on a worker thread and project what happened."""

        tail = _TranscriptTail(
            run_root=self.runs_root / record.run_id,
            emit=lambda event_type, data: self._event(record, event_type, data),
        )
        try:
            record.status = "RUNNING"
            record.started_at = _now()
            self._event(record, "run_started", {"mode": record.mode})
            tail.start()
            self._run_main(record)
        except BaseException as error:  # noqa: BLE001 - SystemExit is a real failure here
            record.error = _describe_failure(error)
            record.status = "FAILED"
            record.failure_class = record.failure_class or "APPLICATION_ERROR"
            self._event(record, "run_failed", {"error": record.error})
        finally:
            tail.stop()
            record.finished_at = _now()
            record.updated_at = record.finished_at
            if record.status not in TERMINAL:
                record.status = "INTERRUPTED"
                self._event(
                    record,
                    "run_failed",
                    {"error": "the execution thread ended without a terminal status"},
                )
            self._persist(record)

    def _run_main(self, record: RunRecord) -> None:
        import atlasos.app.main_driven as main_driven
        from atlasos.app.goal_sandbox import GoalSandboxOptions
        from atlasos.delegation import DelegationConfig

        config = record.config or {}
        run_id = record.run_id
        # One runtime run id per application run. ``run_goal`` refuses to create an existing run
        # directory and derives its process-wide provider name from this id, so reusing a constant
        # would both mislabel the evidence and break the second run in the same process.
        runtime_run_id = f"goal-{run_id}"
        config["runtime_run_id"] = runtime_run_id
        record.config = config
        app_root = self.runs_root / run_id
        seed = resolve_seed(config["workspace"]["name"], roots=self._workspace_roots)
        if seed.as_dict() != config["workspace"]:
            raise WorkspaceRejected("the workspace changed after submission")
        workspace_path = app_root / "workspace"
        seed_record = materialise(seed, workspace_path)
        self._event(
            record,
            "workspace_ready",
            {
                "name": seed_record["name"],
                "commit": seed_record["commit"],
                "files": seed_record["files"],
                "seed_digest": seed_record["seed_digest"],
            },
        )
        sandbox_config = config.get("sandbox") or {}
        sandboxed = bool(sandbox_config.get("enabled"))
        deterministic = self.provider == DETERMINISTIC_PROVIDER
        # Workspace-relative POSIX paths: the tool security policy resolves them against the
        # workspace root and the ownership policy only understands relative paths. An absolute
        # host path would be refused (correctly) before the write ever reaches the sandbox.
        if record.mode == MAIN_DYNAMIC:
            script = delegated_script(
                child_paths={
                    "alpha": "solution/alpha.txt",
                    "beta": "solution/beta.txt",
                },
                main_entry_path="solution/integration.txt",
            )
        else:
            script = direct_script(write_path="solution/answer.txt")
        if deterministic and self.failure_after_request:
            script.fail_from_request = int(self.failure_after_request)
            config["failure_after_request"] = script.fail_from_request
            record.config = config
        if deterministic and self.provider_step_delay > 0:
            script.step_delay_seconds = self.provider_step_delay
            config["provider_step_delay_seconds"] = self.provider_step_delay
            record.config = config
        provider_factory = (
            deterministic_factory(script, run_id=runtime_run_id) if deterministic else None
        )
        delegation_config = config.get("delegation") or {}
        sandbox_options = (
            GoalSandboxOptions(
                provider=str(sandbox_config.get("provider") or self.sandbox_provider),
                source_repo=workspace_path,
                image=sandbox_config.get("image") or self.sandbox_image,
                workspace_root=main_driven.SANDBOX_WORKSPACE_ROOT,
                require_real=True,
            )
            if sandboxed
            else None
        )
        write_scope = tuple(seed.write_scope)
        options = main_driven.GoalRunOptions(
            goal=record.task,
            runs_root=app_root,
            run_id=runtime_run_id,
            workspace_root=workspace_path,
            config=AtlasConfig(
                provider=ProviderConfig(model=str(config.get("model") or "deepseek-chat")),
                tools=ToolsConfig(mode="progressive"),
                context=ContextConfig(policy="upstream_default"),
                placement=PlacementConfig(policy="bypass"),
                pricing=_pricing(),
            ),
            delegation=DelegationConfig(
                enabled=bool(delegation_config.get("enabled")),
                max_parallel_children=int(delegation_config.get("max_parallel_children") or 2),
                max_children_per_call=int(delegation_config.get("max_children_per_call") or 2),
                child_max_turns=int(
                    delegation_config.get("child_max_turns") or DEFAULT_CHILD_MAX_TURNS
                ),
            ),
            provider=str(config.get("provider") or "deepseek"),
            model=str(config.get("model") or "deepseek-chat"),
            max_turns=int(config.get("max_turns") or DEFAULT_MAIN_TURNS),
            token_budget=int(config["budget_tokens"]),
            main_write_paths=write_scope,
            child_write_grants={"coding_worker": write_scope}
            if record.mode == MAIN_DYNAMIC
            else {},
            provider_factory=provider_factory,
            sandbox=sandbox_options,
            preflight_profile="product",
        )
        session = asyncio.run(main_driven.run_goal(options))
        record.session = session
        if deterministic:
            config["provider_evidence"] = script.as_dict()
            record.config = config
        failure = _as_dict(session.get("failure"))
        record.failure_class = str(failure.get("kind") or "") or None
        record.error = str(failure.get("message") or "") or None
        succeeded = session.get("status") == "succeeded"
        record.status = "COMPLETED" if succeeded else "FAILED"
        record.result = {
            "run_id": run_id,
            "runtime_run_id": session.get("run_id"),
            "status": record.status,
            "execution_succeeded": session.get("execution_succeeded"),
            "final_output": _nested_text(session, "main", "output"),
            "metrics": session.get("metrics") or {},
            "acceptance": session.get("acceptance"),
            "isolation": session.get("isolation"),
        }
        self._event(
            record,
            "run_completed" if succeeded else "run_failed",
            {
                "status": record.status,
                "failure_class": record.failure_class,
                "execution_succeeded": session.get("execution_succeeded"),
                "budget": session.get("budget"),
            },
        )

    # ---------------------------------------------------------------- projections
    def _event(self, record: RunRecord, event_type: str, data: dict[str, Any]) -> None:
        with record.lock:
            event = {
                "id": len(record.events) + 1,
                "type": event_type,
                "timestamp": _now(),
                "data": data,
            }
            record.events.append(event)
            path = self.runs_root / record.run_id / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        record.updated_at = event["timestamp"]

    def _persist(self, record: RunRecord) -> None:
        path = self.runs_root / record.run_id / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": record.run_id,
            "mode": record.mode,
            "task": record.task,
            "workspace": record.workspace,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "status": record.status,
            "error": record.error,
            "failure_class": record.failure_class,
            "atlas_run_id": record.atlas_run_id,
            "uat_run_id": record.uat_run_id,
            "cleanup_error": record.cleanup_error,
            "recovery_uncertain": record.recovery_uncertain,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "config": record.config,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if record.result is not None:
            (self.runs_root / record.run_id / "result.json").write_text(
                json.dumps(record.result, indent=2, sort_keys=True), encoding="utf-8"
            )
        # A Main-driven run keeps its session in ``goal/session.json``: the runtime wrote it inside
        # its own run directory, and copying it here would create a second source of the same fact.
        if record.session is not None and record.mode not in MAIN_MODES:
            (self.runs_root / record.run_id / "session.json").write_text(
                json.dumps(record.session, indent=2, sort_keys=True), encoding="utf-8"
            )

    def _load(self, run_id: str) -> RunRecord:
        for record in getattr(self, "_records", {}).values():
            if record.run_id == run_id:
                self._refresh(record)
                return record
        path = self.runs_root / run_id / "run.json"
        if not path.is_file():
            raise ApplicationError(f"unknown run: {run_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = RunRecord(
            run_id=payload["run_id"],
            mode=payload["mode"],
            task=payload["task"],
            workspace=payload.get("workspace"),
            created_at=payload["created_at"],
            updated_at=payload.get("updated_at") or payload["created_at"],
            status=payload.get("status") or "FAILED",
            error=payload.get("error"),
            failure_class=payload.get("failure_class"),
            atlas_run_id=payload.get("atlas_run_id"),
            uat_run_id=payload.get("uat_run_id"),
            cleanup_error=payload.get("cleanup_error"),
            recovery_uncertain=bool(payload.get("recovery_uncertain")),
            config=payload.get("config"),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            # Nothing in this process owns the thread of a record read from disk.
            orphaned=True,
        )
        result_path = self.runs_root / run_id / "result.json"
        if result_path.is_file():
            record.result = json.loads(result_path.read_text(encoding="utf-8"))
        session_path = self.runs_root / run_id / "session.json"
        if not session_path.is_file():
            session_path = _goal_session_path(self.runs_root / run_id, record.config)
        if session_path is not None and session_path.is_file():
            record.session = json.loads(session_path.read_text(encoding="utf-8"))
        events_path = self.runs_root / run_id / "events.jsonl"
        if events_path.is_file():
            record.events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        getattr(self, "_records", {})[run_id] = record
        return record

    def _refresh(self, record: RunRecord) -> None:
        if record.thread is not None and record.thread.is_alive():
            record.status = "RUNNING"
        elif record.orphaned and record.status in {"SUBMITTED", "RUNNING"}:
            # The process that owned this run is gone and there is no terminal evidence. Saying
            # "RUNNING" forever would be a lie; resuming it would be an unauthorised re-execution.
            record.status = "INTERRUPTED"
            record.error = (
                record.error
                or "the process that ran this task exited without writing a terminal status"
            )
            record.finished_at = record.finished_at or _now()
            self._event(record, "run_interrupted", {"detail": "no terminal evidence; not resumed"})
            self._persist(record)
            return
        if record.status in {"SANDBOX_LOST", "CLEANUP_UNRESOLVED"}:
            return
        pending = self.list_approvals(record.run_id)
        if pending:
            record.status = "WAITING_APPROVAL"
            return
        recovery = self.recovery(record.run_id)
        if recovery["state"] == "RECOVERY_REQUIRED":
            record.status = "RECOVERY_REQUIRED"
        elif record.recovery_uncertain:
            record.status = "RECOVERY_UNCERTAIN"
        elif record.status in TERMINAL:
            return

    def summary(self, record: RunRecord) -> dict[str, Any]:
        self._refresh(record)
        return {
            "run_id": record.run_id,
            "mode": record.mode,
            "task": record.task,
            "workspace": record.workspace,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "failure_class": record.failure_class,
            "error": record.error,
            "atlas_run_id": record.atlas_run_id,
            "uat_run_id": record.uat_run_id,
            "cleanup_error": record.cleanup_error,
            "config": record.config,
            "cancel": self.cancel_state(record),
        }

    def cancel_state(self, record: RunRecord) -> dict[str, Any]:
        """Cancellation is not implemented, and the record says so instead of pretending."""

        if record.status in TERMINAL:
            return {"available": False, "reason": "the run is already terminal"}
        return {
            "available": False,
            "reason": (
                "CANCEL_UNAVAILABLE: the execution happens on a worker thread that owns the "
                "sandbox and the provider stream; this version will not display a cancelled "
                "state while the work continues"
            ),
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.runs_root.iterdir():
            if not path.is_dir() or not (path / "run.json").is_file():
                continue
            try:
                items.append(self.summary(self._load(path.name)))
            except ApplicationError:
                continue
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return items[:limit]

    def detail(self, run_id: str) -> dict[str, Any]:
        record = self._load(run_id)
        summary = self.summary(record)
        metrics: dict[str, Any] = {}
        tasks: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        final_output = ""
        # The declared model of a Main-driven run is known before it produces anything, so the page
        # never shows a model the run is not using.
        model = str(_as_dict(record.config).get("model") or "deepseek-chat")
        artifacts: list[dict[str, Any]] = []
        if record.result is not None:
            metrics = record.result.get("metrics") or {}
            tasks = [
                {
                    "task_id": item.get("task_id"),
                    "title": item.get("title"),
                    "assignee": item.get("assignee") or item.get("agent"),
                    "status": item.get("status"),
                    "output": item.get("output"),
                    "prompt_tokens": item.get("prompt_tokens"),
                    "completion_tokens": item.get("completion_tokens"),
                    "duration_ms": item.get("duration_ms"),
                    "depends_on": ["research"] if item.get("task_id") == "review" else [],
                }
                for item in record.result.get("tasks") or []
            ]
            final_output = str(record.result.get("final_output") or "")
            model = str(record.result.get("model") or model)
            tool_calls = list(record.result.get("tool_calls") or [])
        if record.session is not None:
            final_output = str(record.session.get("final_output") or final_output)
            model = str(record.session.get("model") or model)
            tool_calls = list(record.session.get("tool_calls") or [])
            tasks = [
                {
                    "task_id": record.session.get("task_id"),
                    "title": "coding task",
                    "assignee": record.session.get("worker_id"),
                    "status": record.session.get("status"),
                    "output": record.session.get("final_output"),
                }
            ]
            # Unified token semantics: ``prompt_tokens`` is the total prompt (miss + cache reads)
            # and the cache split keeps its own keys. Missing values stay ``None``, never 0.
            metrics = {
                "prompt_tokens": record.session.get("total_prompt_tokens"),
                "prompt_tokens_cache_miss": record.session.get("cache_miss_tokens"),
                "cache_hit_tokens": record.session.get("cache_hit_tokens"),
                "completion_tokens": record.session.get("completion_tokens"),
                "tool_calls": len(tool_calls),
            }
            export = record.session.get("workspace_export")
            if export:
                artifacts.append(
                    {
                        "kind": "workspace_export",
                        "path": str(export),
                        "patch_changed_files": record.session.get("patch_changed_files"),
                    }
                )
        if record.mode in MAIN_MODES and record.session is not None:
            session = record.session
            final_output = _nested_text(session, "main", "output") or final_output
            model = str(session.get("model") or model)
            metrics = _main_metrics(session)
            main_block = _as_dict(session.get("main"))
            tool_calls = [
                {"tool_name": name, "agent": main_block.get("agent") or "atlas-main"}
                for name in (main_block.get("tool_calls") or [])
            ]
            delegation = _as_dict(session.get("delegation"))
            tasks = []
            for batch in delegation.get("batches") or []:
                for child in _as_dict(batch).get("children") or []:
                    child = _as_dict(child)
                    started = child.get("started_offset_ms")
                    finished = child.get("finished_offset_ms")
                    tasks.append(
                        {
                            "task_id": child.get("key"),
                            "title": child.get("key"),
                            "assignee": child.get("worker_id"),
                            "status": child.get("status"),
                            "output": None,
                            "prompt_tokens": child.get("total_prompt_tokens"),
                            "completion_tokens": child.get("completion_tokens"),
                            "duration_ms": (
                                float(finished) - float(started)
                                if started is not None and finished is not None
                                else None
                            ),
                            "depends_on": [],
                        }
                    )
            exported = _as_dict(session.get("artifacts"))
            if exported:
                artifacts.append(
                    {
                        "kind": "workspace_export",
                        "path": str(exported.get("workspace_path") or ""),
                        "patch_changed_files": exported.get("patch_changed_files"),
                        "workspace_digest": exported.get("workspace_digest"),
                    }
                )
        return {
            **summary,
            "model": model,
            "metrics": metrics,
            "artifacts": artifacts,
            "tasks": tasks,
            "workers": [
                {
                    "worker_id": item.get("assignee") or item.get("worker_id"),
                    "task_id": item.get("task_id"),
                    "status": item.get("status"),
                    "title": item.get("title"),
                }
                for item in tasks
            ],
            "tool_calls": tool_calls[-50:],
            "final_output": final_output,
            "events": record.events[-200:],
            "approvals": self.list_approvals(run_id),
            "recovery": self.recovery(run_id),
            "live": self._live_projection(record),
        }

    def _live_projection(self, record: RunRecord) -> dict[str, Any] | None:
        """What a Main-driven run can say about itself right now.

        A run that has not finished has no session yet, so the projection says which fields are
        unavailable instead of showing empty ones as if they were answers.
        """

        if record.mode not in MAIN_MODES:
            return None
        session = record.session or {}
        config = record.config or {}
        delegation = session.get("delegation")
        provider_evidence = config.get("provider_evidence")
        return {
            "mode": record.mode,
            "isolation": session.get("isolation")
            or ("sandbox" if (config.get("sandbox") or {}).get("enabled") else "host"),
            "provider": config.get("provider"),
            "provider_kind": config.get("provider_kind"),
            "budget": session.get("budget")
            or {
                "mode": "guarded",
                "limit_tokens": config.get("budget_tokens"),
                "used_tokens": None,
                "note": "the budget record is written when the run ends",
            },
            "delegation": delegation if isinstance(delegation, dict) else None,
            "verification": session.get("verification"),
            "acceptance": session.get("acceptance"),
            "public_tests": session.get("public_tests"),
            "execution_succeeded": session.get("execution_succeeded"),
            "failure": session.get("failure"),
            "provider_evidence": provider_evidence,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "cancel": self.cancel_state(record),
        }

    def result(self, run_id: str) -> dict[str, Any]:
        detail = self.detail(run_id)
        return {
            "run_id": detail["run_id"],
            "status": detail["status"],
            "final_output": detail["final_output"],
            "metrics": detail["metrics"],
            "tasks": detail["tasks"],
            "failure_class": detail["failure_class"],
            "error": detail["error"],
        }

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        record = self._load(run_id)
        self._refresh(record)
        return [event for event in record.events if int(event["id"]) > after]

    def stream_events(self, run_id: str, after: int = 0):
        sent = after
        terminal_sent = False
        while True:
            record = self._load(run_id)
            self._refresh(record)
            for event in self.events(run_id, sent):
                sent = max(sent, int(event["id"]))
                yield event
            if record.status in TERMINAL or record.status == "SANDBOX_LOST":
                if terminal_sent:
                    return
                terminal_sent = True
            time.sleep(0.25)

    # ---------------------------------------------------------------- workspaces/cancel
    def workspaces(self) -> dict[str, Any]:
        """The workspace menu: allowed roots and the names a run may use."""

        roots = describe_roots(self._workspace_roots)
        return {
            "roots": roots,
            "default": next(
                (name for root in roots for name in root["workspaces"]),
                None,
            ),
            "provider": self.provider,
            "model": (
                DETERMINISTIC_MODEL if self.provider == DETERMINISTIC_PROVIDER else "deepseek-chat"
            ),
            "provider_kind": (
                "DETERMINISTIC" if self.provider == DETERMINISTIC_PROVIDER else "LIVE_MODEL"
            ),
            "sandbox": {"enabled": self.sandbox_enabled, "image": self.sandbox_image},
            "defaults": {
                "budget_tokens": DEFAULT_BUDGET_TOKENS,
                "max_budget_tokens": MAX_BUDGET_TOKENS,
                "max_turns": DEFAULT_MAIN_TURNS,
                "max_turns_ceiling": MAX_MAIN_TURNS,
            },
            "modes": sorted(EXECUTION_MODES),
            "main_modes": sorted(MAIN_MODES),
            "active_run": self.active_main_run(),
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Refuse explicitly: there is no safe cancellation for this execution model yet."""

        record = self._load(run_id)
        state = self.cancel_state(record)
        raise CancelUnavailable(state["reason"])

    # ---------------------------------------------------------------- approvals/recovery
    @property
    def _approval_db(self) -> Path:
        return self.runs_root / "approvals.db"

    @property
    def _suspension_db(self) -> Path:
        return self.runs_root / "suspension.db"

    def list_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self._approval_db.is_file():
            return []
        from atlasos.approvals.store import SQLiteApprovalStore

        store = SQLiteApprovalStore(self._approval_db)
        values = store.list_all()
        items = [item.model_dump(mode="json") for item in values]
        if run_id is None:
            return items
        filtered = []
        for item in items:
            text = json.dumps(item)
            if run_id in text:
                filtered.append(item)
        return filtered

    def _resolve_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if not self._approval_db.is_file():
            raise ApprovalAuthorityUnavailable("no authoritative approval store is present")
        from atlasos.approvals.coordinator import ApprovalCoordinator
        from atlasos.approvals.models import ReviewerIdentity
        from atlasos.approvals.store import SQLiteApprovalStore
        from atlasos.suspension.store import SQLiteSuspensionStore

        store = SQLiteApprovalStore(self._approval_db)
        challenge = store.get(approval_id)
        if challenge is None:
            raise ApprovalAuthorityUnavailable(f"unknown approval: {approval_id}")
        if not self._suspension_db.is_file():
            raise ApprovalAuthorityUnavailable(
                "approval is not attached to an active trusted suspension"
            )
        suspension = SQLiteSuspensionStore(self._suspension_db).get(challenge.suspension_id)
        if suspension is None or suspension.state != "ACTIVE":
            raise ApprovalAuthorityUnavailable(
                "approval is not attached to an active trusted suspension"
            )
        if not any(
            invocation.approval_id == approval_id for invocation in suspension.pending_invocations
        ):
            raise ApprovalAuthorityUnavailable(
                "approval is not one of the suspension's pending human decisions"
            )
        coordinator = ApprovalCoordinator(store)
        resolution = coordinator.resolve(
            approval_id,
            expected_version=challenge.version,
            decision=decision,  # type: ignore[arg-type]
            reviewer=ReviewerIdentity(actor="user:web"),
            reason="explicit local UI decision",
        )
        if resolution.status not in {"resolved", "already_resolved"}:
            raise ApprovalAuthorityUnavailable(f"approval resolution refused: {resolution.status}")
        resolved = resolution.challenge or challenge
        return {
            "approval_id": resolved.approval_id,
            "state": resolved.state,
            "version": resolved.version,
            "actor": resolved.actor,
            "resolution_status": resolution.status,
        }

    def approve(self, approval_id: str) -> dict[str, Any]:
        return self._resolve_approval(approval_id, "APPROVED")

    def reject(self, approval_id: str) -> dict[str, Any]:
        return self._resolve_approval(approval_id, "REJECTED")

    def recovery(self, run_id: str | None = None) -> dict[str, Any]:
        if not self._suspension_db.is_file():
            return {"state": "NONE", "suspensions": []}
        from atlasos.suspension.store import SQLiteSuspensionStore

        store = SQLiteSuspensionStore(self._suspension_db)
        active = store.list_active()
        values = [item.public_projection() for item in active]
        if run_id is not None:
            values = [item for item in values if run_id in json.dumps(item)]
        return {
            "state": "RECOVERY_REQUIRED" if values else "NONE",
            "suspensions": values,
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        recovery = self.recovery(run_id)
        if recovery["state"] == "NONE":
            raise RecoveryAuthorityUnavailable("no active trusted suspension for this run")
        raise RecoveryAuthorityUnavailable(
            "trusted Stage D recovery requires the operator recovery harness; the application "
            "service will not reattach or destroy a sandbox without the M5/M6 authority"
        )
