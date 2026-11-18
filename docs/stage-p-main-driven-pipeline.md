# Stage P0 — Main-driven Full Pipeline Integration

Branch: `feat/stage-p-main-driven-pipeline`, based on `feat/stage-m-main-driven-delegation`
(`5fdeaf4527b162857720b8c2112a4574ee9a96ee`), AnyCode 0.9.0.

Decision: **`STAGE_P0_ACCEPTED_OFFLINE`** with one declared deviation (`DEV-1`, §9). No live model
call was made; no frozen benchmark, approval record or `R0.1.0` file was touched.

---

## 1. Audit findings (M0 → P0)

| # | Question | Finding |
|---|---|---|
| 1 | Did `run_goal()` use OpenSandbox? | **No.** Stage M composed a Host tool surface (`ToolSecurityPolicy(workspace_root=<host dir>)`) and never created a sandbox. `ProjectSandboxManager`, `provision.py`, `export.py` and `SandboxWorkspaceTools` existed but were only wired in `app/coding.py`. |
| 2 | Can Main and children share one sandbox workspace? | **Yes, by construction.** `AtlasToolRuntime(sandbox_tools=…)` replaces the six builtin definitions with sandbox-backed ones, and the delegation plane already shares exactly one runtime between the Main and every child. One sandbox, one logical workspace, one physical backend. |
| 3 | Can a child bypass the sandbox? | No new path was introduced. `SandboxWorkspaceTools._logical()` refuses anything outside `binding.workspace_root`, and the runtime has no Host file tool left to fall back to: the six names are *replaced*, not added. |
| 4 | Are Main/child histories separate? | Yes (stage M): children are built from the assignment only, and the Main receives a bounded projection. Verified again inside the pipeline. |
| 5 | Is one budget shared, and is it concurrent-safe? | `RunBudgetState` uses one `asyncio.Lock` around reserve/settle, never across a provider call. Main and children share the state; each request is admitted atomically. It is a **conservative admission gate on an estimate**, not a 0-overrun guarantee. |
| 6 | Do the safety layers apply to both? | Yes: one `AtlasToolRuntime` → one action gate, one ledger, one idempotency store, one ownership enforcer, one sandbox. |
| 7 | Can a refusal be recovered from? | **No — and this was not known before.** See §2. |
| 8 | Is the Main's context observable per request? | Only partially before this stage: `UsageAttributionHook` had a running total, not a per-request series. |

## 2. Blocking finding: a refusal terminates the run

`ActionSafetyGate` returns `ToolResult(is_error=True, retry_safe=False)` for a DENY, and AnyCode's
runner interprets `retry_safe=False` as *"the external effect may have happened"*:

```text
anycode/core/runner.py:639-647
indeterminate = next((record for _, record in results if not record.retry_safe), None)
if indeterminate is not None:
    terminal_stop = stop_side_effect_unknown(indeterminate.output)
    break
```

Measured in this stage (both against the Host path, without a sandbox):

| Attempt | Result |
|---|---|
| Main writes outside its declared grant | 1 turn, `stop_reason=side_effect_unknown`, run failed |
| Main runs `python3 -m pytest -q` | 1 turn, `stop_reason=side_effect_unknown`, run failed |
| Main runs `git log` while the provider transport fails | `side_effect_unknown`, never "success" |

The conflation is deliberate on both sides — M3 says "a deny is not retryable", AnyCode says "not
retryable means unknown" — but the composition means **an agent cannot survive a single refusal**.

Consequences for this stage, all encoded in the pipeline rather than papered over:

1. The Main is told, in its stable prompt, that interpreter commands are refused and that a refusal
   ends the run.
2. The harness, not the Main, executes the acceptance suite (`GoalSandbox.run_check`), so the
   public tests still run — inside the same sandbox — and the Main cannot fabricate their result.
3. `DEV-1` (§9) records that "the Main runs the public tests" is not achievable under the current
   `R0.1.0` + M3 semantics, and it is a prerequisite for the live stage.

`ledger/bridge.py::_pre_execution_failure` already recognizes `action_denied`, yet the deny result
still carries `retry_safe=False`, so the marker only affects the ledger record, not the runner. Fixing
this needs an owner decision (M3 deny semantics or an AnyCode contract distinction between "do not
retry" and "state unknown"). Stage P0 does not change either.

## 3. Implementation

| File | Status | Responsibility |
|---|---|---|
| `src/atlasos/app/goal_sandbox.py` | new | Sandbox lifecycle for a goal run: provider selection, `SandboxProjectConfig.validate_v1()`, provisioning, isolation probes, `SandboxWorkspaceTools`, export, teardown. `GoalSandboxOptions` refuses `fake` unless `require_real=False` **and** a factory is injected. |
| `src/atlasos/app/goal_verification.py` | new | Host-owned acceptance: R1-2A `VerificationEngine` over the exported snapshot + one fixed command via `SandboxCheckRunner` (a two-method protocol, so the oracle cannot touch lifecycle or export). `INCONCLUSIVE` is never `PASS`. |
| `src/atlasos/app/main_driven.py` | modified | `GoalRunOptions.sandbox`/`acceptance`; sandbox start before the runtime; `sandbox_tools=` on the shared runtime; export + verify after the loop; teardown in `finally`; `metrics` block; `isolation`/`acceptance`/`artifacts`/`sandbox` evidence; CLI flags; stable-prompt authority note. |
| `src/atlasos/delegation/usage.py` | modified | Per-request context series + peak, alongside the running totals. |
| `tests/helpers/stage_p.py` | new | Scripted provider, git fixture, acceptance contract, shared rig. |
| `tests/integration/test_stage_p_pipeline.py` | new | Full pipeline (fake provider) + opt-in real OpenSandbox run. |
| `tests/integration/test_stage_p_boundaries.py` | new | Refusals, child escape, sibling read, sandbox tool failure, shared-write refusal, budget, cancellation. |
| `tests/unit/test_stage_p_goal_metrics.py` | new | Metric accounting, `UNAVAILABLE` semantics, options validation, verdict logic. |

Nothing in AnyCode, the DAG scheduler, the Context Plane, `R0.1.0` or `AtlasConfig` was modified.

## 4. OpenSandbox evidence (real provider, run once)

`ATLAS_STAGE_P_REAL_SANDBOX=1 pytest tests/integration/test_stage_p_pipeline.py::…real_opensandbox…`

```json
{
  "provider": "opensandbox", "provider_is_real": true,
  "capabilities": {"isolation": "container", "networking": "allowlist",
                   "persistent_filesystem": true, "file_transfer": true, "cancellation": true},
  "binding_state": "READY", "workspace_root": "/workspace/project",
  "network_probe": "PASS",
  "host_separation": {"host_only_visible": false,
                      "sandbox_only_visible_on_host": false,
                      "sandbox_only_read_back": true},
  "provision_commands": 4, "provision_uploads": 1,
  "created": true, "destroyed": true, "retained": false,
  "patch_changed_files": 1, "export_files": 5, "export_size_bytes": 20480, "check_runs": 1
}
```

* Provisioning is a `git bundle` + `git clone` inside the container (`rev-parse HEAD` verified).
* `network_probe: PASS` = the empirical egress probe was **blocked**, not that the flag was set.
* `host_only_visible: false` = a Host-only sentinel was not readable from the container.
* The Main's own shell command (`git diff -- app/calc.py`) and every child file read executed through
  `SandboxWorkspaceTools`; the artifact only appeared on the Host through `export_patch`/
  `export_workspace`. The source repository still reads `def add` only, i.e. the Host was never the
  workspace.
* After the run, `docker ps` showed no container: teardown is real, not a bookkeeping flag.

## 5. Execution trace (fake provider, deterministic)

```text
run_goal
 1  sandbox.start            provision → network/host probes → SandboxWorkspaceTools
 2  Main turn 1              AgentRunner → delegate_tasks(calc, text)
 3  DelegationRouter          r0.1.0 gates → delegate_parallel (2)
 4  children (overlap)        barrier(2): both inside the provider simultaneously
      calc: file_read app/calc.py  → "defines add only; subtract missing"
      text: file_read app/text.py  → "defines normalize"
 5  handoff projection        2 summaries + 2 ho- refs; result 1 557 chars
 6  Main turn 2              sees the tool result, resumes its own loop
 7  Main turn 3              file_edit app/calc.py → adds subtract
 8  Main turn 4              bash "git diff -- app/calc.py" inside the sandbox
 9  Main turn 5              final answer (no test claim)
10  export                    workspace.patch + run_dir/workspace
11  oracle                    static checks PASS; python3 -m pytest -q → exit 0
12  teardown                  manager.close → destroyed
```

## 6. Context metrics

From the real-sandbox run:

```json
{
  "main_requests": 5,
  "main_peak_context_tokens": 140,
  "main_cumulative_input_tokens": 700,
  "main_context_tokens_by_request": [140, 140, 140, 140, 140],
  "main_cache_hit_tokens": 200,
  "main_cache_miss_tokens": 500,
  "main_completion_tokens": 50,
  "child_total_prompt_tokens": 1220,
  "child_completion_tokens": 100,
  "system_total_tokens": 2070,
  "handoff_payload_chars": 1557,
  "delegation_rounds": 1,
  "max_observed_child_concurrency": 2,
  "unavailable_fields": []
}
```

Accounting, stated so nothing has to be inferred:

* **context of one request** = full prompt = cache-miss + cache reads (+ cache creation when the
  provider reports it). A cache hit makes a request cheaper, never shorter, so the billed miss side
  is never used as "context length".
* `main_cumulative_input_tokens` sums the Main's own requests only; children are a separate column.
* `system_total_tokens` = Main + children, prompt + completion. Cache-hit tokens are already inside
  the prompt totals and are not added twice.
* `handoff_payload_chars` is the exact size of the JSON the Main received from `delegate_tasks`.
* When no provider usage was observed, the Main fields are `null` and named in
  `unavailable_fields`; nothing is estimated to fill the gap.

## 7. Safety and failure tests

| Scenario | Observation |
|---|---|
| Main writes outside its grant | `OWNERSHIP_DENIED_UNDECLARED_PATH`, no file in sandbox **or** export, run failed closed (`side_effect_unknown`), sandbox destroyed |
| Main runs an interpreter command | `OWNERSHIP_UNCLASSIFIABLE_SHELL`, the command never reaches the provider, coverage reported as `PARTIAL` |
| Child reads `../../../../etc/passwd` | refused by `_logical()`, no content leaks into the delegation evidence or the child's own turns |
| Child calls `delegate.read` | the tool is not in a worker's surface; the sibling's output never appears in the child's conversation |
| Sandbox tool transport failure | surfaced as `side_effect_unknown`, artifact still exported, sandbox destroyed — never an empty success |
| Two children declare the same write path | router refuses (`SHARED_WRITE_RISK`); no child ever starts |
| Budget rejection | `stop_reason=budget_exceeded`, `admission_rejections ≥ 1`, run failed, sandbox destroyed |
| Cancellation mid-run | `CancelledError` propagates, `destroy_count == 1`, no `delegate:` task survives |

## 8. Regression

```text
pytest -q  →  1496 collected, 0 failures, 0 errors, 7 skipped (exit 0)
ruff check src tests  →  clean
pyright               →  0 errors, 0 warnings
```

The 7 skips are 6 pre-existing plus the opt-in real-sandbox test. Frozen material:
`git status -- experiments/ docs/evaluation/ src/atlasos/config.py src/atlasos/workers/routing.py`
is empty — Closure-1/Closure-2 digests, approval records, `R0.1.0` and the `AtlasConfig` schema are
unchanged. Stage M's 42 tests still pass.

New tests: 3 pipeline (1 opt-in) + 8 boundary + 8 unit = 19.

## 9. Known limitations

**DEV-1 — the Main cannot run the acceptance suite.** `R0.1.0`'s shell classifier refuses every
interpreter invocation (`python3 -m pytest`, `pytest`, `node …`), and a refusal is fatal (§2). The
harness therefore runs the suite through `GoalSandbox.run_check`. The tests still execute inside the
same sandbox and the Main cannot see or fake the result, but "Main runs the public tests" is **not**
achieved. Unblocking it requires either an explicit Host-declared test-command allowlist in the R1-1
manifest or a change to M3 deny semantics — both are owner decisions outside this stage.

> **Resolved in stage P0.1** (`docs/stage-p01-execution-semantics.md`): the gate now distinguishes a
> proven pre-execution refusal (`retry_safe=True`, the Main continues) from an unknown side effect
> (`retry_safe=False`, still terminal), and a Host-authorized `run_public_tests` tool runs a fixed
> command inside the sandbox. `R0.1.0` was not touched.

Other limitations:

* **DEV-2 — the deterministic provider is not isolation.** It executes real subprocesses on the
  Host, so `network_probe`/`host_separation` report `NOT_APPLICABLE_FAKE_PROVIDER` instead of a
  number that would mean nothing. Only the opt-in real-provider test claims isolation.
* **DEV-3 — one sandbox per run, one logical workspace.** A child cannot be given its own filesystem;
  its write scope is enforced by ownership, not by a second container.
* **DEV-4 — provider operations are serialized** by the single `SandboxWorkspaceTools` lock. Child
  *reasoning* overlaps (proved by the barrier); child *sandbox calls* queue. This is safer for a
  single-connection provider but caps throughput.
* **DEV-5 — no checkpoint/resume.** A goal run always starts a fresh sandbox; a crash leaves the
  run directory (and, if the process dies before teardown, a container — the retention flag is the
  only mitigation today).
* **DEV-6 — the acceptance contract is supplied by the caller.** `run_goal` does not derive checks
  from the goal text; a run without `acceptance` reports `NOT_CONFIGURED` and cannot be "PASS".

## 10. Stage P1 live execution proposal (not executed)

**Task.** `closure2-pipeline-migration`? No — the frozen benchmark is out of scope here. Proposed:
the same calculator fixture used in §5, extended to three modules, because it is small, deterministic,
and its acceptance contract (`subtract` exists + `pytest` green) is already written and reviewable.

**Fixed configuration.**

| Item | Value |
|---|---|
| Provider / model | DeepSeek `deepseek-chat` (adapter `integrations/deepseek.py`) |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB, 20 min |
| Main | `max_turns=12`, tools = core (incl. `delegate_tasks`/`delegate.read`), grant `app/calc.py` |
| Delegation | 2 children max, `research_worker`, read-only, `child_max_turns=6`, 300 s |
| Budget | `token_budget=240_000`, conservative admission, **not** a hard monetary cap |
| Oracle | static checks + `python3 -m pytest -q` in the sandbox |
| Runs | 1 authorized run, 1 repetition |

**Cost.** ≈ 12 Main turns × ~2.5k prompt tokens + 2 children × 6 turns × ~2k ≈ 60k prompt /
6k completion; at DeepSeek list prices that is the order of **USD 0.01–0.05**, with the price of a
retry or a longer loop not included. Because admission is estimate-based, the guarantee is "stops
admitting when the budget is exhausted", not "never exceeds it".

**What must be approved separately.**

1. The live model call and its budget (§ above).
2. `DEV-1`: accept that the Main will not run the suite itself, or authorize the R1-1/M3 change
   that would let it. **This is the gating question** — a live Main that tries `pytest` on its own
   ends its run.
3. Where the run directory and evidence may be written (a scratch path, not `experiments/results`).

**Report format for the live run.** `session.json` (schema below) + the live proposal id, with
`failure` classified into `execution` / `sandbox` / `ownership` / `budget` / `acceptance`.

## 11. Acceptance

| # | Requirement | Result |
|---|---|---|
| 1 | Goal path really uses OpenSandbox | PASS — `provider_is_real=true`, binding READY |
| 2 | Main and child file/shell tools run in isolation | PASS — sandbox tools + in-container `git diff` + in-container `pytest` |
| 3 | Two children really overlap | PASS — barrier(2), `max_observed_child_concurrency=2`, intersecting intervals |
| 4 | Main resumes its own loop and edits code | PASS — 5 turns, `file_edit` applied, patch exported |
| 5 | Handoff stays bounded and authorized | PASS — 1 557-char projection, `ho-` refs, sibling read refused |
| 6 | Ownership / Action Safety / Budget not bypassed | PASS — 8 boundary tests, denials recorded, budget rejection surfaces |
| 7 | Main peak context recorded per request | PASS — series + peak + `UNAVAILABLE` fallback |
| 8 | Full coding task passes an independent oracle | PASS — static checks + in-sandbox `pytest` exit 0 |
| 9 | Sandbox lifecycle correct under failure/cancel | PASS — destroyed in every scenario, no container left |
| 10 | Full regression, frozen artifacts unchanged | PASS — 1496 tests, empty frozen diff |
| 11 | No live DeepSeek call | PASS — scripted provider only |

`STAGE_P0_ACCEPTED_OFFLINE`, with `DEV-1` recorded as a prerequisite for Stage P1.
