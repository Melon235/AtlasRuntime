"""Stage F — cross-batch settlement.

A batch may only start when the consumption of the batches before it is known and inside the global
budget. Three independent plans with three independent claims cannot enforce that on their own: each
would see a full 32M / USD 32 of headroom and the 96M / USD 96 ceiling would be the sum of three
separate promises instead of one limit.

This module is the check, not a budget scheduler: it reads the evidence the earlier batches already
wrote and answers "may batch *b* start, and with what left". An unknown consumption is **not** zero:
a batch with no report, or a report whose usage is not `COMPLETE`, blocks the next batch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_f import arms

COMPLETE = "COMPLETE"


def _report(evidence_root: Path, plan_id: str) -> dict[str, Any] | None:
    path = evidence_root / plan_id / "matrix-report.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def settlement(manifest: Mapping[str, Any], *, batch: int, evidence_root: Path) -> dict[str, Any]:
    """Everything the earlier batches actually consumed, and what is left for ``batch``."""

    batches = manifest["batches"]
    order = [item["batch"] for item in batches]
    if batch not in order:
        raise KeyError(f"batch {batch} is not in this manifest")

    prior: list[dict[str, Any]] = []
    problems: list[str] = []
    tokens = 0
    cost = 0.0
    unknown = False

    for item in batches:
        if item["batch"] >= batch:
            break
        report = _report(evidence_root, item["plan_id"])
        if report is None:
            problems.append(f"batch {item['batch']} has no matrix report")
            unknown = True
            prior.append({"batch": item["batch"], "state": "NO_REPORT"})
            continue
        usage_complete = str(report.get("usage_completeness")) == COMPLETE
        batch_tokens = report.get("total_tokens")
        batch_cost = report.get("total_cost_usd")
        if not usage_complete or not isinstance(batch_tokens, int):
            problems.append(f"batch {item['batch']} usage is not COMPLETE")
            unknown = True
        else:
            tokens += batch_tokens
        if isinstance(batch_cost, (int, float)) and not isinstance(batch_cost, bool):
            cost += float(batch_cost)
        else:
            problems.append(f"batch {item['batch']} has no cost")
            unknown = True
        prior.append(
            {
                "batch": item["batch"],
                "plan_id": item["plan_id"],
                "state": "REPORTED",
                "stop_reason": report.get("stop_reason"),
                "complete": report.get("complete"),
                "tokens": batch_tokens,
                "cost_usd": batch_cost,
                "usage_completeness": report.get("usage_completeness"),
            }
        )

    # The batch's own ceiling and the manifest's remaining headroom both apply.
    batch_token_stop = int(arms.TOKEN_STOP_PER_BATCH)
    batch_cost_stop = float(arms.COST_STOP_PER_BATCH)
    global_token_stop = int(arms.TOKEN_STOP_TOTAL)
    global_cost_stop = float(arms.COST_STOP_TOTAL)
    remaining_tokens = global_token_stop - tokens
    remaining_cost = global_cost_stop - cost
    allowance_tokens = min(batch_token_stop, remaining_tokens)
    allowance_cost = min(batch_cost_stop, remaining_cost)
    if unknown:
        allowance_tokens = 0
        allowance_cost = 0.0

    return {
        "batch": batch,
        "prior": prior,
        "consumed_tokens": tokens if not unknown else None,
        "consumed_cost_usd": round(cost, 6) if not unknown else None,
        "remaining_tokens": remaining_tokens if not unknown else None,
        "remaining_cost_usd": round(remaining_cost, 6) if not unknown else None,
        "allowed_tokens": allowance_tokens,
        "allowed_cost_usd": round(allowance_cost, 6),
        "unknown_consumption": unknown,
        "problems": problems,
        "may_start": not problems and allowance_tokens > 0 and allowance_cost > 0,
    }


def record_settlement(evidence_root: Path, payload: Mapping[str, Any]) -> Path:
    """Write the settlement next to the batch's own evidence, so the decision is auditable."""

    directory = evidence_root / str(payload["plan_id"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cumulative-settlement.json"
    path.write_text(
        json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


__all__ = ["COMPLETE", "record_settlement", "settlement"]
