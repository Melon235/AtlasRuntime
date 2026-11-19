"""Stage P1b offline check: would the router admit two independent read-only investigations?

This exercises the real admission path (``DelegationService.plan_context`` -> ``DelegationRouter``
over the frozen ``r0.1.0`` gates) with the exact limits the live run uses. It creates no sandbox and
calls no model, and it proves nothing about what a live Main will choose to do: it only establishes
that the runtime *would* admit this shape of work, and which refusals the other shapes get.

    python experiments/stage_p1b_live/check_router_admission.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

from anycode.identity.context import ExecutionContext  # noqa: E402
from anycode.types import ToolSecurityPolicy  # noqa: E402

from atlasos.delegation import (  # noqa: E402
    DelegatedTaskSpec,
    DelegateTasksInput,
    DelegationLimits,
    DelegationService,
)
from atlasos.delegation.router import STAGE_M_POLICY_VERSION  # noqa: E402
from atlasos.workers import WorkerCatalog  # noqa: E402

WORKSPACE_ROOT = "/workspace/project"

#: Two independent investigations of the fixture's two channel packages. Each is read-only, needs no
#: sibling result, and carries the background a child needs because it never sees the Main history.
FILE_INVESTIGATION = DelegatedTaskSpec(
    key="file-channel",
    objective=(
        "Report the exact semantics of the file channel in plugins/file_events: the native record "
        "fields, the level vocabulary and its canonical severity mapping, how an absent level and "
        "absent tags/attrs are handled, how timestamps are parsed, and the error codes the parser "
        "raises. Quote the functions a consumer must call."
    ),
    background=(
        "Repository root is the workspace. The package is vendored and read-only for you. Read "
        "plugins/file_events/{schema,parser,converters,errors}.py; do not modify anything."
    ),
    profile_id="research_worker",
)
MESSAGE_INVESTIGATION = DelegatedTaskSpec(
    key="message-channel",
    objective=(
        "Report the exact semantics of the message channel in plugins/message_events: the native "
        "record fields, the priority vocabulary and its canonical severity mapping, how absent "
        "priority/labels/payload are handled, how epoch milliseconds are converted, how payload "
        "values are rendered, and the error codes the parser raises. Quote the functions a "
        "consumer must call."
    ),
    background=(
        "Repository root is the workspace. The package is vendored and read-only for you. Read "
        "plugins/message_events/{schema,parser,converters,errors}.py; do not modify anything."
    ),
    profile_id="research_worker",
)


def service(run_dir: Path) -> DelegationService:
    context = ExecutionContext(
        principal="user:goal",
        tenant_scope="goal",
        classification="internal",
        attributes={
            "run_id": "p1b-offline-router",
            "task_id": "p1b-offline-router-main",
            "worker_id": "p1b-offline-router-main-worker",
            "worker_profile": "coding_worker",
            "operating_scope": "coding",
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": "p1b-offline-router",
        },
    )
    return DelegationService(
        run_id="p1b-offline-router",
        run_dir=run_dir,
        model="deepseek-chat",
        provider="deepseek",
        parent_context=context,
        parent_task_id="p1b-offline-router-main",
        parent_agent="atlas-main",
        tool_runtime=None,
        limits=DelegationLimits(
            max_parallel_children=2,
            max_children_per_call=2,
            max_delegation_rounds=4,
            child_timeout_seconds=300.0,
            child_max_turns=6,
            result_preview_chars=900,
        ),
        tool_security=ToolSecurityPolicy(workspace_root=WORKSPACE_ROOT),
    )


def decide(svc: DelegationService, tasks: tuple[DelegatedTaskSpec, ...]) -> dict[str, Any]:
    decision = svc.router.decide(
        DelegateTasksInput(tasks=tasks, reason="stage p1b offline admission check"),
        context=svc.plan_context(),
    )
    return {
        "route": decision.route.value,
        "reason_codes": list(decision.reason_codes),
        "blocked_gates": list(decision.blocked_gates),
        "policy_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
        "max_wave_width": decision.max_wave_width,
        "admitted": list(decision.admitted_specs()),
        "children": [
            {
                "key": child.key,
                "profile_id": child.profile_id,
                "admitted": child.admitted,
                "reason_codes": list(child.reason_codes),
            }
            for child in decision.children
        ],
        "frozen_decision": decision.frozen_decision,
    }


def main() -> int:
    catalog = WorkerCatalog()
    research = catalog.resolve("research_worker")
    write_tools = {"file_write", "file_edit", "bash"}

    with tempfile.TemporaryDirectory(prefix="atlas-p1b-router-") as tmp:
        svc = service(Path(tmp))

        two_investigations = decide(svc, (FILE_INVESTIGATION, MESSAGE_INVESTIGATION))
        single_investigation = decide(svc, (FILE_INVESTIGATION,))
        no_background = decide(
            svc,
            (
                FILE_INVESTIGATION.model_copy(update={"background": None}),
                MESSAGE_INVESTIGATION.model_copy(update={"background": None}),
            ),
        )
        write_requested = decide(
            svc,
            (
                FILE_INVESTIGATION.model_copy(update={"write_paths": ("core/adapter.py",)}),
                MESSAGE_INVESTIGATION,
            ),
        )
        chained = decide(
            svc,
            (
                FILE_INVESTIGATION,
                MESSAGE_INVESTIGATION.model_copy(update={"depends_on": ("file-channel",)}),
            ),
        )

    evidence = {
        "policy_version": STAGE_M_POLICY_VERSION,
        "workspace_root": WORKSPACE_ROOT,
        "child_profile": {
            "profile_id": research.profile_id,
            "base_tools": list(research.base_tools),
            "read_only": not (write_tools & set(research.base_tools)),
            "can_delegate": research.can_delegate,
            "max_turns": research.max_turns,
            "no_sibling_result_dependency": [
                len(FILE_INVESTIGATION.depends_on),
                len(MESSAGE_INVESTIGATION.depends_on),
            ],
        },
        "two_parallel_investigations": two_investigations,
        "single_investigation": single_investigation,
        "missing_background": no_background,
        "write_scope_requested": write_requested,
        "declared_dependency": chained,
    }
    evidence["expectations"] = {
        "two_investigations_admitted_parallel": two_investigations["route"] == "delegate_parallel"
        and sorted(two_investigations["admitted"]) == ["file-channel", "message-channel"],
        "single_investigation_admitted": single_investigation["route"] == "delegate_one",
        "missing_background_refused": no_background["route"] == "direct"
        and "CONTEXT_INSUFFICIENT" in no_background["reason_codes"],
        "write_scope_refused": write_requested["route"] == "direct"
        and "WRITE_SCOPE_NOT_GRANTED" in write_requested["reason_codes"],
        "dependency_refused": chained["route"] == "direct"
        and "DEPENDENCY_NOT_SUPPORTED" in chained["reason_codes"],
        "child_is_read_only": bool(evidence["child_profile"]["read_only"]),
        "child_cannot_delegate": not evidence["child_profile"]["can_delegate"],
        "investigations_independent": not any(
            evidence["child_profile"]["no_sibling_result_dependency"]
        ),
    }
    evidence["ready"] = all(evidence["expectations"].values())
    (HERE / "router_admission_report.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence["expectations"], indent=2, sort_keys=True))
    print(
        json.dumps(
            {
                "route": two_investigations["route"],
                "policy_version": two_investigations["policy_version"],
                "policy_digest": two_investigations["policy_digest"],
                "max_wave_width": two_investigations["max_wave_width"],
                "admitted": two_investigations["admitted"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(f"READY={evidence['ready']}")
    return 0 if evidence["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
