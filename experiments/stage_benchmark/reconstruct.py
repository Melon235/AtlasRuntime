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

Stage B-E1 tightened four semantics that a resolver alone could not make true:

* **Admission** is decided per *proposal*, from the Router's own decision record, not from "a route
  exists". ``route="direct"`` is a legitimate decision and admits nobody.
* **Delegation effectiveness** is a six-level ladder (L1 proposal … L6 goal correctness). Reaching
  L3 says a worker completed; it says nothing about the Main using it, and L6 is never attributed
  to the delegation.
* **Failure** is classified from evidence — status, stop reason, sandbox lifecycle, tool outcome —
  with the primary cause separated from contributing diagnostics, and ``unknown`` kept when the
  evidence does not decide.
* **Usage** is the same five-field decomposition the estimate uses, so a record can be costed
  without a second interpretation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ANALYZER_VERSION = "stage-b-reconstruct/1.1"

#: The provider reports no per-child cache split. ``UNKNOWN`` is recorded beside the child prompt
#: tokens so a costing model can price them without pretending they were measured.
CHILD_CACHE_VISIBILITY = "UNKNOWN"

#: Known, documented explanations for a turn-count difference. Anything else is an evidence gap.
_EXPLAINED = (
    "budget rejection: the agent result carried no messages, so the session counted 0 turns",
    "final text-only message: it carries no tool call and is not persisted in the conversation",
)

#: What a Router decision can amount to. ``NO_PROPOSAL`` is a run-level state, the rest are
#: per-proposal states.
PROPOSAL_STATES = (
    "NO_PROPOSAL",
    "PROPOSAL_REJECTED",
    "DELEGATE_ONE_ADMITTED",
    "DELEGATE_PARALLEL_ADMITTED",
    "ROUTER_RESULT_UNKNOWN",
)
_ADMITTING_ROUTES = {"delegate_one", "delegate_parallel"}

#: The delegation ladder. Each level is a separate fact; none implies the next.
LEVELS = (
    ("L1_proposal", "the Main called delegate_tasks"),
    ("L2_admission", "the Router admitted at least one worker"),
    ("L3_worker_completion", "every admitted worker completed successfully"),
    ("L4_handoff_delivery", "a handoff payload reached the Main"),
    ("L5_main_continuation", "the Main edited or tested after the handoff"),
    ("L6_goal_correctness", "public tests and the oracle passed"),
)

#: Commands a read-only research worker cannot run. Used only together with refusal evidence.
_EXECUTION_MARKERS = (
    "python3",
    "python ",
    "pytest",
    "sha256sum",
    "bash",
    "run_public_tests",
    "pip install",
    "subprocess",
)
_REFUSAL_MARKERS = ("tool_not_disclosed", "action_denied", "not disclosed by tool.search")


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
            turn_index = len(turns) + 1
            turn_calls = [
                {
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                    "turn": turn_index,
                }
                for block in uses
            ]
            turns.append({"index": turn_index, "text": text, "tool_calls": turn_calls})
            # The same dicts, not copies: results are attached to the turn's call, and a copy would
            # leave the flat list permanently resultless — which is how tool errors once read zero.
            calls.extend(turn_calls)
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


def _result_json(call: Mapping[str, Any]) -> dict[str, Any] | None:
    """The parsed JSON a tool returned, or ``None`` when the result was not JSON."""

    content = call.get("result")
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _explain_turn_delta(delta: int, session: Mapping[str, Any]) -> str | None:
    if delta == 0:
        return None
    main = session.get("main") or {}
    if delta < 0 and main.get("stop_reason") == "budget_exceeded":
        return _EXPLAINED[0]
    if delta == 1 and main.get("output"):
        return _EXPLAINED[1]
    return None


# --------------------------------------------------------------------------- admission


def proposal_state(batch: Mapping[str, Any] | None) -> str:
    """What one Router result actually was.

    A ``route`` string is not an admission. ``direct`` is a decision that admits nobody, and a
    batch whose children were all refused admits nobody either.
    """

    if not batch:
        return "ROUTER_RESULT_UNKNOWN"
    decision = batch.get("decision")
    if not isinstance(decision, Mapping) and not batch.get("route"):
        return "ROUTER_RESULT_UNKNOWN"
    route = (decision or {}).get("route") or batch.get("route")
    if not route:
        return "ROUTER_RESULT_UNKNOWN"
    declared = (decision or {}).get("children") or []
    admitted = [child for child in declared if child.get("admitted") is True]
    if not admitted:
        # A batch that produced children without a decision block still ran them.
        admitted = [child for child in (batch.get("children") or []) if child.get("worker_id")]
    if route in _ADMITTING_ROUTES and admitted:
        return (
            "DELEGATE_PARALLEL_ADMITTED"
            if route == "delegate_parallel"
            else ("DELEGATE_ONE_ADMITTED")
        )
    if route == "direct" or route in _ADMITTING_ROUTES:
        return "PROPOSAL_REJECTED"
    return "ROUTER_RESULT_UNKNOWN"


def _proposal_outcomes(
    delegate_calls: list[Mapping[str, Any]], batches: list[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pair each ``delegate_tasks`` call with the Router result that answered it."""

    outcomes: list[dict[str, Any]] = []
    gaps: list[str] = []
    for index, call in enumerate(delegate_calls):
        batch = batches[index] if index < len(batches) else None
        decision = (batch or {}).get("decision") or {}
        declared = decision.get("children") or []
        state = proposal_state(batch)
        if batch is None:
            gaps.append(f"proposal {index + 1} has no Router result")
        outcomes.append(
            {
                "proposal": index + 1,
                "turn": call.get("turn"),
                "state": state,
                "route": (decision.get("route") or (batch or {}).get("route")),
                "reason_codes": list(decision.get("reason_codes") or []),
                "blocked_gates": list(decision.get("blocked_gates") or []),
                "admitted_children": sum(1 for child in declared if child.get("admitted") is True),
                "declared_children": len(declared),
                "matched_batch": index < len(batches),
            }
        )
    if len(batches) > len(delegate_calls):
        gaps.append(
            f"{len(batches)} Router results for {len(delegate_calls)} proposals; the extra ones "
            "have no matching call"
        )
    return outcomes, gaps


def _admission_summary(outcomes: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {state: 0 for state in PROPOSAL_STATES}
    for outcome in outcomes:
        counts[outcome["state"]] = counts.get(outcome["state"], 0) + 1
    proposals = len(outcomes)
    admitted = counts.get("DELEGATE_ONE_ADMITTED", 0) + counts.get("DELEGATE_PARALLEL_ADMITTED", 0)
    return {
        "proposals": proposals,
        "admitted": admitted,
        "rejected": counts.get("PROPOSAL_REJECTED", 0),
        "unknown": counts.get("ROUTER_RESULT_UNKNOWN", 0),
        "by_state": counts,
        "state": "NO_PROPOSAL"
        if proposals == 0
        else (
            "ADMITTED"
            if admitted
            else ("REJECTED" if counts.get("PROPOSAL_REJECTED") else "ROUTER_RESULT_UNKNOWN")
        ),
    }


# --------------------------------------------------------------------------- handoff


def _child_files(conversations: Mapping[str, Any], main_task_id: str) -> dict[str, list[str]]:
    """Files each child actually read, by reading tool calls that succeeded."""

    files: dict[str, list[str]] = {}
    for task_id, payload in (conversations.get("tasks") or {}).items():
        if task_id == main_task_id:
            continue
        walked = walk_conversation(payload.get("messages") or [])
        touched = []
        for turn in walked["turns"]:
            for call in turn["tool_calls"]:
                if call["name"] in {"file_read", "grep"} and not call.get("is_error"):
                    path = str((call.get("input") or {}).get("path") or "")
                    if path:
                        touched.append(path)
        files[task_id] = touched
    return files


def _handoff_block(
    *,
    events_calls: list[Mapping[str, Any]],
    conversations: Mapping[str, Any],
    main_task_id: str,
    metrics: Mapping[str, Any],
    delegation_turn: int | None,
    children_succeeded: int,
) -> dict[str, Any]:
    delegate_calls = [call for call in events_calls if call["name"] == "delegate_tasks"]
    refs: list[str] = []
    delivered = False
    for call in delegate_calls:
        parsed = _result_json(call)
        handoff = (parsed or {}).get("handoff") or {}
        refs.extend(handoff.get("refs") or [])
        children = (parsed or {}).get("children") or []
        if handoff.get("refs") or any(child.get("summary") for child in children):
            delivered = True
    payload_chars = int(metrics.get("handoff_payload_chars") or 0)
    if payload_chars > 0:
        delivered = True

    after = [
        call
        for call in events_calls
        if delegation_turn is not None
        and call["turn"] is not None
        and call["turn"] > delegation_turn
        and call["name"] != "delegate_tasks"
    ]
    after_paths = [
        str((call.get("input") or {}).get("path") or "")
        for call in after
        if call["name"] in {"file_read", "grep", "list_files"}
        and (call.get("input") or {}).get("path")
    ]
    child_files = _child_files(conversations, main_task_id)
    all_child_files = {path for paths in child_files.values() for path in paths}
    overlap = sorted({path for path in after_paths if path in all_child_files})

    # Whether the Main *used* a child's finding is not observable from the stored artefacts: it
    # reads files a child also read, and the reasons are not recorded. The field exists so the
    # question is answered "unknown" on the record rather than assumed by a reader.
    observed_use: bool | None = None

    return {
        "delivered": delivered,
        # A payload can arrive and still be useless: a failed child returns a status sentence.
        "usable": bool(delivered and children_succeeded > 0),
        "observed_use": observed_use,
        "observed_use_basis": (
            "no stored evidence distinguishes an integration read from a duplicated one; a shared "
            "path is not proof that a finding was used"
        ),
        "refs": refs,
        "payload_chars": payload_chars,
        "main_calls_after_handoff": len(after),
        "main_reads_after_handoff": after_paths,
        "child_files": child_files,
        "overlap_files": overlap if all_child_files else None,
        "overlap_semantics": (
            "raw path overlap between a child's successful reads and the Main's post-handoff "
            "reads; redundancy requires review, a shared path is not evidence of it"
        ),
        "redundancy": "NOT_ASSESSED" if all_child_files else "UNKNOWN",
    }


# --------------------------------------------------------------------------- levels


def _levels(
    *,
    proposals: int,
    admitted: int,
    children_admitted: int,
    children_succeeded: int,
    handoff_delivered: bool,
    integration_after_handoff: bool,
    goal_passed: bool,
) -> dict[str, Any]:
    met = {
        "L1_proposal": proposals > 0,
        "L2_admission": admitted > 0,
        "L3_worker_completion": children_admitted > 0 and children_succeeded == children_admitted,
        "L4_handoff_delivery": bool(handoff_delivered),
        "L5_main_continuation": bool(integration_after_handoff),
        "L6_goal_correctness": bool(goal_passed),
    }
    reached = 0
    for name, _ in LEVELS:
        if met[name]:
            reached += 1
        else:
            break
    return {
        "met": met,
        "level_reached": reached,
        "definitions": {name: text for name, text in LEVELS},
        "note": (
            "each level is independent evidence; L6 is correctness of the run and is never "
            "attributed to the delegation"
        ),
    }


# --------------------------------------------------------------------------- capability


def _capability_block(
    *,
    delegate_calls: list[Mapping[str, Any]],
    conversations: Mapping[str, Any],
    main_task_id: str,
) -> dict[str, Any]:
    """Whether a delegated objective needed tools its worker did not have.

    The objectives come from the Main's own ``delegate_tasks`` arguments — that is what was asked
    for. Only two things together justify ``invalid``: the objective names a command the profile
    cannot run, and the child's own conversation shows refusals with no successful file read. A
    word in a task description is not evidence.
    """

    profiles: list[str] = []
    objectives: list[str] = []
    for call in delegate_calls:
        for task in (call.get("input") or {}).get("tasks") or []:
            if task.get("profile_id"):
                profiles.append(str(task["profile_id"]))
            if task.get("objective"):
                objectives.append(str(task["objective"]))
    marker_hits = sorted(
        {marker for marker in _EXECUTION_MARKERS for text in objectives if marker in text.lower()}
    )

    refusals = 0
    successful_reads = 0
    for task_id, payload in (conversations.get("tasks") or {}).items():
        if task_id == main_task_id:
            continue
        walked = walk_conversation(payload.get("messages") or [])
        for turn in walked["turns"]:
            for call in turn["tool_calls"]:
                result = str(call.get("result") or "")
                if any(marker in result for marker in _REFUSAL_MARKERS):
                    refusals += 1
                if call["name"] in {"file_read", "grep"} and not call.get("is_error"):
                    successful_reads += 1

    requires_execution = bool(marker_hits)
    if not profiles:
        invalid: bool | None = None
        basis = "no child was admitted, so no capability judgement is possible"
    elif requires_execution and refusals and not successful_reads:
        invalid = True
        basis = (
            "the objective names commands the profile cannot run and the child was refused "
            "without reading a single file"
        )
    elif requires_execution and successful_reads:
        invalid = False
        basis = "the objective names commands, but the child obtained the facts by reading"
    elif not requires_execution:
        invalid = False
        basis = "the objective names no command the profile cannot run"
    else:
        invalid = None
        basis = "the evidence does not decide whether the task was executable"
    return {
        "profiles": sorted(set(profiles)),
        "objective_execution_markers": marker_hits,
        "objective_requires_execution": requires_execution,
        "refusal_evidence": refusals,
        "successful_reads": successful_reads,
        "invalid": invalid,
        "basis": basis,
    }


# --------------------------------------------------------------------------- failure


def classify_failure(record: Mapping[str, Any], session: Mapping[str, Any]) -> dict[str, Any]:
    """Primary cause from evidence, contributing diagnostics kept apart."""

    run = record["run"]
    verification = record["verification"]
    sandbox = session.get("sandbox") or {}
    main = session.get("main") or {}
    stop = run.get("stop_reason") or ""
    status = run.get("status")
    evidence: dict[str, Any] = {
        "status": status,
        "acceptance": run.get("acceptance"),
        "stop_reason": stop or None,
        "sandbox_created": sandbox.get("created"),
        "sandbox_error": sandbox.get("error"),
        "public_test_calls": verification.get("public_test_calls"),
        "public_tests_passed": verification.get("public_tests_passed"),
        "oracle": verification.get("oracle"),
        "changed_files": verification.get("changed_files"),
        "tool_denials": record["counts"].get("tool_denials"),
        "evidence_gaps": len(record["consistency"].get("gaps") or []),
    }

    contributing: list[str] = []
    if record["counts"].get("tool_denials"):
        contributing.append(f"{record['counts']['tool_denials']} tool calls were denied")
    if record["consistency"].get("gaps"):
        contributing.append("the run has evidence gaps; see consistency.gaps")
    if sandbox.get("error"):
        contributing.append("the sandbox reported an error")

    # A suite that never ran is not a suite that failed. Only an explicit FAIL status, or an oracle
    # FAIL, is evidence about the goal; "no verification happened" is a gap, not a verdict.
    statuses = [
        str(item).upper() for item in (verification.get("public_test_statuses") or []) if item
    ]
    last_suite = statuses[-1] if statuses else None
    suite_failed = last_suite == "FAIL"
    oracle_failed = verification.get("oracle") == "FAIL"

    if run.get("acceptance") == "PASS":
        primary = "none"
        confidence = "HIGH"
    elif status == "blocked":
        primary = "blocked"
        confidence = "HIGH"
    elif sandbox and sandbox.get("created") is False:
        primary = "sandbox"
        confidence = "HIGH"
    elif stop == "budget_exceeded":
        primary = "budget"
        confidence = "HIGH"
    elif stop == "max_turns":
        primary = "turn_limit"
        confidence = "HIGH"
    elif stop in {"side_effect_unknown", "tool_error", "tool_failure"}:
        primary = "tool"
        confidence = "HIGH"
    elif status == "failed" and _runtime_signature(session):
        primary = "runtime"
        confidence = "MEDIUM"
    elif suite_failed or oracle_failed:
        primary = "goal"
        confidence = "MEDIUM" if last_suite else "HIGH"
    else:
        primary = "unknown"
        confidence = "LOW"

    if primary in {"budget", "turn_limit"} and (verification.get("changed_files") or 0) == 0:
        contributing.append("the run stopped before changing any file")
    return {
        "primary": primary,
        "contributing": contributing,
        "confidence": confidence,
        "evidence": evidence,
        "stop_reason": main.get("stop_reason"),
    }


def _runtime_signature(session: Mapping[str, Any]) -> bool:
    integrity = session.get("execution_integrity") or {}
    failure = session.get("failure")
    return bool(integrity.get("write_failures")) or (
        isinstance(failure, Mapping) and failure.get("kind") in {"runtime", "internal"}
    )


def _failure_class(record: Mapping[str, Any]) -> str:
    """Backwards-compatible accessor: the primary class of a finished record."""

    failure = record.get("failure")
    if isinstance(failure, Mapping):
        return str(failure.get("primary"))
    return "unknown"


# --------------------------------------------------------------------------- reconstruction


def reconstruct(run_dir: Path) -> dict[str, Any]:
    """One run, from its own artefacts."""

    session = _load(run_dir / "session.json")
    conversations = _load(run_dir / "tool_conversations.json")
    transcript_path = next(iter(sorted((run_dir / "trajectory").glob("*/transcript.jsonl"))), None)
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
    proposed_actions = _count(events, "action_proposed")
    child_calls = sum(
        len(walk_conversation(payload.get("messages") or [])["calls"])
        for task_id, payload in (conversations.get("tasks") or {}).items()
        if task_id != main_task_id
    )
    transcript_agrees = proposed_actions == len(names) + child_calls if events else True

    main_input = int(metrics.get("main_cumulative_input_tokens") or 0)
    main_completion = int(metrics.get("main_completion_tokens") or 0)
    child_input = int(metrics.get("child_total_prompt_tokens") or 0)
    child_completion = int(metrics.get("child_completion_tokens") or 0)
    system_total = metrics.get("system_total_tokens")
    usage_sum = main_input + main_completion + child_input + child_completion
    usage_agrees = system_total is not None and int(system_total) == usage_sum

    # ``main input = hit + miss``. When the provider reports both halves, the identity is checked
    # rather than assumed; when it reports neither, the split is UNKNOWN and the cumulative input is
    # still the one authoritative input count.
    hit_reported = metrics.get("main_cache_hit_tokens")
    miss_reported = metrics.get("main_cache_miss_tokens")
    input_reported = metrics.get("main_cumulative_input_tokens")
    if hit_reported is None or miss_reported is None:
        cache_split_state = "UNKNOWN"
        cache_split_agrees: bool | None = None
    else:
        cache_split_state = "COMPLETE"
        cache_split_agrees = (
            None
            if input_reported is None
            else int(hit_reported) + int(miss_reported) == int(input_reported)
        )

    batches = list(delegation.get("batches") or [])
    children = [child for batch in batches for child in (batch.get("children") or [])]
    child_ids = [item.get("worker_id") for item in children]
    stored_tasks = set(conversations.get("tasks") or {})
    children_stored = all(child in stored_tasks for child in child_ids if child)
    concurrency = max([int(batch.get("max_observed_concurrency") or 0) for batch in batches] or [0])

    delegate_calls = [call for call in calls if call["name"] == "delegate_tasks"]
    outcomes, admission_gaps = _proposal_outcomes(delegate_calls, batches)
    admission = _admission_summary(outcomes)

    first_delegate = delegate_calls[0]["turn"] if delegate_calls else None
    first_write = next(
        (
            turn["index"]
            for turn in walked["turns"]
            if any(c["name"] in {"file_edit", "file_write"} for c in turn["tool_calls"])
        ),
        None,
    )
    children_succeeded = sum(1 for item in children if item.get("success"))
    children_admitted = sum(outcome["admitted_children"] for outcome in outcomes)

    files_changed = (session.get("sandbox") or {}).get("patch_changed_files")
    if files_changed is None:
        files_changed = (session.get("workspace") or {}).get("changed_files")

    public_tests = session.get("public_tests") or {}
    runs = public_tests.get("runs") or []
    statuses = [str(run.get("status") or "").upper() for run in runs]
    if not statuses:
        statuses = [str(item).upper() for item in public_tests.get("statuses") or []]

    test_outcomes = _public_test_outcomes(calls)
    first_test_failure = next(
        (item["turn"] for item in test_outcomes if item["status"] == "FAIL"), None
    )
    post_failure_edits = (
        sum(
            1
            for call in calls
            if call["name"] in {"file_edit", "file_write"}
            and first_test_failure is not None
            and call["turn"] > first_test_failure
        )
        if first_test_failure is not None
        else 0
    )

    handoff = _handoff_block(
        events_calls=calls,
        conversations=conversations,
        main_task_id=main_task_id,
        metrics=metrics,
        delegation_turn=first_delegate,
        children_succeeded=children_succeeded,
    )
    integration_after_handoff = any(
        call["name"] in {"file_edit", "file_write", "run_public_tests"}
        and first_delegate is not None
        and call["turn"] > first_delegate
        for call in calls
    )

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
            f"transcript proposes {proposed_actions} actions, the conversations hold "
            f"{len(names) + child_calls} (main {len(names)} + children {child_calls})"
        )
    if not usage_agrees:
        gaps.append(f"usage sum {usage_sum} does not equal system_total {system_total}")
    if cache_split_agrees is False:
        gaps.append(
            f"cache split {int(hit_reported)} + {int(miss_reported)} does not equal the main "
            f"cumulative input {int(input_reported)}"
        )
    if admission_gaps:
        gaps.extend(admission_gaps)
    if child_ids and not children_stored:
        gaps.append("a child id has no stored conversation")

    record: dict[str, Any] = {
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
            "cache_split_state": cache_split_state,
            "cache_split_agrees": cache_split_agrees,
            "child_cache_visibility": CHILD_CACHE_VISIBILITY,
            "proposals_match_decisions": not admission_gaps,
            "child_ids_stored": children_stored,
            "gaps": gaps,
            "explained": explained,
        },
        "delegation": {
            "proposed": bool(delegate_calls),
            "proposals": len(delegate_calls),
            "rounds": len(batches),
            "routes": [outcome["route"] for outcome in outcomes if outcome.get("route")],
            "children": len(children),
            "children_admitted": children_admitted,
            "children_succeeded": children_succeeded,
            "max_observed_concurrency": concurrency,
            "first_delegation_turn": first_delegate,
            "first_implementation_turn": first_write,
            "admission": admission,
            "proposal_outcomes": outcomes,
            "handoff_delivered": handoff["delivered"],
            "handoff_usable": handoff["usable"],
            "integration_after_handoff": integration_after_handoff,
            # Where the evidence stops. Reported, so no summary has to invent the step after it.
            "benefit": {
                "attribution": "UNKNOWN",
                "reason": (
                    "the record shows ordering and completion, not that the delegation caused the "
                    "outcome; L6 is the run's correctness, not a delegation effect"
                ),
            },
        },
        "handoff": {
            "payload_chars": handoff["payload_chars"],
            "refs": handoff["refs"],
            "delivered": handoff["delivered"],
            "usable": handoff["usable"],
            "observed_use": handoff["observed_use"],
            "observed_use_basis": handoff["observed_use_basis"],
            "main_calls_after_handoff": handoff["main_calls_after_handoff"],
            "main_reads_after_handoff": handoff["main_reads_after_handoff"],
            "child_files": handoff["child_files"],
            "overlap_files": handoff["overlap_files"],
            "overlap_semantics": handoff["overlap_semantics"],
            "redundancy": handoff["redundancy"],
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
        "usage": {
            # ``None`` is not zero: an unreported half of a cache split stays unknown so a costing
            # model can substitute an assumption explicitly instead of reading a measurement.
            "main_prompt_hit_tokens": None if hit_reported is None else int(hit_reported),
            "main_prompt_miss_tokens": None if miss_reported is None else int(miss_reported),
            "child_prompt_tokens": child_input,
            "main_completion_tokens": main_completion,
            "child_completion_tokens": child_completion,
            "main_input_tokens": None if input_reported is None else int(input_reported),
            "child_input_tokens": child_input,
            "cache_split_state": cache_split_state,
            "child_cache_visibility": CHILD_CACHE_VISIBILITY,
            "sum_matches_system_total": usage_agrees,
        },
        "verification": {
            "public_test_calls": int(public_tests.get("calls") or 0),
            "public_test_statuses": statuses,
            "public_test_outcomes": test_outcomes,
            "public_tests_passed": bool(statuses) and statuses[-1] == "PASS",
            "post_failure_edits": post_failure_edits,
            "first_public_test_failure_turn": first_test_failure,
            "changed_files": files_changed,
            "ownership_denials": (session.get("ownership") or {}).get("denied_calls"),
            "oracle": ((session.get("verification") or {}).get("dynamic") or {}).get("status")
            or (session.get("verification") or {}).get("verdict"),
        },
    }
    record["verification"]["goal_passed"] = bool(
        record["verification"]["public_tests_passed"] and record["run"]["acceptance"] == "PASS"
    )
    record["capability"] = _capability_block(
        delegate_calls=delegate_calls, conversations=conversations, main_task_id=main_task_id
    )
    record["delegation"]["levels"] = _levels(
        proposals=len(delegate_calls),
        admitted=admission["admitted"],
        children_admitted=children_admitted,
        children_succeeded=children_succeeded,
        handoff_delivered=handoff["usable"],
        integration_after_handoff=integration_after_handoff,
        goal_passed=record["verification"]["goal_passed"],
    )
    record["failure"] = classify_failure(record, session)
    record["failure_class"] = record["failure"]["primary"]
    return record


def _public_test_outcomes(calls: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each ``run_public_tests`` call with the status its own result reported."""

    outcomes: list[dict[str, Any]] = []
    for call in calls:
        if call["name"] != "run_public_tests":
            continue
        parsed = _result_json(call) or {}
        exit_code = parsed.get("exit_code")
        status = parsed.get("status")
        if isinstance(status, str) and status.upper() in {"PASS", "FAIL"}:
            resolved = status.upper()
        elif isinstance(exit_code, int):
            resolved = "PASS" if exit_code == 0 else "FAIL"
        else:
            resolved = "UNKNOWN"
        outcomes.append(
            {
                "turn": call.get("turn"),
                "status": resolved,
                "exit_code": exit_code if isinstance(exit_code, int) else None,
            }
        )
    return outcomes


def annotate(
    record: dict[str, Any], *, task_id: str, arm_id: str, repetition: int
) -> dict[str, Any]:
    """Attach the cell a record came from. The evidence does not know its own arm."""

    record["run"].update({"task_id": task_id, "arm_id": arm_id, "repetition": repetition})
    return record


# --------------------------------------------------------------------------- aggregation


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


def _level_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each level with its own numerator and denominator. No level implies another."""

    rows = list(rows)
    with_proposal = [row for row in rows if row["delegation"]["proposed"]]
    admitted = [row for row in with_proposal if row["delegation"]["admission"]["admitted"]]
    completed = [
        row for row in admitted if row["delegation"]["levels"]["met"]["L3_worker_completion"]
    ]
    delivered = [
        row for row in completed if row["delegation"]["levels"]["met"]["L4_handoff_delivery"]
    ]
    continued = [
        row for row in delivered if row["delegation"]["levels"]["met"]["L5_main_continuation"]
    ]
    correct = [
        row for row in continued if row["delegation"]["levels"]["met"]["L6_goal_correctness"]
    ]
    return {
        "L1_proposal": {"numerator": len(with_proposal), "denominator": len(rows)},
        "L2_admission": {"numerator": len(admitted), "denominator": len(with_proposal)},
        "L3_worker_completion": {"numerator": len(completed), "denominator": len(admitted)},
        "L4_handoff_delivery": {"numerator": len(delivered), "denominator": len(completed)},
        "L5_main_continuation": {"numerator": len(continued), "denominator": len(delivered)},
        "L6_goal_correctness": {"numerator": len(correct), "denominator": len(continued)},
    }


def aggregate(
    records: Iterable[Mapping[str, Any]], *, structure_of: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Rates and distributions per arm, then per structure.

    Every record counts. Nothing is filtered out for being unsuccessful: a benchmark that drops its
    failures measures nothing. Rates whose denominator is zero are ``None`` (UNKNOWN), never 0.
    """

    items = list(records)
    arms = sorted({item["run"].get("arm_id") for item in items if item["run"].get("arm_id")})
    summary: dict[str, Any] = {
        "analyzer": ANALYZER_VERSION,
        "attempts": len(items),
        "by_arm": {},
        "by_structure": {},
        "paired": [],
    }

    for arm in arms:
        rows = [item for item in items if item["run"].get("arm_id") == arm]
        total = len(rows)
        proposals = sum(row["delegation"]["proposals"] for row in rows)
        admitted = sum(row["delegation"]["admission"]["admitted"] for row in rows)
        children = sum(row["delegation"]["children_admitted"] for row in rows)
        children_ok = sum(row["delegation"]["children_succeeded"] for row in rows)
        levels = _level_counts(rows)
        mechanism = sum(
            1
            for row in rows
            if all(
                row["delegation"]["levels"]["met"][level]
                for level in ("L2_admission", "L3_worker_completion", "L4_handoff_delivery")
            )
            and row["delegation"]["levels"]["met"]["L5_main_continuation"]
        )
        with_proposal = sum(1 for row in rows if row["delegation"]["proposed"])
        effective = sum(
            1
            for row in rows
            if row["delegation"]["levels"]["met"]["L1_proposal"]
            and all(
                row["delegation"]["levels"]["met"][level]
                for level in (
                    "L2_admission",
                    "L3_worker_completion",
                    "L4_handoff_delivery",
                    "L5_main_continuation",
                    "L6_goal_correctness",
                )
            )
        )
        summary["by_arm"][arm] = {
            "attempts": total,
            "oracle_pass_rate": _rate(
                sum(1 for row in rows if row["verification"]["oracle"] == "PASS"), total
            ),
            "goal_success_rate": _rate(
                sum(1 for row in rows if row["verification"]["goal_passed"]), total
            ),
            "budget_exhaustion_rate": _rate(
                sum(1 for row in rows if row["run"].get("stop_reason") == "budget_exceeded"), total
            ),
            "runtime_failure_rate": _rate(
                sum(1 for row in rows if row["failure"]["primary"] in {"runtime", "sandbox"}),
                total,
            ),
            "unknown_failure_rate": _rate(
                sum(1 for row in rows if row["failure"]["primary"] == "unknown"), total
            ),
            "delegation_proposal_rate": _rate(with_proposal, total),
            "router_admission_rate": _rate(admitted, proposals),
            "child_completion_rate": _rate(children_ok, children) if children else None,
            "delegation_mechanism_completion_rate": _rate(mechanism, with_proposal),
            "effective_delegation_rate": _rate(effective, with_proposal),
            "direct_execution_rate": _rate(total - with_proposal, total),
            "levels": {
                level: {
                    "numerator": counts["numerator"],
                    "denominator": counts["denominator"],
                    "rate": _rate(counts["numerator"], counts["denominator"]),
                }
                for level, counts in levels.items()
            },
            "public_test_rate": _rate(
                sum(1 for row in rows if row["verification"]["public_tests_passed"]), total
            ),
            "main_peak_context_median": _median(
                [
                    row["context"]["main_peak_context_tokens"]
                    for row in rows
                    if row["context"]["main_peak_context_tokens"]
                ]
            ),
            "system_total_tokens_median": _median(
                [
                    row["context"]["system_total_tokens"]
                    for row in rows
                    if row["context"]["system_total_tokens"]
                ]
            ),
            "evidence_gaps": sum(len(row["consistency"]["gaps"]) for row in rows),
        }

    if structure_of:
        for structure in sorted(set(structure_of.values())):
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
                "level_reached": {
                    arm: arms_of[arm]["delegation"]["levels"]["level_reached"]
                    for arm in sorted(arms_of)
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
