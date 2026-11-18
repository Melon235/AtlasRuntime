# Closure-2B-1 — Controlled Connectivity Canary and Live Pilot Preparation

Status: `READY_FOR_INDEPENDENT_REVIEW` (offline only).
Live execution: **not approved, not run**. Benchmark, pilot and canary approvals are all
`PENDING_OWNER_APPROVAL`; this stage performed 0 model calls and 0 sandbox runs.

| Item | Value |
|---|---|
| Stage | `closure-2b-1` |
| Baseline | `1bc12d5f9cda929336f017beba1dd51b437784f4` (`fix/closure-2b0-r3-ledger-failclosed`) |
| Branch | `feat/closure-2b1-connectivity-canary` |
| New module | `experiments/evaluation/closure2/connectivity_canary.py` |
| Canary id | `closure2-connectivity-canary-v1` |
| Canary plan digest (unapproved) | recomputed per run; pinned in §4.3 |
| Preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` (unchanged) |
| Pilot plan digest | `a99342dd55adab0fa14a0f69797f42462e38e2f70fba238e28cde7167427bcf8` (unchanged) |
| Model calls | 0 |
| Sandbox runs | 0 |

---

## 1. Architecture audit

Ten questions, answered from the shipped source rather than from the design documents.

### 1.1 How does the Atlas runner initialise a model provider?

`experiments/evaluation/runner.py`:

1. `run_sample()` loads `.env` from the repository root (`load_dotenv(REPO_ROOT/".env")`) and refuses
   before any I/O when `DEEPSEEK_API_KEY` is absent or `sandbox.provider == "fake"`.
2. A **provider factory** is registered with AnyCode under a per-run name
   (`register_provider_factory(f"atlas-eval-{uuid4[:12]}", factory)`); the factory builds the shipped
   `DeepSeekAdapter` via `build_deepseek_adapter(model, on_call)`, which constructs an `AsyncOpenAI`
   client with `max_retries=0` and a 45 s HTTP timeout.
3. Workers receive `provider=<that run-scoped name>` through `ScopedWorkerFactory`, so the model
   client is per run and cannot be shared across runs.
4. `evaluation_resilience()` pins `retry.max_attempts=1` and `max_retries=0`: there are **no**
   automatic provider retries on this path.

The native baseline (`closure1b/native_baseline.py`) does the same through the same
`build_deepseek_adapter`, with its own provider name.

### 1.2 How is the OpenSandbox provider created and destroyed?

* Created by `runner._build_provider(config)` → `atlasos.sandbox.build_sandbox_provider(
  config.sandbox.provider, opensandbox_options=OpenSandboxOptions(endpoint, protocol, cpu, memory,
  ...))`. This constructs a client; it creates nothing on the server.
* Liveness is checked with `await provider.health()` before any work; `unavailable` aborts the run as
  `ENVIRONMENT_FAILURE`.
* The agent sandbox is created by `ProjectSandboxManager.ensure_binding(...)` (one managed sandbox per
  run) and destroyed by `await manager.close(workspace_task_id)` in the `finally` block. A failed
  destroy sets `metrics.sandbox_destroyed = False` and either raises `CLEANUP_FAILURE` or annotates
  an existing failure.
* `retain_sandbox_on_failure=False` is passed explicitly by both the canary and every B-0 arm.

### 1.3 Are the agent sandbox and the oracle sandbox independent?

Yes. `experiments/evaluation/sandboxed_oracle.py` creates a **second** sandbox
(`SandboxSpec(run_id=f"{run_id}-oracle", task_id="oracle", network="none", persistent=False,
labels={"atlas_oracle": "1"})`), uploads the exported workspace plus the hidden tests, runs
`python3 -m pytest`, and destroys it before returning the verdict. The agent sandbox is already closed
at that point (export happens before the `finally`, the oracle runs after it). `RuntimeMetrics` counts
only the agent side; the pilot evidence therefore reports `oracle_sandbox_runs: null` rather than
inferring it.

### 1.4 Which operations produce real provider spend?

Only model requests. On this path they happen inside `engine.run_tasks(team, task_specs)` and each
one passes through the DeepSeek adapter, which appends an `InferenceObservation`. Because retries are
disabled, one request = one billable call. Nothing in the sandbox, the oracle or the export path calls
a model.

### 1.5 Which sandbox operations have real side effects without a model call?

| Operation | Side effect |
|---|---|
| `provider.create` | a container is provisioned on the OpenSandbox server |
| fixture upload / `ensure_binding` | files are written into the container |
| `enforce_evaluation_isolation` | three `execute` calls (`test`, `rm`, `find`) inside the sandbox |
| worker tool calls | `bash`, `file_write`, `file_edit`, `grep`, `list_files` — arbitrary command execution |
| `export_workspace` / `export_patch` | reads the container, writes the run directory |
| oracle `provider.create` + upload + `pytest` + `provider.destroy` | a whole second container lifecycle |
| `manager.close` / `provider.destroy` | resource teardown, which can fail |

So "no model call" is not "no cost and no state": a canary can provision two containers and execute
arbitrary commands while spending nothing on tokens.

### 1.6 What is the minimal task structure `run_sample()` accepts?

`BenchmarkTask` with: `sample_id`, `category`, `mode` (`coding` ⇒ exactly **one** worker),
`prompt`, `target`, `visible_files` (staged into the sandbox), `workers=(WorkerStep(...),)`,
`allowed_paths`, `forbidden_paths`, `required_artifacts`, and optional `hidden_files` (written only
into the oracle sandbox; without them the oracle degrades to static checks). `run_sample` also
requires a real sandbox provider, `DEEPSEEK_API_KEY`, a healthy provider, and a Git executable for
`stage_git_repo`.

### 1.7 Can the existing runner be reused for the probe?

Yes, and it is the only sanctioned way: the canary calls `run_sample` unchanged with a canary
`RunnerSettings`. No agent loop, tool runtime, sandbox manager, oracle or evidence writer was
reimplemented.

### 1.8 Would a single simple task trigger unnecessary multi-worker scheduling?

No. `mode="coding"` with exactly one `WorkerStep`, `delegation_route_mode="off"` (the router is never
invoked), `handoff_mode="legacy"`, `worker_contract_mode="legacy"`. One agent, one sandbox, one task
spec.

### 1.9 How can connectivity be tested without leaking an API key?

The key is only ever read by `build_deepseek_adapter` at call time. The canary's precheck reports
**presence and source only** (`environment` / `dotenv_file_only` / `missing`) plus
`value_printed: false`; it never formats the value, its length or a prefix. Sandbox connectivity is
reported by the provider's `health()` status, not by printing credentials. Tests assert that a planted
key string does not appear anywhere in the precheck or the evidence.

### 1.10 How is the formal pilot's SQLite claim protected?

Three independent barriers:

1. the canary digest is a digest over a payload with `kind="connectivity_canary"`, its own id, its own
   task and its own benchmark namespace — it can never equal `a99342dd…`;
2. the canary claims in a **different file** (`canary-ledger.sqlite`, beside but not equal to
   `pilot-ledger.sqlite`), and `_claim_namespace_is_isolated()` refuses to run if those ever coincide;
3. the canary never imports, calls or patches `run_live_pilot`, and never touches
   `protocol.APPROVAL_RECORD` or `pilot_runner.PILOT_APPROVAL` (both asserted by tests).

---

## 2. Three levels, kept apart

| Level | Entry point | Provider | Sandbox | Cost | Approval |
|---|---|---|---|---|---|
| A — offline precheck | `--precheck` (default) | none | none | 0 | none |
| B — connectivity canary | `--live --confirm-live` | 1 model, 1 task | 1 agent + 1 oracle | bounded by §5 | **canary approval (pending)** |
| C — formal 8-cell pilot | `pilot_runner --live --confirm-live` | 8 cells | 8 agent + 8 oracle | owner ceiling (pending) | benchmark + pilot approval (pending) |

A canary pass proves *the chain works*. It is not a sample: its outcome, tokens and cost may not enter
any benchmark table, and every canary record says so in three separate fields.

---

## 3. The canary task

`canary-add-function`, a synthetic single-file task:

```text
calculator.py            (visible)
    def add(a: int, b: int) -> int:
        raise NotImplementedError("add is not implemented")

test_public.py           (visible)
test_acceptance.py       (hidden, oracle sandbox only)
    assert add(1, 2) == 3
    assert add(-2, 5) == 3
    assert add(0, 0) == 0
```

* `allowed_paths=("calculator.py",)`, `forbidden_paths=("test_public.py", "tests/**")` — editing the
  public test is a forbidden change and fails the scope check.
* `hidden_files={"test_acceptance.py": ...}` is uploaded only into the oracle sandbox, so the oracle
  leg is a real independent pytest session, not a static check.
* No network, no third-party package, no frozen P1/P2/N1 data, no frozen oracle, no frozen seed: the
  canary seed digest differs from every frozen seed digest (asserted).
* Deterministic: three arithmetic assertions; the only pass condition is a correct `add`.
* Existing-runner proof: the fixture is materialised and run through real `pytest` in the integration
  suite — the stub fails, a two-line implementation passes.

Identity, written into the plan payload and therefore into the digest:

```text
canary_id                     closure2-connectivity-canary-v1
kind                          connectivity_canary
is_benchmark                  false
is_official_pilot             false
counts_toward_formal_results  false
benchmark_id                  closure2-connectivity-canary
task_id                       canary-add-function
engine / topology             atlas / single
model                         deepseek-chat        (re-derived from the frozen config)
config                        experiments/configs/final4-context.yaml   (read-only)
attempts                      1
```

---

## 4. Execution stages and gates

### 4.1 Stage 0 — offline precheck

`--precheck` (default) inspects, and creates only a probe file inside the canary output directory:

* configuration: `final4-context.yaml` loads, `sandbox.provider == "opensandbox"`, image
  `atlas-uat-python:3.12`, `network=none`, workspace `/workspace/project`, model `deepseek-chat`;
* credentials: key variable present? (value never read into the report), `.env` present?
* dependencies: `opensandbox` importable, `git` present (blocking); `opensandbox-server` CLI (advisory —
  the server may run in WSL or on another host, so its absence is not proof of a broken endpoint);
* paths: both output roots writable; both outside the formal benchmark trees;
* resources: every limit positive and within the hard ceilings;
* approval: reported, never granted. `live_authorized` is always `false` in a precheck.

Exit `0` when nothing blocks, `1` when something does. `model_calls` and `sandbox_runs` are `0`.

### 4.2 Stage 1 — authorization gate

All of the following, collected together so one refusal never hides the next:

```text
explicit_live_intent            -> LIVE_INTENT_NOT_CONFIRMED
canary identity / namespace     -> CANARY_IDENTITY_MISMATCH
no formal-pilot collision       -> CANARY_FORMAL_IDENTITY_COLLISION
task == canary-add-function     -> CANARY_TASK_MISMATCH
engine == atlas, topology single-> CANARY_ENGINE_MISMATCH
attempts == 1                   -> CANARY_ATTEMPTS_EXCEEDED
limits within hard ceilings     -> CANARY_BUDGET_DRIFT
model == frozen model           -> CANARY_MODEL_MISMATCH / CANARY_CONFIG_UNREADABLE
approval status APPROVED        -> CANARY_APPROVAL_NOT_APPROVED
approved digest == plan digest  -> CANARY_PLAN_DIGEST_MISMATCH
approver/commit/timestamp       -> CANARY_APPROVAL_INCOMPLETE
cost ceiling usable             -> CANARY_COST_CEILING_NOT_APPROVED / _INVALID / _NOT_POSITIVE
```

The digest check proves *what* may run; the structural checks prove that an approval of a drifted
digest still cannot authorise a probe outside the hard ceilings. `run_connectivity_canary` takes no
approval argument, so a caller cannot hand in its own record.

### 4.3 Stage 2 — durable claim

`PilotLedger.claim_plan()` is reused unchanged, against `canary-ledger.sqlite`:

```text
BEGIN IMMEDIATE -> check pilot_claims(plan_digest) -> insert plan row -> insert cell row -> COMMIT
```

One plan claim per digest, taken before any provider request or sandbox provisioning, never released.
This round's unapproved plan digest is
`2b94e162cd178305f7aaef3e64a2bdf7996f72fcba4f208cbd547f7fd9195a1b`; approving a cost ceiling changes
it, which is the point — the owner approves a specific number, not "a canary".

### 4.4 Stage 3 — execution

`RUNNING` is persisted first. If that write fails, the executor is never called. Otherwise the shipped
`run_sample` runs under a second, outer wall clock (`480 s`; the engine itself has `300 s`). A raised
executor, a timeout and a normalisation failure each produce their own status and are never retried.

### 4.5 Stage 4 — oracle and cleanup evidence

| Field | Source | Unknown stays unknown |
|---|---|---|
| `agent_sandbox_created` | `metrics.sandbox_created` | `null` when the executor raised |
| `agent_sandbox_destroyed` | `metrics.sandbox_destroyed` | `null` when not observed |
| `cleanup_error` | `result.safety["cleanup_error"]` | `null` when the destroy succeeded |
| `oracle_passed` | `OracleOutcome.passed` | — |
| `oracle_environment_failure` | `OracleOutcome.environment_failure` | — |
| `oracle_failure_kind` | `OracleOutcome.failure_kind` | `null` when the oracle did not fail |
| `oracle_sandbox_status` | hidden-test `sandbox_id` + failure kind | `UNKNOWN` unless confirmed |

`oracle_sandbox_status` is `CREATED_CONFIRMED` only when the oracle reported a sandbox id,
`NOT_CREATED_CONFIRMED` only when provisioning failed before creation, `NOT_APPLICABLE` when the task
has no hidden files, and `UNKNOWN` otherwise.

A canary **passes** only when every leg is confirmed: oracle passed, no environment failure,
`task_success`, agent sandbox created **and** destroyed, and ledger integrity `COMPLETE`. A leaked
sandbox is a canary failure, not a footnote.

### 4.6 Stage 5 — evidence

`experiments/results/closure2-canary/canary-summary.json` (git-ignored, machine-specific), containing
the identity flags, plan digests, resource limits, the enforcement table, the claim record, the
per-run record and an explicit `not_benchmark_evidence` note. A failed write raises
`CANARY_EVIDENCE_NOT_SAVED` (CLI exit `5`) rather than claiming the record exists.

---

## 5. Canary budget

### 5.1 Proposed minimum (part of the plan digest, not approved)

| Knob | Value | Why this value |
|---|---|---|
| `max_agent_turns` | 6 | read the two files, edit one function, run one pytest: ~3–5 turns in practice |
| `max_model_requests` | 6 | derived from turns; there is no separate request counter |
| `max_output_tokens_per_request` | 1024 | one short function plus a short report |
| `max_prompt_token_budget` | 40 000 | a five-turn canary on a two-file repo is a few thousand tokens; 40k is an order of magnitude of headroom |
| `max_tool_calls` | 12 | read, edit, run, verify, report |
| `timeout_seconds` | 300 | one container, one short task |
| `outer_timeout_seconds` | 480 | adds provisioning, export and the oracle container |
| `max_estimated_cost_usd` | **unset** | the owner sets it; until then the gate refuses with `CANARY_COST_CEILING_NOT_APPROVED` |

Proposed (not approved) ceiling: **$0.05**.

### 5.2 Worst case, from the frozen pricing snapshot

At `$0.30/M` input (all cache-miss) and `$1.20/M` output:

```text
40 000 prompt tokens         x 0.30 / 1e6 = $0.0120
6 x 1 024 output tokens      x 1.20 / 1e6 = $0.0074
                                          = $0.0194
```

Plus at most one request's worth of admission-estimate overrun (the guard is estimate-based), which is
far below `$0.005`. `$0.05` is therefore ≈ 2.5× the computed worst case. The OpenSandbox containers are
self-hosted: they consume host resources, not provider credit.

### 5.3 What is actually hard-limited

| Knob | Mechanism | Strength |
|---|---|---|
| `max_agent_turns` | `AgentConfig.max_turns` in the AnyCode loop | enforced per agent |
| `max_output_tokens_per_request` | `max_tokens` per request | enforced by the provider call |
| `timeout_seconds` | `asyncio.wait_for` around `engine.run_tasks` | wall clock only |
| `outer_timeout_seconds` | `asyncio.wait_for` around the whole execution | wall clock only |
| `max_prompt_token_budget` | `RunBudgetState` admission (`budget_mode="guarded"`) | guarded before send, **estimate-based**; overruns are recorded in `budget_overrun_tokens`, so this is not a mathematical cap |

### 5.4 What is only judged afterwards

* `max_tool_calls` — the run completes, then the count is compared (post-hoc verdict);
* the number of provider requests — no separate counter exists;
* the dollar estimate — computed from reported usage after the fact;
* whether the sandbox was destroyed — observed, not guaranteed in advance.

### 5.5 What is not observable at all

* the **billed** amount: no runner reports an invoice; only a usage estimate exists;
* the oracle sandbox count;
* provider-side retries outside the adapter (`max_retries=0`, `max_attempts=1` — any other retry would
  be invisible);
* a cache split the provider did not report (stays `None`, never `0 %`);
* sandbox creation when the executor raises before returning.

### 5.6 Known asymmetries (restated, not removed)

`max_agent_turns` is not a request cap; the Atlas token guard is not a dollar guard; sandbox timeouts do
not cancel a request already sent; estimated cost is not the bill. All four statements are encoded in
`RESOURCE_ENFORCEMENT` and shipped inside the evidence, not only in this document.

---

## 6. Formal 8-cell pilot — cost preparation

### 6.1 Pricing source

`experiments/configs/final4-context.yaml` (identical block in `eval-benchmark.yaml` and
`closure2-shared-prefix.yaml`), read through `atlasos.config.load_config`:

```text
provider                      deepseek
cache_hit_input_per_million   0.006
cache_miss_input_per_million  0.30
output_per_million            1.20
snapshot_date                 2026-11-06
```

Label: **`PRICING_NOT_LIVE_VERIFIED`**. The snapshot is the repository's frozen DeepSeek Flash
peak-rate table (the `deepseek-chat` alias resolves to Flash); it was not re-checked against the
provider's current price list in this environment. Off-peak rates are roughly half, so the snapshot is
an upper bound per token, not a quote.

### 6.2 Scenarios

Per-cell token assumptions, then summed over the eight proposed cells. The `N1 × A2-P` cell is costed
as **single-worker** because the frozen router refuses that DAG (`direct`), and costing it as multi
would overstate the pilot.

| Scenario | Input tokens | Output tokens | Cache hit | Cache miss | Model requests | Estimated cost |
|---|---|---|---|---|---|---|
| LOW | 685 000 | 40 500 | 380 000 | 305 000 | 114 | **$0.14** |
| EXPECTED | 1 270 000 | 63 000 | 415 500 | 854 500 | 210 | **$0.33** |
| HIGH | 2 080 000 | 96 000 | 0 | 2 080 000 | 288 | **$0.74** |

Assumptions per cell:

| Scenario | Single-worker cell | Multi-worker cell | Cache-hit ratio (Atlas / native) |
|---|---|---|---|
| LOW | 45 000 prompt / 3 500 out / 8 requests | 110 000 / 6 000 / 18 | 0.60 / 0.40 |
| EXPECTED | 90 000 / 6 000 / 20 | 200 000 / 9 000 / 30 | 0.35 / 0.25 |
| HIGH | 260 000 / 12 000 / 36 | 260 000 / 12 000 / 36 | 0.0 / 0.0 |

HIGH is the frozen budget ceiling: 260 000 tokens per run, 36 turns, and no cache credit. EXPECTED is
anchored to the historical E1 multi-worker mean (~139 k prompt tokens per run) scaled to a
three-module task; LOW assumes the shared-prefix layout really does warm the cache for Atlas and that
the agent stays well inside its turn cap.

EXPECTED, per cell (`input / output / assumed hit ratio / cost`):

| Cell | Engine | Topology | Input | Output | Hit ratio | Cost |
|---|---|---|---|---|---|---|
| P1 × N0 | native | single | 90 000 | 6 000 | 0.25 | $0.0276 |
| P1 × N1 | native | multi | 200 000 | 9 000 | 0.25 | $0.0561 |
| P1 × A0 | atlas | single | 90 000 | 6 000 | 0.35 | $0.0249 |
| P1 × A1-L | atlas | multi | 200 000 | 9 000 | 0.35 | $0.0502 |
| P1 × A1-P | atlas | multi | 200 000 | 9 000 | 0.35 | $0.0502 |
| P1 × A2-P | atlas | multi | 200 000 | 9 000 | 0.35 | $0.0502 |
| P2 × A1-P | atlas | multi | 200 000 | 9 000 | 0.35 | $0.0502 |
| N1 × A2-P | atlas | single (router `direct`) | 90 000 | 6 000 | 0.35 | $0.0249 |

### 6.3 Contract upper bound — deliberately incomplete

* Atlas cells (6): the guarded admission bounds a run at ~260 000 tokens; at the higher of the
  miss/output rates that is ≈ **$0.312 per cell**, ≈ **$1.87** for six. This is an approximation: the
  guard is estimate-based and records overruns instead of denying them.
* Native cells (2): there is **no in-run token guard**. Only the 36-turn and 8 000-token per-response
  caps bound output (288 000 tokens → ≈ $0.346 per cell); prompt tokens are
  **`UNBOUNDED_BY_CONTRACT`**.
* Therefore `upper_bound.total_usd` is **`null`**, status `UPPER_BOUND_INCOMPLETE`. Publishing a single
  "maximum pilot cost" would be fiction.

### 6.4 What these numbers are not

They are not a measurement of these tasks (no cell has run), not the historical numbers re-used as
fact, and not a bill. A cell whose cache split is unknown has **no** cost, and is never costed as a
cache miss. The canary itself is excluded from every figure above.

Accounting boundary for this stage: the DeepSeek calls used by the harness to write code are developer
tooling, not AtlasRuntime benchmark spend. The future canary and the future pilot are two separate
ledgers, each requiring its own approval.

---

## 7. Isolation proof

| Concern | Mechanism | Proof |
|---|---|---|
| Formal pilot claim | separate ledger file; separate plan digest; `_claim_namespace_is_isolated()` | unit test: canary claim exists in `canary-ledger.sqlite`, no pilot ledger file is created, `PILOT_ALREADY_CLAIMED` on a second attempt |
| Formal approval records | the canary reads only `CANARY_APPROVAL` | unit test: `protocol.APPROVAL_RECORD` and `pilot_runner.PILOT_APPROVAL` are byte-identical before/after; both stay `PENDING_OWNER_APPROVAL` |
| Formal evidence tree | `_assert_isolated_output()` refuses `experiments/results/closure2` and `experiments/runs/closure2` | unit + integration test: `CanaryIsolationError` before the executor; the formal results directory listing is unchanged |
| 18-cell dry run | not touched | test: fresh manifests identical apart from `generated_at`; committed artifact still matches |
| Formal task data | own fixture, own oracle, no P1/P2/N1 files | test: canary seed digest ∉ frozen seed digests; no hidden-test path reused |
| Live pilot entry point | never imported | test: the module has no `run_live_pilot`; a poisoned `pilot_runner.run_live_pilot` is never reached |

`experiments/results/closure2-canary/` is git-ignored (one line in `.gitignore`, justified in §11) because
the precheck records host-specific facts (paths, dependency presence) and must not be committed as
evidence.

---

## 8. Authorization and refusal matrix — actual results

Every row is an executed offline test, not a description.

| # | Path | Result |
|---|---|---|
| 1 | default state (no approval, no intent) | refused: `LIVE_INTENT_NOT_CONFIRMED`, `CANARY_APPROVAL_NOT_APPROVED`, `CANARY_COST_CEILING_NOT_APPROVED` |
| 2 | live without approval | `CanaryNotAuthorized`, executor called 0 times, no evidence file |
| 3 | approved but no live intent | refused, executor 0 calls |
| 4 | wrong approved digest | `CANARY_PLAN_DIGEST_MISMATCH` |
| 5 | different model (digest re-approved) | `CANARY_MODEL_MISMATCH` |
| 6 | turns above the hard ceiling (digest re-approved) | `CANARY_BUDGET_DRIFT` |
| 7 | budget change inside the ceiling | digest changes → `CANARY_PLAN_DIGEST_MISMATCH` |
| 8 | extra / frozen benchmark task id (digest re-approved) | `CANARY_TASK_MISMATCH` for both |
| 9 | attempts = 2 | `CANARY_ATTEMPTS_EXCEEDED` |
| 10 | ceiling `None` / NaN / inf / string / bool / -1 / 0 | refused with the matching cost reason code |
| 11 | plan id colliding with `closure2-pilot-1` | `CANARY_FORMAL_IDENTITY_COLLISION` |
| 12 | duplicate claim | `PILOT_ALREADY_CLAIMED`, executor 0 calls, no evidence file |
| 13 | output path inside the formal tree | `CanaryIsolationError`, executor 0 calls |
| 14 | `--live` without `--confirm-live` | CLI exit 2, no evidence |
| 15 | `--live --confirm-live` while pending | CLI exit 3, no evidence, no ledger file |

Offline execution paths exercised with a stub executor: pass; provider exception; sandbox creation
failure; cleanup failure; oracle deliverable failure; oracle environment failure; missing usage;
missing cost; invalid cost; outer timeout; `RUNNING` write failure (executor not called); terminal write
failure (status becomes `BLOCKED_LEDGER_POST_EXECUTION` and the real `record` is kept beside it);
normalisation failure (nothing invented); unwritable evidence path (`CANARY_EVIDENCE_NOT_SAVED`).

---

## 9. Verification (offline)

| Check | Result |
|---|---|
| `pytest tests/unit/test_closure2_connectivity_canary.py` | 55 passed |
| `pytest tests/integration/test_closure2_connectivity_canary_entrypoint.py` | 13 passed |
| `pytest` (whole suite, `--basetemp` outside the repo) | **1396 passed, 6 skipped, 1 deselected**, exit 0 |
| Closure-2A preflight | `PASSED`, **62 checks**, 0 failed, `execution_ready: false`, digest `3080f313…` |
| `pilot_runner --dry-run` | 18 cells, `contract_ok: true`, `model_calls: 0`, `agent_sandbox_runs_total: 0`, `execution_ready: false` |
| `ruff check .` / `ruff format --check .` | clean |
| `pyright` | 0 errors |

Baseline for comparison: `1328 passed, 6 skipped, 1 deselected`. The 68 new tests account for the
difference exactly.

---

## 10. Freeze integrity

| Frozen object | Value | Status |
|---|---|---|
| Preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` | unchanged |
| Pilot plan digest | `a99342dd55adab0fa14a0f69797f42462e38e2f70fba238e28cde7167427bcf8` | unchanged |
| Router | `r0.1.0` / `5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244` | unchanged |
| Native config digest | `6cb2d1d91badaa235efa4427e5a53bdfc17610931e7b4c13e524406c2a863d14` | unchanged |
| Tasks P1/P2/N1, arms, budget table | as frozen in `closure2/protocol.py` | unchanged |
| `experiments/configs/*.yaml` | not modified | unchanged |
| Files added | `closure2/connectivity_canary.py`, two test modules, this document | new only |
| Files modified | `docs/project-status.md`, `.gitignore` (one line) | see §11 |

Forbidden files (`src/atlasos/**`, `catalog.py`, `protocol.py`, `preflight.py`, `runner.py`,
`native_baseline.py`, `stage_r0_routing.py`, `architecture-design-contract.md`, `pyproject.toml`,
all YAML) are untouched.

---

## 11. Deviations from the suggested file list

1. **The integration test is named `test_closure2_connectivity_canary_entrypoint.py`**, not
   `test_closure2_connectivity_canary.py`. The test directories are not packages, so two files with the
   same basename collide at collection time (`import file mismatch`) and the whole suite fails to
   collect. The rename follows the existing `test_closure2_pilot_runner.py` /
   `test_closure2_pilot_entrypoint.py` convention.
2. **Two lines in `.gitignore`** for `experiments/results/closure2-canary/`. The precheck records
   host-specific facts (absolute paths, dependency presence); committing it as evidence would be wrong,
   and leaving it untracked would dirty `git status`.

No other file outside the allowed set changed.

---

## 12. Remaining risks — what this stage did not verify

1. **No real provider call.** Key validity, quota, model availability, cache behaviour and true pricing
   are all unverified. The canary is designed to answer exactly these, and has not run.
2. **No real sandbox.** `opensandbox-server` was not contacted; the image `atlas-uat-python:3.12` was not
   pulled or started on this host. The CLI is not on the Windows PATH (it runs in WSL), which the
   precheck reports as advisory, not as health.
3. **No real oracle execution.** The oracle fixture was proven passable/failable on the Host; the sandbox
   oracle path (upload, pytest, destroy) is exercised only by the frozen benchmark's own tests.
4. **Cost figures are conditional.** HIGH is a contract ceiling, EXPECTED is an assumption, LOW is an
   assumption. The native upper bound does not exist.
5. **Parallelism is still unmeasured.** The canary is single-worker by design; multi-worker overlap,
   contention and handoff behaviour remain unverified until a pilot cell runs.
6. **The ledger is single-machine.** It coordinates processes sharing one file; deleting the database is
   outside its threat model, and it is not a cross-host exactly-once mechanism.
7. **One attempt only.** A crashed or failed canary consumes its claim permanently; re-running requires
   a new plan digest and a new approval.
8. **Cost ceiling is advisory.** There is no pre-send dollar guard in either engine.

---

## 13. What the owner must decide before a live canary

1. Set `estimated_cost_ceiling_usd` on `CANARY_PLAN` (proposed `$0.05`) — this changes the plan digest.
2. Approve that digest in `CANARY_APPROVAL` (`status: APPROVED`, `approved_by`, `approval_commit`,
   `approved_at`).
3. Confirm the DeepSeek key and `opensandbox-server` endpoint for the canary host, and answer the
   precheck's advisory items.
4. Only then may `python -m experiments.evaluation.closure2.connectivity_canary --live --confirm-live`
   be run, once.

Nothing in this stage performed any of steps 1–4, and no command in it can approve itself.
