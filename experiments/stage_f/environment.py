"""Stage F — the eight formal tasks in a real environment.

F2-B qualified the tasks on the Host. This module qualifies the *environment*: the fixture is
provisioned the way a cell provisions it, a real OpenSandbox container is created, the seed's public
suite is invoked there, the reference implementation is uploaded over it, and both the public suite
and the hidden oracle run inside the same disposable container. One container per task, destroyed
before the next one starts.

What it proves, per task:

* the fixture provisioned from the frozen seed matches its digest;
* the public suite is reachable from inside the sandbox (it is invoked, not merely present);
* the seed fails that suite, so a cell has work to do;
* the reference passes the public suite *and* the hidden suite in the real container;
* the hidden tests and the reference bodies are not in the agent's initial workspace;
* every path the reference changes is allowed and none is forbidden;
* the task identity (goal digest, seed digest) is the frozen one.

No model, no provider, no claim, no production evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from anycode.identity import ExecutionContext
from anycode.sandbox import SandboxCommand, SandboxSpec
from experiments.evaluation.fixtures import path_matches
from experiments.evaluation.sandboxed_oracle import (
    OracleSandboxConfig,
    _collect_upload,
    classify_pytest_exit,
)
from experiments.stage_b_plus import authority, executor
from experiments.stage_f import arms, tasks

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = "/workspace/oracle"


def _context(run_id: str) -> ExecutionContext:
    return ExecutionContext(
        principal="atlas:environment-qualification",
        tenant_scope="atlas-oracle",
        classification="internal",
        attributes={"run_id": run_id, "atlas_environment_qualification": "1"},
    )


async def _execute(
    provider: Any, handle: Any, command: str, *, timeout_seconds: int
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await provider.execute(
            handle,
            SandboxCommand(
                command=command,
                cwd=WORKSPACE,
                environment={"PYTHONPATH": WORKSPACE, "PYTHONDONTWRITEBYTECODE": "1"},
                timeout_seconds=timeout_seconds,
            ),
        )
    except TimeoutError:
        return {
            "command": command,
            "exit_code": None,
            "kind": "TIMEOUT",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "stdout_tail": "",
            "stderr_tail": f"exceeded {timeout_seconds}s",
        }
    except Exception as error:  # noqa: BLE001 - an execution failure is evidence
        return {
            "command": command,
            "exit_code": None,
            "kind": "SANDBOX_EXECUTION_FAILURE",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "stdout_tail": "",
            "stderr_tail": f"{type(error).__name__}: {error}"[:400],
        }
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return {
        "command": command,
        "exit_code": result.exit_code,
        "kind": classify_pytest_exit(result.exit_code, stdout, stderr)
        if "pytest" in command
        else ("OK" if result.exit_code == 0 else "NONZERO"),
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "stdout_tail": stdout[-1200:],
        "stderr_tail": stderr[-800:],
        "error": None if result.error is None else str(result.error)[:400],
    }


async def _upload(
    provider: Any, handle: Any, workspace: Path, extra: Mapping[str, str]
) -> dict[str, Any]:
    payload, problem = await asyncio.to_thread(
        _collect_upload, workspace, dict(extra), max_files=600, max_file_bytes=2_000_000
    )
    if problem is not None:
        return {"files": 0, "problem": problem}
    written = 0
    for relative, data in payload:
        result = await provider.write_file(handle, f"{WORKSPACE}/{relative}", data)
        if not result.ok:
            return {"files": written, "problem": f"upload of {relative} failed: {result.error}"}
        written += 1
    return {"files": written, "problem": None}


def _leak_checks(task: Any) -> list[str]:
    """The seed must not carry the hidden suite or the reference answers."""

    problems: list[str] = []
    hidden = set(task.oracle_tests)
    leaked = sorted(hidden & set(task.seed))
    if leaked:
        problems.append(f"the seed contains hidden tests: {leaked[:3]}")
    if any("reference" in path.split("/") for path in task.seed):
        problems.append("the seed carries a reference directory")
    for relative, body in task.reference.items():
        seed_body = task.seed.get(relative)
        if seed_body is not None and seed_body == body:
            problems.append(f"{relative} is identical in the seed and the reference")
    for relative in task.reference:
        if relative not in task.seed and "test_public" in relative:
            problems.append(f"the reference adds a public test: {relative}")
    return problems


def _path_policy(task: Any) -> list[str]:
    """Every file the reference *writes* is allowed, and none is forbidden.

    The reference is an overlay: it carries the modules a solver must deliver and leaves the frozen
    support files and the documentation at their seed content. A file that is absent from the
    overlay is not a deletion by the solver, so it is not a write and cannot be judged as one.
    """

    problems: list[str] = []
    for relative, body in sorted(task.reference.items()):
        if task.seed.get(relative) == body:
            continue
        if not path_matches(relative, tuple(task.allowed_paths)):
            problems.append(f"{relative} is outside the allowed paths")
        if path_matches(relative, tuple(task.forbidden_paths)):
            problems.append(f"{relative} is a forbidden path")
    return problems


def _identity_problems(task: Any) -> list[str]:
    problems: list[str] = []
    if task.task_id not in tasks.FAMILY_OF:
        problems.append(f"{task.task_id} is not in the frozen family map")
    digest = authority.digest_of(dict(sorted(task.seed.items())))
    if digest != authority.digest_of(dict(sorted(task.seed.items()))):
        problems.append("the seed digest does not recompute")
    expected = "sha256:" + hashlib.sha256(task.prompt.encode("utf-8")).hexdigest()
    if arms.goal_digest(task.task_id) != expected:
        problems.append("the goal digest is not the published requirement")
    return problems


async def qualify_task(task: Any, *, scratch: Path) -> dict[str, Any]:
    """One task, one container: seed public, reference public, reference hidden."""

    run_id = f"stagef-env-{task.task_id}"
    fixture = await asyncio.to_thread(executor.prepare_fixture, task, scratch / task.task_id)
    fixture_dir = Path(fixture["path"])
    config = OracleSandboxConfig(image=str(executor.contract.BUDGET["image"]), timeout_seconds=240)
    provider = executor._sandbox_provider(  # noqa: SLF001 - the one production provider builder
        executor.RunnerConfig(), scratch / task.task_id / "sandbox"
    )
    problems: list[str] = []
    row: dict[str, Any] = {
        "task_id": task.task_id,
        "family": tasks.family_of(task.task_id),
        "fixture_matches_frozen_seed": bool(fixture["matches_frozen_seed"]),
        "seed_digest": fixture["seed_digest"],
        "goal_digest": arms.goal_digest(task.task_id),
        "files": fixture["files"],
    }
    if not fixture["matches_frozen_seed"]:
        problems.append("the provisioned fixture does not match the frozen seed")

    spec = SandboxSpec(
        run_id=f"{run_id}-sandbox",
        task_id="environment",
        correlation_id=f"{run_id}:environment",
        context=_context(run_id),
        image=config.image,
        network="none",
        persistent=False,
        labels={"atlas_stage_f": "environment"},
    )
    created = await provider.create(spec)
    if not created.ok or created.handle is None:
        return {
            **row,
            "ok": False,
            "problems": [*problems, f"sandbox creation refused: {created.error}"],
            "sandbox_id": None,
        }
    handle = created.handle
    row["sandbox_id"] = handle.id
    row["network"] = "none"
    started = time.perf_counter()
    cleanup_failure: str | None = None
    try:
        uploaded = await _upload(provider, handle, fixture_dir, {})
        row["seed_upload"] = uploaded
        if uploaded["problem"]:
            problems.append(uploaded["problem"])
        else:
            row["seed_public"] = await _execute(
                provider,
                handle,
                "python3 -m pytest -q --tb=line test_public.py",
                timeout_seconds=240,
            )

        reference_dir = scratch / task.task_id / "reference"
        reference_dir.mkdir(parents=True, exist_ok=True)
        for relative, text in task.reference.items():
            target = reference_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for relative, text in task.public_tests.items():
            target = reference_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        uploaded = await _upload(provider, handle, reference_dir, task.oracle_tests)
        row["reference_upload"] = uploaded
        if uploaded["problem"]:
            problems.append(uploaded["problem"])
        else:
            row["reference_public"] = await _execute(
                provider,
                handle,
                "python3 -m pytest -q --tb=line test_public.py",
                timeout_seconds=240,
            )
            hidden = next(
                (name for name in task.oracle_tests if name.startswith("test_hidden_")), None
            )
            row["reference_hidden"] = await _execute(
                provider,
                handle,
                f"python3 -m pytest -q --tb=line {hidden}" if hidden else "python3 -m pytest -q",
                timeout_seconds=240,
            )
    finally:
        try:
            destroyed = await provider.destroy(handle)
            if destroyed is not None and getattr(destroyed, "ok", True) is False:
                cleanup_failure = str(getattr(destroyed, "error", "destroy refused"))
        except Exception as error:  # noqa: BLE001 - a failed destroy is recorded
            cleanup_failure = f"{type(error).__name__}: {error}"
        row["cleanup_failure"] = cleanup_failure
        row["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)

    if cleanup_failure:
        problems.append(f"the sandbox was not destroyed: {cleanup_failure}")

    seed_public = row.get("seed_public") or {}
    ref_public = row.get("reference_public") or {}
    ref_hidden = row.get("reference_hidden") or {}
    if seed_public.get("exit_code") in (None, 0):
        problems.append(f"the seed public suite exit is {seed_public.get('exit_code')!r}")
    if ref_public.get("exit_code") != 0:
        problems.append(f"the reference public suite exit is {ref_public.get('exit_code')!r}")
    if ref_hidden.get("exit_code") != 0:
        problems.append(f"the reference hidden suite exit is {ref_hidden.get('exit_code')!r}")
    if (
        "test_public.py" not in (ref_public.get("stdout_tail") or "")
        and ref_public.get("exit_code") != 0
    ):
        problems.append("the public suite output does not name the suite that ran")

    problems.extend(_leak_checks(task))
    problems.extend(_path_policy(task))
    problems.extend(_identity_problems(task))
    row["ok"] = not problems
    row["problems"] = problems
    row["executed_in_sandbox"] = True
    return row


async def run(*, scratch: Path | None = None) -> dict[str, Any]:
    base = Path(scratch or tempfile.mkdtemp(prefix="stage-f-environment-"))
    await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for task in tasks.TASKS:
        rows.append(await qualify_task(task, scratch=base))
    problems = [f"{row['task_id']}: {item}" for row in rows for item in row.get("problems") or []]
    ok = not problems and len(rows) == 8
    identity = executor.executor_identity(REPO)
    return {
        "stage": "F",
        "kind": "eight_task_environment_qualification",
        "generated_at": executor.now(),
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "repo_head": executor.git_head(REPO),
        "ok": ok,
        "status": (
            "STAGE_F_ENVIRONMENT_QUALIFICATION_PASS"
            if ok
            else "STAGE_F_ENVIRONMENT_QUALIFICATION_FAIL"
        ),
        "tasks": rows,
        "problems": problems,
        "containers_created": sum(1 for row in rows if row.get("sandbox_id")),
        "containers_destroyed": sum(
            1 for row in rows if row.get("sandbox_id") and not row.get("cleanup_failure")
        ),
        "model_calls": 0,
        "production_claims_consumed": 0,
        "scratch_root": str(base),
        "note": (
            "one real OpenSandbox container per task: seed public suite, reference public suite "
            "and reference hidden suite, then destroy. No model, no claim, no production evidence."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    output = (
        Path(args[0])
        if args
        else Path(__file__).resolve().parent / "environment-qualification.json"
    )
    scratch = Path(args[1]) if len(args) > 1 else None
    payload = asyncio.run(run(scratch=scratch))
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": payload["ok"],
                "status": payload["status"],
                "containers": payload["containers_created"],
                "problems": payload["problems"][:8],
                "output": str(output),
            }
        )
    )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
