"""Stage S2 runner: Main System Prompt v2.1 final pilot.

    python experiments/stage_s2_prompt_v21/run_stage_s2.py --offline
    python experiments/stage_s2_prompt_v21/run_stage_s2.py --freeze
    python experiments/stage_s2_prompt_v21/run_stage_s2.py --preflight
    python experiments/stage_s2_prompt_v21/run_stage_s2.py --run

``--offline`` proves the fixture is solvable, that the prompt bytes reach ``AgentConfig`` verbatim,
that the prompt's description of the research worker matches the catalog, and that the frozen router
admits the two independent investigations this fixture contains. It never calls a model.

``--run`` performs **one** live DeepSeek attempt and nothing else. It claims an attempt marker with
``O_CREAT | O_EXCL`` immediately before ``run_goal`` — never during the preflight — and every entry
point refuses afterwards, so neither a fresh scratch directory nor a second invocation can produce a
second attempt. There is no retry path and no fallback provider.

The run is recorded as ``AUTONOMOUS_MAIN_DELEGATION``: nothing in the goal, the prompt or the tool
surface tells the Main to delegate. Whatever it chooses is the observation.
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
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

import s2_analysis as analysis  # noqa: E402
import s2_contract as contract  # noqa: E402
import s2_fixture as fixture_builder  # noqa: E402
import s2_offline as offline  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from atlasos.app.goal_sandbox import GoalSandboxOptions  # noqa: E402
from atlasos.app.live_preflight import READY, run_live_preflight  # noqa: E402
from atlasos.app.main_driven import (  # noqa: E402
    MAIN_PROMPT_V1,
    MAIN_SYSTEM_PROMPT,
    GoalRunOptions,
    MainPromptSpec,
    run_goal,
)
from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig  # noqa: E402
from atlasos.delegation import DelegationConfig  # noqa: E402
from atlasos.delegation.router import STAGE_M_POLICY_VERSION  # noqa: E402

FREEZE_PATH = HERE / "freeze.json"
EVIDENCE_ROOT = HERE / "evidence"
OFFLINE_PATH = HERE / "offline.json"
RUNTIME_SOURCE_ROOT = REPO_ROOT / "src" / "atlasos"
HARNESS_FILES = (
    "run_stage_s2.py",
    "s2_contract.py",
    "s2_fixture.py",
    "s2_offline.py",
    "s2_analysis.py",
    "reference/engine_reference.py",
    "reference/report_reference.py",
    "prompts/main_v21.md",
)
#: The commit this stage starts from. S2 changes no runtime file at all.
BASELINE_COMMIT = "1d03a2f6efb1e4ae45ccf8130d57917fe08eae3a"
#: The delegation machinery S2 promises not to modify. Recorded as a negative check, not a comment.
PROTECTED_RUNTIME_FILES = (
    "src/atlasos/delegation/router.py",
    "src/atlasos/delegation/service.py",
    "src/atlasos/delegation/tool.py",
    "src/atlasos/delegation/handoff.py",
    "src/atlasos/delegation/models.py",
    "src/atlasos/workers/factory.py",
    "src/atlasos/workers/routing.py",
    "src/atlasos/workers/renderer.py",
    "src/atlasos/workers/catalog.py",
    "src/atlasos/handoff.py",
)
#: Fixture roots of earlier stages: S2 must not reuse any of them.
HISTORICAL_FIXTURES = (
    "experiments/fixtures/stage_p1_calculator",
    "experiments/fixtures/stage_p1b_dual_plugin",
    "experiments/fixtures/stage_s1_releaseguard",
    "experiments/fixtures/uat_repo",
)
#: Task vocabulary of those stages. None of it may appear in this stage's goal or fixture paths.
HISTORICAL_TASK_MARKERS = (
    "eventpipe",
    "plugins/file_events",
    "plugins/message_events",
    "calculator",
    "commerce",
    "checkout",
    "releaseguard",
    "artifact",
    "manifest",
    "license",
    "checksum",
    "wheel",
)
#: Earlier fixture manifests, used to prove this file set is not a rename of an old one.
EARLIER_FIXTURE_MANIFESTS = (
    REPO_ROOT / "experiments" / "stage_p1b_live" / "fixture_manifest.json",
    REPO_ROOT / "experiments" / "stage_s1_prompt_v2" / "freeze.json",
)
#: Outside the repository by default: run artefacts must never dirty the worktree.
DEFAULT_SCRATCH = Path(
    os.environ.get("ATLAS_S2_SCRATCH") or (Path(tempfile.gettempdir()) / "atlas-s2-live")
)


class AttemptAlreadyStarted(RuntimeError):
    """A Stage S2 live attempt exists. Nothing may start a second one."""


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
    files = _tree_files(RUNTIME_SOURCE_ROOT)
    return {
        "root": "src/atlasos",
        "file_count": len(files),
        "tree_digest": contract.digest(json.dumps(files, sort_keys=True)),
        "files": files,
    }


def harness_identity() -> dict[str, str]:
    return {name: contract.digest_bytes((HERE / name).read_bytes()) for name in HARNESS_FILES}


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


#: Files this stage writes into the repository *during* admission. A freeze cannot contain the hash
#: of the commit that adds it, so these are excluded from the cleanliness check and listed instead.
GENERATED_BY_ADMISSION = (
    "experiments/stage_s2_prompt_v21/freeze.json",
    "experiments/stage_s2_prompt_v21/offline.json",
)


def git_identity() -> dict[str, Any]:
    status = git("status", "--porcelain")
    lines = [line for line in status.splitlines() if line.strip()]
    own = [line for line in lines if any(name in line for name in GENERATED_BY_ADMISSION)]
    rest = [line for line in lines if line not in own]
    return {
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_status": "\n".join(rest),
        "git_status_excluded_files": own,
        "baseline_commit": BASELINE_COMMIT,
        "main_system_prompt_digest": contract.digest(MAIN_SYSTEM_PROMPT),
    }


def changed_since_baseline(paths: tuple[str, ...] = ()) -> list[str]:
    argv = ["diff", "--name-only", BASELINE_COMMIT, "--", *paths]
    return [line for line in git(*argv).splitlines() if line.strip()]


def is_ancestor(commit: str) -> bool:
    if not commit:
        return False
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
    )
    return completed.returncode == 0


# -- freeze -------------------------------------------------------------------------------------


def write_freeze() -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": contract.frozen_identity(),
        "source": source_identity(),
        "harness": harness_identity(),
        "oracle": {
            "program_digest": contract.digest(contract.HIDDEN_PROGRAM),
            "program_chars": len(contract.HIDDEN_PROGRAM),
            "command_digest": contract.digest(contract.ORACLE_COMMAND),
        },
        "fixture": {
            "files": fixture_builder.fixture_manifest(),
            "fixture_digest": fixture_builder.fixture_digest(),
        },
        "offline": {
            "path": "experiments/stage_s2_prompt_v21/offline.json",
            "digest": (
                contract.digest_bytes(OFFLINE_PATH.read_bytes()) if OFFLINE_PATH.is_file() else None
            ),
        },
        "git": git_identity(),
        "runtime_delta_since_baseline": changed_since_baseline(("src/",)),
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
    marker = attempt_marker(scratch)
    if marker.exists():
        return f"attempt marker exists: {marker}"
    evidence = sorted(EVIDENCE_ROOT.glob("run-*"))
    if evidence:
        return f"collected evidence exists: {evidence[0]}"
    return None


def claim_attempt(scratch: Path, payload: dict[str, Any]) -> Path:
    marker = attempt_marker(scratch)
    marker.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise AttemptAlreadyStarted(f"attempt marker already exists: {marker}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return marker


# -- options ------------------------------------------------------------------------------------


def sandbox_config() -> SandboxConfig:
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


def main_prompt_spec() -> MainPromptSpec:
    """The prompt under test, built from the frozen file and nothing else."""

    return MainPromptSpec(version=contract.PROMPT_V21_VERSION, text=contract.prompt_v21_text())


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
        main_prompt=main_prompt_spec(),
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


def reuse_evidence() -> dict[str, Any]:
    """Whether this task is genuinely new, stated as files and words rather than a promise."""

    fixture_root = contract.FIXTURE_ROOT.resolve()
    inside_history = [
        relative
        for relative in HISTORICAL_FIXTURES
        if str(fixture_root).startswith(str((REPO_ROOT / relative).resolve()))
    ]
    goal_markers = [marker for marker in HISTORICAL_TASK_MARKERS if marker in contract.GOAL.lower()]
    manifest = fixture_builder.fixture_manifest()
    fixture_markers = [
        marker
        for marker in HISTORICAL_TASK_MARKERS
        if any(marker in path.lower() for path in manifest)
    ]
    # Reuse is identical *content* under a new name, not a shared path: two projects may both have a
    # README.md, and that is not a rename.
    shared: list[str] = []
    for path in EARLIER_FIXTURE_MANIFESTS:
        if not path.is_file():
            continue
        frozen = json.loads(path.read_text(encoding="utf-8"))
        previous = frozen.get("files") or frozen.get("fixture", {}).get("files") or {}
        shared.extend(name for name, digest in manifest.items() if previous.get(name) == digest)
    shared = sorted(set(shared))
    ok = not inside_history and not goal_markers and not fixture_markers and not shared
    return {
        "ok": ok,
        "inside_historical_fixture": inside_history,
        "goal_markers": goal_markers,
        "fixture_markers": fixture_markers,
        "identical_files_with_earlier_fixtures": shared,
        "detail": (
            f"fixture root {contract.FIXTURE_ROOT.name} is new; goal markers {goal_markers}; "
            f"fixture markers {fixture_markers}; byte-identical files shared with earlier fixtures "
            f"{shared}"
        ),
    }


def stage_checks(options: GoalRunOptions, offline_report: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"check_id": check_id, "ok": ok, "blocking": blocking, "detail": detail})

    frozen = _freeze()
    identity = frozen.get("identity") or {}
    prompt = identity.get("prompt") or {}
    source = source_identity()
    harness = harness_identity()
    delta = [path.replace(os.sep, "/") for path in changed_since_baseline(("src/",))]
    protected_delta = [path for path in delta if path in set(PROTECTED_RUNTIME_FILES)]

    add(
        "runtime_source_frozen",
        source == frozen.get("source"),
        f"tree digest {source['tree_digest'][:23]} over {source['file_count']} files",
    )
    add("harness_frozen", harness == frozen.get("harness"), "harness digests match the freeze")
    add(
        "freeze_commit_is_ancestor",
        is_ancestor(str((frozen.get("git") or {}).get("git_head", ""))),
        f"freeze written at {(frozen.get('git') or {}).get('git_head', '')[:12]}",
    )
    add("worktree_clean", git_identity()["git_status"] == "", "git status is empty")
    add(
        "runtime_unmodified",
        delta == [],
        f"src/ is identical to {BASELINE_COMMIT[:12]}; S2 adds no runtime code (observed {delta})",
    )
    add(
        "router_workerfactory_handoff_unmodified",
        protected_delta == [],
        f"delegation machinery untouched; observed changes {protected_delta}",
    )

    # -- prompt identity
    add(
        "prompt_v1_default_unchanged",
        contract.digest(MAIN_SYSTEM_PROMPT) == contract.PROMPT_V1_DIGEST
        and MAIN_PROMPT_V1.digest == contract.PROMPT_V1_DIGEST
        and prompt.get("default_digest") == contract.PROMPT_V1_DIGEST,
        f"v1 digest {contract.digest(MAIN_SYSTEM_PROMPT)}",
    )
    add(
        "prompt_v2_frozen",
        contract.prompt_v2_digest() == contract.PROMPT_V2_DIGEST
        and contract.prompt_v2_file_digest() == prompt.get("previous_file_digest"),
        f"v2.0 digest {contract.prompt_v2_digest()}",
    )
    add(
        "prompt_v21_digest_frozen",
        contract.prompt_v21_digest() == contract.PROMPT_V21_DIGEST == prompt.get("selected_digest")
        and contract.prompt_v21_file_digest() == prompt.get("file_digest"),
        f"v2.1 text digest {contract.prompt_v21_digest()}, file digest "
        f"{contract.prompt_v21_file_digest()}",
    )
    selected = options.main_prompt
    add(
        "prompt_v21_selected",
        selected is not None
        and selected.version == contract.PROMPT_V21_VERSION
        and selected.text == contract.prompt_v21_text()
        and selected.digest == contract.PROMPT_V21_DIGEST,
        f"selected prompt version {getattr(selected, 'version', None)!r}; "
        f"digest {getattr(selected, 'digest', None)}",
    )
    add(
        "prompt_v21_carries_no_controlled_instruction",
        contract.forbidden_goal_phrase() is None
        and contract.forbidden_prompt_phrase() is None
        and contract.GOAL not in contract.prompt_v21_text(),
        "neither the goal nor the prompt contains a controlled delegation instruction",
    )
    add(
        "prompt_v21_names_no_fixture_answer",
        not any(
            token in contract.prompt_v21_text().lower()
            for token in (
                "workgrid",
                "scheduler",
                "dependency graph",
                "availability window",
                "build_schedule",
            )
        ),
        "the prompt names no package, symbol or scenario of the fixture under test",
    )
    add(
        "goal_frozen",
        contract.goal_digest() == identity.get("goal_digest") and options.goal == contract.GOAL,
        f"goal digest {contract.goal_digest()[:20]}",
    )

    # -- the fixture is new, and it is solvable
    reused = reuse_evidence()
    add("fixture_not_reused", reused["ok"], reused["detail"])
    add(
        "fixture_matches_freeze",
        {
            "files": fixture_builder.fixture_manifest(),
            "fixture_digest": fixture_builder.fixture_digest(),
        }
        == frozen.get("fixture"),
        f"fixture digest {fixture_builder.fixture_digest()[:23]}",
    )
    add(
        "fixture_solvable",
        bool(offline_report.get("passed"))
        and bool(offline_report["suites"]["F1_fixture_solvability"]["passed"]),
        "the offline reference passes the public suite and the oracle; the provisioned fixture "
        "fails the target suite; five wrong variants are rejected by the oracle alone",
    )
    add(
        "prompt_injection_verified",
        bool(offline_report["suites"]["F2_prompt_injection"]["passed"]),
        "the frozen v2.1 bytes reach AgentConfig verbatim and never reach a child",
    )
    add(
        "worker_capability_matches_catalog",
        bool(
            offline_report["suites"]["F2_prompt_injection"]["checks"]["prompt_matches_the_catalog"]
        )
        and bool(
            offline_report["suites"]["F2_prompt_injection"]["checks"][
                "research_worker_has_no_execution_tool"
            ]
        ),
        "the prompt's description of research_worker equals WorkerCatalog.base_tools",
    )
    add(
        "router_admission_verified",
        bool(offline_report["suites"]["F3_router_admission"]["passed"]),
        "the frozen router admits two independent read-only investigations and refuses the rest",
    )
    add(
        "oracle_not_in_fixture",
        not offline_report["suites"]["F1_fixture_solvability"]["oracle_markers_leaked_into_fixture"]
        and not offline_report["suites"]["F1_fixture_solvability"][
            "reference_files_inside_fixture"
        ],
        "no oracle marker and no reference implementation inside the provisioned fixture",
    )

    # -- run configuration
    add(
        "tool_error_mode_frozen",
        identity.get("tool_error_mode") == contract.TOOL_ERROR_MODE == options.tool_error_mode,
        f"expected {contract.TOOL_ERROR_MODE}, runtime option {options.tool_error_mode}",
    )
    add(
        "budget_and_turn_limits_frozen",
        options.token_budget == contract.TOKEN_BUDGET
        and options.max_turns == contract.MAIN_MAX_TURNS
        and options.delegation.max_delegation_rounds == contract.DELEGATION["max_delegation_rounds"]
        and options.delegation.child_max_turns == contract.DELEGATION["child_max_turns"]
        and options.delegation.max_parallel_children
        == contract.DELEGATION["max_parallel_children"],
        f"budget {options.token_budget}, main turns {options.max_turns}, "
        f"delegation {options.delegation.model_dump()}",
    )
    add(
        "write_scope_is_the_scheduler_layer",
        options.main_write_paths == contract.MAIN_WRITE_PATHS == ("workgrid/scheduler/",),
        f"main_write_paths={list(options.main_write_paths)}",
    )
    add(
        "router_policy_version",
        STAGE_M_POLICY_VERSION == "m.1.0",
        f"frozen router policy {STAGE_M_POLICY_VERSION}",
    )
    add(
        "no_prior_attempt",
        prior_attempt(Path(options.runs_root).parent) is None,
        "no attempt marker and no collected evidence",
    )
    add(
        "scratch_outside_worktree",
        not str(Path(options.runs_root).resolve()).startswith(str(REPO_ROOT.resolve())),
        f"runs_root={options.runs_root}",
    )
    add(
        "single_live_attempt",
        True,
        "the marker is claimed with O_CREAT|O_EXCL after READY and before run_goal",
    )
    return checks


def evaluate(options: GoalRunOptions, offline_report: dict[str, Any]) -> dict[str, Any]:
    report = run_live_preflight(options)
    checks = stage_checks(options, offline_report)
    failures = list(report["blocking_failures"]) + [
        check["check_id"] for check in checks if check["blocking"] and not check["ok"]
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "delegation_mode": "AUTONOMOUS_MAIN_DELEGATION",
        "runtime_status": report["status"],
        "combined_status": READY if report["status"] == READY and not failures else "BLOCKED",
        "combined_blocking_failures": failures,
        "runtime": report,
        "stage_checks": checks,
        "offline": {
            "passed": offline_report.get("passed"),
            "suites": {
                name: suite.get("passed")
                for name, suite in (offline_report.get("suites") or {}).items()
            },
        },
        "freeze": _freeze(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_offline_phase(scratch: Path, *, record: bool = True) -> dict[str, Any]:
    """Run the three offline suites.

    ``record`` writes the report into the repository, and is used by ``--offline`` only. An
    admission run re-executes the suites but keeps its own report in the scratch directory, so the
    file the freeze identified stays byte-identical to the one that was frozen.
    """

    report = offline.run_offline(scratch / "offline")
    if record:
        _write_json(OFFLINE_PATH, report)
    else:
        _write_json(scratch / "offline.json", report)
    return report


def preflight(scratch: Path) -> dict[str, Any]:
    offline_report = run_offline_phase(scratch, record=False)
    fixture, commit = fixture_builder.materialize(scratch / "fixture", with_git=True)
    options = build_options(scratch / "runs", fixture, run_id="stage-s2-preflight")
    combined = evaluate(options, offline_report)
    combined["fixture_commit"] = commit
    _write_json(scratch / "preflight.json", combined)
    return combined


# -- the single live attempt --------------------------------------------------------------------


def summarize(
    session: dict[str, Any], trace: dict[str, Any], preflight_report: dict[str, Any]
) -> dict[str, Any]:
    return {
        "stage": "S2",
        "delegation_mode": "AUTONOMOUS_MAIN_DELEGATION",
        "status": session["status"],
        "acceptance": session["acceptance"],
        "main_prompt": session.get("main_prompt"),
        "tool_error_mode": session.get("tool_error_mode"),
        "execution_integrity": session.get("execution_integrity"),
        "main": trace["main"],
        "early_delegation": {
            key: trace["early_delegation"][key]
            for key in (
                "first_delegate_turn",
                "delegate_turns",
                "total_turns",
                "late",
                "tool_call_count_before_first_delegation",
                "read_target_count_before_first_delegation",
                "read_targets_before_first_delegation",
                "main_context_tokens_at_delegation_request",
                "main_context_tokens_by_request",
            )
        },
        "capability": trace["capability"],
        "implementation": trace["implementation"],
        "handoff_utilisation": {
            key: value
            for key, value in trace["handoff_utilisation"].items()
            if key != "tool_calls_after_first_delegation"
        },
        "delegation": trace["delegation"],
        "metrics": trace["metrics"],
        "verification": {
            "verdict": (session.get("verification") or {}).get("verdict"),
            "static_status": (session.get("verification") or {}).get("static_status"),
            "dynamic": (session.get("verification") or {}).get("dynamic"),
        },
        "public_tests": trace["public_tests"],
        "sandbox": session["sandbox"],
        "ownership": {
            key: session["ownership"][key]
            for key in ("mode", "checked_calls", "allowed_calls", "denied_calls")
        },
        "budget": session["budget"],
        "verdict": trace["verdict"],
        "goal_digest": contract.goal_digest(),
        "preflight_status": preflight_report["combined_status"],
        "git": preflight_report["freeze"]["git"],
        "source_tree_digest": preflight_report["freeze"]["source"]["tree_digest"],
    }


def evidence_dir() -> Path:
    index = 1
    while (EVIDENCE_ROOT / f"run-{index}").exists():
        index += 1
    return EVIDENCE_ROOT / f"run-{index}"


def _collect(run_dir: Path, scratch: Path) -> Path:
    """Copy every artefact out of the scratch directory before anything is summarised."""

    destination = evidence_dir()
    destination.mkdir(parents=True, exist_ok=False)
    for name in (
        "session.json",
        "run_header.json",
        "workspace.patch",
        "tool_conversations.json",
        "workers.json",
        "main_trace.json",
    ):
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    trajectory = next((run_dir / "trajectory").glob("*/transcript.jsonl"), None)
    if trajectory is not None:
        shutil.copy2(trajectory, destination / "transcript.jsonl")
    handoffs = run_dir / "handoffs"
    if handoffs.is_dir():
        shutil.copytree(handoffs, destination / "handoffs", dirs_exist_ok=True)
    for name, source in (
        ("preflight.json", scratch / "preflight.json"),
        # The frozen report, byte-identical to what the freeze identified.
        ("offline.json", OFFLINE_PATH),
        # What the admission run re-executed just before the model call.
        ("offline-preflight.json", scratch / "offline.json"),
    ):
        if source.is_file():
            shutil.copy2(source, destination / name)
    marker = attempt_marker(scratch)
    if marker.is_file():
        shutil.copy2(marker, destination / "attempt-1.json")
    private = run_dir / "private" / "tool_payloads.jsonl"
    if private.is_file():
        (destination / "PRIVATE_PAYLOAD_NOTICE.txt").write_text(
            f"private payload corpus left in {private}\nnot publishable\n", encoding="utf-8"
        )
    return destination


async def run(scratch: Path, offline_report: dict[str, Any]) -> dict[str, Any]:
    reason = prior_attempt(scratch)
    if reason is not None:
        raise AttemptAlreadyStarted(reason)

    fixture, commit = fixture_builder.materialize(scratch / "fixture", with_git=True)
    run_id = f"stage-s2-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    options = build_options(scratch / "runs", fixture, run_id=run_id)

    report = evaluate(options, offline_report)
    _write_json(scratch / "preflight.json", {**report, "fixture_commit": commit})
    if report["combined_status"] != READY:
        raise SystemExit(f"BLOCKED: {report['combined_blocking_failures']}")

    claim_attempt(
        scratch,
        {
            "stage": "S2",
            "delegation_mode": "AUTONOMOUS_MAIN_DELEGATION",
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "goal_digest": contract.goal_digest(),
            "prompt_version": contract.PROMPT_V21_VERSION,
            "prompt_digest": contract.PROMPT_V21_DIGEST,
            "fixture_commit": commit,
            "source_tree_digest": source_identity()["tree_digest"],
            "tool_error_mode": contract.TOOL_ERROR_MODE,
            "git": git_identity(),
        },
    )

    started = datetime.now(UTC)
    session = await run_goal(options)
    finished = datetime.now(UTC)
    run_dir = Path(session["run_dir"])

    # Behaviour first, artefacts second, prose last: the trace is computed from files that exist,
    # and a crash while summarising cannot lose the run.
    trace = analysis.analyze_run(run_dir)
    _write_json(run_dir / "main_trace.json", trace)
    destination = _collect(run_dir, scratch)
    summary = summarize(session, trace, report)
    summary["wall_clock_seconds"] = round((finished - started).total_seconds(), 3)
    _write_json(run_dir / "stage_s2_summary.json", summary)
    _write_json(destination / "stage_s2_summary.json", summary)
    return summary


# -- entry point --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage S2 Main prompt v2.1 final pilot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--offline", action="store_true")
    group.add_argument("--freeze", action="store_true")
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--run", action="store_true")
    parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    scratch = (args.scratch or DEFAULT_SCRATCH).resolve()

    if args.offline:
        report = run_offline_phase(scratch)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "suites": {name: suite["passed"] for name, suite in report["suites"].items()},
                    "fixture_digest": report["fixture_digest"],
                    "path": str(OFFLINE_PATH),
                },
                indent=2,
            )
        )
        return 0 if report["passed"] else 2

    if args.freeze:
        reason = prior_attempt(scratch)
        if reason is not None:
            raise SystemExit(f"refusing to freeze: {reason}")
        if not OFFLINE_PATH.is_file():
            raise SystemExit("refusing to freeze before --offline has been run and recorded")
        payload = write_freeze()
        print(
            json.dumps(
                {
                    "frozen_source": payload["source"]["tree_digest"],
                    "prompt_v21_digest": payload["identity"]["prompt"]["selected_digest"],
                    "fixture_digest": payload["fixture"]["fixture_digest"],
                    "goal_digest": payload["identity"]["goal_digest"],
                    "oracle_digest": payload["oracle"]["program_digest"],
                    "path": str(FREEZE_PATH),
                },
                indent=2,
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
                    "fixture_commit": report["fixture_commit"],
                },
                indent=2,
            )
        )
        return 0 if report["combined_status"] == READY else 2

    reason = prior_attempt(scratch)
    if reason is not None:
        raise SystemExit(f"refusing to run: {reason}")
    offline_report = run_offline_phase(scratch, record=False)
    try:
        summary = asyncio.run(run(scratch, offline_report))
    except AttemptAlreadyStarted as error:
        raise SystemExit(f"refusing to run: {error}") from error
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "succeeded" and summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
