# Closure-2 S08 — Dispatch Integrity Closure

Status: `CLOSURE2_S08_DISPATCH_INTEGRITY_READY_FOR_REVIEW`. Offline repair only: no model request, no
sandbox, no canary cell. The approved seven-cell canary did **not** run and its claim is still unspent.

| Item | Value |
|---|---|
| First reviewed source | `dfd60cd406b81a6f085d3b1bb4dd05a2384cd373` |
| Patch reviewed here | `f8283332c6600bd038a43882e3b766fdc9f16b08` plus the closure patch in this commit |
| Branch | `feat/closure-2-s07-preparation` |
| Canary revision 1 | `closure2-s08-canary-1`, digest `f3d09a85…`, **superseded**, never executed, never claimed |
| Canary revision 2 | `closure2-s08-canary-2`, digest `24235034a05651935daf19eee5c3d96d255911b64cd04564b223d0fb6af6b14f`, `PENDING_OWNER_APPROVAL` |
| Frozen preregistration | `3080f313…` — unchanged |
| Candidate budget profile | `b41a633d…` — unchanged |
| S06 / PR #6 | `e0bc2cc`, untouched |

---

## 1. Root cause

`run_live_pilot()` decided "may the next cell start" with exactly three gates: the cost state, the
pre-execution ledger write and the post-execution ledger write. **None of them reads the record the
engine just produced.** A dispatch loop needs a second question answered — not only "can we still
afford it" but "is another cell still meaningful" — and nothing asked it.

Both engines return a cleanup failure as an ordinary result rather than an exception
(`runner.py:2401-2420`, `native_baseline.py:420-430`): `failure_class="CLEANUP_FAILURE"`,
`metrics.sandbox_destroyed=False`, cost known, `status="COMPLETED"`. `_cost_dispatch_state()` saw a
known cost and cleared the next cell. Measured offline on the approved plan itself: a cleanup failure
on every cell dispatched **7 of 7**, with `stopped_reason = null`.

`raw_evidence_preserved` made the same class of mistake in miniature: it was a constant `True`, a
claim about a file nobody opened.

## 2. The gate

`pilot_runner._dispatch_integrity_block(record)` — one pure function over fields `normalize_result`
already emits, evaluated after the `COMPLETED` row is written and before the next iteration's cost
gate. Fail-closed: a record passes only if every check passes.

| Check | Passes when | Reason code when it does not |
|---|---|---|
| failure class | `TASK_SUCCESS`, `ORACLE_FAILURE`, `BUDGET_EXCEEDED` | `SANDBOX_CLEANUP_UNCONFIRMED`, `ENVIRONMENT_FAILURE`, `SANDBOX_FAILURE`, `PROVIDER_FAILURE`, `RUNTIME_FAILURE`, `EXECUTION_TIMEOUT`, `HOST_ISOLATION_VIOLATION`, `WORKSPACE_POLICY_VIOLATION`, `NOT_EVALUATED`, or `UNCLASSIFIED_FAILURE` for anything else |
| sandbox | created, then destroyed | `SANDBOX_LIFECYCLE_UNKNOWN`, `SANDBOX_NOT_CREATED`, `SANDBOX_CLEANUP_UNCONFIRMED` |
| oracle | no environment fault | `ORACLE_ENVIRONMENT_FAILURE` |
| numbers | cost `COMPLETE`, both token counts legal non-negative integers | `COST_UNKNOWN`, `COST_INVALID`, `TOKEN_USAGE_UNKNOWN`, `TOKEN_USAGE_INVALID` |
| evidence | `result.json` is a non-empty, parseable JSON **object** | `EVIDENCE_MISSING`, `EVIDENCE_EMPTY`, `EVIDENCE_CORRUPT`, `EVIDENCE_NOT_AN_OBJECT`, `EVIDENCE_UNREADABLE`, `EVIDENCE_UNKNOWN` |

`provider_usage_complete` is deliberately **not** required: both engines set it to `None` because
they cannot attest request-level completeness, and demanding it would block every real run. That is an
observation limit, not a missing number.

The one exemption is narrow and has to be *proved*, never inferred from the class: a
`BUDGET_EXCEEDED` cell whose usage is missing continues only when the runner counted zero requests
(`model_calls == 0` **and** `model_calls_source == "request_evidence.requests_observed"`), counted at
least one pre-send admission rejection, carries no token spend, and reports a known zero cost. A
`BUDGET_EXCEEDED` class with a known cost proves nothing about whether a request left the process, so
that combination stops. Native has no admission counter at all, so a native budget stop with unknown
usage always stops.

## 3. Normal failure versus infrastructure failure

| Class | Meaning | Dispatch |
|---|---|---|
| `TASK_SUCCESS` | the deliverable passed | continue |
| `ORACLE_FAILURE` | the deliverable was graded and failed | continue — this is the benchmark's normal negative result |
| `BUDGET_EXCEEDED` | the budget was enforced and measured | continue |
| `TIMEOUT`, `MODEL_FAILURE`, `ENVIRONMENT_FAILURE`, `SANDBOX_FAILURE`, `RUNTIME_FAILURE` | the cell did not produce a usable observation | stop |
| `CLEANUP_FAILURE` | the cell's sandbox is still out there | stop |
| `HOST_ISOLATION_VIOLATION`, `FORBIDDEN_WORKSPACE_CHANGE` | the isolation contract was broken | stop |
| `NOT_EVALUATED`, anything unknown | cannot be trusted as an observation | stop |

An ordinary oracle failure is never converted into an infrastructure failure, and an infrastructure
failure is never recorded as a task outcome: `oracle_passed`, `agent_success`, `task_success` and
`failure_class` keep the values the engine produced.

## 4. Integration, and what a stop means

Order per cell: dispatch → real `EvalRunResult` → `normalize_result` → `COMPLETED` ledger row →
integrity gate → (next iteration) cost gate → next cell.

The exception branch is closed the same way. A raising cell keeps its `EXECUTION_ERROR` record, its
`FAILED` ledger row is written first (a ledger failure there keeps its own `BLOCKED_LEDGER_POST_EXECUTION`
handling), and the plan then stops **directly** with `stopped_reason = "EXECUTION_ERROR"` and
`dispatch_integrity.source = "EXECUTION_ERROR"` — it is no longer left to the next cost gate to notice
an unmeasurable spend. On the last cell the same path sets `is_last_cell = true`, `not_started_cells`
stays empty and the CLI exits `4`.

A blocked cell keeps its `COMPLETED` row: the ledger says the cell returned, not that the run was
sound. The plan claim stays `CONSUMED`; cells that never started keep their `CLAIMED` row (the claim
reserves all seven up front), so a stop leaves auditable "reserved, never dispatched" evidence rather
than a hole. Nothing is deleted, rewritten, retried or resumed.

The last cell has no next dispatch, so the stop is recorded in the summary instead:
`execution_integrity_status = "BLOCKED_DISPATCH_INTEGRITY"`, `stopped_reason`, `not_started_cells`
(empty), and `dispatch_integrity.is_last_cell = true`. The CLI already returns `4` for any non-
`COMPLETE` integrity, so a blocked matrix cannot exit `0`.

One field was added to the summary (`dispatch_integrity`), plus `raw_evidence_state` /
`raw_evidence_bytes` on each cell record. The ledger is unchanged.

## 5. Evidence is now observed

`normalize_result` opens the file the record points at and validates four things: it exists, it is a
regular readable file, it is non-empty, and it parses as a JSON **object**. It records:

* `raw_evidence_state`: `PRESENT` / `MISSING` / `UNREADABLE` / `EMPTY` / `CORRUPT` / `NOT_AN_OBJECT`;
* `raw_evidence_bytes`: the size, when the file could be read at all;
* `raw_evidence_preserved`: `true` only for `PRESENT`.

It never writes, re-creates or repairs a result. An execution-error record reports `NOT_PRODUCED` and
`raw_evidence_preserved = false`, because no result exists. Empty, truncated, non-UTF-8 and
non-object documents are distinct failures with distinct reason codes, and each one stops the plan
instead of being asserted away. `raw_evidence_preserved` is derived from the observation; the
field no longer exists as a constant.

## 6. Offline negative controls

Real `run_live_pilot()`, real SQLite ledger in a scratch directory, fake executor, fake run dirs that
carry a real, parseable `result.json`. 76 tests in
`tests/unit/test_closure2_dispatch_integrity.py`.

| Scenario | Dispatched | `stopped_reason` |
|---|---|---|
| every cell sound | 3 (and 7 of 7 on the canary shape) | `null` |
| oracle FAIL, evidence complete | 3 | `null` |
| measured `BUDGET_EXCEEDED` with legal usage | 3 | `null` |
| `BUDGET_EXCEEDED` with attested zero consumption | 3 | `null` |
| `BUDGET_EXCEEDED` with unknown usage, no proof | 1 | `TOKEN_USAGE_UNKNOWN` |
| executor raises on cell 1 / 2 / 3 | 1 / 2 / 3 | `EXECUTION_ERROR` |
| executor raises on the last cell, via the CLI | 3 | `EXECUTION_ERROR`, exit `4` |
| illegal token count (`-1`, `True`, `"1000"`) | 1 | `TOKEN_USAGE_INVALID` |
| cleanup failure on cell 1 | 1 | `SANDBOX_CLEANUP_UNCONFIRMED` |
| cleanup failure on cell 2 | 2 | `SANDBOX_CLEANUP_UNCONFIRMED` |
| cleanup failure on the last cell | 3 | `SANDBOX_CLEANUP_UNCONFIRMED`, `is_last_cell = true` |
| sandbox creation unknown | 1 | `SANDBOX_LIFECYCLE_UNKNOWN` |
| sandbox never created | 1 | `SANDBOX_NOT_CREATED` |
| oracle environment failure | 1 | `ORACLE_ENVIRONMENT_FAILURE` |
| host isolation violation | 1 | `HOST_ISOLATION_VIOLATION` |
| timeout / provider failure | 1 | `EXECUTION_TIMEOUT` / `PROVIDER_FAILURE` |
| known cost, tokens unknown | 1 | `TOKEN_USAGE_UNKNOWN` |
| `result.json` missing | 1 | `EVIDENCE_MISSING` |
| `result.json` unreadable | 1 | `EVIDENCE_UNREADABLE` |
| `result.json` empty | 1 | `EVIDENCE_EMPTY` |
| `result.json` truncated | 1 | `EVIDENCE_CORRUPT` |
| `result.json` is a JSON array | 1 | `EVIDENCE_NOT_AN_OBJECT` |
| cost reaches / crosses 0.45 | 2 | `COST_CEILING_REACHED` / `COST_CEILING_EXCEEDED` (integrity stays `COMPLETE`) |
| same plan digest twice | 3 then 0 | `PilotAlreadyClaimed` |
| canary shape, blocked at cell 1 / 4 / 7 | 1 / 4 / 7 | `SANDBOX_CLEANUP_UNCONFIRMED`, rest `CLAIMED` |
| CLI with a blocked cell | — | exit `4`, summary written |

Asserted each time: dispatched count, ledger states, `stopped_reason`, `not_started_cells`, integrity
status, claim status.

## 7. Revision identity

The repair changes what a cell may be followed by, so the runner revision inside the plan payload
moves and the digest with it. Revision 1 is not edited — plan, digest, scope and record keep their
values, and its record's status moves from `PENDING_OWNER_APPROVAL` to `SUPERSEDED_BY_REVISION` with a
pointer, so it cannot be signed into a run by accident (it never was approved or claimed).

| | revision 1 | revision 2 |
|---|---|---|
| plan id | `closure2-s08-canary-1` | `closure2-s08-canary-2` |
| runner revision | `closure-2-s08-canary-1` | `closure-2-s08-canary-2` |
| digest | `f3d09a85…` | `24235034a05651935daf19eee5c3d96d255911b64cd04564b223d0fb6af6b14f` |
| approval | `SUPERSEDED_BY_REVISION` | `PENDING_OWNER_APPROVAL` |
| claim | never taken | never taken |

Everything else is byte-identical: the seven cells and their order, repetition 0, one attempt, the USD
0.45 candidate stop, the `b41a633d…` budget profile, the frozen preregistration digest, the benchmark
id. `supersession_record()` states that the authorization does **not** migrate.

The dispatch-integrity closure changes runner *behaviour* but not the plan payload, so revision 2's
digest is unchanged by it — re-verified after the patch, still
`24235034a05651935daf19eee5c3d96d255911b64cd04564b223d0fb6af6b14f`. What changes is the source SHA an
authorization should name: it now has to name the commit that carries this patch.

Revision 2 is registered with the dispatcher so "not authorized" stays a gate decision, and both
revisions have their own summary filename and run root. Formal plans are untouched; their identity is
frozen at S10.

## 8. Verification

| Check | Result |
|---|---|
| Closure-2 unit + integration | **518 passed**, 2 failed |
| Dispatch-integrity tests | 76 passed |
| Revision tests | 11 passed |
| Preflight | `PASSED`, 62 checks, `3080f313…` |
| Ruff (all eight touched files) | clean, `format --check` clean |
| Pyright | 0 errors on the configured scope and on the new modules; `pilot_runner.py:237` remains the one pre-existing error in a line this repair did not touch |
| `src/` | unchanged |
| S06 custody | untouched |

The two failures are the known environment defect in
`tests/integration/test_closure2_preflight.py::test_r2_03_each_pytest_shape_is_classified_and_gated`
(`[assertion]`, `[mixed_failure_and_setup_error]`): this machine's pytest output no longer matches that
test's classifier. They reproduce at `e0bc2cc` and predate S08; the full suite is not green and is not
reported as green.

## 9. Pending

1. **Owner decision** between signing revision 2 (`24235034…`) or rejecting the revision and re-scoping.
2. The authorization commit must name both that digest and the source SHA it was reviewed against;
   the shipped record is pending and the gate refuses it.
3. A transient provider failure — or any exception, including one on the last cell — now stops the
   canary instead of continuing. That is the approved boundary, and it means one attempt may be
   consumed by an infrastructure hiccup rather than by an experiment.
4. The zero-consumption exemption is only reachable if the Atlas engine actually reports
   `requests_observed == 0` together with a pre-send rejection and a zero cost. If it never does, the
   exemption simply never fires, which is the safe direction; a native budget stop with unknown usage
   always stops.
