"""Prepare the separate F-R candidate offline using the existing benchmark admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from experiments.stage_b_plus import authority, executor
from experiments.stage_f import plan

EXPERIMENT = "stage-fr-final-96-v1"
SCOPE = "F_R_FINAL_96"
REQUESTED_MODEL = "deepseek-flash"


def write_candidate(out: Path) -> dict:
    repo = plan.REPO_ROOT.resolve()
    out = out.resolve()
    if out.is_relative_to(repo):
        raise ValueError("F-R evidence must use a new directory outside the source repository")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip():
        raise ValueError("F-R identity requires a clean source checkout")
    if not executor.claim_root()[1]:
        raise ValueError("F-R candidate requires the real claim namespace for collision checks")
    head = executor.git_head(repo)
    existing_executor = executor.executor_identity(repo)
    files = dict(existing_executor["files"])
    for directory in ("src/atlasos", "experiments/stage_f", "experiments/stage_b_plus"):
        for path in (repo / directory).rglob("*.py"):
            relative = path.relative_to(repo).as_posix()
            files[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    # Also bind dependencies and non-Python assets tracked by the execution checkout.
    identity = {"head": head, "files": files, "executor_version": EXPERIMENT}
    identity["executor_digest"] = authority.digest_of(identity)

    reference = plan.manifest()
    historical = json.loads((repo / "experiments/stage_f/manifest.json").read_text("utf-8"))
    if historical["task_facts"] != reference["task_facts"]:
        raise ValueError("current task bytes differ from the Stage F qualification manifest")

    old_claims = {executor.claim_path(p["plan_digest"]).name for p in historical["batches"]}
    batches = []
    for batch, candidate in plan.batch_plans().items():
        batch_id = f"{EXPERIMENT}-b{batch}"
        budget = {**candidate["budget"], "model": REQUESTED_MODEL}
        budget_digest = authority.digest_of(budget)
        for cell in candidate["cells"]:
            cell.update(
                cell_id="fr." + cell["cell_id"].split(".", 1)[1],
                plan_id=batch_id,
                batch_id=batch_id,
                scope=SCOPE,
                budget=budget,
                budget_digest=budget_digest,
            )
        digest = plan.plan_digest(candidate["cells"])
        claim_key = executor.claim_path(digest).name
        if claim_key in old_claims or executor.claim_path(digest).exists():
            raise ValueError(f"claim identity already exists: {claim_key}")
        candidate.update(
            plan_id=batch_id,
            scope=SCOPE,
            stage="F-R",
            master_id=EXPERIMENT,
            executor_version=EXPERIMENT,
            executor_digest=identity["executor_digest"],
            budget=budget,
            budget_digest=budget_digest,
            digest=digest,
            claim_namespace=f"{EXPERIMENT}/{batch_id}",
            claim_key=claim_key,
            runs_subdir=f"runs/{batch_id}",
            evidence_subdir=f"evidence/{batch_id}",
        )
        batches.append(candidate)

    manifest = {
        "status": "CANDIDATE_NOT_AUTHORIZED",
        "experiment": EXPERIMENT,
        "scope": SCOPE,
        "review_candidate_head": head,
        "proposed_requested_model": REQUESTED_MODEL,
        "pricing_status": "PROPOSED_MODEL_NOT_IN_FROZEN_SETTLEMENT",
        "dispatch_status": "PLAN_IDS_NOT_REGISTERED",
        "evidence_root": str(out),
        "executor": identity,
        "existing_production_executor_digest": existing_executor["executor_digest"],
        "task_facts": reference["task_facts"],
        "batches": [
            {key: batch[key] for key in ("plan_id", "digest", "claim_key", "cells_count")}
            for batch in batches
        ],
    }
    manifest["digest"] = authority.digest_of(manifest)
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "manifest.json", manifest)
    for batch in batches:
        prefix = out / batch["plan_id"]
        _write(prefix.with_suffix(".plan.json"), batch)
        _write(
            prefix.with_suffix(".authorization.json"),
            authority.pending_record(
                batch, protocol_digest_value=manifest["digest"], executor=identity
            ),
        )
        report = executor.run_admission_preflight(batch, repo_root=repo)
        for cell, result in zip(batch["cells"], report["cells"], strict=True):
            if cell["execution_mode"] in executor.MAIN_DRIVEN_MODES:
                # Inspect the real Main options as well as the benchmark's structural verdict.
                options = executor.production_goal_options(
                    cell,
                    evidence_root=out / "admission-inspection",
                    fixture_dir=out / "admission-inspection" / "fixture",
                    config=executor.cell_runner_config(cell, executor.RunnerConfig()),
                    run_id="fr-admission-inspection",
                )
                result["effective_requested_model"] = options.model
                result["candidate_model_matches"] = options.model == REQUESTED_MODEL
        report["candidate_configuration_ready"] = sum(
            result["status"] == "READY" and result.get("candidate_model_matches", True)
            for result in report["cells"]
        )
        _write(prefix.with_suffix(".admission.json"), report)
    return manifest


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    manifest = write_candidate(parser.parse_args().out)
    print(f"{manifest['status']} {manifest['digest']}")


if __name__ == "__main__":
    main()
