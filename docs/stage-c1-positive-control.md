# Stage C1 — Coding Delegation Positive Control (preparation)

Status: **`STAGE_C1_POSITIVE_CONTROL_READY_FOR_LIVE_AUTHORIZATION`**

The positive control is frozen, its offline verification passes 12/12, its admission is admitted by
the frozen router through the production configuration, and its pending authorization authorizes
nothing.

This file was first written when the stage was blocked: the frozen Stage B+ grant (`plugins/**`) was
refused by the router for every declared write path (`WRITE_SCOPE_NOT_GRANTED`), because the router
matches a *normalised* declaration and the grant reached it as a glob. The Owner chose the minimal
correction (design A): the C1 cell now binds the same scope through the repository's own
`normalize_declared_path` (`plugins/**` → `plugins/`). The router, the runtime and the scope are
unchanged; §B records both outcomes.

No model was called, no sandbox was created, no claim was written or consumed, no authorization was
signed, and no Stage B+ artefact was rebuilt.

---

## A. Git provenance

| item | value |
| --- | --- |
| repository | `https://github.com/Melon235/AtlasRuntime` (local `D:\Work\AtlasOS`) |
| baseline branch | `feat/stage-b-plus-capability` |
| baseline HEAD | `a55e106893a6893734db6da62c0dafa1f1fa5e2f` |
| stage branch | `feat/stage-c1-positive-control` (created from the baseline HEAD) |
| stage commit | `0a16312` (the plan, the checks and the artefacts), then the grant-correction commit |
| B+ history | untouched: no commit of that branch was amended, rebased or force-pushed |

`experiments/stage_b_plus/` changed in exactly four places:

| file | change | why |
| --- | --- | --- |
| `contract.py` | new `goal_for(cell)`; new `child_grants_for(cell)`; `scope_plan` resolves the C1 plan id/scope; the "only P1 can admit a coding pair" comment replaced by the measured fact | a per-cell goal and a per-cell Host grant must resolve without moving `GOAL_OF` or `Arm.grant_for`; the C1 plan needs one seam into `dispatch` |
| `executor.py` | `production_goal_options` uses `contract.goal_for(cell)` and `contract.child_grants_for(cell)`; `EXECUTOR_VERSION` → `1.3`; `experiments/stage_c1/plan.py` added to `EXECUTOR_FILES` | the goal and the grant a cell executes must be the ones in the plan digest, and the file that decides them belongs in the signed identity |
| `BUDGET`, `EXECUTION_CONTRACT`, task catalog, arms | *unchanged* | the C1 cell runs on the frozen budget, contract and catalog |

`contract.protocol_document()` output is byte-identical to the baseline: the B+ protocol digest is
still `sha256:65e0b993…`, the canary plan digest still `sha256:0b58ab25…`, the formal plan still
`sha256:ab4d0e50…`. The C1 plan is deliberately **not** added to that document, so no Stage B+
identity moved.

## B. P2 actual admission suitability

Task `closure2-plugin-migration` (family P2). Checked against the catalog, the frozen interface and
the production router path, with a fake proposal that exists only on the Host and never entered any
model context.

**Suitable in structure.** The two implementation workstreams the Main would need declare disjoint
writes and need no interface change:

| step | owned paths | dependency |
| --- | --- | --- |
| `csv` | `plugins/csv_plugin.py`, `plugins/csv_fields.py` | none |
| `json` | `plugins/json_plugin.py`, `plugins/json_fields.py` | none |
| `register` (integration, left to the Main) | `plugins/registry.py`, `plugins/loader.py` | `csv`, `json` |

`plugins/base.py` (the frozen plugin interface) is part of the seed and listed in
`forbidden_paths`, so the migration needs no contract edit. The task write scope is `plugins/**`,
which contains every deliverable. `coding_worker` carries `file_write`, `file_edit`, `grep`,
`list_files`, `file_read`, `tool_search`, `dispatch_action` and `bash`, with 20 effective turns in
this run, so both steps are implementable inside their own declared files. The registration step is
*not* delegable in one wave (the router refuses a declared dependency), which is why the Main keeps
integration — exactly the split the design assumed.

**The root cause, measured.** Before the correction the production router refused the P2 pair:

```
grant:        ("plugins/**",)     route: direct
reason code:  WRITE_SCOPE_NOT_GRANTED
blocked gate: GATE_M_PROPOSAL      (refused before the frozen R0.1.0 router is consulted)
admitted:     []
```

The Host grant was the task's glob (`contract.py:236-238` `TASK_WRITE_SCOPE = task.allowed_paths`,
`contract.py:332-342` `Arm.grant_for`), and it reached `main_driven.py:711` unchanged. The router
compares a declared target with the raw grant (`delegation/router.py:193-216` `_write_scope_problem`
via `ownership/models.py:145-156` `path_is_within`), which matches only a normalised file or a
directory declaration ending in `/`. The *enforcement* layer already normalised the same grant
(`ownership/manifest.py:114-118` → `ownership/paths.py:66-84`), so one scope had two representations
that disagreed at the admission gate only. The defect was not P2-specific: every Closure-2 task's
`allowed_paths` is a glob, and the six Stage B+ V3 cells produced no proposal, so the path had never
been exercised.

**The correction (design A).** The C1 cell binds the Host grant through the repository's own
normaliser, at the real sandbox workspace root:

| representation | value | where |
| --- | --- | --- |
| raw (frozen catalog) | `plugins/**` | `contract.TASK_WRITE_SCOPE[TASK_ID]`, recorded, not executed |
| effective (executed) | `plugins/` | `normalize_declared_path("plugins/**", workspace_root="/workspace/project")` |
| normaliser | `atlasos.ownership.paths.normalize_declared_path` | the same function the manifest uses |

Both strings, the normaliser and the workspace root are inside the cell, so the plan digest covers
them. Nothing else moved: the router, `src/atlasos/`, the profile, the scope, the task catalog, the
seed and the oracle are untouched, and no proposal is rewritten — the model still has to declare its
own paths, and those declarations are still matched against the grant.

**Before / after, same proposal, same router:**

| executed grant | declared paths | outcome |
| --- | --- | --- |
| `("plugins/**",)` — raw replay, still refused | `plugins/csv_plugin.py`, … | `direct` / `WRITE_SCOPE_NOT_GRANTED` / `GATE_M_PROPOSAL` |
| `("plugins/",)` — **the production configuration** | `plugins/csv_plugin.py`, … | `delegate_parallel` / `DELEGATION_ADMITTED`, both children admitted, wave width 2 |

The boundary is unchanged (same run, same context, five negative shapes): a shared write is
`SHARED_WRITE_RISK`, a declared dependency is `DEPENDENCY_NOT_SUPPORTED`, a write outside the grant
is `WRITE_SCOPE_NOT_GRANTED`, a path escape is `INVALID_PLAN`, and a research profile asking to
write is `WRITE_SCOPE_NOT_GRANTED`.

**What the runtime does and does not protect** (three layers, not one):

| layer | mechanism | covers |
| --- | --- | --- |
| runtime enforcement | ownership manifest + router grant | `test_public.py`, `docs/INTERFACE.md`, `README.md` — outside `plugins/`, denied |
| task instruction | the P2 prompt and `forbidden_paths` | `plugins/base.py`, which is *inside* `plugins/`; the runtime would accept a write |
| independent oracle | the hidden tests and the reference check | the same `plugins/base.py`, by comparison with the reference |

The correction did not change that split: `plugins/base.py` was inside the granted directory before
the correction (under the manifest's own normalisation) and is inside it after. Nothing in this stage
widens or narrows what the runtime would accept; only what the admission gate accepts moved.

Consequence for the pre-registered outcomes: with the correction, all five outcomes remain reachable
in principle, and A4 no longer depends on a gate that refuses every write. A4 remains a claim about
the mechanism being usable when asked, not about the Main choosing to delegate on its own.

## C. Minimal implementation, and what is reused

New, and all of it is experiment-layer code:

| file | role |
| --- | --- |
| `experiments/stage_c1/plan.py` | the frozen plan: one cell, the goal composition, one intervention, the plan record, the design invariants |
| `experiments/stage_c1/checks.py` | the 12 offline checks and the artefact builder (`python -m experiments.stage_c1.checks`) |
| `experiments/stage_c1/plan.json`, `authorization.json`, `executor.json`, `offline.json` | the frozen artefacts the Owner reviews |
| `tests/integration/test_stage_c1_positive_control.py` | 17 targeted tests over exactly those facts |

Reused without a copy: `experiments.stage_b_plus.contract` (tasks, arms, budget, exposure,
delegation config, public suite, acceptance/oracle contract), `authority` (pending record, signing,
verification, claims), `executor.dispatch` and `executor.live_cell_runner` (the production runner),
`executor.audit_cell` (the full audit), `executor.production_goal_options` (the single
`GoalRunOptions` builder used by both admission and the runner), `executor.run_admission_preflight`,
`closure2.catalog` (P2 seed, reference, oracle), `experiments.stage_b_plus.offline.synthetic_cell_run`
(for the offline control-flow proof only).

No second executor, no second budget system, no second evidence store, no second permission system.
`src/atlasos/` is unchanged.

No second matcher either: the grant correction calls
`atlasos.ownership.paths.normalize_declared_path`, the function the ownership manifest already uses,
rather than reproducing glob semantics.

Two seams in the B+ code, both of them lookups: `contract.scope_plan()` resolves the C1 plan from
`experiments.stage_c1.plan`, and `contract.child_grants_for(cell)` returns the cell's grant when the
cell carries one (every Stage B+ cell does not, so `Arm.grant_for` answers unchanged). Everything
else in `dispatch` and `production_goal_options` is already plan-agnostic.

## D. Exact positive-control goal

`contract.GOAL_OF["closure2-plugin-migration"]` (454 chars, byte for byte) + `"\n\n"` + this
appendix (705 chars, byte for byte):

> For this task, use coding workers to implement independently executable parts of the migration.
>
> Identify at least two independent implementation workstreams when the published interfaces and
> ownership boundaries permit it, and attempt to delegate them together before implementing those
> workstreams yourself.
>
> Choose the decomposition, writable scopes, worker inputs, and deliverables yourself using the
> available runtime capabilities.
>
> Respect all admission, ownership, and safety constraints. If no safe delegation is possible or the
> runtime rejects the proposal, explain the actual reason and continue appropriately.
>
> You remain responsible for integration, public testing, and final task correctness.

Total goal: 1161 chars. The Main system prompt is `main-v2.1-rc1-bplus`, unchanged. The goal carries
no worker assignment, no DAG, no writable-scope list, no reference implementation and no hidden test
name — `plan.validate()` greps every one of those frozen strings out of the goal, and the test suite
asserts it independently.

## E. Frozen digests

| item | value |
| --- | --- |
| plan id | `stage-c1-coding-delegation-positive-control-v1` (unchanged; no V2 id) |
| plan digest | `sha256:537508bf0fc10056987ef9342d9899008609aa9ba9c1734f7de53a3d3bc7ecd2` |
| cell | `c1.closure2-plugin-migration.C3.r1` (order 1, repetition 1, not a benchmark sample) |
| task / arm | `closure2-plugin-migration` / `C3` (coding_worker, exposure `card`) |
| goal digest | `sha256:1496055dfef64496f2efb570b835cbb0936a32bf67ba2df5b97a1e1e1d508e07` |
| intervention digest | `sha256:fad89af68cff6bf619401b838c55b2e5cfa62b1438b0d8b0e5656b7fd6ed47d8` |
| Main prompt digest | `sha256:76ebb03d51b7d4ac7a905261ed59496c5bc94b00f685132c7ad14be2722f3bca` (`main-v2.1-rc1-bplus`; literal P3 `sha256:4147f19c…` untouched) |
| budget digest | `sha256:a6ea43679649cb39c6c4f55f3bbb915b8a0287abcddce511a725ec6abee23eb9` (the frozen Stage B+ `BUDGET`) |
| execution contract | `bplus-exec-3.1` (`EXTERNAL_SANDBOX_ORACLE`, static acceptance, pre-claim preflight) |
| executor version | `bplus-executor-1.3` |
| executor digest | `sha256:c7f3c4c88a86c91be82792fb5da640f8a62e0922840fd9d492606ea8a279937d` |
| executed Host grant | `plugins/` (raw `plugins/**` recorded in the cell; see §B) |
| executor identity files | `stage_b_plus/{executor,authority,contract,cost}.py`, **`stage_c1/plan.py`**, `app/live_preflight.py`, `app/goal_verification.py` |
| B+ protocol digest (unchanged) | `sha256:65e0b993796191bf0b34ceb1c258ff9cbcd8bdbde98c4733fc39302cc9b05460` |
| fixture seed (P2, 11 files) | `sha256:cdc114a995318c7021016a08fff1cfb50b1d14d26f9d8f30ffa0ff12b53f8076` |
| capability card / disclosure | recomputed per run from the effective grant (names `plugins/`, no deliverable, no worker id) |
| run configuration | DeepSeek `deepseek-chat`, router `m.1.0`, 20/20/20 turns, 500 000 tokens, 2 concurrent children, 2 per call, 3 rounds, 4 public-test calls, OpenSandbox `atlas-uat-python:3.12`, cpu 1, 1Gi, network none, matrix concurrency 1 |
| candidate envelope (not approved) | token stop 500 000, cumulative cost stop USD 0.15, per-cell cost stop USD 0.15 |

What moved, and why: the plan digest moved when the cell gained the effective grant and the
normalisation record; the executor digest moved because `contract.py`, `executor.py` and
`experiments/stage_c1/plan.py` are in the signed identity and all three changed. Goal, intervention,
Main prompt, budget, protocol, task, arm, exposure, router and run configuration are byte-identical to
the blocked version, and the V3 executor digest `sha256:9f4c21cc…` stays recorded in its own dispatch
claim and in `executor.json`, not overwritten.

The digest is computed from file bytes, so every added identity file is stored with LF line endings
(`.gitattributes` `eol=lf`) and the worktree was normalised before the digest was taken: a clean
checkout of the commit below reproduces this value, which is what the Owner's signature binds.

## F. Offline verification

`python -m experiments.stage_c1.checks` → **12/12 pass**, exit 0, status
`STAGE_C1_POSITIVE_CONTROL_READY_FOR_LIVE_AUTHORIZATION`. Targeted tests: **17/17 pass**.

| check | what it proves |
| --- | --- |
| C01 | the P2 fixture matches the frozen seed over 11 files; `plugins/base.py` and `test_public.py` present; no hidden test in the workspace |
| C02 | the executed goal is the frozen requirement + one separator + the one appendix, in that order |
| C03 | the Main prompt is the Stage B+ prompt (`sha256:76ebb03d…`); literal P3 untouched |
| C04 | arm C3 offers `coding_worker` with the card, 20 effective turns, the effective grant `plugins/`, write tools, and a disclosure that names neither the raw glob, a deliverable nor a worker id |
| C05 | the production configuration admits the P2 pair (`DELEGATE_PARALLEL`, both children, wave width 2); the raw-glob replay is still refused (`WRITE_SCOPE_NOT_GRANTED`); shared write, declared dependency, out-of-grant write, path escape and research-write are all refused with their own codes |
| C06 | the real `dispatch()` + `audit_cell()` run one cell end to end on a synthetic result: 17 audit checks, no stop, verdict `PASS`, evidence files present |
| C07 | the plan digest recomputes from the cells, is commit-independent, and collides with no B+ plan |
| C08 | canary/formal plan digests and the B+ protocol digest unchanged; V1/V2 cannot be rebuilt; the three consumed claims are byte-identical before and after |
| C09 | no claim exists for the C1 plan digest; the production claim root is in force |
| C10 | the pending authorization authorizes nothing (11 stated refusals from `authority.verify`) |
| C11 | the plan's own design invariants hold |
| C12 | the pre-claim live preflight accepts the C1 cell's production configuration (credential reported separately; it was present on this machine) |

Ruff is clean on every added or edited file (`ruff check`, `ruff format --check`).

Regression on the shared surfaces: 201 tests pass across
`test_stage_b_plus_execution_contract`, `test_bplus_admission_contract`,
`test_bplus_evidence_contract`, `test_stage_b_plus_ablation`,
`test_stage_b_plus_capability_card`, `test_stage_m_delegation_router`, `test_delegation_routing`.

Known pre-existing failures, not introduced and not fixed here: two tests in
`tests/integration/test_stage_b_protocol.py` (`test_the_frozen_runtime_is_the_one_the_last_stage_froze`,
`test_the_freeze_records_what_the_protocol_says`) fail on the baseline `a55e106` as well — the frozen
Stage B runtime digest is `b3bbc6c1…`, the tree is `4eebcf1e…`, identical before and after this
stage. This is the Stage B freeze drift the brief said not to repair.

## G. Pending Owner authorization

`experiments/stage_c1/authorization.json`:

* `status` `PENDING_OWNER_APPROVAL`, `authorizes_live_execution` `false`;
* `cost_stop_usd` and `per_cell_cost_stop_usd` **null**; `token_stop` 500 000; `attempt_limit` 1;
* every signature field null; `approval_provenance` empty;
* `cell_ids` exactly `["c1.closure2-plugin-migration.C3.r1"]`;
* `proposed_envelope` carries the candidate 0.15 / 0.15 as a *proposal* (`approved: false`);
* `prelive_admission.status` = `ADMITTED`, with both grant representations, both outcomes, the
  correction record (design A: normaliser, workspace root, `router_modified: false`) and the five
  boundary codes;
* `review_surface.write_grant` repeats the raw/effective pair, so the Owner sees the representation
  change on the same page as the digests.

The two decisions that remain are the Owner's: approve the candidate envelope (token stop 500 000,
cost stops 0.15 / 0.15) and sign the record with an external provenance artefact. The grant
representation was decided in design A and is recorded; it is not a new authorization.

## H. Claim and historical evidence preservation

* V1, V2 and V3 claims are still present and byte-identical (`sha256:7ae71273…`, `sha256:41fc36da…`,
  `sha256:0b58ab25…` in their `.claim` files); C08 compares them before and after every run of the
  checks. Nothing migrated, nothing released.
* V1 and V2 remain unrebuildable (`scope_plan` raises).
* The C1 plan digest is distinct from all three, and no claim file for it exists.
* Stage B+ artefacts (`protocol.json`, `plan-canary.json`, `plan-formal.json`,
  `authorization-canary.json`, `authorization-formal.json`, `executor.json`, `offline.json`) were not
  rewritten; `python -m experiments.stage_b_plus.offline` was not run.
* V3 external evidence custody: `D:\Work\AtlasRuntime-runs\bplus-canary-3\evidence-index.json`,
  `sha256:660157b3f0d194dae4e4002ef4e9d9bcdaf593b0c7b1a231c2679dca97eaec13`, 476 files, plan
  `stage-b-plus-diagnostic-canary-v3` — re-hashed read-only and matched. Nothing was copied out of it.

## I. Exact future execution command

Not executable until the record in §G is signed. Once signed, a single live attempt is:

```python
import asyncio
from pathlib import Path
from experiments.stage_b_plus import executor

report = asyncio.run(
    executor.dispatch(
        "stage-c1-coding-delegation-positive-control-v1",
        runs_root=Path(r"D:\Work\AtlasRuntime-runs\stage-c1-positive-control"),
        evidence_root=Path(r"D:\Work\AtlasRuntime-runs\stage-c1-positive-control"),
        authority_path=Path(r"D:\Work\AtlasRuntime-authorizations\stage-c1-positive-control.json"),
        mode=executor.MODE_LIVE,
    )
)
```

Preconditions the command itself enforces: a clean worktree at the reviewed commit (so commit this
stage first), `ATLAS_BPLUS_CLAIM_ROOT` unset, the signed record naming that exact head, plan digest,
protocol digest, budget digest and executor digest, both cost stops positive, and a READY pre-claim
admission (which the dispatch writes before it takes the claim). The claim, once written, is spent;
retry and resume are forbidden by the executor itself.

## J. Known limitations

1. **The correction is a representation change, and it is the only one.** The frozen catalog still
   says `plugins/**`; the cell executes `plugins/`. A reader who compares the two strings without the
   normaliser will see a scope change that is not there. The record keeps both, and `plan.validate()`
   refuses a cell whose effective grant and raw grant are not the two representations of one scope.
2. **P2 is a mechanism diagnostic, not a real project.** Two plugins with different input formats and
   a frozen interface. Nothing here generalises to a large coupled codebase.
3. **The instruction is an activation, not an autonomy measurement.** A4 would prove the mechanism
   works when the user asks for delegation. It says nothing about whether the Main would delegate on
   its own; that is B0/B1.
4. **The worker cannot run tests.** `coding_worker` carries `bash`, but the ownership policy refuses
   every interpreter invocation, so a child cannot run `pytest`; the task catalog's "definition of
   done" text names a command the worker cannot execute. The Main verifies with
   `run_public_tests` (Host-only). Handoff quality, not worker self-verification, is what A3/A4 would
   measure.
5. **One worker or two?** If the run admits a single child, that is a single-worker closure and must
   be reported as such; the two-worker parallel mechanism is a separate claim.
6. **Offline control flow uses a synthetic cell.** C06 proves the loop and the audit, not a real
   session; the only real-cell evidence in this stage is zero.
7. **The claim evidence is machine-local.** The three claim files live outside the repository; on a
   machine that does not hold them C08 proves "unchanged", not "present".
8. **`EXECUTOR_FILES` now includes a stage-C file.** A Stage B+ re-run on this branch would report
   the same executor version `1.3` with the C1 plan file in its identity; Stage B+ plans never read
   it, but the digest is shared.
