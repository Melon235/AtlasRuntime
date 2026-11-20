# Stage B-E2A — Benchmark Live Runner Offline Preparation

Status: `STAGE_B_E2A_RUNNER_READY_OFFLINE`.

Branch `exp/stage-b-live-runner`, based on `fix/stage-b-metric-closure` at
`9ba44340aaa92231304e68bd01f940e459f86687`. This stage builds the per-cell executor and its
evidence layer. It does **not** call DeepSeek, start a calibration, or run a scored cell. There is
no `--run` entry point in either runner.

---

## A. Git Provenance

At the start of this session the worktree was clean and HEAD was the E1 freeze chain
(`1138b8d` implementation, `52d4e8d` freeze, `9ba4434` documentation). There was no uncommitted
work to protect, and nothing was reset, cleaned or checked out over existing changes.

`git fetch --prune origin` was attempted directly (the proxy port used in E1 was no longer
listening) and succeeded; `origin/fix/stage-b-metric-closure` matched local HEAD. Only then was
`exp/stage-b-live-runner` created from that verified commit.

All work is under `experiments/stage_benchmark/execution/`, one new integration test file and this
document. `src/atlasos`, the three prompts, the six fixtures, the 36-cell plan, the frozen protocol
and every B0/S1/S2/E1 evidence directory are untouched.

---

## B. Frozen Invariants

Every value below was re-derived from bytes by `admission.preflight()` and the E1 `--check`.

| Invariant | Value |
|---|---|
| Runtime tree | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae`, 130 files |
| Plan | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648`, 36 cells, seed 20261119 |
| Protocol v1.1 | `sha256:937f925306659ea7c1db194e7083e5234949edc76566a78974bbbc77983a1585` |
| Main v1 / v2.0 / v2.1 | `7e6fd829…` / `2bb50b09…` / `4147f19c…` |
| Analysis schema / analyser | `stage-b-result/1.1` / `stage-b-reconstruct/1.1` |
| Execution harness | `sha256:a36b6567d105022c1de49ca6678b3ffafbbadfc445625b7d813a75d0173e1135`, 7 source files |
| Run config | DeepSeek / `deepseek-chat`, 20 main turns, 6 child turns, 2 concurrent children, 3 rounds, 500 000 tokens, corrected tool errors, 4 public-test calls, `research_worker`, router `m.1.0`, OpenSandbox `atlas-uat-python:3.12`, network none, 1 CPU, 1 GiB |

The execution harness has its own digest and its own freeze file
(`experiments/stage_benchmark/execution/freeze.json`). It does not overwrite the E1 freeze. The E1
freeze's protocol digest and plan digest are copied into the execution freeze as references, and
`--check` still validates the upstream artifacts independently.

---

## C. Runner Architecture

Seven modules, one responsibility each:

| Module | Responsibility |
|---|---|
| `models.py` | `CellIdentity`, `Attempt`, `CellRequest`, `CellResult`, the 13 failure kinds |
| `admission.py` | preflight, explicit authorization gate, `O_EXCL` claim, independent freeze |
| `provision.py` | cell identity verification, fresh fixture, `GoalRunOptions`, host oracle, isolation checks |
| `collector.py` | raw evidence copy, derived views, v1.1 analysis, manifest, redaction, recovery |
| `runner.py` | `execute_cell()` orchestration, CLI (`--preflight`, `--offline`, `--freeze`, `--execute-cell`, `--recover-evidence`) |
| `offline.py` | fake provider and 16 synthetic scenarios |
| `__init__.py` | package bootstrap; adds `src` to `sys.path` the way the other experiment runners do |

`runner._run_goal()` is the only import of the real agent entry point. `provision.build_options()`
returns a genuine `GoalRunOptions`, so the live path is composition, not a reimplementation: the
router, WorkerCatalog, WorkerFactory, HandoffStore, tool runtime, budget guardrail and sandbox are
all the frozen upstream ones. The offline seam is a `run_fn` parameter that defaults to
`run_goal`; nothing in the default path knows about fakes.

`runner.py` has no `--run`. `run_stage_b.py` was not edited. The only argument that starts a run is
`--execute-cell CELL_ID`, which additionally requires `--live --confirm-live --authorization FILE`
and returns code 3 without all three.

---

## D. Cell Identity

`provision.cell_from_plan()` recovers `task_id + arm_id + repetition + cell_id` (plus `round` and
`order`) from the frozen plan. A string supplied by the user is only a lookup key; arm and prompt
are never trusted from it.

`verify_identity()` re-derives, per cell: goal text and digest, fixture digest, oracle digest,
prompt version and digest from `protocol.prompt_spec(arm)`, runtime tree digest, plan digest,
router policy (`m.1.0`), worker profile (`research_worker`), model/provider, token budget, main
turns and tool error mode. All 36 cells pass. A mismatch raises `ExecutionBlocked` before any claim
or provider call.

No prompt body is copied into the execution layer. Integration test
`test_no_prompt_body_is_hardcoded_in_the_execution_layer` scans every new source file for the first
120 characters of all three frozen prompts. Each cell materialises its fixture into a fresh
directory with a local git commit (identity set per command, never in global config) and refuses to
touch an existing directory. The run directory is `<root>/<cell_id>/r<repetition>/...`; no two cells
share a mutable workspace.

---

## E. Attempt Safety

`claim_attempt()` writes `<cell>/attempts/attempt-1.json` with
`os.open(path, O_CREAT | O_EXCL | O_WRONLY)` and `fsync`. The claim carries the preflight digest,
the authorization digest and the harness source digest, and explicitly records
`retries_allowed: false`.

Safety properties, each with a test:

- Claim happens after preflight and identity verification, and before provisioning or any provider
  call.
- An existing marker, **or any file in the evidence bundle**, makes `prior_attempt()` refuse.
- The marker is never deleted. A failed run advances its state to `FAILED` in place; it is not
  reissued. Eight threads racing for one cell produce exactly one winner and seven refusals.
- `--freeze` writes only the execution freeze. A test verifies that an existing marker's bytes are
  unchanged after a freeze. Provisioning refuses an existing directory, so "repreparing" a cell
  cannot clear evidence either.
- A live call needs `--live`, `--confirm-live` and an authorization record with status `APPROVED`,
  `approved_by`, `approval_commit`, `approved_at`, the exact cell id, a positive cost ceiling and
  digests equal to the current protocol, plan, runtime and harness. `consumed: true` refuses.
- Without that record `execute_cell(live=True)` raises `ExecutionBlocked` and creates no marker.

Recovery never reissues a claim; it regenerates derived files next to the existing evidence.

---

## F. Evidence Collection

`collector.collect()` runs in three strict phases.

1. **Raw** — copies `session.json`, `tool_conversations.json`, `workspace.patch`, optional
   `tool_state.json` / `workers.json` / `handoffs/` / `private/`, the flattened
   `transcript.jsonl`, the `trajectory/` directory and the attempt marker. Existing targets are
   refused, never overwritten. Anything absent is listed as a gap.
2. **Host views** — derives `router_decisions.json`, `worker_outcomes.json`,
   `handoff_references.json`, `provider_usage.json`, `public_tests.json`, `sandbox_lifecycle.json`
   and `oracle.json` from the session plus the separately collected Host oracle result.
3. **Derived** — writes `analysis.json` from `reconstruct.reconstruct()` + `annotate()` +
   `schema.validate_record()`, carrying `schema_version`, `analyzer_version`,
   `harness_source_digest`, `consistency_checks`, `evidence_gaps`, the cell identity and the full
   v1.1 record; then `evidence_manifest.json` with a digest for every file and the redaction list.

`completeness()` checks the full required list before reporting `ok`. A missing artefact is a gap
and makes the result incomplete; it is never filled with zero or an inferred value. Provider
requests, persisted turns and tool calls stay separate because they are reconstructed by the
frozen analyser.

Before publication, `scrub_evidence()` replaces any oracle source or reference implementation
text found in text artefacts — including the JSON-escaped form — with `[REDACTED_HOST_ORACLE]`.
Redactions are recorded in the manifest and do not alter verdicts.

---

## G. Failure Recovery

`runner._exception_kind()` and `_terminal_kind()` name thirteen outcomes without ever retrying:

| Outcome | Source of truth |
|---|---|
| `none` | succeeded run with a complete bundle |
| `sandbox_creation`, `sandbox_cleanup` | exception text or sandbox lifecycle evidence |
| `provider_exception` | provider exception or unexplained non-success status |
| `budget_exhaustion` | stop reason / rejection code |
| `main_max_turns` | stop reason `max_turns` |
| `router_rejected` | proposals with rejections and no admission |
| `child_failure` | admitted children fewer than succeeded children |
| `handoff_missing` | admitted proposal with no delivered handoff |
| `public_tests_failed`, `oracle_failed` | last suite status / oracle verdict |
| `collector_failure` | collection exception or incomplete bundle |
| `aggregation_failure` | `KeyError` / `TypeError` / `AttributeError` in post-processing |

The B0 failure — a completed run lost to a post-processing `KeyError` — is the reason collection is
separated from execution and why `recover_evidence()` exists. It reads only the existing bundle,
regenerates `analysis.json` and the manifest, and refuses to report success if a raw artefact is
missing. Its CLI form is `python -m experiments.stage_benchmark.execution.runner
--recover-evidence CELL_ID --evidence-root DIR`. It cannot start a sandbox, an agent or a provider:
the only imports on that path are protocol, provision, collector and `reconstruct`.

Every terminal case retains both the marker and whatever evidence was collected. A failed run is
not a signal to try again.

---

## H. Oracle Isolation

`provision.isolation_checks()` re-derives, for each of the six tasks, that the oracle digest is the
frozen one, that no `HIDDEN_OK` / `HIDDEN_FAIL` / oracle marker appears in the goal or fixture, and
that no reference implementation text is present in a fixture file. It runs inside preflight, so a
leak blocks any live start.

The oracle source lives only in `experiments/stage_benchmark/tasks`. Two agents never see it:

- the Main receives the frozen task goal and prompt; neither contains control information;
- the fixture contains only the broken starting code.

`GoalAcceptance.dynamic_command` carries a base64-encoded frozen oracle program and is composed on
the Host; it is not placed in a fixture file or in the goal. It runs during `run_goal`'s
verification phase, after the Main has stopped. Independently,
`provision.host_oracle()` runs the frozen oracle against the exported workspace on the Host and
writes `oracle.json` into the evidence bundle with `executed_after_main_stops: true` and a Host
source tag. The oracle result is never injected back into model context, and redaction runs before
the bundle is published.

---

## I. Calibration Preparation

WorkGrid is registered in `experiments/stage_benchmark/execution/provision.py::CALIBRATION` with
`in_matrix: false` and status `PREPARED_NOT_RUN`. Its cell id is `CALIBRATION.workgrid.c1`, which
preflight explicitly checks is absent from the 36-cell plan. It uses `P1` only as a prompt carrier,
reuses the frozen S2 goal/write-scope/acceptance/public-test contract, copies its own fixture from
`experiments/fixtures/stage_s2_workgrid` into a fresh repo, and uses the existing Host oracle. It
has its own attempt marker under its own evidence path.

Because `verify_identity()` refuses calibration cells that collide with the matrix, and because no
calibration result is written into `protocol.json`, a calibration run cannot be scored into any of
the three arms or consume a matrix cell. `build_options()` returns a real `GoalRunOptions` for it,
so the composed `run_goal()` path is available when an authorised run is eventually requested. No
calibration was executed in this stage.

---

## J. Offline Integration Tests

`execution/offline.py::run_all()` produced `offline.json` with **16/16 cases passed**, zero network
calls:

`direct_pass`, `delegated_two_children`, `router_direct`, `worker_failure`, `handoff_missing`,
`budget_exhaustion`, `tool_denial`, `oracle_fail`, `provider_exception`, `sandbox_lost`,
`collector_crash`, `duplicate_cell_start`, `concurrent_claim`, `evidence_recovery`,
`incomplete_evidence_rejected`, `prompt_arm_injection`.

The first eleven drive the complete `execute_cell()` path with a fake provider and a synthetic
session written into a scratch run directory; the last five check the once-only claim, recovery and
exact arm injection. `tests/integration/test_stage_b_live_runner.py` adds 27 focused tests,
including the 13 failure kinds, authorization refusal, evidence scrub, freeze immutability, the
read-only recovery CLI, and one end-to-end `run_goal()` execution with the repository's scripted
provider and fake sandbox. That last test proves the executor composes the real runtime: the
session is `succeeded`, the sandbox is destroyed once, and the recorded prompt digest equals the
frozen P1 digest.

---

## K. Freeze & Preflight

`preflight.json` has status `READY` with 14 checks. It re-derives protocol v1.1 readiness, the
analysis schema, runtime tree, protocol digest, plan digest, all three prompt digests, all six
tasks' digests, run config, analyser versions, the harness source digest when a freeze exists, the
passing offline report, Host oracle isolation, and the absence of cell markers/evidence conflicts.
With a cell argument it additionally verifies that cell's identity against the plan.

The source-freeze binding is deliberately a **live** rule: an offline suite must be able to rerun
after a harness edit in order to produce the report a new freeze is written from. The live path
passes `require_offline_report=True`, so a stale harness cannot be authorized.

`execution/freeze.json` is `stage-b-execution/1.0`. It records the independent harness digest
`a36b6567…`, the E1 protocol/plan digests, the passing offline report digest, the preflight digest,
the git identity and the calibration state `PREPARED_NOT_RUN`. It never writes to the E1 freeze.

---

## L. Full Regression

| Command | Result |
|---|---|
| `python experiments/stage_benchmark/run_stage_b.py --check` | `READY` |
| `python -m experiments.stage_benchmark.execution.runner --offline` | `READY`, 16/16 cases |
| `python -m experiments.stage_benchmark.execution.runner --preflight` | `READY`, 14 checks |
| `python -m experiments.stage_benchmark.execution.runner --freeze` | written independently |
| `pytest tests/integration/test_stage_b_live_runner.py` | 27 passed |
| `pytest tests/integration/test_stage_b_protocol.py` | 29 passed |
| `pytest tests/integration/test_stage_b_metric_closure.py` | 45 passed |
| `pytest tests/integration/test_stage_s1_main_prompt.py …test_stage_s2_analysis.py` | passed |
| `pytest` (whole suite) | **1678 tests, 0 failures, 0 errors, 7 skipped**, 321.5 s |
| `pyright` | 0 errors, 0 warnings |
| `ruff check experiments/stage_benchmark/execution tests/integration/test_stage_b_live_runner.py` | clean |
| `ruff format --check` on the same files | clean |
| `ruff check .` | 66 errors — all pre-existing, none in new files |
| `ruff format --check .` | 11 files — all pre-existing, none in new files |

The two repository-wide Ruff results match the E1 baseline exactly. Those 66/11 findings live in
frozen `experiments/stage_s1_prompt_v2/**` and S1 fixture files; this stage did not touch them, and
does not describe a scoped pass as a repository-wide pass.

No original assertion was deleted, weakened or skipped.

---

## M. Remaining Risks

1. **No live evidence.** The whole harness is validated against synthetic sessions and one scripted
   `run_goal()`. The first real cell may reveal a field the collector does not map; that will be a
   recorded evidence gap, not an inferred value.
2. **Fake sandbox fidelity.** The scripted provider executes tools locally. Shell-command and
   resource-limit behaviour in real OpenSandbox is still unverified by this stage.
3. **Oracle command placement.** The base64-encoded command is executed by `run_goal` in the
   sandbox during verification, after the Main stops. The separate Host oracle remains the
   authoritative evidence. If a future design decides that no oracle may ever execute in a sandbox,
   `GoalAcceptance.dynamic_command` must be disabled and the Host result fed through a versioned
   protocol change — not silently substituted.
4. **Live authorization format is new.** It has no real signed approval yet; the gate validates
   structure and digests, not a signature. An owner must supply the record.
5. **Recovery writes derived files.** It is read-only with respect to raw evidence and the
   provider, but it does regenerate `analysis.json` and the manifest. Re-running it after a harness
   change can change the derived digest, which is intentional and recorded.
6. **Pre-existing lint failures** remain exactly as in E1 (`ruff check .` 66, format 11). CI would
   still be red on those frozen files.
7. **No calibration or benchmark was run.** Every `status` in this document is an offline status.

---

## N. Final Decision

`STAGE_B_E2A_RUNNER_READY_OFFLINE`.

The executor is built, the once-only claim is atomic and durable, evidence collection and recovery
are separated from the model path, the oracle is Host-owned and redacted, the calibration is
matrix-external and unrun, and an independent freeze records the harness identity. Frozen runtime,
prompts, tasks, plan and protocol v1.1 digests are unchanged. The only non-green check in the
repository is the pre-existing Ruff condition described in `M.6`.

---

## O. Calibration Readiness

Ready when an owner supplies an explicit authorization record: the WorkGrid cell has its own
marker, its own evidence path, a real composed `GoalRunOptions`, the frozen S2 goal/oracle and a
preflight gate. It remains `PREPARED_NOT_RUN`.

Still required before any live action, in order:

1. A written budget ceiling and authorization record with digests matching the final freeze.
2. One authorised WorkGrid calibration (`calibration.workgrid.c1`), outside the matrix, to prove
   the real sandbox and provider produce a complete evidence bundle.
3. A post-calibration preflight confirming zero cell markers in the 36-cell evidence root.
4. Only then, separate authorization for any scored cell.

This stage stops at offline readiness. No DeepSeek session, calibration, benchmark cell or live
runner was started.
