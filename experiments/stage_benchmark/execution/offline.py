"""Offline fake-provider scenarios for the Stage B execution harness.

Every case uses synthetic evidence in a scratch directory. No case opens a network connection,
starts a sandbox, or imports a live provider factory. The real ``run_goal`` composition is exercised
separately by the integration test with the repository's scripted provider.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import protocol
from . import admission, collector, provision
from .models import AttemptAlreadyStarted, CellRequest

CASES = (
    "direct_pass",
    "delegated_two_children",
    "router_direct",
    "worker_failure",
    "handoff_missing",
    "budget_exhaustion",
    "tool_denial",
    "oracle_fail",
    "provider_exception",
    "sandbox_lost",
    "collector_crash",
    "duplicate_cell_start",
    "concurrent_claim",
    "evidence_recovery",
    "incomplete_evidence_rejected",
    "prompt_arm_injection",
)


class FakeProvider:
    """A call-recording stand-in. It never reaches a network or an LLM SDK."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.name = "stage-b-fake"

    def build(self) -> FakeProvider:
        return self

    async def chat(self, messages: list[Any], options: Any) -> dict[str, Any]:
        self.calls.append({"messages": len(messages), "options": str(options)})
        return {"content": [], "stop_reason": "end_turn"}


def _conversation(tool_names: tuple[str, ...] = ()) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "goal"}]}
    ]
    for index, name in enumerate(tool_names):
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"c{index}", "name": name, "input": {}}],
            }
        )
        messages.append({"role": "user", "content": [{"type": "tool_result", "content": "ok"}]})
    return {"messages": messages}


def _batch(route: str, admitted: int, success: bool = True) -> dict[str, Any]:
    return {
        "round": 1,
        "route": route,
        "decision": {
            "route": route,
            "reason_codes": ["DELEGATION_ADMITTED"] if admitted else ["ROUTED_DIRECT"],
            "blocked_gates": [],
            "children": [
                {"key": f"task-{i}", "profile_id": "research_worker", "admitted": True}
                for i in range(admitted)
            ],
        },
        "children": [
            {
                "key": f"task-{i}",
                "worker_id": f"run-main:r1:task-{i}",
                "status": "succeeded" if success else "failed",
                "success": success,
                "turns": 3,
                "tool_calls": 2,
            }
            for i in range(admitted)
        ],
        "max_observed_concurrency": admitted,
    }


def synthetic_session(
    options: Any,
    *,
    status: str = "succeeded",
    acceptance: str = "PASS",
    stop: str = "success",
    public: str = "PASS",
    batches: tuple[dict[str, Any], ...] = (),
    tool_names: tuple[str, ...] = ("file_read", "file_edit"),
    oracle: str = "PASS",
    denied: int = 0,
    handoff_chars: int | None = None,
) -> dict[str, Any]:
    """One deterministic session payload, shaped the way ``reconstruct`` expects."""

    main_task = "run-main"
    if batches and "delegate_tasks" not in tool_names:
        tool_names = ("delegate_tasks",) + tool_names
    conversations = {main_task: _conversation(tool_names)}
    for batch in batches:
        for child in batch.get("children") or ():
            conversations[child["worker_id"]] = _conversation(("file_read",))
    admitted = sum(len(batch.get("children") or ()) for batch in batches)
    child_prompt = 1200 * admitted
    return {
        "run_id": options.run_id,
        "status": status,
        "acceptance": acceptance,
        "main": {
            "task_id": main_task,
            "turns": len(tool_names) + 1,
            "tool_calls": list(tool_names),
            "stop_reason": stop,
            "output": "done" if status == "succeeded" else "",
        },
        "metrics": {
            "main_requests": 14,
            "main_peak_context_tokens": 4000,
            "main_cumulative_input_tokens": 5000,
            "main_cache_hit_tokens": 4000,
            "main_cache_miss_tokens": 1000,
            "main_completion_tokens": 300,
            "child_total_prompt_tokens": child_prompt,
            "child_completion_tokens": 60 * admitted,
            "system_total_tokens": 5300 + child_prompt + 60 * admitted,
            "handoff_payload_chars": (400 if admitted else 0)
            if handoff_chars is None
            else handoff_chars,
        },
        "wall_clock_seconds": 42.0,
        "main_prompt": options.main_prompt.evidence(source="override"),
        "delegation": {"enabled": True, "batches": list(batches)},
        "public_tests": {
            "calls": 1,
            "runs": [{"status": public, "exit_code": 0 if public == "PASS" else 1}],
        },
        "sandbox": {
            "created": True,
            "destroyed": True,
            "error": None,
            "patch_changed_files": 1 if status == "succeeded" else 0,
        },
        "verification": {"verdict": acceptance, "dynamic": {"status": oracle, "exit_code": 0}},
        "ownership": {"denied_calls": denied},
        "provider": options.provider,
        "model": options.model,
    }


def write_synthetic_run(options: Any, session: Mapping[str, Any]) -> Path:
    run_dir = options.runs_root / options.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")
    main = session.get("main") or {}
    conversations = {
        main.get("task_id", "run-main"): _conversation(tuple(main.get("tool_calls") or ()))
    }
    for batch in (session.get("delegation") or {}).get("batches") or ():
        for child in batch.get("children") or ():
            conversations[child["worker_id"]] = _conversation(("file_read",))
    (run_dir / "tool_conversations.json").write_text(
        json.dumps({"tasks": conversations}), encoding="utf-8"
    )
    (run_dir / "workspace.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
    trajectory = run_dir / "trajectory" / options.run_id
    trajectory.mkdir(parents=True, exist_ok=True)
    (trajectory / "transcript.jsonl").write_text(
        "\n".join(
            json.dumps({"event": "action_proposed", "payload": {"name": name}})
            for name in ("file_read", "file_edit", "run_public_tests")
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _run_fn(session: Mapping[str, Any] | None, *, error: str | None = None, omit: bool = False):
    async def execute(options: Any) -> dict[str, Any]:
        if error:
            raise RuntimeError(error)
        if not omit:
            write_synthetic_run(options, session or {})
        return dict(session or {})

    return execute


async def _case(
    name: str,
    root: Path,
    cell_id: str,
    *,
    session_kwargs: dict[str, Any] | None = None,
    error: str | None = None,
    omit: bool = False,
    expect_kind: str = "none",
) -> dict[str, Any]:
    document = protocol.read_protocol()
    cell = provision.cell_from_plan(document, cell_id)

    async def configured(options: Any) -> dict[str, Any]:
        if error:
            raise RuntimeError(error)
        payload = synthetic_session(options, **(session_kwargs or {}))
        if not omit:
            write_synthetic_run(options, payload)
        return payload

    request = CellRequest(
        cell=cell,
        runs_root=root / name / "runs",
        evidence_root=root / name / "evidence",
        protocol_document=document,
        provider_factory=FakeProvider().build,
        sandbox_factory=lambda _root: FakeProvider(),
        run_fn=configured,
    )
    from .runner import execute_cell

    result = await execute_cell(request)
    return {
        "name": name,
        "ok": result.failure_kind == expect_kind,
        "detail": f"expected={expect_kind} got={result.failure_kind} {result.detail}",
    }


async def _suite(root: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    cases.append(await _case("direct_pass", root, "slugkit.P1.r1"))
    cases.append(
        await _case(
            "delegated_two_children",
            root,
            "zonelint.P2.r1",
            session_kwargs={"batches": (_batch("delegate_parallel", 2),)},
        )
    )
    cases.append(
        await _case(
            "router_direct",
            root,
            "pager.P1.r1",
            session_kwargs={"batches": (_batch("direct", 0),)},
            expect_kind="router_rejected",
        )
    )
    cases.append(
        await _case(
            "worker_failure",
            root,
            "ledgerclose.P2.r2",
            session_kwargs={"batches": (_batch("delegate_one", 1, success=False),)},
            expect_kind="child_failure",
        )
    )
    cases.append(
        await _case(
            "handoff_missing",
            root,
            "retrygate.P3.r1",
            session_kwargs={
                "batches": (_batch("delegate_one", 1),),
                "tool_names": ("file_read",),
                "handoff_chars": 0,
            },
            expect_kind="handoff_missing",
        )
    )
    cases.append(
        await _case(
            "budget_exhaustion",
            root,
            "cascade.P1.r2",
            session_kwargs={"status": "failed", "acceptance": "FAIL", "stop": "budget_exceeded"},
            expect_kind="budget_exhaustion",
        )
    )
    cases.append(
        await _case(
            "tool_denial",
            root,
            "slugkit.P2.r2",
            session_kwargs={"tool_names": ("bash", "file_read", "file_edit"), "denied": 1},
        )
    )
    cases.append(
        await _case(
            "oracle_fail",
            root,
            "pager.P3.r2",
            session_kwargs={"status": "failed", "acceptance": "FAIL", "oracle": "FAIL"},
            expect_kind="oracle_failed",
        )
    )
    cases.append(
        await _case(
            "provider_exception",
            root,
            "zonelint.P1.r2",
            error="provider transport failed",
            expect_kind="provider_exception",
        )
    )
    cases.append(
        await _case(
            "sandbox_lost",
            root,
            "ledgerclose.P3.r1",
            error="sandbox lost during tool execution",
            expect_kind="sandbox_creation",
        )
    )
    cases.append(
        await _case(
            "collector_crash",
            root,
            "retrygate.P2.r1",
            omit=True,
            expect_kind="collector_failure",
        )
    )

    # once-only protection and recovery are checked on their own scratch cells
    document = protocol.read_protocol()
    cell = provision.cell_from_plan(document, "cascade.P2.r1")
    directory = admission.cell_dir(root / "duplicate" / "evidence", cell)
    first = admission.claim_attempt(
        directory, cell=cell, preflight_digest="x", authorization_digest="y", source_digest="z"
    )
    try:
        admission.claim_attempt(
            directory, cell=cell, preflight_digest="x", authorization_digest="y", source_digest="z"
        )
        duplicate_ok = False
    except AttemptAlreadyStarted:
        duplicate_ok = True
    cases.append({"name": "duplicate_cell_start", "ok": duplicate_ok, "detail": str(first.path)})

    race_cell = provision.cell_from_plan(document, "cascade.P3.r2")
    race_dir = admission.cell_dir(root / "race" / "evidence", race_cell)
    outcomes: list[str] = []

    def race() -> None:
        try:
            admission.claim_attempt(
                race_dir,
                cell=race_cell,
                preflight_digest="x",
                authorization_digest="y",
                source_digest="z",
            )
            outcomes.append("won")
        except AttemptAlreadyStarted:
            outcomes.append("refused")

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    cases.append(
        {
            "name": "concurrent_claim",
            "ok": outcomes.count("won") == 1 and outcomes.count("refused") == 7,
            "detail": str(outcomes),
        }
    )

    recovered_cell = provision.cell_from_plan(document, "slugkit.P3.r2")
    recovered_dir = admission.cell_dir(root / "direct_pass" / "evidence", recovered_cell)
    # Reuse a complete bundle from a real synthetic case under the recovery cell's name.
    source_dir = admission.cell_dir(
        root / "direct_pass" / "evidence", provision.cell_from_plan(document, "slugkit.P1.r1")
    )
    if source_dir.is_dir():
        import shutil

        (recovered_dir / "evidence").mkdir(parents=True, exist_ok=True)
        for item in (source_dir / "evidence").iterdir():
            target = recovered_dir / "evidence" / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    recovery = collector.recover_evidence(
        recovered_dir / "evidence",
        cell=recovered_cell,
        source_digest=admission.source_identity()["digest"],
    )
    cases.append(
        {
            "name": "evidence_recovery",
            "ok": bool(recovery["ok"]),
            "detail": str(recovery.get("missing") or recovery.get("missing_raw")),
        }
    )

    incomplete = root / "incomplete" / "bundle"
    incomplete.mkdir(parents=True, exist_ok=True)
    (incomplete / "session.json").write_text("{}", encoding="utf-8")
    refused = collector.recover_evidence(
        incomplete, cell=cell, source_digest=admission.source_identity()["digest"]
    )
    cases.append(
        {
            "name": "incomplete_evidence_rejected",
            "ok": refused["ok"] is False,
            "detail": str(refused.get("missing_raw")),
        }
    )

    prompt_ok = True
    details: list[str] = []
    document = protocol.read_protocol()
    for arm in protocol.ARM_IDS:
        spec = protocol.prompt_spec(arm)
        declared = provision.arm_entry(document, arm)
        prompt_ok = (
            prompt_ok
            and spec.version == declared["label"]
            and spec.digest == declared["declared_digest"]
        )
        details.append(f"{arm}:{spec.version}:{spec.digest[:20]}")
    cases.append({"name": "prompt_arm_injection", "ok": prompt_ok, "detail": "; ".join(details)})
    return cases


def run_all(root: Path) -> dict[str, Any]:
    """Run every offline case. This is the report the live preflight depends on."""

    root.mkdir(parents=True, exist_ok=True)
    cases = asyncio.run(_suite(root))
    names = tuple(item["name"] for item in cases)
    return {
        "status": "READY" if all(item["ok"] for item in cases) else "BLOCKED",
        "passed": all(item["ok"] for item in cases),
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "fake",
        "network_calls": 0,
        "cases": cases,
        "case_names": names,
        "coverage": set(CASES).issubset(names),
    }
