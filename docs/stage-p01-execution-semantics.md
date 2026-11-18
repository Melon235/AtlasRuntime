# Stage P0.1 — Execution Semantics and Live Readiness

Branch: `feat/stage-p01-execution-semantics`, based on `feat/stage-p-main-driven-pipeline`
(`b9220e5c1b7f0c9f9f121ebc381006f938a97822`), AnyCode 0.9.0.

Decision: **`STAGE_P01_ACCEPTED_OFFLINE`**. `DEV-1` from stage P0 is fixed at the AtlasRuntime
boundary, the Main can run a Host-authorized public suite, and nothing about the *unknown* case was
relaxed. No live model call was made.

---

## 1. Root cause

The P0 report said "a refusal ends the run". The precise chain is:

```text
ActionSafetyGate.execute                      src/atlasos/actions/gate.py
  ├─ evaluate()                → DENY (rule / external policy / hard precheck / bad input)
  ├─ _failure(...)             → ToolResult(is_error=True, retry_safe=False)   ← the only place
  └─ _execute(...)             ← never reached; the tool body never runs

anycode/core/runner.py:964    retry_safe = result.retry_safe is not False
anycode/core/runner.py:639    indeterminate = next(record for record in results if not record.retry_safe)
anycode/core/runner.py:641    terminal_stop = stop_side_effect_unknown(...)  → break
```

Three facts made this structural rather than accidental:

1. `retry_safe` carries **two** meanings: "safe to retry the call" (M3's intent) and "the external
   effect may have happened" (AnyCode's). M3's `_failure` set `False` for that first meaning.
2. `_normalize_result` (`anycode/tools/executor.py:151`) turns `retry_safe=None` from a
   **side-effecting** tool that returned `is_error=True` into `False`, so even omitting the field was
   not enough for `file_write`/`bash`.
3. A gate refusal happens *before* `_execute` in every branch — measured, not assumed: the
   `executed` argument of `_audit` is `False` on all of them, and the P0.1 tests assert the tool
   body was never called.

So the denial was provably pre-execution while the flag said "unknown side effect".

## 2. The fix

One new, explicit, Host-owned switch on the gate, default **off**:

```python
ActionSafetyGate(..., pre_execution_denials_retry_safe: bool = False)
AtlasToolRuntime(..., pre_execution_denials_retry_safe: bool = False)
```

When it is on, every result the gate returns *before* it calls the executor is
`retry_safe=True` — the message, reason codes, `executed=False` audit and ownership evidence are
byte-identical. The goal path turns it on; every other caller (workflow, coding, evaluation,
materials) keeps the previous behaviour, so no frozen experiment moves.

`_pre_execution_failure()` is the single seam: all nine pre-execution returns in `execute()` and
`execute_revalidated()` route through it, so a future refusal cannot quietly take the other path.

What did **not** change:

* the unknown case — `retry_safe=False` is still returned by `SandboxWorkspaceTools` on a transport
  failure, by AnyCode's idempotency-outcome failure, and by a side-effecting tool that errors
  without stating its safety. `test_the_unknown_case_is_untouched` covers the last one.
* `external_denial_retry_safe` (the R1-1 knob the evaluation runner already uses) keeps its meaning.
* no automatic retry was introduced anywhere: the model decides what to do next.

Supporting fixes found while wiring this up:

* **A silent Main failure was reported as success.** AnyCode converts a provider failure into an
  `error` event and returns without `done`, so `Agent.run` yields `messages=[]`, `success=True`.
  `run_goal` now reports `main_no_result` instead (the guard the delegation service already applied
  to children).
* **A repair could be masked by a stale `.pyc`.** A same-size edit inside the same filesystem second
  leaves `__pycache__` valid (the pyc header stores mtime in seconds), so the second run imports
  the pre-repair bytecode. The public suite therefore runs with `-B`.

## 3. The public test channel

`atlasos/app/public_tests.py` — `run_public_tests`.

| Property | How |
|---|---|
| Command is Host-fixed | `PublicTestContract.command`; `PublicTestInput` has no command field and rejects extras |
| Only the Main may call it | `atlas_role == "main"` **and** `task_id == the run's main task`; a child also never gets it (no profile carries it) |
| Runs in the sandbox | `GoalSandbox.run_public_command` — the same provisioned `/workspace/project`, never the Host |
| Bounded | `timeout_seconds` (120), `max_output_chars` (4 000, head+tail), `max_calls` (4) |
| Auditable | M3 action `tests.public` (distinct from `shell.execute`), M4 ledger, plus per-call evidence: `command_digest`, `call_index`, `exit_code`, `status`, `duration_ms`, `output_digest`, `output_chars`, `output_truncated` |
| Non-terminal on FAIL | `is_error=True, retry_safe=True` — the exit status was observed, so nothing is unknown |
| Terminal on transport ERROR | `is_error=True, retry_safe=False` |
| Refusals are data | identity/cap failures return `status="REFUSED"`, `executed=false`, `retry_safe=True` |

Separation from the oracle:

| | Public suite | Oracle |
|---|---|---|
| Who runs it | the Main, through a tool | the harness, after the Main stops |
| Command | `python3 -B -m pytest -q tests/test_public.py` | `python3 -m pytest -q` |
| Sandbox API | `run_public_command` (text, bounded) | `run_check` (digests only) |
| Reachable from the model | yes, bounded | never registered as a tool |
| Verdict | evidence for the Main | the run's `acceptance` |

The preflight blocks a configuration where the two commands are identical, and
`test_the_main_repairs_after_a_failing_public_run` asserts that only one oracle execution happened
after two public ones.

## 4. Live preflight

`atlasos/app/live_preflight.py`, `run_live_preflight(options)` → `{status, checks[], ...}`, plus
`--preflight` on the CLI (prints the report, exits 2 when BLOCKED, never builds a provider).

`run_goal` runs it whenever `provider_factory is None` — i.e. whenever it would construct the real
DeepSeek adapter — writes `run_dir/preflight.json`, and raises before any provider or sandbox
exists if the status is BLOCKED.

Nineteen checks: sandbox configured, real provider, `require_real`, absolute sandbox workspace,
clean source repo, write scope declared, write scope relative, public suite configured, public ≠
oracle, acceptance configured, acceptance digest declared, budget present (named as advisory),
delegation bounds, context metrics (advisory), provider `deepseek`, model equals
`config.provider.model`, API key present, no injected factory, and a run directory outside
`experiments/results` and `experiments/runs`.

Reported by the current tree (fake provider, no key, no public suite):

```text
sandbox_provider_real              FAIL   sandbox.provider='fake'
sandbox_isolation_required         FAIL   require_real must stay True
public_tests_configured            FAIL   no host-fixed public suite
public_tests_separate_from_oracle  FAIL   one of the two commands is not configured
budget_configured                  FAIL   token_budget=None
live_provider_factory              FAIL   an injected factory is a deterministic test
api_key_present                    FAIL   DEEPSEEK_API_KEY is not set
```

That is the intended behaviour: the default configuration is not live-ready, and the report says
exactly why.

## 5. End-to-end evidence

Deterministic (fake provider, real runtime, real sandbox tool layer, two real `pytest` runs):

```text
t1  Main  delegate_tasks(calc, text)                  → delegate_parallel, 2 children overlap
t2  child calc: file_read app/calc.py
    child text: file_read app/text.py
    handoff: 2 summaries + ho- refs
t3  Main  file_edit app/calc.py   (subtract implemented as a + b — wrong)
t4  Main  run_public_tests        → FAIL, exit 1, "1 failed, 2 passed", output 1 236 chars
t5  Main  file_edit app/calc.py   (repair: a - b)
t6  Main  run_public_tests        → PASS, exit 0
t7  Main  final answer
    export → oracle (separate command) → PASS
```

Real OpenSandbox (`ATLAS_STAGE_P_REAL_SANDBOX=1`):

```json
{"contract": {"command": "python3 -B -m pytest -q tests/test_public.py", "max_calls": 4},
 "command_digest": "973146498eddafc565c6384df51bebcab53d636f249c31be20be88ae4763c683",
 "calls": 2,
 "runs": [{"call_index": 1, "status": "FAIL", "exit_code": 1, "duration_ms": 1430.3,
           "output_chars": 563},
          {"call_index": 2, "status": "PASS", "exit_code": 0, "duration_ms": 1306.3,
           "output_chars": 96}],
 "oracle_is_separate": true}
```

with `provider_is_real: true`, `network_probe: PASS`, `destroyed: true`, and the oracle's own
`python3 -m pytest -q` returning 0.

## 6. Failure matrix

| Scenario | Expectation | Where |
|---|---|---|
| Rule denial | refused, not executed, `retry_safe=True`, loop continues | `test_a_rule_denial_is_a_non_terminal_refusal_when_the_host_opts_in` |
| Same, switch off | `retry_safe=False` (previous behaviour) | `test_the_same_denial_stays_terminal_by_default` |
| Unregistered tool | pre-execution refusal, non-terminal | `test_an_unregistered_tool_is_also_a_pre_execution_refusal` |
| Invalid input | pre-execution refusal, non-terminal | `test_invalid_tool_input_is_a_pre_execution_refusal` |
| Revalidated resume | no execution, non-terminal | `test_a_denied_revalidated_execution_does_not_run_the_tool` |
| Ownership denial | refused, no body, `retry_safe=True`, evidence recorded | `test_an_ownership_denial_is_refused_before_the_body_and_is_non_terminal` |
| Tool ran, outcome unknown | `retry_safe=False`, not downgraded | `test_a_tool_that_ran_and_failed_is_not_downgraded` |
| Ledger on denial | no execution row; audit `executed=False` | `test_the_durable_ledger_records_no_row_for_a_refused_call` |
| Ledger on execution | one COMPLETED / EXECUTED_SUCCESS row | `test_the_durable_ledger_records_a_completed_execution_as_before` |
| Main denial in the pipeline | refused, Main continues, no file written | `test_a_main_write_outside_its_grant_is_refused_and_the_main_continues` |
| Interpreter command | refused, never reaches the provider, Main continues | `test_an_interpreter_command_is_refused_and_never_reaches_the_sandbox` |
| Sandbox transport failure | `side_effect_unknown`, run stops, sandbox destroyed | `test_a_sandbox_tool_failure_is_reported_instead_of_swallowed` |
| Public FAIL | data, Main repairs, re-runs | `test_the_main_repairs_after_a_failing_public_run` |
| Public transport ERROR | `retry_safe=False` | `test_a_transport_failure_is_not_downgraded` |
| Child calls the suite | refused, nothing executed | `test_a_child_agent_cannot_run_the_public_suite` |
| Call cap | refused without executing | `test_the_call_cap_refuses_without_executing` |
| Budget rejection | `budget_exceeded`, no false success | `test_a_budget_rejection_is_reported_not_hidden` |
| Cancellation | children reaped, sandbox destroyed | `test_cancellation_reaps_children_and_destroys_the_sandbox` |

## 7. Regression

```text
pytest -q                    1521 collected, 0 failures, 0 errors, 7 skipped (exit 0)
ruff check src tests         All checks passed
ruff format --check src tests 250 files already formatted
pyright                      0 errors, 0 warnings
```

`git status -- experiments/ docs/evaluation/ src/atlasos/config.py src/atlasos/workers/routing.py`
is empty: Closure-1/Closure-2 digests, approval records, `R0.1.0` and the `AtlasConfig` schema are
unchanged. Stage M (16 + 26) and Stage P0 (19) tests still pass; the two P0 assertions that encoded
the old fatal behaviour were updated to the new contract.

New tests: 9 execution semantics + 7 public tool + 8 preflight + 1 pipeline loop = 25.

## 8. Remaining risks

1. **The gate's switch is a per-composition decision.** Any new entry point that forgets
   `pre_execution_denials_retry_safe=True` gets the old fatal behaviour. It is default-off because
   flipping the default would change the evaluation arms; a future stage may want to make the
   pre-execution contract the default and re-baseline.
2. **A tool that raises** inside the body is converted by AnyCode to `retry_safe=None`, which the
   runner treats as *safe*. Atlas tools avoid this by returning explicit values, but a third-party
   tool that raises after a side effect would be under-reported. Not introduced here; worth a
   follow-up.
3. **Ledger coverage of refusals.** M4 records executions only, so a refusal is recorded by M3
   (audit + trajectory), not in `action_ledger`. That is correct but means "count the denials"
   requires the M3 trail, not the ledger.
4. **Public suite scope.** The contract is one command; a task that needs several suites needs
   several entries or a wrapper script, and a wrapper script would be `UNCLASSIFIABLE_SHELL` if run
   through `bash` (it runs through this tool, not through `bash`, so this is a configuration
   decision, not a blocker).
5. **The fixture's oracle is visible.** The calculator fixture has one visible test file; the
   separation enforced here is procedural (different command, different channel, different
   authorization). Stage P1's task needs a genuinely hidden check to test that boundary harder.
6. **Live cost is still advisory.** The preflight states it; a hard cap would need a provider-side
   limit.

None of these blocks Stage P1; items 1 and 5 are the two to decide before the live run.

## 9. Acceptance

| # | Requirement | Result |
|---|---|---|
| 1 | Pre-execution refusal no longer triggers `side_effect_unknown` | PASS — switch on in the goal path, asserted end-to-end |
| 2 | Genuinely unknown outcomes keep the terminal semantics | PASS — 3 tests, including the pipeline |
| 3 | The Main observes a refusal and continues | PASS — 2-turn loop, no file written |
| 4 | The Main can run a Host-authorized public suite | PASS — `run_public_tests`, fixed command |
| 5 | The public suite only runs in the sandbox | PASS — `run_public_command`, never the Host |
| 6 | A FAIL is repaired and re-verified | PASS — deterministic and real-container runs |
| 7 | The oracle stays independent | PASS — different command/channel, asserted |
| 8 | Concurrency, handoff and metrics unchanged | PASS — Stage M + P0 suites green |
| 9 | Safety / ledger / ownership not loosened | PASS — refusals still refuse, evidence intact |
| 10 | DAG and Closure behaviour unchanged | PASS — 1521 green, frozen diff empty |
| 11 | Offline checks pass | PASS — pytest, ruff, ruff format, pyright |
| 12 | No live DeepSeek call | PASS — scripted providers only |

`STAGE_P01_ACCEPTED_OFFLINE`.
