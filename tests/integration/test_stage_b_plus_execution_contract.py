"""Stage B+ execution contract: identity, authority, claim, dispatch gate.

The properties under test are the ones that would let a run happen without permission or report a
broken matrix as complete:

* the canary is its own object and cannot be dispatched with a formal signature (or the reverse);
* an unsigned record is refused, and a signed one is refused the moment a bound field moves;
* a dispatch claim is created once and never reopened;
* a measurable goal failure continues while an infrastructure failure stops, including on the last
  cell;
* a counter the provider did not report stops the matrix instead of being written as zero.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_b_plus import authority, contract, executor, offline


def _plan(scope: str) -> dict[str, Any]:
    return contract.scope_plan(scope)


def _identity() -> dict[str, Any]:
    return executor.executor_identity()


def _digest() -> str:
    return authority.protocol_digest(contract.protocol_document())


def _statement(tmp_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "owner-statement.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return {
        "kind": "external_file",
        "path": str(path),
        "sha256": "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "content_digest": authority.digest_of(body),
        "body": body,
    }


def _signed(
    tmp_path: Path,
    scope: str,
    *,
    head: str | None = None,
    cost_stop: float = 1.0,
    per_cell: float = 0.5,
    **overrides: Any,
) -> dict[str, Any]:
    plan = _plan(scope)
    head = head if head is not None else executor.git_head()
    record = authority.sign(
        authority.pending_record(plan, protocol_digest_value=_digest(), executor=_identity()),
        approved_by="test",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="test signature, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=cost_stop,
        per_cell_cost_stop_usd=per_cell,
        provenance=_statement(tmp_path, {"statement": "test", "scope": scope}),
    )
    record.update(overrides)
    return record


def _verify(record: dict[str, Any] | None, scope: str, **kwargs: Any) -> dict[str, Any]:
    return authority.verify(
        record,
        plan=_plan(scope),
        head=kwargs.pop("head", executor.git_head()),
        executor=_identity(),
        protocol_digest_value=_digest(),
        repo_root=contract.REPO_ROOT,
        **kwargs,
    )


# -- plans --------------------------------------------------------------------------------------


def test_canary_is_six_cells_one_per_arm() -> None:
    cells = contract.canary_cells()
    assert len(cells) == 6
    assert len({cell["cell_id"] for cell in cells}) == 6
    assert sorted(cell["arm_id"] for cell in cells) == sorted(contract.ARM_IDS)
    assert {cell["task_id"] for cell in cells} == set(contract.CANARY_TASK_IDS)
    assert {cell["repetition"] for cell in cells} == {1}
    assert all(cell["is_benchmark_sample"] is False for cell in cells)
    assert plan_digest_of(cells) == _plan(contract.SCOPE_CANARY)["digest"]


def plan_digest_of(cells: list[dict[str, Any]]) -> str:
    from experiments.stage_benchmark import matrix

    return matrix.plan_digest(cells)


def test_canary_ids_cannot_collide_with_formal_ids() -> None:
    canary = {cell["cell_id"] for cell in contract.canary_cells()}
    formal = {cell["cell_id"] for cell in contract.cells()}
    assert canary.isdisjoint(formal)
    assert all(cell.startswith("canary.") for cell in canary)


def test_formal_plan_identity_is_unchanged() -> None:
    """Approving this revision must not silently move the formal matrix."""

    plan = _plan(contract.SCOPE_FORMAL)
    assert plan["digest"] == contract.REVISION["previous_formal_plan_digest"]
    assert plan["cells_count"] == 24
    assert set(plan["arms"]) == set(contract.FORMAL_ARM_IDS)


def test_canary_and_formal_scopes_are_named_apart() -> None:
    canary, formal = _plan(contract.SCOPE_CANARY), _plan(contract.SCOPE_FORMAL)
    assert canary["scope"] != formal["scope"]
    assert canary["claim_namespace"] != formal["claim_namespace"]
    assert canary["runs_subdir"] != formal["runs_subdir"]
    assert canary["evidence_subdir"] != formal["evidence_subdir"]


# -- authority ----------------------------------------------------------------------------------


def test_pending_record_is_refused() -> None:
    plan = _plan(contract.SCOPE_CANARY)
    pending = authority.pending_record(
        plan, protocol_digest_value=_digest(), executor=_identity()
    )
    verdict = _verify(pending, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert len(verdict["problems"]) >= 7
    with pytest.raises(PermissionError):
        authority.require(
            pending,
            plan=plan,
            head=executor.git_head(),
            executor=_identity(),
            protocol_digest_value=_digest(),
            repo_root=contract.REPO_ROOT,
        )


def test_signed_record_is_accepted(tmp_path: Path) -> None:
    verdict = _verify(_signed(tmp_path, contract.SCOPE_CANARY), contract.SCOPE_CANARY)
    assert verdict["ready"] is True, verdict["problems"]


def test_scope_is_not_interchangeable(tmp_path: Path) -> None:
    canary_record = _signed(tmp_path, contract.SCOPE_CANARY)
    assert _verify(canary_record, contract.SCOPE_CANARY)["ready"] is True
    cross = _verify(canary_record, contract.SCOPE_FORMAL)
    assert cross["ready"] is False
    assert any("scope" in item or "plan_id" in item for item in cross["problems"])


def test_a_missing_or_extra_cell_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    short = dict(record, cell_ids=record["cell_ids"][:-1], cells_count=5)
    assert _verify(short, contract.SCOPE_CANARY)["ready"] is False
    extra = dict(record, cell_ids=[*record["cell_ids"], "canary.invented.C0.r1"])
    verdict = _verify(extra, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("outside the plan" in item for item in verdict["problems"])


def test_a_duplicate_cell_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    duplicated = list(record["cell_ids"])
    duplicated[-1] = duplicated[0]
    verdict = _verify(dict(record, cell_ids=duplicated), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("duplicate" in item for item in verdict["problems"])


def test_a_reordered_cell_list_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    reordered = list(reversed(record["cell_ids"]))
    verdict = _verify(dict(record, cell_ids=reordered), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("order" in item for item in verdict["problems"])


@pytest.mark.parametrize("value", [0, -1.0, True, "1.0", float("inf"), float("nan")])
def test_an_illegal_cost_stop_refuses(tmp_path: Path, value: Any) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(dict(record, cost_stop_usd=value), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("cost_stop_usd" in item for item in verdict["problems"])


def test_per_cell_stop_cannot_exceed_the_cumulative_stop(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY, cost_stop=0.1, per_cell=0.2)
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("per-cell" in item for item in verdict["problems"])


def test_attempt_limit_must_be_one(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(dict(record, attempt_limit=2), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("attempt_limit" in item for item in verdict["problems"])


def test_a_consumed_claim_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(record, contract.SCOPE_CANARY, claim_consumed=True)
    assert verdict["ready"] is False
    assert any("claim" in item for item in verdict["problems"])


def test_provenance_must_still_hash(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    record["approval_provenance"]["sha256"] = "sha256:" + "0" * 64
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("hash" in item for item in verdict["problems"])


def test_a_missing_approval_file_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    Path(record["approval_provenance"]["path"]).unlink()
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("does not exist" in item for item in verdict["problems"])


def test_an_approval_cannot_name_the_head_it_lives_in() -> None:
    """A commit provenance pointing at the execution head is a fixed point; it is refused."""

    head = executor.git_head()
    problems = authority._provenance_problems(
        {
            "kind": "commit",
            "commit": head,
            "head": head,
            "content_digest": authority.digest_of({"a": 1}),
            "body": {"a": 1},
        },
        repo_root=contract.REPO_ROOT,
    )
    assert any("predate" in item for item in problems)


# -- dispatch -----------------------------------------------------------------------------------


def _dispatch(evidence: Path, authority_path: Path | None, runner: Any) -> dict[str, Any]:
    return asyncio.run(
        executor.dispatch(
            contract.SCOPE_CANARY,
            runs_root=evidence / "runs",
            evidence_root=evidence,
            authority_path=authority_path,
            head=executor.git_head(),
            cell_runner=runner,
            repo_root=contract.REPO_ROOT,
            clock=lambda: "2026-11-25T00:00:00Z",
        )
    )


def test_dispatch_without_authority_starts_nothing(tmp_path: Path) -> None:
    report = _dispatch(tmp_path / "evidence", None, offline._synthetic_runner({}))
    assert report["refused"] is True
    assert report["cells_started"] == 0
    assert report["stop_reason"] == executor.STOP_NOT_AUTHORIZED
    assert (tmp_path / "evidence" / "dispatch-refused.json").is_file()


def test_a_clean_dispatch_runs_every_cell(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    evidence = tmp_path / "evidence"
    report = _dispatch(evidence, path, offline._synthetic_runner({}))
    assert report["complete"] is True
    assert report["cells_started"] == 6
    assert report["cells_passed"] == 6
    assert report["stop_reason"] == executor.STOP_NONE
    assert report["is_benchmark_sample"] is False
    assert report["aggregate_includes_sample"] is False
    # the claim and every attempt marker exist exactly once
    assert (evidence / "dispatch.claim").is_file()
    assert len(list((evidence / "cells").glob("*/attempt.json"))) == 6

    again = _dispatch(evidence, path, offline._synthetic_runner({}))
    assert again["refused"] is True
    assert again["cells_started"] == 0


def test_a_measured_failure_does_not_stop_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"status": "failed", "failure": {"kind": "oracle_failed"}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["complete"] is True
    assert report["cells_started"] == 6
    assert report["cells"][0]["verdict"] != "PASS"


def test_an_unknown_failure_kind_stops(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"status": "failed", "failure": {"kind": "something_new"}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["stop_reason"] == executor.STOP_UNKNOWN_FAILURE
    assert report["cells_started"] == 1


def test_the_last_cell_can_still_stop_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    last = contract.canary_cells()[-1]["cell_id"]
    spec = {last: {"overrides": {"sandbox": {"destroyed": False, "retained": True}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["cells_started"] == 6
    assert report["complete"] is False
    assert report["stop_reason"] == executor.STOP_SANDBOX_CLEANUP


def test_an_unknown_token_count_stops_instead_of_zeroing(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"metrics": {"child_completion_tokens": None}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["stop_reason"] == executor.STOP_UNKNOWN_USAGE
    assert report["cells"][0]["usage"]["child_completion_tokens"] is None


def test_a_cost_stop_ends_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY, cost_stop=1e-9, per_cell=1e-9)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner({}))
    assert report["stop_reason"] == executor.STOP_COST_REACHED
    assert report["cells_started"] == 1


# -- usage and cost -----------------------------------------------------------------------------


def test_usage_validation_rejects_what_is_not_a_counter() -> None:
    base = {
        "main_cumulative_input_tokens": 10,
        "main_completion_tokens": 5,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": 15,
    }
    assert executor.validate_usage(base)["ok"] is True
    for field, value in (
        ("main_completion_tokens", True),
        ("child_total_prompt_tokens", -1),
        ("system_total_tokens", None),
        ("main_cumulative_input_tokens", 9.5),
    ):
        patched = dict(base)
        patched[field] = value
        assert executor.validate_usage(patched)["ok"] is False, field


def test_usage_identity_is_checked_not_assumed() -> None:
    broken = {
        "main_cumulative_input_tokens": 10,
        "main_completion_tokens": 5,
        "child_total_prompt_tokens": 3,
        "child_completion_tokens": 1,
        "system_total_tokens": 15,
    }
    report = executor.validate_usage(broken)
    assert report["ok"] is False
    assert any("sum" in item for item in report["problems"])


def test_child_prompt_tokens_are_priced_as_misses() -> None:
    metrics = {
        "main_cumulative_input_tokens": 1_000_000,
        "main_cache_hit_tokens": 1_000_000,
        "main_cache_miss_tokens": 0,
        "child_total_prompt_tokens": 1_000_000,
        "main_completion_tokens": 0,
        "child_completion_tokens": 0,
    }
    prices = {
        "cache_hit_input_per_million": 0.006,
        "cache_miss_input_per_million": 0.3,
        "output_per_million": 1.2,
    }
    cost = executor.cell_cost(metrics, prices)
    assert cost == pytest.approx(0.006 + 0.3)


def test_executor_identity_covers_the_deciding_files() -> None:
    identity = _identity()
    assert identity["executor_version"] == executor.EXECUTOR_VERSION
    assert set(identity["files"]) == set(executor.EXECUTOR_FILES)
    assert identity["executor_digest"] == authority.digest_of(identity["files"])


def test_audit_stops_when_the_applied_exposure_differs() -> None:
    cell = contract.canary_cells()[0]
    session, run_dir = offline._synthetic_session(cell=cell, root=Path(  # noqa: SLF001
        __import__("tempfile").mkdtemp()
    ))
    # pretend the runtime applied the card when the arm asked for native
    session["delegation_controls"]["exposure"]["level"] = "card"
    run = executor.CellRun(
        session=session,
        run_dir=run_dir,
        fixture_root=run_dir,
        oracle={"status": "PASS", "kind": "PASS"},
    )
    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan=_plan(contract.SCOPE_CANARY),
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )
    assert executor.STOP_IDENTITY_MISMATCH in outcome["stops"]
