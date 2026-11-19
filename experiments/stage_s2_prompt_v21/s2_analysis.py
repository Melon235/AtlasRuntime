"""Observable-behaviour analysis of one Stage S2 run.

Everything here is computed from artefacts the runtime already wrote: the Main's stored
conversation, the children's stored conversations, the trajectory transcript, ``session.json`` and
the delegation evidence. Nothing reads the model's private reasoning.

Why this module exists separately: Stage S1 discovered that ``session.json``'s ``main.turns`` and
``main.tool_calls`` are ``0`` / ``[]`` whenever a run ends on a budget rejection, because the agent
result carries an empty message list in that case. The run itself was fine; the summary field was
not. So the counters used for analysis are **reconstructed** from the stored conversation and
cross-checked against the transcript, and the session's own fields are recorded next to them rather
than trusted.

The two dimensions the stage is judged on are independent:

* ``goal`` -- did the coding task complete correctly;
* ``delegation`` -- what did the Main actually do about delegation, and was it appropriate.

A run may be ``GOAL_FAIL + EARLY_EFFECTIVE_DELEGATION`` and that is a meaningful outcome, not a
contradiction.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"file_read", "grep", "list_files"})
WRITE_TOOLS = frozenset({"file_write", "file_edit"})

#: Words that mean "this deliverable needs to execute something", which a read-only research worker
#: cannot do. Used to judge whether a delegated objective was even executable by its worker.
EXECUTION_MARKERS = (
    "python3",
    "python -m",
    "pytest",
    "sha256sum",
    "bash",
    "shell",
    "run the test",
    "run tests",
    "run `",
    "pip install",
    "git ",
    "which ",
    "--version",
)

#: A delegation is "late" when this much investigation already happened before it. Both numbers are
#: reported, and either one is enough: the point is that the decision came after the work.
LATE_READ_TARGETS = 8
LATE_TURN_FRACTION = 0.5


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _target(name: str, payload: dict[str, Any]) -> str:
    """A compact, comparable description of what a call was aimed at."""

    if name in READ_TOOLS | WRITE_TOOLS:
        value = payload.get("path") or payload.get("pattern") or "."
        return str(value)
    if name == "bash":
        command = str(payload.get("command") or payload.get("cmd") or "")
        return "bash: " + " ".join(command.split())[:160]
    if name == "dispatch_action":
        return "dispatch_action: " + str(payload.get("tool_id") or "?")
    if name == "tool_search":
        return "tool_search: " + str(payload.get("query") or "")
    if name == "delegate_tasks":
        tasks = payload.get("tasks") or []
        return "delegate_tasks: " + ",".join(str(item.get("key")) for item in tasks)
    if name == "delegate.read":
        return "delegate.read: " + str(payload.get("ref_id"))
    if name == "run_public_tests":
        return "run_public_tests"
    return name


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message, with its calls, their targets and their outcomes."""

    turns: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        calls = [
            {
                "name": block.get("name"),
                "input": block.get("input") or {},
                "target": _target(block.get("name") or "", block.get("input") or {}),
            }
            for block in message.get("content") or []
            if block.get("type") == "tool_use"
        ]
        results: list[dict[str, Any]] = []
        if index + 1 < len(messages) and messages[index + 1].get("role") == "user":
            for block in messages[index + 1].get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
                results.append(
                    {
                        "is_error": bool(block.get("is_error")),
                        "kind": _error_kind(text),
                        "chars": len(text or ""),
                        "preview": (text or "")[:120],
                    }
                )
        text = "".join(
            block.get("text") or ""
            for block in message.get("content") or []
            if block.get("type") == "text"
        )
        for position, call in enumerate(calls):
            outcome = results[position] if position < len(results) else None
            call["is_error"] = bool(outcome and outcome["is_error"])
            call["error_kind"] = outcome["kind"] if outcome else None
            call["result_preview"] = outcome["preview"] if outcome else ""
        turns.append(
            {
                "turn": len(turns) + 1,
                "tool_calls": calls,
                "text_chars": len(text),
                "text": text.strip()[:400],
            }
        )
    return turns


def _error_kind(text: str) -> str | None:
    if "action_denied" in text:
        return "denied"
    if "tool_not_disclosed" in text:
        return "undisclosed"
    if text.startswith("unknown tool"):
        return "unknown_tool"
    return None


def _paths(calls: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for call in calls:
        if call["name"] in READ_TOOLS:
            target = str(call["target"])
            if target not in seen:
                seen.append(target)
    return seen


def transcript_events(transcript_path: Path) -> list[dict[str, Any]]:
    """Flatten the trajectory transcript into ``atlas_trajectory`` payloads."""

    events: list[dict[str, Any]] = []
    if not transcript_path.is_file():
        return events
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        payload = row.get("payload") or {}
        event = payload.get("atlas_trajectory")
        if isinstance(event, dict):
            events.append(event)
    return events


def reconstruct_main_stats(
    conversations: dict[str, Any],
    events: list[dict[str, Any]],
    main_task_id: str,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Count the Main's turns and tool calls from the stored artefacts.

    This is the authoritative counter. ``session.main`` is recorded alongside so a disagreement is
    visible in the evidence instead of silently propagating into a report.
    """

    messages = ((conversations.get("tasks") or {}).get(main_task_id) or {}).get("messages") or []
    turns = _turns(messages)
    calls = [call for turn in turns for call in turn["tool_calls"]]
    by_name = Counter(call["name"] for call in calls)
    errors = [call for call in calls if call["is_error"]]
    denials = [call for call in calls if call["error_kind"] == "denied"]
    proposed = [event for event in events if event.get("event_type") == "action_proposed"]
    observations = [event for event in events if event.get("event_type") == "tool_observation"]
    reported = (session or {}).get("main") or {}
    # ``session.main.tool_calls`` is a list of tool names, ``session.main.turns`` a count of
    # assistant messages. Comparing them to the reconstruction is the whole point of this function.
    agree = reported.get("turns") == len(turns) and list(reported.get("tool_calls") or []) == [
        call["name"] for call in calls
    ]
    return {
        "assistant_turns": len(turns),
        "tool_calls": len(calls),
        "tool_calls_by_name": dict(sorted(by_name.items())),
        "tool_errors": len(errors),
        "tool_denials": len(denials),
        "transcript_action_proposed": len(proposed),
        "transcript_tool_observations": len(observations),
        "session_reported_turns": reported.get("turns"),
        "session_reported_tool_calls": reported.get("tool_calls"),
        "session_fields_agree": agree,
        "source": "tool_conversations.json + transcript.jsonl",
    }


def _child_tasks(conversations: dict[str, Any], main_task_id: str) -> dict[str, Any]:
    return {
        task_id: value
        for task_id, value in (conversations.get("tasks") or {}).items()
        if task_id != main_task_id
    }


def _delegation_evidence(session: dict[str, Any]) -> dict[str, Any]:
    delegation = session.get("delegation") or {}
    batches = delegation.get("batches") or []
    children = [child for batch in batches for child in batch.get("children") or []]
    return {
        "enabled": session.get("delegation_enabled"),
        "rounds": len(batches),
        "routes": [batch.get("route") for batch in batches],
        "reason_codes": [(batch.get("decision") or {}).get("reason_codes") for batch in batches],
        "blocked_gates": [(batch.get("decision") or {}).get("blocked_gates") for batch in batches],
        "policy_versions": [
            (batch.get("decision") or {}).get("policy_version") for batch in batches
        ],
        "max_wave_width": max(
            (int(batch.get("max_wave_width") or 0) for batch in batches), default=0
        ),
        "max_observed_concurrency": max(
            (int(batch.get("max_observed_concurrency") or 0) for batch in batches), default=0
        ),
        "result_chars": sum(int(batch.get("result_chars") or 0) for batch in batches),
        "children": [
            {
                "key": child.get("key"),
                "profile_id": child.get("profile_id"),
                "status": child.get("status"),
                "success": child.get("success"),
                "turns": child.get("turns"),
                "tool_calls": child.get("tool_calls"),
                "error_code": child.get("error_code"),
                "ref_id": child.get("ref_id"),
                "summary_chars": child.get("summary_chars"),
                "started_offset_ms": child.get("started_offset_ms"),
                "finished_offset_ms": child.get("finished_offset_ms"),
            }
            for child in children
        ],
        "proposals": [
            {
                "reason": (batch.get("proposal") or {}).get("reason"),
                "tasks": (batch.get("proposal") or {}).get("tasks"),
            }
            for batch in batches
        ],
    }


def _execution_markers(text: str) -> list[str]:
    lowered = (text or "").lower()
    return sorted({marker for marker in EXECUTION_MARKERS if marker in lowered})


def analyze_run(run_dir: str | Path) -> dict[str, Any]:
    """Build the S2 behavioural trace for one finished run directory."""

    root = Path(run_dir)
    session = _load(root / "session.json")
    conversations = _load(root / "tool_conversations.json")
    transcript_path = next(iter(sorted(root.glob("trajectory/*/transcript.jsonl"))), None)
    events = transcript_events(transcript_path) if transcript_path else []

    main_task_id = session["main"]["task_id"]
    messages = ((conversations.get("tasks") or {}).get(main_task_id) or {}).get("messages") or []
    turns = _turns(messages)
    counters = reconstruct_main_stats(conversations, events, main_task_id, session)

    delegate_turns = [
        turn["turn"]
        for turn in turns
        if any(call["name"] == "delegate_tasks" for call in turn["tool_calls"])
    ]
    first_delegate = delegate_turns[0] if delegate_turns else None

    before = [
        call
        for turn in turns
        if first_delegate is None or turn["turn"] < first_delegate
        for call in turn["tool_calls"]
    ]
    after = [
        call
        for turn in turns
        if first_delegate is not None and turn["turn"] > first_delegate
        for call in turn["tool_calls"]
    ]

    context_by_request = (session.get("metrics") or {}).get("main_context_tokens_by_request") or []
    context_at_delegation = (
        context_by_request[first_delegate - 1]
        if first_delegate is not None and len(context_by_request) >= first_delegate
        else None
    )

    children = _child_tasks(conversations, main_task_id)
    child_turns = {
        task_id: _turns(value.get("messages") or []) for task_id, value in children.items()
    }
    child_paths = {
        task_id: _paths([call for turn in turn_list for call in turn["tool_calls"]])
        for task_id, turn_list in child_turns.items()
    }
    child_calls = {
        task_id: [call for turn in turn_list for call in turn["tool_calls"]]
        for task_id, turn_list in child_turns.items()
    }
    all_child_paths = sorted({path for paths in child_paths.values() for path in paths})

    post_reads = _paths(after)
    reread_of_child_work = [
        {"path": path, "read_by": [task for task, paths in child_paths.items() if path in paths]}
        for path in post_reads
        if path in all_child_paths
    ]
    writes = [call for call in before + after if call["name"] in WRITE_TOOLS]
    delegate_reads = [call for call in before + after if call["name"] == "delegate.read"]
    public_test_calls = [call for call in before + after if call["name"] == "run_public_tests"]

    first_write_turn = next(
        (
            turn["turn"]
            for turn in turns
            if any(c["name"] in WRITE_TOOLS for c in turn["tool_calls"])
        ),
        None,
    )
    last_read_turn = next(
        (
            turn["turn"]
            for turn in reversed(turns)
            if any(c["name"] in READ_TOOLS for c in turn["tool_calls"])
        ),
        None,
    )

    delegation = _delegation_evidence(session)
    proposals = delegation["proposals"]
    objective_text = json.dumps(proposals, sort_keys=True) if proposals else ""
    markers = _execution_markers(objective_text)

    capability = {
        "child_profile_ids": sorted(
            {child["profile_id"] for child in delegation["children"] if child["profile_id"]}
        ),
        "child_tool_calls_by_name": {
            task_id: dict(sorted(Counter(call["name"] for call in calls).items()))
            for task_id, calls in child_calls.items()
        },
        "child_tool_search_calls": {
            task_id: sum(1 for call in calls if call["name"] == "tool_search")
            for task_id, calls in child_calls.items()
        },
        "child_tool_search_empty": {
            task_id: sum(
                1
                for call in child_calls[task_id]
                if call["name"] == "tool_search"
                and '"matches": []' in (call.get("result_preview") or "")
            )
            for task_id in child_calls
        },
        "child_dispatch_action_calls": {
            task_id: sum(1 for call in calls if call["name"] == "dispatch_action")
            for task_id, calls in child_calls.items()
        },
        "child_undisclosed_tool_refusals": {
            task_id: [call["target"] for call in calls if call["error_kind"] == "undisclosed"]
            for task_id, calls in child_calls.items()
        },
        "child_read_targets": child_paths,
        "child_used_repository_tools": {
            task_id: sum(1 for call in calls if call["name"] in READ_TOOLS)
            for task_id, calls in child_calls.items()
        },
        "objective_execution_markers": markers,
        "objective_requires_execution": bool(markers),
    }

    verification = session.get("verification") or {}
    public_tests = session.get("public_tests") or {}
    public_runs = public_tests.get("runs") or []
    public_passed = bool(public_runs) and public_runs[-1].get("status") == "PASS"
    files_changed = int((session.get("sandbox") or {}).get("patch_changed_files") or 0)
    oracle = verification.get("verdict")

    implementation = {
        "files_changed": files_changed,
        "first_write_turn": first_write_turn,
        "turns_before_first_write": (first_write_turn - 1) if first_write_turn else None,
        "last_read_turn": last_read_turn,
        "investigation_turns": sum(
            1 for turn in turns if any(c["name"] in READ_TOOLS for c in turn["tool_calls"])
        ),
        "write_turns": sum(
            1 for turn in turns if any(c["name"] in WRITE_TOOLS for c in turn["tool_calls"])
        ),
        "bash_calls": sum(1 for turn in turns for c in turn["tool_calls"] if c["name"] == "bash"),
        "bash_denials": sum(
            1
            for turn in turns
            for c in turn["tool_calls"]
            if c["name"] == "bash" and c["error_kind"] == "denied"
        ),
        "public_test_calls": len(public_test_calls),
        "public_test_statuses": [run.get("status") for run in public_runs],
        "stop_reason": (session.get("main") or {}).get("stop_reason"),
    }

    metrics = session.get("metrics") or {}
    late = bool(
        first_delegate is not None
        and (
            len(_paths(before)) >= LATE_READ_TARGETS
            or (turns and first_delegate > len(turns) * LATE_TURN_FRACTION)
        )
    )

    goal = _goal_label(oracle=oracle, public_passed=public_passed, run_status=session.get("status"))
    behaviour, applicable = _delegation_labels(
        first_delegate=first_delegate,
        late=late,
        children=delegation["children"],
        objective_requires_execution=capability["objective_requires_execution"],
        integration_done=bool(writes)
        and first_write_turn is not None
        and first_write_turn > (first_delegate or 0),
        goal=goal,
    )

    return {
        "stage": "S2",
        "run_id": session.get("run_id"),
        "prompt": session.get("main_prompt"),
        "main": {
            "output": (session.get("main") or {}).get("output"),
            "stop_reason": (session.get("main") or {}).get("stop_reason"),
            "stop_message": (session.get("main") or {}).get("stop_message"),
            "output_chars": len((session.get("main") or {}).get("output") or ""),
            "counters": counters,
        },
        "early_delegation": {
            "first_delegate_turn": first_delegate,
            "delegate_turns": delegate_turns,
            "total_turns": len(turns),
            "late": late,
            "late_rule": {
                "read_targets_threshold": LATE_READ_TARGETS,
                "turn_fraction": LATE_TURN_FRACTION,
            },
            "tool_calls_before_first_delegation": before,
            "tool_call_count_before_first_delegation": len(before),
            "read_targets_before_first_delegation": _paths(before),
            "read_target_count_before_first_delegation": len(_paths(before)),
            "main_context_tokens_by_request": context_by_request,
            "main_context_tokens_at_delegation_request": context_at_delegation,
            "turns": turns,
        },
        "capability": capability,
        "implementation": implementation,
        "handoff_utilisation": {
            "tool_calls_after_first_delegation": after,
            "read_targets_after_handoff": post_reads,
            "delegate_read_calls": delegate_reads,
            "reread_after_handoff_of_child_work": reread_of_child_work,
            "child_read_targets": child_paths,
            "main_write_targets": [call["target"] for call in writes],
            "public_test_calls": public_test_calls,
            "handoff_payload_chars": delegation["result_chars"],
            "handoff_reads_metrics": session.get("handoff_reads"),
        },
        "delegation": delegation,
        "metrics": metrics,
        "budget": session.get("budget"),
        "verification": verification,
        "public_tests": {
            "calls": public_tests.get("calls"),
            "statuses": [run.get("status") for run in public_runs],
            "command_digest": public_tests.get("command_digest"),
            "last_exit_code": public_runs[-1].get("exit_code") if public_runs else None,
        },
        "verdict": {
            "goal": goal,
            "delegation_behavior": behaviour,
            "delegation_labels_applicable": applicable,
            "oracle": oracle,
            "public_tests_passed": public_passed,
            "files_changed": files_changed,
            "run_status": session.get("status"),
            "failure": session.get("failure"),
        },
    }


def _goal_label(*, oracle: str | None, public_passed: bool, run_status: str | None) -> str:
    """Correctness dimension. Blocked means the preconditions were never met, not that we failed."""

    if run_status in (None, "blocked"):
        return "GOAL_BLOCKED"
    if oracle == "PASS" and public_passed:
        return "GOAL_PASS"
    return "GOAL_FAIL"


def _delegation_labels(
    *,
    first_delegate: int | None,
    late: bool,
    children: list[dict[str, Any]],
    objective_requires_execution: bool,
    integration_done: bool,
    goal: str,
) -> tuple[str, list[str]]:
    """Behaviour dimension, with every applicable label kept so nothing is hidden by precedence."""

    if first_delegate is None:
        return "DIRECT_EXECUTION", ["DIRECT_EXECUTION"]

    applicable: list[str] = []
    if objective_requires_execution:
        applicable.append("INVALID_CAPABILITY_DELEGATION")
    if late:
        applicable.append("LATE_DELEGATION")
    succeeded = [child for child in children if child.get("success")]
    if not succeeded or not integration_done:
        applicable.append("DELEGATION_EXECUTION_FAILED")
    if succeeded and integration_done and not late and goal == "GOAL_PASS":
        applicable.append("EARLY_EFFECTIVE_DELEGATION")

    for label in (
        "INVALID_CAPABILITY_DELEGATION",
        "DELEGATION_EXECUTION_FAILED",
        "LATE_DELEGATION",
        "EARLY_EFFECTIVE_DELEGATION",
    ):
        if label in applicable:
            return label, applicable
    return "DELEGATION_EXECUTION_FAILED", applicable or ["DELEGATION_EXECUTION_FAILED"]


__all__ = [
    "analyze_run",
    "reconstruct_main_stats",
    "transcript_events",
]
