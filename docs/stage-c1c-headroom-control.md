# Stage C1-C — Completion Headroom Control

Status: **`STAGE_C1C_HEADROOM_CONTROL_READY_FOR_LIVE_AUTHORIZATION`**

Stage C1-B ran the P2 coding-delegation cell once and ended **A3**
(`WORKER_COMPLETED_BUT_INTEGRATION_FAILED`). The delegation mechanism held: the Main proposed, the
router admitted two coding workers, both completed with consumable reports, the Main read both
handoffs, consumed the worker code, integrated it and passed the public suite — and then ran out of
shared budget before it could finish repairing the defects it had just found. The independent oracle
scored the export `FAIL`, and the shared budget refused the Main's seventeenth request.

This stage changes exactly one runtime parameter — the shared nominal token budget,
500,000 → 750,000 — and nothing else, and prepares one cell with one attempt. No model was called,
no sandbox was created, no production claim was written or consumed, and no authorization was
signed.

---

## A. What actually limited the C1-B run

Read from `D:\Work\AtlasRuntime-runs\stage-c1b-completion-2` (35 indexed files) — the run's own
session, ledger and matrix report. Nothing below is re-derived by a second implementation: the
session's `metrics` and `budget` blocks are the authority.

| quantity | value |
| --- | --- |
| system tokens (budget accounting) | 485,340 of 500,000 |
| remaining at the stop | 14,660 |
| the refused request | 17, estimated at 26,501 tokens |
| Main requests completed / rejected | 16 / 1 |
| admitted workers / completed workers | 2 / 2 |
| worker turns | 15 and 14 |
| handoff reads by the Main | 2 |
| ownership refusals | 4 |
| Main peak context | 19,393 tokens |
| independent oracle | FAIL |
| execution integrity | PASS |
| cost | USD 0.104162 |
| wall clock | 128.89 s |

The stop is headroom, not mechanism: the workers had already finished and the Main had already
integrated. 750,000 is +50 % over the nominal budget and leaves **264,660 tokens** over the observed
consumption. That is an estimate of headroom, not a prediction that the next trajectory is the same
one.

## B. Reporting metric consistency (C1-B)

The categories are kept apart, and a cache hit is *inside* prompt tokens — never added on top:

| category | value |
| --- | --- |
| Main prompt tokens | 209,711 |
| — cache hit | 195,072 |
| — cache miss | 14,639 |
| Main completion tokens | 5,143 |
| child prompt tokens | 257,950 |
| child completion tokens | 12,536 |
| Main total (prompt + completion) | 214,854 |
| child total (prompt + completion) | 270,486 |
| system total | 485,340 |
| budget accounting (`used_tokens`) | 485,340 |
| budget limit / remaining | 500,000 / 14,660 |

The identities that make those numbers a record rather than a transcription, all checked in C10:

* 209,711 + 5,143 + 257,950 + 12,536 = 485,340 = `budget.used_tokens`;
* 195,072 + 14,639 = 209,711 — the cache split is the Main input, not an extra quantity;
* the two workers' `actual_tokens` (148,470 + 122,016) = 270,486 = child prompt + child completion;
* `remaining_tokens` = `limit_tokens` − `used_tokens`.

Main completion tokens are the arithmetic remainder of the four reported parts, and the session's
recorded field **is** that remainder (5,143). The remainder is not used as a substitute for the
field; the field is checked against it.

Two further readings, from the per-request context series and the trace audit:

| reading | value |
| --- | --- |
| post-write input tokens | 0 (the last write *is* the last turn) |
| post-public-PASS input tokens | 112,262 (6 requests after the PASS) |
| public PASS turn | 10 |
| last code change turn | 16 |

The public suite passed at turn 10 and the Main edited `plugins/csv_fields.py` and
`plugins/csv_plugin.py` at turns 14–16. **The PASS does not describe the exported workspace**, and
this stage does not read it that way. (The "116,664 tokens on 9 turns after the only write" figure
belongs to Stage C1, not to C1-B; C1-B's numbers are the ones above.)

## C. The minimal budget extension

`contract.BUDGET` stays at the historical 500,000. Three files changed, one seam each, no new
framework, no second executor, no monkeypatch, no environment variable:

| file | change |
| --- | --- |
| `experiments/stage_b_plus/contract.py` | `budget_for(cell)` / `token_budget_for(cell)`: a cell may bind its **complete** budget record; a cell with none resolves to `dict(BUDGET)`, exactly as before |
| `experiments/stage_b_plus/authority.py` | `plan_facts()` computes `budget_digest` from the budget the **plan declares** (falling back to the frozen default) and reports `token_budget` alongside it |
| `experiments/stage_b_plus/executor.py` | `production_goal_options()` calls `contract.token_budget_for(cell)`; the executor identity covers `experiments/stage_c1c/plan.py` and moves to `1.6` |

Why this is the whole change: `production_goal_options` is the single place a production
`GoalRunOptions` is built, and both the pre-claim admission and the live runner call it; `run_goal`
turns `options.token_budget` into `RunBudgetState(limit_tokens=max(1, int(...)))`. Nothing else in
the path reads a token budget.

What did **not** move: the B+ canary and formal plans, the C1 plan and the C1-B plan keep 500,000
tokens, `sha256:a6ea4367…` and their own nominal capacities (6 ×, 24 ×, 1 ×, 1 × 500,000). `src/`
was modified in no file.

## D. The C1-C production configuration

One cell, one attempt, `is_benchmark_sample: false`. Every value below is read back from
`production_goal_options()`, not restated by hand:

| parameter | value |
| --- | --- |
| cell | `c1c.closure2-plugin-migration.C3.r1` |
| task / arm | `closure2-plugin-migration` / `C3` (`coding_worker`, exposure `card`) |
| provider / model | `deepseek` / `deepseek-chat` |
| shared nominal token budget | **750,000** (planned 500,000) |
| Main max turns / worker max turns | 20 / 20 |
| max concurrent children / per call / delegation rounds | 2 / 2 / 3 |
| public test max calls | 4 |
| router policy | `m.1.0` (unchanged) |
| ownership | existing `enforce`, effective grant `plugins/` (raw `plugins/**`) |
| sandbox | OpenSandbox, `atlas-uat-python:3.12`, 1 CPU, `1Gi`, network `none`, 30 min |
| matrix concurrency / attempt limit | 1 / 1 |
| Main prompt | `main-v2.2-rc1-c1b`, `sha256:4f742d08…` (unchanged) |
| worker contract | `c1b-1.0`, instructions `sha256:0776e1c6…`, rendered prompt `sha256:5b0fa74e…` (unchanged) |
| goal | `sha256:1496055d…` (unchanged) |

A real `run_goal` on these options — scripted model, fake sandbox, no provider and no sandbox
service — reports `RunBudgetState(limit_tokens=750000, mode=guarded)`. The number is therefore
checked where it is consumed (C03), not only where it is declared.

## E. The plan

`stage-c1c-completion-headroom-control-v1`, scope `C1C_HEADROOM_CONTROL_ONLY`, stage `C1C`,
`sha256:4481bba2d3a0a9e0e3b7b04c5de3c851c70f1d4d03892043e23046782ab727bb`.

The plan record carries the complete effective budget, its digest (inside every cell, therefore
inside the plan digest), the nominal capacity (750,000), the cost-stop candidates (0.25 / 0.25,
unapproved) and the intervention record:

```
kind: resource_headroom_control
shared_token_budget_before: 500000
shared_token_budget_after:  750000
main_prompt_changed / worker_contract_changed / goal_changed / task_changed: false
turns_changed / router_changed / grant_changed / oracle_changed:              false
single_variable_ablation: true    statistical_claim: false
```

Registration: `contract.scope_plan()` resolves the C1-C plan id **and** scope in the same seam used
by C1 and C1-B, so `executor.dispatch(PLAN_ID, …)` never meets the C1-B `unknown scope` defect.
Both entries resolve to the same record; unknown ids and the two superseded B+ canary ids are still
refused.

## F. The authority's budget identity

`authority.plan_facts()` returns `budget_digest sha256:d05da339…` and `token_budget 750000` for this
plan. The pending record binds them; the historical digest `sha256:a6ea4367…` is unchanged for
B+, C1 and C1-B.

Negative controls, each refused with the expected reason (C04):

| candidate | refused for |
| --- | --- |
| the pending record | status, signature, head, both cost stops |
| correctly signed, but `budget_digest` = the 500,000 digest | `budget_digest` |
| correctly signed, but `executor_digest` = `bplus-executor-1.5` | `executor_digest` |
| correctly signed, but `plan_digest` = the C1-B digest | `plan_digest` |
| correctly signed, but `protocol_digest` altered | `protocol_digest` |
| the C1-B record, signed, presented for the C1-C plan | `scope`, `plan_id`, `plan_digest`, `budget_digest` |

A correctly signed record for this plan *is* accepted, so the negatives are not a broken verifier.

## G. The real dispatch entry, offline

`executor.dispatch("stage-c1c-completion-headroom-control-v1", mode=MODE_OFFLINE_PROOF, …)` with a
temporary signed record, a throwaway claim root and the shared synthetic cell: plan-id resolution →
authority verification (against the C1-C budget digest) → one-shot claim → cell → audit → matrix
report. Verdict `PASS`, execution integrity `PASS`, 17 audit checks, `token_stop 750000`,
`cost_stop_usd 0.25`, `plan_digest` in the written matrix report = the C1-C digest. A synthetic PASS
proves the control flow is reachable; it is not evidence that the model completes the task.

## H. Production admission

`run_admission_preflight` on the real C1-C plan: **READY**, no blocking failures, no contract
failures, `coding_worker` / `card` / `EXTERNAL_SANDBOX_ORACLE`, fixture seed matches. The credential
is reported separately (`credential_present`), as it is a fact about the machine.

The router boundary is unchanged (C05): the normalised grant `plugins/` admits the P2 `csv`/`json`
pair as a two-wide `delegate_parallel` wave under `m.1.0`; the raw glob is still refused
(`WRITE_SCOPE_NOT_GRANTED`) and all five boundary shapes still produce their own codes.

## I. Frozen identity

| item | value |
| --- | --- |
| plan | `stage-c1c-completion-headroom-control-v1` / `sha256:4481bba2…` |
| scope | `C1C_HEADROOM_CONTROL_ONLY` |
| executor | `bplus-executor-1.6` / `sha256:59152911c7465b4252763c5e9d07ca4bac26cd5fe23b70d6d5bf5b4ae163714a` |
| protocol | `bplus-2.2.0` / `sha256:65e0b993…` (unchanged) |
| execution contract | `bplus-exec-3.1` / `sha256:242fcef5…` |
| effective budget | `sha256:d05da339…`, nominal capacity 750,000 |
| goal | `sha256:1496055d…` |
| Main prompt | `main-v2.2-rc1-c1b` / `sha256:4f742d08…` |
| worker instructions | `c1b-1.0` / `sha256:0776e1c6…`, rendered `sha256:5b0fa74e…` |
| intervention | `sha256:d08c43c7…` |

The executor identity now covers `experiments/stage_c1c/plan.py`, because the cell's budget is part
of what runs; the resolver itself lives in `contract.py` and `executor.py`, which were already
covered. `experiments/stage_c1b/executor.json` keeps `bplus-executor-1.5` / `sha256:145f6c83…` as a
historical record and is not regenerated.

## J. History

Five consumed production claims are present and byte-identical before and after this preparation:
V1, V2, V3, C1 and C1-B. The C1-B evidence index re-hashes to `sha256:c8505498…` over 35 files, and
`PRE_CLAIM_DISPATCH_ENTRY_FAILURE.json` is intact (`sha256:f629f8d7…`, claim not consumed, 0 cells,
0 model requests, 0 sandboxes). The C1-B `plan.json`, `authorization.json`, `executor.json` and
`offline.json` still carry the C1-B identities (`sha256:d74c4d6f…`, `bplus-executor-1.5`).

## K. Authorization and claims

`experiments/stage_c1c/authorization.json`: `PENDING_OWNER_APPROVAL`,
`authorizes_live_execution: false`, both cost fields **null**, `token_stop` 750,000, attempt limit 1,
eleven stated refusals from `authority.verify`, plus a `review_surface` that names the goal, the
prompt, the worker contract, the grant, the effective budget digest and the unapproved cost-stop
candidates.

No claim exists for `sha256:4481bba2…`.

## L. Exact execution entry

After the Owner signs **the stage commit's HEAD and `bplus-executor-1.6`** with both cost stops
finite and positive, one attempt, in a new run directory:

```python
import asyncio
import sys
from pathlib import Path
from experiments.stage_b_plus import executor

root = Path(r"D:\Work\AtlasRuntime-runs\stage-c1c-headroom-control")
report = asyncio.run(
    executor.dispatch(
        "stage-c1c-completion-headroom-control-v1",
        runs_root=root / "runs",
        evidence_root=root / "evidence",
        authority_path=Path(r"D:\Work\AtlasRuntime-authorizations\stage-c1c-headroom-control.json"),
        mode=executor.MODE_LIVE,
    )
)
print(report.get("stop_reason"), report.get("complete"), report.get("total_cost_usd"))
sys.exit(0 if report.get("complete") else 4)
```

One claim, one attempt, no retry, no resume; the plan digest is the one-shot identity.

## M. Pre-registered reading

A0 `NO_PROPOSAL`; A1 `PROPOSED_REJECTED`; A2 `ADMITTED_BUT_WORKER_FAILED`;
A3 `WORKER_COMPLETED_BUT_INTEGRATION_FAILED`; A4 `REAL_CODING_DELEGATION_E2E_PASS`.

A4 requires all of: a real proposal, router admission, legal worker edits, worker completion with a
consumable report, the Main consuming it, a completed integration, a normal finish, and an
**independent oracle PASS**. A public PASS that precedes the last code change is not a verdict on the
exported workspace and will not be reported as one.

Closure rule: C1-C is the last positive resource-headroom control on P2. Whatever happens, no C1-D,
no prompt change and no further budget increase is created automatically. If L1–L5 hold, the Main
finishes normally and the oracle still fails, the limitation is the task's correctness, recorded as
such; no further P2 tuning follows from a single cell.

Limitations: one cell, one attempt, one model, one task — a mechanism diagnostic, not a
significance claim; the headroom estimate assumes nothing about trajectory reproducibility; the
budget does not buy turns (20/20 unchanged), so a run that needs more turns fails the same way; a
run that needs more than 750,000 tokens would end exactly as C1-B did. The executor identity is a
hash of the files' bytes as checked out, so it is line-ending-bound (CRLF here, like `1.3`–`1.5`):
the live dispatch must run from this working tree, where the recorded digests recompute.
