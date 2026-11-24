"""Stage B S06: executable reference policy for the 36-cell dispatch, exercised offline.

``runner --execute-cell`` runs exactly one cell and exits, so nothing in this repository enforces
order, concurrency, stop-loss or claim hygiene across cells. This module is the executable statement
of what an S06 executor must guarantee, driven against the real claim primitives on a temp evidence
root. It is a specification, not a scheduler: no model, no sandbox, no cell.

The properties: fixed order, at most one cell active, at most one claim per cell, stop at the first
cell that is not COMPLETED, never skip a failed cell, never clean up a claim, and never let two
processes own one cell.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark import protocol  # noqa: E402
from experiments.stage_benchmark.execution import admission, provision  # noqa: E402
from experiments.stage_benchmark.execution.models import AttemptAlreadyStarted  # noqa: E402

PLAN_PATH = REPO_ROOT / "experiments" / "stage_benchmark" / "s06-36cell-plan.json"


def _cells() -> list:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    document = protocol.read_protocol()
    return [provision.cell_from_plan(document, cell["cell_id"]) for cell in plan["cells"]]


def _dispatch(root: Path, *, fail_at: str | None = None) -> dict:
    """The reference policy. It stops at the first non-COMPLETED cell and never retries."""

    started: list[str] = []
    active = 0
    peak = 0
    for cell in _cells():
        cell_root = admission.cell_dir(root / "cells", cell)
        # One cell at a time: the executor must not start the next claim until this one returns.
        active += 1
        peak = max(peak, active)
        admission.claim_attempt(
            cell_root,
            cell=cell,
            preflight_digest="sha256:reference",
            authorization_digest="sha256:reference",
            source_digest="sha256:reference",
        )
        outcome = "FAILED" if cell.cell_id == fail_at else "COMPLETED"
        admission.mark_attempt(cell_root, "COLLECTED" if outcome == "COMPLETED" else "FAILED")
        active -= 1
        started.append(cell.cell_id)
        if outcome != "COMPLETED":
            break
    return {"started": started, "peak_active": peak}


def test_a_clean_matrix_runs_in_the_frozen_order_once(tmp_path: Path) -> None:
    result = _dispatch(tmp_path)
    expected = [cell.cell_id for cell in _cells()]
    assert result["started"] == expected
    assert len(result["started"]) == 36
    assert result["peak_active"] == 1


def test_the_run_stops_at_the_first_cell_that_is_not_completed(tmp_path: Path) -> None:
    expected = [cell.cell_id for cell in _cells()]
    failing = expected[16]
    result = _dispatch(tmp_path, fail_at=failing)
    assert result["started"] == expected[:17]
    assert result["started"][-1] == failing
    # Nothing after the failure was started, and the failed cell was not skipped over.
    assert set(expected[17:]).isdisjoint(result["started"])
    assert failing in result["started"]


def test_a_claim_is_never_removed_after_a_failure(tmp_path: Path) -> None:
    expected = [cell.cell_id for cell in _cells()]
    _dispatch(tmp_path, fail_at=expected[3])
    marker = admission.attempt_path(
        admission.cell_dir(tmp_path / "cells", _cells()[3])
    )
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["state"] == "FAILED"
    assert payload["retries_allowed"] is False


def test_a_second_attempt_on_the_same_cell_is_refused(tmp_path: Path) -> None:
    cell = _cells()[0]
    cell_root = admission.cell_dir(tmp_path / "cells", cell)
    admission.claim_attempt(
        cell_root,
        cell=cell,
        preflight_digest="sha256:a",
        authorization_digest="sha256:a",
        source_digest="sha256:a",
    )
    with pytest.raises(AttemptAlreadyStarted):
        admission.claim_attempt(
            cell_root,
            cell=cell,
            preflight_digest="sha256:b",
            authorization_digest="sha256:b",
            source_digest="sha256:b",
        )


def test_two_processes_cannot_own_one_cell_even_without_the_pre_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_CREAT|O_EXCL is the real arbiter; the prior check is only the cheap path."""

    cell = _cells()[5]
    cell_root = admission.cell_dir(tmp_path / "cells", cell)
    admission.claim_attempt(
        cell_root,
        cell=cell,
        preflight_digest="sha256:a",
        authorization_digest="sha256:a",
        source_digest="sha256:a",
    )
    monkeypatch.setattr(admission, "prior_attempt", lambda _path: None)
    with pytest.raises(AttemptAlreadyStarted):
        admission.claim_attempt(
            cell_root,
            cell=cell,
            preflight_digest="sha256:b",
            authorization_digest="sha256:b",
            source_digest="sha256:b",
        )


def test_an_existing_claim_blocks_the_cell_before_any_model_call(tmp_path: Path) -> None:
    cell = _cells()[7]
    cell_root = admission.cell_dir(tmp_path / "cells", cell)
    admission.claim_attempt(
        cell_root,
        cell=cell,
        preflight_digest="sha256:a",
        authorization_digest="sha256:a",
        source_digest="sha256:a",
    )
    preflight = admission.preflight(
        evidence_root=tmp_path / "cells",
        cell=cell,
        live=True,
        offline_report={"passed": True},
    )
    assert preflight["status"] == "BLOCKED"
    problems = " ".join(preflight["problems"])
    assert "no_cell_marker_or_evidence" in problems
