# Stage C2 — Coding-aware Natural Delegation Decision

**Status:** `STAGE_C2_NATURAL_DELEGATION_READY_FOR_LIVE_AUTHORIZATION`.
Preparation only: no model call, no sandbox, no production claim. Every number below is an offline
fact about this repository at `feat/stage-c2-natural-delegation`.

Plan: `stage-c2-coding-aware-natural-delegation-v1` · scope `C2_NATURAL_DELEGATION_DIAGNOSTIC_ONLY`
Plan digest `sha256:d0a4c212fda8bf76b20efd995115474b326cf78f787bd162b05c75829bc16038`
Executor `bplus-executor-1.7` / `sha256:e7d7bf87822976c11d497c75ed9a0799d19d57ec39ecc33de5858591f0859b12`

## 0. The question this stage asks

Stage C1, C1-B and C1-C all ran a user goal that carried an explicit appendix asking the Main to
delegate the independent parts of the task. All three produced real proposals, real admissions and
real workers; C1-C ran the full path to delivery and still failed the independent oracle. None of
them measured the thing this stage is about: **whether the Main chooses a delegation route on its own
when the goal says nothing about delegation.**

C1-C's closure rule applies — no C1-D, no further prompt tuning for P2, no budget increase. C2
therefore does not re-run P2. It removes the appendix and varies one thing inside a task: the Main
decision prompt.

## A. Task suitability — P+ and N−

Both tasks are reused from the frozen Closure-2 catalog
(`experiments/evaluation/closure2/catalog.py`). No fixture, public suite or oracle is authored here,
and no answer is written by this stage.

| | **P+** `closure2-pipeline-migration` | **N−** `closure2-state-machine-refactor` |
|---|---|---|
| seed files / lines | 10 / 160 | 8 / 105 |
| reference implementation | 71 lines (proves the oracle is satisfiable) | 50 lines |
| deliverables | 6 | 4 |
| frozen candidate workers | 3 (`parse`, `aggregate`, `integrate`) | 2 (`machine`, `callers`) |
| worker write sets | `parse` → parsing, normalise · `aggregate` → aggregate, store · `integrate` → api, report | `machine` → state · `callers` → approval, audit, shipping, **state** |
| independent pair (disjoint writes, no declared dependency) | **`parse` + `aggregate`** | **none** |
| shared write path | none | **`lifecycle/state.py`** |
| declared dependency | `integrate` depends on both halves | `callers` depends on `machine` |
| frozen interface in the workspace | `docs/INTERFACE.md`, `test_public.py`, `README.md` | same |
| integration seam left to the Main after the pair | `pipeline/api.py`, `pipeline/report.py` | n/a (no independent pair) |
| public / oracle test functions | 9 / 11 | 7 / 7 |
| context load (seed + shared context + public suite) | 9,310 chars | 6,917 chars |

The selection is **structural, not a file count**. P+ is chosen because its two workstreams own
disjoint files, implement a published interface, need nothing from each other, and leave two modules
of integration work behind; N− is the control because both of its steps must edit the same state
table, so the candidate split is not a split at all. `C00` recomputes this from the catalog on every
run and `C05` feeds the two frozen worker sets through the real router.

Honest limits of this selection, recorded rather than smoothed over:

- the fixtures are small in absolute terms (P+ is 160 seed lines). Whether a task this size *should*
  be delegated is the question the stage measures; the suitability argument rests on ownership and
  interface structure, not on volume;
- P2 is deliberately **not** reused: it has three real runs, all ending in an oracle failure, and a
  fourth run would not be a new question;
- whether a fixture this size admits a *beneficial* delegation is not claimed. Direct execution is a
  legitimate outcome for P+ too, and `C00` records the workload numbers so a reader can disagree with
  the selection on the evidence.

## B. Main prompt diff — B0 → B1

**B0** `main-v2.2-rc1-c1b` — byte-identical reuse of `experiments/stage_c1b/prompts.py`
(`sha256:4f742d08c6ed04c6ecd07ac446825e17bc8cdf78ec04e956845d64c3092093af`, 9,768 chars).
The module is imported, not copied, and its digest is re-checked.

**B1** `main-v3.0-rc1-coding-aware` (`sha256:32e082a7…`, 13,414 chars, +37.3 %). Exactly three
section bodies are replaced; the heading list and order are identical, and every other section is
byte-identical to B0.

| # | section | problem in the retained bytes | change | chars |
|---|---|---|---|---|
| R1 | `## EARLY PLANNING CHECKPOINT` | the checkpoint recognises only *investigation* work, so two disjoint implementation halves are never a candidate, and there is no property list to assess them by | investigation and implementation work are separated; seven properties (task scale, workstream independence, interface stability, exclusive writable scope, concrete deliverables, context exposure, expected coordination cost) are named; a coding-candidate list is added; the Direct decision stays valid when coordination costs more than it saves | 2,042 → 4,162 |
| R2 | `## DELEGATION CONTRACT` | the contract describes a bounded *research* task only: nothing says who may write where, that one concurrent batch must not share a writable path, or what an implementation worker returns | implementation-task fields added: writable ownership, disjoint paths in one parallel wave, and the completion report. No research instruction is changed | 1,163 → 1,900 |
| R3 | `## HANDOFF-FIRST INTEGRATION` | the handoff section assumes the artefact is findings; after a coding batch it is code in a shared workspace, and the boundary between "consume the report" and "verify the change" is not stated | names the code-plus-report artefact, requires the changed files to be read before they are relied on, frees the Main from re-implementing a module it reviewed, keeps verification mandatory | 817 → 1,606 |

What is **not** changed: `## IDENTITY`, `## EXECUTION WORKFLOW`, `## WHEN TO WORK DIRECTLY`,
`## IMPLEMENTATION AND VERIFICATION DISCIPLINE`, `## CONTEXT AND COST`, `## WORKSPACE SAFETY`,
`## FINAL DELIVERY`, and — byte for byte — C1-B's `## TESTING AND RECOVERY`, the section whose
absence caused the C1 verification loop. All of that is asserted, not asserted-about:
`prompts.unchanged_sections()` must be all `True`.

Neither prompt asks for a proposal. B1 states that "nothing here requires a proposal, a fixed worker
count, or a delegation call on any particular task", that "working directly is a normal outcome", and
that "delegation is a preference based on expected value, not a mandatory action". `C02`/`C11` refuse
a mandate phrase (`you must delegate`, `at least one proposal`, `never work directly`, …) and refuse
any task or design token (`closure2`, `coding_worker`, `pipeline/`, `lifecycle/`, `plugins/`,
`benchmark`, `experiment`, `test_public`, `test_hidden`, …).

**Full diff.** The complete before/after bytes of all three sections — not a summary — are in
`experiments/stage_c2/plan.json` under `prompt_changes` (with per-section digests and char counts),
and are reproducible with:

```powershell
.\.venv\Scripts\python.exe -c "from experiments.stage_c2 import prompts; [print(r['id'], r['section'], len(r['before']), '->', len(r['after'])) for r in prompts.change_records()]"
```

## C. The four-cell design

| order | cell id | task | prompt arm |
|---|---|---|---|
| 1 | `c2.closure2-pipeline-migration.C3.B0.r1` | P+ | B0 |
| 2 | `c2.closure2-state-machine-refactor.C3.B0.r1` | N− | B0 |
| 3 | `c2.closure2-pipeline-migration.C3.B1.r1` | P+ | B1 |
| 4 | `c2.closure2-state-machine-refactor.C3.B1.r1` | N− | B1 |

- One attempt per cell, one repetition, `is_benchmark_sample: false`, `is_diagnostic: true`.
- **Within a task** the two cells differ only in `main_prompt{,_version,_digest}`, `prompt_arm`, and
  the fixed execution position (`order`, `round`, `cell_id`). `C02` computes the differing key set on
  the record and refuses anything outside that allowlist; goal, grant, worker contract, budget,
  profile, exposure and execution contract must be identical.
- The order is fixed here and must not be amended because of a result: grouped by arm, both baselines
  first. Consequence: the two B1 cells run third and fourth, so a machine-level warm-up effect is not
  counterbalanced. With four cells there is no way to counterbalance it; recorded as a limitation.
- The user goal is the task's own published requirement — `contract.GOAL_OF[task_id]` — byte for
  byte. No activation appendix, no decomposition, no worker count, no writable-path list, no DAG, no
  P+/N− label, no experiment vocabulary, no prior result.

## D. Production configuration and frozen identity

| item | value |
|---|---|
| model / provider | `deepseek-chat` / `deepseek` |
| worker | `coding_worker`, exposure `card`, worker completion contract `c1b-1.0` (`sha256:5b0fa74e…`) |
| turns | Main 20, child 20, worker 20 |
| shared token budget | **750,000 per cell** (`sha256:d05da339…`), 4 × 750,000 = 3,000,000 capacity |
| delegation | 2 concurrent / 2 per call / 3 rounds / preview 900 chars |
| public suite | `run_public_tests`, max 4 calls |
| sandbox | OpenSandbox, network `none`, 1 cpu, 1 GiB, `atlas-uat-python:3.12`, 30 min |
| router | `m.1.0` — unchanged |
| matrix concurrency | 1 |
| execution contract | `bplus-exec-3.1` / `sha256:…` (unchanged from Stage B+) |
| protocol | `bplus-2.2.0` / `sha256:65e0b993…` (unchanged) |
| executor | `bplus-executor-1.7` — the same executor program; the identity list gained the two C2 modules because `contract.py` had to gain the plan entry |
| P+ grant | raw `pipeline/**` → effective `pipeline/` via `normalize_declared_path` |
| N− grant | raw `lifecycle/**` → effective `lifecycle/` via the same normaliser |

Both grants are the task's own frozen `allowed_paths`, converted by the repository's own normaliser,
exactly as Stage C1 did for `plugins/**`. Nothing is widened: `scope_changed: false`,
`profile_changed: false`, `matches_frozen_task_scope: true`, and `C03` checks that every deliverable
is inside the effective grant. The Main's own write scope is the task scope, unchanged.

The budget moves from the frozen 500,000 to 750,000 — the C1-C value — so the decision question is not
confounded with the C1-B budget stop. `contract.BUDGET` itself stays at 500,000 and every earlier plan
still resolves to it (`C01`, `C08`).

## E. Formal entry, offline control flow

`executor.dispatch(PLAN_ID, mode=MODE_OFFLINE_PROOF)` — the production entry, unmodified — resolved
the plan by id, verified a signed record in memory, claimed in a throwaway claim root, ran four
synthetic cells and wrote the audit and matrix report:

```
plan_digest  sha256:d0a4c212…     mode offline_proof     complete true
cells        4 planned / 4 started / 4 passed           stop_reason NONE
verdicts     PASS ×4              execution_integrity PASS ×4   goal_correctness true ×4
token_stop   3,000,000            cost_stop 1.00 / per-cell 0.25
evidence     matrix-report.json, ledger.jsonl, attempt + analysis for c01…c04
```

`C06` is that run; `C07` is the pre-claim admission over the same four `GoalRunOptions`.

## F. Production admission

`executor.run_admission_preflight` → `status: READY`, no blocking failure, no contract blocking
failure, for all four cells: `coding_worker`, exposure `card`, oracle mode
`EXTERNAL_SANDBOX_ORACLE`, fixture matches the frozen seed. The machine credential is reported as
present and is *not* used: admission builds no provider, calls no model and creates no sandbox.

## G. Metrics, interpretation, and the data source of record

The reporting contract is data (`plan.REPORTING_CONTRACT`), carried into
`authorization.json.review_surface`, and `C12` verifies that every field it names exists on the
runtime model that writes it.

**Correctness is reported on five separate axes** — static acceptance, public test, independent
oracle, Main completion, execution integrity. Final correctness is the **cell audit's oracle verdict**
(`CellRun.oracle.status` / `.passed`), which for a live run is the external sandboxed oracle on the
exported workspace.

It is explicitly **not** `experiments/stage_benchmark/reconstruct`'s `verification.oracle` /
`L6_goal_correctness`. That field is the in-run static acceptance; in Stage C1-C it read `PASS` while
the independent oracle returned `FAIL`. The exclusion is checked against the analyzer's own `LEVELS`
constant, so the trap cannot quietly reappear under the same name.

**Cache** is read from provider usage fields: Main `main_cache_hit_tokens` / `main_cache_miss_tokens`;
children `delegation.totals.cache_hit_tokens` / `.cache_miss_tokens`. The analyzer's
`child_cache_visibility` is a constant `UNKNOWN` and reports `UNKNOWN` even when the split exists, so
it is excluded by name. A hit is inside prompt tokens and is never added on top of a prompt total;
`UNKNOWN` is not zero.

**Handoff** is three separate facts, never one:
preview delivered → `delegation.batches[].result_chars` + `result_truncated`;
full reference read → `delegate.read` calls in the Main transcript;
shared-file consumption → Main `file_read`/`file_edit` on worker-owned paths after the batch.
In C1-C the preview was delivered and the reference was never read — the Main simply read the shared
workspace files — so conflating the three would have misreported a delivered handoff as a consumed
one.

**Diagnostic metrics** per cell: natural proposal (a `delegate_tasks` call the goal did not ask for),
first proposal turn, the Main's visible planning reason, router admission, reason codes, route,
worker completion, observed parallelism, completion reports, Main artefact consumption, final public
test, independent oracle, execution integrity. Resource metrics: Main peak context, Main cumulative
input, child input/output, system tokens, cache hit/miss, cost, latency, ownership refusals, repeated
reads / no-op operations, and the turn order of the last effective write versus the last public test.

Reading rules, fixed before the run:

- delegation count is **not** a metric that is better when higher;
- a P+ cell with no proposal is **not** automatically a model failure — it is a Direct decision that
  must be argued from the recorded evidence (task scale, coupling, coordination cost);
- an N− proposal refused by the router and an N− Direct choice made without a proposal are recorded
  as different facts;
- four cells, one attempt: no rate, no causal effect, no significance claim. The stage can show that
  a route was chosen under one prompt and not the other **on these two tasks**; it cannot show that
  B1 makes the Main delegate in general.

## H. Pending Owner authorization

`experiments/stage_c2/authorization.json` is `PENDING_OWNER_APPROVAL` and authorizes nothing:
`authorizes_live_execution: false`, `cost_stop_usd: null`, `per_cell_cost_stop_usd: null`,
`token_stop: 3,000,000`, `attempt_limit: 1`, all six signature fields `null`. `C10` proves the
verifier refuses it.

Candidate envelope — **none of it approved**:

| field | candidate | basis |
|---|---|---|
| per-cell cost stop | USD 0.25 | C1-C measured USD 0.086984 for one cell; about 2.9× margin |
| cumulative cost stop | USD 1.00 | four cells at the per-cell candidate |
| cumulative token stop | 3,000,000 | four cells × 750,000 |
| attempt limit | 1 | one dispatch for the whole plan |

A live dispatch additionally requires: the reviewed head recorded as `reviewed_head` and
`execution_head`, a non-empty `approved_by` / `approved_at` / `owner_statement`, an external
provenance artefact whose bytes still hash to the recorded value, and a clean worktree with the
production claim root in force.

## I. History preserved

- Six consumed production claims — B+ v1, v2, v3, C1, C1-B, C1-C — are byte-identical before and
  after this preparation, and are re-hashed by `C09`.
- No claim exists for the C2 plan digest.
- The C1-C evidence index (`D:\Work\AtlasRuntime-runs\stage-c1c-headroom-control`, 35 files,
  `sha256:5defb4cd…`) re-hashes to its recorded digest. It is read only.
- B+ canary/formal, C1, C1-B and C1-C plan digests, the C1-C budget digest, the C1-C executor identity
  and the B+ protocol digest are unchanged (`C01`, `C08`). The superseded canary ids stay refused.
- **Corrections outside the new module.** Stage C1-C's own live run consumed its claim, so two of
  C1-C's recorded assertions described a pre-run state that no longer exists: `_check_history` claimed
  "no claim exists for the C1-C plan", and two integration tests asserted the same and compared the
  C1-C executor to the *current* identity. They now assert the post-run fact (the claim exists, is
  the C1-C claim, and moved during no preparation) and freeze the C1-C executor at 1.6. No C1-C
  evidence file was rewritten.

## J. Future live entry

```powershell
# after the Owner signs experiments/stage_c2/authorization.json and records the reviewed head
.\.venv\Scripts\python.exe -c "import asyncio, sys; from pathlib import Path; from experiments.stage_b_plus import executor; r = asyncio.run(executor.dispatch('stage-c2-coding-aware-natural-delegation-v1', runs_root=Path(r'D:\Work\AtlasRuntime-runs\stage-c2'), evidence_root=Path(r'D:\Work\AtlasRuntime-runs\stage-c2\evidence'), authority_path=Path(r'D:\Work\AtlasRuntime-authorizations\stage-c2.json'), mode=executor.MODE_LIVE)); print(r['stop_reason'], r['cells_passed'], '/', r['cells_planned'])"
```

A live dispatch refuses: a dirty worktree, a claim root override, a non-live cell runner, injected
prices, a consumed claim, or a record whose head/executor/plan/budget/protocol digests do not match.
Launch it detached (see `docs/limitations.md` and the harness notes) — a foreground shell can be
cancelled while the model is still running, and the one attempt is unrecoverable after the claim.

## K. Known limitations

1. Four cells, one attempt each. No rate, no causal effect, no significance. The two tasks are not a
   sample of anything.
2. B1 is a **combined** three-section revision. It is not an ablation of any one section, so if B1
   differs from B0 the cause is not attributable between R1/R2/R3 without a further run.
3. B1 discloses the "one concurrent batch must not share a writable path" invariant. An N− Direct
   decision under B1 may therefore be caused by that disclosure rather than by better structural
   analysis. The report must not read it as a capability difference.
4. The fixtures are small. Direct execution may be the correct decision on P+ as well; that outcome
   is a legitimate result and makes the P+ pair uninformative for the delegation question rather than
   proving a prompt effect.
5. Order is fixed, baselines first, no counterbalancing (matrix concurrency 1).
6. The router and the ownership policy are untouched: an unreasonable proposal is refused by the
   existing gates, and C2 adds no special-case rejection.
7. The analyzer's `L6_goal_correctness` / `verification.oracle` and `child_cache_visibility` defects
   are **worked around in the reporting layer**, not fixed in the analyzer. A reader using the Stage B
   analyzer directly will still get an in-run oracle verdict where an independent verdict is meant.
8. P2's three oracle failures stand as a negative result. This stage does not re-run P2 and makes no
   claim that P2 is solvable.
9. The cost and token stops are candidates. A live run needs the Owner's numbers, not these.
10. The executor identity moved to 1.7 because `contract.py` gained the plan entry. Earlier stages'
    recorded identities are frozen at the values they ran under; their evidence is not rebuilt.

## Verification performed

```powershell
.\.venv\Scripts\python.exe -m experiments.stage_c2.checks      # 13/13 PASS, artefacts rewritten
.\.venv\Scripts\python.exe -m pytest tests/integration/test_stage_c2_natural_delegation.py        # 17 passed
.\.venv\Scripts\python.exe -m pytest tests/integration/test_stage_c1c_headroom_control.py         # 16 passed
.\.venv\Scripts\python.exe -m pytest tests/integration/test_stage_c1_positive_control.py tests/integration/test_stage_c1b_completion_control.py tests/integration/test_stage_b_plus_execution_contract.py tests/integration/test_bplus_admission_contract.py   # 137 passed together with the two above
.\.venv\Scripts\python.exe -m pytest tests/unit                                                   # all passed
.\.venv\Scripts\python.exe -m ruff check experiments/stage_c2 tests/integration/test_stage_c1c_headroom_control.py experiments/stage_c1c/checks.py experiments/stage_b_plus   # clean
.\.venv\Scripts\python.exe -m ruff format --check experiments/stage_c2 tests/integration/test_stage_c1c_headroom_control.py experiments/stage_c1c/checks.py   # clean
.\.venv\Scripts\python.exe -m pyright tests/integration/test_stage_c2_natural_delegation.py       # 0 errors
```

**Lint debt, reported rather than hidden.** `ruff check .` over the whole repository is *not* clean
at this commit: 66 findings, all of them in `experiments/stage_s1_prompt_v2/` and
`experiments/fixtures/stage_s1_releaseguard/`, none in a file this stage touched, and all present
before it. `ruff format --check .` reports 34 files for the same reason. They are outside this
stage's scope and were left alone (AGENTS §6: record, do not fix out of scope). The files this stage
created or edited are clean under both commands.

One thing worth recording about the partial suite: `pytest tests/integration` as a single call shows
a batch of failures in `test_stage_p_*`, `test_stage_d_*` and `test_verified_reattachment.py`. Each of
those files passes when re-run alone, and the failure mode is the IDE's safe-delete guard firing
inside the fake sandbox teardown, not an assertion. They are unrelated to this stage and are not a
regression.

Not run, and not claimed: any live model call, any sandbox creation, any production claim, the Stage
B+ formal matrix, the Closure-2 independent evaluation.
