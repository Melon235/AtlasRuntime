"""Stage S2: Main turn/tool statistics are reconstructed from artefacts, not from the summary field.

Stage S1 ended on a budget rejection and ``session.json`` reported ``main.turns = 0`` and
``main.tool_calls = []`` while the stored conversation held 18 assistant turns and 36 tool calls.
An analysis that trusted the field would have reported a Main that did nothing.

These tests cover the three ways a run can end — clean success, budget rejection, tool failure — and
assert that the reconstruction counts the same things the conversation says, whatever the summary
field claims. They are the regression test for that defect; they do not change the runtime's session
schema.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

STAGE_DIR = Path(__file__).resolve().parents[2] / "experiments" / "stage_s2_prompt_v21"
sys.path.insert(0, str(STAGE_DIR))

import s2_analysis as analysis  # noqa: E402

MAIN = "run-main"


def tool_use(
    name: str, payload: dict[str, Any] | None = None, call_id: str = "c"
) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": payload or {}}


def tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "content": text, "is_error": is_error}


def conversation(turns: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]) -> dict[str, Any]:
    """``turns`` is a list of (assistant blocks, results for those blocks)."""

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "the goal"}]}
    ]
    for blocks, results in turns:
        messages.append({"role": "assistant", "content": blocks})
        if results:
            messages.append({"role": "user", "content": results})
    return {"tasks": {MAIN: {"messages": messages}}}


def session(turns: int, calls: list[Any]) -> dict[str, Any]:
    return {"main": {"task_id": MAIN, "turns": turns, "tool_calls": calls}}


# --------------------------------------------------------------------------- success


def test_a_clean_run_counts_what_the_conversation_holds() -> None:
    data = conversation(
        [
            (
                [
                    tool_use("file_read", {"path": "a.py"}, "c1"),
                    tool_use("file_read", {"path": "b.py"}, "c2"),
                ],
                [tool_result("a"), tool_result("b")],
            ),
            ([tool_use("file_write", {"path": "a.py"}, "c3")], [tool_result("written")]),
        ]
    )
    stats = analysis.reconstruct_main_stats(
        data, [], MAIN, session(2, ["file_read", "file_read", "file_write"])
    )

    assert stats["assistant_turns"] == 2
    assert stats["tool_calls"] == 3
    assert stats["tool_calls_by_name"] == {"file_read": 2, "file_write": 1}
    assert stats["tool_errors"] == 0
    assert stats["session_reported_turns"] == 2
    assert stats["session_fields_agree"] is True
    assert stats["source"] == "tool_conversations.json + transcript.jsonl"


# --------------------------------------------------------------------------- budget rejection


def test_a_budget_stop_with_zeroed_session_fields_still_counts_every_turn() -> None:
    data = conversation(
        [
            ([tool_use("file_read", {"path": "a.py"}, "c1")], [tool_result("a")]),
            ([tool_use("bash", {"command": "ls"}, "c2")], [tool_result("denied", is_error=True)]),
            ([tool_use("delegate_tasks", {"tasks": [{"key": "k"}]}, "c3")], [tool_result("{}")]),
        ]
    )
    # This is exactly the S1 shape: the loop ended on a rejection, the result carried no messages.
    stats = analysis.reconstruct_main_stats(data, [], MAIN, session(0, []))

    assert stats["assistant_turns"] == 3
    assert stats["tool_calls"] == 3
    assert stats["session_reported_turns"] == 0
    assert stats["session_reported_tool_calls"] == []
    # The disagreement is recorded rather than smoothed over.
    assert stats["session_fields_agree"] is False


# --------------------------------------------------------------------------- tool failure


def test_tool_failures_and_denials_are_counted_apart() -> None:
    data = conversation(
        [
            (
                [
                    tool_use("bash", {"command": "cat x"}, "c1"),
                    tool_use("dispatch_action", {"tool_id": "bash"}, "c2"),
                ],
                [
                    tool_result(
                        "action_denied: rule:allow-shell.execute, OWNERSHIP_UNCLASSIFIABLE_SHELL",
                        is_error=True,
                    ),
                    tool_result(
                        "tool_not_disclosed: 'bash' has not been disclosed by tool.search",
                        is_error=True,
                    ),
                ],
            ),
            ([tool_use("file_read", {"path": "a.py"}, "c3")], [tool_result("a")]),
        ]
    )
    stats = analysis.reconstruct_main_stats(
        data, [], MAIN, session(2, ["bash", "dispatch_action", "file_read"])
    )

    assert stats["tool_calls"] == 3
    assert stats["tool_errors"] == 2
    assert stats["tool_denials"] == 1
    assert stats["session_fields_agree"] is True


# --------------------------------------------------------------------------- transcript cross-check


def test_transcript_events_are_counted_from_the_trajectory_payloads() -> None:
    events = [
        {"event_type": "action_proposed"},
        {"event_type": "action_proposed"},
        {"event_type": "tool_observation"},
        {"event_type": "sandbox_lifecycle"},
    ]
    data = conversation([([tool_use("file_read", {"path": "a.py"}, "c1")], [tool_result("a")])])

    stats = analysis.reconstruct_main_stats(data, events, MAIN, session(1, [""]))

    assert stats["transcript_action_proposed"] == 2
    assert stats["transcript_tool_observations"] == 1


# --------------------------------------------------------------------------- labels


def test_goal_and_delegation_are_two_independent_dimensions() -> None:
    # A correct result reached without delegating is a pass, not a prompt failure.
    goal, behaviour = analysis._goal_label, analysis._delegation_labels  # noqa: SLF001

    assert goal(oracle="PASS", public_passed=True, run_status="succeeded") == "GOAL_PASS"
    assert goal(oracle="FAIL", public_passed=False, run_status="succeeded") == "GOAL_FAIL"
    assert goal(oracle=None, public_passed=False, run_status="blocked") == "GOAL_BLOCKED"

    direct, labels = behaviour(
        first_delegate=None,
        late=False,
        children=[],
        objective_requires_execution=False,
        integration_done=False,
        goal="GOAL_PASS",
    )
    assert direct == "DIRECT_EXECUTION"
    assert labels == ["DIRECT_EXECUTION"]

    # A delegation whose objective needed a shell is a capability error, and the label says so.
    invalid, applicable = behaviour(
        first_delegate=6,
        late=False,
        children=[{"success": False}],
        objective_requires_execution=True,
        integration_done=False,
        goal="GOAL_FAIL",
    )
    assert invalid == "INVALID_CAPABILITY_DELEGATION"
    assert "INVALID_CAPABILITY_DELEGATION" in applicable

    # GOAL_FAIL and EARLY_EFFECTIVE_DELEGATION remain logically separate: the behaviour label needs
    # both a successful child and an integration after it, so it is not awarded here either.
    late, late_labels = behaviour(
        first_delegate=17,
        late=True,
        children=[{"success": True}],
        objective_requires_execution=False,
        integration_done=True,
        goal="GOAL_FAIL",
    )
    assert late == "LATE_DELEGATION"
    assert "EARLY_EFFECTIVE_DELEGATION" not in late_labels
