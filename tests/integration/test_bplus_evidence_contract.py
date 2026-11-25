"""Stage B+ producer/consumer contract: export identity and public-test evidence.

v2 stopped a cell whose oracle had passed, because the audit compared the producer's *archive*
digest with a *directory* hash, and read a top-level ``public_tests.status`` the producer never
writes. Both defects survived the offline proofs because those proofs rewrote the session into the
shape the audit happened to want.

Every test here therefore feeds the object the producer actually returns into ``audit_cell``. The
first one uses the real ``live_cell_runner``; the rest use the synthetic builder, which now emits
the production shape (archive digest, no top-level public status).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_b_plus import authority, contract, executor, offline, probe
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider


def _plan() -> dict[str, Any]:
    return contract.scope_plan(contract.CANARY_PLAN_ID)


def _audit(cell: dict[str, Any], run: executor.CellRun, **kwargs: Any) -> dict[str, Any]:
    return executor.audit_cell(
        cell=cell,
        run=run,
        plan=kwargs.pop("plan", _plan()),
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
        **kwargs,
    )


# -- the real producer -------------------------------------------------------------------------


def test_the_live_runner_output_passes_the_export_and_public_checks(tmp_path: Path) -> None:
    """One cell through the production runner, then straight into the real audit."""

    # C1, not C2: the provider registry is process-global and refuses a duplicate run id, and the
    # execution-contract tests already drive C2 in this process.
    cell = contract.canary_cells()[1]
    scripted = probe.ScriptedProvider(
        main_script=[lambda _messages: [probe.TextBlock(text="REPORT: nothing to do.")]],
        child_scripts={},
    )
    config = executor.RunnerConfig(
        llm_provider="fake",
        llm_factory=lambda: scripted,
        sandbox_provider_name="fake",
        sandbox_factory=lambda root: FakeSandboxProvider(root),
        require_real_sandbox=False,
        main_max_turns=2,
    )
    runner = executor.live_cell_runner(config)
    context = {
        "plan": _plan(),
        "runs_root": tmp_path / "runs",
        "evidence_root": tmp_path / "evidence",
    }

    async def _await() -> executor.CellRun:
        return await runner(cell, context)

    run = asyncio.run(_await())
    export = run.export or {}
    outcome = _audit(cell, run)

    # The producer records an archive digest; the runner adds a directory digest. They are different
    # values and neither is compared with the other.
    assert export["workspace_digest"] == (run.session or {})["artifacts"]["workspace_digest"]
    assert export["directory_digest"].startswith("sha256:")
    assert export["directory_digest"] != export["workspace_digest"]
    assert export["archive_digest_verifiable"] is False

    # The fake sandbox is not isolated, so the audit refuses it — and only for that reason.
    assert set(outcome["stops"]) == {executor.STOP_HOST_ISOLATION}
    assert outcome["public_test_status"] == "NOT_RUN"
    assert outcome["goal_correctness"] is not True


# -- export identity ---------------------------------------------------------------------------


def test_a_healthy_export_records_two_separate_identities(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    export = run.export or {}

    assert export["workspace_digest"] != export["directory_digest"]
    assert export["archive_digest_verifiable"] is False
    assert _audit(cell, run)["stops"] == []


def test_a_changed_export_directory_is_refused(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    target = Path((run.export or {})["workspace_path"]) / "pipeline" / "api.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"


def test_a_file_count_the_directory_does_not_hold_is_refused(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.session is not None
    run.session["artifacts"]["workspace_files"] = 99

    assert executor.STOP_EXPORT in _audit(cell, run)["stops"]


def test_a_missing_directory_identity_is_refused(tmp_path: Path) -> None:
    """The historical v2 shape: only the archive digest exists, so nothing can be re-checked."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    export = dict(run.export or {})
    export.pop("directory_digest", None)
    export.pop("directory_files", None)
    run.export = export

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]
    assert any(
        "no directory digest" in item["detail"] for item in outcome["checks"] if not item["ok"]
    )


def test_the_archive_digest_is_never_compared_with_the_directory(tmp_path: Path) -> None:
    """The archive digest may differ from the directory digest — that is the whole point."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.session is not None
    assert run.export is not None
    archive = "sha256:" + "f" * 64
    # Same value on both sides, still different from the directory digest.
    run.session["artifacts"]["workspace_digest"] = archive
    run.export["workspace_digest"] = archive

    outcome = _audit(cell, run)

    assert outcome["stops"] == []
    assert archive != run.export["directory_digest"]


def test_a_diverged_archive_digest_is_refused(tmp_path: Path) -> None:
    """The runner and the session must record the same archive digest."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.session is not None
    run.session["artifacts"]["workspace_digest"] = "sha256:" + "f" * 64

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]
    assert any("archive digest" in item["detail"] for item in outcome["checks"] if not item["ok"])


def test_a_workspace_path_the_runner_did_not_score_is_refused(tmp_path: Path) -> None:
    """Both sides must name the same scoring object."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.export is not None
    run.export["workspace_path"] = str(tmp_path / "elsewhere")

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]
    assert any("scored" in item["detail"] for item in outcome["checks"] if not item["ok"])


def test_an_invalid_directory_file_count_is_refused(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.export is not None
    run.export["directory_files"] = "ten"

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]


def test_the_directory_identity_must_come_from_the_runner(tmp_path: Path) -> None:
    """A value that only exists in the session has no provenance."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.session is not None
    assert run.export is not None
    run.session["artifacts"]["directory_digest"] = run.export["directory_digest"]
    run.session["artifacts"]["directory_files"] = run.export["directory_files"]
    run.export.pop("directory_digest")
    run.export.pop("directory_files")

    outcome = _audit(cell, run)

    assert executor.STOP_EXPORT in outcome["stops"]


# -- public test evidence ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "state", "goal"),
    [
        ("public_fail_then_pass", "PASS", True),
        ("public_pass_then_fail", "FAIL", False),
        ("public_not_run", "NOT_RUN", None),
        ("public_error", "ERROR", None),
    ],
)
def test_the_public_verdict_is_the_last_recorded_run(
    tmp_path: Path, behaviour: str, state: str, goal: bool | None
) -> None:
    cell = contract.canary_cells()[0]
    outcome = _audit(cell, offline.synthetic_cell_run(cell, tmp_path, behaviour=behaviour))

    assert outcome["public_test_status"] == state
    assert outcome["goal_correctness"] is goal
    assert outcome["verdict"] != "STOPPED"  # a public result is data, not an integrity failure


@pytest.mark.parametrize(
    ("behaviour", "state"),
    [
        ("public_missing_last_status", "INCOMPLETE"),
        ("public_corrupt_status", "INVALID"),
        ("public_identity_mismatch", "INVALID"),
        ("public_exit_contradiction", "INVALID"),
    ],
)
def test_broken_public_evidence_is_refused_and_never_scored(
    tmp_path: Path, behaviour: str, state: str
) -> None:
    """A counted call with no record, a corrupt status, a foreign suite, a contradicted exit."""

    cell = contract.canary_cells()[0]
    outcome = _audit(cell, offline.synthetic_cell_run(cell, tmp_path, behaviour=behaviour))

    assert outcome["public_test_status"] == state
    assert executor.STOP_MISSING_EVIDENCE in outcome["stops"]
    assert outcome["goal_correctness"] is not True
    assert outcome["verdict"] == "STOPPED"


def test_a_counted_call_without_a_record_is_not_the_earlier_pass(tmp_path: Path) -> None:
    """The reachable producer shape: pass, edit, then a sandbox error that records nothing."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path, behaviour="public_missing_last_status")
    assert run.session is not None
    suite = run.session["public_tests"]
    assert suite["calls"] == 2
    assert len(suite["runs"]) == 1
    assert suite["runs"][-1]["status"] == "PASS"

    verdict = executor.public_test_verdict(suite)

    assert verdict["state"] == "INCOMPLETE"
    assert verdict["passed"] is None
    assert verdict["calls_consistent"] is False


def test_a_foreign_suite_identity_is_refused(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    task = contract.TASKS_BY_ID[cell["task_id"]]
    frozen = contract.public_test_spec(task)
    session = offline._synthetic_session(cell=cell, root=tmp_path)[0]  # noqa: SLF001

    good = executor.public_test_verdict(session["public_tests"], expected=frozen)
    bad = executor.public_test_verdict(
        dict(
            session["public_tests"],
            runs=[dict(session["public_tests"]["runs"][0], command_digest="sha256:" + "0" * 64)],
        ),
        expected=frozen,
    )

    assert good["state"] == "PASS"
    assert bad["state"] == "INVALID"
    assert bad["identity_consistent"] is False


def test_a_missing_public_suite_cannot_be_bypassed(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(
        cell, tmp_path, overrides={"public_tests": {"status": "NOT_CONFIGURED", "runs": []}}
    )

    outcome = _audit(cell, run)

    assert outcome["public_test_status"] == "NOT_CONFIGURED"
    assert executor.STOP_IDENTITY_MISMATCH in outcome["stops"]


def test_the_producer_writes_no_top_level_public_status(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    session = offline._synthetic_session(cell=cell, root=tmp_path)[0]  # noqa: SLF001

    assert "status" not in session["public_tests"]
    assert session["public_tests"]["runs"]
    assert session["public_tests"]["calls"] == len(session["public_tests"]["runs"])


def test_the_verdict_reads_runs_and_counts_calls(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    session = offline._synthetic_session(cell=cell, root=tmp_path)[0]  # noqa: SLF001

    verdict = executor.public_test_verdict(session["public_tests"])

    assert verdict["state"] == "PASS"
    assert verdict["calls_consistent"] is True
    assert verdict["last_exit_code"] == 0


def test_a_missing_public_block_is_not_a_pass(tmp_path: Path) -> None:
    assert executor.public_test_verdict(None)["state"] == "NOT_CONFIGURED"
    assert executor.public_test_verdict(None)["passed"] is not True
    assert executor.public_test_verdict({"runs": [], "calls": 0})["state"] == "NOT_RUN"
    # A block with no call count cannot be read as "nothing ran".
    assert executor.public_test_verdict({"runs": []})["state"] == "INCOMPLETE"


# -- oracle and stop policy --------------------------------------------------------------------


def test_an_unrun_oracle_cannot_produce_a_pass(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    run.oracle = None

    outcome = _audit(cell, run)

    assert outcome["verdict"] == "STOPPED"
    assert outcome["goal_correctness"] is not True


def test_a_measured_oracle_failure_does_not_stop_the_matrix(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    outcome = _audit(cell, offline.synthetic_cell_run(cell, tmp_path, behaviour="oracle_fail"))

    assert outcome["stops"] == []
    assert outcome["verdict"] == "MEASURED_FAILURE"
    assert outcome["execution_integrity"] == "PASS"


def test_an_integrity_failure_stops_the_matrix(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    Path((run.export or {})["workspace_path"], "pipeline", "api.py").unlink()

    outcome = _audit(cell, run)

    assert outcome["stops"]
    assert outcome["verdict"] == "STOPPED"


# -- the real producer, with real runs --------------------------------------------------------


def test_the_real_runner_records_a_public_test_failure(tmp_path: Path) -> None:
    """A scripted Main that actually calls the tool, through the production runner."""

    from anycode.types import ToolUseBlock

    # C0: the provider registry is process-global and refuses a duplicate run id, and the other
    # live-runner tests in this suite already drive C1 and C2.
    cell = contract.canary_cells()[0]
    scripted = probe.ScriptedProvider(
        main_script=[
            lambda _messages: [
                ToolUseBlock(
                    id="call-public", name="run_public_tests", input={"reason": "check the suite"}
                )
            ],
            lambda _messages: [probe.TextBlock(text="REPORT: the suite is red.")],
        ],
        child_scripts={},
    )
    config = executor.RunnerConfig(
        llm_provider="fake",
        llm_factory=lambda: scripted,
        sandbox_provider_name="fake",
        sandbox_factory=lambda root: FakeSandboxProvider(root),
        require_real_sandbox=False,
        main_max_turns=4,
    )
    runner = executor.live_cell_runner(config)
    context = {
        "plan": _plan(),
        "runs_root": tmp_path / "runs",
        "evidence_root": tmp_path / "evidence",
    }

    async def _await() -> executor.CellRun:
        return await runner(cell, context)

    run = asyncio.run(_await())
    block = (run.session or {})["public_tests"]
    outcome = _audit(cell, run)

    assert block["calls"] == 1
    assert len(block["runs"]) == 1
    assert block["runs"][0]["status"] == "FAIL"
    assert block["runs"][0]["exit_code"] not in (0, None)
    assert outcome["public_test_status"] == "FAIL"
    assert outcome["goal_correctness"] is False
    # The fake sandbox is still not isolated; that remains the only integrity stop.
    assert set(outcome["stops"]) == {executor.STOP_HOST_ISOLATION}


class _ProviderCommandRunner:
    """The fake provider presented as the one capability ``PublicTestRunner`` needs."""

    def __init__(self, provider: FakeSandboxProvider, handle: Any) -> None:
        self.provider = provider
        self.handle = handle
        self.ready = True

    async def run_public_command(
        self, command: str, *, timeout_seconds: int = 120, output_chars: int = 4000
    ) -> dict[str, Any]:
        from anycode.sandbox import SandboxCommand

        result = await self.provider.execute(
            self.handle,
            SandboxCommand(
                command=command, cwd="/workspace/project", timeout_seconds=timeout_seconds
            ),
        )
        text = result.stdout or ""
        return {
            "command": command,
            "cwd": "/workspace/project",
            "ok": result.ok,
            "exit_code": None if not result.ok else result.exit_code,
            "status": "ERROR" if not result.ok else ("PASS" if result.exit_code == 0 else "FAIL"),
            "output": text[-output_chars:],
            "output_chars": len(text),
            "output_truncated": len(text) > output_chars,
            "output_digest": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "elapsed_ms": 1.0,
            "error": None if result.ok else (result.stderr or "sandbox error"),
        }


def test_the_real_producer_records_fail_then_pass(tmp_path: Path) -> None:
    """``PublicTestRunner`` over a real workspace: the seed fails, the reference passes."""

    import shutil

    from anycode.identity.context import ExecutionContext
    from anycode.types import AgentInfo, ToolUseContext
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    from atlasos.app.public_tests import PublicTestInput, PublicTestRunner
    from atlasos.sandbox.models import SandboxSpec

    cell = contract.canary_cells()[1]
    task = contract.TASKS_BY_ID[cell["task_id"]]
    spec = contract.public_test_spec(task)
    fixture = executor.prepare_fixture(task, tmp_path / "fixture")
    if fixture["matches_frozen_seed"] is not True:
        raise AssertionError("the fixture does not match the frozen seed")

    provider = FakeSandboxProvider(tmp_path / "sandbox")
    created = asyncio.run(
        provider.create(
            SandboxSpec(
                run_id="producer",
                task_id="producer",
                correlation_id="producer:producer",
                context=ExecutionContext(principal="user:test", tenant_scope="atlas"),
                image="atlas-uat-python:3.12",
                network="none",
            )
        )
    )
    handle = provider.handles[created.handle.id if created.handle else "sandbox-1"]
    project = provider.root / handle.id / "workspace" / "project"
    shutil.copytree(
        fixture["path"], project, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git")
    )

    runner = PublicTestRunner(
        _ProviderCommandRunner(provider, handle),
        spec,
        task_id="producer-main",
        agent_name="atlas-main",
    )
    context = ToolUseContext(
        agent=AgentInfo(name="atlas-main", role="main", model="fake"),
        execution_context=ExecutionContext(
            principal="user:test",
            tenant_scope="atlas",
            attributes={
                "run_id": "producer",
                "task_id": "producer-main",
                "worker_id": "producer-main-worker",
                "atlas_role": "main",
            },
        ),
    )

    async def _run() -> list[dict[str, Any]]:
        await runner.run_public_tests(PublicTestInput(reason="first"), context)
        for name, text in task.reference.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        await runner.run_public_tests(PublicTestInput(reason="after the fix"), context)
        return runner.evidence()

    runs = asyncio.run(_run())

    assert [item["status"] for item in runs] == ["FAIL", "PASS"]
    assert [item["call_index"] for item in runs] == [1, 2]
    assert all(item["test_id"] == spec.test_id for item in runs)
    assert all(item["command_digest"] == spec.command_digest() for item in runs)

    # The producer's own records, unedited, are what the gate reads.
    block = {
        "contract": spec.payload(),
        "command_digest": spec.command_digest(),
        "calls": runner.calls,
        "runs": runs,
        "oracle_is_separate": True,
    }
    verdict = executor.public_test_verdict(block, expected=spec)
    assert verdict["state"] == "PASS"
    assert verdict["calls_consistent"] is True
    assert verdict["identity_consistent"] is True
    assert verdict["outcome_consistent"] is True

    session, run_dir, fixture_info, artifacts = offline._synthetic_session(  # noqa: SLF001
        cell=cell, root=tmp_path / "cell"
    )
    session["public_tests"] = block
    (run_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    export = dict(artifacts)
    export.update(executor.workspace_identity(Path(str(artifacts["workspace_path"]))))
    export["archive_digest_verifiable"] = False
    outcome = _audit(
        cell,
        executor.CellRun(
            session=session,
            run_dir=run_dir,
            fixture_root=Path(fixture_info["path"]),
            oracle={
                "status": "PASS",
                "kind": "HIDDEN_TESTS_PASSED",
                "execution_mode": "sandbox",
                "sandbox_id": "oracle-producer",
            },
            fixture=fixture_info,
            export=export,
            sandbox_plan=executor.sandbox_plan(require_real=False, provider_name="fake").as_dict(),
            expected_run_id=f"bplus-c{int(cell['order']):02d}",
        ),
    )

    assert outcome["public_test_status"] == "PASS"
    assert outcome["public_test_runs"] == 2
    assert outcome["stops"] == [], [
        item for item in outcome["checks"] if not item["ok"]
    ]
    assert outcome["goal_correctness"] is True


# -- the matrix --------------------------------------------------------------------------------


def _statement(tmp_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "statement.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return {
        "kind": "external_file",
        "path": str(path),
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "content_digest": authority.digest_of(body),
        "body": body,
    }


def _signed(tmp_path: Path) -> dict[str, Any]:
    plan = _plan()
    head = executor.git_head()
    return authority.sign(
        authority.pending_record(
            plan,
            protocol_digest_value=authority.protocol_digest(contract.protocol_document()),
            executor=executor.executor_identity(),
        ),
        approved_by="test",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="test signature, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=1.0,
        per_cell_cost_stop_usd=0.5,
        provenance=_statement(tmp_path, {"statement": "test"}),
    )


def test_a_healthy_six_cell_matrix_completes(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(_signed(tmp_path)), encoding="utf-8")
    with executor.claim_root_override(tmp_path / "claims"):
        report = asyncio.run(
            executor.dispatch(
                contract.CANARY_PLAN_ID,
                runs_root=tmp_path / "runs",
                evidence_root=tmp_path / "evidence",
                authority_path=path,
                cell_runner=offline._synthetic_runner({}),  # noqa: SLF001
                repo_root=contract.REPO_ROOT,
                mode=executor.MODE_OFFLINE_PROOF,
                clock=lambda: "2026-11-25T00:00:00Z",
            )
        )

    assert report["complete"] is True, report.get("stop_reason")
    assert report["cells_passed"] == 6
    # The floor is the sum of what each cell cost, so six identical cells are six times one cell.
    assert report["observed_cost_floor_usd"] == round(
        sum(cell["delta_cost_usd"] for cell in report["cells"]), 6
    )
