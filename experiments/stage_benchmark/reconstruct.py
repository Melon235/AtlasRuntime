"""Reconstruction of one run, and aggregation across runs.

Two stages of this project were mis-summarised by reading ``session.json`` alone: Stage S1 reported
``main.turns = 0`` for a run that made eighteen requests (the agent result carried no messages
after a budget rejection), and Stage S2 reported fourteen turns for a conversation that stored
thirteen (the final text-only message is kept only as ``session.main.output``). Both runs were
fine; the summary field was not.

So this module never reads a single counter and believes it. It separates the three quantities that
were being conflated — provider requests, assistant turns, tool calls — rebuilds them from the
stored conversation and the trajectory, and records every disagreement with an explanation or as a
gap.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ANALYZER_VERSION = "stage-b-reconstruct/1.0"

#: Known, documented explanations for a turn-count difference. Anything else is an evidence gap.
_EXPLAINED = (
    "budget rejection: the agent result carried no messages, so the session counted 0 turns",
    "final text-only message: it carries no tool call and is not persisted in the conversation",
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_uses(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [block for block in message.get("content") or [] if block.get("type") == "tool_use"]


def _tool_results(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [block for block in message.get("content") or [] if block.get("type") == "tool_result"]


def walk_conversation(messages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Assistant turns and tool calls of one stored conversation, in order."""

    turns: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            text = "".join(
                block.get("text") or ""
                for block in message.get("content") or []
                if block.get("type") == "text"
            )
            uses = _tool_uses(message)
            turns.append(
                {
                    "index": len(turns) + 1,
                    "text": text,
                    "tool_calls": [
                        {"name": block.get("name"), "input": block.get("input") or {}}
                        for block in uses
                    ],
                }
            )
            for block in uses:
                calls.append({"name": block.get("name"), "input": block.get("input") or {}})
        elif role == "user":
            results = _tool_results(message)
            if results and turns:
                for index, result in enumerate(results):
                    if index < len(turns[-1]["tool_calls"]):
                        turns[-1]["tool_calls"][index]["is_error"] = bool(result.get("is_error"))
                        turns[-1]["tool_calls"][index]["result"] = result.get("content")
    for turn in turns:
        for call in turn["tool_calls"]:
            call.setdefault("is_error", False)
            call.setdefault("result", "")
    return {"turns": turns, "calls": calls}


def transcript_events(path: Path | None) -> list[dict[str, Any]]:
    """Every trajectory event, whatever shape the recorder used.

    The runtime's own recorder writes ``{"event": ...}``; the collected M5 transcript writes
    ``{"kind": "lifecycle", "payload": {"atlas_trajectory": {"event_type": ...}}}``. An analyser
    that understands only one of them silently counts zero of everything, which is exactly the
    class of defect this module exists to prevent.
    """

    if path is None or not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        inner = (item.get("payload") or {}).get("atlas_trajectory") or {}
        event = item.get("event") or inner.get("event_type") or item.get("kind")
        events.append({"event": event, "raw": item})
    return events


def _count(events: Iterable[Mapping[str, Any]], kind: str) -> int:
    return sum(1 for event in events if event.get("event") == kind)


def _explain_turn_delta(delta: int, session: Mapping[str, Any]) -> str | None:
    if delta == 0:
        return None
    main = session.get("main") or {}
    if delta < 0 and main.get("stop_reason") == "budget_exceeded":
        return _EXPLAINED[0]
    if delta == 1 and main.get("output"):
        return _EXPLAINED[1]
    return None


def reconstruct(run_dir: Path) -> dict[str, Any]:
    """One run, from its own artefacts."""

    session = _load(run_dir / "session.json")
    conversations = _load(run_dir / "tool_conversations.json")
    transcript = run_dir / "trajectory"
    transcript_path = next(iter(sorted(transcript.glob("*/transcript.jsonl"))), None)
    if transcript_path is None and (run_dir / "transcript.jsonl").is_file():
        transcript_path = run_dir / "transcript.jsonl"
    events = transcript_events(transcript_path)

    main_task_id = (session.get("main") or {}).get("task_id") or ""
    main_conversation = (conversations.get("tasks") or {}).get(main_task_id) or {}
    walked = walk_conversation(main_conversation.get("messages") or [])
    calls = walked["calls"]
    names = [call["name"] for call in calls]
    by_name: dict[str, int] = {}
    for name in names:
        by_name[name] = by_name.get(name, 0) + 1

    reported = session.get("main") or {}
    metrics = session.get("metrics") or {}
    delegation = session.get("delegation") or {}

    observed_turns = len(walked["turns"])
    reported_turns = int(reported.get("turns") or 0)
    turns_delta = reported_turns - observed_turns
    turn_gap = _explain_turn_delta(turns_delta, session)

    calls_agree = list(reported.get("tool_calls") or []) == names
    proposed = _count(events, "action_proposed")
    # The transcript covers the Main and every child, so the comparison is against all stored
    # conversations, not only the Main's.
    child_calls = sum(
        len(walk_conversation(payload.get("messages") or [])["calls"])
        for task_id, payload in (conversations.get("tasks") or {}).items()
        if task_id != main_task_id
    )
    transcript_agrees = proposed == len(names) + child_calls if events else True

    main_input = int(metrics.get("main_cumulative_input_tokens") or 0)
    main_completion = int(metrics.get("main_completion_tokens") or 0)
    child_input = int(metrics.get("child_total_prompt_tokens") or 0)
    child_completion = int(metrics.get("child_completion_tokens") or 0)
    system_total = metrics.get("system_total_tokens")
    usage_sum = main_input + main_completion + child_input + child_completion
    usage_agrees = system_total is not None and int(system_total) == usage_sum

    # A batch is one delegation round. Its ``children`` are the admitted tasks and their outcomes.
    batches = list(delegation.get("batches") or [])
    children = [child for batch in batches for child in (batch.get("children") or [])]
    routes = [batch.get("route") for batch in batches if batch.get("route")]
    rounds = len(batches)
    decided = sum(1 for batch in batches if batch.get("decision"))
    child_ids = [item.get("worker_id") for item in children]
    stored_tasks = set(conversations.get("tasks") or {})
    children_stored = all(child in stored_tasks for child in child_ids if child)
    router_agrees = decided == rounds
    concurrency = max([int(batch.get("max_observed_concurrency") or 0) for batch in batches] or [0])

    gaps: list[str] = []
    explained: list[str] = []
    if not calls_agree:
        if (
            not (reported.get("tool_calls") or [])
            and reported.get("stop_reason") == "budget_exceeded"
        ):
            explained.append(
                "session recorded no tool calls: the run ended on a budget rejection and the agent "
                "result carried an empty record list"
            )
        else:
            gaps.append(
                f"session reports {len(reported.get('tool_calls') or [])} tool calls, the "
                f"conversation holds {len(names)}"
            )
    if turns_delta and turn_gap is None:
        gaps.append(
            f"unexplained turn difference: session {reported_turns} vs stored {observed_turns}"
        )
    if not transcript_agrees:
        gaps.append(
            f"transcript proposes {proposed} actions, the conversations hold "
            f"{len(names) + child_calls} (main {len(names)} + children {child_calls})"
        )
    if not usage_agrees:
        gaps.append(f"usage sum {usage_sum} does not equal system_total {system_total}")
    if not router_agrees:
        gaps.append(f"{rounds} delegation rounds but {len(routes)} router decisions stored")
    if child_ids and not children_stored:
        gaps.append("a child id has no stored conversation")

    first_delegate = next(
        (
            turn["index"]
            for turn in walked["turns"]
            if any(c["name"] == "delegate_tasks" for c in turn["tool_calls"])
        ),
        None,
    )
    first_write = next(
        (
            turn["index"]
            for turn in walked["turns"]
            if any(c["name"] in {"file_edit", "file_write"} for c in turn["tool_calls"])
        ),
        None,
    )
    children_succeeded = sum(1 for item in children if item.get("success"))
    files_changed = (session.get("sandbox") or {}).get("patch_changed_files")
    if files_changed is None:
        files_changed = (session.get("workspace") or {}).get("changed_files")

    public_tests = session.get("public_tests") or {}
    runs = public_tests.get("runs") or []
    statuses = [str(run.get("status") or "").upper() for run in runs]
    if not statuses:
        statuses = [str(item).upper() for item in public_tests.get("statuses") or []]

    record = {
        "analyzer": {"version": ANALYZER_VERSION, "transcript_events": len(events)},
        "run": {
            "run_id": session.get("run_id"),
            "status": session.get("status"),
            "acceptance": session.get("acceptance"),
            "stop_reason": reported.get("stop_reason"),
        },
        "prompt": session.get("main_prompt") or {},
        "counts": {
            "provider_requests": metrics.get("main_requests"),
            "assistant_turns": observed_turns,
            "session_turns": reported_turns,
            "turns_delta": turns_delta,
            "turns_delta_reason": turn_gap,
            "tool_calls": len(names),
            "tool_calls_by_name": dict(sorted(by_name.items())),
            "tool_errors": sum(1 for call in calls if call.get("is_error")),
            "tool_denials": sum(
                1 for call in calls if "action_denied" in str(call.get("result") or "")
            ),
        },
        "consistency": {
            "tool_calls_agree": calls_agree,
            "turns_explained": turns_delta == 0 or turn_gap is not None,
            "transcript_agrees": transcript_agrees,
            "usage_sums_agree": usage_agrees,
            "router_rounds_agree": router_agrees,
            "child_ids_stored": children_stored,
            "gaps": gaps,
            "explained": explained,
        },
        "delegation": {
            "proposed": first_delegate is not None,
            "proposals": sum(1 for call in calls if call["name"] == "delegate_tasks"),
            "rounds": rounds,
            "routes": routes,
            "children": len(children),
            "children_succeeded": children_succeeded,
            "max_observed_concurrency": concurrency,
            "first_delegation_turn": first_delegate,
            "first_implementation_turn": first_write,
        },
        "context": {
            "main_peak_context_tokens": metrics.get("main_peak_context_tokens"),
            "main_cumulative_input_tokens": main_input or None,
            "main_cache_hit_tokens": metrics.get("main_cache_hit_tokens"),
            "main_cache_miss_tokens": metrics.get("main_cache_miss_tokens"),
            "child_total_prompt_tokens": child_input,
            "completion_tokens": main_completion + child_completion,
            "system_total_tokens": system_total,
            "handoff_payload_chars": metrics.get("handoff_payload_chars"),
            "wall_clock_seconds": session.get("wall_clock_seconds"),
        },
        "verification": {
            "public_test_calls": int(public_tests.get("calls") or 0),
            "public_test_statuses": statuses,
            "public_tests_passed": bool(statuses) and statuses[-1] == "PASS",
            "changed_files": files_changed,
            "ownership_denials": (session.get("ownership") or {}).get("denied_calls"),
            "oracle": ((session.get("verification") or {}).get("dynamic") or {}).get("status")
            or (session.get("verification") or {}).get("verdict"),
        },
    }
    record["verification"]["goal_passed"] = bool(
        record["verification"]["public_tests_passed"] and record["run"]["acceptance"] == "PASS"
    )
    record["failure_class"] = _failure_class(record)
    return record


def annotate(
    record: dict[str, Any], *, task_id: str, arm_id: str, repetition: int
) -> dict[str, Any]:
    """Attach the cell a record came from. The evidence does not know its own arm."""

    record["run"].update({"task_id": task_id, "arm_id": arm_id, "repetition": repetition})
    return record


def _failure_class(record: Mapping[str, Any]) -> str:
    if record["run"].get("acceptance") == "PASS":
        return "none"
    stop = record["run"].get("stop_reason") or ""
    if stop == "budget_exceeded":
        return "budget"
    if stop == "max_turns":
        return "turn_limit"
    if record["run"].get("status") == "failed" and not record["verification"]["public_test_calls"]:
        return "goal"
    return "goal"


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def aggregate(
    records: Iterable[Mapping[str, Any]], *, structure_of: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Rates and distributions per arm, then per structure.

    Every record counts. Nothing is filtered out for being unsuccessful: a benchmark that drops its
    failures measures nothing.
    """

    items = list(records)
    arms = sorted({item["run"].get("arm_id") for item in items if item["run"].get("arm_id")})
    summary: dict[str, Any] = {
        "attempts": len(items),
        "by_arm": {},
        "by_structure": {},
        "paired": [],
    }

    for arm in arms:
        rows = [item for item in items if item["run"].get("arm_id") == arm]
        total = len(rows)
        delegated = [row for row in rows if row["delegation"]["proposed"]]
        admitted = [row for row in delegated if row["delegation"]["routes"]]
        children = sum(row["delegation"]["children"] for row in rows)
        children_ok = sum(row["delegation"]["children_succeeded"] for row in rows)
        effective = sum(
            1
            for row in rows
            if row["delegation"]["children_succeeded"] > 0
            and (row["delegation"]["first_implementation_turn"] or 0)
            > (row["delegation"]["first_delegation_turn"] or 0)
        )
        summary["by_arm"][arm] = {
            "attempts": total,
            "oracle_pass_rate": _rate(
                sum(1 for r in rows if r["verification"]["oracle"] == "PASS"), total
            ),
            "goal_success_rate": _rate(
                sum(1 for r in rows if r["verification"]["goal_passed"]), total
            ),
            "budget_exhaustion_rate": _rate(
                sum(1 for r in rows if r["run"].get("stop_reason") == "budget_exceeded"), total
            ),
            "delegation_proposal_rate": _rate(len(delegated), total),
            "router_admission_rate": _rate(len(admitted), len(delegated)) if delegated else None,
            "child_completion_rate": _rate(children_ok, children) if children else None,
            "effective_delegation_rate": _rate(effective, total),
            "direct_execution_rate": _rate(total - len(delegated), total),
            "public_test_rate": _rate(
                sum(1 for r in rows if r["verification"]["public_tests_passed"]), total
            ),
            "main_peak_context_median": _median(
                [
                    r["context"]["main_peak_context_tokens"]
                    for r in rows
                    if r["context"]["main_peak_context_tokens"]
                ]
            ),
            "system_total_tokens_median": _median(
                [
                    r["context"]["system_total_tokens"]
                    for r in rows
                    if r["context"]["system_total_tokens"]
                ]
            ),
            "evidence_gaps": sum(len(r["consistency"]["gaps"]) for r in rows),
        }

    if structure_of:
        structures = sorted(set(structure_of.values()))
        for structure in structures:
            rows_of = [
                item for item in items if structure_of.get(item["run"].get("task_id")) == structure
            ]
            summary["by_structure"][structure] = {
                arm: {
                    "attempts": len([r for r in rows_of if r["run"].get("arm_id") == arm]),
                    "oracle_pass_rate": _rate(
                        sum(
                            1
                            for r in rows_of
                            if r["run"].get("arm_id") == arm
                            and r["verification"]["oracle"] == "PASS"
                        ),
                        len([r for r in rows_of if r["run"].get("arm_id") == arm]),
                    ),
                    "delegation_proposal_rate": _rate(
                        sum(
                            1
                            for r in rows_of
                            if r["run"].get("arm_id") == arm and r["delegation"]["proposed"]
                        ),
                        len([r for r in rows_of if r["run"].get("arm_id") == arm]),
                    ),
                }
                for arm in arms
            }

    pairs: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (item["run"].get("task_id"), item["run"].get("repetition"))
        pairs.setdefault(key, {})[item["run"].get("arm_id")] = item
    for (task_id, repetition), arms_of in sorted(pairs.items(), key=lambda pair: str(pair[0])):
        summary["paired"].append(
            {
                "task_id": task_id,
                "repetition": repetition,
                "oracle": {arm: arms_of[arm]["verification"]["oracle"] for arm in sorted(arms_of)},
                "delegated": {
                    arm: arms_of[arm]["delegation"]["proposed"] for arm in sorted(arms_of)
                },
                "peak_context": {
                    arm: arms_of[arm]["context"]["main_peak_context_tokens"]
                    for arm in sorted(arms_of)
                },
                "system_total_tokens": {
                    arm: arms_of[arm]["context"]["system_total_tokens"] for arm in sorted(arms_of)
                },
            }
        )
    return summary
