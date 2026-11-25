# Stage B+ — Six-Arm Capability x Exposure Ablation (final offline closure)

Status: candidate `STAGE_B_PLUS_READY_FOR_LIVE_CANARY_REVIEW`
Protocol `bplus-2.0.0` · digest `sha256:c7dad972…` · executor `bplus-executor-1.0` `sha256:917d95e4…`
Canary plan `sha256:7ae71273…` (6 cells) · Formal plan `sha256:ab4d0e50…` (24 cells, unchanged)
Baseline `e0bc2cc` · no model call, no sandbox, no cell.

Companion: `docs/stage-b-plus-capability-audit.md` (Phases A, B, E — what the code does).

---

## B. Owner decisions, as implemented

| id | Decision | Where it lives in code |
|---|---|---|
| R-B+1 | `main-v2.1-rc1-bplus` = P3 minus the `## WORKER CAPABILITY CONTRACT` section | `contract.prompt_text("p3-bplus")`; `p3-literal` kept and digest-pinned |
| G-B+2 | the Main verifies; no worker test tool, no bash widening | nothing added to any profile; `run_public_tests` stays Host-only |
| G-B+3 | router rules unchanged | no change to `routing.py`; `TRIVIAL_DIRECT_TASK`, `SHARED_WRITE_RISK`, `WRITE_SCOPE_NOT_GRANTED`, `UNKNOWN_PROFILE` all still fire (probes `L06`–`L08`) |
| Canary | six cells including the two no-disclosure controls | `contract.ARMS` C0–C5, `canary_cells()` |

The formal matrix is still the four-arm crossed design: `FORMAL_ARM_IDS = C0..C3`. C4/C5 are
controls, marked `is_control`, excluded from the formal plan, and every canary cell carries
`is_benchmark_sample = false`.

---

## C. Exposure terminology (as corrected)

| Level | Label | What the Main gets | New in this stage |
|---|---|---|---|
| `none` | shipped | the shipped description, byte for byte (819 chars) | no — it is the pre-existing text |
| `native` | basic derived disclosure | derived role, the tools it has, the tools it does not have, its limits, how a result returns | **yes** |
| `card` | full capability contract | the above plus capability, ownership, task-input, deliverable, verification and handoff contracts | **yes** |

**`native` is not S06's exposure.** S06 had no derived disclosure at all; its only capability text
was the prompt paragraph this revision deletes. C0/C2 therefore compare "derived prose" with "derived
card", and C4/C5 supply the missing no-disclosure baseline.

### What the numbers can and cannot mean (`contract.COUPLING_DISCLOSURE`, X1–X7)

* **X1** profile and description are coupled: a disclosure is derived from the profile that runs.
* **X2** `native` and `card` differ in content and length (≈2.2–2.5 k characters), not only in shape.
* **X3** the frozen schema still defaults `profile_id` to `research_worker` and has no `enum`.
* **X4** C5 can lose proposals to that default; `UNKNOWN_PROFILE` is counted separately.
* **X5** `NO_PROPOSAL` is not `ROUTER_REJECTED`; admission is `N/A`, never 0.
* **X6** `C2 − C0` is not a pure permission effect.
* **X7** **C4 and C5 send byte-identical requests** (both 819 chars, same digest). With no
  disclosure the two controls differ only in which profile the run accepts, so a `C5 − C4` difference
  cannot be attributed to the Main's information at all.

---

## D. The six-cell canary

`stage-b-plus-diagnostic-canary-v1`, scope `CANARY_ONLY`, task `closure2-pipeline-migration` (P1),
one repetition, fixed order C0…C5, digest `sha256:7ae71273…`.

| order | cell | arm | profile | exposure |
|---|---|---|---|---|
| 1 | `canary.closure2-pipeline-migration.C0.r1` | C0 | research | native |
| 2 | `…C1.r1` | C1 | research | card |
| 3 | `…C2.r1` | C2 | coding | native |
| 4 | `…C3.r1` | C3 | coding | card |
| 5 | `…C4.r1` | C4 | research | none |
| 6 | `…C5.r1` | C5 | coding | none |

Fixed order, not randomized: six cells are a mechanism diagnostic and a readable order is worth more
than a shuffle nobody can balance. Canary and formal share nothing that could be confused — different
plan id, digest, cell ids (`canary.` prefix), claim namespace, runs subdir, evidence subdir and
authorization record.

---

## E. Six actual provider inputs

Captured from the real composition (`inputs/request-C*.json`, `inputs/diff.json`). Common to all six:
system prompt 9,033 chars `76ebb03d`, `delegate_tasks` schema 1,131 chars `b76380f3`, first user turn
identical.

| arm | profile | exposure | description chars | description digest | effective turns | write grant |
|---|---|---|---|---|---|---|
| C0 | research | native | 1,613 | `8a801b21` | 20 | none |
| C1 | research | card | 3,030 | `3f364f23` | 20 | none |
| C2 | coding | native | 1,805 | `9fc86fd3` | 20 | `pipeline/**` |
| C3 | coding | card | 3,336 | `365593c9` | 20 | `pipeline/**` |
| C4 | research | none | 819 | `387e2594` | 20 | none |
| C5 | coding | none | 819 | `387e2594` | 20 | `pipeline/**` |

Five distinct descriptions, six arms: C4 and C5 are identical on the wire (X7). No arm receives the
frozen DAG, a worker assignment, a placement decision, the reference implementation or the hidden
oracle; no arm carries a delegation instruction.

---

## F. The minimal live executor

`experiments/stage_b_plus/executor.py` + `authority.py`. One entry point, `dispatch(plan_id, …)`,
which is the only thing that can execute a cell:

```text
dispatch
 ├─ resolve plan from the contract (canary or formal)          → scope, digest, ordered cells
 ├─ executor_identity()                                        → digest over the 4 deciding files
 ├─ git_head()                                                 → the actual execution head
 ├─ authority.verify(record, plan, head, executor, protocol)   → field-by-field, refuses on any gap
 ├─ create <evidence>/dispatch.claim   O_CREAT|O_EXCL          → exactly once, never reopened
 ├─ fresh ledger check                                         → an existing ledger is never appended
 └─ per cell, in plan order
     ├─ create <evidence>/cells/<id>/attempt.json  O_CREAT|EXCL → one attempt per cell
     ├─ append dispatch.jsonl (fsync)
     ├─ cell_runner(cell, context)                            → live: DeepSeek + OpenSandbox
     ├─ audit_cell(...)                                       → the post-cell gate
     ├─ append ledger.jsonl (fsync) + analysis.json
     └─ stop? → break with the first stop reason
```

`cell_runner` is the single injected dependency:
`executor.live_cell_runner()` builds the real DeepSeek adapter and the real `GoalSandbox` (network
none, image `atlas-uat-python:3.12`, cpu 1, 1 GiB) and runs the real public suite;
`executor.host_cell_runner()` runs the same `run_goal` in host mode with a scripted provider and is
what the offline checks use. Nothing else in the module knows which one it got.

Reused, not rebuilt: `run_goal` (the whole agent loop, delegation service, ownership policy, handoff
store), `GoalAcceptance`/`PublicTestContract` (Stage P), `closure2_preflight._run_pytest` (the oracle
runner and its classification), `stage_benchmark.estimate` (pricing), the `O_CREAT|O_EXCL` claim
pattern from Stage B's admission module. New code is the authority contract, the gate, the dispatch
loop and the identity digest.

**The oracle is independent and host-side.** Hidden tests are written into a *copy* of the finished
workspace, never into the workspace the agents used, so a Main cannot read them during the run and a
mutated workspace cannot invalidate the verdict.

---

## G. Authorization and the one-shot claim

`experiments/stage_b_plus/authority.py`, contract `bplus-authority-1.0`. A signature binds, and is
re-checked against, every one of:

`status` · `authorizes_live_execution` · `scope` · `plan_id` · `plan_digest` · `protocol_digest` ·
`reviewed_head` · `execution_head` · `executor_digest` · `budget_digest` · exact `cell_ids` in exact
`cell_order` · `cells_count` · `arms` · `tasks` · `cost_stop_usd` · `per_cell_cost_stop_usd` ·
`token_stop` · `attempt_limit = 1` · `owner_statement` · `approved_by` · `approved_at` ·
`approval_provenance` · `reuses_s06_or_s08_balance = false`.

Refusal cases, all covered by tests: unsigned; scope mismatch (canary signature vs formal plan and
the reverse); wrong head; wrong plan/protocol/executor/budget digest; a missing, extra, duplicated or
reordered cell; a zero, negative, boolean, infinite, NaN or string cost; a per-cell stop above the
cumulative stop; `attempt_limit != 1`; an already-consumed claim; a provenance whose bytes no longer
hash; an approval commit that *is* the execution head.

**No circular dependency.** `approval_provenance` names an artefact outside the record — a signed file
(re-hashed from bytes) or an earlier commit (must be an ancestor, and must not be the head). A
signature cannot name the commit that contains it.

Two independent pending records ship, both with every signature field `null`:
`authorization-canary.json` (CANARY_ONLY) and `authorization-formal.json` (FORMAL_MATRIX_ONLY). For the
canary, the proposal in `cost.json` is a **$0.60 cumulative stop / $0.15 per cell**; the earlier $0.40
figure was for a four-cell shape and does not carry over. The formal $3.00 remains a candidate.

---

## H. Dispatch integrity: negative controls

Every stop below is exercised offline and is in `offline.json` as `L16`–`L23`.

| Situation | Result |
|---|---|
| a measured goal failure (`oracle_failed`, `public_tests_failed`, `budget_exhaustion`, `main_max_turns`) | `MEASURED_FAILURE` verdict, matrix **continues** |
| an unrecognised failure kind | `UNKNOWN_FAILURE_KIND`, stop |
| provider/transport exception | `PROVIDER_FAILURE`, stop |
| sandbox not created | `SANDBOX_FAILURE`, stop |
| sandbox retained or not destroyed | `SANDBOX_CLEANUP_FAILURE`, stop — **including on the last cell** |
| isolation probe failed | `HOST_ISOLATION_VIOLATION`, stop |
| oracle collection error or timeout | `ORACLE_ENVIRONMENT_FAILURE`, stop |
| a counter missing, `bool`, negative or non-integer | `UNKNOWN_OR_ILLEGAL_USAGE`, stop; the aggregate is left `null`, never 0 |
| `session.json` missing or unparseable | `MISSING_OR_CORRUPT_EVIDENCE`, stop |
| applied profile or exposure differs from the arm | `IDENTITY_MISMATCH`, stop |
| cumulative cost or tokens at the stop | `COST_STOP_REACHED` / `TOKEN_STOP_REACHED` |
| second dispatch, or a second attempt on one cell | `CLAIM_CONSUMED`, refused before any cell |

The last cell has no special case: a matrix that ends on a broken sandbox reports `complete: false`,
`stop_reason`, `stopped_at_order` and the anomaly. A `complete: true` report therefore means every
cell ran and none stopped, not merely that the loop ended.

---

## I. Candidate parameter binding

`contract.BUDGET`, one value for every arm and both scopes: main turns 20 · worker turns 20 · child
turns 20 · 500 000 tokens · 2 concurrent children · 2 children per call · 3 rounds · 4 public-test
calls · 900-char preview · concurrency 1 · cpu 1 · 1 GiB · network none · `atlas-uat-python:3.12` ·
`m.1.0` · `corrected`.

Two binding questions answered rather than assumed:

* **Does a profile cap override the run cap?** The child is built with
  `min(profile.max_turns, child_max_turns)`, so a run-level value cannot raise a profile. B+ sets the
  profile's own `max_turns` to 20 through `GoalRunOptions.worker_max_turns`, and every run records the
  effective value; `L05` asserts it is 20 for every arm. A value above `child_max_turns` is refused,
  not clipped.
* **Does 500 000 cover the Main *and* every worker?** Yes — one run-scoped budget is passed to the
  session and charged by the runtime for the Main and each child (`token_budget` on
  `GoalRunOptions`, `budget` block in `session.json`), and `system_total_tokens` is the sum of Main
  input, Main completion, child prompt and child completion. `L11` checks the identity on a real run.
  If the runtime had offered a Main-only budget this would be reported as a blocker; it does not.

---

## J. Canary acceptance contract

Three verdicts, kept apart so a valid run is never mistaken for a successful hypothesis:

| Axis | Meaning |
|---|---|
| **Execution validity** | every cell has a unique correct identity, the right profile and exposure, the same goal/fixture/oracle, a complete runtime result, measurable usage, readable evidence, a clean sandbox teardown and a valid one-shot claim |
| **Natural delegation observed** | the Main called `delegate_tasks` on its own in at least one cell — reported per cell, never aggregated into "delegation works" |
| **End-to-end delegation observed** | a proposal was admitted, a child completed, a handoff was delivered and the Main continued |
| **Goal correctness** | the public suite and the independent host oracle both passed |

The canary may legitimately return six `NO_PROPOSAL` cells. That is a valid diagnostic result, and it
must not be reported as "delegation failed" any more than a single admission may be reported as
"delegation works". `L1`–`L6`, the proposal states (`NO_PROPOSAL`, `INVALID_PROPOSAL`,
`UNKNOWN_PROFILE`, `TRIVIAL_DIRECT_TASK`, `SHARED_WRITE_RISK`, `WRITE_SCOPE_NOT_GRANTED`, `ADMITTED`)
and all eighteen context metrics come from the Stage B analyzer unchanged.

---

## K. Cost, corrected

The frozen price block: hit `$0.006`/M, miss `$0.30`/M, output `$1.20`/M. A miss costs 50× a hit and
**child prompts are priced as misses**, because the provider reports no child cache split.

| | Canary (6 cells, 3.0 M nominal) | Formal (24 cells, 12 M nominal) |
|---|---|---|
| S0 reference (one cell = one S06 cell) | $0.030 | $0.121 |
| S1b delegated, 6-turn child | $0.089 | $0.354 |
| S1 delegated, 20-turn child | $0.497 | $1.986 |
| S2 budget fill at S06's 93.7 % mixture | $0.180 | $0.720 |
| sensitivity: 50 % cache hit | $0.559 | $2.238 |
| sensitivity: high output share | $0.507 | $2.030 |
| sensitivity: 90 % cells delegate | $0.730 | $2.918 |
| sensitivity: children run to 20 turns | $0.807 | $3.229 |
| range | **$0.030 – $0.807** | **$0.121 – $3.229** |

Two corrections to the previous reading:

* **S0 is a reference scenario, not a mathematical lower bound.** A B+ cell has a different task,
  prompt and worker cap from an S06 cell, so "S06 cost" is an anchor, not a floor.
* **S2 is not an absolute HIGH.** It is the nominal 12 M tokens priced at S06's measured mixture
  (93.66 % hits). With a cold cache those same tokens cost more, and the sensitivity rows are how much
  more. The dominant variable is delegation rate × worker turn cap: 20 turns costs 5.6× the 6-turn
  shape, because the child prompt is re-sent every turn and every copy is a miss.

Proposed and unsigned: canary **$0.60** cumulative / **$0.15** per cell; formal **$3.00** cumulative /
**$0.25** per cell. Neither is an approval, neither reuses an S06 or S08 remainder, and the formal
stop does not cover the worst sensitivity row — which is what a stop is for.

---

## L. Targeted offline verification: 29 checks, 29 pass

`offline.json`. Zero model requests, zero sandboxes, zero cells.

| id | Check |
|---|---|
| L01 | six canary cells, unique ids, each arm once, one task, one repetition, not benchmark samples |
| L02 | six provider inputs captured, no provider constructed |
| L03 | C4/C5 keep the shipped description byte for byte |
| L04 | every disclosed arm names its own profile's tools |
| L05 | effective worker turns are 20 in every arm |
| L06 | coding pair adheres to its grant; a research write is refused |
| L07 | `WRITE_SCOPE_NOT_GRANTED` at the router, no `file_write` reached a research child |
| L08 | two children constructed by the real factory and ran their loops |
| L09 | a handoff reached the Main through `delegate.read` |
| L10 | the Main continued after the handoff |
| L11 | one real host-mode `run_goal`: applied exposure and profile match the arm, usage reconciles |
| L12 | the pending record is refused |
| L13 | a signed record is refused when the head or a digest moves |
| L14/L15 | canary and formal signatures are not interchangeable, both directions |
| L16 | a clean six-cell dispatch completes and passes |
| L17 | a consumed claim refuses before any cell starts |
| L18 | a measured failure continues the matrix |
| L19 | a sandbox cleanup failure on the **last** cell stops the matrix |
| L20 | a transport exception is a provider failure and stops |
| L21 | an unreported counter stops instead of becoming 0 |
| L22 | missing evidence stops instead of scoring a pass |
| L23 | a cost stop below one cell ends the matrix after that cell |
| L24 | canary cells are excluded from any formal aggregate |
| X01–X05 | contract self-validates · frozen oracles still discriminate · Freeze 1.6 unchanged · Closure-2 untouched · executor identity is derived |

Tests added in this round: `tests/integration/test_stage_b_plus_execution_contract.py` (34) plus the
updated `test_stage_b_plus_ablation.py` (46) and `test_stage_b_plus_capability_card.py` (18), all
passing; `tests/unit` still 0 failures. **Not claimed:** a green full suite. The 50 failures in
`test_stage_b_matrix_executor.py` + `test_stage_b_execution_projection.py` are unchanged and were
reproduced line-for-line on a pristine `e0bc2cc` worktree.

Limits of the offline evidence, stated: the gate checks run against synthetic cell results, because
the gate is what is under test; one real `run_goal` proves the runner wiring but runs in host mode, so
the gate correctly stops it for having no sandbox evidence. No offline check can prove a cell passes.

---

## M. Final digests

```text
protocol   bplus-2.0.0        sha256:c7dad972f4882e5808dbcaa47f4b4532e101f7e2e44d9e1c90e0eb2c14620067
executor   bplus-executor-1.0 sha256:917d95e46b1575bb6da44427c43b48f68ee8af1ded59e3662758efa951f49590
canary     stage-b-plus-diagnostic-canary-v1  sha256:7ae71273eea29f489edc657ada9a26c834e178289ac6744807350d674dc53575  (6)
formal     stage-b-plus-formal-matrix-v1      sha256:ab4d0e509962885c9a24d981446c4697f00e794b6b03e5a5d9bf42cfe91aee72  (24, unchanged)
budget     sha256 of contract.BUDGET          in both pending records
```

Revision record (`protocol.json → revision`): `bplus-1.0.0` was never authorized and never dispatched;
its protocol digest `789bce6e…` and formal plan digest `ab4d0e50…` are both recorded. The formal plan
digest is *identical* to the previous revision — the 24-cell matrix did not move, only the arm set,
the canary and the execution contract did.

---

## N. Isolation

Stage B Freeze 1.6: `git diff` against `e0bc2cc` is empty; raw `73713095…`, LF `73713095…`,
CRLF `004d4b77…` (the value Stage B recorded). The difference is line-ending rendering in a fresh
worktree, not a modification.

Closure-2 and `experiments/stage_benchmark/` are untouched (`X04`), both S07 commits are still
reachable, and S08's branch, authorization, ledger, runs root and evidence were neither read nor
written. PR #6 still points at `e0bc2cc` and is unmerged.

---

## O / P. Pending authorizations

Both records are complete skeletons with `null` signatures, `authorizes_live_execution: false`,
`reuses_s06_or_s08_balance: false` and `attempt_limit: 1`. To authorize the canary the Owner fills
`reviewed_head`, `execution_head`, `cost_stop_usd`, `per_cell_cost_stop_usd`, `owner_statement`,
`approved_by`, `approved_at` and `approval_provenance`, then sets `status: APPROVED` and
`authorizes_live_execution: true`. The provenance is an external artefact, not the record itself.

The formal record is the same shape for 24 cells. Signing one cannot run the other.

---

## Q. Remaining blockers and the exact question set

**Blockers to execution (none are engineering gaps):**

1. **Owner signature.** Nothing runs without it; the gate refuses in eleven ways.
2. **Credential and sandbox host.** A live dispatch needs `DEEPSEEK_API_KEY` and a reachable
   OpenSandbox server. Neither was exercised here, and the previous live attempt in this repository
   died on a proxy problem, so the connectivity canary is a real prerequisite rather than a formality.
3. **Cost ceiling.** The canary proposal is $0.60; the formal proposal ($3.00) does not cover the
   worst sensitivity row ($3.23) and the Owner may prefer either a higher stop or a lower worker cap.

**Answers:**

1. *Does a formal entry point for the six-cell canary exist?* Yes — `executor.dispatch` with
   `CANARY_ONLY`, bound to the plan digest, the executor digest and the head.
2. *Do C4/C5 really add no disclosure?* Yes: 819 chars, `387e2594`, byte-identical to the shipped
   description, and identical to each other.
3. *Are capability, exposure and budget actually in force?* Yes: exposure and profile are recorded per
   run and checked by the gate (`IDENTITY_MISMATCH` otherwise); effective turns are 20; the writes
   grant is the task's frozen scope; the 500 k budget is shared by Main and children.
4. *Are natural delegation, router refusal and worker failure distinguishable?* Yes — `NO_PROPOSAL`,
   `PROPOSAL_REJECTED` with a reason code, `UNKNOWN_PROFILE`, `ADMITTED` and `MEASURED_FAILURE` are
   separate fields in the same evidence.
5. *Are normal task failures and infrastructure failures handled differently?* Yes: a measured goal
   failure continues, everything else stops, including on the last cell.
6. *Is there an independent, non-retryable claim?* Yes: one `dispatch.claim` and one `attempt.json`
   per cell, both `O_CREAT|O_EXCL`, `retry: FORBIDDEN`, `resume: FORBIDDEN`.
7. *What exactly must the Owner approve?* The head containing this commit, protocol
   `c7dad972…`, executor `917d95e4…`, canary plan `7ae71273…` with its six named cells in that order,
   and $0.60 / $0.15. The three values are re-derived at dispatch time; a signature naming anything
   else is refused.
