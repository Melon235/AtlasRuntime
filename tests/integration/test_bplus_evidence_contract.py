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
        "no directory identity" in item["detail"]
        for item in outcome["checks"]
        if not item["ok"]
    )


def test_the_archive_digest_is_never_compared_with_the_directory(tmp_path: Path) -> None:
    """Overwriting the archive digest must not change the export verdict."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    assert run.session is not None
    run.session["artifacts"]["workspace_digest"] = "sha256:" + "f" * 64

    assert _audit(cell, run)["stops"] == []


# -- public test evidence ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "state", "goal"),
    [
        ("public_fail_then_pass", "PASS", True),
        ("public_pass_then_fail", "FAIL", False),
        ("public_not_run", "NOT_RUN", None),
        ("public_error", "ERROR", None),
        ("public_unknown", "UNKNOWN", None),
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
    assert executor.public_test_verdict(None)["state"] in {"NOT_RUN", "NOT_CONFIGURED"}
    assert executor.public_test_verdict(None)["passed"] is not True
    assert executor.public_test_verdict({"runs": []})["state"] == "NOT_RUN"


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
