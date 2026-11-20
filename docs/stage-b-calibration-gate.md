# Stage B-E2A Closure — Calibration Gate Hardening

Status: `STAGE_B_CALIBRATION_GATE_READY_OFFLINE`.

Branch `fix/stage-b-calibration-gate`, based on `exp/stage-b-live-runner` at
`2dcf840796626e7f793c1d2e073ef6a12ded8627`. This stage fixes how the offline executor *judges* a
run and *stores* its evidence before WorkGrid calibration is allowed. No model call, no
calibration, no scored cell.

---

## A. Git Provenance

At session start the worktree was clean at the expected E2A head. `fix/stage-b-calibration-gate`
was created from that verified commit; nothing was reset, cleaned or overwritten.

Changes are confined to `experiments/stage_benchmark/execution/`, its integration tests, a new
versioned freeze and this document. `src/atlasos`, prompts, fixtures, goals, oracles, Protocol v1.1,
the 36-cell plan and all B0/S1/S2/E1/E2A evidence are unchanged. The E2A freeze is preserved
byte-for-byte and archived rather than overwritten.

---

## B. Frozen Invariants

| Identity | Digest |
|---|---|
| Runtime tree (130 files) | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae` |
| Protocol v1.1 | `sha256:937f925306659ea7c1db194e7083e5234949edc76566a78974bbbc77983a1585` |
| Plan (36 cells) | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648` |
| E1 freeze | `sha256:a8dac818908da6c86f3b33008bb01e8a8a931136b5e03aa2c33a974979150c01` |
| E2A freeze (archived) | `sha256:7bf630e55dfd1cb8fbf15bd8a2a40cb0ee001987d6bf3c68ad527144c8e5a7e6` |
| Execution harness v1.1 (8 files) | `sha256:2ad509933456e14eecaa37d892872b4d0b5b7e362076ad3b4095835f3fc85d59` |

`preflight` re-derives all of them. The E1 analyser stays `stage-b-reconstruct/1.1`; the new
overlay is a separate, versioned adapter (`stage-b-execution-analysis/1.0`) and never edits an E1
field.

---

## C. Calibration Entry

`runner.py` adds `--execute-calibration` and `--recover-calibration`. The entry resolves
`provision.calibration_cell()` (`CALIBRATION.workgrid.c1`, `P1`, `in_matrix=False`) and refuses to
look in the plan for it. It requires the same `--live --confirm-live --authorization` triple as a
matrix cell; the same preflight, byte-level identity verification, cell-scoped digest-bound
authorization and O_EXCL claim run unchanged inside `execute_cell()`. No safety check is bypassed.

Calibration uses `evidence_root/calibration` while matrix cells use `evidence_root`, so a completed
calibration cannot occupy a matrix marker or evidence path. `verify_identity()` for a calibration
cell now re-derives the S2 freeze's goal text and digest, every fixture file digest, public-test
contract and command digest, `HIDDEN_PROGRAM` and `ORACLE_COMMAND` digests, and the write scope.
`isolation_checks()` additionally checks that the calibration goal carries no oracle marker.

The Host oracle path was extended to WorkGrid: `host_oracle()` executes the frozen S2
`HIDDEN_PROGRAM` on the Host, after the Main stops, and tags the result with its Host source. A
test confirms the calibration cell is absent from the 36-cell plan and that a matrix preflight stays
`READY` after a calibration marker exists.

---

## D. Execution / Goal / Delegation Separation

The old `_terminal_kind()` promoted `route="direct"` to `router_rejected` and any incomplete child
to `child_failure`, even when the Main recovered and the oracle passed. It has been replaced by a
three-axis verdict:

| Axis | Source | Sets the terminal kind? |
|---|---|---|
| Execution integrity | sandbox lifecycle, provider status, collector result, budget/turn stops | yes |
| Goal correctness | Host oracle (authoritative) + public tests + advisory sandbox verification | yes |
| Delegation diagnostics | router decisions, child outcomes, handoff delivery | **no** |

Delegation results appear as `delegation_diagnostics` on `CellResult` and in the versioned
`execution_view` (`ROUTER_REJECTED`, `child_failure`, `handoff_missing`, or `NOT_APPLICABLE` on a
Direct run). They can no longer turn a passing, correctly executed cell into a failure, and they can
no longer hide a failing oracle. A goal failure never masks a collector or sandbox fault, because
integrity is evaluated first. `goal_unknown` is a named terminal kind; an UNKNOWN is not a pass.

---

## E. Host Oracle Authority

`collector.derive()` no longer copies `session.verification` into `oracle.json`. A missing Host
result is written as `{"ok": null, "source": "UNKNOWN", "status": "UNKNOWN"}` with an explicit gap;
nothing silently stands in for it. `adapter.host_status()` accepts only results tagged `host:` (or
`fake:host` in offline tests) as authoritative.

`execution_view.goal_correctness` records `host_oracle`, `sandbox_verification`, `public_tests`,
`authoritative_source`, `oracle_agreement` and the final `status`. `AGREE` or `ORACLE_DISAGREEMENT`
is computed between the two oracle sources and the mismatch is also written into `evidence_gaps`.
Final Goal Correctness uses the Host oracle for the oracle component and requires public tests to
pass; a runner that exits cleanly with a failing Host oracle is `oracle_failed`, never `none`. The
same path applies to WorkGrid. The oracle source stays on the Host and is never put in the goal,
tool results or fixture; redaction runs before publication.

---

## F. Evidence Immutability

Raw artefacts are copied once into the private `_raw/` directory and hashed. The published bundle
holds byte-for-byte copies of those artefacts; only the published copies are scrubbed.
`manifest.json` now records `raw_digests` / `raw_file_count` and `files` / `file_count`, with
`immutable_raw: true` and the redaction list. A test proves the raw `session.json` bytes are
unchanged by scrubbing and that the published copy loses the oracle text.

`recover-evidence` and `recover-calibration` never start a provider, sandbox or agent. Recovery
restores missing *published* copies from raw, regenerates `analysis.json` and the manifest with
`analysis_version` and `harness_source_digest`, and verifies before/after raw digests. Missing raw
evidence returns `BLOCKED`; it is never fabricated. A Direct run has no proposal, so
`router_decisions.json` records `NOT_APPLICABLE` and produces no router evidence gap.

---

## G. Once-only Protection

Two O_EXCL markers were tested end to end: a second calibration is refused, and a failed
post-processing attempt leaves `retries_allowed: false` and refuses any second provider call. The
matrix root remains free of markers after a calibration. `--freeze` writes only
`freeze-1.1.json` plus the archived `archive/freeze-1.0.json`; it never touches a cell directory.
The live path still requires the explicit authorization record and matching digests.

Freeze ordering rule: source edits require a fresh `--offline`, then `--preflight`, then a new
versioned `--freeze`. Preflight binds the harness digest to the freeze in live mode; an offline
suite may rerun after a source edit so it can generate the report the next freeze is written from.

---

## H. Offline Tests

`offline.json`: **23/23 cases passed**, zero network calls. The original 16 cases remain; seven
new ones cover router-direct recovery, child-failure recovery, child failure with oracle FAIL,
provider success with a FAILING Host oracle, oracle disagreement, missing Host oracle, and a Direct
run with no router gap.

`tests/integration/test_stage_b_calibration_gate.py` adds 14 counterexamples covering the
sixteen required scenarios (published redaction is asserted inside the raw-immutability test):

1. calibration entry / cell identity; 2. calibration outside the 36-cell plan;
3. no authorization cannot enter the live path; 4. Direct recovery + PASS;
5. child failure recovery + PASS; 6. child failure + oracle FAIL;
7. provider success + Host FAIL; 8. sandbox/Host disagreement;
9. missing Host oracle; 10. no false router gap on a Direct run;
11. raw SHA-256 unchanged by scrub; 12. published evidence contains no oracle text;
13. recovery does not modify raw evidence; 14. duplicate calibration blocked;
15. calibration marker does not affect a matrix cell; 16. post-processing exception cannot cause
a second model call.

The E2A freeze test was updated to accept the versioned 1.1 freeze **and** assert that the 1.0
original is archived with a matching digest. No old assertion was dropped or weakened.

---

## I. Full Regression

| Command | Result |
|---|---|
| `python -m experiments.stage_benchmark.execution.runner --offline` | `READY`, 23/23 cases |
| `python -m experiments.stage_benchmark.execution.runner --preflight` | `READY`, 15 checks |
| `python -m experiments.stage_benchmark.execution.runner --freeze` | `FROZEN`, `stage-b-execution/1.1` |
| E1 `run_stage_b.py --check` | `READY` |
| `pytest tests/integration/test_stage_b_calibration_gate.py` | 14 passed |
| `pytest tests/integration/test_stage_b_live_runner.py` | 27 passed |
| E1 Protocol + Metric Closure tests | 29 + 45 passed |
| `pytest` (whole suite) | **1692 tests, 0 failures, 0 errors, 7 skipped**, 356.0 s |
| `pyright` | 0 errors, 0 warnings |
| Ruff check / format on changed files | clean |
| `ruff check .` | 66 errors — all pre-existing, none in new files |
| `ruff format --check .` | 11 files — all pre-existing, none in new files |

Repository-wide Ruff still fails on frozen Stage S1 artefacts at the E1 baseline. Those files were
not modified and a scoped pass is not presented as a repository-wide pass.

---

## J. Remaining Risks

1. **No live calibration.** All oracle results here are fake or synthetic. The first real
   WorkGrid run could still reveal a provider/sandbox field the collector does not map; that will
   be an evidence gap, never an inferred value.
2. **Oracle disagreement policy is pre-registered but not live-tested.** The Host result wins Goal
   Correctness; a disagreement is recorded, not silently averaged.
3. **Public tests and Host oracle must both pass.** A Host PASS with failing public tests is not a
   goal pass; this is deliberate and differs from reading only `session.verification`.
4. **Recovery rewrites derived files.** It never touches raw evidence or calls a model, but
   re-running it after a harness change changes the derived digest, which is recorded.
5. **The E2A freeze remains as an archived original.** Consumers that hard-code `freeze.json` for
   the harness digest must move to `freeze-1.1.json`; preflight already does.
6. **Pre-existing Ruff failures** remain exactly as at E1; CI will still be red on frozen S1 files.

---

## K. Final Decision

`STAGE_B_CALIBRATION_GATE_READY_OFFLINE`.

The calibration entry exists and is guarded, execution integrity / goal correctness / delegation
diagnostics are independent verdicts, the Host oracle is authoritative with disagreement and
missing-evidence handling, raw evidence is immutable with a separately redacted publication, and
the independent freeze is versioned with the E2A original preserved. Frozen runtime, prompts,
tasks, plan and Protocol v1.1 digests did not move. No calibration or benchmark cell was run.

Calibration remains `PREPARED_NOT_RUN` and still requires an owner's written budget ceiling and an
authorization record with matching digests before it can be started.
