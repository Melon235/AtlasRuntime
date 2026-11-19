# Stage Reliability-Closure + Stage B0 Preparation

Branch `fix/reliability-closure-b0`, based on `exp/stage-p1b-real-delegation` at
`5865f7560b1cbdcd090571e1741d4589d7eb0d72` (stage P1b, `STAGE_P1B_FAILED`).

Closes the three defects stage P1b recorded, and prepares — without running — the first controlled
real-delegation experiment.

**Verdict: `RELIABILITY_CLOSURE_ACCEPTED_OFFLINE`.** No model was called in this round. Stage B0 is
frozen and its preflight is READY, but the live attempt is not authorized here.

---

## A. Git provenance

| Item | Value |
|---|---|
| Baseline verified | `exp/stage-p1b-real-delegation` @ `5865f7560b1cbdcd090571e1741d4589d7eb0d72`, worktree clean, local = remote (`0 0`) |
| Branch | `fix/reliability-closure-b0` (from the baseline) |
| Runtime at baseline | still the stage P0.1 freeze: `git diff 5fad913..5865f75 -- src/` is empty |
| P1b evidence | `experiments/stage_p1b_live/evidence/run-1/` complete (session, transcript, ledger export, empty patch, attempt marker) |
| Frozen artifacts | unchanged: `git diff 5865f75 -- experiments/stage_p1b_live experiments/fixtures experiments/results experiments/evaluation docs/stage-p1b-main-driven-delegation.md docs/stage-p01-execution-semantics.md` is empty |
| Source files changed this round | 3: `app/goal_sandbox.py`, `app/main_driven.py`, `sandbox/opensandbox_provider.py` |
| Added | 2 test modules (24 tests), `experiments/reliability_closure/`, `experiments/stage_b0_controlled/`, this report |

Untouched by instruction and by diff: AnyCode 0.9.0, `AtlasConfig`, R0.1.0 / m.1.0 Router,
`ScopedWorkerFactory`, `HandoffStore`, the context plane, the M3/M4/M6 machinery, the frozen
benchmark and every earlier experiment result.

---

## B. DEV-3 — completed-command errors on the goal path

### B.1 Root cause

The G3-1 fix exists and works; **the goal path never selected it**:

```text
run_goal
  └─ GoalSandbox.start()
       └─ SandboxWorkspaceTools(manager, task_id, provider)     # tool_error_mode omitted
            = TOOL_ERROR_MODE_LEGACY                             # the constructor default
```

Under `legacy` the bash tool returns `retry_safe=None` for a completed non-zero exit. AnyCode's
`_normalize_result` fills `None` with `not (side_effecting and is_error)` → `False` →
`runner.py:639` stops the loop with `side_effect_unknown`. That is exactly what ended stage P1b on
turn 9, and the G3-1 `corrected` mode (which states `retry_safe=True` for an observed completion)
was never reachable from `run_goal`.

### B.2 Fix (composition only)

| File | Change |
|---|---|
| `app/goal_sandbox.py` | `GoalSandboxOptions.tool_error_mode: str = "corrected"`, validated against `TOOL_ERROR_MODES`; `start()` passes it to `SandboxWorkspaceTools` and records it in `_SandboxEvidence.tool_error_mode` |
| `app/main_driven.py` | `GoalRunOptions.tool_error_mode: str = "corrected"`, applied to the sandbox options by `_goal_sandbox_options()` (one knob, one value) and recorded as `session["tool_error_mode"]` |

Why `corrected` is the default *here only*: `GoalSandbox` and `run_goal` are the main-driven goal
path and nothing else. `SandboxWorkspaceTools` keeps `legacy` as its default, so the static-DAG path,
the Stage E/G evaluation runner and every historical caller are byte-identical unless they opt in.
`experiments/evaluation/runner.py` already had its own `RunnerSettings.tool_error_mode`.

Nothing else in the chain changed: no AnyCode edit, no AgentRunner edit, no second error classifier,
no rewrite of `SandboxWorkspaceTools`.

### B.3 Semantics kept apart

| Situation | `is_error` | `retry_safe` (corrected) | Runner |
|---|---|---|---|
| exit 0 | False | **True** | continues |
| exit 1 / 2 / 128, output or none | True | **True** | shows the error, continues |
| `SandboxCommandResult.ok is False` (transport / SDK) | True | False (both modes) | stops: `side_effect_unknown` |
| `SandboxLostError`, `SandboxOwnershipError` | True | False (both modes) | stops |
| path validation failure before execution | True | False (both modes) | stops |
| M3 refusal before execution | True | True since P0.1 | continues |

`retry_safe=True` here means **"the outcome is known, the loop may continue"**. It is not
replayability: no consumer re-executes a tool because of it (G3-1 §2 audit, re-verified for this
round in §F). The flag's only control-flow effect is the terminal stop it no longer triggers.

---

## C. DEV-2 — command output fidelity

### C.1 SDK contract (audited, `opensandbox==1.2.0`)

`models/execd.py` is explicit:

* `ExecutionLogs.stdout` / `.stderr` are `list[OutputMessage]`; each `OutputMessage.text` is a
  **fragment**, and the model's own `_format_output_messages` inserts `"\n"` between two fragments
  only when neither side already carries a line boundary;
* `Execution.text` / `Execution.__str__` are the SDK's *display* rendering of the same list;
* there is no raw byte stream, no per-fragment offset and no explicit "end of line" marker.

`OpenSandboxProvider._execution_text` used `"".join(...)`, which discarded every boundary the SDK
expects the caller to restore. That is the defect stage P1b's Main reported ("The `cat` output has
lost newlines in some places").

### C.2 Fix

`_join_log_messages()` in `sandbox/opensandbox_provider.py` implements **exactly the SDK's rule**
(separator when neither side has a boundary character; `\r\n` then lone `\r` normalised to `\n`),
with one deliberate difference: the result is not `rstrip`-ed, because Atlas consumes command output
as data (an exported `git diff`, a test report), not as a line to print. A conformance test runs the
SDK's own formatter over the same fragments and asserts equality, so a future SDK change fails a
test instead of silently disagreeing.

### C.3 Real-container evidence

`experiments/reliability_closure/probe_output_fidelity.py` (READY, 11/11 checks, container
destroyed), raw fragments captured per case:

| Case | Fragments | Adapter output | `"".join` would give | Adapter correct |
|---|---|---|---|---|
| `printf 'hello'` | 1 (`'hello'`) | `hello` | `hello` | yes |
| `alpha\nbeta\ngamma\n` | 3 | `alpha⏎beta⏎gamma` | `alphabetagamma` | **yes** |
| `one\ntwo` (no final newline) | 2 | `one⏎two` | `onetwo` | **yes** |
| `a\n\nb\n` | 3 (`a`,`\n`,`b`) | `a⏎b` | `a\nb` | limitation |
| `a\n\n\n\nb\n` | 5 | `a⏎⏎⏎b` | `a\n\n\nb` | limitation |
| `x\r\ny\r\n` | 2 | `x⏎y` | `xy` | **yes** |
| `true` | 0 | `` | `` | yes |
| stderr only, exit 2 | 0 stdout / 1 stderr | stderr delivered | same | yes |
| Python traceback, exit 1 | 0 stdout / n stderr | traceback in stderr | same | yes |
| `git log --oneline` | 1 | one line | same | yes |
| `git diff` | 7 | 127 chars, 7 lines | one 127-char line | **yes** |
| one 4000-char line | 1 | 4000 chars | same | yes (not split) |

Measured limits, stated not implied:

1. **The final newline is never delivered.** A command that ends with `\n` produces no trailing
   empty fragment, so `"alpha\nbeta\ngamma\n"` reconstructs as `alpha\nbeta\ngamma`. No adapter rule
   can restore it, and the adapter does not invent one. Consequence: `workspace.patch` written by
   `export_patch` has no trailing newline.
2. **An interior blank line is under-represented.** A blank line arrives as a `"\n"`-only fragment,
   which is indistinguishable from a separator already present; `a\n\nb\n` reconstructs as `a\nb`.
   The SDK's own renderer produces the same text, so this is the provider's encoding, not the
   adapter's arithmetic.
3. **Byte identity is not recoverable.** These are *structural* guarantees. When byte identity
   matters, the evidence is the `SandboxEvidence` digest of `stdout\0stderr` produced by
   `execute()`, not the text.

What the fix does guarantee: one line per line for ordinary output, correct stderr separation, and
no more glued `cat`/`pytest`/`git diff` text.

---

## D. DEV-4 — the terminating turn's evidence

### D.1 Audit

| Component | Behaviour | Consequence |
|---|---|---|
| `ConversationSnapshotHook.before_turn` | saves the conversation **before** each model turn | the assistant message of the turn that kills the run is never saved; P1b's `tool_conversations.json` held 16 of 18 calls |
| `AtlasAgentRunner._execute_tool_blocks` | writes `action_proposed` for **every** block of the response *before* executing any of them, with `input_digest`, `input_shape`, `input_size`, and `input_ref`/`input_state` when integrity recording is on | the proposal survives the terminal stop — this is the mechanism to reuse |
| `TrajectoryRecorder` | appends synchronously, one line per event | the failing call's proposal and its `execution_outcome` (`state=UNCERTAIN`, `completion_kind=EXECUTED_ERROR`) are durable |
| goal path, P1b | `execution_integrity_mode` left at `off` | every proposal had `input_ref=null`, so only the digest survived |

Root cause: not a missing mechanism, an unselected one.

### D.2 Fix

`run_goal` now builds `ToolPayloadCorpus.for_run(run_dir)` and passes
`execution_integrity_mode="record"` to `AtlasToolRuntime` when `GoalRunOptions.tool_payload_recording`
is true (default). `session["execution_integrity"]` records mode, payload file, record count, write
failures and `"authority": "evidence_only"`.

Result, proven by test (§E): after a run that dies with `side_effect_unknown`, the exact input of the
call that killed it is recoverable from `private/tool_payloads.jsonl` via the `input_ref` in the
transcript, together with call id, tool name, input digest and error classification.

No AgentRunner change, no conversation-store change, no trajectory schema change. `tool_conversations.json`
still stops at the last turn boundary — the corpus is the recovery path, and the audit above says why
that is the minimal correct one.

### D.3 Privacy boundary

* the corpus is written to `<run_dir>/private/tool_payloads.jsonl`, never to `session.json`, never to
  a report, and never into an evidence bundle;
* it holds the **exact** payload, which is the point (digest verification depends on it): a
  `file_write` body or a `bash` command containing a secret would be stored verbatim. It must be run
  through `anycode.security.redaction.redact_text` before any publication, and the directory is
  marked non-publishable in `experiments/stage_b0_controlled/run_stage_b0.py::_collect`;
* it carries no runtime authority: nothing reads it to decide permission, replay or verification;
* the switch can be turned off (`tool_payload_recording=False`), and the evidence then says
  `mode: "off"` rather than reporting an empty corpus as if it were complete.

---

## E. Runtime semantic evidence

`tests/integration/test_reliability_closure_goal_path.py`, 9 tests through the production stack
(`run_goal` → `GoalSandbox` → `SandboxWorkspaceTools` → real `ToolExecutor`, M3 gate, M4 bridge,
`AtlasAgentRunner`); only the model and the sandbox provider are faked.

| Requirement | Test | Assertion |
|---|---|---|
| exit 0 → continue | `test_completed_non_zero_exit_is_feedback_not_termination` (3 turns) | `stop_reason=success`, mode recorded |
| exit 2 → read and continue | same test | 3 model turns, error + exit code in turn 2, one execution |
| P1b reproduced under `legacy` | `test_legacy_mode_reproduces_the_p1b_termination` | 1 turn, `side_effect_unknown`, one execution, legacy recorded |
| no automatic replay | both | bash appears exactly once in the provider's command log |
| transport failure (`ok=False`) | `test_unobserved_transport_failure_still_stops_the_run` | `side_effect_unknown`, `sandbox execution failed` |
| `SandboxLostError` | `test_lost_sandbox_still_stops_the_run` | `side_effect_unknown` |
| M3/R1-1 refusal is non-fatal | `test_pre_execution_denial_stays_non_fatal` | 3 turns, 1 denial (`OWNERSHIP_UNCLASSIFIABLE_SHELL`), command never executed |
| batch with one known failure | `test_batch_with_one_known_failure_keeps_every_result` | both results correlated by `tool_use_id`, error isolated, run continues |
| M4 does not authorise replay | `test_ledger_records_the_completed_error_without_authorising_replay` | `UNCERTAIN` / `EXECUTED_ERROR` / `retry_safe=false`, zero `REPLAYED`, zero `COMPLETED` for the failing command |
| terminating input recoverable | `test_terminating_turn_input_survives_side_effect_unknown` | proposal `RECORDED`, `input_ref` resolves to `{"command": ...}`, digest matches, `execution_outcome` present |
| evidence mechanism removable | `test_payload_recording_can_be_switched_off` | `mode=off`, no corpus dir, `input_ref=null` |

`tests/unit/test_opensandbox_output_fidelity.py`, 15 tests: single line, multi-line, fragment
already ending with `\n`, fragment starting with `\n`, blank fragment, empty output, stderr,
traceback, `git diff`, CRLF, lone `\r`, trailing newline preserved, SDK-format conformance, and the
two adapter-wiring cases.

---

## F. Safety and ledger evidence

* **M4 unchanged.** A completed command error is still `UNCERTAIN` / `EXECUTED_ERROR` /
  `retry_safe=false` (asserted directly against `ledger.db`). The runner continuing and the ledger
  being conservative are deliberately independent — that separation is the G3-1 design, and this
  stage did not touch it.
* **No replay.** Zero `REPLAYED` completion kinds; no row claims `COMPLETED` for the failing
  command; the provider's command log shows one execution per model command.
* **Unknown outcomes still stop the run.** Transport failure and `SandboxLostError` both keep
  `retry_safe=false` in both modes (§E).
* **Ownership.** The goal policy, the R1-1 classifier and the denial reason codes are unchanged; the
  only difference is that a refusal is now read by a Main that survives it.
* **Known G3-1 caveat unchanged:** `retry_safe=True` also marks AnyCode's idempotency record
  `prunable=1`. `prune_completed` has no caller in either repository, so nothing can replay; if a
  future stage enables pruning, this must be re-audited first.

---

## G. Regression

```text
pytest                   1545 tests, 0 failures, 0 errors, 7 skipped, 1 deselected, exit 0
                         (base 1521 + 24 new)
ruff check .             All checks passed!
ruff format --check .    508 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
```

Targeted re-runs, all green: `test_stage_m_main_driven`, `test_stage_p_pipeline` (incl. the sandbox
pipeline), `test_stage_p_boundaries`, `test_p01_preflight`, `test_stage_g3_1_agent_tool_errors`,
`test_sandbox_tool_error_semantics`, `test_opensandbox_provider`, `test_closure1c_request_evidence`,
and the whole `tests/integration/test_anycode_dag.py` suite.

Frozen-artifact check (§A) is part of the acceptance, not a manual step: the diff against
`5865f75` for every stage artefact is empty.

No test was relaxed, skipped or deleted; the two pre-existing timing-sensitive tests were not
touched and did not fail in this run. (`pytest -q --tb=no` can exit non-zero without printing a
failure summary on this host; the JUnit XML is the authoritative record and reports 0 failures.)

---

## H. Stage B0 — controlled real delegation, prepared not run

Harness: `experiments/stage_b0_controlled/` (contract, freeze, runner, README).

### H.1 What it measures, and what it must never claim

It measures the **mechanism**: with a Host-authored instruction that this run validates the
delegation tool, does a real Main call `delegate_tasks`, does the Router admit two independent
read-only children, do their executions overlap, does the Main receive bounded summaries plus refs,
resume its own loop and integrate?

Every artefact is labelled `CONTROLLED_DELEGATION`. It is never `AUTONOMOUS_MAIN_DELEGATION`. Stage
P1b remains the only autonomous sample, and it produced no delegation.

### H.2 What is frozen (`freeze.json`)

| Item | Value |
|---|---|
| Goal digest | `sha256:5995bcaa3395510896017bbd9e43466afc4482ee7387e61a0ee0e4c5285bb483` |
| Controlled instruction digest | recorded separately in the same file (`sha256:` of the instruction paragraph) |
| Fixture | 23 byte digests, **equal** to `experiments/stage_p1b_live/fixture_manifest.json` (computed by the P1b function itself) |
| Public suite / oracle | `p1b_contract.PUBLIC_TESTS` and `p1b_contract.ORACLE_COMMAND`, imported not copied |
| System prompt | `MAIN_SYSTEM_PROMPT`, unchanged (digest checked at preflight and in the attempt marker) |
| Model / budget | `deepseek-chat`, 300 000 tokens, 20 Main turns |
| Delegation | 2 parallel, 2 per call, 6 child turns, 300 s, 4 rounds, 900-char previews |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, network none, 1 CPU / 1 GiB, 20 min |
| Write scope | Main `core/`; children read-only |
| Attempts | exactly 1, marker `<scratch>/attempts/attempt-1.json`, no retry loop |

Preflight (run this round, no model, no provider): **READY** — 19 runtime checks + 9 stage checks,
0 blocking, `delegation_mode=CONTROLLED_DELEGATION`.

### H.3 The instruction

It is appended to the *goal text* (Host-authored, published with the contract, hashed separately) —
not to the system prompt, not to a tool result:

> HOST-AUTHORED EXPERIMENT INSTRUCTION (this run is a runtime validation, not a product request):
> This run validates AtlasRuntime's delegation plane. Before you edit any file, call
> `delegate_tasks` exactly once, with two independent read-only research tasks — one per plugin
> package — each with its own objective and enough background for a worker that cannot see this
> conversation. Then use what they report, do the integration yourself, and run the public test
> suite. This instruction is scaffolding: the run is recorded as CONTROLLED_DELEGATION, not as
> evidence that an unconstrained agent chose to delegate.

### H.4 Fairness conditions

* no fake tool result is injected; the Main's own loop calls the real tool;
* children run through the real `DelegationService` → `ScopedWorkerFactory`;
* the Router is the frozen m.1.0 instance and may still refuse;
* handoffs come from `HandoffStore`, not from assembly;
* there is no second execution loop outside the Main;
* the fixture, public suite and oracle are the P1b ones, unchanged by this round's fixes
  (`test_reliability_closure_goal_path` runs on the *calculator* fixture, so the eventpipe fixture and
  its oracle semantics are untouched).

### H.5 Acceptance

`B0_ACCEPTED_CONTROLLED_LIVE` requires: Main called `delegate_tasks`; Router admitted 2 children in
one batch; `max_observed_concurrency >= 2` with overlapping intervals; both children completed
distinct read-only investigations; the Main read at most the handoffs it needed; the Main made the
integration edit itself; public suite and oracle PASS; context/budget/ownership/sandbox evidence
complete; sandbox destroyed.

If no delegation occurs, the result is `B0_INCONCLUSIVE` and the report must attribute the stop to
Main proposal. A refusal is attributed to Router admission; a child failure to Worker Execution; and
so on. The runner exits 1 for anything but full acceptance — it does not retry.

---

## I. Remaining risks

1. **DEV-3 changes goal-path dynamics.** A failing command no longer ends the run, so a run can
   consume more turns and tokens before stopping. The turn limit (20) and the admission budget
   (300 000) are the only ceilings; neither was changed here. Stage B0 will show whether 20 turns
   suffice for this fixture.
2. **B0 may still produce no delegation.** The instruction is a request, not a guarantee: the model
   may answer it with prose or with one task instead of two. That outcome is informative and must be
   reported as `B0_INCONCLUSIVE`, not worked around.
3. **DEV-2 is structural, not byte-exact** (§C.3): final newline lost, an interior blank line
   collapsed. Anything that needs exact bytes must use the `SandboxEvidence` digest.
4. **`workspace.patch` has no trailing newline** because of (3). Applying it needs
   `git apply --recount`-style tolerance, or a re-export through the digest-verified path.
5. **The payload corpus has no size cap** and stores payloads verbatim (DEV-4). Acceptable for a
   host-local run directory that is deleted with the scratch; a bounded/quota mechanism is a
   separate change and was deliberately not added here.
6. **The G3-1 `prunable` caveat** (§F) still applies: do not enable idempotency pruning without
   re-auditing replay.
7. **Public suite ≠ oracle.** Two of the six oracle variants in the P1b fixture pass the public suite
   and fail the oracle; that remains intentional.

---

## J. Final decision

**`RELIABILITY_CLOSURE_ACCEPTED_OFFLINE`.**

| Criterion | Status |
|---|---|
| 1. Goal Mode wired to G3-1 `corrected` | ✅ `GoalSandboxOptions` + `GoalRunOptions`, recorded in `session.json` |
| 2. Completed non-zero shell result reaches the Main | ✅ exit 2 → 3 turns, error text and exit code in the next request |
| 3. Genuinely unknown outcomes still terminate | ✅ transport failure and `SandboxLostError`, both modes |
| 4. No automatic replay of side effects | ✅ one execution per command, zero `REPLAYED` |
| 5. Ledger semantics and limits stated | ✅ `UNCERTAIN`/`EXECUTED_ERROR`/`retry_safe=false`, asserted from `ledger.db` |
| 6. Output newline fix with SDK contract and evidence | ✅ SDK rule reproduced + conformance test + 12-case real-container probe |
| 7. P1b's exact failure deterministically reproduced and fixed offline | ✅ legacy control test + corrected test on the same fixture and command |
| 8. Full regression | ✅ 1545 tests, 0 failures; ruff and pyright clean |
| 9. Frozen artefacts unchanged | ✅ empty diff against `5865f75` |
| 10. No live DeepSeek call | ✅ none in this round |
| 11. Stage B0 prepared, not executed | ✅ frozen, preflight READY, no attempt marker |

Stopping here: no Stage B0 run, no Stage B benchmark, no further runtime features.
