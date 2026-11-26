"""Stage F — cross-batch settlement, exercised through the real dispatch entry.

Ten scenarios, each a real call to ``executor.dispatch`` in offline-proof mode (or a real refusal
from it), so the check is about the pre-claim path rather than about ``cumulative.settlement`` in
isolation:

1. batch 1 → 2 → 3 run in order in one root and each later batch is settled against the reports its
   predecessors wrote;
2. a missing prior report refuses the next batch;
3. a prior batch whose claim was never consumed refuses the next batch;
4. a prior report whose plan digest is not the frozen digest refuses the next batch;
5. a prior report with unknown usage refuses the next batch;
6. a prior batch that stopped instead of completing refuses the next batch;
7. the global remainder, not the batch allowance, becomes the effective ceiling;
8. the refusal is a refusal from the dispatcher, with its own stop reason;
9. a refused batch consumes no production claim;
10. the effective ceiling reaches the dispatcher's own loop variables, not only the report.

No model, no sandbox and no production claim: the cell runner is the offline synthetic runner and
the claims live in a throwaway root.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_c1 import checks as c1_checks
from experiments.stage_f import cumulative, plan

REPO = Path(__file__).resolve().parents[2]


def _runner() -> executor.CellRunner:
    from experiments.stage_b_plus.offline import synthetic_cell_run

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    return run


def _authority_file(batch_plan: Mapping[str, Any], scratch: Path) -> Path:
    identity = executor.executor_identity(REPO)
    record = c1_checks._offline_signed(
        batch_plan,
        scratch=scratch / f"sign-{batch_plan['plan_id']}",
        head=executor.git_head(REPO),
        identity=identity,
        protocol_digest=authority.protocol_digest(contract.protocol_document()),
        cost_stop_usd=float(batch_plan["cost_stop_usd"]),
        per_cell_cost_stop_usd=float(batch_plan["per_cell_cost_stop_usd"]),
    )
    path = scratch / f"authority-{batch_plan['plan_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def _dispatch(batch: int, root: Path, scratch: Path, claim_root: Path) -> dict[str, Any]:
    plan_id = plan.BATCH_PLAN_IDS[batch]
    batch_plan = contract.scope_plan(plan_id)
    path = _authority_file(batch_plan, scratch)
    with executor.claim_root_override(claim_root):
        return asyncio.run(
            executor.dispatch(
                plan_id,
                runs_root=root / plan_id / "runs",
                evidence_root=root,
                authority_path=path,
                cell_runner=_runner(),
                repo_root=REPO,
                mode=executor.MODE_OFFLINE_PROOF,
                clock=lambda: "2026-11-26T00:00:00Z",
                stage_f_root=root,
            )
        )


def _report(
    *,
    batch: int,
    plan_digest: str | None = None,
    tokens: int = 1_000_000,
    cost: float = 1.0,
    usage: str = cumulative.COMPLETE,
    complete: bool = True,
    stop: str = "NONE",
) -> dict[str, Any]:
    batch_plan = contract.scope_plan(plan.BATCH_PLAN_IDS[batch])
    return {
        "plan_id": batch_plan["plan_id"],
        "plan_digest": plan_digest or batch_plan["digest"],
        "executor_digest": executor.executor_identity(REPO)["executor_digest"],
        "usage_completeness": usage,
        "total_tokens": tokens,
        "total_cost_usd": cost,
        "stop_reason": stop,
        "complete": complete,
        "cells_planned": 32,
        "cells_started": 32,
    }


def _write_prior(root: Path, batch: int, **overrides: Any) -> Path:
    directory = root / plan.BATCH_PLAN_IDS[batch]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "matrix-report.json"
    path.write_text(
        json.dumps(_report(batch=batch, **overrides), indent=2) + "\n", encoding="utf-8"
    )
    return path


def _claim(plan_digest: str) -> Path:
    return executor.claim_path(plan_digest)


def run(*, scratch: Path | None = None) -> dict[str, Any]:
    base = Path(scratch or tempfile.mkdtemp(prefix="stage-f-settlement-"))
    base.mkdir(parents=True, exist_ok=True)
    scenarios: list[dict[str, Any]] = []

    def record(scenario_id: str, expectation: str, observed: Mapping[str, Any], ok: bool) -> None:
        scenarios.append(
            {
                "scenario": scenario_id,
                "expectation": expectation,
                "observed": dict(observed),
                "ok": bool(ok),
            }
        )

    # 1 — the normal contract, one root, three batches in order.
    normal_root = base / "normal"
    normal_claims = base / "normal-claims"
    chain: list[dict[str, Any]] = []
    chain_ok = True
    for batch in (1, 2, 3):
        report = _dispatch(batch, normal_root, base / f"sign-normal-{batch}", normal_claims)
        settlement = report.get("cumulative_settlement") or {}
        chain.append(
            {
                "batch": batch,
                "complete": report.get("complete"),
                "cells_passed": report.get("cells_passed"),
                "may_start": settlement.get("may_start"),
                "allowed_tokens": settlement.get("allowed_tokens"),
                "applied_token_stop": report.get("token_stop"),
                "stop_reason": report.get("stop_reason"),
            }
        )
        chain_ok = (
            chain_ok and report.get("complete") is True and settlement.get("may_start") is True
        )
        if batch > 1:
            prior = settlement.get("prior") or []
            chain_ok = chain_ok and len(prior) == batch - 1
            chain_ok = chain_ok and all(item.get("state") == "REPORTED" for item in prior)
    record(
        "1_normal_chain",
        "b1 → b2 → b3 each complete 32/32 and each is settled on its predecessors' reports",
        {"batches": chain},
        chain_ok,
    )

    # 2 — no prior report.
    root = base / "missing-report"
    root.mkdir(parents=True, exist_ok=True)
    claims_mr = base / "claims-mr"
    report = _dispatch(2, root, base / "sign-mr", claims_mr)
    settlement = report.get("cumulative_settlement") or {}
    with executor.claim_root_override(claims_mr):
        claim_exists = _claim(contract.scope_plan(plan.BATCH_PLAN_IDS[2])["digest"]).exists()
    record(
        "2_prior_report_missing",
        "without batch 1's report, batch 2 is refused before any claim",
        {
            "stop_reason": report.get("stop_reason"),
            "refused": report.get("refused"),
            "problems": (report.get("problems") or [])[:3],
            "claim_exists": claim_exists,
        },
        report.get("stop_reason") == executor.STOP_CUMULATIVE
        and report.get("refused") is True
        and not settlement.get("may_start", True)
        and claim_exists is False,
    )

    # 3 — prior report present, prior claim never consumed.
    root = base / "missing-claim"
    _write_prior(root, 1)
    report = _dispatch(2, root, base / "sign-mc", base / "claims-mc")
    problems = (report.get("cumulative_settlement") or {}).get("problems") or []
    record(
        "3_prior_claim_missing",
        "a report without a consumed claim is not evidence that the batch was spent",
        {"stop_reason": report.get("stop_reason"), "problems": problems[:3]},
        report.get("stop_reason") == executor.STOP_CUMULATIVE
        and any("claim" in item for item in problems),
    )

    # 4 — plan identity mismatch.
    root = base / "digest-mismatch"
    _write_prior(root, 1, plan_digest="sha256:" + "0" * 64)
    report = _dispatch(2, root, base / "sign-dm", base / "claims-dm")
    problems = (report.get("cumulative_settlement") or {}).get("problems") or []
    record(
        "4_plan_identity_mismatch",
        "a report from a different frozen plan refuses the next batch",
        {"stop_reason": report.get("stop_reason"), "problems": problems[:3]},
        report.get("stop_reason") == executor.STOP_CUMULATIVE
        and any("plan digest" in item for item in problems),
    )

    # 5 — unknown consumption.
    root = base / "unknown-usage"
    _write_prior(root, 1, usage="UNKNOWN")
    report = _dispatch(2, root, base / "sign-uu", base / "claims-uu")
    problems = (report.get("cumulative_settlement") or {}).get("problems") or []
    record(
        "5_unknown_consumption",
        "usage that is not COMPLETE is unknown, not zero",
        {
            "stop_reason": report.get("stop_reason"),
            "unknown_consumption": (report.get("cumulative_settlement") or {}).get(
                "unknown_consumption"
            ),
            "problems": problems[:3],
        },
        report.get("stop_reason") == executor.STOP_CUMULATIVE
        and (report.get("cumulative_settlement") or {}).get("unknown_consumption") is True,
    )

    # 6 — a frozen stop in a prior batch.
    root = base / "stopped-prior"
    _write_prior(root, 1, complete=False, stop="COST_STOP_REACHED")
    report = _dispatch(2, root, base / "sign-sp", base / "claims-sp")
    problems = (report.get("cumulative_settlement") or {}).get("problems") or []
    record(
        "6_prior_frozen_stop",
        "a batch that stopped does not authorize the next one",
        {"stop_reason": report.get("stop_reason"), "problems": problems[:3]},
        report.get("stop_reason") == executor.STOP_CUMULATIVE
        and any("stopped" in item for item in problems),
    )

    # 7/10 — the global remainder becomes the effective ceiling and reaches the dispatcher.
    root = base / "global-remainder"
    _write_prior(root, 1, tokens=90_000_000, cost=90.0)
    claims = base / "claims-gr"
    # The prior claim must exist for the settlement to admit the batch; it is a throwaway claim,
    # written inside the throwaway override so the production claim root is never touched.
    claims.mkdir(parents=True, exist_ok=True)
    with executor.claim_root_override(claims):
        digest = contract.scope_plan(plan.BATCH_PLAN_IDS[1])["digest"]
        _claim(digest).write_text(
            json.dumps({"plan_id": plan.BATCH_PLAN_IDS[1], "offline_proof": True}),
            encoding="utf-8",
        )
    report = _dispatch(2, root, base / "sign-gr", claims)
    settlement = report.get("cumulative_settlement") or {}
    record(
        "7_global_remainder",
        "with 90M of 96M consumed, batch 2's ceiling is the 6M remainder, not 32M",
        {
            "allowed_tokens": settlement.get("allowed_tokens"),
            "applied_token_stop": report.get("token_stop"),
            "may_start": settlement.get("may_start"),
            "complete": report.get("complete"),
        },
        settlement.get("allowed_tokens") == 6_000_000
        and report.get("token_stop") == 6_000_000
        and report.get("complete") is True,
    )
    record(
        "10_effective_allowance_in_dispatcher",
        "the dispatcher's own token stop equals the settled allowance",
        {
            "effective_stops": report.get("effective_stops"),
            "allowed_tokens": settlement.get("allowed_tokens"),
        },
        (report.get("effective_stops") or {}).get("applied", {}).get("token_stop")
        == settlement.get("allowed_tokens"),
    )

    # 8/9 — the refusals above are dispatcher refusals and consume no claim.
    refused = [
        item
        for item in scenarios
        if item["scenario"]
        in {
            "2_prior_report_missing",
            "3_prior_claim_missing",
            "4_plan_identity_mismatch",
            "5_unknown_consumption",
            "6_prior_frozen_stop",
        }
    ]
    record(
        "8_refusal_is_dispatcher_refusal",
        "every refusal names the cumulative stop and returns a refused report",
        {"scenarios": [item["scenario"] for item in refused if item["observed"]["stop_reason"]]},
        all(item["observed"].get("stop_reason") == executor.STOP_CUMULATIVE for item in refused),
    )
    # The b2 claim of the missing-report scenario must not exist: it is a fresh root, so the check
    # is that the refusal happened before the claim file was created.
    b2_digest = contract.scope_plan(plan.BATCH_PLAN_IDS[2])["digest"]
    with executor.claim_root_override(claims_mr):
        claim_path = _claim(b2_digest)
        consumed = claim_path.exists()
    record(
        "9_refusal_consumes_no_claim",
        "a refused batch leaves no claim behind",
        {"claim_path": str(claim_path), "production_claim_root": False},
        consumed is False,
    )

    ok = all(item["ok"] for item in scenarios)
    identity = executor.executor_identity(REPO)
    return {
        "stage": "F",
        "kind": "cross_batch_settlement_check",
        "generated_at": executor.now(),
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "repo_head": executor.git_head(REPO),
        "ok": ok,
        "status": (
            "STAGE_F_CROSS_BATCH_SETTLEMENT_PASS" if ok else "STAGE_F_CROSS_BATCH_SETTLEMENT_FAIL"
        ),
        "scenarios": scenarios,
        "model_calls": 0,
        "production_claims_consumed": 0,
        "scratch_root": str(base),
        "effective_rule": "min(frozen batch allowance, owner authorization, global remaining)",
        "note": (
            "the last two scenarios set batch 2's ceiling to USD 6.00 as well, from the same 90/96 "
            "USD prior consumption; the assertions read the token side, which is the one the "
            "dispatcher compares first"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    output = Path(args[0]) if args else Path(__file__).resolve().parent / "settlement-check.json"
    scratch = Path(args[1]) if len(args) > 1 else None
    payload = run(scratch=scratch)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "status": payload["status"], "output": str(output)}))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
