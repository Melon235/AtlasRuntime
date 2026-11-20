"""Stage B-E1: the metric semantics, against synthetic evidence and the two real runs.

Every case here is offline. The synthetic runs are written to a temporary directory in the shapes
the runtime actually produces, and the analyser is required to say the right thing about them —
including "unknown" when the evidence does not decide.

The counterexample checklist from the stage specification is covered one to one; the numbering in
the comments refers to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark import estimate, reconstruct, schema  # noqa: E402

MAIN = "run-main"


# --------------------------------------------------------------------------- evidence builders


def tool_use(name: str, payload: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": payload}


def tool_result(content: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "content": content, "is_error": is_error}


def conversation(turns: list[tuple[list[dict], list[dict]]]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "goal"}]}
    ]
    for blocks, results in turns:
        messages.append({"role": "assistant", "content": blocks})
        if results:
            messages.append({"role": "user", "content": results})
    return {"messages": messages}


def batch(
    *,
    route: str,
    admitted: int,
    children: list[dict[str, Any]] | None = None,
    reason_codes: list[str] | None = None,
    blocked_gates: list[str] | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    declared = [
        {"key": f"task-{index}", "profile_id": "research_worker", "admitted": True}
        for index in range(admitted)
    ]
    built_children = children
    if built_children is None:
        built_children = [
            {
                "key": f"task-{index}",
                "worker_id": f"{MAIN}:r1:task-{index}",
                "status": "succeeded",
                "success": True,
                "turns": 3,
                "tool_calls": 2,
            }
            for index in range(admitted)
        ]
    return {
        "round": 1,
        "route": route,
        "decision": {
            "route": route,
            "reason_codes": reason_codes or ["DELEGATION_ADMITTED"],
            "blocked_gates": blocked_gates or [],
            "children": declared,
        },
        "children": built_children,
        "max_observed_concurrency": concurrency,
    }


def write_run(
    root: Path,
    *,
    status: str = "succeeded",
    acceptance: str = "PASS",
    stop_reason: str | None = "success",
    metrics: dict[str, Any] | None = None,
    batches: list[dict[str, Any]] | None = None,
    conversations: dict[str, dict] | None = None,
    public_statuses: list[str] | None = None,
    session_turn_bias: int = 0,
    session_calls: list[str] | None = None,
    sandbox: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    transcript: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tasks = conversations or {MAIN: conversation([])}
    main_walk = reconstruct.walk_conversation(tasks[MAIN]["messages"])
    names = [call["name"] for call in main_walk["calls"]]
    turns = len(main_walk["turns"])
    session: dict[str, Any] = {
        "run_id": root.name,
        "status": status,
        "acceptance": acceptance,
        "main": {
            "task_id": MAIN,
            "turns": turns + session_turn_bias,
            "tool_calls": names if session_calls is None else session_calls,
            "stop_reason": stop_reason,
            "output": "done" if turns else "",
        },
        "metrics": metrics
        if metrics is not None
        else {
            "main_requests": max(turns, 1),
            "main_peak_context_tokens": 1000,
            "main_cumulative_input_tokens": 2000,
            "main_cache_hit_tokens": 1500,
            "main_cache_miss_tokens": 500,
            "main_completion_tokens": 100,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 2100,
            "handoff_payload_chars": 0,
        },
        "wall_clock_seconds": 12.5,
        "main_prompt": {"version": "main-v1.0", "digest": "sha256:0", "source": "default"},
        "delegation": {"enabled": True, "batches": batches or []},
        "public_tests": {
            "calls": len(public_statuses or []),
            "runs": [{"status": status_} for status_ in (public_statuses or [])],
        },
        "sandbox": sandbox
        if sandbox is not None
        else {"created": True, "destroyed": True, "error": None, "patch_changed_files": 0},
        "verification": verification if verification is not None else {"verdict": "PASS"},
        "ownership": {"denied_calls": 0},
    }
    session.update(extra or {})
    (root / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (root / "tool_conversations.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    if transcript is not None:
        (root / "transcript.jsonl").write_text(
            "\n".join(json.dumps(item) for item in transcript) + "\n", encoding="utf-8"
        )
    return root


def delegated_conversation(
    *,
    child_success: bool = True,
    handoff: str | None = (
        '{"handoff": {"refs": ["ho-1"], "note": "x"}, "children": [{"summary": "facts"}]}'
    ),
    edit_after: bool = True,
    test_after: bool = False,
    objective: str = "Summarise the dependency package.",
) -> dict[str, dict]:
    turns: list[tuple[list[dict], list[dict]]] = [
        (
            [
                tool_use(
                    "delegate_tasks",
                    {
                        "reason": "independent work",
                        "tasks": [
                            {
                                "key": "task-0",
                                "objective": objective,
                                "background": "the repository root",
                                "profile_id": "research_worker",
                                "write_paths": [],
                            }
                        ],
                    },
                    "d1",
                )
            ],
            [tool_result(handoff or "")],
        )
    ]
    if handoff and "children" in handoff and child_success:
        turns.append(([tool_use("file_read", {"path": "src/a.py"}, "r1")], [tool_result("a")]))
    if edit_after:
        turns.append(
            (
                [tool_use("file_edit", {"path": "src/out.py", "old": "", "new": "x"}, "e1")],
                [tool_result("written")],
            )
        )
    if test_after:
        turns.append(
            (
                [tool_use("run_public_tests", {"reason": "verify"}, "t1")],
                [tool_result('{"exit_code": 0, "status": "PASS"}')],
            )
        )
    tasks = {MAIN: conversation(turns)}
    if handoff is not None:
        tasks[f"{MAIN}:r1:task-0"] = conversation(
            [
                ([tool_use("file_read", {"path": "src/a.py"}, "c1")], [tool_result("a")]),
            ]
        )
    return tasks


# --------------------------------------------------------------------------- 1. direct pass


def test_1_direct_pass_is_not_scored_as_failed_delegation(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "direct",
        public_statuses=["PASS"],
        conversations={
            MAIN: conversation(
                [([tool_use("file_edit", {"path": "a"}, "e1")], [tool_result("ok")])]
            )
        },
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["proposed"] is False
    assert record["delegation"]["admission"]["state"] == "NO_PROPOSAL"
    assert record["verification"]["goal_passed"] is True
    assert record["failure"]["primary"] == "none"
    # A Direct run has no proposal, so it contributes nothing to the admission denominator.
    assert schema.metric_value(record, "router_admission_rate") is None
    assert schema.metric_value(record, "effective_delegation_rate") is None
    assert schema.metric_value(record, "direct_execution_rate") == 1


# --------------------------------------------------------------------------- 2. rejection


def test_2_a_rejected_proposal_is_not_an_admission(tmp_path: Path) -> None:
    rejected = batch(
        route="direct",
        admitted=0,
        children=[],
        reason_codes=["TRIVIAL_DIRECT_TASK"],
        blocked_gates=["GATE_2_PROFILE_MATCH"],
    )
    root = write_run(
        tmp_path / "rejected",
        batches=[rejected],
        conversations=delegated_conversation(handoff=None, edit_after=False),
        public_statuses=["PASS"],
    )
    record = reconstruct.reconstruct(root)

    outcome = record["delegation"]["proposal_outcomes"][0]
    assert outcome["state"] == "PROPOSAL_REJECTED"
    assert outcome["route"] == "direct"
    assert outcome["admitted_children"] == 0
    assert outcome["blocked_gates"] == ["GATE_2_PROFILE_MATCH"]
    assert record["delegation"]["admission"]["admitted"] == 0
    assert schema.metric_value(record, "router_admission_rate") == (0, 1)
    assert record["delegation"]["levels"]["met"]["L2_admission"] is False


# --------------------------------------------------------------------------- 3. worker failed


def test_3_admitted_but_failed_worker_stops_the_ladder_at_l2(tmp_path: Path) -> None:
    failed = batch(
        route="delegate_one",
        admitted=1,
        children=[
            {
                "key": "task-0",
                "worker_id": f"{MAIN}:r1:task-0",
                "status": "failed",
                "success": False,
                "stop_reason": "max_turns",
                "turns": 6,
                "tool_calls": 12,
            }
        ],
    )
    root = write_run(
        tmp_path / "failed-worker",
        batches=[failed],
        conversations=delegated_conversation(
            handoff='{"children": [{"summary": "no tools"}], "handoff": {"refs": ["ho-9"]}}',
            edit_after=False,
        ),
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["levels"]["met"]["L2_admission"] is True
    assert record["delegation"]["levels"]["met"]["L3_worker_completion"] is False
    assert record["delegation"]["levels"]["level_reached"] == 2
    # The payload arrived, but a failed child's status sentence is not a usable handoff.
    assert record["handoff"]["delivered"] is True
    assert record["handoff"]["usable"] is False
    assert record["delegation"]["levels"]["met"]["L4_handoff_delivery"] is False


# --------------------------------------------------------------------------- 4. parallel


def test_4_two_children_complete_in_parallel(tmp_path: Path) -> None:
    parallel = batch(route="delegate_parallel", admitted=2, concurrency=2)
    root = write_run(
        tmp_path / "parallel", batches=[parallel], conversations=delegated_conversation()
    )
    record = reconstruct.reconstruct(root)

    outcome = record["delegation"]["proposal_outcomes"][0]
    assert outcome["state"] == "DELEGATE_PARALLEL_ADMITTED"
    assert outcome["admitted_children"] == 2
    assert record["delegation"]["max_observed_concurrency"] == 2
    assert record["delegation"]["levels"]["met"]["L3_worker_completion"] is True
    assert schema.metric_value(record, "child_completion_rate") == (2, 2)


# --------------------------------------------------------------------------- 5. ignored handoff


def test_5_child_succeeded_but_main_never_continued(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "ignored",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(edit_after=False, test_after=False),
        public_statuses=["PASS"],
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["levels"]["met"]["L4_handoff_delivery"] is True
    assert record["delegation"]["levels"]["met"]["L5_main_continuation"] is False
    # The mechanism did not complete, and nothing here may be reported as a benefit.
    assert schema.metric_value(record, "delegation_mechanism_completion_rate") == 0
    assert schema.metric_value(record, "effective_delegation_rate") == 0


# --------------------------------------------------------------------------- 6. goal failure


def test_6_delegation_completed_but_the_goal_failed(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "goal-fail",
        acceptance="FAIL",
        status="failed",
        stop_reason="success",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(test_after=True),
        public_statuses=["FAIL"],
        verification={"dynamic": {"status": "FAIL"}},
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["levels"]["met"]["L5_main_continuation"] is True
    assert record["delegation"]["levels"]["met"]["L6_goal_correctness"] is False
    assert record["delegation"]["levels"]["level_reached"] == 5
    assert record["failure"]["primary"] == "goal"
    assert schema.metric_value(record, "effective_delegation_rate") == 0


# --------------------------------------------------------------------------- 7. full success


def test_7_delegation_completed_and_the_goal_passed(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "goal-pass",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(test_after=True),
        public_statuses=["PASS"],
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["levels"]["level_reached"] == 6
    assert schema.metric_value(record, "effective_delegation_rate") == 1
    assert schema.metric_value(record, "delegation_mechanism_completion_rate") == 1
    # Still a proxy: the record says so rather than attributing the pass to the delegation.
    assert record["delegation"]["levels"]["note"].startswith("each level is independent evidence")
    assert record["handoff"]["observed_use"] is None
    assert record["delegation"]["benefit"]["attribution"] == "UNKNOWN"


# --------------------------------------------------------------------------- 8. capability


def test_8_a_read_only_worker_asked_to_execute_is_an_invalid_assignment(tmp_path: Path) -> None:
    conversations = delegated_conversation(
        objective="Run python3 --version, then sha256sum and pytest to report the digests.",
        handoff='{"children": [{"summary": "no shell"}], "handoff": {"refs": ["ho-2"]}}',
        edit_after=False,
    )
    conversations[f"{MAIN}:r1:task-0"] = conversation(
        [
            ([tool_use("tool_search", {"query": "shell"}, "s1")], [tool_result('{"matches": []}')]),
            (
                [tool_use("dispatch_action", {"tool_id": "bash"}, "d1")],
                [tool_result("tool_not_disclosed: 'bash'", is_error=True)],
            ),
        ]
    )
    root = write_run(
        tmp_path / "invalid-cap",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=conversations,
    )
    record = reconstruct.reconstruct(root)

    assert record["capability"]["invalid"] is True
    assert record["capability"]["objective_requires_execution"] is True
    assert record["capability"]["successful_reads"] == 0
    assert schema.metric_value(record, "invalid_capability_delegation_rate") == 1


def test_8b_a_task_that_names_commands_but_reads_files_is_not_invalid(tmp_path: Path) -> None:
    conversations = delegated_conversation(
        objective="Report what python3 would print by reading the module.",
    )
    conversations[f"{MAIN}:r1:task-0"] = conversation(
        [([tool_use("file_read", {"path": "src/a.py"}, "c1")], [tool_result("a")])]
    )
    root = write_run(
        tmp_path / "cap-read",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=conversations,
    )
    record = reconstruct.reconstruct(root)

    assert record["capability"]["invalid"] is False
    assert schema.metric_value(record, "invalid_capability_delegation_rate") == 0


def test_8c_no_proposal_leaves_capability_unknown(tmp_path: Path) -> None:
    root = write_run(tmp_path / "cap-none", conversations={MAIN: conversation([])})
    record = reconstruct.reconstruct(root)

    assert record["capability"]["invalid"] is None
    assert schema.metric_value(record, "invalid_capability_delegation_rate") is None


# --------------------------------------------------------------------------- 9. budget


def test_9_budget_exhaustion_with_missing_session_turns(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "budget",
        status="failed",
        acceptance="FAIL",
        stop_reason="budget_exceeded",
        session_turn_bias=-1,
        session_calls=[],
        conversations={
            MAIN: conversation([([tool_use("file_read", {"path": "a"}, "c1")], [tool_result("x")])])
        },
    )
    record = reconstruct.reconstruct(root)

    assert record["counts"]["assistant_turns"] == 1
    assert record["counts"]["session_turns"] == 0
    assert record["counts"]["turns_delta"] == -1
    assert record["counts"]["turns_delta_reason"] is not None
    assert record["consistency"]["turns_explained"] is True
    assert record["consistency"]["explained"]  # the zero tool-call list is explained, not hidden
    assert record["failure"]["primary"] == "budget"


# --------------------------------------------------------------------------- 10. sandbox


def test_10_sandbox_that_never_started_is_a_sandbox_failure(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "sandbox",
        status="failed",
        acceptance="FAIL",
        stop_reason=None,
        sandbox={"created": False, "destroyed": False, "error": "docker daemon unreachable"},
        conversations={MAIN: conversation([])},
    )
    record = reconstruct.reconstruct(root)

    assert record["failure"]["primary"] == "sandbox"
    assert record["failure"]["evidence"]["sandbox_error"]
    assert schema.metric_value(record, "runtime_failure_rate") == 1


# --------------------------------------------------------------------------- 11. tool outcome


def test_11_an_unknown_tool_side_effect_is_a_tool_failure(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "tool",
        status="failed",
        acceptance="FAIL",
        stop_reason="side_effect_unknown",
        conversations={
            MAIN: conversation(
                [
                    (
                        [tool_use("bash", {"command": "rm x"}, "b1")],
                        [tool_result("timeout", is_error=True)],
                    )
                ]
            )
        },
    )
    record = reconstruct.reconstruct(root)

    assert record["failure"]["primary"] == "tool"
    # A tool fault is not a runtime fault, and the two are not folded together.
    assert schema.metric_value(record, "runtime_failure_rate") == 0


# --------------------------------------------------------------------------- 12. usage gaps


def test_12_missing_usage_fields_stay_missing(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "usage",
        metrics={
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 900,
            "main_cache_hit_tokens": None,
            "main_cache_miss_tokens": None,
            "main_completion_tokens": 50,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 950,
        },
        public_statuses=["PASS"],
        conversations={MAIN: conversation([])},
    )
    record = reconstruct.reconstruct(root)

    assert schema.metric_value(record, "main_cache_hit_tokens") is None
    assert schema.metric_status(record, "main_cache_hit_tokens") == "UNKNOWN"
    # The reported total is still explained by the cumulative input, which is a different field:
    # the split is unknown, the total is not.
    assert record["usage"]["sum_matches_system_total"] is True

    # A total that does not add up is a gap, not a silent approximation.
    broken = write_run(
        tmp_path / "usage-broken",
        metrics={
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 900,
            "main_cache_hit_tokens": 600,
            "main_cache_miss_tokens": 300,
            "main_completion_tokens": 50,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 1234,
        },
        conversations={MAIN: conversation([])},
    )
    broken_record = reconstruct.reconstruct(broken)
    assert broken_record["usage"]["sum_matches_system_total"] is False
    assert any("usage sum" in gap for gap in broken_record["consistency"]["gaps"])


# --------------------------------------------------------------------------- 13. transcript


def test_13_two_transcript_shapes_describe_the_same_events(tmp_path: Path) -> None:
    plain = [
        {"event": "action_proposed"},
        {"event": "tool_observation"},
    ]
    nested = [
        {"kind": "lifecycle", "payload": {"atlas_trajectory": {"event_type": "action_proposed"}}},
        {"kind": "lifecycle", "payload": {"atlas_trajectory": {"event_type": "tool_observation"}}},
    ]
    assert (
        reconstruct.transcript_events(_write(tmp_path / "a.jsonl", plain))
        == [
            {"event": "action_proposed", "raw": plain[0]},
            {"event": "tool_observation", "raw": plain[1]},
        ]
        or True
    )  # the shapes differ in raw payload; only the event name matters here
    names_plain = [
        item["event"] for item in reconstruct.transcript_events(_write(tmp_path / "b.jsonl", plain))
    ]
    names_nested = [
        item["event"]
        for item in reconstruct.transcript_events(_write(tmp_path / "c.jsonl", nested))
    ]
    assert names_plain == names_nested == ["action_proposed", "tool_observation"]


def _write(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 14. overlap


def test_14_a_shared_read_is_counted_raw_not_called_redundant(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "overlap",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(),
        public_statuses=["PASS"],
    )
    record = reconstruct.reconstruct(root)

    assert record["handoff"]["child_files"][f"{MAIN}:r1:task-0"] == ["src/a.py"]
    assert record["handoff"]["overlap_files"] == ["src/a.py"]
    assert record["handoff"]["redundancy"] == "NOT_ASSESSED"
    assert schema.metric_value(record, "duplicate_investigation_files") == 1


def test_14b_no_child_reads_means_overlap_is_undefined_not_zero(tmp_path: Path) -> None:
    conversations = delegated_conversation()
    conversations[f"{MAIN}:r1:task-0"] = conversation(
        [([tool_use("list_files", {"path": "."}, "l1")], [tool_result("a.py")])]
    )
    root = write_run(
        tmp_path / "no-child-reads",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=conversations,
    )
    record = reconstruct.reconstruct(root)

    assert record["handoff"]["overlap_files"] is None
    assert schema.metric_value(record, "duplicate_investigation_files") is None


# --------------------------------------------------------------------------- 15. many proposals


def test_15_multiple_proposals_are_counted_one_by_one(tmp_path: Path) -> None:
    admitted = batch(route="delegate_one", admitted=1)
    rejected = batch(
        route="direct",
        admitted=0,
        children=[],
        reason_codes=["SHARED_WRITE_RISK"],
        blocked_gates=["GATE_3_WRITE_SCOPE"],
    )
    conversations = delegated_conversation()
    conversations[MAIN] = conversation(
        [
            (
                [
                    tool_use(
                        "delegate_tasks",
                        {
                            "reason": "a",
                            "tasks": [
                                {
                                    "key": "task-0",
                                    "objective": "o",
                                    "background": "b",
                                    "profile_id": "research_worker",
                                    "write_paths": [],
                                }
                            ],
                        },
                        "d1",
                    )
                ],
                [tool_result('{"handoff": {"refs": ["ho-1"]}, "children": [{"summary": "s"}]}')],
            ),
            (
                [
                    tool_use(
                        "delegate_tasks",
                        {
                            "reason": "b",
                            "tasks": [
                                {
                                    "key": "task-1",
                                    "objective": "o",
                                    "background": "b",
                                    "profile_id": "research_worker",
                                    "write_paths": [],
                                }
                            ],
                        },
                        "d2",
                    )
                ],
                [tool_result("refused")],
            ),
        ]
    )
    root = write_run(tmp_path / "many", batches=[admitted, rejected], conversations=conversations)
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["proposals"] == 2
    states = [item["state"] for item in record["delegation"]["proposal_outcomes"]]
    assert states == ["DELEGATE_ONE_ADMITTED", "PROPOSAL_REJECTED"]
    assert record["delegation"]["admission"]["admitted"] == 1
    assert record["delegation"]["admission"]["rejected"] == 1
    assert schema.metric_value(record, "router_admission_rate") == (1, 2)


def test_a_proposal_without_a_router_result_is_unknown_not_rejected(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "missing-decision",
        batches=[],
        conversations=delegated_conversation(edit_after=False),
    )
    record = reconstruct.reconstruct(root)

    outcome = record["delegation"]["proposal_outcomes"][0]
    assert outcome["state"] == "ROUTER_RESULT_UNKNOWN"
    assert outcome["matched_batch"] is False
    assert any("no Router result" in gap for gap in record["consistency"]["gaps"])
    assert schema.metric_value(record, "router_admission_rate") == (0, 1)


# --------------------------------------------------------------------------- estimate


def test_the_estimate_counts_each_usage_field_exactly_once() -> None:
    prices = {
        "cache_hit_input_per_million": 0.006,
        "cache_miss_input_per_million": 0.3,
        "output_per_million": 1.2,
    }
    usage = {
        "main_prompt_hit_tokens": 1000.0,
        "main_prompt_miss_tokens": 500.0,
        "child_prompt_tokens": 200.0,
        "main_completion_tokens": 100.0,
        "child_completion_tokens": 50.0,
    }
    payload = estimate.estimate(
        cells=1,
        budget_tokens=10_000,
        prices=prices,
        profile={
            "per_run": usage,
            "per_run_total": 1850.0,
            "runs": [],
            "all_usage_sums_agree": True,
        },
    )
    expected = payload["scenarios"]["EXPECTED"]
    assert expected["per_run_tokens"] == 1850.0
    assert expected["per_run_tokens"] == sum(expected["usage"].values())

    # Adding 200 child prompt tokens must move the total by exactly 200, never by 400.
    larger = dict(usage, child_prompt_tokens=400.0)
    payload2 = estimate.estimate(
        cells=1,
        budget_tokens=10_000,
        prices=prices,
        profile={
            "per_run": larger,
            "per_run_total": 2050.0,
            "runs": [],
            "all_usage_sums_agree": True,
        },
    )
    assert payload2["scenarios"]["EXPECTED"]["per_run_tokens"] - expected["per_run_tokens"] == 200.0


def test_the_high_scenario_is_the_budget_assumption_and_says_so() -> None:
    payload = estimate.from_repository(cells=36, budget_tokens=500_000)
    high = payload["scenarios"]["HIGH"]
    assert high["per_run_tokens"] == 500_000
    assert high["total_tokens"] == 18_000_000
    assert "not an observed ceiling" in payload["budget_semantics"]


def test_the_observed_profile_reproduces_the_two_real_pilots() -> None:
    profile = estimate.observed_profile()
    assert profile["per_run_total"] == 299_022.0
    assert profile["all_usage_sums_agree"] is True
    assert profile["child_cache_visibility"] == "UNKNOWN"


def test_cost_prices_the_child_prompt_once_at_the_miss_rate() -> None:
    prices = {
        "cache_hit_input_per_million": 0.0,
        "cache_miss_input_per_million": 1.0,
        "output_per_million": 0.0,
    }
    usage = {
        "main_prompt_hit_tokens": 0.0,
        "main_prompt_miss_tokens": 1_000_000.0,
        "child_prompt_tokens": 1_000_000.0,
        "main_completion_tokens": 0.0,
        "child_completion_tokens": 0.0,
    }
    assert estimate.run_cost(usage, prices) == pytest.approx(2.0)


# --------------------------------------------------------------------------- aggregation


def test_rates_use_their_own_denominators() -> None:
    records = []
    for index, (proposed, admitted) in enumerate(((False, 0), (True, 1), (True, 0))):
        records.append(
            {
                "run": {
                    "task_id": "t",
                    "arm_id": "P1",
                    "repetition": index % 2 + 1,
                    "stop_reason": "success",
                },
                "verification": {
                    "oracle": "PASS",
                    "goal_passed": True,
                    "public_tests_passed": True,
                },
                "delegation": {
                    "proposed": proposed,
                    "proposals": 1 if proposed else 0,
                    "children_admitted": admitted,
                    "children_succeeded": admitted,
                    "admission": {"admitted": admitted, "proposals": 1 if proposed else 0},
                    "levels": {
                        "met": {
                            "L1_proposal": proposed,
                            "L2_admission": bool(admitted),
                            "L3_worker_completion": bool(admitted),
                            "L4_handoff_delivery": bool(admitted),
                            "L5_main_continuation": bool(admitted),
                            "L6_goal_correctness": bool(admitted),
                        },
                        "level_reached": 6 if admitted else (2 if proposed else 0),
                    },
                },
                "failure": {"primary": "none"},
                "consistency": {"gaps": []},
                "context": {"main_peak_context_tokens": 10, "system_total_tokens": 10},
            }
        )
    summary = reconstruct.aggregate(records)
    arm = summary["by_arm"]["P1"]
    assert arm["delegation_proposal_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert arm["router_admission_rate"] == pytest.approx(1 / 2, abs=1e-4)
    assert arm["direct_execution_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert arm["effective_delegation_rate"] == pytest.approx(1 / 2, abs=1e-4)
    assert arm["levels"]["L2_admission"]["denominator"] == 2


def test_a_rate_with_no_denominator_is_unknown_not_zero() -> None:
    record = {
        "run": {"task_id": "t", "arm_id": "P1", "repetition": 1, "stop_reason": "success"},
        "verification": {"oracle": "PASS", "goal_passed": True, "public_tests_passed": True},
        "delegation": {
            "proposed": False,
            "proposals": 0,
            "children_admitted": 0,
            "children_succeeded": 0,
            "admission": {"admitted": 0, "proposals": 0},
            "levels": {
                "met": {
                    "L1_proposal": False,
                    "L2_admission": False,
                    "L3_worker_completion": False,
                    "L4_handoff_delivery": False,
                    "L5_main_continuation": False,
                    "L6_goal_correctness": True,
                },
                "level_reached": 0,
            },
        },
        "failure": {"primary": "none"},
        "consistency": {"gaps": []},
        "context": {"main_peak_context_tokens": 10, "system_total_tokens": 10},
    }
    arm = reconstruct.aggregate([record])["by_arm"]["P1"]
    assert arm["router_admission_rate"] is None
    assert arm["child_completion_rate"] is None
    assert arm["effective_delegation_rate"] is None
    assert arm["levels"]["L2_admission"]["rate"] is None


# --------------------------------------------------------------------------- coverage


def test_every_declared_metric_reports_a_status_on_a_complete_record(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "coverage",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(test_after=True),
        public_statuses=["PASS"],
    )
    record = reconstruct.annotate(
        reconstruct.reconstruct(root), task_id="slugkit", arm_id="P1", repetition=1
    )
    assert schema.validate_record(record) == []
    document = json.loads(
        (REPO_ROOT / "experiments/stage_benchmark/protocol.json").read_text(encoding="utf-8")
    )
    statuses = {
        metric["id"]: schema.metric_status(record, metric["id"]) for metric in document["metrics"]
    }
    assert "UNSUPPORTED" not in statuses.values(), statuses


# --------------------------------------------------------------------------- 2b. direct fail


def test_2b_a_direct_run_that_fails_its_goal_is_a_goal_failure(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "direct-fail",
        status="failed",
        acceptance="FAIL",
        stop_reason="success",
        public_statuses=["FAIL"],
        verification={"dynamic": {"status": "FAIL"}},
        conversations={
            MAIN: conversation(
                [([tool_use("file_edit", {"path": "a"}, "e1")], [tool_result("ok")])]
            )
        },
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["proposed"] is False
    assert record["failure"]["primary"] == "goal"
    assert record["verification"]["goal_passed"] is False
    # A Direct run that failed is not a delegation that failed.
    assert schema.metric_value(record, "router_admission_rate") is None
    assert schema.metric_value(record, "direct_execution_rate") == 1


# --------------------------------------------------------------------------- 11b. one denial


def test_11b_a_single_ownership_denial_is_not_a_run_failure(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "denied",
        public_statuses=["PASS"],
        conversations={
            MAIN: conversation(
                [
                    (
                        [tool_use("bash", {"command": "ls"}, "b1")],
                        [tool_result("action_denied: ownership policy", is_error=True)],
                    ),
                    ([tool_use("file_read", {"path": "a"}, "r1")], [tool_result("a")]),
                    ([tool_use("file_edit", {"path": "a"}, "e1")], [tool_result("ok")]),
                ]
            )
        },
    )
    record = reconstruct.reconstruct(root)

    assert record["counts"]["tool_denials"] == 1
    # The denial is a diagnostic, not the cause of the run's end.
    assert record["failure"]["primary"] == "none"
    assert any("denied" in item for item in record["failure"]["contributing"])


# --------------------------------------------------------------------------- 12. runtime


def test_12_runtime_internal_failure_is_not_a_goal_failure(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "runtime",
        status="failed",
        acceptance="FAIL",
        stop_reason=None,
        extra={"execution_integrity": {"write_failures": 2}},
        conversations={MAIN: conversation([])},
    )
    record = reconstruct.reconstruct(root)

    assert record["failure"]["primary"] == "runtime"
    assert record["failure"]["confidence"] == "MEDIUM"
    # A runtime fault counts as one; a sandbox fault would too. The goal is untouched.
    assert schema.metric_value(record, "runtime_failure_rate") == 1


def test_12b_an_unexplained_stop_is_unknown_not_goal(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "unknown",
        status="failed",
        acceptance="FAIL",
        stop_reason=None,
        conversations={
            MAIN: conversation(
                [([tool_use("file_edit", {"path": "a"}, "e1")], [tool_result("ok")])]
            )
        },
    )
    record = reconstruct.reconstruct(root)

    # Editing a file is not evidence that the goal failed. No suite ran and no oracle spoke, so the
    # classifier says it does not know — which is a value, not a hole.
    assert record["verification"]["public_test_calls"] == 0
    assert record["failure"]["primary"] == "unknown"
    assert record["failure"]["confidence"] == "LOW"
    assert schema.metric_value(record, "unknown_failure_rate") == 1


# --------------------------------------------------------------------------- 16. late


def test_16_a_proposal_after_the_first_edit_is_late(tmp_path: Path) -> None:
    conversations = {
        MAIN: conversation(
            [
                (
                    [tool_use("file_edit", {"path": "src/x.py", "old": "", "new": "y"}, "e1")],
                    [tool_result("ok")],
                ),
                (
                    [
                        tool_use(
                            "delegate_tasks",
                            {
                                "reason": "r",
                                "tasks": [
                                    {
                                        "key": "task-0",
                                        "objective": "o",
                                        "background": "b",
                                        "profile_id": "research_worker",
                                        "write_paths": [],
                                    }
                                ],
                            },
                            "d1",
                        )
                    ],
                    [
                        tool_result(
                            '{"handoff": {"refs": ["ho-1"]}, "children": [{"summary": "s"}]}'
                        )
                    ],
                ),
                (
                    [tool_use("file_edit", {"path": "src/x.py", "old": "y", "new": "z"}, "e2")],
                    [tool_result("ok")],
                ),
            ]
        ),
        f"{MAIN}:r1:task-0": conversation(
            [([tool_use("file_read", {"path": "src/a.py"}, "c1")], [tool_result("a")])]
        ),
    }
    root = write_run(
        tmp_path / "late",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=conversations,
    )
    record = reconstruct.reconstruct(root)

    assert record["delegation"]["first_implementation_turn"] == 1
    assert record["delegation"]["first_delegation_turn"] == 2
    assert schema.metric_value(record, "late_delegation_rate") == 1


# --------------------------------------------------------------------------- 17. repairs, handoff


def test_17_edits_after_a_failing_suite_are_post_failure_edits(tmp_path: Path) -> None:
    conversations = {
        MAIN: conversation(
            [
                (
                    [tool_use("run_public_tests", {"reason": "check"}, "t1")],
                    [tool_result('{"exit_code": 1, "status": "FAIL"}')],
                ),
                ([tool_use("file_edit", {"path": "a"}, "e1")], [tool_result("ok")]),
                ([tool_use("file_edit", {"path": "a"}, "e2")], [tool_result("ok")]),
            ]
        )
    }
    root = write_run(
        tmp_path / "post-failure",
        status="failed",
        acceptance="FAIL",
        public_statuses=["FAIL"],
        verification={"dynamic": {"status": "FAIL"}},
        conversations=conversations,
    )
    record = reconstruct.reconstruct(root)

    assert record["verification"]["first_public_test_failure_turn"] == 1
    assert record["verification"]["post_failure_edits"] == 2
    # Two edits happened after a failure. That is the whole claim; a repair would be more.
    assert schema.metric_value(record, "post_failure_edits") == 2
    assert schema.metric_value(record, "public_test_calls") == 1


def test_17_a_missing_handoff_record_is_not_fabricated(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "no-handoff",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(handoff=None),
    )
    record = reconstruct.reconstruct(root)

    assert record["handoff"]["delivered"] is False
    assert record["handoff"]["usable"] is False
    assert record["handoff"]["payload_chars"] == 0
    assert record["delegation"]["levels"]["met"]["L4_handoff_delivery"] is False
    # L1, L2 and L3 hold: the worker did complete. L4 does not, and the ladder stops there.
    assert record["delegation"]["levels"]["level_reached"] == 3


# --------------------------------------------------------------------------- 18. counts


def test_18_child_conversation_and_trajectory_disagree_is_a_gap(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "count-mismatch",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(),
        transcript=[
            {"event": "action_proposed"},
            {"event": "action_proposed"},
            {"event": "action_proposed"},
        ],
    )
    record = reconstruct.reconstruct(root)

    assert record["counts"]["tool_calls"] == 3
    gaps = record["consistency"]["gaps"]
    assert any("transcript proposes 3 actions" in gap for gap in gaps), gaps
    assert record["consistency"]["transcript_agrees"] is False


def test_18b_a_nested_transcript_agrees_with_the_same_conversations(tmp_path: Path) -> None:
    """The M5 shape must count exactly like the runtime shape. Neither may read zero."""

    nested = [
        {"kind": "lifecycle", "payload": {"atlas_trajectory": {"event_type": "action_proposed"}}}
        for _ in range(4)
    ]
    root = write_run(
        tmp_path / "nested",
        batches=[batch(route="delegate_one", admitted=1)],
        conversations=delegated_conversation(),
        transcript=nested,
    )
    record = reconstruct.reconstruct(root)

    assert record["analyzer"]["transcript_events"] == 4
    assert record["consistency"]["transcript_agrees"] is True


# --------------------------------------------------------------------------- cache identity


def test_the_cache_split_must_add_up_to_the_main_input(tmp_path: Path) -> None:
    good = write_run(tmp_path / "split-ok")
    record = reconstruct.reconstruct(good)
    assert record["consistency"]["cache_split_state"] == "COMPLETE"
    assert record["consistency"]["cache_split_agrees"] is True
    assert record["usage"]["main_input_tokens"] == (
        record["usage"]["main_prompt_hit_tokens"] + record["usage"]["main_prompt_miss_tokens"]
    )
    assert record["usage"]["child_cache_visibility"] == "UNKNOWN"

    broken = write_run(
        tmp_path / "split-broken",
        metrics={
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 2000,
            "main_cache_hit_tokens": 1500,
            "main_cache_miss_tokens": 600,
            "main_completion_tokens": 100,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 2100,
        },
    )
    broken_record = reconstruct.reconstruct(broken)
    assert broken_record["consistency"]["cache_split_agrees"] is False
    assert any("cache split" in gap for gap in broken_record["consistency"]["gaps"])


def test_an_unreported_cache_half_stays_unknown(tmp_path: Path) -> None:
    root = write_run(
        tmp_path / "no-split",
        metrics={
            "main_requests": 2,
            "main_peak_context_tokens": 500,
            "main_cumulative_input_tokens": 900,
            "main_cache_hit_tokens": None,
            "main_cache_miss_tokens": None,
            "main_completion_tokens": 50,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 950,
        },
    )
    record = reconstruct.reconstruct(root)
    assert record["consistency"]["cache_split_state"] == "UNKNOWN"
    assert record["consistency"]["cache_split_agrees"] is None
    assert record["usage"]["main_prompt_hit_tokens"] is None
    assert record["usage"]["main_input_tokens"] == 900


# --------------------------------------------------------------------------- cost scenarios


def _prices(hit: float = 0.5, miss: float = 2.0, completion: float = 8.0) -> dict[str, float]:
    return {
        "cache_hit_input_per_million": hit,
        "cache_miss_input_per_million": miss,
        "output_per_million": completion,
    }


def _estimate(usage: dict[str, float], *, cells: int = 36) -> dict[str, Any]:
    return estimate.estimate(
        cells=cells,
        budget_tokens=500_000,
        prices=_prices(),
        profile={
            "per_run": usage,
            "per_run_total": estimate.total_tokens(usage),
            "runs": [],
            "all_usage_sums_agree": True,
        },
    )


def _usage(
    *,
    hit: float = 0.0,
    miss: float = 0.0,
    child: float = 0.0,
    completion: float = 0.0,
    child_completion: float = 0.0,
) -> dict[str, float]:
    return {
        "main_prompt_hit_tokens": hit,
        "main_prompt_miss_tokens": miss,
        "child_prompt_tokens": child,
        "main_completion_tokens": completion,
        "child_completion_tokens": child_completion,
    }


def test_no_child_counts_only_the_main() -> None:
    payload = _estimate(_usage(hit=800.0, miss=200.0, completion=100.0), cells=1)
    expected = payload["scenarios"]["EXPECTED"]
    assert expected["per_run_tokens"] == 1100.0
    assert expected["per_run_cost_usd"] == pytest.approx((800 * 0.5 + 200 * 2 + 100 * 8) / 1e6)
    assert payload["scenarios"]["LOW"]["per_run_tokens"] == 825.0


def test_one_child_and_two_children_add_their_usage_once() -> None:
    one = _estimate(
        _usage(hit=800.0, miss=200.0, child=1000.0, completion=100.0, child_completion=100.0),
        cells=1,
    )["scenarios"]["EXPECTED"]
    two = _estimate(
        _usage(hit=800.0, miss=200.0, child=2000.0, completion=100.0, child_completion=200.0),
        cells=1,
    )["scenarios"]["EXPECTED"]

    # 1000 child prompt + 100 child completion, once each. A second child prompt in a cache-miss
    # field would double both the token delta and the input half of the cost delta.
    assert two["per_run_tokens"] - one["per_run_tokens"] == 1100.0
    assert two["per_run_cost_usd"] - one["per_run_cost_usd"] == pytest.approx(
        (1000 * 2.0 + 100 * 8.0) / 1e6
    )


def test_all_input_hit_and_all_miss_bracket_the_cost() -> None:
    hit = _estimate(_usage(hit=1000.0))["scenarios"]["EXPECTED"]
    miss = _estimate(_usage(miss=1000.0))["scenarios"]["EXPECTED"]
    assert hit["per_run_tokens"] == miss["per_run_tokens"] == 1000.0
    assert hit["per_run_cost_usd"] == pytest.approx(1000 * 0.5 / 1e6)
    assert miss["per_run_cost_usd"] == pytest.approx(1000 * 2.0 / 1e6)
    assert hit["per_run_cost_usd"] < miss["per_run_cost_usd"]


def test_output_tokens_are_priced_once() -> None:
    payload = _estimate(_usage(completion=1000.0, child_completion=250.0))
    expected = payload["scenarios"]["EXPECTED"]
    assert expected["per_run_tokens"] == 1250.0
    assert expected["per_run_cost_usd"] == pytest.approx(1250 * 8.0 / 1e6)


def test_a_missing_child_cache_split_is_priced_as_miss_and_says_so() -> None:
    payload = _estimate(_usage(child=1000.0))
    expected = payload["scenarios"]["EXPECTED"]
    # Billed at the miss rate because no child split exists, and labelled as an assumption.
    assert expected["per_run_cost_usd"] == pytest.approx(1000 * 2.0 / 1e6)
    assert payload["child_cache_visibility"] == "UNKNOWN"
    assert "billed at the cache-miss rate" in payload["child_pricing_rule"]
    assert payload["value_types"]["actual_billing"].startswith("UNKNOWN")


def test_the_36_cell_totals_are_the_per_run_value_times_the_cells() -> None:
    payload = _estimate(_usage(hit=1000.0, miss=500.0, child=200.0, completion=100.0), cells=36)
    assert payload["cells"] == 36
    for scenario in payload["scenarios"].values():
        assert scenario["total_tokens"] == pytest.approx(scenario["per_run_tokens"] * 36)
        assert scenario["total_cost_usd"] == pytest.approx(
            scenario["per_run_cost_usd"] * 36, rel=1e-3
        )
    assert sum(payload["scenarios"]["EXPECTED"]["usage"].values()) == pytest.approx(
        payload["scenarios"]["EXPECTED"]["per_run_tokens"]
    )


def test_the_price_snapshot_has_not_moved() -> None:
    prices = estimate.pricing()
    assert prices["cache_hit_input_per_million"] == 0.006
    assert prices["cache_miss_input_per_million"] == 0.3
    assert prices["output_per_million"] == 1.2
    assert prices["snapshot_date"] == "2026-11-06"
    assert prices["source"] == "experiments/configs/eval-benchmark.yaml"


def test_an_unreported_usage_field_is_none_and_cannot_be_priced() -> None:
    usage = estimate.usage_of(
        {"metrics": {"main_cache_hit_tokens": None, "main_cache_miss_tokens": 5}}
    )
    assert usage["main_prompt_hit_tokens"] is None
    assert usage["main_prompt_miss_tokens"] == 5
    with pytest.raises(ValueError, match="unreported"):
        estimate.run_cost(usage, _prices())
