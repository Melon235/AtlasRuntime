# Closure-2 S07 — Formal Evaluation Preparation

Status: `CLOSURE2_S07_PREPARATION_READY_FOR_REVIEW`.

> **Superseded in part.** `docs/closure-2-s07-preparation-closure.md` closes the owner decisions this
> report lists in §K (R-1, R-4, R-5, R-6) and replaces its cost candidate in §I. Read that document
> for the current state; this one stays as the record of what S07 first measured.

This stage prepares the Closure-2 formal six-arm evaluation and runs nothing. It reads the existing
preregistration, verifies the shipped implementation against it, diagnoses why S06 produced no
delegation, and turns the 54-cell matrix into a reproducible offline plan. No model request, no
sandbox, no canary, no pilot, no formal cell.

| Item | Value |
|---|---|
| Branch | `feat/closure-2-s07-preparation`, off `e0bc2cc` (the S06 execution HEAD) |
| Frozen preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` |
| Preflight | `PASSED`, 62 checks, 0 failed, 0 model calls, 0 sandbox runs |
| Candidate formal plan | `experiments/results/closure2/formal-plan-candidate.json`, 3 plans x 18 cells |
| Formal executions prepared | 54 (6 arms x 3 tasks x 3 repetitions) |
| Live runs this stage | 0 |
| PR #6 | untouched: `feat/stage-b-s05-freeze-1-6` still points at `e0bc2cc`, the PR is still draft/open |

---

## A. Git provenance

| Item | Value |
|---|---|
| Repository | `D:\Work\AtlasRuntime` (`Melon235/AtlasRuntime`) |
| Baseline | `e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1` — the exact commit S06 executed |
| Branch | `feat/closure-2-s07-preparation`, created with `--no-track` from the baseline |
| Plan commit | `20ddd3e` — `experiments/evaluation/closure2/formal_plan.py`, its 20 offline tests, and the candidate artifact |
| Documentation commit | the commit that carries this file |
| Runtime source | unchanged: `git diff e0bc2cc -- src/` is empty |
| Protocol source | unchanged: `protocol.py`, `preflight.py`, `pilot_runner.py`, `pilot_ledger.py`, `connectivity_canary.py`, `catalog.py` are byte-identical to `e0bc2cc` |
| S06 execution identity | preserved as history: `e0bc2cc` is still the merge base, PR #6 still points at it, nothing was force-pushed or amended |

The S06 result artifacts stay out of this branch on purpose. Publishing a 36-cell summary here would
attach new bytes to the commit the S06 authorization binds; S07 produces a separate document set
instead.

---

## B. S06 evidence reference and immutability

Custody root, outside the repository: `D:\Work\AtlasRuntime-s06-live-20261124-231047`.

| Artifact | Statement |
|---|---|
| `matrix-run.json` | approved head `e0bc2cc`, executor 1.2.1, exit code 0, `COMPLETED`, 1671.219 s, authorization bytes identical before and after |
| `reconciliation.json` | independent re-audit of all 36 cells, `problems: []` |
| `analysis.json` | 36 cells, delegation block: `proposals 0`, `admitted 0`, `rejected 0`, `router_states {NO_PROPOSAL: 36}` |
| `evidence-index.json` | 1048 files, 16 958 603 bytes |
| `archive/cells` | the sealed copy the index describes |

**Re-verified read-only this stage.** All 1048 entries of `evidence-index.json` were re-hashed against
`archive/cells`: **1048 match, 0 mismatch, 0 missing**. Nothing in that tree was written, renamed,
moved or deleted by S07; the verification opened files for reading only.

S06 execution identity to keep quoting: `e0bc2cc`, executor `stage-b-matrix-executor/1.2.1`,
`executor_digest sha256:438fe815…`, config `sha256:05f366b5…`, freeze `stage-b-execution/1.6`
(`raw sha256:004d4b77…`, `content sha256:73713095…`), authorization `sha256:9165bf75…`.

Published numbers from that run, repeated here because S07 reasons from them:

| Metric | Value |
|---|---|
| Cells | 36 dispatched, 36 observed, 30 executor PASSED, 6 FAILED, 0 invalid |
| Goal / oracle / public pass | 31 / 31 / 34 |
| Provider requests | 432 |
| Tokens | 3 032 172 (hit 2 754 304, miss 186 553, completion 91 315) |
| Cache-hit ratio | 93.66 % |
| Estimated cost (frozen prices) | USD 0.182070 |
| Delegation | 36/36 enabled, 0 proposals, 0 children, 0 handoffs, 0 child tokens |
| Retry / resume | 0 / 0 |

---

## C. Existing Closure-2 preregistration audit

Verified from the repository, not from the stage brief.

| Item | Verified value |
|---|---|
| Preregistration | `docs/closure-2-preregistration.md`, protocol revision `closure-2a-r1`, repair revision `closure-2a-r2` |
| Digest | recomputed `3080f313…` = pinned `C2A_R1_PREREGISTRATION_DIGEST`; superseded `f7bbdfb8…` kept as a constant |
| Tasks | P1 `closure2-pipeline-migration`, P2 `closure2-plugin-migration`, N1 `closure2-state-machine-refactor` |
| Task digests | all eight per task match `C2A_FROZEN_TASK_DIGESTS` |
| Arms | `N0, N1, A0, A1-L, A1-P, A2-P` |
| Matrices | `EXECUTION_ORDER` = tasks x repetitions outer, arms inner, arm order rotated per repetition, 3 repetitions |
| Budget | single 36 turns / 260 k / 90 calls / 1800 s; multi 12 per worker x 3 / 260 k / 90 calls / 1800 s |
| Router | `r0.1.0`, digest `5e13c910…`, unchanged since Closure-1 |
| Preflight | 62 checks, `PASSED` |

### C.1 Existing

* Frozen tasks, oracles, requirements, DAGs, deliverables and fixtures, each with a digest table.
* Six arms mapped onto two real engines: `experiments.evaluation.runner.run_sample` (Atlas) and
  `experiments.evaluation.closure1b.native_baseline.run_native_sample` (native AnyCode 0.9.0).
* The 18-cell plan (`pilot_runner.plan_cells()`), the arm adapter, the frozen-settings builder
  (`settings_for`), the result normaliser (`normalize_result`), and the contract checker
  (`check_frozen_contract`, no problems).
* Two-layer authorization: benchmark-definition approval (`protocol.APPROVAL_RECORD`) and
  execution-scope approval (`PILOT_APPROVAL`, `ROUTING_PAIR_APPROVAL`, `CANARY_APPROVAL`).
* A one-shot SQLite lease keyed by plan digest (`pilot_ledger`), plus a separate namespace for the
  canary.
* An isolated sandboxed oracle with the hidden tests, and four independent pytest sessions per task
  that prove the oracle is real.
* The connectivity canary (`connectivity_canary.py`) with its own id, ledger, run root and evidence.

### C.2 Missing (nothing here was added in S07 unless stated)

| Gap | Consequence | Where it bites |
|---|---|---|
| No formal-matrix plan wired into the runner | the 54-cell matrix cannot be dispatched at all | S08/S11 |
| One claim per plan digest, claim never released; `run_live_pilot` runs one repetition per call | a 3-repetition plan cannot be executed in one scope | S08 (solved by three repetition-scoped plans, see §G) |
| No cross-repetition order wrapper (the S06 `run_matrix_once.py` equivalent for Closure-2) | no single dispatch, no exit-code capture, no matrix-level stop | S08 |
| No 54-cell aggregate analysis (only the 2-cell `routing_pair_analysis.py`) | cross-arm tables and paired comparisons do not exist yet | S12 |
| `parallel_overlap_evidence` is hard `None` in `normalize_result` | parallel-execution overlap cannot be reported | permanent unless worker timings are exposed |
| Main prompt override absent from `RunnerSettings` | the S06 P3 prompt (`main-v2.1-rc1`) cannot be used in Closure-2 without a new setting | S10 decision |
| Canary plan is `CONSUMED_BY_EXECUTION` | a new canary scope needs a new digest, not a status edit | S08 |
| `docs/closure-2-preregistration.md` §4 still shows `PENDING_OWNER_APPROVAL` while `protocol.APPROVAL_RECORD` is `APPROVED` | the document and the code disagree about the approval state | owner decision R-1 |

### C.3 Reusable

* `preflight.py` — 62 checks, all offline. Reuse unchanged.
* `pilot_runner.py` — arm adapter, `settings_for`, `normalize_result`, cost policy, lease handling.
  Reuse with a third plan id.
* `pilot_ledger.py` — reuse unchanged; the per-plan granularity is what forces three plans.
* `connectivity_canary.py` — reuse the mechanism (`OFFLINE_PRECHECK` / `CONNECTIVITY_CANARY`
  separation, own ledger, own evidence) with a new scope digest.
* `catalog.py` + the R1-2H isolated oracle — reuse unchanged.
* `experiments/evaluation/runner.py` and `closure1b/native_baseline.py` — both engines, unchanged.
* S06's *patterns*, not its code: exit-code-observing wrapper, reconciliation pass, analysis pass,
  out-of-repo custody with an in-repo index, frozen-price cost accounting.

---

## D. Zero-proposal diagnosis

The question S06 left open: did the Main never ask, or did the router refuse?

### D.1 What the evidence says

| Evidence | Value | File |
|---|---|---|
| Tool surface per cell | contains `delegate_tasks` and `delegate.read` in **36/36** cells | `session.json → tool_surface` |
| Delegation enabled | `true` in 36/36, limits `max_parallel_children=2`, `max_children_per_call=2`, `max_delegation_rounds=3`, `child_max_turns=6` | `session.json → delegation` |
| Actual delegation calls | **0** across 36 runs | `session.json → main.tool_calls` |
| Router records | `{"decisions": [], "reason": "no delegation proposal in this run", "status": "NOT_APPLICABLE"}` in all 36 | `router_decisions.json` |
| Analysis block | `proposals 0`, `admitted 0`, `rejected 0`, `router_states {NO_PROPOSAL: 36}` | `analysis.json` |
| Children / handoffs / child tokens | 0 / 0 / 0 | `analysis.json` |

**This is `NO_PROPOSAL`, not `ROUTER_REJECTED`.** No admission decision was ever requested, so S06
contains no evidence that the router refuses anything — and none that it admits correctly either.
`rejected: 0` must never be read as "the router rejected the work".

### D.2 Task structure (the dominant explanation)

Every S06 task is a single-module (or two-module) implementation of an unimplemented function or
feature. The host-declared write scope per cell:

| Task | Writable files | Goal shape |
|---|---|---|
| `zonelint` | `zonelint/preflight.py` | implement `run_preflight` |
| `slugkit` | `slugkit/core.py` | implement the slug rules |
| `pager` | `pager/page.py` | implement `page_bounds`, `next_cursor` |
| `retrygate` | `retrygate/policy.py` | implement `decide()` |
| `ledgerclose` | `ledgerclose/close.py` | implement the closing statement |
| `cascade` | `cascade/lexer.py`, `cascade/parser.py` | add the `^` operator |

Five of six are **one** file. There is no independent unit a subworker could own: a worker cannot take
half a module, and the host would have to grant it the same single path the Main already holds. The
task set therefore contains no natural parallel decomposition, which is exactly the condition the
prompt v2.1 checkpoint describes as "work directly".

Corroborating numbers: 12.0 mean turns of a 20-turn cap, 33/36 cells stopping at `success`, and both
turn-cap failures were `cascade` (the only two-file task). Nothing in the runs suggests a Main that was
short of capacity.

### D.3 Worker capability (the second independent cause)

`experiments/stage_benchmark/protocol.py` freezes `worker_profiles: ("research_worker",)`. In
`src/atlasos/workers/catalog.py` that profile is `base_tools = (tool_search, dispatch_action,
file_read, grep, list_files)`, `max_turns = 6`, `can_delegate = False` — no `file_write`, no
`file_edit`, no `bash`, no `run_public_tests`. In S06 a delegated child could not have written the file
the task was about. Delegation could only ever have bought *investigation*, and each task's
investigation was a handful of file reads the Main had already done by turn 8.

The runtime does ship a `coding_worker` (`file_write`, `file_edit`, `bash`, `max_turns = 8`). S06's
frozen protocol excluded it. The capability gap was therefore a **protocol decision**, not an engine
limitation — and it is the decision that most directly made implementation delegation impossible.

### D.4 Prompt wording (a contributing factor, not a proven cause)

`main_v21.md` says "Delegate independent **read-only investigations** early when the expected benefits
justify worker cost", adds a capability contract telling the Main that `research_worker` has no bash,
python or pytest, and forbids delegating test execution to it. It also keeps "Direct is valid when the
work is small, tightly coupled and coordination costs more". Under those instructions, Direct is the
specified answer for six single-file implementations — but v1.0 and v2.0 produced the same zero, so
the wording cannot be isolated from the task and capability causes with S06 data.

### D.5 Hypotheses, and what would falsify each

| # | Hypothesis | Evidence for | Falsifier |
|---|---|---|---|
| H1 | The task set offers no independent unit to own | 5/6 tasks are one file; all six are single-module | a task with two disjoint writable regions and a real shared integration point still yields 0 proposals |
| H2 | The only worker profile cannot do the work | `worker_profiles = ("research_worker",)`, read-only | a writable worker profile is present and proposals are still 0 |
| H3 | The prompt's Direct bias suppresses proposals | v2.1 checkpoint + capability contract + "not delegation for its own sake" | with the same tasks and profiles, a neutral prompt yields proposals (not testable within S06's budget) |
| H4 | The delegate tool is unusable in practice | none: it was registered in 36/36, `side_effecting=False`, no refusal was recorded | a run where the Main calls it and the runtime refuses for schema reasons |

H1 and H2 are supported by static evidence and are testable in Closure-2: A1-P hands the Main a
pre-decomposed 3-worker DAG with disjoint write sets, so the parallel unit exists by construction.
H3 and H4 stay unproven; S07 asserts nothing about them.

**What this means for Closure-2:** the Closure-2 multi arms do not depend on the Main proposing
anything. `DelegationRouteOff` executes the frozen candidate DAG directly, and `selective` only lets
the router collapse it to one worker. Closure-2 therefore measures multi-worker benefit without
depending on the behaviour that S06 could not elicit.

---

## E. Workload topology analysis

All three tasks are migrations of a working v1 repository to a frozen v2 contract published in
`docs/INTERFACE.md`. Numbers below are from the preflight's `dag` and `router` records.

### E.1 P1 `closure2-pipeline-migration` (positive)

| Step | Owns | Depends on | Wave |
|---|---|---|---|
| `parse` | `pipeline/parsing.py`, `pipeline/normalise.py` | — | 0 |
| `aggregate` | `pipeline/aggregate.py`, `pipeline/store.py` | — | 0 |
| `integrate` | `pipeline/api.py`, `pipeline/report.py` | `parse`, `aggregate` | 1 |

Wave width 2, write sets disjoint, shared write risk none, interface proven against the public fixture.
Integration cost: `integrate` composes both halves and is where a wrong contract surfaces. Handoff
need: the two wave-0 workers must pass their *interfaces*, not their files, to `integrate` — which is
what the runtime's dependency delivery is for.

### E.2 P2 `closure2-plugin-migration` (positive)

| Step | Owns | Depends on | Wave |
|---|---|---|---|
| `csv` | `plugins/csv_plugin.py`, `plugins/csv_fields.py` | — | 0 |
| `json` | `plugins/json_plugin.py`, `plugins/json_fields.py` | — | 0 |
| `register` | `plugins/registry.py`, `plugins/loader.py` | `csv`, `json` | 1 |

Wave width 2, disjoint, and genuinely different local problems (CSV quoting vs nested JSON) so the two
halves are not the same task twice. `plugins/base.py` is already v2 and forbidden to edit, which is
what makes the split real rather than speculative.

### E.3 N1 `closure2-state-machine-refactor` (negative control)

| Step | Owns | Depends on | Wave |
|---|---|---|---|
| `machine` | `lifecycle/state.py` | — | 0 |
| `callers` | `lifecycle/approval.py`, `lifecycle/audit.py`, `lifecycle/shipping.py`, `lifecycle/state.py` | `machine` | 1 |

Wave width 1, and **both steps write `lifecycle/state.py`**. The frozen router refuses the plan:
`GATE_2_DEPENDENCY_COUPLING` = `block`, reason codes `DATA_DEPENDENCY`, `SHARED_WRITE_RISK`,
`UNSTABLE_INTERFACE`, with `GATE_1` reporting `NO_PARALLEL_WAVE` as info. This is the case where
decomposition is not available, and it exists to check that selective routing says so.

### E.4 Does each task actually test the mechanism?

| Question | P1 | P2 | N1 |
|---|---|---|---|
| Independent nodes | 2 | 2 | 0 |
| Parallel wave width | 2 | 2 | 1 |
| Shared write risk | none | none | `lifecycle/state.py` in both steps |
| Ownership boundary | file-exact | file-exact | overlapping |
| Artifact handoff | interfaces only | interfaces only | the same file |
| Integration cost | one composing module | one composing module | the whole refactor |
| Main could do it alone | observed (N0 and A0 both passed) | **not observed** — P2 never ran single | observed (A2-P collapsed to one worker and passed) |
| Oracle independent | 11 hidden tests | 12 | 7 |

None of the three tasks was designed to be solvable *only* by delegation, and None was padded: every
file is a real module in the dependency chain. The positive tasks are large enough that the
multi-worker arm has to pay real integration cost — which is the effect being measured.

**No task, oracle, requirement, DAG or fixture was modified in S07.** The digests are the C2-A ones.

---

## F. Six-arm implementation and fairness

### F.1 Arm to implementation, verified per arm

| Arm | Runner | Topology | Layout | Route | Config | Workers (P1/P2/N1) |
|---|---|---|---|---|---|---|
| N0 | `run_native_sample` | single | native | off | `eval-benchmark.yaml` | 1/1/1 |
| N1 | `run_native_sample` | multi | native | off | `eval-benchmark.yaml` | 3/3/2 |
| A0 | `run_sample` | single | legacy | off | `final4-context.yaml` | 1/1/1 |
| A1-L | `run_sample` | multi | legacy | off | `final4-context.yaml` | 3/3/2 |
| A1-P | `run_sample` | multi | shared_prefix | off | `closure2-shared-prefix.yaml` | 3/3/2 |
| A2-P | `run_sample` | multi | shared_prefix | selective | `closure2-shared-prefix.yaml` | 3/3/1 |

Arm config digests, unchanged from the registration:

| Arm | `config_digest` |
|---|---|
| N0 | `e74f8e36…` |
| N1 | `aadc7f12…` |
| A0 | `4ae0f211…` |
| A1-L | `296a1db1…` |
| A1-P | `fb1a2dff…` |
| A2-P | `a1bc879a…` |

### F.2 Controlled (verified, not assumed)

| Variable | Control | How it is checked |
|---|---|---|
| Model | `deepseek-chat` in all three YAMLs; `model_override` passed explicitly to both engines | `effective_model()`, `check_frozen_contract()` |
| Fixture | one builder, `catalog.build_task(task, topology=...)` | `effective_public_task_digest` equal inside `(N0, A0)` and inside `(N1, A1-L, A1-P)` |
| Oracle | same hidden tests, same isolated sandbox, same image | four independent pytest sessions per task |
| Tools | same six basic coding tools for native; Atlas adds only its own documented mechanisms | `neutral_adapter.NEUTRAL_TOOLS`; tool surface recorded per run |
| Prompt | same default Main system prompt in every arm; the task text is the frozen public task | no arm carries a prompt override |
| Budget | same table, same sandbox resources, same `max_tokens`, temperature 0.0 | `frozen_budget()` asserts the numbers |
| Cost | Main and workers share one provider observation stream | whole-run prompt + completion tokens; worker tokens are inside the totals (25 model calls for a 3-worker A1-L cell, 32 for A1-P) |
| Cache | per-request split aggregated at run level; `COMPLETE` in the pilot | `cache_split_status`; `Unknown` when the provider omits a split |
| Layout ablation | `context.prompt_layout` is the **only** differing effective key | `layout_ablation_delta()`, preflight `layout` block |

### F.3 Not controlled, and how each limits the claim

| Difference | Why it stays | Consequence for claims |
|---|---|---|
| Native has no context plane, gate, ledger, trajectory or budget guard | those are the mechanisms under test | N-vs-A is whole-system only |
| Budget enforcement: Atlas refuses before send, native is post-hoc | different runtimes | report oracle pass, budget overrun, rejected requests and "passed but over budget" separately |
| Dependency delivery: Atlas projection vs AnyCode prerequisite context | each engine's native mechanism | never attribute an N-vs-A gap to the Context Plane |
| Task size differs across tasks (10 / 11 / 8 visible files, 11 / 12 / 7 hidden assertions) | frozen tasks | compare topologies only inside one task |
| A1-P vs A2-P may execute the identical DAG on P1/P2 | the router admits there | those cells are not independent samples; the routing comparison is N1-only |
| Parallel overlap | not exposed by either runtime | report `UNKNOWN`, never 0 |

### F.4 Attribution allowed by this design

| Pair | What may be claimed |
|---|---|
| `N0` vs `A0` | whole-system difference on one worker |
| `N1` vs `A1-L` | whole-system difference on the same DAG |
| `A1-L` vs `A1-P` | prompt layout only |
| `A1-P` vs `A2-P` | the router's decision, not a quality difference |
| `A0` vs `A1-P` | decomposition plus the context-plane projection it entails |
| `N0` vs `N1` | decomposition inside one engine |

---

## G. Canary / pilot / formal matrix plan

### G.1 Order rule

`EXECUTION_ORDER`: tasks x repetitions are the outer loop, arms the inner loop, the arm order rotates
per repetition, and no arm is completed before another starts. Implemented as shipped in
`formal_plan.py`:

* repetition `r` uses `ARM_ORDER` rotated left by `2 * r`;
* tasks stay in frozen order inside a repetition (`P1, P2, N1`);
* no randomisation — the order is a deterministic rotation, so no seed can drift it.

| Repetition | Arm order |
|---|---|
| 0 | `N0, N1, A0, A1-L, A1-P, A2-P` |
| 1 | `A0, A1-L, A1-P, A2-P, N0, N1` |
| 2 | `A1-P, A2-P, N0, N1, A0, A1-L` |

Every arm occupies three different positions across the three repetitions; every position is held by
three different arms. Residual confound: P1 always runs first in a block, P2 second, N1 last, so
time-of-day drift aliases onto task. Rotating the task order as well would remove it and is owner
decision **R-6** (it would change the plan digests, so it must be settled before S10).

### G.2 Three repetition-scoped plans, not one 54-cell plan

`PilotLedger` keeps one claim row per plan digest and never releases it, and `run_live_pilot` takes one
repetition per call. A single plan with `repetitions=(0,1,2)` could therefore be dispatched exactly
once. The candidate is therefore three plans of 18 cells each:

| Plan id | Repetition | Cells | `plan_digest` |
|---|---|---|---|
| `closure2-formal-r1` | 0 | 18 | `96a85b7a7347b8b60d3a6b3eb637af27911cfdbe2433ac90906252f106c3890c` |
| `closure2-formal-r2` | 1 | 18 | `a82e9aadd3c1de19ebfba1ce0bab16f6d51421c45fc0fd2ef74e19c3f7e70111` |
| `closure2-formal-r3` | 2 | 18 | `86257750d61272ac937508206d3117ce0c6f578b574c835fff57667245c9b931` |

These digests are **candidates**. `plan_scope_ids()` still returns exactly two ids, so no formal plan
is dispatchable today; `authorization_probe()` runs the shipped gate against each candidate and gets a
refusal whose reasons are the approval status and its three missing fields — nothing about the
contract, the tasks or the digests.

### G.3 Canary plan

Reuse `connectivity_canary.py` unchanged in mechanism, with a **new** scope digest (its current plan is
`CONSUMED_BY_EXECUTION`). One synthetic task, Atlas single worker, `deepseek-chat`, one attempt,
`OFFLINE_PRECHECK` before the live probe. It proves the chain — provider, sandbox, tools, isolated
oracle, cleanup — and its result may never be counted as a benchmark sample.

New requirement for this stage: after the canary, **one real multi-worker cell** (P1 `A1-P`) must run
before the matrix. The canary is single-worker by design and cannot show that a child is created,
handed a context, and graded. That one cell is the only thing that can distinguish "the multi path
works" from "the multi path is configured".

### G.4 Pilot plan

Purpose: calibrate the two parameters that S06 and the 8-cell pilot show are close to their limits —
worker turns and token budget — without touching the frozen matrix. Proposed: one repetition of P1
(6 cells), because P1 is a positive 3-worker task and its `A1-P` cell was the most expensive in the
pilot (207 775 tokens of a 260 000 budget).

This is **not** the same as the 2026-11-18 8-cell pilot: that one spanned three tasks with one
repetition and is already consumed. A second pilot needs its own scope and its own approval.

### G.5 Formal matrix plan

| Stage | Cells | Model calls | Cost ceiling (advisory) | Purpose |
|---|---|---|---|---|
| Canary | 0 benchmark cells | ≤ 6 requests | USD 0.05 | chain connectivity |
| Multi-path probe | 1 (P1 A1-P) | ~32 | USD 0.05 | prove a child really runs |
| Pilot | 6 (P1 x 6 arms) | ~120 | USD 0.30 | calibrate turns and tokens |
| Formal r1 | 18 | ~121 | USD 1.00 | repetition 0 |
| Formal r2 | 18 | ~121 | USD 1.00 | repetition 1 |
| Formal r3 | 18 | ~121 | USD 1.00 | repetition 2 |
| **Total** | **54 + 7** | **~1,250** | **USD 3.40** | |

Each formal plan requires its own owner approval naming its digest; the three are separate claims by
construction. Approving one must not approve another, which the current gate already enforces.

### G.6 Stopping policy

Inherited from S06 and kept explicit:

| Condition | Action |
|---|---|
| Cell fails the oracle, the public suite, the budget or the turn cap | record `FAILED`, continue (`MEASURED_AGENT_FAILURE_CONTINUE`) |
| Sandbox never becomes healthy before any model call | exclude the run with its reason, continue, and stop the plan after 2 consecutive occurrences |
| Provider authentication / quota error | stop the plan immediately; no retry |
| Ledger write failure | stop the plan (`BLOCKED_LEDGER_*`); a crash is not a retry |
| Estimated cost reaches the plan ceiling | stop dispatching further cells; the cell in flight may overspend |
| Operator kill | stop and record, never resume |
| Never | retry a cell, resume a plan, re-run a consumed claim |

The pilot runner already fails closed on the ledger and the cost ceiling; the missing piece is the
matrix wrapper that maps a provider-level failure to "stop this plan" (S08).

### G.7 Evidence contract

Per cell, from `normalize_result`: the flat record with `oracle_passed`, `task_success`,
`failure_class`, `executed_worker_count`, `single_worker_downgrade`, provider tokens, cache hit/miss
and split status, `estimated_cost_usd` with its state, model calls and their source, tool calls, budget
verdict with its basis, sandbox status, and `raw_result_path`.

Per plan, from `run_live_pilot`: the live summary with plan id, plan digest, approved digest, cells,
`execution_claim` (attempt id, ledger path, `CONSUMED`), `execution_integrity_status`, cost state, and
`not_started_cells`. Three summaries, one per repetition, named per plan by
`LIVE_SUMMARY_FILENAMES`.

Custody: raw runs stay in `experiments/runs/closure2*` (git-ignored). Published material is a
sanitized aggregate plus an evidence index of byte lengths and SHA-256, following the S06 pattern. No
raw workspace, patch, hidden test or credential is published.

Rules that carry over: a missing number stays `None`; an unknown cache split stays `Unknown`; a cost is
labelled estimate or provider-usage, never both; `PASSED`/`FAILED` is the executor verdict and is
reported next to oracle correctness, never merged with it.

### G.8 Statistical analysis plan

* Unit of analysis: `(task, arm, repetition)`. 54 observations, 9 paired blocks of 6 arms.
* Primary: independent oracle correctness. Report counts and per-cell verdicts, never a bare
  percentage.
* Paired comparisons inside each block: exact McNemar on discordant pairs (at most 9 blocks, so the
  exact test is the only defensible one); for tokens and cost, report per-block deltas and ranges,
  with a sign test rather than a t-test.
* `n = 3` per cell means **no** claim of statistical significance for token or cost differences. The
  preregistration already forbids "any statistical claim from a single repetition"; 3 is the floor for
  a descriptive interval, not for a p-value.
* Report budget outcomes separately from correctness, and report `single_worker_downgrade` for every
  A2-P cell so a collapsed router decision is never counted as a multi-worker sample.
* Cost is a frozen-price estimate; cache hit rate never substitutes for cost.

---

## H. Reusable infrastructure (what S08 does not have to build)

| Component | Reuse as | Change needed |
|---|---|---|
| `preflight.py` (62 checks) | as-is | none |
| `pilot_runner.settings_for` / `normalize_result` | as-is | none |
| `pilot_ledger` | as-is | none (claims are per plan digest) |
| `connectivity_canary` | mechanism | new scope digest + new approval |
| Isolated oracle (R1-2H) | as-is | none |
| Atlas + native engines | as-is | none |
| `formal_plan.py` (this stage) | order + identities | wire the three plan ids into `pilot_scope` / `plan_cells_for` / summary filenames / run roots |
| S06 executor patterns | wrapper, reconciliation, custody | new wrapper for Closure-2 plans |

---

## I. Cost and token estimate

Basis: the one real 8-cell Closure-2 pilot (2026-11-18), USD 0.08404, 941 453 tokens, 162 model calls.
Using the P1 block as the template for one repetition:

| Arm | Tokens (P1) | Cost (P1) |
|---|---|---|
| N0 | 35 264 | 0.00478 |
| N1 | 92 137 | 0.01060 |
| A0 | 43 455 | 0.00521 |
| A1-L | 153 057 | 0.01436 |
| A1-P | 207 775 | 0.01628 |
| A2-P | 156 433 | 0.01279 |
| **Block** | **688 121** | **0.06402** |

| Scenario | Tokens | Model calls | Estimated cost |
|---|---|---|---|
| One formal repetition (18 cells) | ~2.06 M | ~121 | ~USD 0.19 |
| Three formal repetitions (54 cells) | ~6.19 M | ~1,100 | ~USD 0.58 |
| Total incl. pilot and probes | ~6.9 M | ~1,250 | ~USD 0.70 |
| Proposed ceiling | 6.5 M advisory | — | USD 1.00 per plan, USD 3.40 total |

The ceiling is roughly 5x the estimated per-plan cost, which keeps one bad cell from stopping the
matrix while keeping a runaway bounded. A cell that hits the per-cell 260 k token budget is a measured
failure, not a cost overrun of the ceiling.

Wall clock: 46 s per cell in S06, 71 s per cell in the 8-cell pilot. 54 cells is therefore ~45–75
minutes of provider time plus sandbox provisioning; plan a single supervised dispatch per repetition,
not one 3-hour session.

---

## J. Offline verification

Everything below ran with 0 model calls and 0 sandboxes.

| Check | Result |
|---|---|
| Preflight (62 checks) | `PASSED`, `failed: []`, digest `3080f313…` |
| `execution_ready` | `true` — but see R-1: it is the benchmark-definition approval, not a scope approval |
| Closure-2 unit tests (4 files) | 287 passed |
| Closure-2 integration tests (4 files) | 82 passed |
| New formal-plan tests | 20 passed |
| Ruff | clean on both new files |
| Pyright | 0 errors, 0 warnings |
| Six arms distinguishable | six distinct `arm_config_digest` and six distinct `settings_digest`: no two arms resolve to the same execution settings |
| Single arm cannot start a worker | `build_task(topology="single").workers == 1` with `task_id="main"` for all three tasks |
| Multi arm has a real delegation path | P1/P2 = 3 workers, wave width 2, disjoint; the runtime executes the DAG without a model proposal |
| Negative task has a refusal basis | N1 = 2 workers, wave width 1, both steps write `lifecycle/state.py`, router blocks `GATE_2` |
| Worker usage is not lost | a 3-worker A1-L cell reports 25 model calls and 146 730 prompt tokens; cache split `COMPLETE` |
| Oracle stays independent | four separate pytest sessions per task; the seed leg fails, the reference leg passes (11/12/7 hidden tests) |
| Evidence supports cross-arm comparison | `normalize_result` records arm, topology, task digest, settings digest, worker count and downgrade per cell |
| Unauthorized execution is refused | `authorization_probe()` refused all three candidate plans; the pilot, routing-pair and canary approvals are all `CONSUMED_BY_EXECUTION` |
| S06 custody intact | 1048/1048 digests re-verified |

Not verified, and not claimed: that a real model *proposes* delegation. Closure-2 does not need that
behaviour, and S07 is forbidden from making the model request that would measure it.

---

## K. Remaining blockers and owner decisions

| # | Item | Why it blocks | Recommendation |
|---|---|---|---|
| R-1 | `protocol.APPROVAL_RECORD` is `APPROVED` and `approval_gate()` returns `execution_ready=true`, while `docs/closure-2-preregistration.md` §4 shows `PENDING_OWNER_APPROVAL` | the document and the code disagree; a reader can mistake one gate for the whole authorization | decide before S08: reset the record to `CONSUMED_BY_EXECUTION` (outside the digest, so the frozen identity does not move) or correct the document. Doing neither leaves a stale "execution ready" flag in every dry-run |
| R-2 | Worker turn cap 12 | 1 of 5 multi cells failed in the 8-cell pilot because the `csv` worker hit 12 turns with no output | decide the cap before S10 freeze (12 vs 16). Changing it changes the registration digest |
| R-3 | Token budget 260 k per cell | 2 of 8 pilot cells used 208 k and 217 k (80–84 %). One budget exhaustion turns a solvable cell into an incomplete one, and only the Atlas arms are refused *before* send | decide 260 k vs 400 k at S10, with the asymmetry stated either way |
| R-4 | Formal plans are not wired | the 54-cell matrix has identities but no entry point | S08 wires the three ids; each still needs its own approval |
| R-5 | Canary plan is spent | a second canary needs a new scope digest | S08 creates the new scope; no mechanism change |
| R-6 | Task order is fixed inside a repetition | time-of-day drift aliases onto task | decide whether to rotate tasks per repetition before S10 |
| R-7 | Cost ceilings are unapproved | the plan digests contain the number | owner sets the ceiling when approving each plan |
| R-8 | The v2.1 Main prompt is not available to Closure-2 | `RunnerSettings` has no prompt override, and the frozen arm matrix has no prompt axis | decide whether Closure-2 keeps the default prompt (recommended: yes, all arms identical) or whether a prompt axis becomes a new, pre-registered variable |
| R-9 | `parallel_overlap` is unobservable | neither runtime exports worker start/end | accept `UNKNOWN` for V1, or expose timings as a separate, non-scoring change |
| R-10 | S06's prompt finding does not transfer | S06 compared prompt arms with zero delegation; Closure-2 compares systems with a fixed prompt | do not cite S06's P3 as Closure-2's baseline. It is a candidate for a *future* prompt axis, not for this matrix |

---

## L. S08 handoff

S08 is a live canary, under a separate authorization. What it needs, in order:

1. Resolve R-1 so a dry run cannot report "execution ready" while the document says pending.
2. Create the new canary scope (new digest) and record an owner approval naming it.
3. Run `OFFLINE_PRECHECK`, then the canary, then the single P1 `A1-P` multi-path probe.
4. Report the probe's `executed_worker_count` and child token line explicitly: that number is the
   difference between "the multi path works" and "the multi path is configured".
5. Then the 6-cell P1 pilot under its own scope, to calibrate R-2 and R-3 before S10 freezes anything.

Nothing is authorized by this document. The candidate plan is a proposal with digests, not a freeze;
S10 is where a protocol revision and its approval are written.

---

## Evidence index

| Path | Contents |
|---|---|
| `experiments/evaluation/closure2/formal_plan.py` | order rule, three plan scopes, candidate approval records, offline gate probe |
| `experiments/results/closure2/formal-plan-candidate.json` | the 54 cells, their identities and the refusal of each plan |
| `tests/unit/test_closure2_formal_plan.py` | 20 offline tests over the order, the identities and the refusal |
| `docs/closure-2-preregistration.md` | the frozen registration this stage audits (unchanged) |
| `docs/closure-2-fairness-protocol.md` | the fairness protocol this stage verifies (unchanged) |
| `docs/closure-2-benchmark-design.md` | task design and oracle scope (unchanged) |
| `D:\Work\AtlasRuntime-s06-live-20261124-231047` | S06 custody, unchanged, 1048/1048 digests re-verified |
