# Closure-2B-0 — Live Pilot Runner Readiness

Status: `AWAITING_INDEPENDENT_REVIEW` (offline only). Live execution: **not approved, not run**.

Revision `closure-2b-0-r1` adds the pilot scope authorization, honest sandbox accounting, three-state
budget/usage semantics and the native config fingerprint (§R1). The eight-cell pilot is unchanged and
still unapproved; benchmark and pilot approvals are both `PENDING_OWNER_APPROVAL`.

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
9. the six arm digests are distinct;
10. the native config's full effective digest equals `FROZEN_NATIVE_CONFIG_DIGEST` (R1-04 — the
    per-field checks above cannot see a change to pricing, tools or trajectory, and pricing feeds
    the cost estimate).

Any failure is a `PilotContractError` and stops the run — for the dry run it is reported as
`contract_ok: false` and the CLI exits `1`.

## 4. Approval and the execution order

Two independent authorizations, checked in this order so that nothing expensive can happen first:

1. `live_intent` must be `True` (CLI: `--live --confirm-live`; no environment variable can set it);
2. **Layer A — the definition**: `require_execution_approval()` raises `PermissionError` unless the
   live registration still hashes to the frozen digest and `protocol.APPROVAL_RECORD` names that
   digest with approver, commit and timestamp;
3. **Layer B — the scope**: `require_pilot_authorization()` raises `PilotNotAuthorized` unless the
   requested `(cells, repetition)` tuple is exactly the approved one in `PILOT_PLAN`, the record
   names the current plan digest, and the cost ceiling is usable (§4.1);
4. `require_frozen_contract()` — any drift aborts the whole pilot rather than running part of it;
5. only then is an executor constructed and a cell dispatched.

Approving the benchmark does not authorize any cell, and approving a pilot plan does not authorize a
benchmark: both records are still `PENDING_OWNER_APPROVAL`, and this stage changed neither.

### 4.1 Pilot scope authorization (R1-01)

| Object | Contents | Can it change the other? |
|---|---|---|
| `PILOT_PLAN` (frozen) | `plan_id`, benchmark id, preregistration digest, runner revision, the 8 cell ids in execution order, approved repetitions `(0,)`, `max_cell_executions=8`, `estimated_cost_ceiling_usd` | — |
| `PILOT_APPROVAL` (mutable) | `status`, `approved_plan_digest`, `approved_by`, `approval_commit`, `approved_at` | No: moving it to approved cannot change the plan digest |

The plan digest is `canonical_digest(PILOT_PLAN.payload())` — no timestamp, no path, no random id.
Changing a cell, a repetition, the execution cap or the ceiling moves it; changing the approval state
does not.

The requested sequence must equal the authorized sequence **exactly**. An extra cell, a missing cell,
a duplicate cell and an unapproved repetition are each refused with the offending difference named. A
partial retry or a single-cell diagnostic run would need its own authorization record, which this
stage deliberately does not define. `run_live_pilot` takes no approval argument at all, so a caller
cannot hand in its own record; the tests substitute the module-level record instead.

### 4.2 Cost ceiling semantics (R1-01)

`estimated_cost_ceiling_usd` is part of the plan, so approving a plan approves its ceiling. It is
`None` here: the owner sets the number, and until then `cost_policy()["valid"]` is false and a live
run refuses with `COST_CEILING_NOT_APPROVED`. `None` is never read as "unlimited".

**`COST_CEILING_NOT_HARD_ENFORCED`.** Neither engine has a pre-send dollar guard — Atlas guards
*tokens* before send, native only after the run — so nothing here can promise a maximum spend. The
ceiling is checked **between** cells against the runner's `estimated_cost_usd`:

* after each cell, if any cost so far is unknown, dispatch stops (`COST_UNKNOWN`);
* if the known sum exceeds the ceiling, dispatch stops (`COST_CEILING_EXCEEDED`);
* a cell already executing can overspend before that check sees it.

`observed_cost_usd_total` stays `None`: no runner reports a billed amount, and an estimate is not an
observation. `cost_ceiling_guarantee` is always `COST_CEILING_NOT_HARD_ENFORCED`.

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
              provider_token_usage_observed, provider_usage_complete, provider_usage_source,
              estimated_cost_usd, model_calls, model_calls_source, model_calls_semantics,
              tool_calls
budget        budget_exceeded, budget_final_verdict, budget_verdict_basis,
              budget_admission_rejected, budget_observed_limit_exceeded,
              budget_rejected_requests, budget_record, deliverable_passed_but_budget_failed
timing        agent_latency_ms, end_to_end_latency_ms, parallel_overlap_evidence,
              parallel_overlap_source
isolation     workspace_isolated, sandbox_created, sandbox_destroyed, sandbox_creation_status,
              sandbox_count_scope, oracle_sandbox_counted
```

**Missing is missing.** A value the runner does not expose is `None` plus a `*_source` field naming
why; it is never written as `0`. An unknown cache split stays `None` with
`cache_split_status` from the runner, so it cannot be read as a 0 % hit rate.

`parallel_overlap_evidence` is currently `None` with an explicit reason: the runner keeps worker
lifecycle timings in `run_dir/stage_e_evidence.json` and does not return them. Computing overlap from
the DAG would be exactly the "DAG allows parallelism" → "parallelism happened" confusion this schema
is meant to prevent.

### 8.1 Sandbox counting (R1-02)

A sandbox count is only published when every cell's creation state is known:

| Field | Meaning |
|---|---|
| `sandbox_creation_status` (per cell) | `SANDBOX_CREATED_CONFIRMED`, `SANDBOX_NOT_CREATED_CONFIRMED`, `SANDBOX_CREATION_UNKNOWN` |
| `agent_sandbox_runs_confirmed_min` | cells that confirm a created sandbox |
| `agent_sandbox_runs_confirmed_not_created` | cells that confirm none was created |
| `agent_sandbox_runs_unknown` | cells with no lifecycle evidence |
| `agent_sandbox_runs_total` | the confirmed count, but only when `unknown == 0`; otherwise `None` |
| `agent_sandbox_runs_complete` | `unknown == 0` |
| `oracle_sandbox_runs` | always `None` — the oracle runs in its own sandbox and the runner does not count it |

A cell that raises before returning a result has no lifecycle record, so its state is `UNKNOWN` and
the total becomes `None`. A failed *cleanup* (`sandbox_destroyed=false`) does not erase the creation
fact. The scope is named `agent_sandbox_only`; the oracle side is never inferred from
`sandbox_created`.

### 8.2 Budget three-state (R1-03)

`budget_final_verdict` is `EXCEEDED`, `NOT_EXCEEDED` or `UNKNOWN`, and `budget_verdict_basis` says
which evidence produced it:

| Basis | Verdict |
|---|---|
| `runner_failure_class` (`BUDGET_EXCEEDED`) | `EXCEEDED` — outranks any counter |
| `pre_send_rejections` (Atlas `admission_rejections > 0`) | `EXCEEDED` |
| `post_hoc_check` (native `exceeded`) | `EXCEEDED` / `NOT_EXCEEDED` |
| `no_negative_verdict_from_runner` | `UNKNOWN` |

Zero pre-send refusals alone are **not** a negative verdict: the last admitted request can still
cross the threshold, so that case stays `UNKNOWN` (`budget_exceeded = None`). `budget_exceeded` maps
`EXCEEDED → true`, `NOT_EXCEEDED → false`, `UNKNOWN → null`.

`deliverable_passed_but_budget_failed` follows three-state logic:

| `oracle_passed` | `budget_exceeded` | composite |
|---|---|---|
| `false` | anything | `false` (a failed deliverable cannot be "passed but over budget") |
| `true` | `true` | `true` |
| `true` | `false` | `false` |
| `true` | `null` | `null` |

`provider_usage_complete` and `cache_usage_complete` are independent. Neither runner attests that
*every* request reported usage, so token-usage completeness is `null` with
`provider_usage_source = "not_exposed_by_runner: …"`; `provider_token_usage_observed` only says
whether the totals are present. A complete cache split does not imply complete token usage, and it is
never copied across.

## 9. Failure classification

| Situation | Recorded as |
|---|---|
| the cell raised before producing a result | `status=EXECUTION_ERROR`, `failure_class=HARNESS_ERROR`, numeric fields `None`, `sandbox_creation_status=UNKNOWN`; dispatch stops because the spend is now unknown |
| oracle failed on the deliverable | `oracle_passed=false`, `oracle_environment_failure=false` |
| oracle infrastructure failed | `oracle_environment_failure=true` + `oracle_failure_kind` |
| budget exceeded | `budget_exceeded=true` with a basis, separately from `oracle_passed` |
| earned success but over budget | `deliverable_passed_but_budget_failed=true` |
| A2-P on N1 | `resolved_route=direct`, `executed_worker_count=1`, `multi_worker_execution=false` |

No cell is retried on another engine, and no failure is converted into a pass.

## 10. Limitations and what Closure-2B-1 needs

1. **No live cell has run.** Nothing here measured turns, tokens, cache behaviour, latency or cost.
2. **Both approvals are pending.** `execution_ready` and `pilot_execution_ready` are `false`, and this
   stage approved nothing.
3. **The gates are verified, not enforced by a runner.** There is no B-1 runner yet; when one is
   written it must call `require_execution_approval()` and `require_pilot_authorization()` before any
   sandbox or provider work.
4. **`COST_CEILING_NOT_HARD_ENFORCED`.** The ceiling is checked between cells; a cell already running
   can overspend first. No absolute spend guarantee is claimed anywhere in the schema.
5. **OpenSandbox availability is assumed, not tested at this stage.** A real cell needs
   `opensandbox-server` running; the dry run deliberately does not touch it.
6. **Real parallelism is unmeasured.** The multi arms may or may not overlap; the evidence field is
   honest about missing it.
7. **`model_calls` has two sources.** Atlas reports `requests_observed` (requests sent); native falls
   back to `model_turns` (completed responses). `model_calls_semantics` names which one produced the
   number.
8. **One repetition per pilot cell.** No variance estimate, no significance claim, no ranking.
9. **Sandbox counting covers the agent side only.** The oracle sandbox is not observable here, so
   `oracle_sandbox_runs` is `None` and must not be inferred from `sandbox_created`.
10. **Provider usage completeness is unproven.** The archives report aggregated usage; neither runner
    attests that every request reported it, so the field stays `null` until B-1 can prove it.

---

# Closure-2B-0-R1 — authorization and evidence integrity repair

Four defects found by review of B-0. Each was reproduced on `9e3ce87` before being fixed. No frozen
task, config, router policy or budget value changed; the preregistration digest is still
`3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765`.

| Issue | Root cause on B-0 | Repair |
|---|---|---|
| B0-R1-01 | `require_execution_approval()` approves the *benchmark definition*, not a cell scope; `--cells` and `--repetition` were unconstrained, so an approval of "the benchmark" could be stretched into any 18 cells × any repetition | `PilotPlan` + `pilot_plan_digest()` + `PILOT_APPROVAL` + `pilot_authorization_gate()`; the requested `(cells, repetition)` must equal the approved one exactly, the plan digest must be the approved one, and the cost ceiling must be usable |
| B0-R1-02 | an exception path recorded no `sandbox_created` key, and the summary summed only `sandbox_created is True`, so a cell that provisioned a sandbox and then raised was undercounted | per-cell `sandbox_creation_status` (`CREATED` / `NOT_CREATED` / `UNKNOWN`), `agent_sandbox_runs_confirmed_min`, `agent_sandbox_runs_unknown`, `agent_sandbox_runs_total = None` unless complete, and an explicit `agent_sandbox_only` scope with `oracle_sandbox_runs = None` |
| B0-R1-03 | `budget_exceeded` was inferred from `admission_rejections`, and `provider_usage_complete` was copied from `cache_metrics_complete` | `budget_final_verdict` + `budget_verdict_basis` (runner verdict outranks counters), three-state `deliverable_passed_but_budget_failed`, and independent `provider_token_usage_observed` / `cache_usage_complete` with `provider_usage_complete = null` |
| B0-R1-04 | `check_frozen_contract()` checked native *fields* but not the whole effective config, so a pricing/tools/trajectory change would slip through | `FROZEN_NATIVE_CONFIG_DIGEST` = `6cb2d1d91badaa235efa4427e5a53bdfc17610931e7b4c13e524406c2a863d14`, verified with the same `config_semantic_digest()` used everywhere else and enforced in `check_frozen_contract()` |

## R1.1 Order of the live path

```text
live_intent  ->  benchmark approval (Layer A)  ->  pilot scope approval (Layer B)
             ->  frozen contract (incl. native config digest)  ->  executor
             ->  after each cell: cost dispatch check (unknown or over ceiling stops dispatch)
```

## R1.2 Accepted behaviour, exercised offline

* extra cell / missing cell / duplicate cell / unapproved repetition / wrong plan digest /
  missing approver|commit|timestamp / missing or non-positive ceiling → refused before any executor;
* benchmark approved alone → refused; pilot approved alone → refused;
* a cell whose cost is unknown, or whose sum crosses the ceiling, stops the next dispatch with
  `stopped_reason` and `not_started_cells` recorded;
* a raised cell is `EXECUTION_ERROR` with `sandbox_creation_status=UNKNOWN`, and the pilot total
  sandbox count becomes `None`;
* `admission_rejections=0` with `failure_class=BUDGET_EXCEEDED` → `EXCEEDED` (basis
  `runner_failure_class`);
* oracle pass with unknown budget → `deliverable_passed_but_budget_failed = null`;
* token totals present with no cache split → `provider_token_usage_observed=true`,
  `provider_usage_complete=null`, `cache_usage_complete=null`;
* a pricing/tools/provider/trajectory edit in the native YAML moves its digest; a comment-only edit
  does not; restoring the file restores the frozen digest.
