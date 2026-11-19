"""Stage P1b sandbox rehearsal: no model call, real container.

It proves the parts of the run that do not involve the model, using the same sandbox configuration,
the same frozen fixture, the same public command and the same oracle command as the live runner:

1. the fixture provisions cleanly into a real OpenSandbox container;
2. the network and host-separation probes pass;
3. the public suite **fails** on the provisioned fixture (the gap is real in the container, not only
   on the host);
4. the oracle command fails on the provisioned fixture;
5. after the reference implementation is written inside the container, the public suite passes and
   the oracle command prints ``HIDDEN_OK`` — through ``run_public_command`` (visible text) and
   through ``run_check`` (the exact call the harness oracle makes, digests only);
6. the exported workspace contains no oracle text;
7. the sandbox is destroyed.

    python experiments/stage_p1b_live/rehearse_sandbox.py

It creates no attempt marker and consumes no provider tokens.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

import p1b_contract as contract  # noqa: E402
from run_stage_p1b_live import build_fixture, build_options  # noqa: E402

from atlasos.actions.binding import canonical_digest  # noqa: E402
from atlasos.app.goal_sandbox import GoalSandbox  # noqa: E402
from atlasos.config import AtlasConfig, ToolsConfig  # noqa: E402

REFERENCE = HERE / "reference" / "core"
CHUNK = 1800
RUN_ID = "stage-p1b-rehearsal"


def upload_commands(path: str, text: str) -> list[str]:
    """Write ``text`` into the sandbox in bounded commands: no heredoc, no shell quoting."""

    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    commands: list[str] = []
    for index in range(0, len(payload), CHUNK):
        chunk = payload[index : index + CHUNK]
        if index == 0:
            body = (
                "import base64,pathlib;"
                f"pathlib.Path('{path}').write_bytes(base64.b64decode('{chunk}'))"
            )
        else:
            body = (
                "import base64,pathlib;"
                f"p=pathlib.Path('{path}');"
                f"p.write_bytes(p.read_bytes()+base64.b64decode('{chunk}'))"
            )
        commands.append(f'python3 -c "{body}"')
    return commands


def context() -> ExecutionContext:
    return ExecutionContext(
        principal="user:goal",
        tenant_scope="goal",
        classification="internal",
        attributes={
            "run_id": RUN_ID,
            "task_id": f"{RUN_ID}-main",
            "worker_id": f"{RUN_ID}-main-worker",
            "worker_profile": "coding_worker",
            "operating_scope": "coding",
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": RUN_ID,
        },
    )


def trim(outcome: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(outcome)
    trimmed["output"] = (outcome.get("output") or "")[-1500:]
    return trimmed


async def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    scratch = Path(tempfile.mkdtemp(prefix="atlas-p1b-rehearsal-"))
    fixture, commit = build_fixture(scratch / "fixture")
    options = build_options(scratch / "runs", fixture, run_id=RUN_ID)
    run_dir = scratch / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scratch": str(scratch),
        "fixture_commit": commit,
        "public_command": contract.PUBLIC_TESTS.command,
        "oracle_command_digest": canonical_digest(contract.ORACLE_COMMAND),
        "oracle_command_chars": len(contract.ORACLE_COMMAND),
        "steps": {},
    }
    steps = evidence["steps"]

    sandbox = GoalSandbox(
        options.sandbox,
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        run_id=RUN_ID,
        run_dir=run_dir,
    )
    await sandbox.start(task_id=f"{RUN_ID}-main", context=context(), worker_id=f"{RUN_ID}-mw")
    steps["provisioned"] = sandbox.evidence.as_dict()
    try:
        steps["public_before"] = trim(
            await sandbox.run_public_command(contract.PUBLIC_TESTS.command)
        )
        steps["oracle_before"] = trim(
            await sandbox.run_public_command(contract.ORACLE_COMMAND, timeout_seconds=420)
        )

        for name, relative in (
            ("adapter.py", "core/adapter.py"),
            ("registry.py", "core/registry.py"),
        ):
            text = (REFERENCE / name).read_text(encoding="utf-8")
            for command in upload_commands(relative, text):
                outcome = await sandbox.run_public_command(command, timeout_seconds=120)
                if outcome["exit_code"] != 0:
                    steps["upload_failure"] = trim(outcome)
                    raise SystemExit("reference upload failed")

        steps["public_after"] = trim(
            await sandbox.run_public_command(contract.PUBLIC_TESTS.command)
        )
        steps["oracle_after"] = trim(
            await sandbox.run_public_command(contract.ORACLE_COMMAND, timeout_seconds=420)
        )
        steps["oracle_via_run_check"] = await sandbox.run_check(
            contract.ORACLE_COMMAND, timeout_seconds=420
        )

        exported = await sandbox.export()
        workspace = Path(exported["workspace_path"])
        steps["export"] = exported
        steps["oracle_text_in_export"] = [
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")  # noqa: ASYNC240 - one local scan after the run
            if path.is_file() and "HIDDEN_OK" in path.read_text(encoding="utf-8", errors="replace")
        ]
    finally:
        steps["destroyed"] = await sandbox.close()
        # Provisioning counters are only final after teardown; the pre-close snapshot reads zero.
        steps["provisioned_final"] = sandbox.evidence.as_dict()

    expectations = {
        "provisioned_real": bool(steps["provisioned"]["provider_is_real"]),
        "network_probe_pass": steps["provisioned"]["network_probe"] == "PASS",
        "host_separation_clean": steps["provisioned"]["host_separation"].get(
            "sandbox_only_visible_on_host"
        )
        is False,
        "public_fails_before": steps["public_before"]["status"] == "FAIL",
        "oracle_fails_before": steps["oracle_before"]["exit_code"] not in (0, None),
        "public_passes_after": steps["public_after"]["status"] == "PASS",
        "oracle_passes_after": steps["oracle_after"]["exit_code"] == 0
        and "HIDDEN_OK" in steps["oracle_after"]["output"],
        "run_check_passes_after": bool(steps["oracle_via_run_check"]["ok"])
        and steps["oracle_via_run_check"]["exit_code"] == 0,
        "oracle_not_in_export": not steps["oracle_text_in_export"],
        "sandbox_destroyed": bool(steps["destroyed"]),
    }
    evidence["expectations"] = expectations
    evidence["ready"] = all(expectations.values())
    (HERE / "sandbox_rehearsal.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(expectations, indent=2, sort_keys=True))
    print(f"READY={evidence['ready']}  scratch={scratch}")
    return 0 if evidence["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
