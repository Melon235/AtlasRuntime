"""Stage B0 runner: one controlled real delegation run.

    python experiments/stage_b0_controlled/run_stage_b0.py --freeze
    python experiments/stage_b0_controlled/run_stage_b0.py --preflight
    python experiments/stage_b0_controlled/run_stage_b0.py --run

``--run`` performs **one** live DeepSeek attempt. It claims an attempt marker with
``O_CREAT | O_EXCL`` immediately before calling ``run_goal`` — never during the preflight — and
every entry point refuses afterwards: a marker is a permanent record that this experiment has been
run, so neither ``--freeze``, nor ``--run``, nor a fresh scratch directory can produce a second
attempt. There is no retry path.

The run is recorded as ``CONTROLLED_DELEGATION`` (see ``b0_contract``): the Main is told by the Host
that the run validates the delegation tool. Nothing is injected into tool results, no child is
faked, the Router and the worker factory are the production ones, and the Main's own loop does the
integration.

Identity model (see ``docs/stage-b0-controlled-live.md`` §B):

* **Runtime source identity** — a digest over every file under ``src/atlasos``. Authoritative and
  blocking: a live run must execute exactly the frozen source.
* **Harness identity** — digests of this file and ``b0_contract.py``. Blocking for the same reason.
* **Git provenance** — which commit the freeze was written from, whether the worktree was clean, and
  whether that commit is still an ancestor of HEAD. Recorded, and the ancestry is enforced: a
  freeze file can never contain the hash of the commit that adds it, so equality would be a check
  that cannot pass, while "still in this history" is a real integrity constraint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
P1B_DIR = REPO_ROOT / "experiments" / "stage_p1b_live"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(P1B_DIR))

import b0_contract as contract  # noqa: E402
import run_stage_p1b_live as p1b_runner  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from atlasos.app.goal_sandbox import GoalSandboxOptions  # noqa: E402
from atlasos.app.live_preflight import READY, run_live_preflight  # noqa: E402
from atlasos.app.main_driven import MAIN_SYSTEM_PROMPT, GoalRunOptions, run_goal  # noqa: E402
from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig  # noqa: E402
from atlasos.delegation import DelegationConfig  # noqa: E402

FREEZE_PATH = HERE / "freeze.json"
EVIDENCE_ROOT = HERE / "evidence"
RUNTIME_SOURCE_ROOT = REPO_ROOT / "src" / "atlasos"
HARNESS_FILES = ("b0_contract.py", "run_stage_b0.py")
#: Outside the repository by default: run artefacts must never dirty the worktree.
DEFAULT_SCRATCH = Path(
    os.environ.get("ATLAS_B0_SCRATCH") or (Path(tempfile.gettempdir()) / "atlas-b0-live")
)


class AttemptAlreadyStarted(RuntimeError):
    """A B0 live attempt exists. Nothing may start a second one."""


# -- identity -----------------------------------------------------------------------------------


def _tree_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
            continue
        files[relative.as_posix()] = contract.digest_bytes(path.read_bytes())
    return files


def source_identity() -> dict[str, Any]:
    """Authoritative identity of the runtime this experiment is allowed to execute."""

    files = _tree_files(RUNTIME_SOURCE_ROOT)
    return {
        "root": "src/atlasos",
        "file_count": len(files),
        "tree_digest": contract.digest(json.dumps(files, sort_keys=True)),
        "files": files,
    }


def harness_identity() -> dict[str, str]:
    return {name: contract.digest_bytes((HERE / name).read_bytes()) for name in HARNESS_FILES}


def git_identity() -> dict[str, Any]:
    """Provenance only: what was checked out, and whether it was clean."""

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout.strip()

    return {
        "git_head": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_status": _git("status", "--porcelain"),
        "main_system_prompt_digest": contract.digest(MAIN_SYSTEM_PROMPT),
    }


def is_ancestor(commit: str) -> bool:
    if not commit:
        return False
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def frozen_manifest() -> dict[str, Any]:
    """The fixture's byte digests, computed by the stage P1b function itself.

    Delegating rather than re-implementing is the point: B0 must measure the *same* fixture, so
    there is one manifest implementation (byte digests, same skip list) and one identity.
    """

    files = p1b_runner.fixture_manifest()
    return {"files": files, "fixture_digest": contract.digest(json.dumps(files, sort_keys=True))}


def write_freeze() -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": contract.frozen_identity(),
        "source": source_identity(),
        "harness": harness_identity(),
        "fixture": frozen_manifest(),
        "p1b_fixture_manifest_digest": contract.digest(
            (P1B_DIR / "fixture_manifest.json").read_text(encoding="utf-8")
        ),
        "git": git_identity(),
    }
    FREEZE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _freeze() -> dict[str, Any]:
    if not FREEZE_PATH.is_file():
        raise SystemExit(f"no freeze at {FREEZE_PATH}; run --freeze first")
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


# -- one-shot protection ------------------------------------------------------------------------


def attempt_marker(scratch: Path) -> Path:
    return scratch / "attempts" / "attempt-1.json"


def prior_attempt(scratch: Path) -> str | None:
    """Why this experiment must not start again, or ``None``.

    Two independent records are consulted so that deleting one cannot unblock a second attempt: the
    scratch marker (written before the provider call) and any collected evidence directory.
    """

    marker = attempt_marker(scratch)
    if marker.exists():
        return f"attempt marker exists: {marker}"
    evidence = sorted(EVIDENCE_ROOT.glob("run-*"))
    if evidence:
        return f"collected evidence exists: {evidence[0]}"
    return None


def claim_attempt(scratch: Path, payload: dict[str, Any]) -> Path:
    """Create the marker atomically. Called immediately before ``run_goal``, never before."""

    marker = attempt_marker(scratch)
    marker.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:  # another process won the race, or a prior attempt exists
        raise AttemptAlreadyStarted(f"attempt marker already exists: {marker}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return marker


# -- options ------------------------------------------------------------------------------------


def sandbox_config() -> SandboxConfig:
    """The stage P1b configuration; ``require_real`` lives on the goal sandbox options."""

    return SandboxConfig(
        enabled=True,
        provider="opensandbox",
        endpoint=os.environ.get("OPEN_SANDBOX_ENDPOINT", "localhost:8080"),
        protocol="http",
        image=contract.IMAGE,
        workspace_root="/workspace/project",
        network="none",
        timeout_minutes=contract.SANDBOX_TIMEOUT_MINUTES,
        ready_timeout_seconds=240,
        cpu="1",
        memory="1Gi",
        use_server_proxy=True,
        retain_on_failure=False,
    )


def build_options(run_root: Path, fixture: Path, *, run_id: str) -> GoalRunOptions:
    return GoalRunOptions(
        goal=contract.GOAL,
        runs_root=run_root,
        workspace_root=run_root / "host-workspace",
        config=AtlasConfig(tools=ToolsConfig(mode="progressive"), sandbox=sandbox_config()),
        delegation=DelegationConfig(
            enabled=True,
            max_parallel_children=contract.DELEGATION["max_parallel_children"],
            max_children_per_call=contract.DELEGATION["max_children_per_call"],
            max_delegation_rounds=contract.DELEGATION["max_delegation_rounds"],
            child_timeout_seconds=contract.DELEGATION["child_timeout_seconds"],
            child_max_turns=contract.DELEGATION["child_max_turns"],
            result_preview_chars=contract.RESULT_PREVIEW_CHARS,
        ),
        provider=contract.PROVIDER,
        model=contract.MODEL,
        max_turns=contract.MAIN_MAX_TURNS,
        token_budget=contract.TOKEN_BUDGET,
        main_write_paths=contract.MAIN_WRITE_PATHS,
        sandbox=GoalSandboxOptions(
            provider="opensandbox",
            source_repo=fixture,
            image=contract.IMAGE,
            workspace_root="/workspace/project",
            require_real=True,
            tool_error_mode=contract.TOOL_ERROR_MODE,
        ),
        acceptance=contract.acceptance(),
        public_tests=contract.PUBLIC_TESTS,
        run_id=run_id,
        tool_error_mode=contract.TOOL_ERROR_MODE,
    )


# -- preflight ----------------------------------------------------------------------------------


def stage_checks(options: GoalRunOptions) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"check_id": check_id, "ok": ok, "blocking": blocking, "detail": detail})

    frozen = _freeze()
    identity = frozen.get("identity") or {}
    source = source_identity()
    harness = harness_identity()

    add(
        "runtime_source_frozen",
        source == frozen.get("source"),
        f"tree digest {source['tree_digest'][:23]} over {source['file_count']} files",
    )
    add(
        "harness_frozen",
        harness == frozen.get("harness"),
        "runner and contract digests match the freeze",
    )
    add(
        "freeze_commit_is_ancestor",
        is_ancestor(str((frozen.get("git") or {}).get("git_head", ""))),
        f"freeze written at {(frozen.get('git') or {}).get('git_head', '')[:12]}",
    )
    add("worktree_clean", git_identity()["git_status"] == "", "git status is empty")
    add(
        "tool_error_mode_frozen",
        identity.get("tool_error_mode") == contract.TOOL_ERROR_MODE == options.tool_error_mode,
        f"expected {contract.TOOL_ERROR_MODE}, runtime option {options.tool_error_mode}",
    )
    add(
        "goal_frozen",
        contract.goal_digest() == identity.get("goal_digest"),
        f"goal digest {contract.goal_digest()[:20]}",
    )
    add(
        "instruction_frozen",
        contract.instruction_digest() == identity.get("controlled_instruction_digest"),
        "controlled instruction digest",
    )
    add(
        "delegation_mode_labelled",
        contract.DELEGATION_MODE == "CONTROLLED_DELEGATION"
        and "AUTONOMOUS_MAIN_DELEGATION" not in contract.GOAL,
        f"mode={contract.DELEGATION_MODE}",
    )
    add(
        "instruction_is_host_authored",
        contract.CONTROLLED_INSTRUCTION in contract.GOAL
        and "HOST-AUTHORED EXPERIMENT INSTRUCTION" in contract.GOAL,
        "the instruction is part of the goal text, not a tool result or a system-prompt edit",
    )
    add(
        "main_prompt_unchanged",
        contract.digest(MAIN_SYSTEM_PROMPT) == contract.MAIN_SYSTEM_PROMPT_DIGEST,
        "MAIN_SYSTEM_PROMPT equals the stage P0.1 digest",
    )
    add(
        "goal_passed_to_runtime", options.goal == contract.GOAL, "run options carry the frozen goal"
    )
    add(
        "fixture_matches_p1b",
        frozen_manifest() == frozen.get("fixture"),
        "fixture file digests equal the stage P1b freeze",
    )
    add(
        "oracle_not_in_fixture",
        not any(
            "HIDDEN_OK" in path.read_text(encoding="utf-8", errors="replace")
            for path in contract.FIXTURE_ROOT.rglob("*")
            if path.is_file()
        ),
        "hidden marker found in the fixture: none",
    )
    add(
        "single_live_attempt",
        True,
        "the marker is claimed with O_CREAT|O_EXCL after READY and before run_goal",
    )
    return checks


def evaluate(options: GoalRunOptions) -> dict[str, Any]:
    """The admission report: runtime checks plus stage checks. Never calls a model."""

    report = run_live_preflight(options)
    checks = stage_checks(options)
    failures = list(report["blocking_failures"]) + [
        check["check_id"] for check in checks if check["blocking"] and not check["ok"]
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "delegation_mode": contract.DELEGATION_MODE,
        "runtime_status": report["status"],
        "combined_status": READY if report["status"] == READY and not failures else "BLOCKED",
        "combined_blocking_failures": failures,
        "runtime": report,
        "stage_checks": checks,
        "freeze": _freeze(),
    }


def _write_preflight(scratch: Path, report: dict[str, Any], commit: str) -> Path:
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "preflight.json"
    path.write_text(
        json.dumps({**report, "fixture_commit": commit}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def preflight(scratch: Path) -> dict[str, Any]:
    fixture, commit = p1b_runner.build_fixture(scratch / "fixture")
    options = build_options(scratch / "runs", fixture, run_id="stage-b0-preflight")
    combined = evaluate(options)
    combined["fixture_commit"] = commit
    _write_preflight(scratch, combined, commit)
    return combined


# -- the single live attempt ---------------------------------------------------------------------


async def run(scratch: Path) -> dict[str, Any]:
    reason = prior_attempt(scratch)
    if reason is not None:
        raise AttemptAlreadyStarted(reason)

    fixture, commit = p1b_runner.build_fixture(scratch / "fixture")
    run_id = f"stage-b0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    options = build_options(scratch / "runs", fixture, run_id=run_id)

    report = evaluate(options)
    _write_preflight(scratch, report, commit)
    if report["combined_status"] != READY:
        raise SystemExit(f"BLOCKED: {report['combined_blocking_failures']}")

    claim_attempt(
        scratch,
        {
            "stage": "B0",
            "delegation_mode": contract.DELEGATION_MODE,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "goal_digest": contract.goal_digest(),
            "instruction_digest": contract.instruction_digest(),
            "fixture_commit": commit,
            "source_tree_digest": source_identity()["tree_digest"],
            "tool_error_mode": contract.TOOL_ERROR_MODE,
            "git": git_identity(),
        },
    )

    session = await run_goal(options)
    summary = summarize(session, report)
    run_dir = Path(session["run_dir"])
    (run_dir / "stage_b0_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    _collect(run_dir, scratch)
    return summary


def summarize(session: dict[str, Any], preflight_report: dict[str, Any]) -> dict[str, Any]:
    delegation = session.get("delegation") or {}
    batches = delegation.get("batches") or []
    children = [child for batch in batches for child in batch["children"]]
    return {
        "stage": "B0",
        "delegation_mode": contract.DELEGATION_MODE,
        "status": session["status"],
        "acceptance": session["acceptance"],
        "tool_error_mode": session.get("tool_error_mode"),
        "execution_integrity": session.get("execution_integrity"),
        "main": {
            "turns": session["main"]["turns"],
            "tool_calls": session["main"]["tool_calls"],
            "stop_reason": session["main"]["stop_reason"],
            "stop_message": session["main"]["stop_message"],
        },
        "delegation": {
            "rounds": len(batches),
            "routes": [batch["route"] for batch in batches],
            "reason_codes": [batch["decision"]["reason_codes"] for batch in batches],
            "max_observed_concurrency": max(
                (batch["max_observed_concurrency"] for batch in batches), default=0
            ),
            "children": [
                {
                    "key": child["key"],
                    "status": child["status"],
                    "success": child["success"],
                    "turns": child["turns"],
                    "tool_calls": child["tool_calls"],
                    "started_offset_ms": child["started_offset_ms"],
                    "finished_offset_ms": child["finished_offset_ms"],
                    "ref_id": child["ref_id"],
                    "summary_chars": child["summary_chars"],
                }
                for child in children
            ],
        },
        "metrics": session["metrics"],
        "verification": {
            "verdict": (session["verification"] or {}).get("verdict"),
            "static_status": (session["verification"] or {}).get("static_status"),
            "dynamic": (session["verification"] or {}).get("dynamic"),
        },
        "sandbox": session["sandbox"],
        "ownership": {
            key: session["ownership"][key]
            for key in ("mode", "checked_calls", "allowed_calls", "denied_calls")
        },
        "budget": session["budget"],
        "goal_digest": contract.goal_digest(),
        "instruction_digest": contract.instruction_digest(),
        "preflight_status": preflight_report["combined_status"],
        "fixture_commit": preflight_report["fixture_commit"],
        "git": preflight_report["freeze"]["git"],
        "source_tree_digest": preflight_report["freeze"]["source"]["tree_digest"],
    }


def evidence_dir(root: Path = EVIDENCE_ROOT) -> Path:
    """The next unused ``run-N`` directory. Existing evidence is never written into."""

    index = 1
    while (root / f"run-{index}").exists():
        index += 1
    return root / f"run-{index}"


def _collect(run_dir: Path, scratch: Path) -> Path:
    destination = evidence_dir()
    destination.mkdir(parents=True, exist_ok=False)
    for name in (
        "session.json",
        "run_header.json",
        "stage_b0_summary.json",
        "workspace.patch",
        "tool_conversations.json",
    ):
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    trajectory = next((run_dir / "trajectory").glob("*/transcript.jsonl"), None)
    if trajectory is not None:
        shutil.copy2(trajectory, destination / "transcript.jsonl")
    preflight = scratch / "preflight.json"
    if preflight.is_file():
        shutil.copy2(preflight, destination / "preflight.json")
    marker = attempt_marker(scratch)
    if marker.is_file():
        shutil.copy2(marker, destination / "attempt-1.json")
    private = run_dir / "private" / "tool_payloads.jsonl"
    if private.is_file():
        # The payload corpus is private evidence: it stays under the run directory and is never
        # copied into the publishable bundle. Sanitising it requires anycode redaction first.
        (destination / "PRIVATE_PAYLOAD_NOTICE.txt").write_text(
            f"private payload corpus left in {private}\nnot publishable\n", encoding="utf-8"
        )
    return destination


# -- entry point ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B0 controlled delegation run")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze", action="store_true")
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--run", action="store_true")
    parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    scratch = (args.scratch or DEFAULT_SCRATCH).resolve()

    if args.freeze:
        # A freeze after an attempt would rewrite the contract the attempt was run under.
        reason = prior_attempt(scratch)
        if reason is not None:
            raise SystemExit(f"refusing to freeze: {reason}")
        payload = write_freeze()
        print(
            json.dumps(
                {
                    "frozen_source": payload["source"]["tree_digest"],
                    "goal_digest": payload["identity"]["goal_digest"],
                    "path": str(FREEZE_PATH),
                }
            )
        )
        return 0
    if args.preflight:
        report = preflight(scratch)
        print(
            json.dumps(
                {
                    "status": report["combined_status"],
                    "runtime_checks": len(report["runtime"]["checks"]),
                    "stage_checks": len(report["stage_checks"]),
                    "blocking_failures": report["combined_blocking_failures"],
                    "delegation_mode": report["delegation_mode"],
                }
            )
        )
        return 0 if report["combined_status"] == READY else 2

    try:
        summary = asyncio.run(run(scratch))
    except AttemptAlreadyStarted as error:
        raise SystemExit(f"refusing to run: {error}") from error
    print(json.dumps(summary, indent=2, sort_keys=True))
    accepted = (
        summary["delegation"]["rounds"] >= 1
        and len([child for child in summary["delegation"]["children"] if child["success"]]) >= 2
        and summary["delegation"]["max_observed_concurrency"] >= 2
        and summary["acceptance"] == "PASS"
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
