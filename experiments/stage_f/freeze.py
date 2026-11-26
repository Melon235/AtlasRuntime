"""Stage F — the one freeze the matrix runs on.

Writes, in one operation: the master manifest (executor, adapter, budget, pricing and settlement
identities, the eight task identities and the order seed), the three batch plans, and the three
pending authorization records. A plan that does not recompute from the manifest's seed is not
written at all — the operation validates first and refuses rather than freezing a broken matrix.

What it does not do: sign anything. Every authorization field stays null, ``authorizes_live``
execution stays false, and the cost fields stay unapproved. Only the Owner can change that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import plan, tasks

REPO = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = Path(__file__).resolve().parent


def authorization_path(batch: int) -> Path:
    return EVIDENCE_DIR / f"authorization-b{batch}.json"


def build() -> dict[str, Any]:
    """Every frozen record, without touching the disk."""

    manifest_record = plan.manifest()
    problems = plan.validate(manifest_record)
    plans = {batch: plan.batch_plan(batch) for batch in (1, 2, 3)}
    for batch, record in plans.items():
        if record["digest"] != manifest_record["batches"][batch - 1]["plan_digest"]:
            problems.append(f"batch {batch}: the plan digest is not the manifest's")
        if record["executor_digest"] != manifest_record["executor_digest"]:
            problems.append(f"batch {batch}: the executor digest is not the manifest's")
    identity = executor.executor_identity(REPO)
    protocol = authority.protocol_digest(contract.protocol_document())
    authorizations = {
        batch: authority.pending_record(
            plans[batch],
            protocol_digest_value=protocol,
            executor=identity,
        )
        for batch in (1, 2, 3)
    }
    for batch, record in authorizations.items():
        verdict = authority.verify(
            record,
            plan=plans[batch],
            head=executor.git_head(REPO),
            executor=identity,
            protocol_digest_value=protocol,
            repo_root=REPO,
        )
        if verdict["ready"] is not False:
            problems.append(f"batch {batch}: the pending record would authorize a live run")
        if record.get("authorizes_live_execution") is not False:
            problems.append(f"batch {batch}: the pending record claims live authorization")
    return {
        "manifest": manifest_record,
        "plans": plans,
        "authorizations": authorizations,
        "problems": problems,
        "identity": identity,
        "protocol_digest": protocol,
    }


def write(*, output: Path | None = None) -> dict[str, Any]:
    built = build()
    if built["problems"]:
        raise SystemExit(
            "refusing to freeze a matrix with problems: " + "; ".join(built["problems"])
        )
    manifest_record = built["manifest"]
    contract.write_document(manifest_record, EVIDENCE_DIR / "manifest.json")
    for batch, record in built["plans"].items():
        contract.write_document(record, EVIDENCE_DIR / f"plan-b{batch}.json")
    for batch, record in built["authorizations"].items():
        contract.write_document(record, authorization_path(batch))
    summary = {
        "stage": "F",
        "kind": "final_freeze",
        "generated_at": executor.now(),
        "repo_head": executor.git_head(REPO),
        "executor_version": built["identity"]["executor_version"],
        "executor_digest": built["identity"]["executor_digest"],
        "adapter": manifest_record["adapter"],
        "budget_digest": manifest_record["budget_digest"],
        "pricing": manifest_record["pricing"],
        "settlement_contract": manifest_record["settlement_contract"],
        "execution_contract": manifest_record["execution_contract"],
        "main_prompt_digest": manifest_record["main_prompt_digest"],
        "worker_contract_digest": manifest_record["worker_contract_digest"],
        "manifest_digest": authority.digest_of(manifest_record),
        "master_id": plan.MASTER_ID,
        "cells_total": manifest_record["cells_total"],
        "tasks": [
            {
                "task_id": task.task_id,
                "family": tasks.family_of(task.task_id),
                "goal_digest": manifest_record["task_facts"][task.task_id]["goal_digest"],
                "grant_digest": manifest_record["task_facts"][task.task_id]["grant_digest"],
                "static_dag_digest": manifest_record["task_facts"][task.task_id][
                    "static_dag_digest"
                ],
                # The visible fixture: the seed plus the declared public suite, exactly what
                # ``prepare_fixture`` materialises and digests.
                "seed_digest": authority.digest_of(
                    dict(sorted(plan.visible_files(task.task_id).items()))
                ),
                "deliverables": len(task.deliverables),
                "oracle_tests": len(task.oracle_tests),
            }
            for task in tasks.TASKS
        ],
        "batches": [
            {
                "batch": batch,
                "plan_id": built["plans"][batch]["plan_id"],
                "plan_digest": built["plans"][batch]["digest"],
                "cells": built["plans"][batch]["cells_count"],
                "token_stop": built["plans"][batch]["nominal_token_capacity"],
                "cost_stop_usd": built["plans"][batch]["cost_stop_usd"],
                "authorization": str(authorization_path(batch).relative_to(REPO)),
                "authorization_status": built["authorizations"][batch]["status"],
                "authorizes_live_execution": built["authorizations"][batch][
                    "authorizes_live_execution"
                ],
            }
            for batch in (1, 2, 3)
        ],
        "protocol_digest": built["protocol_digest"],
        "delivery_status": "STAGE_F_FROZEN_AWAITING_OWNER_AUTHORIZATION",
        "live_execution_authorized": False,
        "cost_approved": False,
        "real_model_calls": 0,
        "production_claims_consumed": 0,
    }
    _ = output
    return summary


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    write_all = "--write" in args
    payload = write() if write_all else build()["manifest"]
    if write_all:
        target = EVIDENCE_DIR / "freeze.json"
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {"written": True, "freeze": str(target), "status": payload["delivery_status"]}
            )
        )
    else:
        print(json.dumps({"manifest_digest": authority.digest_of(payload)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
