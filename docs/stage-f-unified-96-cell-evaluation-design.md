# Stage F — Unified 96-cell Formal Evaluation: Design Review

**Status:** `STAGE_F_UNIFIED_96_CELL_EVALUATION_DESIGN_READY_FOR_OWNER_REVIEW`

Baseline `feat/stage-c2-natural-delegation` @ `63fdebfc334a87152e14044613158dcd6cb1647b`,
executor `bplus-executor-1.7` / `sha256:e7d7bf87…`, C2 plan `sha256:d0a4c212…`, C2 evidence index
`sha256:200fdc0d…`. Seven production claims (B+ v1/v2/v3, C1, C1-B, C1-C, C2) stay consumed and
untouched; C1/C1-B/C1-C/C2 are never re-run.

This document designs the evaluation. It runs nothing: no model call, no probe, no claim, no batch.
Every number about the runtime below is read from source in this checkout, with `file:line`.

Sections A–L are the requested review; the appendix is the preparation-gate list that must be
implemented before the Owner can authorize a live matrix.

---

## A. Current evidence boundary

What the repository can prove today, and what it cannot.

| Claim | Status | Evidence |
|---|---|---|
| Instructed dynamic coding delegation works end to end | **PROVEN, L1–L5** | C1 (A2), C1-B (A3), C1-C (A3): proposal → router admission → 2 workers → completion reports → Main integration → public PASS → normal finish |
| Shared 750K budget is not the binding constraint | **PROVEN** | C1-C: 352,919 / 750,000 used, zero rejections, Main normal finish |
| Independent oracle can fail a run that the public suite passed | **PROVEN** | C1-C: public PASS, oracle `HIDDEN_TESTS_FAILED`; C2 c03: static PASS, oracle FAIL |
| A4 (delegated task that is *also* correct) | **NOT OBTAINED** | P2 has three runs, all oracle FAIL; C1/C1-B/C1-C never reached A4 |
| Natural (uninstructed) delegation | **NEGATIVE** | C2: 4/4 cells Direct, 0 proposals; the one failure was a tool-policy loop, not a routing decision |
| Context Engineering end-to-end benefit | **NOT PROVEN** | Only mechanism-level results (dedup neutral, 64/128 retrieval); no arm comparison |
| Multi-agent / static-DAG benefit | **NOT PROVEN** | No A1-vs-A2-vs-A3 comparison exists at any scale |
| Native AnyCode baseline | **PARTIALLY MEASURED** | Closure-1B defines A0 (`docs/closure-1b-native-baseline.md`) and exercised it in scripted/acceptance shape; no budget-equivalent run and no multi-task matrix |

Two consequences for this design:

1. The matrix must be able to return **"delegation does not pay"** as a valid result. Nothing in the
   engineering may make A3 win by construction (the failure mode the brief names as "不得故意采用错误
   分解制造 A3 优势" applies symmetrically to A1/A2 resource caps).
2. Because *no* arm has yet produced a correct delegated delivery on a task where the single agent
   also fails, the matrix's most informative cells are the hard families (X, C), not the two small
   ones that are already known to be solvable directly in 8–15 turns.

---

## B. Eight-task inventory and selection

Every runnable fixture in the tree was inventoried. Real counts:

| Catalog | Tasks | Coding? | Public test file | Independent oracle | Reference impl. | Sandboxed oracle wired |
|---|---|---|---|---|---|---|
| `experiments/evaluation/closure2/catalog.py` | 3 (`pipeline-migration`, `plugin-migration`, `state-machine-refactor`) | yes | yes | yes (`oracle_tests`) | **yes** | **yes** |
| `experiments/evaluation/final4/validation_tasks.py` | 3 (serial / parallel / coupled) | yes, tiny | yes | yes | yes | yes |
| `experiments/evaluation/stage_e_tasks.py` | 12 ×2 arms | yes (3–6 stub modules) | no separate public file | yes (`hidden_files`) | **no** | compatible, not declared |
| `experiments/evaluation/stage_r05_tasks.py` | 6 ×2 arms | yes (2–3 modules) | contract in docstring | yes | **no** | compatible, not declared |
| `experiments/evaluation/tasks.py` | 30 (`bugfix-*`, `feature-*`, `multiworker-*`, `longcontext-*`, `tool-*`) | 25 coding, 5 analysis | yes | yes (`hidden_files`) | **no** | compatible, not declared |
| `experiments/evaluation/final3/` | — (measurement scripts, not tasks) | — | — | — | — | — |
| `experiments/evaluation/stage_r1_2_business.py` | 1 synthetic business artifact | no code seed | `verification` spec | verification, not pytest | example artifact | **no** |

Facts that shape the selection:

- Only **closure2 (3)** and **final4 (3)** carry a reference implementation *and* a feasibility proof
  that the seed fails and the reference passes. Everything else has a hidden oracle but **no proof
  the oracle is satisfiable**, so it cannot enter a formal matrix without that proof.
- `final4/validation_*` are contract-validation stubs (5–10 seed lines), not tasks.
- `stage_e` / `stage_r05` / `tasks.py` fixtures are real code but single-purpose: they exist to test
  routing and ownership, with 1–5 line stubs — too small to carry a 40-turn arm.
- **No fixture in the repository has an injectable clock** (grep for `sleep|monotonic|tick(` over all
  catalogs: zero hits). The C2 family needs one, so it must be built.

### The eight, by family

| # | Family | Task id | Source | Verdict |
|---|---|---|---|---|
| S1 | S — small | `closure2-pipeline-migration` | closure2 | **REUSE** |
| S2 | S — small | `closure2-state-machine-refactor` | closure2 | **REUSE** |
| P1 | P — parallel implementation | `f-p1-connector-migration` | new fixture, pipeline skeleton | **ADAPT** |
| P2 | P — parallel implementation | `closure2-plugin-migration` | closure2 | **REUSE** (role: two protocol adapters) |
| X1 | X — cross-cutting | `f-x1-api-compat-audit` | new | **NEW** |
| X2 | X — cross-cutting | `f-x2-distributed-diagnosis` | new | **NEW** |
| C1 | C — complex lifecycle | `f-c1-config-lifecycle` | new (reuses `stage_e e1-01-config` module split as a skeleton) | **NEW** |
| C2 | C — complex lifecycle | `f-c2-scheduler-retry` | new (reuses N1's state-table shape + `bugfix-004-retry` semantics) | **NEW** |

Result: **3 reuse, 1 adapt, 4 new**. The four new ones are the minimum that the families require; no
existing fixture covers "audit N call sites and repair", "diagnose a multi-component failure from
logs and history", "configuration precedence/migration", or "retry/cancel/timeout with a clock".

Why the two small tasks stay in the matrix even though C2 showed both arms solve them directly: they
are the only pair with a measured baseline (C2: 15/10 turns, $0.008/$0.005, oracle PASS on 3 of 4
cells) and they give the matrix a **known-solvable floor**. An arm that cannot solve S1/S2 has a
mechanism defect, not a capability gap — that is worth one family of 24 cells.

---

## C. The eight tasks: requirement, interface, dependencies, oracle

For reused tasks the numbers are read from the catalog; for new ones this is the build specification,
not a claim that they exist.

### S1 `closure2-pipeline-migration` — REUSE

- **Requirement:** migrate a 6-module ingest pipeline from v1 to a frozen v2 contract published as a
  workspace file (`docs/INTERFACE.md`); v1's `build_report` skips normalisation.
- **Scale:** 10 seed files / 160 lines; reference 71 lines; 6 deliverables.
- **Dependencies:** `parse` → `normalise` (data), `aggregate` → `store`, `integrate` depends on both.
- **Independent pair:** `parse` + `aggregate` (disjoint paths, no dependency) — C2 verified.
- **Oracle:** 11 hidden test functions in a separate sandbox; captures the missing normalisation, the
  `count` aggregation change, ordering, `ValueError` cases, and the report render contract.
- **Static DAG (A2):** 3 nodes, `parse` ‖ `aggregate` → `integrate`.

### S2 `closure2-state-machine-refactor` — REUSE

- **Requirement:** one lifecycle state table plus three callers; v1 lacks `rejected`/`cancelled`, uses
  `KeyError` where the contract says `ValueError`, and two transitions do not exist.
- **Scale:** 8 files / 105 lines; reference 50; 4 deliverables.
- **Coupling:** both candidate steps write `lifecycle/state.py` — the negative control in C2.
- **Oracle:** 7 hidden tests.
- **Static DAG (A2):** 2 nodes, **sequential** (`state` → `callers`). Deliberately not parallel.

### P1 `f-p1-connector-migration` — ADAPT

- **Requirement:** three input connectors (csv file, newline JSON, a key/value feed) must be migrated
  behind one frozen `sources/` interface, with independent parse/validate/convert logic per source
  and a shared normalisation + registry step that composes them.
- **Scale target:** seed 12–14 files / 200–260 lines; reference 90–120 lines; 7–8 deliverables.
- **Independent pair:** two connectors (`sources/csv.py`, `sources/jsonl.py`) — disjoint files,
  published `sources/base.py` protocol, no cross-call.
- **Integration seam left to the agent:** `sources/registry.py` + `pipeline/merge.py`.
- **Oracle:** ≥12 hidden tests, of which ≥4 per connector (quoting/blank lines/duplicate keys), ≥2 on
  merge precedence, ≥2 on error typing. Reference must pass; seed must fail.
- **Static DAG (A2):** 4 nodes — `csv` ‖ `jsonl` → `kv` → `merge`. Never more than two concurrent
  workers (the frozen cap is 2): the first pair runs together, the third connector joins after them,
  and the integration node closes. The three connectors are independent by interface, so this is a
  legal decomposition, not a manufactured one.
- **Anti-rigging note:** the three sources are independent *by construction of the interface*, not by
  withholding a shared file the direct agent would need.

### P2 `closure2-plugin-migration` — REUSE (role: protocol adapter upgrade)

- **Requirement:** two adapters (`csv`, `json`) upgraded to the frozen v2 plugin protocol, plus
  registry/loader dispatch.
- **Scale:** 11 seed files / ~110 lines; reference 6 files; 3 workers (`csv`, `json`, `register`).
- **Oracle:** hidden plugin suite; covers error typing, ordering, loader registration.
- **Static DAG (A2):** 3 nodes, `csv` ‖ `json` → `register`.

### X1 `f-x1-api-compat-audit` — NEW

- **Requirement:** a frozen `core/api.py` signature changed (v1 → v2); five subsystem call sites still
  use v1 shapes. The agent must find every call site, classify real vs benign mismatches, repair the
  real ones, and leave the benign ones alone. Public suite covers 3 of the 5 call sites.
- **Scale target:** seed 15–18 files / 300–380 lines across 4 subsystems; reference 60–90 lines of
  edits; 5–7 changed files.
- **Why delegation is plausible:** the audit is a genuine read-heavy investigation (≈4 subsystems ×
  ≈40 lines), and the repairs are in disjoint files.
- **Independent pair:** `billing/` and `reports/` call sites (disjoint packages).
- **Oracle:** ≥10 hidden tests, of which ≥3 exercise a call site the public suite does **not** cover
  (that is the point: a public-only repair fails), plus ≥2 asserting that a benign site was **not**
  changed.
- **Static DAG (A2):** `audit-core` → {`billing`, `reports`} → `verify`.
- **Fairness:** the "do not change benign sites" assertion is published in `docs/INTERFACE.md`, so it
  is a stated requirement, not a hidden trap.

### X2 `f-x2-distributed-diagnosis` — NEW

- **Requirement:** a three-component local system (producer → queue → worker) drops messages under a
  specific interleaving; the repository carries logs, a failure record and a history file. The agent
  must reproduce the fault locally, fix it, and keep the fix from breaking the normal path.
- **Scale target:** seed 16–20 files / 350–450 lines including fixtures/log files; reference 40–70
  lines of fix; 2–4 changed files.
- **Determinism:** all components run in-process; the fault is driven by an ordered event script, no
  real concurrency or sleep.
- **Independent workstreams:** log/state forensics (read-only) ‖ reproduction harness (write), then a
  shared repair.
- **Oracle:** ≥10 hidden tests: the fault must be gone, throughput/order preserved, and the failure
  record must still be reproducible from the seed when the fix is reverted.
- **Static DAG (A2):** `forensics` (read-only) ‖ `repro` → `repair` → `verify`.

### C1 `f-c1-config-lifecycle` — NEW

- **Requirement:** a configuration stack with schema, layered loader, validator, merge (precedence:
  explicit > env > file > default) and migration from v1 to v2 keys. Existing code applies precedence
  in the wrong order, ignores unknown keys, and has no migration path.
- **Scale target:** seed 14–16 files / 320–420 lines; reference 100–140 lines; 6–8 changed files.
- **Coupling:** merge and migration share the schema table — this is the family's *hard-coupling*
  control, and the natural Direct case.
- **Oracle:** ≥14 hidden tests, of which ≥4 on precedence permutations, ≥3 on migration of a v1 file,
  ≥2 on unknown-key rejection.
- **Static DAG (A2):** 2 nodes, `schema+migration` → `loader+merge` (sequential; the shared schema table
  makes a parallel split illegal, which is the honest DAG).

### C2 `f-c2-scheduler-retry` — NEW

- **Requirement:** a job scheduler with transitions (queued → running → done/failed/cancelled),
  retry with backoff, cancellation, timeout and idempotent re-delivery, driven by an **injected
  clock**. The seed ignores cancellation during retry, retries non-idempotent deliveries, and has no
  timeout.
- **Scale target:** seed 15–18 files / 340–440 lines; reference 110–150 lines; 7–9 changed files.
- **Determinism:** `clock: Callable[[], float]` is part of the frozen interface; tests advance it
  explicitly. No `sleep`, no wall-clock dependence.
- **Oracle:** ≥14 hidden tests: cancellation during backoff, timeout boundary (exactly at limit and
  one tick past), duplicate delivery idempotency, retry exhaustion, and state-table completeness.
- **Static DAG (A2):** `state-machine` ‖ `retry-policy` → `scheduler` → `verify`; the two halves are
  disjoint files that agree on the frozen transition table, which is a legitimate parallel split.

Every new task must also ship (all are qualification inputs, see §H): frozen seed, `docs/INTERFACE.md`,
`test_public.py`, hidden oracle, reference implementation, allowed/forbidden paths, and the four
digests.

---

## D. The four arms: real source entries and their differences

| | A0 | A1 | A2 | A3 |
|---|---|---|---|---|
| Entry | `experiments/evaluation/closure1b/runner_entry.py::run_unit` | `executor.dispatch` → `live_cell_runner` → `main_driven.run_goal` | `executor.dispatch` → new adapter → `evaluation/runner.py::run_sample` | same as A1 |
| Agent topology | AnyCode `Orchestrator` + `NeutralEngine`, one agent (one or more per task, no main) | one Main + delegation plane | static `TaskSpec` DAG from `WorkerStep.depends_on`, sink = integration node | one Main + router |
| Tool surface | 6 neutral sandbox tools (`final4/neutral_adapter.py:316-411`) | Atlas tool runtime + `delegate_tasks`/`delegate.read` | same `AtlasToolRuntime` as A1 (`runner.py:2095-2115`) | same as A1 |
| Context plane | **none** | `AtlasContextEngine` | `AtlasContextEngine` | `AtlasContextEngine` |
| Ownership / action gate | **none** (anycode `ToolSecurityPolicy` only) | same gate as A3 | same modules, different policy config; closure2 froze `ownership_mode="off"` | same as A1 |
| Budget | **no guard** (`budget_mode="legacy"`, post-hoc `_post_hoc_budget`) | one `RunBudgetState` per run | one `RunBudgetState` shared by every worker (`runner.py:1898-1902, 2303`) | same as A1 |
| Oracle | `sandboxed_oracle` after export | same | same | same |

### A0 — native, real entry

`run_unit(task_id, configuration, *, runs_root, repetition_id=0)` (`runner_entry.py:133`). It does not
build its own loop: `run_native_sample(...)` (`native_baseline.py`) constructs `NeutralEngine(AnyCode)`
with the 6-tool backend and runs `engine.run_tasks`. The provider path is real DeepSeek; the sandbox is
real OpenSandbox and **refused if not real** (`runner_entry.py:111-129`, `native_baseline.py:236-240`).

Absent, explicitly (`docs/closure-1b-native-baseline.md:52-54`): context plane, `ScopedWorker`,
`ActionSafetyGate`, durable ledger, progressive disclosure, delegation router, **shared budget guard**,
binding store, ownership lock, recovery.

Consequence for Stage F: A0 is the only arm whose *architecture* differs wholesale, which is exactly
what the A0↔A1 comparison is for — and also why A0 needs a budget seam before that comparison means
anything (see §E).

### A1 — AtlasRuntime Direct-only, real entry

`main_driven.run_goal(options)` with `options.delegation = DelegationConfig(enabled=False)`. The
ablation is already implemented and clean:

```665:668:src/atlasos/app/main_driven.py
    # Ablation: with delegation disabled neither tool exists, so the Main cannot delegate at all
    delegation_tools = (
        [delegate_tool.definition(), reader.tool_definition()] if delegation_config.enabled else []
    )
```

So A1 does not "have a disabled tool the Main keeps trying" — the tool is **absent from the surface**,
which is the property the brief asks for. Everything else (context plane, ownership, action gate,
budget, public tests, completion discipline) is identical to A3.

### A3 — dynamic selective delegation, real entry

Same entry as A1 with `DelegationConfig(enabled=True, max_parallel_children=2,
max_children_per_call=2, max_delegation_rounds=3, child_max_turns=20, result_preview_chars=900)`
(`contract.delegation_config()`), router `m.1.0` unchanged, worker profiles `research_worker` +
`coding_worker` with `card` exposure, worker completion contract `c1b-1.0`.

**A1/A3 prompt parity.** Both carry `main-v3.0-rc1-coding-aware` verbatim as the cell's `main_prompt`
(the mechanism C2 built: the prompt travels in the cell and is covered by the plan digest). The only
A1/A3 difference is `delegation.enabled`, i.e. the tool surface. No prompt fork, no separate text.

### A2 — static DAG, real entry

`experiments/evaluation/runner.py::run_sample` compiles `BenchmarkTask.workers` (`WorkerStep` with
`depends_on`) into AnyCode `TaskSpec`s (`runner.py:2228-2236`), runs them through
`AtlasContextEngine.run_tasks` (`runner.py:2342`), with one shared sandbox binding
(`runner.py:2161-2166`) and one shared `RunBudgetState` (`runner.py:1898-1902`).

Three facts the design must accept and disclose:

1. **There is no Main.** The "integration step" is an ordinary sink node with `depends_on`; nothing
   enforces a single final node on this path (`workflow.compile_workflow`'s unique-sink rule is not
   used here). Consequence: every "Main Peak Context / Main cumulative input / natural proposal"
   metric is **NOT_APPLICABLE** for A2, not zero.
2. **The decomposition is a human prior.** It is authored per task, frozen into the plan, and
   disclosed in the report. `owned_paths` exists in the catalog's worker dicts but is **not** carried
   by `WorkerStep`; ownership on this path is configured `off` in the closure-2 pilot profile
   (`pilot_runner.py:842`). Stage F must either carry ownership structurally or state that A2 has no
   write-scope enforcement — see §E.
3. **Evidence shape differs.** `run_sample` writes `result.json` / `stage_e_evidence.json` /
   `workspace.patch`, **no `session.json`**; `executor.audit_cell` requires a main-driven
   `session.json` and `REQUIRED_USAGE_FIELDS` with a main/child split (`executor.py:988-1025`,
   `136-142`). An adapter is required or every A2 cell is STOPPED as `MODE_VIOLATION`.

---

## E. Unified resource contract and its feasibility

### The contract

| Parameter | Value | Where it must take effect |
|---|---|---|
| provider / model | DeepSeek / `deepseek-chat` | `RunnerConfig.llm_provider`, `contract.BUDGET["model"]` |
| Main / single-agent max turns | 40 | `RunnerConfig.main_max_turns` (A1/A3), native `MAX_TURN_PER_WORKER` (A0) |
| Worker max turns | 20 | `contract.BUDGET["worker_max_turns"]` (A1/A3 children), `WorkerStep.max_turns` (A2) |
| Static integration max turns | 40 | the integration node's `max_turns` (A2) |
| Shared system token budget / cell | 1,000,000 | cell `budget.token_budget` → `contract.token_budget_for(cell)` |
| Max concurrent / per call | 2 / 2 | `DelegationConfig` (A3 only) |
| Max delegation rounds | 3 | as above; reported for A2 as "sequential waves" |
| Sandbox | `atlas-uat-python:3.12`, 1 CPU, 1 GiB, network none, 30 min | `SandboxPlan` / `OpenSandboxOptions` |
| Matrix concurrency | 1 | dispatch loop |
| Attempt limit | 1 | claim |
| Per-cell cost stop | USD 1.00 candidate | `executor.cell_cost` + stop |
| Total / batch cost stop | USD 96.00 / 32.00 candidate | dispatch aggregate |

The global `contract.BUDGET` stays at 500,000 / 20 turns and is **not** edited; every Stage F cell
carries its own `budget` block, which `contract.budget_for(cell)` already honours. This is the same
mechanism C2 used for 750K, so no runtime change is needed for A1/A3 tokens or turns.

### Feasibility per arm

**A1 / A3 — READY.** Cell budget → `GoalRunOptions.token_budget` → `RunBudgetState(limit_tokens=…)`
(`executor.py:1762-1765`, `main_driven.py`). `main_max_turns=40` via `RunnerConfig`. Guarded accounting,
pre-admission rejection, post-run cost stop already exercised by three stages.

**A2 — READY WITH AN ADAPTER.** `run_sample(settings.token_budget=1_000_000)` creates one shared
`RunBudgetState`; every worker gets a `RunBudgetHook` sharing it (`anycode_engine.py:112-123`). Guarded
rejection exists on this path. Two gaps:

- the **usage split** — `RunBudgetState` yields a total, `audit_cell` wants
  `main_cumulative_input_tokens` / `child_total_prompt_tokens`; an adapter must attribute "all workers"
  to `child_*` and set `main_* = 0` **and** declare `usage_attribution: "dag_all_children"` so the
  analysis layer refuses to compute Main metrics;
- the **session shape** — the adapter must emit a `session.json`-compatible record (or a
  `cell_audit` variant) with `status`, `failure.kind`, `public_tests`, `sandbox_plan`, `oracle`,
  `export`.

**A0 — NOT READY; this is the one real comparability risk.** Today the native path has:

- no request admission: token usage is judged **post hoc** (`native_baseline.py:579-606`,
  `budget_enforcement: "post_hoc"`, `budget_mode="legacy"` at `runner_entry.py:86`);
- a hard-coded `TOKEN_BUDGET = 120_000` inherited from final4 (`final4/runner_entry.py:61`), which
  would mark almost every 1M-cell `BUDGET_EXCEEDED` post hoc;
- **no per-request usage records** (`request_evidence=False`, `runner_entry.py:95`; only per-agent
  aggregates at `native_baseline.py:437-452`), so "per-request input tokens / context growth" would be
  NOT_APPLICABLE for A0 while available for A1–A3.

Three options, in order of preference:

1. **Neutral budget hook (recommended).** `NeutralEngine.build_agent` currently passes no hooks
   (`final4/neutral_adapter.py:426-435`). `atlasos.budget` depends only on the anycode tokenizer and
   types (`budget.py:28-36`) — it is *not* a context/delegation/ledger mechanism. Inject a
   `RunBudgetHook(RunBudgetState(limit_tokens=1_000_000))` from the **closure1b side** (subclass or a
   `hooks=` parameter) so `neutral_adapter.py` keeps its zero-`atlasos`-import neutrality, and flip
   `budget_mode` to `guarded` so the record stops claiming `legacy`.
   *Residual difference to disclose:* pre-admission estimates, not exact pre-spend.
2. **Metering adapter in the provider factory** (`native_baseline.py:249-255`), rejecting the next
   request after the limit. Cheaper, but live-mode only and it can overshoot by one request.
3. **No guard, reported as `NOT_COMPARABLE` on budget.** Legitimate but weak: the A0↔A1 comparison is
   the matrix's headline, and an unbounded A0 would be a strawman in A0's favour on cost and against
   it on tokens.

Recommendation: **option 1, plus `request_evidence=True`** so A0's per-request series is measured the
same way. Both changes are metering, not optimisation — they add no context plane, no router, no
ownership, and cannot make A0 smarter.

### Fairness rules carried forward from Closure-2

`docs/closure-2-fairness-protocol.md` already states the four rules Stage F needs: identical model and
resource envelope; **budget semantics are not equivalent and are reported separately** (§4); known
differences are listed rather than removed (§5); claims are made per-arm. Stage F adds one: the
`usage_attribution` tag, so no metric can be silently compared across attributions.

---

## F. 96-cell matrix and balanced order

8 tasks × 4 arms × 3 repetitions = 96 cells, as three 32-cell batches (one batch = all 8 tasks × 4 arms,
one repetition).

Sequence pool:

```
Q1: A0, A1, A3, A2      Q2: A1, A2, A0, A3
Q3: A2, A3, A1, A0      Q4: A3, A0, A2, A1
```

Order generation (deterministic, seeded, computed once and frozen into both the manifest and each batch
plan):

1. `task_order = shuffle(task_ids, seed=DEFAULT_SEED + 0)` — one global task order, reused by all
   batches so that batch *b* runs tasks in `rotate(task_order, b * 3)`.
2. For batch *b* (0..2) and task position *p* (0..7) in that batch's task order:
   `sequence = QUATERNIONS[(p + b) mod 4]`.
   - Within a batch, `p` runs 0..7 → each quaternion is used **exactly twice**;
   - across batches, a given task sees three different quaternions (`p`, `p+1`, `p+2` mod 4), so
     position-in-batch and arm position are not confounded with repetition.
3. Cells inside a task run in the quaternion order; a cell never starts before the previous cell's
   claim is settled and its evidence is written (matrix concurrency 1).

**Honest limitation:** with three repetitions and four sequences, no task can visit all four
quaternions. The design is an incomplete counterbalance, not a Latin square; the residual position
effect is bounded to "which sequence a task never sees", and it is reported, not assumed away.

Binding: the generated order, the quaternion assignment and the seed are written into the master
manifest **and** into each batch plan; a batch whose order does not recompute from the manifest's seed
fails the plan check.

Every cell gets a fresh sandbox, a fresh workspace from the frozen seed, a fresh run directory and its
own evidence. Nothing crosses cells. Provider-side prompt cache may persist across requests; the design
does **not** claim cold-cache independence and reports cache hit/miss per cell as measured.

---

## G. Three-batch freeze, claim, and stop policy

| | Batch 1 | Batch 2 | Batch 3 |
|---|---|---|---|
| Cells | 32 | 32 | 32 |
| Plan id | `stage-f-96-b1` | `stage-f-96-b2` | `stage-f-96-b3` |
| Plan digest | own | own | own |
| Claim | own one-shot | own one-shot | own one-shot |
| Token capacity | 32,000,000 | 32,000,000 | 32,000,000 |
| Cost stop (candidate) | USD 32.00 | USD 32.00 | USD 32.00 |

Master manifest `stage-f-96-manifest-v1` binds all three plan digests, the task catalog digest, the
arm/prompt/grant digests, the order seed and the three claims' identities **before the first live
dispatch**. Consequences, stated as rules:

- A batch plan cannot be regenerated after any sibling has run: the manifest's digest covers it.
- No batch may be re-run; a consumed claim stays consumed.
- A result from batch 1 may not change batch 2's or 3's configuration. The only permitted reaction is
  the frozen stop policy.
- Cost/token stops are checked post-run and by guarded accounting, as inherited; they are not a
  real-time hard ceiling, and the report says so.

Stop semantics are the existing ones (`executor.py`): correctness failure is a **measured result** and
the matrix continues; infrastructure failure, evidence-integrity failure, an unclassified stop, or a
cost/token stop ends the batch with the remaining cells recorded `NOT_RUN`. Nothing is retried,
resumed, re-run or replaced.

---

## H. Task qualification plan

Nine checks per task; all executable offline before any live cell.

| # | Check | How it is decided |
|---|---|---|
| 1 | Seed and reference are distinguishable | run public suite + oracle on the seed (expect fail) and on the reference (expect pass); record both verdicts |
| 2 | Reference public PASS | `prepare_fixture(reference)` → public suite in sandbox |
| 3 | Reference independent Oracle PASS | `sandboxed_oracle.run_hidden_tests_in_sandbox` on the reference workspace |
| 4 | Oracle captures the key defects | per-defect mutation: revert one behaviour of the reference and confirm the oracle fails |
| 5 | Every oracle check has a public basis | each hidden test names the `docs/INTERFACE.md` clause it enforces; a test with no clause is removed |
| 6 | Fixture leaks nothing | seed contains no hidden test file, no reference file, no answer text; digest-checked |
| 7 | All four arms get the same bytes | one fixture digest per task, materialised once and reused; `main_prompt` never carries task text |
| 8 | Direct has a real path | reference is achievable without delegation and within 40 turns — evidenced by an A1 dry run or by the reference's size, stated either way |
| 9 | Static DAG is legal | every node's write set is disjoint from its concurrent siblings; every dependency is real; a DAG that would deadlock or double-write is rejected by the plan check |

Checks 1–4 and 6 are automated in the task factory; 5 and 9 are reviewed records in the plan
(`oracle_basis`, `static_dag`); 7 and 8 are plan-level invariants.

---

## I. Metrics and analysis contract

### Attribution first

Every normalized cell carries `usage_attribution ∈ {single_agent, main_child, dag_all_children}`.
The analysis layer refuses to compute a Main-scoped metric unless the attribution is `main_child`. This
is the mechanism that prevents "A2 has Main peak context 0" from ever entering a table.

### Primary

| Metric | A0 | A1 | A2 | A3 |
|---|---|---|---|---|
| Independent Oracle pass | ✓ | ✓ | ✓ | ✓ |
| Delivered success (oracle ∧ delivered ∧ integrity) | ✓ | ✓ | ✓ | ✓ |
| Primary/Main completion | single agent | Main | **NOT_APPLICABLE** | Main |
| Execution integrity | ✓ (sandbox lifecycle + export) | ✓ | ✓ | ✓ |
| Main peak context | single-agent peak | Main peak | **N/A** | Main peak |
| Main cumulative input | single-agent total | Main total | **N/A** | Main total |
| System total tokens | ✓ | ✓ | ✓ | ✓ |
| Estimated cost | ✓ | ✓ | ✓ | ✓ |
| Wall-clock | ✓ | ✓ | ✓ | ✓ |

### Context, delegation, resource, reliability

- **Context:** per-request input series, context growth, max context, repeated reads, handoff inline
  size, full reference reads, tool-result offload, compaction events. A0 has the series only after
  `request_evidence=True`; A2's "per-request" is per-worker.
- **Delegation:** natural proposal, first proposal turn, Direct decision + visible reason, router route
  and reason codes, admission/start/completion per child, observed concurrency and overlap, valid
  completion reports, handoff preview / full read / shared-artifact consumption, Main continuation. All
  **NOT_APPLICABLE** for A0 and A2 (A2's DAG is pre-declared, so "proposal" has no meaning).
- **Resource:** prompt/completion, cache hit/miss, main/child/system attribution, budget accounting,
  agent duration, sandbox provisioning/export/oracle/cleanup durations.
- **Reliability:** ownership refusals, repeated-refusal loops, budget rejections, max-turn stops,
  failure classification, unknown failures, isolation/integrity violations.

Correctness is the audit's independent oracle only. `reconstruct`'s `verification.oracle` /
`L6_goal_correctness` is excluded by name (the C2 reporting contract already encodes this).

### Analysis

Pair on (task, repetition): 24 cells per arm, 6 per arm per family, 3 per arm per task. Report all 96
raw rows, then success counts and denominators, medians and interquartile ranges, and paired resource
deltas for the pairs where **both** arms ran. Resource tables are reported twice: over all runs, and
over successful runs — never only the latter.

Four comparisons, each with its own claim limit:

| Comparison | What it can claim |
|---|---|
| A0 vs A1 | the overall architecture difference (context plane + ownership + ledger + guarded budget **as a bundle**) — explicitly **not** a context-plane ablation |
| A1 vs A2 | the value of a *human-authored* structure when it is correct |
| A1 vs A3 | the value of letting the agent decide — the median case is expected to be ≈0 |
| A2 vs A3 | human prior vs autonomous planning, including A3's wrong-decomposition cost |

A3 is reported twice: all 24 cells, and the subset where a delegation actually happened. A Direct A3
cell is not a failure; it is the treatment working as designed on that task.

Not permitted, and stated in the report template: excluding failed runs to manufacture a resource win;
calling 96 runs "96 independent tasks"; calling A0↔A1 a single-mechanism attribution; reading one
sequence's difference as a causal effect.

---

## J. Minimal engineering change list

Ordered, smallest first. Nothing here touches router, context plane, budget runtime, worker contract or
ownership policy. No second benchmark framework: everything reuses `dispatcher`, `audit_cell`,
`prepare_fixture`, `sandboxed_oracle`, `authority`, and the existing plan/claim machinery.

| # | Change | Files | Size | Why it is unavoidable |
|---|---|---|---|---|
| 1 | `execution_mode` on the cell (`native` / `atlas_direct` / `atlas_static_dag` / `atlas_dynamic`) + `cell_runner_for(cell)` in dispatch, defaulting to `live_cell_runner()` | `experiments/stage_b_plus/executor.py` (`dispatch`), new `experiments/stage_f/arms.py` | ~120 lines | one dispatcher must run four topologies; `cell_runner` is already a parameter |
| 2 | Session normaliser: `native result → session shape` and `dag result → session shape`, with `usage_attribution` | new `experiments/stage_f/normalise.py` | ~250 lines | `audit_cell` needs `session.json`; without it A0/A2 cells stop as `MODE_VIOLATION` |
| 3 | A0 metering: `hooks=[RunBudgetHook(...)]` injected from the closure1b side + `request_evidence=True` + 1,000,000 limit constant | `experiments/evaluation/closure1b/native_baseline.py`, `runner_entry.py`, `final4/neutral_adapter.py` (accept `hooks=`) | ~60 lines | otherwise A0 is unbounded, post-hoc judged, and has no per-request series |
| 4 | A2 adapter: `run_sample(...)` wrapper binding `worker_steps`, shared budget, sandbox, oracle, and a session-shaped record | new `experiments/stage_f/static_dag.py` | ~200 lines | no per-cell evidence format exists on that path |
| 5 | Task catalog: 4 new fixtures + one registry module; reuse `BenchmarkTask`, `prepare_fixture`, `closure2.Task` shape | new `experiments/stage_f/tasks/*.py` | 4 × ~250 lines + fixtures | the four families have no fixture |
| 6 | Task qualification harness (9 checks, table in §H) | new `experiments/stage_f/qualify.py` | ~300 lines | the matrix cannot start on unqualified tasks |
| 7 | Batch plan builder: order generation, quaternion assignment, digests, manifest binding | new `experiments/stage_f/plan.py` | ~250 lines | three plans + one manifest, deterministic and re-derivable |
| 8 | Analysis layer: 96-row table, paired deltas, per-family, attribution guards | new `experiments/stage_f/analyze.py` | ~350 lines | the existing analyzer has the L6 trap and no arm dimension |
| 9 | Preparation gate script (appendix) | new `experiments/stage_f/preflight.py` | ~300 lines | the C1-B lesson: components green, entry unresolved |

Explicitly **not** changed: `src/atlasos/delegation/*`, `src/atlasos/context/*`, `src/atlasos/budget.py`,
`src/atlasos/ownership/*`, `src/atlasos/workers/catalog.py`, `contract.BUDGET`, the C1/C1-B/C1-C/C2
plans and evidence. Known issues in recovery guard, main reserve, handoff summary and context loading
remain **recorded debt**, not work items for this stage.

One judgement call to flag: item 3 changes the native arm's code. It is metering only, and the
alternative is a stated `NOT_COMPARABLE`; the Owner picks. If item 3 is refused, A0 still runs, but its
budget column is reported as `post_hoc` and its per-request series as `NOT_RUN`.

---

## K. Remaining risks and known limitations

1. **A0 budget equivalence.** Even with the hook, A0 decides *before* a request using an estimate;
   Atlas rejects using its own reservation model. Same ceiling, different enforcement. Reported, not
   hidden.
2. **A2 has no Main**, so the headline context metrics exist for three arms out of four. Any A1-vs-A2
   context comparison is "Main vs the sum of workers", which is a different quantity, not a better one.
3. **A2 ownership.** Closure-2 froze `ownership_mode="off"` for that path; `WorkerStep` carries no write
   paths. Unless Stage F adds structural ownership to the static DAG, A2 is a *less* constrained arm.
   Fixing it means either extending `WorkerStep` or declaring the asymmetry — the second is cheap and
   honest, the first is a runtime change. Recommendation: declare it, and note that the frozen DAG
   cannot double-write by construction because the plan check verifies disjoint concurrent write sets.
4. **Four new fixtures are the biggest delivery risk**, not the runtime. Each needs a reference
   implementation that proves oracle satisfiability, plus per-defect mutation evidence. If X2 or C2
   cannot meet that bar inside the preparation budget, the honest move is to drop to 6 tasks × 4 arms ×
   3 reps = 72 cells **before** freezing, never after.
5. **A3 may again produce zero proposals** (C2 measured 0/4). The matrix would then measure A1 twice on
   the natural-decision axis; that is a finding about the tasks' scale, and the design must not respond
   by adding delegation pressure to the prompt.
6. **Turn-cap asymmetry.** 40 turns for a Main and 40 for a single agent are not equivalent: a Main also
   spends turns on delegation calls and handoffs. A2's integration node gets 40 while a single worker
   gets 20. This is disclosed; it is not correctable without inventing a turn-equivalence model.
7. **Cache.** No cold-cache guarantee across cells; A1/A3 share a provider but not a session. Cache
   hit/miss is reported per cell and is inside prompt tokens.
8. **Cost estimate.** Observed C-series cost per cell: $0.0047–$0.0192 for 58K–192K system tokens
   (C2), $0.087 for 353K (C1-C, cache-miss heavy). At the 1M cap a cell lands around $0.04–$0.25; the
   96-cell expectation is **$4–$24**, with the $32/$96 stops bounding the worst case. The stops are
   candidates, not approvals.
9. **Time.** 96 cells at ~60–300 s each plus sandbox provisioning, concurrency 1 → roughly 6–12 hours
   across three batches. A batch stop leaves `NOT_RUN` cells that are never filled.
10. **Ceiling on inference.** 96 cells over 8 tasks is 24 observations per arm, 3 per arm per task, and
    the tasks are not a sample of anything. The report will state per-task results and family results,
    and will not generalise to "delegation helps".

---

## L. Closure route from formal evaluation to product delivery

1. **Freeze** — after the Owner approves this design: implement §J, run §H, then freeze the master
   manifest, the three batch plans, the executor identity, the prompt/grant/budget digests and the
   cost ceiling in one commit. Nothing about the matrix may change afterwards.
2. **Run** the three batches, one attempt each, no retry. Stop on the frozen rules; keep `NOT_RUN`
   cells as `NOT_RUN`.
3. **Report** the 96-row table, the per-family and per-task results, the paired deltas, the oracle
   verdicts and the five correctness axes per cell — plus an explicit "what this matrix cannot say".
4. **Decide mechanism defaults.** Using the measured result, decide for each mechanism whether it stays
   default-on (context plane, delegation, static DAG path), becomes opt-in, or is retired. Any change of
   default is a separate, small, reviewable commit — not a rewrite.
5. **Close.** `docs/final-roadmap.md` and `docs/project-status.md` get the final numbers; the remaining
   known issues (recovery guard, main reserve, handoff summary, context loading, the tool-refusal
   probing loop found in C2) become a numbered debt list with owners.
6. **Stop.** No C1-D, no C2-B, no C3, no new arm, no new task after the freeze. A negative or flat
   matrix is a delivered result, not a reason to reopen the design.

---

## Appendix — preparation gates (must pass before any live dispatch)

Twelve gates; all offline, all scripted, all producing a JSON record.

1. **Task qualification** — §H, 9 checks × 8 tasks, with the seed-fails / reference-passes evidence.
2. **Four-arm fairness** — one fixture digest per task reused by all four arms; identical goal bytes;
   identical sandbox image/resources; A1/A3 prompt digest equality; A2's DAG disclosed and verified
   disjoint.
3. **Matrix identity** — three batch plan digests + one manifest digest; order recomputes from the
   seed; every task appears once per arm per batch; 96 cells total, no duplicates.
4. **Effective configuration** — per cell: prompt bytes and digest, grant, budget (1,000,000), turns
   (40/20/40), delegation limits, tool surface (A1 has no `delegate_tasks` in its definitions), model,
   image. Read through the production resolver, not from the plan file.
5. **Plan resolution** — `contract.scope_plan(plan_id)` and `(scope)` return the frozen digest for all
   three batches; the manifest digest covers them.
6. **Real dispatch entry, offline** — `executor.dispatch(batch_plan_id, MODE_OFFLINE_PROOF)` runs 32
   synthetic cells through resolution → authority → claim → audit → matrix report, for **all three**
   batches, in a throwaway claim root.
7. **Per-arm synthetic execution** — a scripted provider and fake sandbox produce one cell per arm
   (A0, A1, A2, A3) in the normalised session shape, so the normaliser and `audit_cell` are exercised
   before a real cell exists. This is the gate that would have caught C1-B's unresolved entry.
8. **Independent oracle path** — one reference workspace per task scored by the sandboxed oracle; the
   recorded `sandbox_id`, `execution_mode` and `returncode` are asserted.
9. **Pending authorization refuses** — an unsigned record verifies `ready=false` for every batch plan;
   a signed record for one batch cannot authorize another.
10. **Historical preservation** — seven claims byte-identical; C2's evidence index re-hashes; older
    plan digests unchanged; no Stage F claim exists.
11. **Budget and stop semantics** — guarded accounting on for all four arms; the per-cell and per-batch
    stops are finite; `usage_attribution` present on every arm's record; A0's `budget_mode` is not
    `legacy` (or the record says `post_hoc` on purpose).
12. **Clean-room** — worktree clean at the frozen head, run root empty, default claim root, no
    `LAUNCH.lock` present, `opensandbox` healthy, credential present.
