"""Closure-2B-0-R2/R3: the durable execution lease, against a real SQLite file.

These tests do not mock the database. They write files, hold real locks, and (for the race) start
real Python processes, because "two processes cannot both claim the plan" is exactly the property a
mock would hide. R3 adds the other half: a lock that arrives while the pilot is running must
stop the dispatch, and it is a real lock contending with a real state write.

No model call, no sandbox: the ledger is the only thing under test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.pilot_ledger import (
    PilotAlreadyClaimed,
    PilotLedger,
    PilotLedgerLocked,
    PilotLedgerStateInvalid,
    PilotLedgerUnavailable,
    default_ledger_path,
)
from experiments.evaluation.closure2.pilot_runner import (
    PILOT_PLAN,
    REPO_ROOT,
    Cell,
    build_dry_run_manifest,
    run_live_pilot,
)

PLAN_DIGEST = PILOT_PLAN.digest()
CELLS = [("closure2-pipeline-migration::N0", 0), ("closure2-pipeline-migration::N1", 0)]


def claim(
    ledger: PilotLedger,
    *,
    digest: str = PLAN_DIGEST,
    cells: list[tuple[str, int]] | None = None,
):
    return ledger.claim_plan(
        plan_digest=digest,
        plan_id="closure2-pilot-1",
        cells=list(cells if cells is not None else CELLS),
        runner_revision="closure-2b-0-r2",
        git_commit="deadbeef",
    )


def test_a_plan_can_be_claimed_exactly_once(tmp_path: Path) -> None:
    ledger = PilotLedger(tmp_path / "ledger.sqlite")
    lease = claim(ledger)

    assert lease.plan_digest == PLAN_DIGEST
    row = ledger.get_claim(PLAN_DIGEST)
    assert row is not None
    assert row["claim_status"] == "CONSUMED"
    assert [cell["status"] for cell in ledger.cell_rows(PLAN_DIGEST)] == ["CLAIMED", "CLAIMED"]

    with pytest.raises(PilotAlreadyClaimed) as error:
        claim(ledger)
    assert error.value.reason_code == "PILOT_ALREADY_CLAIMED"
    assert "already claimed" in str(error.value)


def test_the_claim_survives_a_new_connection_and_process_boundary(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    claim(PilotLedger(path))
    # A fresh object, a fresh connection: the claim is in the file, not in memory.
    with pytest.raises(PilotAlreadyClaimed):
        claim(PilotLedger(path))


def test_a_second_plan_with_the_same_cell_keys_is_refused_by_the_primary_key(
    tmp_path: Path,
) -> None:
    ledger = PilotLedger(tmp_path / "ledger.sqlite")
    claim(ledger)
    rows = ledger.cell_rows(PLAN_DIGEST)
    assert {(row["cell_id"], row["repetition_id"]) for row in rows} == set(CELLS)


def test_an_illegal_transition_is_refused(tmp_path: Path) -> None:
    ledger = PilotLedger(tmp_path / "ledger.sqlite")
    lease = claim(ledger)
    lease.mark_running(*CELLS[0])
    lease.mark_completed(*CELLS[0], result_path="runs/x/result.json")

    with pytest.raises(PilotLedgerStateInvalid):
        lease.mark_running(*CELLS[0])
    with pytest.raises(PilotLedgerStateInvalid):
        lease.mark(*CELLS[1], "COMPLETED")  # CLAIMED -> COMPLETED skips RUNNING


def test_a_failed_transaction_leaves_no_half_claim(tmp_path: Path) -> None:
    """The plan row and the cell rows are one transaction, not two writes."""

    path = tmp_path / "ledger.sqlite"
    ledger = PilotLedger(path)
    claim(ledger)
    # Simulate a crash-free leftover: a cell row for another digest, which will make the plan's cell
    # insert fail after its own row was written.
    connection = sqlite3.connect(str(path))
    connection.execute(
        "INSERT INTO cell_claims (plan_digest, cell_id, repetition_id, attempt_id, status, "
        "claimed_at) VALUES (?, ?, ?, ?, 'CLAIMED', '2026-11-17T00:00:00+00:00')",
        ("other-digest", CELLS[0][0], 0, "stray"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(PilotAlreadyClaimed):
        claim(ledger, digest="other-digest")
    assert ledger.get_claim("other-digest") is None, "the plan row must be rolled back"


def test_the_claim_is_not_released_by_marking_a_cell_failed(tmp_path: Path) -> None:
    ledger = PilotLedger(tmp_path / "ledger.sqlite")
    lease = claim(ledger)
    lease.mark_running(*CELLS[0])
    lease.mark_failed(*CELLS[0], failure_reason="RuntimeError: boom")

    rows = ledger.cell_rows(PLAN_DIGEST)
    assert rows[0]["status"] == "FAILED"
    assert "boom" in rows[0]["failure_reason"]
    with pytest.raises(PilotAlreadyClaimed):
        claim(ledger)


def test_recover_interrupted_marks_running_cells_but_keeps_the_claim(tmp_path: Path) -> None:
    ledger = PilotLedger(tmp_path / "ledger.sqlite")
    lease = claim(ledger)
    lease.mark_running(*CELLS[0])

    changed = ledger.recover_interrupted(PLAN_DIGEST)

    assert changed == [f"{CELLS[0][0]}@0"]
    statuses = [cell["status"] for cell in ledger.cell_rows(PLAN_DIGEST)]
    assert statuses == ["INTERRUPTED_UNKNOWN", "CLAIMED"]
    with pytest.raises(PilotAlreadyClaimed):
        claim(ledger)


def test_a_locked_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = PilotLedger(path)
    claim(ledger)  # creates the schema

    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        impatient = PilotLedger(path, busy_timeout_ms=100)
        with pytest.raises(PilotLedgerLocked) as error:
            claim(impatient, digest="another-digest")
        assert error.value.reason_code == "PILOT_LEDGER_LOCKED"
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    # The lock is gone, so the ledger is usable again: the failure was a refusal, not corruption.
    claim(ledger, digest="another-digest")


def test_an_unusable_ledger_path_fails_closed(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("I am a file", encoding="utf-8")
    ledger = PilotLedger(blocker / "ledger.sqlite")
    with pytest.raises(PilotLedgerUnavailable) as error:
        claim(ledger)
    assert error.value.reason_code == "PILOT_LEDGER_UNAVAILABLE"


def test_a_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    path.write_bytes(b"this is not a sqlite database" * 10)
    ledger = PilotLedger(path)
    with pytest.raises(PilotLedgerUnavailable):
        claim(ledger)


def test_the_default_ledger_is_user_state_not_a_results_directory() -> None:
    path = default_ledger_path()
    assert path.is_absolute()
    assert not path.is_relative_to(REPO_ROOT)
    assert "pilot-ledger.sqlite" in path.name
    assert "closure2" in path.parts


def test_a_dry_run_never_touches_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Planning is not a claim: the ledger is not opened, and no file appears."""

    from experiments.evaluation.closure2 import pilot_runner

    path = tmp_path / "never-created.sqlite"

    def forbidden() -> None:
        raise AssertionError("the dry run must not open the ledger")

    monkeypatch.setattr(pilot_runner, "_ledger_factory", forbidden)
    manifest = build_dry_run_manifest()
    assert manifest["mode"] == "DRY_RUN"
    assert manifest["pilot_execution_ready"] is False
    assert not path.exists()


# ------------------------------------------------------------------ cross-process uniqueness

_RACE_SCRIPT = r"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[4])
from experiments.evaluation.closure2.pilot_ledger import PilotLedger, PilotLedgerError

ledger = PilotLedger(Path(sys.argv[1]), busy_timeout_ms=10000)
go = Path(sys.argv[2])
digest = sys.argv[3]

time.sleep(random.uniform(0.0, 0.05))
deadline = time.time() + 30
while not go.exists():
    if time.time() > deadline:
        print(json.dumps({"ok": False, "code": "BARRIER_TIMEOUT"}))
        raise SystemExit(0)
    time.sleep(0.005)

try:
    ledger.claim_plan(
        plan_digest=digest,
        plan_id="closure2-pilot-1",
        cells=[["cell-a", 0], ["cell-b", 0]],
        runner_revision="race",
    )
except PilotLedgerError as error:
    print(json.dumps({"ok": False, "code": error.reason_code}))
else:
    print(json.dumps({"ok": True, "code": "CLAIMED"}))
"""


def test_four_processes_race_and_exactly_one_wins(tmp_path: Path) -> None:
    """Real processes, real file, real lock: not a thread, not a mock."""

    ledger_path = tmp_path / "ledger.sqlite"
    go = tmp_path / "go"
    script = _RACE_SCRIPT
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(ledger_path), str(go), PLAN_DIGEST, str(REPO_ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    go.write_text("go", encoding="utf-8")
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    successes = [item for item in results if item["ok"]]
    refusals = [item for item in results if not item["ok"]]
    assert len(successes) == 1, results
    assert len(refusals) == 3, results
    assert {item["code"] for item in refusals} == {"PILOT_ALREADY_CLAIMED"}

    # The winner's rows are the only rows.
    rows = PilotLedger(ledger_path).cell_rows(PLAN_DIGEST)
    assert {row["cell_id"] for row in rows} == {"cell-a", "cell-b"}


_CRASH_SCRIPT = r"""
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from experiments.evaluation.closure2.pilot_ledger import PilotLedger

ledger = PilotLedger(Path(sys.argv[1]))
lease = ledger.claim_plan(
    plan_digest=sys.argv[2],
    plan_id="closure2-pilot-1",
    cells=[["cell-a", 0]],
    runner_revision="crash",
)
lease.mark_running("cell-a", 0)
sys.stdout.write("claimed\n")
sys.stdout.flush()
os._exit(9)
"""


def test_a_process_that_dies_after_claiming_keeps_the_claim(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite"
    completed = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(ledger_path), PLAN_DIGEST, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 9, completed.stderr
    assert "claimed" in completed.stdout

    ledger = PilotLedger(ledger_path)
    with pytest.raises(PilotAlreadyClaimed):
        ledger.claim_plan(
            plan_digest=PLAN_DIGEST,
            plan_id="closure2-pilot-1",
            cells=[("cell-a", 0)],
            runner_revision="restart",
        )

    # The dead process left a RUNNING cell; recovery records that, and still does not hand back the
    # right to execute.
    assert ledger.recover_interrupted(PLAN_DIGEST) == ["cell-a@0"]
    assert [row["status"] for row in ledger.cell_rows(PLAN_DIGEST)] == ["INTERRUPTED_UNKNOWN"]
    with pytest.raises(PilotAlreadyClaimed):
        ledger.claim_plan(
            plan_digest=PLAN_DIGEST,
            plan_id="closure2-pilot-1",
            cells=[("cell-a", 0)],
            runner_revision="restart-again",
        )


# ------------------------------------------------- R3: a lock during the run stops the dispatch

_APPROVED = {
    "status": "APPROVED",
    "approved_digest": protocol.C2A_R1_PREREGISTRATION_DIGEST,
    "approved_by": "integration-test",
    "approval_commit": "0" * 40,
    "approved_at": "2026-11-17T00:00:00+00:00",
}


def _authorize(monkeypatch: pytest.MonkeyPatch, cells: tuple[Cell, ...]) -> str:
    """Approve both layers for exactly ``cells`` and return the plan digest."""

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(_APPROVED))
    plan = dataclasses.replace(
        PILOT_PLAN,
        cells=tuple(cell.cell_id for cell in cells),
        repetitions=(0,),
        max_cell_executions=len(cells),
        estimated_cost_ceiling_usd=5.0,
    )
    monkeypatch.setattr(pilot_runner, "PILOT_PLAN", plan)
    monkeypatch.setattr(
        pilot_runner,
        "PILOT_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "integration-test",
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-17T00:00:00+00:00",
        },
    )
    return plan.digest()


class _LockAfterClaimLedger(PilotLedger):
    """A real ledger that loses the write lock the moment the claim commits."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, busy_timeout_ms=120)
        self._holder: sqlite3.Connection | None = None

    def claim_plan(self, **kwargs: Any) -> Any:
        lease = super().claim_plan(**kwargs)
        self._holder = sqlite3.connect(str(self.path), isolation_level=None)
        self._holder.execute("BEGIN EXCLUSIVE")
        return lease

    def release(self) -> None:
        if self._holder is not None:
            self._holder.execute("ROLLBACK")
            self._holder.close()
            self._holder = None


class _LockOnSecondWriteLedger(PilotLedger):
    """A real ledger that locks itself once the first cell's RUNNING row has been committed."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, busy_timeout_ms=120)
        self.writes = 0
        self._holder: sqlite3.Connection | None = None

    def set_cell_state(
        self, plan_digest: str, cell_id: str, repetition_id: int, status: str, **fields: Any
    ) -> None:
        self.writes += 1
        if self.writes == 2 and self._holder is None:
            self._holder = sqlite3.connect(str(self.path), isolation_level=None)
            self._holder.execute("BEGIN EXCLUSIVE")
        super().set_cell_state(plan_digest, cell_id, repetition_id, status, **fields)

    def release(self) -> None:
        if self._holder is not None:
            self._holder.execute("ROLLBACK")
            self._holder.close()
            self._holder = None


def test_a_real_lock_before_the_cell_stops_it_reaching_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contention on the RUNNING write, with a real lock on a real database."""

    cells = (Cell("closure2-pipeline-migration", "A0"), Cell("closure2-pipeline-migration", "A1-P"))
    digest = _authorize(monkeypatch, cells)
    ledger = _LockAfterClaimLedger(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: ledger)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        raise AssertionError("the executor must not run without a durable RUNNING row")

    try:
        summary = asyncio.run(
            run_live_pilot(cells, runs_root=tmp_path, executor=executor, live_intent=True)
        )
    finally:
        ledger.release()

    assert calls == []
    assert summary["execution_integrity_status"] == "BLOCKED_LEDGER_PRE_EXECUTION"
    assert summary["ledger_integrity"]["error_code"] == "PILOT_LEDGER_LOCKED"
    assert summary["ledger_integrity"]["executed_before_failure"] is False
    assert summary["not_started_cells"] == [cell.cell_id for cell in cells]

    # The claim was already committed and stays consumed; the lock did not turn into a retry.
    stored = PilotLedger(ledger.path).get_claim(digest)
    assert stored is not None and stored["claim_status"] == "CONSUMED"
    with pytest.raises(PilotAlreadyClaimed):
        PilotLedger(ledger.path).claim_plan(
            plan_digest=digest,
            plan_id="closure2-pilot-1",
            cells=[(cell.cell_id, 0) for cell in cells],
            runner_revision="after-lock",
        )
    assert [row["status"] for row in PilotLedger(ledger.path).cell_rows(digest)] == [
        "CLAIMED",
        "CLAIMED",
    ]


def test_a_real_lock_after_the_cell_ran_keeps_the_result_and_stops_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cells = (Cell("closure2-pipeline-migration", "A0"), Cell("closure2-pipeline-migration", "A1-P"))
    digest = _authorize(monkeypatch, cells)
    ledger = _LockOnSecondWriteLedger(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: ledger)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _stub_result()

    try:
        summary = asyncio.run(
            run_live_pilot(cells, runs_root=tmp_path, executor=executor, live_intent=True)
        )
    finally:
        ledger.release()

    assert len(calls) == 1, "the second cell must not be dispatched after a persistence failure"
    assert [record["arm_id"] for record in summary["cells"]] == ["A0"]
    assert summary["cells"][0]["status"] == "COMPLETED"
    assert summary["execution_integrity_status"] == "BLOCKED_LEDGER_POST_EXECUTION"
    assert summary["ledger_integrity"]["attempted_status"] == "COMPLETED"
    assert summary["ledger_integrity"]["error_code"] == "PILOT_LEDGER_LOCKED"
    assert summary["not_started_cells"] == [cells[1].cell_id]
    # The cell really ran, and the database really still says RUNNING: no invented COMPLETED row.
    assert [row["status"] for row in PilotLedger(ledger.path).cell_rows(digest)] == [
        "RUNNING",
        "CLAIMED",
    ]


def _stub_result() -> Any:
    """The minimum a runner result needs to be normalised."""

    from types import SimpleNamespace

    return SimpleNamespace(
        identity=SimpleNamespace(
            run_id="eval-stub",
            repetition_id=0,
            model="deepseek-chat",
            git_commit="deadbeef",
            git_dirty=False,
        ),
        metrics=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=200,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            estimated_cost_usd=0.01,
            model_turns=2,
            tool_calls=3,
            latency_ms=1000.0,
            sandbox_created=True,
            sandbox_destroyed=True,
            worker_selection=["w1"],
            extra={},
        ),
        oracle=SimpleNamespace(passed=True, environment_failure=False, failure_kind=None),
        environment={
            "sandbox_image": "atlas-uat-python:3.12",
            "workspace_isolation": "remote_opensandbox_container",
            "agent_latency_ms": 900.0,
        },
        run_dir="experiments/runs/closure2/stub",
        agent_success=True,
        success=True,
        failure_class="TASK_SUCCESS",
        failure_reason=None,
    )
