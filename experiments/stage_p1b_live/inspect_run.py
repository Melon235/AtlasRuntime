"""Read one completed run directory and print the stage P1b evidence.

The runtime records tool *inputs* as digests in the trajectory and keeps the verbatim conversation
in ``tool_conversations.json``; this script joins the two so the Main's decision sequence can be
reported without guessing. It never touches a sandbox, a provider or a mutable run artefact.

    python experiments/stage_p1b_live/inspect_run.py <run-dir>
    python experiments/stage_p1b_live/inspect_run.py --scratch %TEMP%/atlas-p1b-live-1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def conversation(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Task id -> ordered messages, in the shape the model saw them."""

    payload = load(run_dir / "tool_conversations.json")
    tasks = payload.get("tasks") or {}
    return {
        str(task_id): list((entry or {}).get("messages") or []) for task_id, entry in tasks.items()
    }


def transcript(run_dir: Path, run_id: str) -> list[dict[str, Any]]:
    path = run_dir / "trajectory" / run_id / "transcript.jsonl"
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        events.append(event["payload"].get("atlas_trajectory", {}))
    return events


def tool_trace(run_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """One row per proposed tool call: what, whether it was decided, and whether it executed."""

    events = transcript(run_dir, run_id)
    proposed: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data") or {}
        kind = event.get("event_type")
        call_id = data.get("call_id")
        if kind == "action_proposed":
            row = {
                "seq": None,
                "call_id": call_id,
                "tool": data.get("tool_name"),
                "input_digest": data.get("input_digest"),
                "input_size": data.get("input_size"),
                "decision": None,
                "reason_codes": [],
                "executed": None,
                "result_chars": None,
            }
            proposed[call_id] = row
            trace.append(row)
        elif kind == "action_decision" and call_id in proposed:
            proposed[call_id]["decision"] = data.get("outcome") or data.get("decision")
            proposed[call_id]["reason_codes"] = list(
                data.get("reason_codes") or data.get("reasonCodes") or []
            )
        elif kind == "execution_outcome" and call_id in proposed:
            proposed[call_id]["executed"] = data.get("executed", data.get("status"))
            proposed[call_id]["result_chars"] = data.get("output_chars") or data.get("result_chars")
    return trace


def call_sequence(run_dir: Path) -> list[dict[str, Any]]:
    """Verbatim ordered list of every tool call the Main made, with its result status."""

    rows: list[dict[str, Any]] = []
    for task_id, messages in conversation(run_dir).items():
        for message in messages:
            if not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    rows.append(
                        {
                            "task": task_id,
                            "tool": block.get("name"),
                            "input": block.get("input"),
                            "id": block.get("id"),
                        }
                    )
                elif block.get("type") == "tool_result":
                    for row in rows:
                        if row["id"] == block.get("tool_use_id"):
                            row["is_error"] = block.get("is_error")
                            row["result"] = str(block.get("content"))[:400]
    return rows


def final_text(run_dir: Path) -> list[str]:
    texts: list[str] = []
    for messages in conversation(run_dir).values():
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text")))
    return texts


def summarize(run_dir: Path) -> dict[str, Any]:
    session = load(run_dir / "session.json")
    run_id = session["run_id"]
    delegation = session.get("delegation") or {}
    return {
        "run_id": run_id,
        "status": session["status"],
        "execution_succeeded": session["execution_succeeded"],
        "failure": session["failure"],
        "acceptance": session["acceptance"],
        "stop_reason": session["main"]["stop_reason"],
        "stop_message": session["main"]["stop_message"],
        "main_turns": session["main"]["turns"],
        "main_tool_calls": session["main"]["tool_calls"],
        "main_output": session["main"]["output"],
        "delegation_enabled": session["delegation_enabled"],
        "delegation_batches": [
            {
                "round": batch["round"],
                "route": batch["route"],
                "reason_codes": batch["decision"]["reason_codes"],
                "blocked_gates": batch["decision"]["blocked_gates"],
                "max_observed_concurrency": batch["max_observed_concurrency"],
                "children": [
                    {
                        "key": child["key"],
                        "status": child["status"],
                        "success": child["success"],
                        "turns": child["turns"],
                        "tool_calls": child["tool_calls"],
                        "started_offset_ms": child["started_offset_ms"],
                        "finished_offset_ms": child["finished_offset_ms"],
                        "ref_id": child["ref_id"],
                        "summary_chars": child["summary_chars"],
                    }
                    for child in batch["children"]
                ],
            }
            for batch in delegation.get("batches", [])
        ],
        "delegation_totals": delegation.get("totals"),
        "metrics": session["metrics"],
        "public_tests": {
            "calls": session["public_tests"]["calls"],
            "runs": session["public_tests"]["runs"],
        },
        "verification": {
            "verdict": (session["verification"] or {}).get("verdict"),
            "static_status": (session["verification"] or {}).get("static_status"),
            "dynamic": (session["verification"] or {}).get("dynamic"),
        },
        "sandbox": session["sandbox"],
        "ownership": session["ownership"],
        "budget": session["budget"],
        "artifacts": session["artifacts"],
        "tool_surface": session["tool_surface"],
        "tool_trace": tool_trace(run_dir, run_id),
        "call_sequence": call_sequence(run_dir),
        "assistant_texts": final_text(run_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage P1b run inspector")
    parser.add_argument("run_dir", nargs="?", type=Path)
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print the whole evidence object")
    parser.add_argument("--out", type=Path, default=None, help="write the evidence object here")
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    if run_dir is None:
        scratch = args.scratch
        if scratch is None:
            parser.error("give a run directory or --scratch")
        runs = sorted((scratch / "runs").iterdir())
        if not runs:
            parser.error(f"no run directories under {scratch / 'runs'}")
        run_dir = runs[-1]

    evidence = summarize(run_dir)
    if args.out is not None:
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    if args.json:
        print(json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    print(
        json.dumps(
            {
                k: v
                for k, v in evidence.items()
                if k
                not in {
                    "tool_trace",
                    "call_sequence",
                    "assistant_texts",
                    "main_output",
                    "metrics",
                }
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    print("\n== metrics ==")
    print(json.dumps(evidence["metrics"], indent=2, sort_keys=True))
    print("\n== main tool calls ==")
    for index, row in enumerate(evidence["call_sequence"], start=1):
        payload = json.dumps(row.get("input"), ensure_ascii=False)[:160]
        print(f"{index:2d}. {row['tool']:<12} err={str(row.get('is_error')):<5} {payload}")
    print("\n== assistant text ==")
    for text in evidence["assistant_texts"]:
        print("-", text[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
