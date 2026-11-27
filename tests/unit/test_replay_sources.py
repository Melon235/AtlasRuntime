"""Discovery, identity and read-boundary tests for the Stage H1 replay workbench."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlasos.app.replay.sources import (
    EvidenceReader,
    EvidenceRoot,
    EvidenceTooLarge,
    PathOutsideRoot,
    ReplayConfigError,
    normalize_label,
    parse_root_specs,
    scan,
)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _analysis(cell_id: str, *, plan: str, oracle: str = "FAIL") -> dict:
    return {
        "cell_id": cell_id,
        "plan_id": plan,
        "scope": "TEST_SCOPE",
        "arm_id": "C3",
        "task_id": "demo-task",
        "repetition": 1,
        "is_benchmark_sample": False,
        "outcome": {
            "cell_id": cell_id,
            "arm_id": "C3",
            "task_id": "demo-task",
            "profile_id": "coding_worker",
            "status": "succeeded",
            "failure_kind": None,
            "agent_success": True,
            "public_test_status": "PASS",
            "public_test_runs": 1,
            "public_test_problems": [],
            "static_acceptance": "PASS",
            "oracle_passed": oracle == "PASS",
            "oracle_status": oracle,
            "goal_correctness": oracle == "PASS",
            "execution_integrity": "PASS",
            "executor_verdict": "PASS" if oracle == "PASS" else "MEASURED_FAILURE",
            "checks": [{"check": "usage_is_complete", "ok": True, "detail": "ok"}],
            "stops": [],
            "usage": {
                "main_cumulative_input_tokens": 1000,
                "main_completion_tokens": 100,
                "child_total_prompt_tokens": 500,
                "child_completion_tokens": 50,
                "system_total_tokens": 1650,
            },
            "delta_cost_usd": 0.01,
            "cost_usd": 0.01,
            "verdict": "PASS" if oracle == "PASS" else "MEASURED_FAILURE",
            "duration_s": 2.0,
            "notes": [],
        },
        "oracle": {
            "status": oracle,
            "kind": "HIDDEN_TESTS_PASSED" if oracle == "PASS" else "HIDDEN_TESTS_FAILED",
            "passed": oracle == "PASS",
            "execution_mode": "sandbox",
            "returncode": 0 if oracle == "PASS" else 1,
            "image": "atlas-uat-python:3.12",
            "source": "tests",
            "stderr_excerpt": "",
        },
        "export": {
            "patch_path": "workspace.patch",
            "patch_digest": "sha256:patch",
            "patch_changed_files": 2,
            "workspace_path": "workspace",
            "workspace_files": 3,
            "workspace_digest": "sha256:ws",
            "directory_digest": "sha256:dir",
        },
        "duration_s": 2.0,
        "notes": [],
    }


def _session() -> dict:
    return {
        "run_id": "bplus-c01",
        "mode": "coding",
        "goal": "demonstrate the replay projection",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "status": "succeeded",
        "execution_succeeded": True,
        "failure": None,
        "main": {
            "agent": "atlas-main",
            "worker_id": "w-main",
            "task_id": "main",
            "success": True,
            "stop_reason": "success",
            "turns": 3,
            "tool_calls": ["bash", "delegate_tasks"],
            "requests": 2,
            "total_prompt_tokens": 1000,
            "cache_hit_tokens": 800,
            "cache_miss_tokens": 200,
            "completion_tokens": 100,
            "peak_context_tokens": 500,
        },
        "delegation": {
            "run_id": "bplus-c01",
            "enabled": True,
            "limits": {"child_max_turns": 20, "max_parallel_children": 2},
            "batches": [
                {
                    "round": 1,
                    "route": "delegate_parallel",
                    "decision": {
                        "route": "delegate_parallel",
                        "reason_codes": ["DELEGATION_ADMITTED"],
                        "blocked_gates": [],
                        "policy_version": "m.1.0",
                        "policy_digest": "digest",
                        "children": [
                            {
                                "key": "t1",
                                "profile_id": "coding_worker",
                                "admitted": True,
                                "reason_codes": ["DELEGATION_ADMITTED"],
                            },
                            {
                                "key": "t2",
                                "profile_id": "coding_worker",
                                "admitted": False,
                                "reason_codes": ["SIDE_EFFECT_RISK"],
                            },
                        ],
                        "frozen_decision": {
                            "proposal_id": "prop-1",
                            "approved_task_ids": ["t1"],
                            "reason_codes": ["DELEGATION_ADMITTED"],
                            "evidence_refs": ["ev-1"],
                            "gates": [
                                {
                                    "gate": "GATE_0_PROPOSAL_VALIDITY",
                                    "status": "pass",
                                    "reason_codes": [],
                                }
                            ],
                        },
                    },
                    "children": [
                        {
                            "key": "t1",
                            "worker_id": "w-1",
                            "profile_id": "coding_worker",
                            "status": "completed",
                            "success": True,
                            "stop_reason": "success",
                            "turns": 4,
                            "tool_calls": 5,
                            "started_offset_ms": 10.0,
                            "finished_offset_ms": 2000.0,
                            "total_prompt_tokens": 500,
                            "completion_tokens": 50,
                            "cache_hit_tokens": 0,
                            "cache_miss_tokens": 500,
                            "ref_id": "ho-1",
                        }
                    ],
                    "totals": {"children_started": 1},
                    "batch_span_ms": 2000.0,
                    "max_observed_concurrency": 1,
                }
            ],
            "totals": {"children_started": 1},
            "started_at": "2026-11-25T11:32:36Z",
            "finished_at": "2026-11-25T11:32:38Z",
        },
        "delegation_enabled": True,
        "child_tokens": {
            "children_started": 1,
            "children_completed": 1,
            "prompt_tokens": 500,
            "total_prompt_tokens": 500,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 500,
            "completion_tokens": 50,
        },
        "metrics": {
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 1000,
            "main_context_tokens_by_request": [400, 500],
            "main_cache_hit_tokens": 800,
            "main_cache_miss_tokens": 200,
            "main_completion_tokens": 100,
            "child_total_prompt_tokens": 500,
            "child_completion_tokens": 50,
            "system_total_tokens": 1650,
            "handoff_payload_chars": 50,
            "delegation_rounds": 1,
            "max_observed_child_concurrency": 1,
            "unavailable_fields": [],
        },
        "sandbox": {
            "provider": "opensandbox",
            "patch_changed_files": 2,
            "patch_digest": "sha256:patch",
        },
        "artifacts": {"patch_path": "workspace.patch", "workspace_path": "workspace"},
        "verification": {
            "verdict": "PASS",
            "static_status": "PASS",
            "oracle_mode": "EXTERNAL_SANDBOX_ORACLE",
        },
        "public_tests": {
            "runs": [
                {
                    "call_index": 1,
                    "status": "PASS",
                    "exit_code": 0,
                    "duration_ms": 10.0,
                    "output_chars": 12,
                    "reason": "public suite",
                }
            ]
        },
        "budget": {
            "mode": "guarded",
            "limit_tokens": 10000,
            "used_tokens": 1650,
            "remaining_tokens": 8350,
            "requests_started": 2,
            "requests_completed": 2,
            "admission_rejections": 0,
        },
        "ownership": {
            "mode": "enforce",
            "workers": {"w-main": {"paths": ["src/"]}, "w-1": {"paths": ["src/a.py"]}},
            "denials": [
                {
                    "worker_id": "w-main",
                    "tool_name": "bash",
                    "target": "rm -rf /",
                    "reason_code": "OWNERSHIP_UNCLASSIFIABLE_SHELL",
                    "decision": "DENY",
                    "detail": "no analyzable write set",
                }
            ],
        },
        "tool_surface": ["bash", "delegate_tasks"],
        "execution_integrity": {"mode": "record", "records": 2},
    }


def _transcript() -> str:
    rows = [
        {
            "seq": 1,
            "ts": "2026-11-25T11:32:11Z",
            "kind": "lifecycle",
            "payload": {
                "atlas_trajectory": {
                    "event_id": "e1",
                    "run_id": "bplus-c01",
                    "event_type": "sandbox_lifecycle",
                    "source": "m5",
                    "occurred_at": None,
                    "correlation": {"worker_id": "w-main"},
                    "data": {"state": "READY", "provider_name": "opensandbox"},
                }
            },
        },
        {
            "seq": 2,
            "ts": "2026-11-25T11:32:18Z",
            "kind": "lifecycle",
            "payload": {
                "atlas_trajectory": {
                    "event_id": "e2",
                    "run_id": "bplus-c01",
                    "event_type": "action_proposed",
                    "source": "model",
                    "occurred_at": None,
                    "correlation": {"worker_id": "w-main", "call_id": "call_1", "tool_id": "bash"},
                    "data": {"call_id": "call_1", "tool_name": "bash", "tool_id": "bash"},
                }
            },
        },
        {
            "seq": 3,
            "ts": "2026-11-25T11:32:19Z",
            "kind": "lifecycle",
            "payload": {
                "atlas_trajectory": {
                    "event_id": "e3",
                    "run_id": "bplus-c01",
                    "event_type": "action_decision",
                    "source": "m3",
                    "occurred_at": None,
                    "correlation": {"worker_id": "w-main", "call_id": "call_1", "tool_id": "bash"},
                    "data": {
                        "call_id": "call_1",
                        "tool_id": "bash",
                        "phase": "execution",
                        "decision": "DENY",
                        "reason_codes": ["OWNERSHIP_UNCLASSIFIABLE_SHELL"],
                    },
                }
            },
        },
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def _seed_cell(root: Path, *, cell_id: str, plan: str = "stage-c1c-demo") -> Path:
    cell = root / "stage-c1c" / "evidence" / "cells" / "c01"
    _write(cell / "analysis.json", _analysis(cell_id, plan=plan))
    _write(
        cell / "attempt.json",
        {
            "cell_id": cell_id,
            "plan_id": plan,
            "state": "CLAIMED",
            "attempt_limit": 1,
            "claimed_at": "2026-11-25T11:32:00Z",
        },
    )
    agent = cell / "agent" / "bplus-c01"
    _write(agent / "session.json", _session())
    _write(
        agent / "workers.json",
        {
            "schema_version": 1,
            "workers": {
                "w-1": {
                    "assignment": {
                        "task": "implement the deliverable",
                        "profile_id": "coding_worker",
                    },
                    "task_id": "t1",
                    "worker_id": "w-1",
                }
            },
        },
    )
    _write(
        agent / "delegation" / "handoffs.json",
        {
            "schema_version": 1,
            "run_id": "bplus-c01",
            "refs": {
                "ho-1": {
                    "key": "t1",
                    "parent_agent": "atlas-main",
                    "parent_task_id": "main",
                    "run_id": "bplus-c01",
                    "worker_id": "w-1",
                }
            },
        },
    )
    _write(
        agent / "handoffs" / "ho-1.json",
        {
            "ref_id": "ho-1",
            "created_at": "2026-11-25T11:32:38Z",
            "length": 50,
            "digest": "digest",
            "producer_key": "delegate:w-1",
            "producer_title": "t1 (coding_worker)",
            "run_id": "bplus-c01",
        },
    )
    (agent / "trajectory" / "bplus-c01").mkdir(parents=True, exist_ok=True)
    (agent / "trajectory" / "bplus-c01" / "transcript.jsonl").write_text(
        _transcript(), encoding="utf-8"
    )
    (agent / "workspace.patch").write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n",
        encoding="utf-8",
    )
    return cell


def _seed_matrix(root: Path, *, cell_id: str, plan: str = "stage-c1c-demo") -> None:
    _write(
        root / "stage-c1c" / "evidence" / "matrix-report.json",
        {
            "stage": "B+",
            "mode": "live",
            "plan_id": plan,
            "scope": "TEST_SCOPE",
            "started_at": "2026-11-25T11:32:00Z",
            "finished_at": "2026-11-25T11:33:00Z",
            "cells_planned": 1,
            "cells_started": 1,
            "cells_passed": 0,
            "complete": True,
            "usage_completeness": "COMPLETE",
            "total_tokens": 1650,
            "stop_reason": "NONE",
            "cells": [
                {
                    "cell_id": cell_id,
                    "arm_id": "C3",
                    "status": "succeeded",
                    "agent_success": True,
                    "oracle_status": "FAIL",
                    "oracle_passed": False,
                    "goal_correctness": False,
                    "execution_integrity": "PASS",
                    "executor_verdict": "MEASURED_FAILURE",
                    "verdict": "MEASURED_FAILURE",
                    "cost_usd": 0.01,
                    "duration_s": 2.0,
                }
            ],
        },
    )


# --------------------------------------------------------------------- discovery


def test_discovers_one_run_from_session_analysis_and_matrix(tmp_path: Path) -> None:
    _seed_cell(tmp_path, cell_id="c1c.demo.C3.r1")
    _seed_matrix(tmp_path, cell_id="c1c.demo.C3.r1")

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 1
    run = result.runs[0]
    assert run.kind == "unified_cell"
    assert run.cell_id == "c1c.demo.C3.r1"
    assert run.run_key.startswith("runs.unified_cell.c1c.demo.c3.r1.")
    assert run.has("analysis") and run.has("session") and run.has("transcript")
    assert run.matrix_row is not None, "the matrix row is attached, not a second run"
    assert result.stats["matrix_rows_matched"] == 1
    assert result.stats["runs"] == 1


def test_duplicate_cell_identity_is_one_run_and_copies_are_reported(tmp_path: Path) -> None:
    cell_id = "c1c.demo.C3.r1"
    _seed_cell(tmp_path, cell_id=cell_id)
    copy_root = tmp_path / "stage-c1c-copy"
    _seed_cell(copy_root, cell_id=cell_id)

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 1, "one logical cell must not appear as two runs"
    assert result.stats["duplicate_cells"] == 1
    assert result.duplicates and result.duplicates[0]["cell_id"] == cell_id
    assert result.duplicates[0]["copy"] != result.duplicates[0]["canonical"]


def test_same_cell_id_in_another_plan_keeps_both_runs(tmp_path: Path) -> None:
    cell_id = "c1c.demo.C3.r1"
    _seed_cell(tmp_path / "first", cell_id=cell_id, plan="plan-a")
    _seed_cell(tmp_path / "second", cell_id=cell_id, plan="plan-b")

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 2, "a different experiment must not be overwritten"
    keys = {run.run_key for run in result.runs}
    assert len(keys) == 2


def test_analysis_without_execution_evidence_has_its_own_kind(tmp_path: Path) -> None:
    cell = tmp_path / "stage-f-preflight" / "dispatch-b1" / "cells" / "c01"
    _write(cell / "analysis.json", _analysis("f.demo.A0.r1", plan="stage-f-demo"))
    _write(
        cell / "attempt.json",
        {
            "cell_id": "f.demo.A0.r1",
            "plan_id": "stage-f-demo",
            "state": "CLAIMED",
            "attempt_limit": 1,
            "claimed_at": "2026-11-26T00:00:00Z",
        },
    )

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 1
    assert result.runs[0].kind == "cell_analysis"


def test_unsupported_and_malformed_evidence_is_reported_not_fabricated(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "analysis.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "orphan").mkdir()
    _write(tmp_path / "orphan" / "attempt.json", {"cell_id": "orphan", "state": "CLAIMED"})
    (tmp_path / "stray" / "nested").mkdir(parents=True)
    (tmp_path / "stray" / "nested" / "transcript.jsonl").write_text("", encoding="utf-8")

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert result.runs == []
    reasons = {problem["reason"] for problem in result.problems}
    assert "MALFORMED_EVIDENCE" in reasons
    assert "UNSUPPORTED_LAYOUT" in reasons
    assert "INCOMPLETE_EVIDENCE" in reasons


def test_scratch_directories_are_pruned_and_disclosed(tmp_path: Path) -> None:
    _seed_cell(tmp_path, cell_id="c1c.demo.C3.r1")
    _seed_cell(tmp_path / "tmp-should-not-be-read", cell_id="c1c.demo.C3.r1")

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert result.stats["pruned_scratch_dirs"] == 1
    assert any(problem["reason"] == "SCRATCH_DIRECTORIES_PRUNED" for problem in result.problems)


# ------------------------------------------------------------------ read boundary


def test_path_traversal_and_absolute_paths_are_rejected(tmp_path: Path) -> None:
    reader = EvidenceReader(EvidenceRoot(label="runs", path=tmp_path))
    for candidate in ("../escape.json", "a/../../escape.json", "/etc/passwd", "C:/secret"):
        with pytest.raises(PathOutsideRoot):
            reader.resolve(candidate)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "replay-outside"
    outside.mkdir(exist_ok=True)
    _write(outside / "secret.json", {"secret": "value"})
    root = tmp_path / "root"
    root.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating the test symlink")
    reader = EvidenceReader(EvidenceRoot(label="runs", path=root))
    with pytest.raises(PathOutsideRoot):
        reader.resolve("linked/secret.json")


def test_oversized_json_is_refused(tmp_path: Path) -> None:
    from atlasos.app.replay import sources

    big = tmp_path / "big.json"
    big.write_text('{"a": "' + "x" * 1024 + '"}', encoding="utf-8")
    reader = EvidenceReader(EvidenceRoot(label="runs", path=tmp_path))
    original = sources.MAX_JSON_BYTES
    sources.MAX_JSON_BYTES = 64
    try:
        with pytest.raises(EvidenceTooLarge):
            reader.read_json("big.json")
    finally:
        sources.MAX_JSON_BYTES = original


def test_private_payload_corpus_is_never_walked(tmp_path: Path) -> None:
    secret = "sk-live-abcdefghijklmnop"
    agent = tmp_path / "stage-c1c" / "evidence" / "cells" / "c01" / "agent" / "bplus-c01"
    _write(agent / "session.json", _session())
    (agent / "private").mkdir(parents=True, exist_ok=True)
    (agent / "private" / "tool_payloads.jsonl").write_text(
        json.dumps({"payload": secret}) + "\n", encoding="utf-8"
    )

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 1
    for path in result.runs[0].files.values():
        for entry in path:
            assert "tool_payloads" not in entry


def test_root_specs_and_labels(tmp_path: Path) -> None:
    root = tmp_path / "atlas-runs"
    root.mkdir()
    roots = parse_root_specs([f"alias={root}"], base=tmp_path)
    assert len(roots) == 1
    assert roots[0].label == "alias"
    assert normalize_label("Stage C1C / Headroom") == "stage-c1c-headroom"

    with pytest.raises(ReplayConfigError):
        parse_root_specs([str(tmp_path / "missing")])


def test_environment_root_list_is_used_when_no_flag_is_given(tmp_path: Path) -> None:
    import os

    root = tmp_path / "runs"
    root.mkdir()
    roots = parse_root_specs([], env_value=f"{root}{os.pathsep}{root}")
    assert [item.path for item in roots] == [root.resolve()], "duplicates collapse to one root"


# ------------------------------------------------------------------ execution identity (H2 A1)


def _seed_cell_evidence(cell: Path, *, cell_id: str, provider: str, digest: str, plan: str) -> None:
    """A cell evidence record with the provenance the Stage F adapter writes."""

    _write(
        cell / "evidence.json",
        {
            "schema_version": "stage-f-cell-evidence/1.2",
            "cell_id": cell_id,
            "plan_id": plan,
            "execution_mode": "atlas_direct",
            "is_benchmark_sample": False,
            "provenance": {
                "source": "main-driven session.json",
                "source_digest": digest,
                "provider": provider,
                "model": provider,
            },
            "correctness": {"agent_success": True, "delivered_success": False},
        },
    )


def test_scripted_and_live_records_of_the_same_cell_stay_separate(tmp_path: Path) -> None:
    """Same plan and cell, different executions: a scripted proof is not a live run."""

    cell_id = "f.f-p1-connector-migration.A1.r1"
    plan = "stage-f-p1-compat-smoke"
    live = _seed_cell(tmp_path / "live", cell_id=cell_id, plan=plan)
    _seed_cell_evidence(live, cell_id=cell_id, plan=plan, provider="deepseek", digest="sha256:live")
    scripted = _seed_cell(tmp_path / "archive", cell_id=cell_id, plan=plan)
    _seed_cell_evidence(
        scripted, cell_id=cell_id, plan=plan, provider="scripted", digest="sha256:scripted"
    )
    session = json.loads((scripted / "agent" / "bplus-c01" / "session.json").read_text("utf-8"))
    session["run_id"] = "scripted-c01"
    session["provider"] = "scripted"
    _write(scripted / "agent" / "bplus-c01" / "session.json", session)

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 2, "a scripted proof must not be merged into a live run"
    bands = sorted(run.execution["band"] for run in result.runs)
    assert bands == ["LIVE_MODEL", "SCRIPTED"]
    assert result.stats["duplicate_cells"] == 0


def test_two_executions_of_one_cell_are_not_collapsed(tmp_path: Path) -> None:
    """Equal plan and cell but different sessions: two runs, not one."""

    cell_id = "f.f-c1-config-lifecycle.A1.r1"
    first = _seed_cell(tmp_path / "b1", cell_id=cell_id, plan="plan")
    second = _seed_cell(tmp_path / "b2", cell_id=cell_id, plan="plan")
    for cell, run_id in ((first, "bplus-b1"), (second, "bplus-b2")):
        session = json.loads((cell / "agent" / "bplus-c01" / "session.json").read_text("utf-8"))
        session["run_id"] = run_id
        _write(cell / "agent" / "bplus-c01" / "session.json", session)

    result = scan([EvidenceRoot(label="runs", path=tmp_path)])

    assert len(result.runs) == 2
    assert sorted(run.execution["execution_id"] for run in result.runs) == ["bplus-b1", "bplus-b2"]


def test_rehearsal_copies_collapse_only_when_they_claim_the_same_verdict(
    tmp_path: Path,
) -> None:
    """A rehearsal has no execution to identify it; its claimed verdict is what can be compared."""

    cell_id = "f.demo.A0.r1"
    for index, root in enumerate(("one", "two")):
        cell = tmp_path / root / "dispatch" / "cells" / "c01"
        payload = _analysis(cell_id, plan="plan-a")
        payload["outcome"]["duration_s"] = float(index + 1)
        payload["outcome"]["cost_usd"] = 0.1 * (index + 1)
        _write(cell / "analysis.json", payload)

    same = scan([EvidenceRoot(label="runs", path=tmp_path)])
    assert len(same.runs) == 1, "two copies of one rehearsal are one row"
    assert same.duplicates and same.duplicates[0]["proof"] == "verdict_digest"

    changed = _analysis(cell_id, plan="plan-a", oracle="PASS")
    _write(tmp_path / "two" / "dispatch" / "cells" / "c01" / "analysis.json", changed)
    differing = scan([EvidenceRoot(label="runs", path=tmp_path)])
    assert len(differing.runs) == 2, "a rehearsal that claims another verdict stays its own row"
    assert differing.stats["duplicate_cells"] == 0
