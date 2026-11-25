# Closure-2 S07 — Preparation Closure

Status: `CLOSURE2_S07_READY_FOR_INDEPENDENT_REVIEW`.

This stage closes the S07 preparation. It resolves the authorization ambiguity (R-1), binds the
owner's two budget decisions into the execution path (R-2/R-3 as **candidates**), wires the three
formal plans into the existing entry point (R-4), audits and re-balances the execution order (R-6),
prepares an independent S08 canary scope (R-5), and re-derives every cost number (R-7). Nothing is
executed: zero model requests, zero sandboxes, zero canary cells, zero pilot cells, zero formal cells.

It supersedes §K and §L of `docs/closure-2-s07-formal-evaluation-prep.md` for the decisions it
resolves. The earlier report stays in the repository as history and is not rewritten.

| Item | Value |
|---|---|
| Branch | `feat/closure-2-s07-preparation` |
| Base | `e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1` (the exact commit S06 executed) |
| Frozen preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` — **unchanged** |
| Candidate budget revision | `closure-2-s07-candidate-budget-1`, profile `b41a633d…` |
| Candidate formal plans | `closure2-formal-r1/r2/r3`, 18 cells each, 54 identities |
| Candidate canary scope | `closure2-s08-canary-1`, 7 cells, approval `PENDING` |
| Preflight | `PASSED`, 62 checks, digest `3080f313…`, 0 model calls, 0 sandboxes |
| PR #6 | untouched: `feat/stage-b-s05-freeze-1-6` still points at `e0bc2cc`, still draft/open |
| Live runs this stage | 0 |

---

## A. Git provenance and remote HEAD

| Item | Value |
|---|---|
| Repository | `D:\Work\AtlasRuntime` (`Melon235/AtlasRuntime`) |
| Baseline | `e0bc2cc` — S06's execution HEAD, still the merge base of this branch |
| Existing commits on the branch | `20ddd3e` (plan module + tests), `0247e57` (S07 report), `f6cde31` (turn-cap note) |
| This stage | one commit for the code and the four offline artifacts, one commit for this document |
| Runtime source | `git diff e0bc2cc -- src/` is empty: no runtime module was touched |
| Protocol source | `protocol.py`, `preflight.py`, `catalog.py`, `pilot_ledger.py`, `connectivity_canary.py` are byte-identical to `e0bc2cc` |
| PR #6 | not modified, not merged, no new PR created |

The two consumed scopes, the S06 authorization and the frozen registration are historical objects in
this branch's ancestry. Nothing in this stage amended, rebased, force-pushed or re-signed them.

---

## B. Historical authorization provenance (R-1)

R-1 asked why `protocol.APPROVAL_RECORD` says `APPROVED` while `docs/closure-2-preregistration.md` §4
still prints `PENDING_OWNER_APPROVAL`. The answer, verified from the repository rather than assumed:

| Item | Verified value | Where |
|---|---|---|
| Record status | `APPROVED` | `experiments/evaluation/closure2/protocol.py` |
| Digest it names | `3080f313…` = the frozen R1 prereregistration digest | same |
| Approver | `repository_owner` | same |
| Approval commit | `de2acbc4fad395e29364aa6900e388afb492f962` | same |
| Approved at | `2026-11-18T08:43:30+00:00` | same |
| What it approves | the benchmark **definition**: tasks, arms, budget table, metrics, exclusions — `preregistration_payload()` | `approval_gate()` |
| What it does **not** approve | any cell list, repetition, cost ceiling or plan digest | `pilot_authorization_gate()` |

The record was flipped to `APPROVED` on 2026-11-18 to authorize the eight-cell pilot's *definition*
layer. Ten minutes later the pilot's own scope record (`PILOT_APPROVAL`, digest `0f61f84a…`,
attempt `f862981c…`) was consumed by the execution; the routing pair followed at 11:19:06
(`41551523…`, attempt `f51f374a…`), and the connectivity canary's scope was consumed the same day.

The document disagrees because it is a **snapshot taken at freeze time**: §4 shows the record as it
was when `closure-2a-r1` was written. The record is deliberately outside `preregistration_payload()`
— that is what lets an approval move without moving the identity it approves, and it is asserted by
the preflight's `approval_outside_frozen_identity` check. So the document is not wrong about the
freeze; it is simply not the live record.

**Resolution chosen: document the drift and prove it inert. The record was not edited.**

Resetting it to `CONSUMED_BY_EXECUTION` would have been the other option R-1 offered, and it was
rejected: rewriting a historical approval record is exactly what this stage is forbidden to do, and a
consumed definition-level record would misdescribe the eight-cell pilot it legitimately authorized.

What is *enforced* instead, and tested:

1. `protocol.APPROVAL_RECORD` cannot authorize any scope. `pilot_authorization_gate()` requires a
   record that names the plan digest, an approver, a commit and a timestamp — none of which exists
   for any plan that is not already spent.
2. Every plan the CLI can name resolves to a record whose status is `CONSUMED_BY_EXECUTION`
   (pilot, routing pair) or `PENDING_OWNER_APPROVAL` (three formal plans, canary). The set of
   *runnable* plans is empty, asserted by `test_the_benchmark_approval_alone_still_authorizes_nothing`.
3. Nothing was added to the generic gate rules. The refusal comes from the existing scope layer, not
   from a new bypass or a widened condition.

The S08 canary therefore uses **new, independent, pending** material (§L). No historical record can
be stretched into it: `authorization_template()["cannot_be_satisfied_by"]` names each of them.

---

## C. Owner-selected budget configuration

| Knob | Frozen (historical) | Candidate (this stage) | Status |
|---|---|---|---|
| Worker max turns | 12 per worker | **20 per worker** | owner decision 2026-11-25 |
| Per-cell token budget | 260 000 | **500 000** | owner decision 2026-11-25 |
| Single-agent max turns | 36 | **36 (unchanged)** | not part of the decision |
| Workers per multi cell | 3 | 3 | unchanged |
| Max tool calls | 90 | 90 | unchanged |
| Wall clock | 1800 s | 1800 s | unchanged |
| Temperature / `max_tokens` | 0.0 / 8000 | 0.0 / 8000 | unchanged |
| Image, CPU, memory, network | `atlas-uat-python:3.12`, 1, `1Gi`, `none` | same | unchanged |

### C.1 Why the numbers are a third object, not an edit

Neither approved number can be edited where it currently lives:

* `protocol.BUDGET` is inside `preregistration_payload()`. Changing it moves `3080f313…` — the digest
  the S06 execution, the two consumed scopes and the definition approval all name — and moves all six
  arm digests with it.
* `catalog.build_task()` is worse: the worker and task turn caps are part of
  `effective_public_task_digest`, so the frozen C2-A task table would stop matching and the preflight
  would fail.

So the stage adds `experiments/evaluation/closure2/candidate_budget.py`, which contains:

| Object | Revision | Digest | Content |
|---|---|---|---|
| `FROZEN_PROFILE` | `closure-2a-r1` | `e1d5eea9aca7433ea0593b40c8ee9e7755e571a139e9b270a332e59cfad82dc4` | read *from* `protocol.BUDGET`: 36 / 12 / 260 000 |
| `CANDIDATE_PROFILE` | `closure-2-s07-candidate-budget-1` | `b41a633d1d39b3fe95845498b4b6c575b6615b74033ddab1b825be944ed7f89d` | 36 / 20 / 500 000 |
| `candidate_registration_digest()` | — | `a1ce048a27c088761dd02da003681552377ed3230d951cb168e466cb2dd97503` | frozen registration digest **+** candidate profile digest |

`candidate_registration_digest()` is a new identity for "this frozen benchmark, under this budget
profile". It is not a re-freeze: it does not recompute `preregistration_payload()` and it does not
replace `C2A_R1_PREREGISTRATION_DIGEST`. The revision record
(`experiments/results/closure2/candidate-budget.json`) states what it supersedes, for which stages
only, and that historical runs keep the numbers they were executed with.

Only the approved knobs moved — `changed_knobs()` returns exactly
`multi.max_turns_per_worker`, `multi.token_budget`, `single.token_budget` (the token budget is one
decision landing on both topology blocks). Derived values such as `theoretical_worker_turns` are
excluded from that report on purpose.

### C.2 What "per cell" covers, and what it does not

| Question | Answer |
|---|---|
| Does the 500 000 cover Main *and* the workers? | Yes. One `RunBudgetState(limit_tokens=500_000)` per run; every agent of that run draws from it. |
| Is it split per worker? | No. There is no per-worker allocation: a 3-worker cell shares one budget, and the first request that would breach it is refused (Atlas) or judged after the run (native). |
| Are the 20 turns shared? | No. The turn cap is **per agent**: 20 for each worker, 36 for the single agent. A 3-worker P1 cell can therefore reach 60 theoretical turns inside one 500 000-token budget. |
| Does the single arm get less? | No. `single.token_budget` is 500 000, the same as multi. Its 36-turn cap did not move, so it is not handicapped either way. |

---

## D. Actual runtime parameter binding

A document that says 500 000 is not a runtime that admits against 500 000. The chain below is the one
the code executes, and each arrow is covered by a test.

```text
candidate_budget.CANDIDATE_PROFILE            (20 / 500 000)
  └─ formal_plan.formal_plan(r)               binds budget_profile_digest into the plan digest
       └─ pilot_runner.pilot_scope(plan_id)   returns (plan, approval record)
            └─ run_live_pilot(..., plan_id)   profile = plan.budget_profile()  ← fail-closed lookup
                 └─ settings_for(cell, budget_profile=profile)
                      ├─ token_budget      = 500_000
                      ├─ max_turns         = 20 (multi) / 36 (single)
                      └─ worker_max_turns  = 20 (multi) / None (single)
                           └─ runner.run_sample / native_baseline.run_native_sample
                                └─ models.effective_agent_max_turns(step, settings, override)
                                     └─ AgentConfig(max_turns=20)
                      └─ RunBudgetState(limit_tokens=500_000)          (Atlas, guarded before send)
```

### D.1 The trap this stage found

`catalog.build_task()` declares `max_turns=12` on every worker step, and both engines read
`step.max_turns or settings.max_turns`. A change confined to `protocol.BUDGET` — or even to
`RunnerSettings.max_turns` alone — would have executed **12 turns while reporting 20**: the per-step
declaration wins. That is precisely the "only the display field changed" failure the brief forbids.

Two changes fix it without touching the frozen task data:

1. `RunnerSettings.worker_max_turns: int | None = None` — a run-level override that wins over a
   per-step declaration. It is **absent from `settings.digest()` when `None`**, so every settings
   digest already recorded in an artifact still hashes to the same value.
2. `models.effective_agent_max_turns(step_max_turns, settings_max_turns, worker_max_turns)` — one
   function, imported by *both* engines. The two inline copies of `step.max_turns or
   settings.max_turns` are gone; a test asserts neither engine's source contains that expression and
   that both import the same function object.

Proofs, all offline:

| Claim | Evidence |
|---|---|
| The multi cell is built with 20 turns | `settings_for(A1-P, candidate).max_turns == 20` and `.worker_max_turns == 20` |
| The agent config really gets 20 | `effective_agent_max_turns(step=12, settings=20, override=20) == 20`; both engines call it |
| The frozen task data was **not** edited | `cell.benchmark_task().workers[0].max_turns == 12` while the effective cap is 20 |
| Admission uses 500 000 | `settings_for(...).token_budget == 500_000` → `RunBudgetState(limit_tokens=500_000)` / post-hoc native check |
| The frozen default is untouched | `settings_for(cell)` still yields `(12, None, 260_000)`; `pilot-dry-run.json` re-derives field-for-field except for approval state |
| An unknown profile digest fails closed | `plan.budget_profile()` raises; the gate also reports `plan budget profile is not resolvable` |

### D.2 Six-arm comparability

| Arm | Topology | Effective turn cap | Token budget | Budget semantics |
|---|---|---|---|---|
| N0 | single | 36 | 500 000 | post-hoc (native) |
| A0 | single | 36 | 500 000 | guarded before send (Atlas) |
| N1 | multi ×3 | 20 per worker | 500 000 | post-hoc |
| A1-L | multi ×3 | 20 per worker | 500 000 | guarded |
| A1-P | multi ×3 | 20 per worker | 500 000 | guarded |
| A2-P | multi ×3 / ×1 | 20 per worker | 500 000 | guarded |

All six share one token budget, so no arm is handicapped by the budget axis. The turn axis is the
same shape it always had (one agent gets one allowance; a decomposition gets the same theoretical
ceiling spread over its workers), only the numbers moved. The enforcement asymmetry (guarded vs
post-hoc) is unchanged and stays reported, not removed.

---

## E. Three formal plans and their wiring (R-4)

`plan_scope_ids()` now returns six ids: the two consumed plans, the three formal plans and the canary.
Wiring them makes "not authorized" a decision of the gate instead of the absence of an entry point.

| Plan id | Repetition | Cells | Plan digest | Ceiling | Gate |
|---|---|---|---|---|---|
| `closure2-formal-r1` | 0 | 18 | `79df2c0dc8f9474e93d73bbe4b3d54065fe62a9876794973f63d78227e1da14e` | USD 1.20 | refused |
| `closure2-formal-r2` | 1 | 18 | `a23a8a6203c51ee0a1fa595b595a20cbda41000b7ba627e84b712a1696be0798` | USD 1.20 | refused |
| `closure2-formal-r3` | 2 | 18 | `5462fde26fabe8cd06a8ce68f99789e09b28fbe40fd7c8557c8fd30783db4f3d` | USD 1.20 | refused |

These replace the S07 candidate digests (`96a85b7a…`, `a82e9aad…`, `86257750…`): the plans now bind
the candidate budget profile and execute the balanced order, so the old digests name a different
scope. Regenerating the candidate artifact is not a workaround for a claim — none of the three was
ever claimed, and the consumed claims (`0f61f84a…`, `41551523…`) are untouched.

### E.1 Invariants, each asserted

| Requirement | How it is checked |
|---|---|
| 18 cells per plan | `len(repetition_cells(r)) == 18` |
| Six arms × three tasks per plan | per-arm count == 3, per-task count == 6, every `(task, arm)` present exactly once |
| Three distinct repetitions | `plan.repetitions == (r,)`, three distinct plan digests |
| 54 unique identities | the set of `(cell_id, repetition)` has 54 members |
| Digest matches content | `plan.cells == tuple(cell.cell_id …)` in the generated order; swapping two cells changes the digest |
| Budget is inside the identity | swapping the profile digest changes the plan digest |
| Non-retryable claims preserved | `pilot_ledger` unchanged: one claim per plan digest, taken before any side effect, never released |
| No bypass by renaming | the plan ids are the ones in `pilot_runner.FORMAL_PLAN_IDS`; a digest is `canonical_digest(plan.payload())` |
| Unauthorized ⇒ refused | `authorization_probe(r)["execution_ready"] is False` for all three, with approval-only reasons |
| Cross-plan statistics and cost reconciliation | `aggregate_plan_summaries()` (§E.2) |

### E.2 Cross-plan aggregation

`formal_plan.aggregate_plan_summaries([r1, r2, r3])` produces one table: 54 executions, oracle and
executor pass counts, provider prompt/completion tokens, cache hit/miss, model calls, estimated cost,
and a `cost_reconciliation` block (per-plan ceilings, spend, `within_ceiling`). It refuses rather than
merges when a summary names a plan that is not one of the three, when a plan is summarized twice, when
a `(cell, repetition)` identity repeats, or when a plan does not carry 18 cells. Any unknown term
leaves the corresponding total `None` — never `0`.

The budget figures inside the plans are **candidates**. S11 freezes them; nothing here is approved.

---

## F. Schedule balance audit (R-6)

### F.1 What the S07 order did

S07 rotated the **arm** order per repetition but kept the task order fixed: P1 was always the first
block, P2 the second, N1 the third, in all three repetitions. Time-of-day, provider warmth and
operator attention therefore aliased onto the *task*, which is the one factor the design cannot
average out afterwards.

Measured on the S07 schedule (`schedule_audit(schedule="s07-frozen-task-order")`):
`every_task_in_each_third_once` is **False** — P1 is in the early third 3/3 times, N1 in the late
third 3/3 times.

### F.2 The balanced revision candidate

Two rotations, no randomisation, therefore no seed to record and no order that can drift:

* **tasks** rotate left by 1 per repetition: `[P1,P2,N1] → [P2,N1,P1] → [N1,P1,P2]`;
* **arms** rotate left by `2 × (repetition + block_index)` inside each block.

The second rotation is the part the S07 rule missed. With the base rotation alone an arm sits in the
same sixth of every repetition, so all three of its executions in a session land in the same third.
Rotating per block puts every arm in the early, the middle and the late third **once per repetition**.

| Repetition | Task order | Block arm orders |
|---|---|---|
| 0 | P1, P2, N1 | `N0 N1 A0 A1-L A1-P A2-P`, `A0 A1-L A1-P A2-P N0 N1`, `A1-P A2-P N0 N1 A0 A1-L` |
| 1 | P2, N1, P1 | `A0 A1-L A1-P A2-P N0 N1`, `A1-P A2-P N0 N1 A0 A1-L`, `N0 N1 A0 A1-L A1-P A2-P` |
| 2 | N1, P1, P2 | `A1-P A2-P N0 N1 A0 A1-L`, `N0 N1 A0 A1-L A1-P A2-P`, `A0 A1-L A1-P A2-P N0 N1` |

Measured properties of the wired schedule (`"balanced-double-rotation"`):

| Property | Result |
|---|---|
| Every task in the early, middle and late third exactly once | `True` |
| Every arm in the early, middle and late third exactly once per repetition | `True` (3/3/3 over the matrix) |
| Every arm holds ≥3 distinct positions inside a block | `True` |
| Every execution index is held by three distinct arms across repetitions | `True` |
| Reproducible without a seed | `True` — the rule is a rotation, not a draw |
| Original order still computable | `True` — `schedule="s07-frozen-task-order"` is retained |

`arm_execution_indices` for `N0`, for example, are `[0, 4, 2]`, `[6, 10, 8]`, `[12, 16, 14]`: early,
middle, late in each repetition and early/middle/late across the matrix.

### F.3 Revision status

This is a **revision candidate**, not a freeze. `docs/closure-2-preregistration.md` is not edited:
its rule ("tasks × repetitions are the outer loop, arms the inner loop, the arm order rotated per
repetition") is still satisfied — the task blocks stay the outer loop and no arm is completed before
another starts. The final order is frozen at S10, and the S07 order remains reproducible for
comparison.

---

## G. S08 canary contract (R-5)

The 2026-11-18 connectivity canary is single-worker, Atlas-only, and its scope is
`CONSUMED_BY_EXECUTION`; a status edit cannot revive it and, more importantly, it could never have
covered a worker being created, the native engine, or a router refusal. The S08 scope is therefore new
material: `experiments/evaluation/closure2/s08_canary.py`, plan id `closure2-s08-canary-1`, digest
`f3d09a8523ae3312872be31dfcc1c1544104de1afa3c9db3b6a5f10108589fbd`.

### G.1 Cells and order

Seven cells, one attempt, one repetition, ordered from the cheapest failure to the most expensive one:

| # | Cell | Layer it falsifies first | Covers |
|---|---|---|---|
| 1 | `P1::N0` | provider → sandbox → tools → isolated oracle | native single, chain |
| 2 | `P1::A0` | the Atlas execution path | Atlas single, task-scoped context |
| 3 | `P1::N1` | AnyCode's own DAG | native multi, disjoint write sets |
| 4 | `P1::A1-L` | the Atlas DAG, legacy layout | multi-worker DAG |
| 5 | `P1::A1-P` | the Atlas DAG under shared prefix | worker creation, capability, handoff, usage |
| 6 | `P1::A2-P` | the router on a plan it must admit | selective arm, router audit |
| 7 | `N1::A2-P` | the router on a plan it must refuse | `GATE_2`, shared-write risk |

Coverage is a table, not a paragraph: `coverage_matrix()` maps each requirement to the cells that
prove it, and `check_contract()` fails if any of `native_single`, `native_multi_dag`, `atlas_single`,
`atlas_multi_dag`, `legacy_prompt_layout`, `shared_prefix_prompt_layout`, `independent_write_sets`,
`worker_creation`, `worker_capability`, `artifact_handoff`, `worker_usage_counted`,
`selective_arm_refuses_negative_task` has no cell.

### G.2 Budget, cost estimate and stop

| Item | Value |
|---|---|
| Worker max turns | 20 per worker (36 for the two single-agent cells) |
| Per-cell token budget | 500 000, shared by every agent of the cell |
| Attempts | 1 |
| Estimated cost (measured basis) | USD 0.0822 |
| Estimated cost (0 % cache hit) | USD 0.3031 |
| Nominal capacity | 3 500 000 tokens, USD 1.05 at the miss price — **not an estimate** |
| Proposed cost stop | **USD 0.45** (`PROPOSED_NOT_APPROVED`) |

### G.3 Acceptance criteria

Thirteen criteria, each naming the field that decides it — the full list is in
`experiments/results/closure2/s08-canary-contract.json`. The ones this stage was told to guarantee:

| # | Criterion | Decided by |
|---|---|---|
| A3 | the positive multi path really creates workers | `executed_worker_count == 3`, three distinct worker ids, the two wave-0 workers start before `integrate` |
| A4 | a worker can write, inside its own paths | at least one `file_write`/`file_edit` by a worker, no path outside its `owned_paths` |
| A5 | the artifact handoff reaches the downstream node | the `integrate` request carries both dependency outputs; otherwise `HANDOFF_NOT_OBSERVED` and the criterion fails |
| A6 | the positive wave is observable **as a wave** | two independent wave-0 workers before the dependent step. Wall-clock overlap is **not** observable (`parallel_overlap_evidence` is null) and the canary records the structure, never an overlap number |
| A7 | the selective arm refuses N1 | `resolved_route == "direct"`, `executed_worker_count == 1`, `SHARED_WRITE_RISK` + blocked `GATE_2_DEPENDENCY_COUPLING` |
| A8 | the selective arm admits P1 | `resolved_route` is a delegation route with `executed_worker_count == 3` |
| A9 | worker usage is inside the run totals | provider tokens and cache split recorded per cell; multi cells show several times the single cell's request count; no field claims a per-agent split the runtime does not measure |
| A10 | the oracle is independent | `oracle_mode == "sandbox"`, `oracle_passed` recorded next to the executor verdict, never merged |
| A11 | sandboxes are cleaned up | 7 confirmed, 0 not-created, 0 unknown, no residual container |
| A12 | evidence is complete and outside the repository | per-cell run dirs, summary, evidence index of byte lengths + SHA-256 |
| A13 | the canary is never a benchmark sample | `canary_id`, `is_benchmark_sample == False`, absent from the S11 aggregate |

**Declared before execution:** the canary repeats the `(cell, repetition 0)` identities of formal r1.
The formal plan still runs them; a canary observation is never reused as a formal sample, and the
formal r1 claim is a separate one-shot claim on a separate digest. This is an owner-visible
consequence, not a silent reuse (see §K, R-11).

### G.4 Evidence contract and what is never published

Per cell: identity and route, executed worker count and downgrade flag, oracle/task/failure verdicts,
provider prompt and completion tokens, cache hit/miss and split status, estimated cost and its state,
model calls and their source, budget verdict and basis, sandbox creation and destruction, raw result
path. Per run: plan id and digest, approved digest, approval status, budget profile revision and
digest, cell count, execution claim, integrity status, stopped reason, not-started cells, cost policy
and breach flag, sandbox counts.

Never published: hidden tests, credentials or provider payloads, the raw container filesystem.

---

## H. Updated stage-wise cost estimate (R-7)

Three layers, kept apart. Every number comes from `experiments/evaluation/closure2/stage_costs.py`,
which is arithmetic over the measured 2026-11-18 usage and the frozen price snapshot
(hit USD 0.006/M, miss USD 0.30/M, output USD 1.20/M, snapshot 2026-11-06).

| Stage | Cells | Main tokens (est.) | Worker tokens (est.) | System total (est.) | EXPECTED | HIGH (0 % hit) | Nominal capacity | Proposed stop |
|---|---|---|---|---|---|---|---|---|
| S08 canary | 7 | 78 719 | 806 966 | 885 685 | USD 0.0822 | USD 0.3031 | 3 500 000 tok / USD 1.05 | USD 0.45 |
| S09 pilot (P1 × 6 arms) | 6 | 78 719 | 761 752 | 840 472 | USD 0.0775 | USD 0.2871 | 3 000 000 tok / USD 0.90 | USD 0.45 |
| S11 formal matrix | 54 | 679 554 | 6 223 296 | 6 902 850 | USD 0.6406 | USD 2.3601 | 27 000 000 tok / USD 8.10 | USD 3.60 (3 × 1.20) |
| **Total** | **67** | | | | **USD 0.80** | **USD 2.95** | **33 500 000 tok / USD 10.05** | **USD 4.50** |

### H.1 How each layer is derived

| Layer | Basis | What it is not |
|---|---|---|
| EXPECTED | the eight measured cells of the 2026-11-18 pilot, inherited by arm and scaled by a task-size factor for the ten never-run cells, **×1.25 for multi cells** (12 → 20 turns) | not a prediction of these exact runs, and not a bill |
| HIGH | the same token counts priced at a **0 % cache hit** | not a capacity bound |
| Nominal capacity | cells × 500 000 tokens at the miss price | not an estimate, and never used as one |
| Proposed stop | 1.5 × HIGH, rounded up to 5 cents, and required to stay ≤ 60 % of the nominal envelope | not approved |

"Main" here means the single-agent cells; "worker" means the multi cells. The runtime aggregates usage
per run and exposes **no per-agent token split**, so a finer attribution would be invented. Main and
worker therefore sum to the system total by construction.

Sensitivity of the formal matrix to the cap uplift: 0 % → USD 0.5297, +25 % (the model) → USD 0.6406,
+50 % → USD 0.7514.

### H.2 What happened to the old USD 3.40

It is **not** carried forward. It was an advisory *ceiling* (5× a USD 0.70 estimate) derived from the
260 000-token profile. Two things changed: the per-cell envelope grew 1.92×, and this stage derives
the stop from the pessimistic price instead of from the estimate. The new candidate total
(USD 4.50) is higher than the old ceiling because the stops now sit above a genuinely re-derived HIGH
(USD 2.95), not because the expected spend grew — that moved from USD 0.70 to USD 0.80.

No S06 authorization balance is reused. Every Closure-2 live allowance needs its own owner approval.

---

## I. Offline verification

Everything below ran with 0 model calls and 0 sandboxes.

| Check | Result |
|---|---|
| Preflight | `PASSED`, 62 checks, `failed: []`, digest `3080f313…` |
| Frozen contract (`check_frozen_contract`) | `[]` — preregistration, task table, router digest, budget table, sandbox contract, arm digests all unchanged |
| Candidate budget contract (`check_candidate`) | `[]` |
| Formal plan contract (`check_candidate`) | `[]`, 54 executions, 3 distinct digests |
| Canary contract (`check_contract`) | `[]`, coverage complete |
| Cost contract | `[]`; every stop ≥ its HIGH and ≤ 60 % of the envelope |
| Closure-2 unit + integration tests | **431 passed**, 2 failed — both `test_r2_03` pytest-output classification cases in `tests/integration/test_closure2_preflight.py`, **pre-existing** (see §I.1) |
| New/changed test files | 64 passed (`candidate_budget` 14, `s08_canary` 13, `stage_costs` 8, `formal_plan` 29 — was 20) |
| Regression files on `RunnerSettings` / native baseline | 109 passed (`closure1b_native_baseline`, `closure1a_prompt_layout`, `final4_context_plane`, `r1_2h_oracle_sandbox`, `r1_2_verification_modes`, `final3_inspect_adapter`) |
| Ruff | clean on every changed and added file; `ruff format --check` clean |
| Pyright | 0 errors over the configured `src` + `tests` scope, and 0 over the five new/edited modules. The closure-2 package keeps one **pre-existing** typing error (`pilot_runner.py:237`, `build_task(topology=str)`), in a line this stage did not touch |
| Dry-run manifest | re-derived field-for-field; the only differences against the committed `pilot-dry-run.json` are approval-derived fields that were already stale before this stage (the artifact was generated 2026-11-17, before the record was approved) |
| S06 custody | 1048/1048 digests re-verified, 0 mismatched, 0 missing (§J) |

### I.1 The two pre-existing failures

`test_r2_03_each_pytest_shape_is_classified_and_gated[assertion]` and
`[mixed_failure_and_setup_error]` fail because the classifier in that test no longer matches this
machine's pytest output (`UNKNOWN_ERROR` where `TEST_FAILURE` is expected, `SETUP_ERROR` where
`MIXED_ERROR` is expected). Evidence that it predates this stage: both files are byte-identical to
`e0bc2cc` (`git diff e0bc2cc -- <file>` empty), the failure reproduces in isolation, and it
reproduces in a throw-away worktree at `e0bc2cc`. It is not repaired here — it is an environment
mismatch in a test about the preflight's pytest classifier, outside this stage's scope.

---

## J. S06 evidence immutability

| Item | Statement |
|---|---|
| Custody root | `D:\Work\AtlasRuntime-s06-live-20261124-231047`, outside the repository |
| Re-verification | 1048 entries of `evidence-index.json` re-hashed against `archive/cells`: **1048 match, 0 mismatch, 0 missing**, read-only |
| Committed identity | `e0bc2cc` is still the base of this branch and still the head of PR #6's branch |
| Runner state | `git diff e0bc2cc -- src/` empty |
| Files this stage added | none inside the custody root; the four new artifacts live under `experiments/results/closure2/` |

S06's published numbers are unchanged and are still the ones §H anchors to (36 cells, USD 0.182070,
3 032 172 tokens, 0 proposals).

---

## K. Pending owner decisions

| # | Item | State after this stage | Needed before |
|---|---|---|---|
| R-1 | definition approval vs document | **resolved by evidence**: drift explained, history unedited, zero runnable plans proven by test | — |
| R-2 | worker turn cap | candidate = 20, bound and tested | S10 freeze |
| R-3 | per-cell token budget | candidate = 500 000, bound and tested | S10 freeze |
| R-4 | formal plans not wired | **done**: three ids reachable, cells resolvable, all refused | — |
| R-5 | canary plan spent | **new scope prepared**, `PENDING_OWNER_APPROVAL` | S08 approval |
| R-6 | task order fixed inside a repetition | balanced schedule prepared and wired as a candidate; S07 order retained | S10 freeze |
| R-7 | cost ceilings unapproved | candidate USD 0.45 / 0.45 / 3.60, total USD 4.50 | per-plan approval |
| R-8 | v2.1 Main prompt unavailable | unchanged: all arms keep the default prompt | S10 decision (recommendation: keep it) |
| R-9 | `parallel_overlap` unobservable | unchanged; the canary criterion records the wave, never an overlap | V1 acceptance |
| R-10 | S06 prompt finding does not transfer | unchanged | — |
| R-11 | canary and formal r1 share `(cell, repetition 0)` | declared exclusion written before execution; the formal plan still runs them | S08 approval should acknowledge it |
| R-12 | candidate budget needs a frozen registration revision | the candidate has its own digest and a derived registration identity; the frozen `3080f313…` is untouched | S10 writes the new revision |

The last two are new. R-11 exists because a canary that covers the real mechanisms must touch real
cells; R-12 exists because a budget is part of a registration, and this stage deliberately produced a
candidate instead of editing the frozen one.

---

## L. S08 independent authorization template

The only record that can authorize the canary. It ships `PENDING_OWNER_APPROVAL`, and the shipped gate
refuses the plan while it does.

```json
{
  "status": "PENDING_OWNER_APPROVAL",
  "approved_plan_digest": "f3d09a8523ae3312872be31dfcc1c1544104de1afa3c9db3b6a5f10108589fbd",
  "approved_by": null,
  "approval_commit": null,
  "approved_at": null,
  "scope": {
    "plan_id": "closure2-s08-canary-1",
    "cells": [
      "closure2-pipeline-migration::N0",
      "closure2-pipeline-migration::A0",
      "closure2-pipeline-migration::N1",
      "closure2-pipeline-migration::A1-L",
      "closure2-pipeline-migration::A1-P",
      "closure2-pipeline-migration::A2-P",
      "closure2-state-machine-refactor::A2-P"
    ],
    "repetitions": [0],
    "max_cell_executions": 7,
    "estimated_cost_stop_usd": 0.45,
    "budget_profile_digest": "b41a633d1d39b3fe95845498b4b6c575b6615b74033ddab1b825be944ed7f89d",
    "worker_max_turns": 20,
    "per_cell_token_budget": 500000,
    "attempts": 1
  },
  "cannot_be_satisfied_by": [
    "protocol.APPROVAL_RECORD: it approves the benchmark definition, not an execution scope",
    "the S06 authorization: a different stage, a different digest, already spent",
    "closure2-pilot-1 / closure2-routing-pair-1 / closure2-connectivity-canary-v1: consumed",
    "any of the three formal plans: a different digest and a different cell list"
  ],
  "granting_requires": "a new commit that sets status=APPROVED with an approver, that commit's hash, a timestamp and exactly the digest above"
}
```

Approving it authorizes one attempt of seven cells and USD 0.45. It authorizes no formal cell, no
retry, no resume and no rerun of S06 or of the 2026-11-18 scopes.

---

## Evidence index

| Path | Contents |
|---|---|
| `experiments/evaluation/closure2/candidate_budget.py` | the frozen and candidate profiles, their digests, the registration identity, the knob-diff proof |
| `experiments/results/closure2/candidate-budget.json` | the revision record: values, provenance, budget sharing, six-arm comparability |
| `experiments/evaluation/closure2/formal_plan.py` | the three plans, both schedules, the balance audit, the cross-plan aggregator |
| `experiments/results/closure2/formal-plan-candidate.json` | 54 cells, their order, identities, profile binding and per-plan refusal |
| `experiments/evaluation/closure2/s08_canary.py` | the canary scope, coverage matrix, acceptance criteria, evidence contract, template |
| `experiments/results/closure2/s08-canary-contract.json` | the same, as the artifact S08 is authorized against |
| `experiments/evaluation/closure2/stage_costs.py` | the three-layer estimator and the stop rule |
| `experiments/results/closure2/stage-cost-estimate.json` | the stage-wise numbers of §H |
| `experiments/evaluation/models.py` | `effective_agent_max_turns` — the one cap rule both engines call |
| `experiments/evaluation/runner.py`, `closure1b/native_baseline.py` | the run-level override, applied at the agent-config call site |
| `experiments/evaluation/closure2/pilot_runner.py` | plan registry, profile resolution, per-plan summaries and run roots |
| `tests/unit/test_closure2_candidate_budget.py` … `test_closure2_stage_costs.py` | 44 offline proofs |
| `D:\Work\AtlasRuntime-s06-live-20261124-231047` | S06 custody, unchanged, 1048/1048 re-verified |

Nothing in this document authorizes an execution. `LIVE_EXECUTION_AUTHORIZED = NO`,
`DEEPSEEK_CALLS_ALLOWED = 0`, `REAL_SANDBOX_CREATION_ALLOWED = 0`, `S08_CANARY_AUTHORIZED = NO`.
