# Closure-2B-0 — Live Pilot Runner Readiness

Status: `AWAITING_INDEPENDENT_REVIEW` (offline only). Live execution: **not approved, not run**.

This stage adds one thin orchestration module over the frozen Closure-2A benchmark. It implements no
agent loop, no scheduler, no worker, no sandbox and no oracle: it maps six frozen arms onto the two
engines that already exist, verifies every frozen identity before planning or executing, and writes
one flat evidence record per run.

| Item | Value |
|---|---|
| Stage | `closure-2b-0` |
| Benchmark | `closure2-final-benchmark-v1` |
| Preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` → see §3 |
| Router | `r0.1.0`, `5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244` |
| Planned cells | 18 (3 tasks × 6 arms) |
| Proposed live pilot | 8 cells, 1 repetition — awaiting owner approval |
| Dry-run artifact | `experiments/results/closure2/pilot-dry-run.json` |
| Model calls | 0 |
| Sandbox runs | 0 |

---

## 1. Entry points and reused components

| Concern | Reused, not reimplemented |
|---|---|
| Atlas execution | `experiments.evaluation.runner.run_sample` |
| Native execution | `experiments.evaluation.closure1b.native_baseline.run_native_sample` |
| Frozen task builder | `closure2.catalog.build_task(task, topology=...)` |
| Frozen identity | `closure2.protocol` (digest, arm digests, budget, approval gate) |
| Router decision | `closure2.preflight.router_audit` (the shipped `r0.1.0` policy) |
| Oracle | whatever each runner already calls — `evaluate_oracle_isolated`, sandboxed |
| Settings contract | `experiments.evaluation.runner.RunnerSettings` |

New module: `experiments/evaluation/closure2/pilot_runner.py` (single file, by design).

Two entry points, never one entry point with a switchable flag:

```bash
python -m experiments.evaluation.closure2.pilot_runner --dry-run     # planning only (default)
python -m experiments.evaluation.closure2.pilot_runner --live --confirm-live
```

`--live` without `--confirm-live` exits `2` with `LIVE_INTENT_NOT_CONFIRMED`. There is no environment
variable, config key or default that selects live. `run_live_pilot(..., live_intent=False)` — the
default — refuses before it looks at anything else.

## 2. Six-arm mapping

Every arm goes through `catalog.build_task`, so Native and Atlas receive the same public task input.

| Arm | Engine | Topology | Layout | Route | Config | `budget_mode` |
|---|---|---|---|---|---|---|
| N0 | native AnyCode | single | native | off | `eval-benchmark.yaml` | `legacy` (post-hoc) |
| N1 | native AnyCode | multi | native | off | `eval-benchmark.yaml` | `legacy` (post-hoc) |
| A0 | Atlas | single | legacy | off | `final4-context.yaml` | `guarded` |
| A1-L | Atlas | multi | legacy | off | `final4-context.yaml` | `guarded` |
| A1-P | Atlas | multi | shared_prefix | off | `closure2-shared-prefix.yaml` | `guarded` |
| A2-P | Atlas | multi candidate | shared_prefix | selective (`r0.1.0`) | `closure2-shared-prefix.yaml` | `guarded` |

Per-arm identity recorded in the manifest (values are the frozen ones; `settings_digest` is new and
covers the exact `RunnerSettings` each arm will run with):

| Arm | `settings_digest` | `arm_config_digest` | `budget_semantics` |
|---|---|---|---|
| N0 | `fc11cccc5d94fec8…` | `e74f8e36fafb5310…` | `post_hoc_after_run` |
| N1 | `9a4864c35e0bd25f…` | `aadc7f129ce23239…` | `post_hoc_after_run` |
| A0 | `190989d6eaf2cf5a…` | `4ae0f21142df3ff6…` | `guarded_before_send` |
| A1-L | `6eff1bcef4a571a4…` | `296a1db1e70124e9…` | `guarded_before_send` |
| A1-P | `228d90671eab5dce…` | `fb1a2dff282e84bd…` | `guarded_before_send` |
| A2-P | `627e35dcc4d2ad20…` | `a1bc879ad7c1227a…` | `guarded_before_send` |

**`A2-P` records the executed topology, not the arm name.** The manifest carries
`resolved_route_expectation` plus `executed_worker_count` / `executed_topology`:

| Task | Expected route | Executed workers | Executed topology |
|---|---|---|---|
| P1 | `delegate_parallel` | 3 | `multi` |
| P2 | `delegate_parallel` | 3 | `multi` |
| N1 | `direct` (`SHARED_WRITE_RISK`, `UNSTABLE_INTERFACE`, `GATE_2_DEPENDENCY_COUPLING`) | 1 | `single_worker_direct` |

The route expectation is computed by the shipped policy, and is labelled
`route_expectation_is_prediction: true` in the artifact. It is not an execution result. When A2-P and
A1-P resolve to the same DAG, the pair is not two independent samples and must not be reported as a
router quality gain.

**Model.** The native engine resolves `settings.model_override or "deepseek-chat"` and never reads
the YAML; Atlas resolves `settings.model_override or config.provider.model`. Every arm therefore gets
an explicit `model_override` read from the frozen config, so "both engines used the same model" is a
fact rather than an inference from the arm name.

## 3. Frozen tasks and digests

Tasks, seeds, references, oracles, requirements and DAGs are untouched; the runner only reads them.

| Task | Family | `effective_public_task_digest` (single / multi) |
|---|---|---|
| `closure2-pipeline-migration` | P1 | `b89987bd…` / `c4b5373c…` |
| `closure2-plugin-migration` | P2 | `4e97b92d…` / `8bf208d3…` |
| `closure2-state-machine-refactor` | N1 | `7014163b…` / `104306bf…` |

`check_frozen_contract()` re-derives, on every run:

1. preregistration digest equals `3080f3137a408856337681c14ff4e10f…` (frozen);
2. router policy digest equals `5e13c910…`;
3. the arm matrix and the task order are the frozen ones;
4. every task digest equals the C2-A table (`C2A_FROZEN_TASK_DIGESTS`);
5. the budget table is `36 / 260 000 / 90 / 1800` (single) and `12 × 3 / 260 000 / 90 / 1800`;
6. the shared contract is image `atlas-uat-python:3.12`, `1` CPU, `1Gi`, `network=none`,
   `/workspace/project`, temperature `0`, `max_tokens` 8000;
7. all three YAML configs resolve to that same sandbox block and the same provider model;
8. the A1-L/A1-P configs still differ **only** in `context.prompt_layout`;
9. the six arm digests are distinct.

Any failure is a `PilotContractError` and stops the run — for the dry run it is reported as
`contract_ok: false` and the CLI exits `1`.

## 4. Approval and the execution order

`run_live_pilot` is ordered so that nothing expensive can happen first:

1. `live_intent` must be `True` (CLI: `--live --confirm-live`);
2. `require_execution_approval()` — raises `PermissionError` unless the live registration still hashes
   to the frozen digest and the approval record names that digest with approver, commit and
   timestamp;
3. `require_frozen_contract()` — any drift aborts the whole pilot rather than running part of it;
4. only then is an executor constructed and a cell dispatched.

The approval record is still `PENDING_OWNER_APPROVAL` and this stage did not change it. Offline
evidence that the gate is on the path: unit tests assert the executor list stays empty for a pending
record, a wrong digest, a missing approver/commit/timestamp, and a drifted budget.

## 5. Dry-run semantics

`build_dry_run_manifest()` reads YAML and frozen Python data only. It creates no sandbox, builds no
provider adapter, reads no API key and makes no network call. The integration suite proves the first
two by making `build_sandbox_provider` raise if it is ever called during a dry run.

The artifact states its own nature:

```json
{ "mode": "DRY_RUN", "model_calls": 0, "sandbox_runs": 0,
  "contains_live_outcomes": false, "route_expectation_is_prediction": true }
```

Each cell carries: benchmark id, task, arm, engine, runner, topology, layout, requested route,
resolved route expectation (with reason codes and blocked gates), candidate DAG, model, config path
(repository-relative), config semantic digest, arm config digest, effective public task digest,
settings digest, preregistration digest, the budget block, the budget semantics, and
`execution_allowed: false`.

## 6. Proposed live pilot (awaiting approval)

| Task | Arms | Purpose |
|---|---|---|
| P1 | N0, N1, A0, A1-L, A1-P, A2-P | the six-arm execution chain and the main fairness pairs |
| P2 | A1-P | a second positive DAG through the same chain |
| N1 | A2-P | the router-refused path that must run direct |

Eight cells, one repetition each. This is a proposal:

* the owner reviews the cell selection and the cost ceiling;
* the owner approves the preregistration digest, which changes `execution_ready` to `true`;
* the pilot manifest is frozen before the first cell runs;
* no cell may be dropped after seeing a result, and no statistical claim may rest on one repetition;
* Closure-2C keeps the three paired repetitions for the formal matrix.

## 7. Budget and fairness

| Knob | Single | Multi |
|---|---|---|
| turns | 36 | 12 per worker × 3 |
| run token budget | 260 000 | 260 000 |
| tool calls | 90 | 90 |
| wall clock | 1800 s | 1800 s |
| temperature / `max_tokens` | 0.0 / 8000 | 0.0 / 8000 |

`RunnerSettings` defaults (14 turns, 120 000 tokens, 40 calls, 900 s, 4096 tokens) are **all different**
from the frozen contract, so `settings_for()` passes every value explicitly and a test pins the
result.

The enforcement mechanisms are **not** made equivalent:

* Atlas admits each request against a shared run budget **before sending** (`budget_mode="guarded"`);
* native has no in-run guard: the same thresholds are applied **after the run**
  (`budget_mode="legacy"`).

Every record carries `budget_semantics`, `budget_exceeded`, `budget_rejected_requests`,
`budget_record`, and `deliverable_passed_but_budget_failed`. Equal theoretical turns are not equal
scheduling opportunities, and the reports must say so.

## 8. Evidence schema

One flat record per cell (`normalize_result`). Raw `result.json`, `run.json` and, when captured,
`stage_e_evidence.json` stay in `run_dir` untouched; the summary only points at them
(`raw_result_path`, `raw_evidence_preserved: true`).

```text
identity      benchmark_id, protocol_revision, preregistration_digest, repair_revision,
              task_id, arm_id, repetition_id, run_id, run_dir, git_commit, git_dirty
digests       task_digest, effective_public_task_digest, arm_config_digest, settings_digest
execution     engine, topology, resolved_route, executed_worker_count, multi_worker_execution,
              single_worker_downgrade, model, sandbox_image, budget_semantics
outcome       agent_success, oracle_passed, task_success, failure_class, failure_reason,
              oracle_environment_failure, oracle_failure_kind
usage         provider_prompt_tokens, provider_completion_tokens, cache_hit_tokens,
              cache_miss_tokens, cache_usage_complete, cache_split_status,
              provider_usage_complete, provider_usage_source, estimated_cost_usd,
              model_calls, model_calls_source, tool_calls
budget        budget_exceeded, budget_rejected_requests, budget_record,
              deliverable_passed_but_budget_failed
timing        agent_latency_ms, end_to_end_latency_ms, parallel_overlap_evidence,
              parallel_overlap_source
isolation     workspace_isolated, sandbox_created, sandbox_destroyed
```

**Missing is missing.** A value the runner does not expose is `None` plus a `*_source` field naming
why; it is never written as `0`. An unknown cache split stays `None` with
`cache_split_status` from the runner, so it cannot be read as a 0 % hit rate.

`parallel_overlap_evidence` is currently `None` with an explicit reason: the runner keeps worker
lifecycle timings in `run_dir/stage_e_evidence.json` and does not return them. Computing overlap from
the DAG would be exactly the "DAG allows parallelism" → "parallelism happened" confusion this schema
is meant to prevent.

## 9. Failure classification

| Situation | Recorded as |
|---|---|
| the cell raised before producing a result | `status=EXECUTION_ERROR`, `failure_class=HARNESS_ERROR`, numeric fields `None`; the pilot continues |
| oracle failed on the deliverable | `oracle_passed=false`, `oracle_environment_failure=false` |
| oracle infrastructure failed | `oracle_environment_failure=true` + `oracle_failure_kind` |
| budget exceeded | `budget_exceeded=true`, separately from `oracle_passed` |
| earned success but over budget | `deliverable_passed_but_budget_failed=true` |
| A2-P on N1 | `resolved_route=direct`, `executed_worker_count=1`, `multi_worker_execution=false` |

No cell is retried on another engine, and no failure is converted into a pass.

## 10. Limitations and what Closure-2B-1 needs

1. **No live cell has run.** Nothing here measured turns, tokens, cache behaviour, latency or cost.
2. **Approval is pending.** `execution_ready` is `false` and this stage did not approve anything.
3. **The gate is verified, not enforced by a runner.** There is no B-1 runner yet; when one is
   written it must call `require_execution_approval()` before any sandbox or provider work.
4. **Cost control is a manual decision.** The pilot has no per-run cost cap beyond the token budget;
   the owner sets the ceiling before approval.
5. **OpenSandbox availability is assumed, not tested at this stage.** A real cell needs
   `opensandbox-server` running; the dry run deliberately does not touch it.
6. **Real parallelism is unmeasured.** The multi arms may or may not overlap; the evidence field is
   honest about missing it.
7. **`model_calls` has two sources.** Atlas reports `requests_observed`; native falls back to
   `model_turns`. The source is recorded per record and must be read before comparing arms.
8. **One repetition per pilot cell.** No variance estimate, no significance claim, no ranking.
