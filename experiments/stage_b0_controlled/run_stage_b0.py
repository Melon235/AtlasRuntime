"""Stage B0 runner: one controlled real delegation run. Prepared, not executed.

    python experiments/stage_b0_controlled/run_stage_b0.py --freeze
    python experiments/stage_b0_controlled/run_stage_b0.py --preflight
    python experiments/stage_b0_controlled/run_stage_b0.py --run

``--run`` performs **one** live DeepSeek attempt. It writes a persistent marker before calling
``run_goal`` and refuses to start when one exists; there is no retry loop. ``--preflight`` never
constructs a provider and never touches the model.

The run is recorded as ``CONTROLLED_DELEGATION`` (see ``b0_contract``): the Main is told by the
Host that the run validates the delegation tool. Nothing is injected into the tool results, no
child is faked, the Router and the worker factory are the production ones, and the Main's own loop
does the integration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
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
EVIDENCE_DIR = HERE / "evidence" / "run-1"
#: Outside the repository by default: run artefacts must never dirty the worktree.
DEFAULT_SCRATCH = Path(
    os.environ.get("ATLAS_B0_SCRATCH") or (Path(tempfile.gettempdir()) / "atlas-b0-live")
)


# -- freeze -------------------------------------------------------------------------------------


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
        "fixture": frozen_manifest(),
        "p1b_fixture_manifest_digest": contract.digest(
            (P1B_DIR / "fixture_manifest.json").read_text(encoding="utf-8")
        ),
        "runtime": _runtime_identity(),
    }
    FREEZE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _runtime_identity() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    return {
        "git_head": head,
        "git_branch": branch,
        "git_status": dirty,
        "main_system_prompt_digest": contract.digest(MAIN_SYSTEM_PROMPT),
    }


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
        ),
        acceptance=contract.acceptance(),
        public_tests=contract.PUBLIC_TESTS,
        run_id=run_id,
    )


# -- preflight ----------------------------------------------------------------------------------


def stage_checks(options: GoalRunOptions) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"check_id": check_id, "ok": ok, "blocking": blocking, "detail": detail})

    add(
        "goal_frozen", contract.goal_digest() == _freeze()["identity"]["goal_digest"], "goal digest"
    )
    add(
        "instruction_frozen",
        contract.instruction_digest() == _freeze()["identity"]["controlled_instruction_digest"],
        "controlled instruction digest",
    )
    add(
        "delegation_mode_labelled",
        contract.DELEGATION_MODE == "CONTROLLED_DELEGATION"
        and "CONTROLLED_DELEGATION" not in contract.GOAL.split("HOST-AUTHORED")[0],
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
        f"sha256:{contract.digest(MAIN_SYSTEM_PROMPT).split(':')[1][:16]}",
    )
    add(
        "goal_passed_to_runtime", options.goal == contract.GOAL, "run options carry the frozen goal"
    )
    add(
        "fixture_matches_p1b",
        _fixture_matches(),
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
        "the attempt marker is written just before run_goal and never removed by this script",
    )
    return checks


def _fixture_matches() -> bool:
    p1b = json.loads((P1B_DIR / "fixture_manifest.json").read_text(encoding="utf-8"))
    return frozen_manifest()["files"] == p1b.get("files")


def _freeze() -> dict[str, Any]:
    if not FREEZE_PATH.is_file():
        raise SystemExit(f"no freeze at {FREEZE_PATH}; run --freeze first")
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


def preflight(scratch: Path) -> dict[str, Any]:
    fixture, commit = p1b_runner.build_fixture(scratch / "fixture")
    options = build_options(scratch / "runs", fixture, run_id="stage-b0-preflight")
    report = run_live_preflight(options)
    checks = stage_checks(options)
    failures = list(report["blocking_failures"]) + [
        check["check_id"] for check in checks if check["blocking"] and not check["ok"]
    ]
    combined = {
        "generated_at": datetime.now(UTC).isoformat(),
        "delegation_mode": contract.DELEGATION_MODE,
        "runtime_status": report["status"],
        "combined_status": READY if report["status"] == READY and not failures else "BLOCKED",
        "combined_blocking_failures": failures,
        "fixture_commit": commit,
        "runtime": report,
        "stage_checks": checks,
        "freeze": _freeze(),
    }
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "preflight.json").write_text(
        json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8"
    )
    return combined


# -- the single live attempt ---------------------------------------------------------------------


async def run(scratch: Path) -> dict[str, Any]:
    attempt = scratch / "attempts" / "attempt-1.json"
    if attempt.exists():
        raise SystemExit(
            f"attempt marker already exists at {attempt}: stage B0 allows exactly one live attempt"
        )
    report = preflight(scratch)
    if report["combined_status"] != READY:
        raise SystemExit(f"BLOCKED: {report['combined_blocking_failures']}")
    fixture, commit = p1b_runner.build_fixture(scratch / "fixture")
    runs_root = scratch / "runs"
    run_id = f"stage-b0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    options = build_options(runs_root, fixture, run_id=run_id)
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_text(
        json.dumps(
            {
                "stage": "B0",
                "delegation_mode": contract.DELEGATION_MODE,
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "goal_digest": contract.goal_digest(),
                "instruction_digest": contract.instruction_digest(),
                "fixture_commit": commit,
                "runtime": _runtime_identity(),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    session = await run_goal(options)
    summary = summarize(session, report)
    run_dir = Path(session["run_dir"])
    (run_dir / "stage_b0_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    _collect(run_dir)
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
        "runtime": preflight_report["freeze"]["runtime"],
        "fixture_commit": preflight_report["fixture_commit"],
    }


def _collect(run_dir: Path) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    for name in (
        "session.json",
        "run_header.json",
        "stage_b0_summary.json",
        "workspace.patch",
        "tool_conversations.json",
    ):
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, EVIDENCE_DIR / name)
    trajectory = next((run_dir / "trajectory").glob("*/transcript.jsonl"), None)
    if trajectory is not None:
        shutil.copy2(trajectory, EVIDENCE_DIR / "transcript.jsonl")
    private = run_dir / "private" / "tool_payloads.jsonl"
    if private.is_file():
        # The payload corpus is private evidence: it is archived under the run directory only. If a
        # sanitised copy is needed, it must be redacted with anycode.security.redaction first.
        (EVIDENCE_DIR / "PRIVATE_PAYLOAD_NOTICE.txt").write_text(
            f"private payload corpus left in {private}\nnot publishable\n", encoding="utf-8"
        )


# -- entry point ---------------------------------------------------------------------------------


def _drop_read_only(function: Any, path: str, _: Any) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


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
    if scratch.exists() and args.freeze:
        shutil.rmtree(scratch, onexc=_drop_read_only)

    if args.freeze:
        payload = write_freeze()
        print(json.dumps({"frozen": payload["identity"]["goal_digest"], "path": str(FREEZE_PATH)}))
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

    summary = asyncio.run(run(scratch))
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
