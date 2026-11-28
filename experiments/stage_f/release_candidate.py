"""Freeze or launch the registered Stage F-R experiment through the Stage B+ dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import arms, plan, tasks
from experiments.stage_f.tasks import revisions

EXPERIMENT = "stage-fr-final-96-v3"
SCOPE = "F_R_FINAL_96_V3"
BATCH_IDS = {batch: f"{EXPERIMENT}-b{batch}" for batch in (1, 2, 3)}
V4_EXPERIMENT = "stage-fr-final-96-v4"
V4_SCOPE = "F_R_FINAL_96_V4"
V4_BATCH_IDS = {batch: f"{V4_EXPERIMENT}-b{batch}" for batch in (1, 2, 3)}
RELEASES = {EXPERIMENT: (SCOPE, BATCH_IDS), V4_EXPERIMENT: (V4_SCOPE, V4_BATCH_IDS)}
PLAN_RELEASES = {
    plan_id: experiment for experiment, (_, ids) in RELEASES.items() for plan_id in ids.values()
}
RELEASE_SCOPES = frozenset(scope for scope, _ in RELEASES.values())
REQUESTED_MODEL = "deepseek-flash"
STOP_POLICY = {
    "version": "fr-v3-cell-budget/1",
    "verified_cell_admission_exhaustion": "MEASURED_FAILURE",
    "requirements": [
        "pre-request rejection with no later admissions",
        "provider journal, terminal budget ledger and actual usage reconcile within the cell cap",
        "valid export, cleanup, execution integrity and completed independent Oracle",
    ],
    "resource_or_evidence_violation": "STOP_MATRIX",
    "cost_and_cumulative_stops": "UNCHANGED",
}
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
QUALIFICATION_PATH = Path(__file__).with_name("v3-qualified-revisions.json")


def _revision_binding(task: tasks.Task) -> dict[str, Any]:
    revision = revisions.REVISIONS.get(task.task_id, 1)
    identity = revisions.task_identity(task, revision)
    seed = plan.visible_files(task.task_id, task=task)
    return {
        "task_revision": revision,
        "task_revision_digest": identity["task_digest"],
        "fixture_seed_digest": authority.digest_of(seed).removeprefix("sha256:"),
        "task_oracle_digest": identity["oracle_digest"],
    }


def bound_task(cell: Mapping[str, Any]) -> tasks.Task:
    """Resolve a production cell and reject any mismatch before a Runner can start."""
    if cell.get("release_experiment") not in RELEASES:
        raise ValueError("cell has no registered F-R experiment")
    requested_model(cell)
    task_id = str(cell["task_id"])
    task = revisions.task_at_revision(task_id, revisions.REVISIONS.get(task_id, 1))
    if type(cell.get("task_revision")) is not int or any(
        cell.get(key) != value for key, value in _revision_binding(task).items()
    ):
        raise ValueError(f"task revision identity mismatch for {task_id}")
    if cell.get("execution_mode") == "atlas_static_dag":
        dag = arms.static_dag(task_id, task=task)
        if (
            cell.get("static_dag") != dag
            or cell.get("static_dag_digest") != authority.digest_of(dag)
        ):
            raise ValueError(f"static DAG revision mismatch for {task_id}")
    return task


def requested_model(cell: Mapping[str, Any]) -> str:
    """Resolve the trusted F-R model, preserving the historical Main default."""
    experiment = cell.get("release_experiment")
    if experiment is None:
        return str(contract.BUDGET["model"])
    if (
        PLAN_RELEASES.get(cell.get("plan_id")) != experiment
        or experiment not in RELEASES
        or cell.get("scope") != RELEASES[experiment][0]
        or (cell.get("budget") or {}).get("model") != REQUESTED_MODEL
    ):
        raise ValueError("cell does not match the registered F-R model contract")
    return REQUESTED_MODEL


def pricing_config() -> Any:
    from atlasos.config import Pricing

    return Pricing.model_validate({key: PRICING[key] for key in Pricing.model_fields})


def goal_run_id(cell: Mapping[str, Any], key: str) -> str:
    if not cell.get("release_experiment"):
        return f"bplus-{key}"
    requested_model(cell)
    version = str(cell["release_experiment"]).rsplit("-", 1)[1].removeprefix("v")
    return f"fr{version}-{cell['plan_id'].rsplit('-', 1)[1]}-{key}"


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
    experiment = PLAN_RELEASES.get(plan_id)
    if experiment is None:
        raise ValueError(f"unknown F-R batch: {plan_id!r}")
    scope, ids = RELEASES[experiment]
    batch = next(position for position, name in ids.items() if name == plan_id)
    version = experiment.rsplit("-", 1)[1].removeprefix("v")
    record = plan.batch_plan(batch)
    budget = {**record["budget"], "model": REQUESTED_MODEL}
    budget_digest = authority.digest_of(budget)
    pricing_digest = authority.digest_of(PRICING)
    settlement_digest = authority.digest_of(SETTLEMENT)
    stop_policy_digest = authority.digest_of(STOP_POLICY)
    resolved = {
        task_id: revisions.task_at_revision(task_id, revisions.REVISIONS.get(task_id, 1))
        for task_id in record["tasks"]
    }
    bindings = {task_id: _revision_binding(task) for task_id, task in resolved.items()}
    for cell in record["cells"]:
        task_id = cell["task_id"]
        cell.update(
            cell_id=f"fr{version}." + cell["cell_id"].split(".", 1)[1],
            plan_id=plan_id,
            batch_id=plan_id,
            scope=scope,
            release_experiment=experiment,
            budget=budget,
            budget_digest=budget_digest,
            pricing_digest=pricing_digest,
            settlement_digest=settlement_digest,
            stop_policy_digest=stop_policy_digest,
            **bindings[task_id],
        )
        if cell["execution_mode"] == "atlas_static_dag":
            dag = arms.static_dag(task_id, task=resolved[task_id])
            cell.update(static_dag=dag, static_dag_digest=authority.digest_of(dag))
    digest = plan.plan_digest(record["cells"])
    record.update(
        stage="F-R",
        master_id=experiment,
        plan_id=plan_id,
        scope=scope,
        budget=budget,
        budget_digest=budget_digest,
        pricing_digest=pricing_digest,
        settlement_digest=settlement_digest,
        stop_policy_digest=stop_policy_digest,
        digest=digest,
        claim_namespace=f"{experiment}/{plan_id}",
        claim_key=executor.claim_path(digest).name,
        runs_subdir=f"runs/{plan_id}",
        evidence_subdir=plan_id,
        cost_stop_usd=SETTLEMENT["per_batch_cost_stop_usd"],
        per_cell_cost_stop_usd=SETTLEMENT["per_cell_cost_stop_usd"],
    )
    return record


def _manifest(head: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    experiment = identity["executor_version"]
    scope, ids = RELEASES[experiment]
    reference = plan.manifest()
    historic = json.loads((plan.REPO_ROOT / "experiments/stage_f/manifest.json").read_text("utf-8"))
    if historic["task_facts"] != reference["task_facts"]:
        raise ValueError("historical revision bytes differ from the Stage F qualification manifest")
    receipts = json.loads(QUALIFICATION_PATH.read_text("utf-8"))
    if set(receipts) != set(revisions.REVISIONS):
        raise ValueError("F-R qualifications must cover the four approved revisions")
    historic_qualification = json.loads(
        QUALIFICATION_PATH.with_name("task-qualification.json").read_text("utf-8")
    )
    if historic_qualification["status"] != "STAGE_F_TASK_QUALIFICATION_PASS":
        raise ValueError("Stage F historical task qualification is incomplete")
    task_facts = {}
    for task_id in reference["task_facts"]:
        revision = revisions.REVISIONS.get(task_id, 1)
        task = revisions.task_at_revision(task_id, revision)
        task_identity = revisions.task_identity(task, revision)
        if revision == 1:
            qualification = next(
                row for row in historic_qualification["qualification"] if row["task_id"] == task_id
            )
            if not qualification["qualified"]:
                raise ValueError(f"historical qualification is incomplete for {task_id}")
            qualification_digest = authority.digest_of(qualification)
        else:
            receipt = receipts[task_id]
            proof = receipt["proof"]
            qualification = proof["qualification"]
            if (
                proof["task"] != task_identity
                or receipt["qualification_digest"] != authority.digest_of(proof)
                or qualification != {
                    "seed_public": "BUSINESS_FAILURE", "seed_oracle": "BUSINESS_FAILURE",
                    "reference_public": "PASS", "reference_oracle": "PASS",
                }
                or not proof["mutations"]
                or not all(value is True for value in proof["mutations"].values())
                or not all(
                    proof[key] is True
                    for key in ("fixture", "equivalent", "static_dag", "leak_checks")
                )
            ):
                raise ValueError(f"invalid qualification receipt for {task_id}")
            qualification_digest = receipt["qualification_digest"]
        task_facts[task_id] = {
            **plan._task_facts(task_id, task=task),
            "revision_identity": task_identity,
            "qualification_digest": qualification_digest,
        }
    budget = {**arms.budget(), "model": REQUESTED_MODEL}
    batches = []
    for plan_id in ids.values():
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
        "master_id": experiment,
        "scope": scope,
        "execution_head": head,
        "executor_digest": identity["executor_digest"],
        "executor_version": identity["executor_version"],
        "task_facts": task_facts,
        "task_revision_digest": authority.digest_of({
            task_id: fact["revision_identity"] for task_id, fact in task_facts.items()
        }),
        "qualification_digest": authority.digest_of({
            task_id: fact["qualification_digest"] for task_id, fact in task_facts.items()
        }),
        "budget": budget,
        "budget_digest": authority.digest_of(budget),
        "batches": batches,
        "pricing": PRICING,
        "pricing_digest": authority.digest_of(PRICING),
        "settlement_contract": SETTLEMENT,
        "settlement_digest": authority.digest_of(SETTLEMENT),
        "stop_policy": STOP_POLICY,
        "stop_policy_digest": authority.digest_of(STOP_POLICY),
    }


def verified_manifest(root: Path, *, head: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    frozen = json.loads((root / "manifest.json").read_text("utf-8"))
    if frozen != _manifest(head, identity):
        raise ValueError("F-R frozen manifest differs from the registered execution contract")
    for plan_id in RELEASES[identity["executor_version"]][1].values():
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


def _execution_identity(root: Path, experiment: str = EXPERIMENT) -> tuple[str, dict[str, Any]]:
    if root.is_relative_to(plan.REPO_ROOT.resolve()):
        raise ValueError("use an external F-R evidence root")
    if executor.git_dirty() or not executor.claim_root()[1]:
        raise ValueError("F-R requires a clean checkout and the production claim namespace")
    return executor.git_head(), executor.executor_identity(experiment_id=experiment)


async def prepare(root: Path, experiment: str = EXPERIMENT) -> bool:
    head, identity = _execution_identity(root, experiment)
    manifest = _manifest(head, identity)
    for batch in manifest["batches"]:
        if executor.claim_path(batch["plan_digest"]).exists():
            raise ValueError(f"existing claim for {batch['plan_id']}")
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
    _write(root / "manifest.json", manifest)
    _write(root / "executor.json", identity)
    ready = True
    for plan_id in RELEASES[experiment][1].values():
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


async def launch(root: Path, experiment: str = EXPERIMENT) -> bool:
    head, identity = _execution_identity(root, experiment)
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
    for plan_id in RELEASES[experiment][1].values():
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
    parser.add_argument("--experiment", choices=RELEASES, default=EXPERIMENT)
    args = parser.parse_args()
    root = args.out.resolve()
    action = prepare if args.action == "prepare" else launch
    ready = asyncio.run(action(root, args.experiment))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
