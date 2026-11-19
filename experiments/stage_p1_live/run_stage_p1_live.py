"""Stage P1: one authorized live run of the Main-driven coding pipeline.

Not part of the test suite and not a benchmark runner. It builds the calculator fixture as a clean
git repository, admits the run through ``run_live_preflight``, executes ``run_goal`` exactly once,
and writes the evidence pointers next to the run directory the runtime creates.

    python experiments/stage_p1_live/run_stage_p1_live.py --preflight
    python experiments/stage_p1_live/run_stage_p1_live.py --run

There is no retry loop and no fallback: a BLOCKED preflight exits 2, and a failed run exits 1 with
its evidence left in place. Re-running the goal means re-running this command by hand, which the
stage rules forbid without a new approval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlasos.app.goal_sandbox import GoalSandboxOptions  # noqa: E402
from atlasos.app.goal_verification import GoalAcceptance  # noqa: E402
from atlasos.app.live_preflight import READY, run_live_preflight  # noqa: E402
from atlasos.app.main_driven import GoalRunOptions, run_goal  # noqa: E402
from atlasos.app.public_tests import PublicTestContract  # noqa: E402
from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig  # noqa: E402
from atlasos.delegation import DelegationConfig  # noqa: E402
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "stage_p1_calculator"
FIXTURE_SKIP = {"build.py", "__pycache__", ".pytest_cache"}

MODEL = "deepseek-chat"
IMAGE = "atlas-uat-python:3.12"
TOKEN_BUDGET = 240_000
MAIN_MAX_TURNS = 12
MAIN_WRITE_PATHS = ("app/calc.py",)

GOAL = (
    "The calculator project under this workspace is missing subtract(a, b) in app/calc.py; the "
    "public test suite expects it and currently fails. Three modules take part (app/calc.py, "
    "app/text.py, app/units.py) and app/units.py builds on app/calc.py, so check what each module "
    "provides and what would break before you change anything. Implement subtract without changing "
    "the existing public behaviour, and use the public test suite to check the result."
)

#: The command the Main may run. Host-fixed, one file, no arguments from the model.
PUBLIC_TESTS = PublicTestContract(
    test_id="public",
    command="python3 -B -m pytest -q tests/test_public.py",
    timeout_seconds=180,
    max_output_chars=4000,
    max_calls=4,
)

#: The harness oracle. The Main never sees this string: it is not a tool, and it is never written
#: into the workspace, so it cannot be read from the sandbox filesystem either.
HIDDEN_CHECK = (
    'python3 -c "from app.calc import add, subtract; '
    "from app.text import normalize; "
    "from app.units import GRAMS_PER_KILOGRAM, total_grams; "
    "assert add(2, 3) == 5; "
    "assert subtract(9, 4) == 5; "
    "assert subtract(0, 0) == 0; "
    "assert subtract(-3, -1) == -2; "
    "assert normalize('  a  ') == 'a'; "
    "assert GRAMS_PER_KILOGRAM == 1000; "
    "assert total_grams(1, 2) == 3000; "
    "print('HIDDEN_OK')\""
)
ORACLE_COMMAND = f"python3 -m pytest -q && {HIDDEN_CHECK}"


def acceptance() -> GoalAcceptance:
    spec = VerificationSpec(
        spec_id="stage-p1-calculator",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=("app/calc.py", "app/text.py", "app/units.py"),
        checks=(
            CheckSpec(
                check_id="calc-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="app/calc.py",
            ),
            CheckSpec(
                check_id="units-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="app/units.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(spec=spec, dynamic_command=ORACLE_COMMAND, dynamic_timeout_seconds=300)


def build_fixture(destination: Path) -> tuple[Path, str]:
    """Materialise the fixture as a clean repository with one commit, outside the repo."""

    if destination.exists():
        raise SystemExit(f"fixture destination already exists: {destination}")
    destination.mkdir(parents=True)
    for source in sorted(FIXTURE_ROOT.rglob("*")):
        if any(part in FIXTURE_SKIP for part in source.relative_to(FIXTURE_ROOT).parts):
            continue
        if source.is_dir():
            continue
        target = destination / source.relative_to(FIXTURE_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(destination), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "stage-p1@atlasruntime.invalid")
    git("config", "user.name", "Stage P1 Fixture")
    git("add", "-A")
    git(
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture: calculator project without subtract",
    )
    return destination, git("rev-parse", "HEAD")


def sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        provider="opensandbox",
        endpoint=os.environ.get("OPEN_SANDBOX_ENDPOINT", "localhost:8080"),
        protocol="http",
        image=IMAGE,
        workspace_root="/workspace/project",
        network="none",
        timeout_minutes=20,
        ready_timeout_seconds=240,
        cpu="1",
        memory="1Gi",
        use_server_proxy=True,
        # The goal path always destroys its sandbox (``GoalSandboxOptions.retain_on_failure``
        # decides that); this flag belongs to the coding composition and stays off here.
        retain_on_failure=False,
    )


def build_options(run_root: Path, fixture: Path) -> GoalRunOptions:
    run_id = f"stage-p1-live-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    return GoalRunOptions(
        goal=GOAL,
        runs_root=run_root,
        # Host label only: the sandbox workspace is what the tools see.
        workspace_root=run_root / "host-workspace",
        config=AtlasConfig(
            tools=ToolsConfig(mode="progressive"),
            sandbox=sandbox_config(),
        ),
        delegation=DelegationConfig(
            max_parallel_children=2,
            max_children_per_call=2,
            max_delegation_rounds=4,
            child_timeout_seconds=300.0,
            child_max_turns=6,
            result_preview_chars=900,
        ),
        provider="deepseek",
        provider_factory=None,
        model=MODEL,
        max_turns=MAIN_MAX_TURNS,
        token_budget=TOKEN_BUDGET,
        main_write_paths=MAIN_WRITE_PATHS,
        sandbox=GoalSandboxOptions(
            provider="opensandbox",
            source_repo=fixture,
            image=IMAGE,
            workspace_root="/workspace/project",
            require_real=True,
        ),
        acceptance=acceptance(),
        public_tests=PUBLIC_TESTS,
        run_id=run_id,
    )


def _header(options: GoalRunOptions, fixture_commit: str) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    accept = options.acceptance
    assert accept is not None
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "git_head": head,
        "git_branch": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "fixture_commit": fixture_commit,
        "fixture_repo": str(options.sandbox.source_repo) if options.sandbox else None,
        "provider": options.provider,
        "model": options.model,
        "main_max_turns": options.max_turns,
        "token_budget": options.token_budget,
        "main_write_paths": list(options.main_write_paths),
        "delegation": options.delegation.model_dump(),
        "sandbox": options.config.sandbox.model_dump(),
        "public_test_command": PUBLIC_TESTS.command,
        "public_test_command_digest": PUBLIC_TESTS.command_digest(),
        "oracle_command_digest": __import__(
            "atlasos.actions.binding", fromlist=["canonical_digest"]
        ).canonical_digest(ORACLE_COMMAND),
        "acceptance_spec_digest": accept.spec.digest,
        "routing_policy_version": "m.1.0",
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage P1 live pipeline runner")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(os.environ.get("ATLAS_P1_SCRATCH", Path.cwd() / ".stage-p1-live")),
    )
    args = parser.parse_args(argv)
    if not (args.preflight or args.run):
        parser.error("choose --preflight or --run")

    load_dotenv(REPO_ROOT / ".env")
    scratch: Path = args.scratch
    run_root = scratch / "runs"
    fixture, commit = build_fixture(scratch / "fixture")
    options = build_options(run_root, fixture)
    header = _header(options, commit)

    report = run_live_preflight(options)
    # ``build_fixture`` already created the scratch root, so only the report is written here.
    await asyncio.to_thread(_write_json, scratch / "preflight.json", report)
    print(json.dumps({"status": report["status"], "failures": report["blocking_failures"]}))
    if report["status"] != READY:
        print("preflight is BLOCKED; no provider or sandbox was created", file=sys.stderr)
        return 2
    if args.preflight:
        return 0

    session = await run_goal(options)
    run_dir = Path(session["run_dir"])
    header["run_dir"] = str(run_dir)
    await asyncio.to_thread(_write_json, run_dir / "run_header.json", header)
    summary = {
        "status": session["status"],
        "acceptance": session["acceptance"],
        "execution_succeeded": session["execution_succeeded"],
        "failure": session["failure"],
        "main": session["main"],
        "metrics": session["metrics"],
        "delegation": {
            "route": [batch["route"] for batch in session["delegation"]["batches"]],
            "totals": session["delegation"]["totals"],
            "max_observed_concurrency": [
                batch["max_observed_concurrency"] for batch in session["delegation"]["batches"]
            ],
            "children": [
                {
                    key: child[key]
                    for key in (
                        "key",
                        "profile_id",
                        "status",
                        "success",
                        "turns",
                        "tool_calls",
                        "started_offset_ms",
                        "finished_offset_ms",
                        "ref_id",
                        "error_code",
                    )
                }
                for batch in session["delegation"]["batches"]
                for child in batch["children"]
            ],
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
    }
    await asyncio.to_thread(_write_json, run_dir / "stage_p1_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if session["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
