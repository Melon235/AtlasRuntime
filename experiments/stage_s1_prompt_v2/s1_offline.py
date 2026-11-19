"""Stage S1 offline preparation: fixture solvability, prompt injection, router admission.

None of this calls a model. Each suite answers a question that must be settled *before* the single
live attempt, and each records what it actually observed rather than what it hoped to see.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import s1_contract as contract
import s1_fixture as fixture
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from atlasos.app.main_driven import (
    MAIN_PROMPT_V1,
    MAIN_SYSTEM_PROMPT,
    GoalRunOptions,
    MainPromptSpec,
    run_goal,
)
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation import DelegationConfig
from atlasos.delegation.models import DelegateTasksInput
from atlasos.delegation.router import DelegationPlanContext, DelegationRouter, ProfileFacts
from atlasos.workers import WorkerCatalog

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference"


def _tail(text: str, limit: int = 1200) -> str:
    return text[-limit:]


def _run(argv: list[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    return {
        "argv": argv[1:],
        "exit_code": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _pytest(root: Path, *targets: str) -> dict[str, Any]:
    return _run([sys.executable, "-B", "-m", "pytest", "-q", *targets], root)


def _oracle(root: Path) -> dict[str, Any]:
    outcome = _run([sys.executable, "-c", contract.HIDDEN_PROGRAM], root)
    outcome["hidden_ok"] = "HIDDEN_OK" in outcome["stdout_tail"]
    outcome["hidden_fail"] = "HIDDEN_FAIL" in (outcome["stdout_tail"] + outcome["stderr_tail"])
    return outcome


# -- F1: the fixture is solvable, and wrong implementations are rejected -------------------------

REFERENCE_FILES = {
    "releaseguard/audit/report.py": REFERENCE / "report_reference.py",
    "releaseguard/audit/service.py": REFERENCE / "service_reference.py",
}

#: Each entry is a plausible, wrong implementation: one small edit to the reference. The oracle must
#: reject every one of them, otherwise the oracle is not measuring the contract.
WRONG_VARIANTS: tuple[dict[str, str], ...] = (
    {
        "name": "checks_sorted_alphabetically",
        "file": "releaseguard/audit/report.py",
        "old": "sequence = tuple(ordered[check_id] for check_id in CHECK_ORDER if check_id in ordered)",
        "new": "sequence = tuple(ordered[check_id] for check_id in sorted(ordered))",
    },
    {
        "name": "warnings_fail_the_release",
        "file": "releaseguard/audit/report.py",
        "old": "failed = bool(errors) or (effective.fail_on_warnings and bool(warnings))",
        "new": "failed = bool(errors) or bool(warnings)",
    },
    {
        "name": "missing_artifact_never_reported",
        "file": "releaseguard/audit/service.py",
        "old": "if usable.get(entry.path) is not None and usable[entry.path] not in scanned",
        "new": "if False",
    },
    {
        "name": "extras_never_fatal",
        "file": "releaseguard/audit/service.py",
        "old": (
            "extras_severity = Severity.WARNING if policy.allow_extra_artifacts else Severity.ERROR"
        ),
        "new": "extras_severity = Severity.WARNING",
    },
    {
        "name": "broken_manifest_reports_passes",
        "file": "releaseguard/audit/service.py",
        "old": (
            '        reason = "the manifest could not be parsed, so there is nothing to compare"\n'
            "        for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):\n"
            "            checks.append(_skip(check_id, reason))"
        ),
        "new": (
            '        reason = "the manifest could not be parsed, so there is nothing to compare"\n'
            "        for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):\n"
            "            checks.append(_check(check_id, [], reason))"
        ),
    },
)


def apply_reference(root: Path) -> None:
    for relative, source in REFERENCE_FILES.items():
        target = root / relative
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def apply_variant(root: Path, variant: dict[str, str]) -> None:
    target = root / variant["file"]
    text = target.read_text(encoding="utf-8")
    if variant["old"] not in text:
        raise RuntimeError(
            f"variant {variant['name']!r} no longer matches {variant['file']}: the reference moved"
        )
    target.write_text(text.replace(variant["old"], variant["new"], 1), encoding="utf-8")


def verify_fixture_solvability(scratch: Path) -> dict[str, Any]:
    """Prove the frozen fixture is solvable, and that the oracle is not trivially satisfiable."""

    cases: dict[str, Any] = {}

    baseline = fixture.copy_for_experiment(scratch / "case-baseline")
    compatibility = _pytest(baseline, "tests/test_metadata.py", "tests/test_artifacts.py")
    target = _pytest(baseline, "tests/test_public_audit.py")
    whole = _pytest(baseline, "tests")
    baseline_oracle = _oracle(baseline)
    cases["baseline"] = {
        "description": "the frozen fixture as provisioned",
        "compatibility_suites": compatibility,
        "target_suite": target,
        "whole_suite": whole,
        "oracle": baseline_oracle,
        "compatibility_passes": compatibility["exit_code"] == 0,
        "target_fails": target["exit_code"] != 0,
        "oracle_rejects": baseline_oracle["exit_code"] != 0,
    }

    reference = fixture.copy_for_experiment(scratch / "case-reference")
    apply_reference(reference)
    reference_suite = _pytest(reference, "tests")
    reference_oracle = _oracle(reference)
    cases["reference"] = {
        "description": "the offline reference implementation of the two incomplete modules",
        "suite": reference_suite,
        "oracle": reference_oracle,
        "suite_passes": reference_suite["exit_code"] == 0,
        "oracle_passes": reference_oracle["exit_code"] == 0 and reference_oracle["hidden_ok"],
    }

    variants: dict[str, Any] = {}
    for variant in WRONG_VARIANTS:
        case = fixture.copy_for_experiment(scratch / f"case-{variant['name']}")
        apply_reference(case)
        apply_variant(case, variant)
        outcome = _oracle(case)
        variants[variant["name"]] = {
            "file": variant["file"],
            "patch": {"old": variant["old"], "new": variant["new"]},
            "oracle": outcome,
            # Rejected means the oracle did not certify it. A wrong implementation may trip an
            # assertion or raise while asserting; either way it must not print HIDDEN_OK.
            "rejected": outcome["exit_code"] != 0 and not outcome["hidden_ok"],
        }

    index = fixture.materialize(scratch / "fixture-repo", with_git=True)
    oracle_leaks = [
        name
        for name, _ in fixture.fixture_manifest().items()
        if "HIDDEN_OK" in (contract.FIXTURE_ROOT / name).read_text(encoding="utf-8", errors="replace")
        or "HIDDEN_FAIL"
        in (contract.FIXTURE_ROOT / name).read_text(encoding="utf-8", errors="replace")
        or "releaseguard/audit/service_reference" in name
    ]
    reference_in_fixture = [
        name
        for name in fixture.fixture_manifest()
        if "reference" in name or name.endswith("service_reference.py")
    ]

    passed = (
        cases["baseline"]["compatibility_passes"]
        and cases["baseline"]["target_fails"]
        and cases["baseline"]["oracle_rejects"]
        and cases["reference"]["suite_passes"]
        and cases["reference"]["oracle_passes"]
        and all(item["rejected"] for item in variants.values())
        and not oracle_leaks
        and not reference_in_fixture
    )
    return {
        "suite": "F1_fixture_solvability",
        "passed": passed,
        "cases": cases,
        "wrong_variants": variants,
        "oracle_markers_leaked_into_fixture": oracle_leaks,
        "reference_files_inside_fixture": reference_in_fixture,
        "fixture_commit": index[1],
        "fixture_files": len(fixture.fixture_manifest()),
    }


# -- F2: what the Main's AgentConfig actually receives ------------------------------------------

MAIN_GOAL = "Add a subtract function and report what arithmetic exists."
CHILD_MARKER = "MARK-S1ALPHA"
DELEGATED_TASK = {
    "key": "alpha",
    "objective": f"{CHILD_MARKER} find where arithmetic is implemented",
    "background": "The workspace is a small Python project at the repository root.",
    "profile_id": "research_worker",
}


class InjectionRig:
    """Scripted provider that records the exact system prompt of every request it serves."""

    name = "stage-s1-injection-rig"

    def __init__(self, main_prompt_text: str) -> None:
        self.main_prompt_text = main_prompt_text
        self.requests: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}

    def build(self) -> InjectionRig:
        return self

    def _role(self, system_prompt: str | None) -> str:
        text = system_prompt or ""
        if text == self.main_prompt_text:
            return "main"
        if CHILD_MARKER in text:
            return "child"
        return "unknown"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        role = self._role(options.system_prompt)
        self.requests.append(
            {
                "role": role,
                "system_prompt": options.system_prompt,
                "messages": [message.model_dump(mode="json") for message in messages],
            }
        )
        turn = self.calls.get(role, 0)
        self.calls[role] = turn + 1
        if role == "main" and turn == 0:
            blocks: list[Any] = [
                ToolUseBlock(
                    id="call-s1", name="delegate_tasks", input={"tasks": [DELEGATED_TASK]}
                )
            ]
        elif role == "main":
            blocks = [TextBlock(text="MAIN-FINAL: integrated")]
        elif role == "child":
            blocks = [TextBlock(text="ALPHA-RESULT: arithmetic lives in app.py")]
        else:
            blocks = [TextBlock(text="done")]
        has_tool_use = any(isinstance(block, ToolUseBlock) for block in blocks)
        return LLMResponse(
            id=f"{role}-{turn}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if has_tool_use else "end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=2),
        )

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()

    def system_prompts(self, role: str) -> list[str]:
        return [item["system_prompt"] or "" for item in self.requests if item["role"] == role]


async def _injection_run(
    root: Path, rig: InjectionRig, **overrides: Any
) -> dict[str, Any]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    options = GoalRunOptions(
        goal=MAIN_GOAL,
        runs_root=root / "runs",
        workspace_root=workspace,
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        provider_factory=rig.build,
        delegation=DelegationConfig(),
        run_id=f"stage-s1-offline-{uuid.uuid4().hex[:8]}",
        **overrides,
    )
    return await run_goal(options)


async def verify_prompt_injection(scratch: Path) -> dict[str, Any]:
    """F2: the default is byte-identical, the override is exactly the frozen file, children unaffected."""

    frozen_text = contract.prompt_v2_text()
    checks: dict[str, Any] = {}

    default_rig = InjectionRig(MAIN_SYSTEM_PROMPT)
    default_session = await _injection_run(scratch / "inject-default", default_rig)
    override_rig = InjectionRig(frozen_text)
    override_session = await _injection_run(
        scratch / "inject-override",
        override_rig,
        main_prompt=MainPromptSpec(
            version=contract.PROMPT_V2_VERSION, text=frozen_text
        ),
    )

    default_prompts = default_rig.system_prompts("main")
    override_prompts = override_rig.system_prompts("main")
    checks["default_prompt_bytes_are_v1"] = bool(default_prompts) and all(
        prompt == MAIN_SYSTEM_PROMPT for prompt in default_prompts
    )
    checks["default_digest_is_frozen_v1"] = (
        default_session["main_prompt"]["digest"] == contract.PROMPT_V1_DIGEST
        and MAIN_PROMPT_V1.digest == contract.PROMPT_V1_DIGEST
    )
    checks["default_source_is_default"] = default_session["main_prompt"]["source"] == "default"
    checks["default_version_label"] = (
        default_session["main_prompt"]["version"] == contract.PROMPT_V1_VERSION
    )
    checks["override_bytes_are_the_frozen_file"] = bool(override_prompts) and all(
        prompt == frozen_text for prompt in override_prompts
    )
    checks["override_digest_is_frozen_v2"] = (
        override_session["main_prompt"]["digest"] == contract.PROMPT_V2_DIGEST
    )
    checks["override_digest_matches_the_file"] = (
        contract.prompt_v2_digest() == contract.PROMPT_V2_DIGEST
    )
    checks["override_source_is_override"] = override_session["main_prompt"]["source"] == "override"
    checks["override_version_label"] = (
        override_session["main_prompt"]["version"] == contract.PROMPT_V2_VERSION
    )
    checks["override_does_not_contain_the_goal"] = (
        contract.GOAL not in frozen_text and MAIN_GOAL not in frozen_text
    )
    checks["delegate_tasks_on_the_main_surface"] = (
        "delegate_tasks" in override_session["tool_surface"]
        and "delegate.read" in override_session["tool_surface"]
    )
    checks["router_policy_is_m_1_0"] = (
        override_session["delegation"]["batches"][0]["decision"]["policy_version"] == "m.1.0"
    )
    checks["delegation_was_admitted"] = (
        override_session["delegation"]["batches"][0]["route"] == "delegate_one"
    )
    checks["child_ran"] = (
        override_session["delegation"]["batches"][0]["children"][0]["success"] is True
    )
    checks["child_prompt_identical_under_both_prompts"] = (
        default_rig.system_prompts("child") == override_rig.system_prompts("child") != []
    )
    child_prompt = (override_rig.system_prompts("child") or [""])[0]
    checks["child_prompt_is_the_worker_render"] = child_prompt.startswith("TRUSTED WORKER PROFILE")
    checks["child_prompt_has_no_main_prompt"] = (
        frozen_text not in child_prompt and MAIN_SYSTEM_PROMPT not in child_prompt
    )
    checks["handoff_reached_the_main"] = "ALPHA-RESULT" in json.dumps(
        [item for item in override_rig.requests if item["role"] == "main"][-1]["messages"],
        sort_keys=True,
    )
    checks["no_forbidden_goal_phrase"] = contract.forbidden_goal_phrase() is None

    return {
        "suite": "F2_prompt_injection",
        "passed": all(checks.values()),
        "checks": checks,
        "default_session_main_prompt": default_session["main_prompt"],
        "override_session_main_prompt": override_session["main_prompt"],
        "child_prompt_digest": "sha256:"
        + hashlib.sha256(child_prompt.encode("utf-8")).hexdigest(),
        "note": (
            "a scripted provider proves the interface and the bytes; it cannot prove that a real "
            "model decides to delegate"
        ),
    }


# -- F3: the frozen router admits the fixture's two independent investigations -------------------


def _plan_context(*, max_children: int, grants: dict[str, tuple[str, ...]]) -> DelegationPlanContext:
    catalog = WorkerCatalog()
    profiles = {
        profile_id: ProfileFacts(
            profile_id=profile_id,
            base_tools=tuple(profile.base_tools),
            read_only=not ({"file_write", "file_edit", "bash"} & set(profile.base_tools)),
            can_delegate=bool(profile.can_delegate),
        )
        for profile_id, profile in catalog.profiles.items()
    }
    return DelegationPlanContext(
        run_id="stage-s1-offline",
        parent_task_id="stage-s1-offline-main",
        max_children=max_children,
        profiles=profiles,
        granted_write_paths=grants,
        workspace_root="/workspace/project",
    )


def _request(tasks: list[dict[str, Any]]) -> DelegateTasksInput:
    return DelegateTasksInput.model_validate({"tasks": tasks})


def verify_router_admission() -> dict[str, Any]:
    """F3: offline admission of the shapes this fixture actually has.

    This is a statement about the frozen router, never about the Main's decision.
    """

    router = DelegationRouter()
    context = _plan_context(max_children=2, grants={"coding_worker": ("releaseguard/audit/",)})
    background = (
        "The repository root contains the releaseguard package. Metadata rules live in "
        "releaseguard/metadata, artifact rules in releaseguard/artifacts, and the shared audit in "
        "releaseguard/audit. Report concrete function signatures and error codes."
    )
    cases: dict[str, Any] = {}

    parallel = router.decide(
        _request(
            [
                {
                    "key": "metadata-rules",
                    "objective": "Audit the metadata and version rules and report their contracts.",
                    "background": background,
                    "profile_id": "research_worker",
                },
                {
                    "key": "artifact-rules",
                    "objective": "Audit the artifact scanner and path rules and report their contracts.",
                    "background": background,
                    "profile_id": "research_worker",
                },
            ]
        ),
        context=context,
    )
    cases["two_independent_read_only_tasks"] = {
        "route": str(parallel.route),
        "admitted": parallel.admitted,
        "admitted_keys": list(parallel.admitted_specs()),
        "max_wave_width": parallel.max_wave_width,
        "policy_version": parallel.policy_version,
        "expected": "admitted as one parallel wave",
    }

    shared_write = router.decide(
        _request(
            [
                {
                    "key": "left",
                    "objective": "Write the left half of the audit service.",
                    "background": background,
                    "profile_id": "coding_worker",
                    "write_paths": ["releaseguard/audit/service.py"],
                },
                {
                    "key": "right",
                    "objective": "Write the right half of the audit service.",
                    "background": background,
                    "profile_id": "coding_worker",
                    "write_paths": ["releaseguard/audit/service.py"],
                },
            ]
        ),
        context=context,
    )
    cases["shared_write"] = {
        "route": str(shared_write.route),
        "admitted": shared_write.admitted,
        "reason_codes": list(shared_write.reason_codes),
        "expected": "refused",
    }

    no_background = router.decide(
        _request(
            [
                {
                    "key": "uncontextualised",
                    "objective": "Look into the audit layer.",
                    "profile_id": "research_worker",
                }
            ]
        ),
        context=context,
    )
    cases["missing_background"] = {
        "route": str(no_background.route),
        "admitted": no_background.admitted,
        "reason_codes": list(no_background.reason_codes),
        "expected": "refused",
    }

    fake_parallel = router.decide(
        _request(
            [
                {
                    "key": "first",
                    "objective": "Read the metadata package.",
                    "background": background,
                    "profile_id": "research_worker",
                },
                {
                    "key": "second",
                    "objective": "Read the artifact package.",
                    "background": background,
                    "profile_id": "research_worker",
                    "depends_on": ["first"],
                },
            ]
        ),
        context=context,
    )
    cases["declared_dependency_in_one_wave"] = {
        "route": str(fake_parallel.route),
        "admitted": fake_parallel.admitted,
        "reason_codes": list(fake_parallel.reason_codes),
        "expected": "refused",
    }

    checks = {
        "parallel_admitted": cases["two_independent_read_only_tasks"]["admitted"]
        and cases["two_independent_read_only_tasks"]["max_wave_width"] == 2,
        "shared_write_refused": not cases["shared_write"]["admitted"]
        and "SHARED_WRITE_RISK" in cases["shared_write"]["reason_codes"],
        "missing_background_refused": not cases["missing_background"]["admitted"],
        "dependency_refused": not cases["declared_dependency_in_one_wave"]["admitted"]
        and "DEPENDENCY_NOT_SUPPORTED" in cases["declared_dependency_in_one_wave"]["reason_codes"],
        "policy_version_is_m_1_0": cases["two_independent_read_only_tasks"]["policy_version"]
        == "m.1.0",
    }
    return {
        "suite": "F3_router_admission",
        "passed": all(checks.values()),
        "checks": checks,
        "cases": cases,
        "note": (
            "offline admission of the frozen router; it is not evidence that a real Main proposed "
            "these tasks"
        ),
    }


# -- the whole offline programme ----------------------------------------------------------------


def run_offline(scratch: Path) -> dict[str, Any]:
    import asyncio

    scratch.mkdir(parents=True, exist_ok=True)
    solvability = verify_fixture_solvability(scratch / "solvability")
    injection = asyncio.run(verify_prompt_injection(scratch / "injection"))
    router = verify_router_admission()
    identity = contract.frozen_identity()
    return {
        "stage": "S1",
        "generated_at": datetime.now(UTC).isoformat(),
        "prompt": identity["prompt"],
        "goal_digest": identity["goal_digest"],
        "fixture_digest": fixture.fixture_digest(),
        "suites": {
            "F1_fixture_solvability": solvability,
            "F2_prompt_injection": injection,
            "F3_router_admission": router,
        },
        "passed": solvability["passed"] and injection["passed"] and router["passed"],
    }


__all__ = [
    "InjectionRig",
    "apply_reference",
    "apply_variant",
    "run_offline",
    "verify_fixture_solvability",
    "verify_prompt_injection",
    "verify_router_admission",
    "WRONG_VARIANTS",
]
