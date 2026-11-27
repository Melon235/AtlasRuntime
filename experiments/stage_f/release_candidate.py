"""Freeze or launch the registered Stage F-R experiment through the Stage B+ dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import plan

EXPERIMENT = "stage-fr-final-96-v2"
SCOPE = "F_R_FINAL_96_V2"
BATCH_IDS = {batch: f"{EXPERIMENT}-b{batch}" for batch in (1, 2, 3)}
REQUESTED_MODEL = "deepseek-flash"
PRICING = {
    "provider": "deepseek",
    "model": REQUESTED_MODEL,
    "currency": "USD",
    "snapshot_date": "2026-11-27",
    "basis": "published peak rates per million tokens",
    "cache_hit_input_per_million": 0.006,
    "cache_miss_input_per_million": 0.30,
    "output_per_million": 1.20,
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
}
SETTLEMENT = {
    "token_stop_total": 96_000_000,
    "cost_stop_total": 20.0,
    "per_batch_cost_stop_usd": 10.0,
    "per_cell_cost_stop_usd": 1.0,
    "attempt_limit": 1,
    "matrix_concurrency": 1,
    "prior_batches": "complete, known usage, same frozen identity, consumed claim",
    "missing_cache_split": "price input as cache miss",
    "unknown_usage": "stop with unknown total and preserve known consumption",
}


def requested_model(cell: Mapping[str, Any]) -> str:
    """Resolve the trusted F-R model, preserving the historical Main default."""
    experiment = cell.get("release_experiment")
    if experiment is None:
        return str(contract.BUDGET["model"])
    if (
        experiment != EXPERIMENT
        or cell.get("plan_id") not in BATCH_IDS.values()
        or cell.get("scope") != SCOPE
        or (cell.get("budget") or {}).get("model") != REQUESTED_MODEL
    ):
        raise ValueError("cell does not match the registered F-R model contract")
    return REQUESTED_MODEL


def pricing_config() -> Any:
    from atlasos.config import Pricing

    return Pricing.model_validate({key: PRICING[key] for key in Pricing.model_fields})


def verify_authority(
    record: Mapping[str, Any] | None,
    *,
    batch: Mapping[str, Any],
    manifest: Mapping[str, Any],
    head: str,
    identity: Mapping[str, Any],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    verdict = authority.verify(
        record, plan=batch, head=head, executor=identity,
        protocol_digest_value=authority.digest_of(manifest),
        repo_root=repo_root or plan.REPO_ROOT,
        claim_consumed=executor.claim_path(batch["digest"]).exists(),
    )
    if verdict["ready"] and record is not None and (
        float(record["cost_stop_usd"]) > batch["cost_stop_usd"]
        or float(record["per_cell_cost_stop_usd"]) > batch["per_cell_cost_stop_usd"]
        or record["token_stop"] > batch["nominal_token_capacity"]
    ):
        verdict["problems"].append("authorization exceeds the frozen F-R budget")
        verdict["ready"] = False
    return verdict


def batch_plan(plan_id: str) -> dict[str, Any]:
    batch = next((position for position, name in BATCH_IDS.items() if name == plan_id), None)
    if batch is None:
        raise ValueError(f"unknown F-R batch: {plan_id!r}")
    record = plan.batch_plan(batch)
    budget = {**record["budget"], "model": REQUESTED_MODEL}
    budget_digest = authority.digest_of(budget)
    pricing_digest = authority.digest_of(PRICING)
    settlement_digest = authority.digest_of(SETTLEMENT)
    for cell in record["cells"]:
        cell.update(
            cell_id="fr2." + cell["cell_id"].split(".", 1)[1],
            plan_id=plan_id,
            batch_id=plan_id,
            scope=SCOPE,
            release_experiment=EXPERIMENT,
            budget=budget,
            budget_digest=budget_digest,
            pricing_digest=pricing_digest,
            settlement_digest=settlement_digest,
        )
    digest = plan.plan_digest(record["cells"])
    record.update(
        stage="F-R",
        master_id=EXPERIMENT,
        plan_id=plan_id,
        scope=SCOPE,
        budget=budget,
        budget_digest=budget_digest,
        pricing_digest=pricing_digest,
        settlement_digest=settlement_digest,
        digest=digest,
        claim_namespace=f"{EXPERIMENT}/{plan_id}",
        claim_key=executor.claim_path(digest).name,
        runs_subdir=f"runs/{plan_id}",
        evidence_subdir=plan_id,
        cost_stop_usd=SETTLEMENT["per_batch_cost_stop_usd"],
        per_cell_cost_stop_usd=SETTLEMENT["per_cell_cost_stop_usd"],
    )
    return record


def _manifest(head: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    reference = plan.manifest()
    historic = json.loads((plan.REPO_ROOT / "experiments/stage_f/manifest.json").read_text("utf-8"))
    if historic["task_facts"] != reference["task_facts"]:
        raise ValueError("current task bytes differ from the Stage F qualification manifest")
    batches = []
    for plan_id in BATCH_IDS.values():
        record = contract.scope_plan(plan_id)
        batches.append({
            "batch": record["batch"],
            "plan_id": plan_id,
            "plan_digest": record["digest"],
            "claim_key": record["claim_key"],
            "cells": record["cells_count"],
            "token_capacity": record["nominal_token_capacity"],
            "cost_stop_usd": record["cost_stop_usd"],
        })
    return {
        "master_id": EXPERIMENT,
        "scope": SCOPE,
        "execution_head": head,
        "executor_digest": identity["executor_digest"],
        "executor_version": identity["executor_version"],
        "task_facts": reference["task_facts"],
        "batches": batches,
        "pricing": PRICING,
        "pricing_digest": authority.digest_of(PRICING),
        "settlement_contract": SETTLEMENT,
        "settlement_digest": authority.digest_of(SETTLEMENT),
    }


def verified_manifest(root: Path, *, head: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    frozen = json.loads((root / "manifest.json").read_text("utf-8"))
    if frozen != _manifest(head, identity):
        raise ValueError("F-R frozen manifest differs from the registered execution contract")
    for plan_id in BATCH_IDS.values():
        exported = json.loads((root / f"{plan_id}.plan.json").read_text("utf-8"))
        if authority.digest_of(exported) != authority.digest_of(frozen_batch(plan_id, identity)):
            raise ValueError(f"F-R frozen plan differs from the registered plan: {plan_id}")
    return frozen


def frozen_batch(plan_id: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **contract.scope_plan(plan_id),
        "executor_digest": identity["executor_digest"],
        "executor_version": identity["executor_version"],
    }


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _execution_identity(root: Path) -> tuple[str, dict[str, Any]]:
    if root.is_relative_to(plan.REPO_ROOT.resolve()):
        raise ValueError("use an external F-R evidence root")
    if executor.git_dirty() or not executor.claim_root()[1]:
        raise ValueError("F-R requires a clean checkout and the production claim namespace")
    return executor.git_head(), executor.executor_identity(experiment_id=EXPERIMENT)


async def prepare(root: Path) -> bool:
    head, identity = _execution_identity(root)
    manifest = _manifest(head, identity)
    for batch in manifest["batches"]:
        if executor.claim_path(batch["plan_digest"]).exists():
            raise ValueError(f"existing claim for {batch['plan_id']}")
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
    _write(root / "manifest.json", manifest)
    _write(root / "executor.json", identity)
    ready = True
    for plan_id in BATCH_IDS.values():
        record = frozen_batch(plan_id, identity)
        _write(root / f"{plan_id}.plan.json", record)
        _write(root / f"{plan_id}.authorization.json", authority.pending_record(
            record, protocol_digest_value=authority.digest_of(manifest), executor=identity
        ))
        admission = await executor.release_production_admission(record)
        _write(root / f"{plan_id}.admission.json", admission)
        settlement = executor._stage_f_settlement(
            record, stage_root=root, identity=identity, manifest=manifest
        )
        _write(root / f"{plan_id}.settlement-preflight.json", settlement)
        print(f"{plan_id}: {admission['status']} ({len(admission['cells'])} cells)")
        ready &= admission["status"] == "READY"
    print(f"manifest digest: {authority.digest_of(manifest)}")
    return ready


async def launch(root: Path) -> bool:
    head, identity = _execution_identity(root)
    manifest = verified_manifest(root, head=head, identity=identity)
    # Require authority for the whole sequence up front. The dispatcher re-verifies it before
    # each claim and checks real reports and headroom before it opens each subsequent batch.
    for batch in manifest["batches"]:
        plan_id = batch["plan_id"]
        record = json.loads((root / f"{plan_id}.authorization.json").read_text("utf-8"))
        verdict = verify_authority(
            record, batch=contract.scope_plan(plan_id), manifest=manifest, head=head,
            identity=identity,
        )
        if not verdict["ready"]:
            print(f"{plan_id}: NOT_AUTHORIZED ({'; '.join(verdict['problems'])})")
            return False
    for plan_id in BATCH_IDS.values():
        report = await executor.dispatch(
            plan_id, runs_root=root / "runs" / plan_id, evidence_root=root,
            authority_path=root / f"{plan_id}.authorization.json",
        )
        print(f"{plan_id}: {report['stop_reason']}")
        if not report.get("complete"):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "launch"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.out.resolve()
    ready = asyncio.run(prepare(root) if args.action == "prepare" else launch(root))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
