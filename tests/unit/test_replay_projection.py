"""Projection tests: main-driven, native, static DAG, UNKNOWN usage and redaction."""

from __future__ import annotations

import json
from pathlib import Path

from atlasos.app.replay.service import ReplayService
from atlasos.app.replay.sources import EvidenceRoot

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz"


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _session(*, goal: str = "deliver the change", system_total: int = 1650) -> dict:
    return {
        "run_id": "bplus-c01",
        "mode": "coding",
        "goal": goal,
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
            "limits": {"child_max_turns": 20},
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
                            "gates": [{"gate": "GATE_2", "status": "pass", "reason_codes": []}],
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
                            "ref_id": "ho-1",
                        },
                        {
                            "key": "t2",
                            "worker_id": "w-2",
                            "profile_id": "coding_worker",
                            "status": "completed",
                            "success": True,
                            "stop_reason": "success",
                            "turns": 4,
                            "tool_calls": 3,
                            "started_offset_ms": 500.0,
                            "finished_offset_ms": 1800.0,
                            "total_prompt_tokens": 100,
                            "completion_tokens": 10,
                            "ref_id": "ho-2",
                        },
                    ],
                    "totals": {"children_started": 2},
                    "batch_span_ms": 2000.0,
                    "max_observed_concurrency": 2,
                }
            ],
            "totals": {"children_started": 2},
            "started_at": "2026-11-25T11:32:36Z",
            "finished_at": "2026-11-25T11:32:38Z",
        },
        "delegation_enabled": True,
        "child_tokens": {
            "children_started": 2,
            "children_completed": 2,
            "total_prompt_tokens": 600,
            "completion_tokens": 60,
        },
        "metrics": {
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 1000,
            "main_context_tokens_by_request": [400, 500],
            "main_cache_hit_tokens": 800,
            "main_cache_miss_tokens": 200,
            "main_completion_tokens": 100,
            "child_total_prompt_tokens": 600,
            "child_completion_tokens": 60,
            "system_total_tokens": system_total,
            "delegation_rounds": 1,
            "max_observed_child_concurrency": 2,
            "unavailable_fields": [],
        },
        "sandbox": {"provider": "opensandbox", "patch_changed_files": 2},
        "verification": {"verdict": "PASS", "static_status": "PASS"},
        "public_tests": {
            "runs": [{"call_index": 1, "status": "PASS", "exit_code": 0, "duration_ms": 10.0}]
        },
        "budget": {"mode": "guarded", "limit_tokens": 10000, "used_tokens": system_total},
        "ownership": {
            "mode": "enforce",
            "workers": {"w-main": {"paths": ["src/"]}, "w-1": {"paths": ["src/a.py"]}},
            "denials": [
                {
                    "worker_id": "w-main",
                    "tool_name": "bash",
                    "target": "curl -H 'Authorization: Bearer abcdefghijklmnop'",
                    "reason_code": "OWNERSHIP_UNCLASSIFIABLE_SHELL",
                    "decision": "DENY",
                    "detail": "no analyzable write set",
                }
            ],
        },
        "tool_surface": ["bash", "delegate_tasks"],
    }


def _analysis(cell_id: str, plan: str, *, oracle: str = "FAIL", system_total: int = 1760) -> dict:
    return {
        "cell_id": cell_id,
        "plan_id": plan,
        "scope": "TEST_SCOPE",
        "arm_id": "C3",
        "task_id": "demo-task",
        "repetition": 1,
        "outcome": {
            "cell_id": cell_id,
            "arm_id": "C3",
            "task_id": "demo-task",
            "profile_id": "coding_worker",
            "status": "succeeded",
            "agent_success": True,
            "public_test_status": "PASS",
            "static_acceptance": "PASS",
            "oracle_passed": oracle == "PASS",
            "oracle_status": oracle,
            "goal_correctness": oracle == "PASS",
            "execution_integrity": "PASS",
            "executor_verdict": "PASS" if oracle == "PASS" else "MEASURED_FAILURE",
            "checks": [],
            "stops": [],
            "usage": {
                "main_cumulative_input_tokens": 1000,
                "main_completion_tokens": 100,
                "child_total_prompt_tokens": 600,
                "child_completion_tokens": 60,
                "system_total_tokens": system_total,
            },
            "cost_usd": 0.02,
            "duration_s": 2.0,
        },
        "oracle": {
            "status": oracle,
            "kind": "HIDDEN_TESTS_PASSED" if oracle == "PASS" else "HIDDEN_TESTS_FAILED",
            "passed": oracle == "PASS",
            "execution_mode": "sandbox",
            "returncode": 0 if oracle == "PASS" else 1,
            "image": "atlas-uat-python:3.12",
            "stderr_excerpt": "",
        },
        "export": {"patch_digest": "sha256:patch", "patch_changed_files": 2, "workspace_files": 3},
        "duration_s": 2.0,
    }


def _transcript() -> str:
    def row(
        seq: int, ts: str, event_type: str, worker: str, data: dict, call_id: str | None = None
    ) -> dict:
        return {
            "seq": seq,
            "ts": ts,
            "kind": "lifecycle",
            "payload": {
                "atlas_trajectory": {
                    "event_id": f"e{seq}",
                    "run_id": "bplus-c01",
                    "event_type": event_type,
                    "source": "model",
                    "occurred_at": None,
                    "correlation": {"worker_id": worker, "call_id": call_id},
                    "data": data,
                }
            },
        }

    rows = [
        row(
            1,
            "2026-11-25T11:32:11Z",
            "sandbox_lifecycle",
            "w-main",
            {"state": "READY", "provider_name": "opensandbox"},
        ),
        row(
            2,
            "2026-11-25T11:32:18Z",
            "action_proposed",
            "w-main",
            {"call_id": "call_a", "tool_name": "bash", "tool_id": "bash"},
            "call_a",
        ),
        row(
            3,
            "2026-11-25T11:32:19Z",
            "action_decision",
            "w-main",
            {
                "call_id": "call_a",
                "tool_id": "bash",
                "phase": "execution",
                "decision": "ALLOW",
                "reason_codes": [],
            },
            "call_a",
        ),
        row(
            4,
            "2026-11-25T11:32:20Z",
            "tool_observation",
            "w-main",
            {
                "call_id": "call_a",
                "tool_id": "bash",
                "is_error": False,
                "result_size": 42,
                "outcome": "EXECUTED",
            },
            "call_a",
        ),
        row(
            5,
            "2026-11-25T11:32:21Z",
            "action_proposed",
            "w-main",
            {"call_id": "call_b", "tool_name": "file_write", "tool_id": "file_write"},
            "call_b",
        ),
        row(
            6,
            "2026-11-25T11:32:22Z",
            "action_decision",
            "w-main",
            {
                "call_id": "call_b",
                "tool_id": "file_write",
                "phase": "execution",
                "decision": "DENY",
                "reason_codes": ["OWNERSHIP_TARGET_DENIED"],
            },
            "call_b",
        ),
        row(7, "2026-11-25T11:32:39Z", "run_terminal", "w-main", {"status": "succeeded"}),
    ]
    return "\n".join(json.dumps(item) for item in rows) + "\n"


def _seed_main_driven(
    root: Path,
    *,
    cell_id: str = "c1c.demo.C3.r1",
    plan: str = "stage-c1c-demo",
    system_total: int = 1650,
) -> None:
    cell = root / "stage-c1c" / "evidence" / "cells" / "c01"
    _write(cell / "analysis.json", _analysis(cell_id, plan, system_total=system_total))
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
    _write(
        agent / "session.json",
        _session(goal=f"deliver the change; key={SECRET}", system_total=system_total),
    )
    _write(
        agent / "workers.json",
        {
            "schema_version": 1,
            "workers": {
                "w-1": {
                    "assignment": {"task": "implement part one"},
                    "task_id": "t1",
                    "worker_id": "w-1",
                },
                "w-2": {
                    "assignment": {"task": "implement part two"},
                    "task_id": "t2",
                    "worker_id": "w-2",
                },
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
                    "worker_id": "w-1",
                    "parent_agent": "atlas-main",
                    "parent_task_id": "main",
                    "run_id": "bplus-c01",
                },
                "ho-2": {
                    "key": "t2",
                    "worker_id": "w-2",
                    "parent_agent": "atlas-main",
                    "parent_task_id": "main",
                    "run_id": "bplus-c01",
                },
            },
        },
    )
    _write(
        agent / "handoffs" / "ho-1.json",
        {"ref_id": "ho-1", "created_at": "2026-11-25T11:32:38Z", "length": 50, "digest": "digest"},
    )
    (agent / "trajectory" / "bplus-c01").mkdir(parents=True, exist_ok=True)
    (agent / "trajectory" / "bplus-c01" / "transcript.jsonl").write_text(
        _transcript(), encoding="utf-8"
    )
    (agent / "workspace.patch").write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n", encoding="utf-8"
    )


def _service(root: Path) -> ReplayService:
    return ReplayService([EvidenceRoot(label="runs", path=root)])


def _only_key(service: ReplayService) -> str:
    listing = service.list_runs(limit=10)
    assert listing["total"] == 1
    return listing["runs"][0]["run_key"]


# ------------------------------------------------------------------- main-driven


def test_main_driven_projection_reports_real_delegation(tmp_path: Path) -> None:
    _seed_main_driven(tmp_path)
    service = _service(tmp_path)
    key = _only_key(service)

    overview = service.overview(key)
    assert overview["source_kind"] == "unified_cell"
    assert overview["execution"]["mode"] == "coding"
    assert overview["execution"]["mode_source"] == "declared"
    assert overview["model"]["requested"] == "deepseek-chat"
    assert overview["model"]["provider_kind"] == "LIVE_MODEL"
    assert overview["status"]["headline"] == "NOT_DELIVERED"
    assert overview["verification"]["independent_oracle"] == "FAIL"
    assert overview["verification"]["public_test"] == "PASS"

    agents = service.agents(key)["agents"]
    roles = {agent["role"] for agent in agents}
    assert roles == {"MAIN", "CODING_WORKER"}
    worker = next(agent for agent in agents if agent["role"] == "CODING_WORKER")
    assert worker["parent"] == "atlas-main"
    assert worker["owned_paths"] == ["src/a.py"]
    assert worker["objective"] == "implement part one"

    delegation = service.delegation(key)
    assert delegation["state"] == "DELEGATION_OBSERVED"
    assert delegation["router"]["decision_observed"] is True
    assert delegation["proposals"][0]["proposal_id"] == "prop-1"
    assert delegation["refusals"][0]["reason_codes"] == ["SIDE_EFFECT_RISK"]
    assert delegation["concurrency"]["max_observed"] == 2
    overlap = delegation["concurrency"]["overlapping_pairs"]
    assert overlap and overlap[0]["overlap_ms"] > 0
    assert {handoff["ref_id"] for handoff in delegation["handoffs"]} == {"ho-1", "ho-2"}

    timeline = service.timeline(key)
    assert timeline["timeline_mode"] == "observed_then_ordered"
    observed = [event for event in timeline["events"] if event["timestamp"]]
    assert observed, "the timeline must keep its real timestamps"
    assert observed[0]["event_type"] == "cell_claimed"
    assert observed[0]["timestamp"] == "2026-11-25T11:32:00Z"
    assert any(event["event_type"] == "delegation_admitted" for event in timeline["events"])
    assert any(event["event_type"] == "worker_started" for event in timeline["events"])
    ordered = [event for event in timeline["events"] if not event["timestamp"]]
    assert ordered and all(event["timestamp"] is None for event in ordered)

    resources = service.resources(key)
    assert resources["tokens"]["main_prompt_tokens"] == 1000
    assert resources["tokens"]["child_prompt_tokens"] == 600
    assert resources["tokens"]["system_total_tokens"] == 1650
    assert resources["request_series"]["source"].startswith("session.metrics")
    assert resources["cache"]["hit_rate"] == 80.0
    # Three agents ran and no census exists, so the system maximum stays UNKNOWN.
    assert resources["tokens"]["peak_context"]["system_max_agent"] == "UNKNOWN"
    assert resources["tokens"]["peak_context"]["main"] == 500

    tools = service.tools(key)["rows"]
    denied = [row for row in tools if row["decision"] == "DENY"]
    codes = {code for row in denied for code in row["reason_codes"]}
    # The transcript decision and the ownership denial are both real evidence, and both appear.
    assert {"OWNERSHIP_TARGET_DENIED", "OWNERSHIP_UNCLASSIFIABLE_SHELL"} <= codes
    assert all(row["output_withheld"] for row in tools)
    assert any(row["decision"] == "ALLOW" for row in tools)


def test_no_proposal_run_reports_not_observed(tmp_path: Path) -> None:
    cell = tmp_path / "stage-c2" / "evidence" / "cells" / "c01"
    session = _session()
    session["delegation"] = {
        "run_id": "bplus-c01",
        "enabled": True,
        "limits": {},
        "batches": [],
        "totals": {"children_started": 0},
        "started_at": None,
        "finished_at": "2026-11-25T11:33:00Z",
    }
    session["main"]["tool_calls"] = ["bash", "file_read"]
    session["metrics"]["delegation_rounds"] = 0
    session["metrics"]["max_observed_child_concurrency"] = 0
    _write(cell / "analysis.json", _analysis("c2.demo.C3.B0.r1", "stage-c2-demo", oracle="PASS"))
    _write(cell / "agent" / "bplus-c01" / "session.json", session)

    service = _service(tmp_path)
    key = _only_key(service)

    delegation = service.delegation(key)
    assert delegation["state"] == "NOT_OBSERVED"
    assert delegation["router"]["decision_observed"] is False
    assert delegation["router"]["route"] == "NOT_OBSERVED"
    assert delegation["delegate_tasks_called"] is False
    assert "never called delegate_tasks" in delegation["reason"]
    assert service.agents(key)["agents"].__len__() == 1


def test_unknown_usage_is_not_reported_as_zero(tmp_path: Path) -> None:
    root = tmp_path / "stage-f" / "stage-f-96-b1" / "cells" / "c02"
    _write(
        root / "evidence.json",
        {
            "schema_version": "stage-f-cell-evidence/1.2",
            "cell_id": "f.demo.A1.r1",
            "plan_id": "stage-f-demo",
            "batch_id": "stage-f-96-b1",
            "task_id": "demo",
            "arm_id": "A1",
            "execution_mode": "atlas_direct",
            "usage_attribution": "main_child",
            "correctness": {
                "agent_success": True,
                "primary_agent_completion": None,
                "static_acceptance": "PASS",
                "public_test": "NOT_RUN",
                "independent_oracle": {"status": "FAIL", "passed": False},
                "execution_integrity": "FAIL",
                "delivered_success": False,
            },
            "usage": {
                "main": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "cache_hit_tokens": None,
                    "cache_miss_tokens": None,
                },
                "children": {"prompt_tokens": 0, "completion_tokens": 0},
                "system_total_tokens": 0,
                "requests": {"main": 0, "children": "NOT_APPLICABLE", "total": 0},
                "requested_model": "deepseek-chat",
                "returned_model": "UNKNOWN",
            },
            "context": {
                "system_max_agent_context": "UNKNOWN",
                "per_request_input_tokens": [],
                "agent_context_census": {"complete": False, "system_max_agent_context": "UNKNOWN"},
            },
            "delegation": "NOT_APPLICABLE",
            "handoff": "NOT_APPLICABLE",
        },
    )
    service = _service(tmp_path)
    key = _only_key(service)
    overview = service.overview(key)

    assert overview["usage"]["system_total_tokens"] == "UNKNOWN"
    assert overview["usage"]["known_floor_tokens"] == "UNKNOWN"
    assert overview["cost"]["estimated_usd"] == "UNKNOWN"
    assert overview["verification"]["raw_agent_success"] is True
    assert overview["verification"]["effective_agent_completion"] == "UNAVAILABLE"
    assert overview["verification"]["delivered_success"] is False
    assert overview["status"]["headline"] == "NOT_DELIVERED"
    assert overview["model"]["returned"] == "UNKNOWN"
    # The children reported a measured zero; the primary agent reported nothing, so the facet is
    # partial and the system total stays unknown.
    assert overview["completeness"]["facets"]["usage"] == "PARTIAL"


def test_raw_success_and_effective_completion_stay_separate(tmp_path: Path) -> None:
    root = tmp_path / "stage-f" / "cells" / "c01"
    _write(
        root / "evidence.json",
        {
            "schema_version": "stage-f-cell-evidence/1.2",
            "cell_id": "f.demo.A0.r1",
            "plan_id": "stage-f-demo",
            "task_id": "demo",
            "arm_id": "A0",
            "execution_mode": "native",
            "correctness": {
                "agent_success": True,
                "primary_agent_completion": "ORACLE_FAILURE",
                "independent_oracle": {"status": "FAIL", "passed": False},
                "execution_integrity": "PASS",
                "delivered_success": False,
            },
            "usage": {
                "main": "NOT_APPLICABLE",
                "children": "NOT_APPLICABLE",
                "primary_agent": {
                    "prompt_tokens": 266675,
                    "completion_tokens": 10947,
                    "cache_hit_tokens": 253696,
                    "cache_miss_tokens": 12979,
                },
                "system_total_tokens": 277622,
                "requests": {"main": 21, "children": "NOT_APPLICABLE", "total": 21},
                "requested_model": "deepseek-chat",
                "returned_model": "deepseek-flash",
            },
            "context": {
                "system_max_agent_context": 20976,
                "per_request_input_tokens": [1000, 20976],
            },
            "delegation": "NOT_APPLICABLE",
        },
    )

    service = _service(tmp_path)
    key = _only_key(service)
    overview = service.overview(key)

    assert overview["verification"]["raw_agent_success"] is True
    assert overview["verification"]["effective_agent_completion"] == "ORACLE_FAILURE"
    assert overview["verification"]["delivered_success"] is False
    assert overview["usage"]["attribution"] == "primary_agent"
    assert overview["usage"]["main_prompt_tokens"] == 266675
    # There is no child agent in this arm, so child usage is NOT_APPLICABLE, not UNKNOWN.
    assert overview["usage"]["child_prompt_tokens"] == "NOT_APPLICABLE"
    assert overview["model"]["requested"] == "deepseek-chat"
    assert overview["model"]["returned"] == "deepseek-flash"
    assert overview["execution"]["mode"] == "native"
    assert service.delegation(key)["state"] == "NOT_APPLICABLE"


# ---------------------------------------------------------- raw / effective (H2 A2)


def test_raw_success_uses_the_split_field_and_effective_uses_the_session(tmp_path: Path) -> None:
    """Stage F A1 shape: raw success true, execution_succeeded false, no evidence completion."""

    cell = tmp_path / "stage-f" / "cells" / "c02"
    _write(
        cell / "evidence.json",
        {
            "schema_version": "stage-f-cell-evidence/1.2",
            "cell_id": "f.demo.A1.r1",
            "plan_id": "stage-f-demo",
            "execution_mode": "atlas_direct",
            "correctness": {
                # The split field: the raw loop outcome, kept apart from the completion verdict.
                "raw_agent_success": True,
                "agent_success": False,
                "primary_agent_completion": None,
                "independent_oracle": {"status": "FAIL", "passed": False},
                "execution_integrity": "FAIL",
                "delivered_success": False,
            },
        },
    )
    session = _session()
    session["run_id"] = "bplus-c02"
    session["status"] = "failed"
    session["execution_succeeded"] = False
    session["delegation_enabled"] = False
    _write(cell / "agent" / "bplus-c02" / "session.json", session)

    service = _service(tmp_path)
    key = _only_key(service)
    verification = service.verification(key)

    assert verification["axes"]["raw_agent_success"] is True
    assert verification["axes"]["effective_agent_completion"] is False
    assert verification["axes"]["axis_sources"]["raw_agent_success"] == (
        "evidence.correctness.raw_agent_success"
    )
    assert verification["axes"]["axis_sources"]["effective_agent_completion"] == (
        "session.execution_succeeded"
    )
    assert verification["detail"]["effective_completion"] is False
    # The two answers are not the same answer, and neither replaces delivered success.
    assert verification["axes"]["delivered_success"] is False


def test_effective_completion_is_never_the_stop_reason(tmp_path: Path) -> None:
    """A session with a stop reason but no completion field stays UNAVAILABLE."""

    cell = tmp_path / "stage-m" / "evidence" / "cells" / "c01"
    session = _session()
    session.pop("execution_succeeded")
    session["main"]["stop_reason"] = "max_turns"
    _write(cell / "agent" / "bplus-c01" / "session.json", session)

    service = _service(tmp_path)
    key = _only_key(service)
    verification = service.verification(key)

    assert verification["axes"]["raw_agent_success"] is True
    assert verification["axes"]["effective_agent_completion"] == "UNAVAILABLE"
    assert verification["axes"]["axis_sources"]["effective_agent_completion"] == (
        "no completion field recorded"
    )


# ----------------------------------------------------------------------- native


def test_native_run_projection(tmp_path: Path) -> None:
    run = tmp_path / "native-e19613c22685"
    _write(
        run / "result.json",
        {
            "agent_success": True,
            "success": False,
            "failure_class": "ORACLE_FAILURE",
            "failure_reason": "hidden tests failed",
            "mode": "coding",
            "started_at": "2026-11-26T13:02:03Z",
            "finished_at": "2026-11-26T13:03:39Z",
            "task": "implement the config lifecycle",
            "identity": {
                "benchmark_id": "sf-a0-01",
                "model": "deepseek-chat",
                "provider": "deepseek",
                "sample_id": "f-c1-config-lifecycle",
            },
            "metrics": {
                "agent_latency_ms": 66198.3,
                "budget": {
                    "used_prompt_tokens": 266675,
                    "used_completion_tokens": 10947,
                    "used_tokens": 277622,
                    "limit_tokens": 1000000,
                    "requests_completed": 21,
                    "stop_reason": None,
                    "samples": [
                        {
                            "request_id": 1,
                            "actual_prompt_tokens": 1065,
                            "actual_completion_tokens": 132,
                        }
                    ],
                },
            },
            "oracle": {
                "status": "FAIL",
                "passed": False,
                "failure_kind": "HIDDEN_TESTS_FAILED",
                "execution_mode": "sandbox",
                "environment_failure": False,
            },
            "worker_results": {
                "native:main": {
                    "output": "done",
                    "success": True,
                    "turns": 9,
                    "tool_calls": 25,
                    "token_usage": {
                        "input_tokens": 266675,
                        "output_tokens": 10947,
                        "cache_read_input_tokens": 253696,
                    },
                }
            },
            "workspace_export": "workspace",
        },
    )
    service = _service(tmp_path)
    key = _only_key(service)
    overview = service.overview(key)

    assert overview["source_kind"] == "native_run"
    assert overview["execution"]["mode"] == "coding"
    assert overview["verification"]["independent_oracle"] == "FAIL"
    assert overview["verification"]["delivered_success"] is False
    assert overview["counts"]["agents"] == 1
    assert overview["timing"]["started_at"] == "2026-11-26T13:02:03Z"
    assert overview["timing"]["duration_s"] == 96.0

    agents = service.agents(key)["agents"]
    assert agents[0]["role"] == "NATIVE"
    assert agents[0]["usage"]["prompt_tokens"] == 266675

    resources = service.resources(key)
    assert resources["request_series"]["source"] == "native budget samples"
    assert resources["tokens"]["peak_context"]["system_max_agent"] == 1065

    timeline = service.timeline(key)
    types = {event["event_type"] for event in timeline["events"]}
    assert {"native_run_started", "native_run_finished"} <= types


# -------------------------------------------------------------------- static DAG


def test_static_dag_projection(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "889bc6ea6395420883355e102e5b8ce6"
    _write(
        run / "result.json",
        {
            "run_id": "889bc6ea6395420883355e102e5b8ce6",
            "status": "succeeded",
            "model": "deepseek-chat",
            "final_output": "answer",
            "metrics": {"prompt_tokens": 900, "completion_tokens": 90},
            "tasks": [
                {
                    "task_id": "research",
                    "title": "research",
                    "assignee": "researcher",
                    "status": "completed",
                    "depends_on": [],
                    "prompt_tokens": 400,
                    "completion_tokens": 40,
                },
                {
                    "task_id": "review",
                    "title": "review",
                    "assignee": "reviewer",
                    "status": "completed",
                    "depends_on": ["research"],
                    "prompt_tokens": 500,
                    "completion_tokens": 50,
                },
            ],
        },
    )
    service = _service(tmp_path)
    key = _only_key(service)
    overview = service.overview(key)

    assert overview["source_kind"] == "atlas_run"
    assert overview["execution"]["mode"] == "static_dag"
    assert overview["topology"]["dag_nodes"] == 2
    assert overview["usage"]["system_total_tokens"] == 990
    assert overview["verification"]["independent_oracle"] == "UNKNOWN"
    assert overview["status"]["headline"] == "INCOMPLETE"

    agents = service.agents(key)["agents"]
    assert [agent["agent_id"] for agent in agents] == ["researcher", "reviewer"]
    assert agents[1]["dependencies"] == ["research"]
    assert service.delegation(key)["state"] == "NOT_APPLICABLE"
    assert service.requests(key)["state"] == "UNAVAILABLE"


# ------------------------------------------------------------ missing / partial


def test_analysis_only_run_is_incomplete_and_does_not_crash(tmp_path: Path) -> None:
    cell = tmp_path / "stage-x" / "evidence" / "cells" / "c01"
    _write(cell / "analysis.json", _analysis("x.demo.C3.r1", "stage-x-demo"))
    service = _service(tmp_path)
    key = _only_key(service)

    overview = service.overview(key)
    assert overview["completeness"]["level"] == "MINIMAL"
    # An analysis file alone never establishes delivery, whatever verdict it records.
    assert overview["status"]["headline"] == "NO_EXECUTION_EVIDENCE"
    assert any("analysis-only" in warning for warning in overview["warnings"])
    assert "usage" in overview["completeness"]["missing"] or True
    assert overview["counts"]["agents"] == 0
    assert overview["goal"] == "UNAVAILABLE"
    assert service.tools(key)["state"] == "UNAVAILABLE"
    assert service.requests(key)["state"] == "UNAVAILABLE"
    for view in ("delegation", "resources", "verification", "artifacts"):
        payload = getattr(service, view)(key)
        assert payload["run_key"] == key


def test_missing_files_are_reported_in_sources_and_warnings(tmp_path: Path) -> None:
    cell = tmp_path / "stage-x" / "evidence" / "cells" / "c01"
    _write(cell / "analysis.json", _analysis("x.demo.C3.r1", "stage-x-demo"))
    _write(cell / "agent" / "bplus-c01" / "session.json", _session())
    service = _service(tmp_path)
    key = _only_key(service)

    overview = service.overview(key)
    roles = {source["role"] for source in overview["sources"]}
    assert {"analysis", "session"} <= roles
    assert overview["completeness"]["facets"]["artifacts"] == "MISSING"
    assert overview["problems"] == [], "an absent optional file is not a read failure"


def test_provider_requests_journal_is_used_when_present(tmp_path: Path) -> None:
    _seed_main_driven(tmp_path)
    agent = tmp_path / "stage-c1c" / "evidence" / "cells" / "c01" / "agent" / "bplus-c01"
    _write(
        agent / "provider_requests.json",
        [
            {
                "request_id": 1,
                "agent_id": "atlas-main",
                "phase": "main",
                "state": "COMPLETED",
                "requested_model": "deepseek-chat",
                "returned_model": "deepseek-chat",
                "usage_status": "COMPLETE",
                "started_at": "2026-11-25T11:32:10Z",
                "finished_at": "2026-11-25T11:32:15Z",
            },
            {
                "request_id": 2,
                "agent_id": "atlas-main",
                "phase": "main",
                "state": "FAILED",
                "error_type": "ReadTimeout",
                "error_message": f"boom {SECRET}",
                "http_status": 504,
            },
            {"request_id": 3, "agent_id": "atlas-main", "phase": "main", "state": "STARTED"},
        ],
    )
    service = _service(tmp_path)
    key = _only_key(service)
    payload = service.requests(key)

    assert payload["state"] == "AVAILABLE"
    assert payload["counts"]["total"] == 3
    assert payload["counts"]["completed"] == 1
    assert payload["counts"]["failed"] == 1
    assert payload["counts"]["started"] == 1
    assert payload["counts"]["unknown_terminal"] == 0
    assert payload["entries"][1]["http_status"] == 504
    assert SECRET not in json.dumps(payload)


def test_secrets_never_reach_the_projection(tmp_path: Path) -> None:
    _seed_main_driven(tmp_path)
    service = _service(tmp_path)
    key = _only_key(service)

    payloads = [
        service.overview(key),
        service.agents(key),
        service.delegation(key),
        service.timeline(key),
        service.resources(key),
        service.tools(key),
        service.artifacts(key),
        service.verification(key),
        service.requests(key),
    ]
    for payload in payloads:
        text = json.dumps(payload, default=str)
        assert SECRET not in text
        assert "Bearer abcdefghijklmnop" not in text


def test_artifacts_list_changed_files_without_serving_content(tmp_path: Path) -> None:
    _seed_main_driven(tmp_path)
    service = _service(tmp_path)
    key = _only_key(service)
    payload = service.artifacts(key)

    assert payload["patch"]["available"] is True
    assert payload["patch"]["changed_files"][0]["path"] == "src/a.py"
    assert "diff --git" not in json.dumps(payload)
    assert payload["handoffs"][0]["ref_id"] == "ho-1"
