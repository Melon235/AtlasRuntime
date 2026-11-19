"""Observable-behaviour analysis of one Stage S1 run.

Everything here is computed from artefacts the runtime already wrote: the Main's stored conversation,
the children's stored conversations, ``session.json`` and the delegation evidence. Nothing reads the
model's private reasoning, and nothing is inferred that was not recorded.

The two questions that matter:

* when did the Main first propose delegation, and how much had it read by then;
* after the handoff, did it re-read what a worker had already investigated, and can that be justified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"file_read", "grep", "list_files"})
WRITE_TOOLS = frozenset({"file_write", "file_edit"})


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _target(name: str, payload: dict[str, Any]) -> str:
    """A compact, comparable description of what a call was aimed at."""

    if name in READ_TOOLS | WRITE_TOOLS:
        value = payload.get("path") or payload.get("pattern") or "."
        return str(value)
    if name == "bash":
        command = str(payload.get("command") or "")
        return "bash: " + " ".join(command.split())[:160]
    if name == "delegate_tasks":
        tasks = payload.get("tasks") or []
        return "delegate_tasks: " + ",".join(str(item.get("key")) for item in tasks)
    if name == "delegate.read":
        return "delegate.read: " + str(payload.get("ref_id"))
    if name == "run_public_tests":
        return "run_public_tests"
    return name


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message: its tool calls and their targets."""

    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        calls = [
            {"name": block.get("name"), "target": _target(block.get("name") or "", block.get("input") or {})}
            for block in message.get("content") or []
            if block.get("type") == "tool_use"
        ]
        text = "".join(
            block.get("text") or ""
            for block in message.get("content") or []
            if block.get("type") == "text"
        )
        turns.append({"turn": len(turns) + 1, "tool_calls": calls, "text_chars": len(text)})
    return turns


def _paths(calls: list[dict[str, Any]]) -> list[str]:
    """Distinct read targets, in first-use order."""

    seen: list[str] = []
    for call in calls:
        if call["name"] in READ_TOOLS:
            target = str(call["target"])
            if target not in seen:
                seen.append(target)
    return seen


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
        "reason_codes": [
            (batch.get("decision") or {}).get("reason_codes") for batch in batches
        ],
        "blocked_gates": [
            (batch.get("decision") or {}).get("blocked_gates") for batch in batches
        ],
        "policy_versions": [
            (batch.get("decision") or {}).get("policy_version") for batch in batches
        ],
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
    }


def analyze_run(run_dir: str | Path) -> dict[str, Any]:
    """Build the S1 behavioural trace for one finished run directory."""

    root = Path(run_dir)
    session = _load(root / "session.json")
    conversations = _load(root / "tool_conversations.json")
    main_task_id = session["main"]["task_id"]
    main_messages = (conversations.get("tasks") or {}).get(main_task_id, {}).get("messages") or []
    turns = _turns(main_messages)

    delegate_turns = [
        turn["turn"] for turn in turns if any(call["name"] == "delegate_tasks" for call in turn["tool_calls"])
    ]
    first_delegate = delegate_turns[0] if delegate_turns else None

    before = [call for turn in turns if first_delegate is None or turn["turn"] < first_delegate for call in turn["tool_calls"]]
    after = [call for turn in turns if first_delegate is not None and turn["turn"] > first_delegate for call in turn["tool_calls"]]

    context_by_request = (session.get("metrics") or {}).get("main_context_tokens_by_request") or []
    context_at_delegation = (
        context_by_request[first_delegate - 1]
        if first_delegate is not None and len(context_by_request) >= first_delegate
        else None
    )

    children = _child_tasks(conversations, main_task_id)
    child_paths: dict[str, list[str]] = {}
    for task_id, value in children.items():
        child_paths[task_id] = _paths(
            [call for turn in _turns(value.get("messages") or []) for call in turn["tool_calls"]]
        )
    all_child_paths = sorted({path for paths in child_paths.values() for path in paths})

    post_reads = _paths(after)
    reread_of_child_work = [
        {"path": path, "read_by": [task for task, paths in child_paths.items() if path in paths]}
        for path in post_reads
        if path in all_child_paths
    ]
    writes = [call["target"] for call in before + after if call["name"] in WRITE_TOOLS]
    delegate_reads = [call for call in before + after if call["name"] == "delegate.read"]
    public_test_calls = [call for call in before + after if call["name"] == "run_public_tests"]

    delegation = _delegation_evidence(session)
    verification = session.get("verification") or {}
    public_tests = session.get("public_tests") or {}
    public_runs = public_tests.get("runs") or []
    public_passed = bool(public_runs) and public_runs[-1].get("status") == "PASS"

    verdict_inputs = {
        "delegation_proposed": first_delegate is not None,
        "delegation_admitted": any(route in ("delegate_one", "delegate_parallel") for route in delegation["routes"]),
        "children_executed": sum(1 for child in delegation["children"] if child["status"] == "completed"),
        "children_succeeded": sum(1 for child in delegation["children"] if child["success"]),
        "max_observed_concurrency": delegation["max_observed_concurrency"],
        "handoff_read_used": len(delegate_reads) > 0,
        "public_tests_passed": public_passed,
        "oracle_verdict": verification.get("verdict"),
        "run_status": session.get("status"),
        "failure": session.get("failure"),
    }
    verdict_inputs["s1_decision_from_rules"] = _decision(verdict_inputs)

    return {
        "stage": "S1",
        "run_id": session.get("run_id"),
        "prompt": session.get("main_prompt"),
        "goal_digest_note": "see freeze.json; the goal is not repeated here",
        "main": {
            "turns": session["main"]["turns"],
            "tool_calls": session["main"]["tool_calls"],
            "stop_reason": session["main"]["stop_reason"],
            "stop_message": session["main"]["stop_message"],
            "output_chars": len(session["main"].get("output") or ""),
            "output": session["main"].get("output"),
        },
        "early_delegation": {
            "first_delegate_turn": first_delegate,
            "delegate_turns": delegate_turns,
            "tool_calls_before_first_delegation": before,
            "tool_call_count_before_first_delegation": len(before),
            "read_targets_before_first_delegation": _paths(before),
            "main_context_tokens_by_request": context_by_request,
            "main_context_tokens_at_delegation_request": context_at_delegation,
            "turns": turns,
        },
        "handoff_utilisation": {
            "tool_calls_after_first_delegation": after,
            "read_targets_after_handoff": post_reads,
            "delegate_read_calls": delegate_reads,
            "reread_after_handoff_of_child_work": reread_of_child_work,
            "child_read_targets": child_paths,
            "main_write_targets": writes,
            "public_test_calls": public_test_calls,
            "handoff_payload_chars": delegation["result_chars"],
            "handoff_reads_metrics": session.get("handoff_reads"),
        },
        "delegation": delegation,
        "metrics": session.get("metrics"),
        "budget": session.get("budget"),
        "verification": verification,
        "public_tests": {
            "calls": public_tests.get("calls"),
            "statuses": [run.get("status") for run in public_runs],
            "command_digest": public_tests.get("command_digest"),
            "last_exit_code": public_runs[-1].get("exit_code") if public_runs else None,
        },
        "verdict_inputs": verdict_inputs,
    }


def _decision(inputs: dict[str, Any]) -> str:
    """The stage's own classification, applied to observed facts only.

    The labels are the ones the stage specification defines; the rules are stated here so the label
    can be recomputed from the evidence rather than trusted.
    """

    oracle = inputs["oracle_verdict"]
    if inputs["run_status"] == "succeeded" and inputs["failure"] is None and not inputs["delegation_proposed"]:
        return "S1_V2_DIRECT_PASS" if oracle == "PASS" else "S1_V2_DIRECT_FAIL"
    if not inputs["delegation_proposed"]:
        return "S1_V2_DIRECT_FAIL" if oracle != "PASS" else "S1_V2_DIRECT_PASS"
    if inputs["failure"] is not None:
        return "S1_V2_RUNTIME_FAILED"
    delegated_ok = (
        inputs["delegation_admitted"]
        and inputs["children_succeeded"] >= 1
        and oracle == "PASS"
        and inputs["public_tests_passed"]
    )
    if delegated_ok:
        return "S1_V2_AUTONOMOUS_DELEGATION_PASS"
    return "S1_V2_DELEGATION_PARTIAL"


__all__ = ["analyze_run"]
