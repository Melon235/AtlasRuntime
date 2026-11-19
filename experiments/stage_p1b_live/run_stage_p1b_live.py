"""Stage P1b: one authorized live run of a Main-driven multi-agent coding task.

Not part of the test suite and not a benchmark runner. It builds the dual-plugin fixture as a clean
git repository, admits the run through ``run_live_preflight`` plus the P1b freeze checks, and then
executes ``run_goal`` **exactly once**.

    python experiments/stage_p1b_live/run_stage_p1b_live.py --preflight
    python experiments/stage_p1b_live/run_stage_p1b_live.py --run

There is no retry loop and no fallback. ``--preflight`` never builds a provider and never creates an
attempt marker. ``--run`` creates a persistent attempt marker before the first model call and
refuses to start when one already exists, so a second live attempt requires deleting that marker by
hand, which the stage rules forbid without a new approval.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

import p1b_contract as contract  # noqa: E402

from atlasos.actions.binding import canonical_digest  # noqa: E402
from atlasos.app.goal_sandbox import GoalSandboxOptions  # noqa: E402
from atlasos.app.live_preflight import READY, run_live_preflight  # noqa: E402
from atlasos.app.main_driven import MAIN_SYSTEM_PROMPT, GoalRunOptions, run_goal  # noqa: E402
from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig  # noqa: E402
from atlasos.delegation import DelegationConfig  # noqa: E402
from atlasos.delegation.router import STAGE_M_POLICY_VERSION, DelegationRouter  # noqa: E402

ATTEMPT_MARKER = "attempt-1.json"


# -- provenance helpers -------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def fixture_manifest() -> dict[str, str]:
    files: list[str] = []
    for path in sorted(contract.FIXTURE_ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(contract.FIXTURE_ROOT)
        if any(part in contract.FIXTURE_SKIP for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return {
        name: "sha256:" + hashlib.sha256((contract.FIXTURE_ROOT / name).read_bytes()).hexdigest()
        for name in files
    }


def build_fixture(destination: Path) -> tuple[Path, str]:
    """Materialise the frozen fixture as a clean repository with one commit, outside the repo.

    Rebuilding from the frozen manifest on every invocation is deliberate: ``--preflight`` is meant
    to be run before ``--run`` in the same scratch directory, and a stale copy must never be able to
    differ from what the freeze recorded.
    """

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in fixture_manifest():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(contract.FIXTURE_ROOT / name, target)

    def run_git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(destination), *args], check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    run_git("init", "-q", "-b", "main")
    run_git("config", "user.email", "stage-p1b@atlasruntime.invalid")
    run_git("config", "user.name", "Stage P1b Fixture")
    run_git("add", "-A")
    run_git(
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture: dual event channel project without the unified normalization path",
    )
    return destination, run_git("rev-parse", "HEAD")


# -- configuration ------------------------------------------------------------


def sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        provider="opensandbox",
        endpoint=os.environ.get("OPEN_SANDBOX_ENDPOINT", "localhost:8080"),
        protocol="http",
        image=contract.IMAGE,
        workspace_root="/workspace/project",
        network="none",
        timeout_minutes=20,
        ready_timeout_seconds=240,
        cpu="1",
        memory="1Gi",
        use_server_proxy=True,
        retain_on_failure=False,
    )


def build_options(run_root: Path, fixture: Path, *, run_id: str | None = None) -> GoalRunOptions:
    return GoalRunOptions(
        goal=contract.GOAL,
        runs_root=run_root,
        workspace_root=run_root / "host-workspace",
        config=AtlasConfig(tools=ToolsConfig(mode="progressive"), sandbox=sandbox_config()),
        delegation=DelegationConfig(
            enabled=True,
            max_parallel_children=2,
            max_children_per_call=2,
            max_delegation_rounds=4,
            child_timeout_seconds=300.0,
            child_max_turns=6,
            result_preview_chars=900,
        ),
        provider=contract.PROVIDER,
        provider_factory=None,
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


# -- P1b freeze checks (appended to the runtime preflight report) -------------


def freeze_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"check_id": check_id, "ok": ok, "blocking": blocking, "detail": detail})

    frozen = json.loads(contract.FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    expected = frozen.get("files") or {}
    actual = fixture_manifest()
    changed = sorted(n for n in set(expected) | set(actual) if expected.get(n) != actual.get(n))
    add(
        "fixture_manifest_frozen",
        not changed,
        f"{len(expected)} frozen files; changed: {changed}",
    )

    goal_digest = "sha256:" + hashlib.sha256(contract.GOAL.encode("utf-8")).hexdigest()
    add(
        "goal_frozen",
        goal_digest == frozen.get("goal_digest"),
        f"goal digest {goal_digest}",
    )

    prompt_digest = "sha256:" + hashlib.sha256(MAIN_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    add(
        "main_prompt_unchanged",
        prompt_digest == contract.MAIN_SYSTEM_PROMPT_DIGEST,
        f"MAIN_SYSTEM_PROMPT {prompt_digest} (stage P0.1 froze "
        f"{contract.MAIN_SYSTEM_PROMPT_DIGEST})",
    )

    runtime_dirty = git("status", "--porcelain", "src/")
    add(
        "runtime_tree_unmodified",
        not runtime_dirty,
        "src/ is clean" if not runtime_dirty else f"src/ has changes: {runtime_dirty}",
    )

    leaks = [
        name
        for name in actual
        if "HIDDEN_OK" in (contract.FIXTURE_ROOT / name).read_text(encoding="utf-8")
        or "HIDDEN_FAIL" in (contract.FIXTURE_ROOT / name).read_text(encoding="utf-8")
    ]
    add("oracle_not_in_fixture", not leaks, f"hidden marker found in: {leaks}")

    add(
        "public_oracle_separate",
        contract.PUBLIC_TESTS.command != contract.ORACLE_COMMAND,
        "the public suite and the oracle command differ",
    )
    add(
        "main_write_scope_frozen",
        contract.MAIN_WRITE_PATHS == ("core/",),
        f"main_write_paths={list(contract.MAIN_WRITE_PATHS)}",
    )
    add(
        "single_live_attempt",
        True,
        "the attempt marker is written just before run_goal and never removed by this script",
    )
    return checks


def _header(options: GoalRunOptions, fixture_commit: str) -> dict[str, Any]:
    accept = options.acceptance
    assert accept is not None
    router = DelegationRouter()
    policy = router.policy_for(options.delegation.max_children_per_call)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_status": git("status", "--porcelain"),
        "fixture_commit": fixture_commit,
        "fixture_manifest": fixture_manifest(),
        "goal": contract.GOAL,
        "goal_digest": "sha256:" + hashlib.sha256(contract.GOAL.encode("utf-8")).hexdigest(),
        "main_system_prompt_digest": "sha256:"
        + hashlib.sha256(MAIN_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "provider": options.provider,
        "model": options.model,
        "main_max_turns": options.max_turns,
        "token_budget": options.token_budget,
        "main_write_paths": list(options.main_write_paths),
        "delegation": options.delegation.model_dump(),
        "sandbox": options.config.sandbox.model_dump(),
        "public_test_command": contract.PUBLIC_TESTS.command,
        "public_test_command_digest": contract.PUBLIC_TESTS.command_digest(),
        "oracle_command_digest": canonical_digest(contract.ORACLE_COMMAND),
        "acceptance_spec_digest": accept.spec.digest,
        "routing_policy_version": STAGE_M_POLICY_VERSION,
        "routing_policy_digest": policy.digest(),
        "routing_policy_max_parallel_workers": policy.max_parallel_workers,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def patch_paths(patch_text: str) -> list[str]:
    """Files the Main actually changed, read from the exported unified diff.

    Parsed with a regular expression rather than line-by-line because the OpenSandbox provider
    joins its log messages without a separator (``opensandbox_provider._execution_text``), so a
    command's stdout arrives as one collapsed string. The runtime is frozen for this stage, so the
    parser adapts instead of the runtime. ``__pycache__`` entries are ignored: bytecode noise is not
    an edit.
    """

    # Only the left-hand path is captured: with newlines collapsed the right-hand one runs straight
    # into the ``index <mode>`` field, while the left-hand one is still followed by a space.
    paths = {
        left
        for left in re.findall(r"diff --git a/(\S+) b/", patch_text)
        if "__pycache__" not in left
    }
    return sorted(paths)


def summarize(session: dict[str, Any]) -> dict[str, Any]:
    delegation = session["delegation"]
    batches = delegation["batches"] if delegation else []
    patch_file = Path(session["artifacts"]["patch_path"]) if session["artifacts"] else None
    patch_text = (
        patch_file.read_text(encoding="utf-8")
        if patch_file is not None and patch_file.exists()
        else ""
    )
    touched = patch_paths(patch_text)
    return {
        "status": session["status"],
        "execution_succeeded": session["execution_succeeded"],
        "failure": session["failure"],
        "acceptance": session["acceptance"],
        "main": session["main"],
        "metrics": session["metrics"],
        "delegation_enabled": session["delegation_enabled"],
        "delegation": {
            "batches": [
                {
                    "round": batch["round"],
                    "route": batch["route"],
                    "reason_codes": batch["decision"]["reason_codes"],
                    "blocked_gates": batch["decision"]["blocked_gates"],
                    "policy_version": batch["decision"]["policy_version"],
                    "policy_digest": batch["decision"]["policy_digest"],
                    "max_wave_width": batch["decision"]["max_wave_width"],
                    "max_observed_concurrency": batch["max_observed_concurrency"],
                    "batch_span_ms": batch["batch_span_ms"],
                    "result_chars": batch["result_chars"],
                    "children": [
                        {
                            key: child[key]
                            for key in (
                                "key",
                                "worker_id",
                                "profile_id",
                                "status",
                                "success",
                                "stop_reason",
                                "turns",
                                "tool_calls",
                                "started_offset_ms",
                                "finished_offset_ms",
                                "prompt_tokens",
                                "total_prompt_tokens",
                                "cache_hit_tokens",
                                "cache_miss_tokens",
                                "completion_tokens",
                                "output_chars",
                                "summary_chars",
                                "ref_id",
                                "error_code",
                                "error_message",
                            )
                        }
                        for child in batch["children"]
                    ],
                }
                for batch in batches
            ],
            "totals": delegation["totals"] if delegation else None,
        },
        "public_tests": session["public_tests"],
        "sandbox": session["sandbox"],
        "ownership": {
            key: session["ownership"][key]
            for key in (
                "mode",
                "coverage",
                "checked_calls",
                "allowed_calls",
                "denied_calls",
                "denials",
            )
        },
        "budget": session["budget"],
        "verification": session["verification"],
        "artifacts": session["artifacts"],
        "isolation": session["isolation"],
        "handoff_reads": session["handoff_reads"],
        "tool_surface": session["tool_surface"],
        "patch_touched_paths": touched,
        "patch_within_write_scope": all(path.startswith("core/") for path in touched),
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage P1b live Multi-Agent runner")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(os.environ.get("ATLAS_P1B_SCRATCH", Path.cwd() / ".stage-p1b-live")),
    )
    args = parser.parse_args(argv)
    if not (args.preflight or args.run):
        parser.error("choose --preflight or --run")

    load_dotenv(REPO_ROOT / ".env")
    scratch: Path = args.scratch
    marker = scratch / "attempts" / ATTEMPT_MARKER
    if args.run and marker.exists():
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "reason": "an attempt marker already exists; a second live attempt is not "
                    "authorized",
                    "marker": str(marker),
                    "marker_payload": json.loads(marker.read_text(encoding="utf-8")),
                },
                indent=2,
            )
        )
        return 3

    run_root = scratch / "runs"
    fixture, commit = build_fixture(scratch / "fixture")
    options = build_options(run_root, fixture)
    header = _header(options, commit)

    report = run_live_preflight(options)
    p1b = freeze_checks()
    report["p1b_checks"] = p1b
    blocking = report["blocking_failures"] + [
        item["check_id"] for item in p1b if item["blocking"] and not item["ok"]
    ]
    report["combined_status"] = READY if not blocking else "BLOCKED"
    report["combined_blocking_failures"] = blocking
    await asyncio.to_thread(_write_json, scratch / "preflight.json", report)
    print(
        json.dumps(
            {
                "status": report["combined_status"],
                "runtime_checks": len(report["checks"]),
                "p1b_checks": len(p1b),
                "blocking_failures": blocking,
            }
        )
    )
    if blocking:
        print("preflight is BLOCKED; no provider or sandbox was created", file=sys.stderr)
        return 2
    if args.preflight:
        return 0

    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        marker,
        {
            "attempt": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "git_head": header["git_head"],
            "fixture_commit": commit,
            "goal_digest": header["goal_digest"],
            "model": options.model,
            "token_budget": options.token_budget,
            "scratch": str(scratch),
        },
    )

    started = datetime.now(UTC)
    session = await run_goal(options)
    finished = datetime.now(UTC)
    run_dir = Path(session["run_dir"])
    header["run_dir"] = str(run_dir)
    header["started_at"] = started.isoformat()
    header["finished_at"] = finished.isoformat()
    header["wall_clock_seconds"] = round((finished - started).total_seconds(), 3)
    await asyncio.to_thread(_write_json, run_dir / "run_header.json", header)

    summary = summarize(session)
    summary["wall_clock_seconds"] = header["wall_clock_seconds"]
    await asyncio.to_thread(_write_json, run_dir / "stage_p1b_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if session["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
