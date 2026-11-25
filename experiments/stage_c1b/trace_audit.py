"""Stage C1-B trace audit: what the three agents actually did, per turn.

Read-only. The auditor never touches the evidence tree; it reads the run's recorded files and
derives counts from them. Every number in the Stage C1-B report comes from here.

The classification is deliberately crude and *stated*, because the alternative is a judgement
dressed up as a measurement:

``productive``
    a turn containing at least one accepted ``file_write`` / ``file_edit``.
``discovery``
    a turn whose successful calls are reads only (``file_read``, ``list_files``, ``grep``,
    ``tool_search``).
``verification``
    a turn whose calls are ``bash`` / ``run_public_tests`` / ``delegate.read`` only. A shell call
    the ownership policy refused is still a verification attempt: the agent tried to verify.
``delegation``
    a turn containing ``delegate_tasks``.
``refused_only``
    every call in the turn was denied.

A turn with several kinds is counted once per kind it belongs to and reported as mixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"file_read", "list_files", "grep", "tool_search"})
WRITE_TOOLS = frozenset({"file_write", "file_edit"})
SHELL_TOOLS = frozenset({"bash", "run_public_tests", "delegate.read"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _decisions(transcript: Path) -> dict[str, dict[str, Any]]:
    """call_id -> {allowed, worker_id, tool_id, at} from the recorded decisions."""

    out: dict[str, dict[str, Any]] = {}
    if not transcript.is_file():
        return out
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        event = (record.get("payload") or {}).get("atlas_trajectory") or {}
        if event.get("event_type") != "action_decision":
            continue
        correlation = event.get("correlation") or {}
        call_id = correlation.get("call_id")
        if not call_id:
            continue
        data = event.get("data") or {}
        out[str(call_id)] = {
            "allowed": str(data.get("decision") or data.get("outcome") or "").lower()
            in {"allow", "allowed", "permit"},
            "decision": data.get("decision") or data.get("outcome"),
            "reason_codes": list(data.get("reason_codes") or ()),
            "worker_id": correlation.get("worker_id"),
            "tool_id": correlation.get("tool_id"),
            "occurred_at": event.get("occurred_at"),
        }
    return out


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message, including text-only turns.

    A turn that carries no tool call still consumed the turn budget: the children's last turn is
    exactly that (one produced an 88-character sentence, the other nothing).
    """

    turns: list[dict[str, Any]] = []
    index = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        index += 1
        parts = message.get("content") or []
        text = " ".join(str(part.get("text") or "") for part in parts if part.get("text")).strip()
        calls = [part for part in parts if part.get("name")]
        turns.append({"turn": index, "text": text, "calls": calls})
    return turns


def _results(messages: list[dict[str, Any]]) -> dict[str, str]:
    """call_id -> result text (the projection the agent actually saw)."""

    out: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "user":
            continue
        for part in message.get("content") or []:
            call_id = part.get("tool_use_id")
            if not call_id:
                continue
            content = part.get("content")
            out[str(call_id)] = content if isinstance(content, str) else _canonical(content)
    return out


def _denials(transcript: Path) -> list[dict[str, Any]]:
    """Every DENY the policy layer recorded, with the rule that produced it."""

    out: list[dict[str, Any]] = []
    if not transcript.is_file():
        return out
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = (json.loads(line).get("payload") or {}).get("atlas_trajectory") or {}
        if event.get("event_type") != "action_decision":
            continue
        data = event.get("data") or {}
        if str(data.get("decision") or "").upper() != "DENY":
            continue
        correlation = event.get("correlation") or {}
        out.append(
            {
                "worker_id": correlation.get("worker_id"),
                "tool_id": correlation.get("tool_id"),
                "call_id": correlation.get("call_id"),
                "rule": data.get("policy_rule_id"),
                "reason_codes": list(data.get("reason_codes") or ()),
            }
        )
    return out


def _classify(call: dict[str, Any], decision: dict[str, Any] | None) -> str:
    tool = str(call.get("name") or "")
    if tool in WRITE_TOOLS:
        return "write"
    if tool in READ_TOOLS:
        return "read"
    if tool in SHELL_TOOLS:
        return "shell"
    if tool in {"delegate_tasks", "delegate.read"}:
        return "delegate"
    return "other"


def audit_agent(
    task_id: str,
    messages: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    *,
    totals: dict[str, Any] | None = None,
    per_request_context: list[int] | None = None,
) -> dict[str, Any]:
    turns = _turns(messages)
    results = _results(messages)
    rows: list[dict[str, Any]] = []
    seen: dict[str, list[int]] = {}

    for turn in turns:
        calls: list[dict[str, Any]] = []
        for call in turn["calls"]:
            call_id = str(call.get("id") or "")
            decision = decisions.get(call_id)
            kind = _classify(call, decision)
            allowed = None if decision is None else bool(decision["allowed"])
            signature = _digest([call.get("name"), call.get("input") or {}])
            seen.setdefault(signature, []).append(turn["turn"])
            calls.append(
                {
                    "call_id": call_id,
                    "tool": call.get("name"),
                    "kind": kind,
                    "allowed": allowed,
                    "reason_codes": list((decision or {}).get("reason_codes") or ()),
                    "signature": signature,
                    "paths": _paths(call),
                    "command": _command(call),
                    "result_head": (results.get(call_id) or "")[:160],
                    "denied": (allowed is False)
                    or (results.get(call_id) or "").startswith("action_denied"),
                }
            )
        kinds = {item["kind"] for item in calls}
        writes = [item for item in calls if item["kind"] == "write" and not item["denied"]]
        refusals = [item for item in calls if item["denied"]]
        labels: list[str] = []
        if writes:
            labels.append("productive")
        if kinds & {"read"} and not writes:
            labels.append("discovery")
        if kinds & {"shell"}:
            labels.append("verification")
        if "delegate" in kinds:
            labels.append("delegation")
        if calls and len(refusals) == len(calls):
            labels.append("refused_only")
        rows.append(
            {
                "turn": turn["turn"],
                "text": turn["text"][:200],
                "labels": labels,
                "calls": calls,
                "writes": [item["paths"] for item in writes],
                "denied": len(refusals),
            }
        )

    write_turns = [row["turn"] for row in rows if row["writes"]]
    read_turns = [row["turn"] for row in rows if "discovery" in row["labels"]]
    verify_turns = [row["turn"] for row in rows if "verification" in row["labels"]]
    refused_calls = sum(1 for row in rows for item in row["calls"] if item["denied"])
    repeated = {
        signature: turns_seen for signature, turns_seen in seen.items() if len(turns_seen) > 1
    }
    repeated_calls = sum(len(value) - 1 for value in repeated.values())
    python_attempts = [
        row["turn"]
        for row in rows
        for item in row["calls"]
        if item["kind"] == "shell" and "python" in str(item["command"] or "")
    ]
    git_checks = [
        row["turn"]
        for row in rows
        for item in row["calls"]
        if item["kind"] == "shell" and "git " in str(item["command"] or "")
    ]
    last_write = write_turns[-1] if write_turns else None
    after_write = [row for row in rows if last_write is not None and row["turn"] > last_write]

    token_after: int | None = None
    if per_request_context and last_write is not None:
        # The Main's per-request context sizes are recorded in order; request i corresponds to the
        # (i+1)-th model call, so the turns after the last write are the trailing entries.
        token_after = sum(per_request_context[last_write:])

    return {
        "task_id": task_id,
        "turns": len(rows),
        "productive_turns": len(write_turns),
        "discovery_turns": len(read_turns),
        "verification_turns": len(verify_turns),
        "refused_calls": refused_calls,
        "repeated_equivalent_calls": repeated_calls,
        "python_attempt_turns": python_attempts,
        "git_check_turns": git_checks,
        "first_write_turn": write_turns[0] if write_turns else None,
        "last_write_turn": last_write,
        "turns_after_last_write": len(after_write),
        "labels_after_last_write": [row["labels"] for row in after_write],
        "context_tokens_after_last_write": token_after,
        "totals": totals or {},
        "rows": rows,
    }


def _paths(call: dict[str, Any]) -> list[str]:
    payload = call.get("input") or {}
    out: list[str] = []
    if isinstance(payload.get("path"), str):
        out.append(payload["path"])
    for key in ("paths", "write_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            out.extend(str(item) for item in value)
    for task in payload.get("tasks") or ():
        if isinstance(task, dict):
            out.extend(str(item) for item in task.get("write_paths") or ())
            out.append(f"task:{task.get('key')}")
    return out


def _command(call: dict[str, Any]) -> str | None:
    payload = call.get("input") or {}
    command = payload.get("command")
    return str(command) if isinstance(command, str) else None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Stage C1-B worker execution audit")
    parser.add_argument("--evidence", type=Path, required=True, help="C1 run root")
    parser.add_argument(
        "--write", action="store_true", help="write trace-audit.json next to this file"
    )
    arguments = parser.parse_args(argv)

    evidence = arguments.evidence
    agent = _find_agent_dir(evidence)
    session = json.loads((agent / "session.json").read_text(encoding="utf-8"))
    conversations = json.loads((agent / "tool_conversations.json").read_text(encoding="utf-8"))
    decisions = _decisions(agent / "trajectory" / session["run_id"] / "transcript.jsonl")
    denials = _denials(agent / "trajectory" / session["run_id"] / "transcript.jsonl")

    report: dict[str, Any] = {
        "stage": "C1B",
        "run_id": session["run_id"],
        "evidence_root": str(evidence),
        "agents": {},
        "denials_by_worker": dict(Counter(str(item["worker_id"]) for item in denials)),
        "denial_reason_codes": dict(Counter(str(item.get("reason_code")) for item in denials)),
        "session_ownership": {
            "checked": session["ownership"]["checked_calls"],
            "allowed": session["ownership"]["allowed_calls"],
            "denied": session["ownership"]["denied_calls"],
        },
        "session_child_tokens": session["child_tokens"],
        "session_metrics": session["metrics"],
        "session_budget": {
            key: session["budget"][key]
            for key in (
                "limit_tokens",
                "used_tokens",
                "remaining_tokens",
                "requests_started",
                "admission_rejections",
                "stop_reason",
                "last_rejection",
            )
            if key in session["budget"]
        },
        "session_status": {
            "status": session.get("status"),
            "failure": session.get("failure"),
            "execution_succeeded": session.get("execution_succeeded"),
        },
        "budget_workers": session["budget"]["workers"],
    }

    children = {
        item["worker_id"]: item
        for item in (
            session["delegation"]["batches"][0]["children"]
            if session["delegation"]["batches"]
            else []
        )
    }
    for task_id, payload in conversations["tasks"].items():
        totals = children.get(task_id, {})
        report["agents"][task_id] = audit_agent(
            task_id,
            payload["messages"],
            decisions,
            totals={
                key: totals.get(key)
                for key in ("status", "success", "stop_reason", "turns", "tool_calls", "ref_id")
            },
            per_request_context=(
                session["metrics"]["main_context_tokens_by_request"]
                if task_id == "bplus-c01-main"
                else None
            ),
        )

    if arguments.write:
        out = Path(__file__).resolve().parent / "trace-audit.json"
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("written", out)

    for task_id, data in report["agents"].items():
        print(
            f"{task_id}: turns={data['turns']} productive={data['productive_turns']} "
            f"discovery={data['discovery_turns']} verification={data['verification_turns']} "
            f"refused={data['refused_calls']} repeats={data['repeated_equivalent_calls']} "
            f"first_write={data['first_write_turn']} last_write={data['last_write_turn']} "
            f"after_write={data['turns_after_last_write']} "
            f"python_turns={data['python_attempt_turns']} git_turns={data['git_check_turns']}"
        )
    print("denials_by_worker", report["denials_by_worker"])
    print("denial_reason_codes", report["denial_reason_codes"])
    return 0


def _find_agent_dir(evidence: Path) -> Path:
    """The directory holding ``session.json``, wherever the runner put it under the run root."""

    sessions = sorted(evidence.rglob("session.json"))
    if not sessions:
        raise SystemExit(f"no agent session under {evidence}")
    return sessions[0].parent


if __name__ == "__main__":
    raise SystemExit(main())
