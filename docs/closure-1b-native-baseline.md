# Closure-1B — Native AnyCode Baseline Parity

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`.

| Item | Value |
|---|---|
| `PREVIOUS_APPROVED_SHA` | `7b30e590a96487b05fb6e03cd7e5cd683eee523d` |
| `C1_B_HEAD_SHA` | the commit that contains this document |
| Branch | `feat/final-closure` |
| Design contract | `37eba87` — unchanged |
| AnyCode | 0.9.0, unmodified |
| Live model calls | **0** |
| Real container | local OpenSandbox, `atlas-uat-python:3.12`, `network=none` |

---

## A. What the native arm is

> **AnyCode 0.9.0 native agent/orchestration with a neutral OpenSandbox tool adapter.**

That sentence is the whole claim. It is **not** an unmodified upstream full-stack environment, and
the run identity says so: `engine = anycode-0.9.0-native-agents-with-neutral-opensandbox-tool-adapter`
is part of `config_digest`, so a native-sandbox run can never be mistaken for a Host-copy run or an
Atlas run.

### A.1 Execution chain

```text
materialize_fixture → stage_git_repo → GitProjectProvisioner.inspect
  → OpenSandboxProvider.create            (trusted_sandbox_spec: image, network=none, workspace_root)
  → GitProjectProvisioner.provision       (bundle upload → clone → HEAD verification)
  → enforce_evaluation_isolation          (staging bundle removed, private-name sweep)
  → NeutralEngine.run_tasks               (AnyCode agent loop + DAG scheduler + ToolRegistry/ToolExecutor)
        └─ six tools → SandboxToolBackend → provider.execute/read_file/write_file
  → export_patch → export_workspace       (same functions the Atlas arm uses)
  → provider.destroy                      (confirmed, never assumed)
  → evaluate_oracle_isolated              (same hidden tests, same acceptance, fresh sandbox)
```

### A.2 Shared infrastructure (held constant on purpose)

`OpenSandboxProvider`, `GitProjectProvisioner`, `export_patch`/`export_workspace`,
`evaluate_oracle_isolated`, the provider usage adapter (`InferenceObservation`), fixture staging and
`stage_git_repo(FIXTURE_COMMIT_LABEL)`.

Holding these constant is what makes the experiment an *environment* comparison. None of them is an
Atlas optimisation: each is the control plane the two arms must share for the comparison to mean
anything.

### A.3 Not injected

No Context Plane, no `ScopedWorker`, no `ActionSafetyGate`, no durable ledger, no progressive
disclosure, no `tool_search` / `dispatch_action` / `history.read` / `handoff.read`, no delegation
router, no shared budget guard, no `ProjectSandboxManager` / binding store / ownership lock / recovery.

A test asserts the neutral registry contains **exactly** the six basic tools.

### A.4 Native semantics preserved

- AnyCode's own agent loop, wave scheduler and DAG dependency handling. Native multi still gets
  AnyCode's `Relevant output from prerequisite tasks` context — no Atlas handoff, no ContextStore.
- One shared container workspace per run; all workers of a multi arm edit the same checkout.
- The agent configuration is the upstream `AgentConfig` with `ToolSecurityPolicy(workspace_root=...)`
  pointing at the container, never at the Host.

---

## B. The two entry points

| Unit | Shape | Container | Workspace |
|---|---|---|---|
| **N0** | native single: one worker, the whole public task turn | one per run | `/workspace/project` |
| **N1** | native multi: the same candidate DAG as the Atlas multi arm (same task ids, same `depends_on`, same public facts, same local assignments) | one per run | one shared checkout |

`experiments/evaluation/closure1b/runner_entry.py` is the new, separately identifiable entry
(`benchmark_id = atlasos-closure1b-native-sandbox-v1`). The Final-4 manifest, its unit results and
the 22/24 run inventory are untouched.

### B.1 Historical Host-copy baseline

`experiments/evaluation/baseline.py` still exists and still behaves as it did — Windows system
prompt, disposable Host copy, Host oracle — because its numbers were produced that way. It now
records `workspace_isolation=disposable_host_copy`, `historical_host_mode=true`,
`comparable_with_atlas_sandbox=false` and `superseded_by`. Its numbers must never be quoted as
sandbox-condition results.

### B.2 Two defects in the historical N0 entry, fixed

| Defect | Evidence | Fix |
|---|---|---|
| `UNITS["N0"]` had no `route`, but `settings_for()` reads `unit["route"]` | `KeyError` before any work started | `"route": "off"` added, with the reason written next to it |
| `run_unit()` called `run_baseline_sample()` without `baseline_root` | `TypeError: missing required argument` | the parameter is passed; a test stubs the call and asserts the kwarg |

Both are covered by `tests/integration/test_closure1b_native_baseline.py`
(`test_the_historical_n0_unit_now_resolves_its_route`, `..._passes_the_baseline_root`).

---

## C. Parity with the Atlas arm

### C.1 Environment

`python -m experiments.evaluation.final4.parity_check` → **`BASELINE_PARITY_PASS`**, every check true:

| Check | Result |
|---|---|
| same Git fixture, same `source_commit` on both arms | true |
| same seed files in the checkout | true |
| same file content after the same edit | true |
| same grep result paths | true |
| same shell / `uname` / Python version | true |
| network blocked in both | true |
| native tool surface exactly the six basic tools | true |
| native tool writes all succeeded | true |
| native destroy confirmed / Atlas destroy confirmed | true / true |

The pre-Closure-1B probe wrote the native seed *through the tools* instead of cloning the fixture, so
the native arm had no `.git` and the two arms were not comparable (stale `.git`-less workspace vs a
Git bundle checkout). Both arms now provision the same committed repository through
`GitProjectProvisioner`, and the probe runs before any mutation so the file listings are comparable.

### C.2 Task information

| Fact | Atlas | Native | Identical? |
|---|---|---|---|
| shared public block | `WorkerStep.shared_context` → public block of the user turn | prepended to the task description | yes, verbatim |
| worker's own objective | `WorkerStep.prompt` | task description body | yes, verbatim |
| environment note | system prompt (legacy layout) | system prompt | same text |
| declared dependencies | frozen DAG | frozen DAG | yes |
| dependency output delivery | Atlas legacy handoff / Context Plane projection | AnyCode native prerequisite context | **different mechanism, same information** |
| trusted worker profile | Atlas `ScopedWorker` profile | none (native `AgentConfig`) | **different, by design** |
| system prompt layout | profile + scope + tools + rules | coding-agent instruction + environment | **different, by design** |

Neither arm is given duplicated task text and neither is deliberately weakened. The comparison is
"same task facts, native organisation vs Atlas organisation".

### C.3 The six basic tools

| Tool | Atlas (`SandboxWorkspaceTools`) | Neutral (`SandboxToolBackend`) |
|---|---|---|
| `bash` | validated cwd, output cap, `is_error` on non-zero | same contract; unknown status is an error |
| `file_read` | workspace-jailed path, error on provider failure | same |
| `file_write` | creates parent, error on provider failure | same |
| `file_edit` | reads, replaces one occurrence, error if absent | same |
| `grep` | base64 helper, workspace-relative root | `grep -rn`, exit 1 = no match, exit >1 = error |
| `list_files` | base64 helper, workspace-relative root | python `os.listdir`, workspace-jailed |

Differences that remain and can affect performance: Atlas validates through a richer policy layer and
adds truncation/annotation text; the neutral tools return plainer output. Neither changes which
operations are possible.

**Defects fixed in the neutral backend (B1.4/B3.1):**

| Defect | Before | After |
|---|---|---|
| path rewriting | `raw.lstrip("./")` turned `.env` into `env` and accepted anything | `posixpath.normpath` inside the workspace; `.env` stays `.env`; `../x`, `/etc/passwd` refused |
| unknown exit code | `int(result.exit_code or 0)` reported success | `None` is returned and the tool returns `is_error=True` with "exit status was not reported" |
| provider error | not checked; a failed command looked like empty success | `result.ok` is checked and a provider error raises, never exit 0 |
| `grep` | `grep ... || true`, exit swallowed | exit 1 = no match; exit >1 or `ok=False` = tool error |
| workspace preparation | `mkdir -p` failed on an existing directory under a non-Linux shell | idempotent `os.makedirs`, checked |

### C.4 Resources and budget

Same image, 1 CPU, 1 Gi, `network=none`, same workspace root, same wall-clock timeout, same
`max_turns` per worker, same `max_tokens`, same temperature (0.0), same oracle.

**The budget mechanisms are not the same, and the runs say so.** Atlas rejects a request that would
breach the run budget *before sending it*; the native arm has no such guard. The same public
thresholds (`token_budget`, `max_tool_calls`, wall clock) are applied to the native run **post hoc**
and recorded as `budget_enforcement: "post_hoc"` with an explicit `enforcement_note`. No claim is made
that the two arms share a hard budget mechanism.

### C.5 Known remaining differences

1. Dependency-output delivery mechanism (Atlas projection vs AnyCode prerequisite context).
2. System prompt content and structure (Atlas trusted profile vs native instruction).
3. Tool output formatting and truncation annotation.
4. Budget enforcement (pre-send rejection vs post-hoc verdict).
5. Atlas adds a trajectory recorder, ledger and gate; the native arm has none — these are the
   mechanisms under study, not controlled variables.

---

## D. Deterministic acceptance on real containers

`python -m experiments.evaluation.closure1b.acceptance` → **`REAL_SANDBOX_ACCEPTED`**,
`experiments/results/closure1b/acceptance.json`.

The provider is a script that replays the frozen Final-4 reference solution's tool calls. The agent
loop, `ToolRegistry`, `ToolExecutor`, the container and the oracle are real; the model is not called.

| Fact | N0 (`validation-serial`) | N1 (`validation-parallel`) |
|---|---|---|
| oracle | **PASS** | **PASS** |
| files written by the agent match the reference solution | 2/2 | 3/3 |
| tool calls executed through the real executor | 6 | 15 |
| workers | 1 | 3 (w1, w2, w3) |
| container | Linux, Python 3.12.16, git 2.47.3 | Linux, Python 3.12.16, git 2.47.3 |
| container HEAD == provisioned source commit | yes | yes |
| isolation sweep passed | yes | yes |
| sandbox destroyed (provider-confirmed) | yes | yes |
| agent latency | 4.8 s | 8.3 s |
| provision latency | 13.2 s | 12.1 s |
| export latency | 4.7 s | 4.7 s |

**Concurrency is measured, not assumed.** N1's two dependency-free workers each emitted
`agent_start` before the other emitted `agent_complete` (orchestrator events, not the DAG shape):
`independent_pair_overlapped = true`, 3 workers started and 3 completed. The timings are recorded in
the evidence file.

These numbers prove the environment and the mechanism wiring. They say nothing about model quality —
no model was called.

---

## E. Lifecycle failure handling

Every path is classified and persisted; a normal or an exceptional exit both attempt cleanup, and an
unconfirmed `destroy` is never reported as success.

| Failure | Classified as | Cleanup |
|---|---|---|
| create failed | `ENVIRONMENT_FAILURE` | nothing to destroy |
| provisioning failed | `ENVIRONMENT_FAILURE` | destroy attempted, result recorded |
| isolation failed | `ENVIRONMENT_FAILURE` | destroy attempted, result recorded |
| agent loop failed | `RUNTIME_FAILURE` | destroy attempted |
| wall-clock timeout | `TIMEOUT` | destroy attempted |
| export failed | `RUNTIME_FAILURE` (or `SANDBOX_*`) | destroy attempted |
| destroy not confirmed | `CLEANUP_FAILURE` | recorded in `safety.cleanup_error` and `environment.sandbox_destroy` |
| oracle environment failure | `ENVIRONMENT_FAILURE` (from the oracle's own kind) | oracle sandbox destroyed by the oracle |
| provider command failure / unknown exit | tool error, never success | n/a |

A non-`Exception` `BaseException` (e.g. cancellation of the task) is re-raised, not turned into a
result, and the `finally` still destroys the container.

---

## F. Tests

New: `tests/integration/test_closure1b_native_baseline.py` (20 tests, offline, `FakeSandboxProvider`).
They cover the lifecycle success and failure paths, the two backend path/exit-code defects, the
"no Host fallback" guard, the six-tool surface, task-turn parity, both historical N0 defects and the
concurrency detector.

The real-container acceptance is **not** part of the default suite: it needs a running OpenSandbox.
When the provider is unavailable the script exits `3` with `status: BLOCKED` and never falls back to a
Host workspace.

Regression: `pytest -q` → 1018 tests, 0 failures, 6 skipped; `ruff check .`, `ruff format --check .`
and `pyright` clean.

---

## G. Not established

- Real-model behaviour, task quality or cost of either arm. No model was called.
- Cache hit/miss for the native arm: the scripted provider emits no usage, so its token fields stay
  `None` rather than being filled with zeros.
- Any comparison number between N0/N1 and the Atlas arms. This stage establishes that the baseline
  *can* run under comparable conditions; it does not run the comparison (that is Closure-2).
- Recovery of an interrupted native run. The native arm has no checkpoint and does not claim one.
