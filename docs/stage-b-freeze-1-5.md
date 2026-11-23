# Stage B — E2F evidence closure and execution freeze 1.5

Status: `STAGE_B_S03_FREEZE15_PR_SUBMITTED_AWAITING_REVIEW`.
Session: S03. No sandbox was created, no model was called, no calibration or benchmark cell ran.

This stage does three things and nothing else:

1. An independent, read-only audit of the S02 E2F provisioning evidence.
2. A one-observation exception for the exit code the S02 launcher failed to capture.
3. A versioned execution freeze so the next freeze is `1.5` and `1.4` can never be overwritten.

---

## 1. S02 evidence audit

Read-only. The files were not moved, re-generated, or deleted.

| Artefact | Bytes | SHA-256 |
| --- | --- | --- |
| `experiments/stage_b_smoke/e2f-provisioning/attempts/attempt-a22c2b7650af7e33/provisioning-result.json` | 18118 | `6ab3455472c212c5d20bd9ce4319c69a113ae3cd09801c63d8dfc67fd5313ae9` |
| `…/claims/attempt-a22c2b76….json` | 1144 | `5057dee83c1fb5357dd161e9d8acefee2d6992abe7a1ca250b51ceda24f1b054` |

Both match the S02 report byte for byte.

What the audit re-derived from the evidence itself:

| Fact | Recorded |
| --- | --- |
| verdict / status | `PROVISIONING_SMOKE_PASS` / `PASSED` |
| claim state | `COMPLETED`, bound to `sha256:a22c2b76…`, `retries_allowed=false` |
| gate checks | 22 named checks, 22 true, 8 identity gate checks, all true |
| `primary_failure` / `secondary_failures` | `null` / `[]` |
| `unconfirmed` | `[]` |
| sandbox / binding / run id | `835d554a-b360-420d-a593-e70aa92ee10a` / `0061c253…` / `SANDBOX.SMOKE.stage-b-e2f-provisioning.a22c2b7650af7e33` |
| workspace | `pwd=/workspace/project`, HEAD `f7744eaa…`, clean, 21/21 files, all six commands exit 0, leak scan `CLEAN` |
| isolation | `network_probe=PASS`, all three host-separation axes correct |
| cleanup | `DESTROYED`, `residual=false` |
| non-calls | `model_requests=0`, `calibration_attempts=0`, `benchmark_cells=0` |

Two discrepancies are recorded rather than smoothed over:

- **Check count.** S02 reported `23/23`. The evidence carries **22** gate checks plus 8 identity
  gate checks; every one is true and none is missing. The count was off by one, the claim that
  every check passed holds.
- **Claim digest.** The claim file now hashes to `5057dee8…`, while the evidence recorded
  `75cc2863…` at creation time. `mark_claim` rewrites `state`/`updated_at` in place, so the
  difference is the documented state advance, not a tamper signal.

Identity provenance was re-derived from Git, not taken on trust: the ten harness files at
`022c734` reproduce `sha256:cd8f2551…` under the checkout's CRLF rendering, the gate source
reproduces `sha256:c9c5b697…`, and the runtime tree reproduces `sha256:4eebcf1e…` (130 files) by
re-running the harness digest function on the clean worktree.

## 2. Exit code: a single, recorded observation exception

`runner.py` returns `0` if and only if `verdict == PROVISIONING_SMOKE_PASS`, and the recorded
verdict is exactly that. That is a derivation, not an observation, and the stage does not upgrade
it.

The launcher was `Start-Process -PassThru` + `$p.WaitForExit()` + `$p.ExitCode`. The captured file
`stage-b-e2f-022c734.exitcode.txt` (45 B, `sha256:da4d8e2a…`) literally reads:

```text
exit= finished=2026-11-23T16:55:07.4389328Z
```

The exit code was `$null`, stderr is empty (0 B, `sha256:e3b0c442…`), and no other exit-code record
exists in the approvals directory. stdout (926 B, `sha256:7d323f96…`) carries the same gate verdict.

The review record is `experiments/stage_b_smoke/e2f-provisioning/reviews/s03-evidence-review.json`:

| Field | Value |
| --- | --- |
| Gate Verdict | `PASS` |
| Process Exit Code | `UNOBSERVED` |
| Review Disposition | `ACCEPTED_WITH_OBSERVATION_EXCEPTION` |
| Reason | launcher did not capture the OS exit code; no evidence of a gate-level failure |
| Retry | `FORBIDDEN` |
| Original S02 Status | `UNCHANGED` (`STAGE_B_E2F_S02_LIVE_UNKNOWN`) |
| Approved for next offline stage | `YES` |

## 3. The defect this stage fixes

`execution/admission.py` hard-coded both halves of the freeze:

```python
FREEZE_PATH = EXECUTION_DIR / "freeze-1.4.json"     # the write target
...
"freeze_version": "stage-b-execution/1.4",          # the document's own version
```

So `runner --freeze` would have **overwritten the freeze a completed live run was authorized
against**. Nothing calls `--freeze` automatically, and this stage does not call it either.

## 4. What changed

The chain is data, and the pointer is the only thing that moves:

```python
FROZEN_CHAIN: tuple[tuple[str, Path, str], ...] = (   # 1.0 … 1.4, digest-pinned
    ...
)
FREEZE_VERSION = "stage-b-execution/1.5"
FREEZE_PATH = EXECUTION_DIR / "freeze-1.5.json"
```

- `frozen_chain_problems()` re-derives every spent freeze from bytes. `write_freeze()` refuses on
  any mismatch, and so does `preflight()`.
- `write_freeze()` refuses to run at all if the active freeze already exists: a written freeze is
  never rewritten.
- `archive_path(version)` derives the archived duplicate from `ARCHIVE_DIR`, so a test can isolate
  the whole chain in a temp tree with one monkeypatch.
- `freeze_history_problems()` requires the active freeze to name the chain it supersedes, with the
  real digests. A freeze that does not is not live-ready.
- `preflight()` gains three checks: `active_execution_freeze_present`,
  `execution_freeze_chain_intact`, `execution_freeze_supersedes_history`, and one archived-copy
  check per spent version. Only the first is relaxed offline, because that is the state a new
  freeze is written *from*.
- The 1.5 document records the E2F attempt (`provisioning_gate`): status, verdict, evidence digest,
  the 22/22 check count, `process_exit_code=UNOBSERVED`, `retry=FORBIDDEN`, and the review digest.
  The review fields are read from the review file; an absent file records `ABSENT`, never a pass.
- `e2f_provisioning.identity_checks()` now reads the freeze through `active_freeze_document()` so an
  unwritten 1.5 cannot crash a reader. The protocol and plan digests it compares are identical in
  1.4 and 1.5.

### Why the pins are line-ending normalised

This repository checks text out as LF (`* text=auto eol=lf`), but this worktree renders
`experiments/**` as CRLF (`w/crlf`) while `src/atlasos` stays LF. A raw-byte pin would therefore
fail on a fresh clone while the *content* is unchanged. `content_digest()` hashes
CRLF-normalised bytes, so a rendering difference is not treated as a content difference, and any
real edit — including one trailing byte — still fails the chain check. The 1.5 document records
both numbers per version: `digest` (the bytes this worktree rendered) and `content_digest` (the
pin).

## 5. Proving the negatives

`tests/integration/test_stage_b_freeze_1_5.py` (14 tests, offline):

| Property | Test |
| --- | --- |
| the writer targets 1.5, not 1.4 | `test_the_active_freeze_is_1_5_and_1_4_is_the_last_spent_version`, `test_the_freeze_cli_targets_1_5_and_leaves_1_4_alone` |
| 1.4 keeps its bytes | `test_writing_1_5_never_touches_the_bytes_of_1_4` |
| a moved 1.4 is detected | `test_a_moved_1_4_blocks_the_writer_and_the_live_preflight` |
| a written freeze is immutable | `test_an_existing_active_freeze_is_never_rewritten` |
| missing 1.5 is not live-ready | `test_a_missing_1_5_cannot_reach_live_ready` |
| a wrong source digest is refused | `test_a_wrong_source_digest_cannot_reach_live_ready` |
| a broken history chain is refused | `test_a_broken_history_chain_cannot_reach_live_ready` |
| 1.5 declares 1.4's real digest | `test_the_written_freeze_declares_1_5_and_supersedes_1_4_by_its_real_digest` |
| 1.5 does not authorize c3 | `test_the_new_freeze_does_not_authorize_c3`, `test_the_prepared_c3_request_is_not_an_approval` |
| E2F evidence survives a freeze | `test_the_e2f_evidence_is_untouched_by_a_new_freeze` |
| missing evidence stays `NOT_RUN` | `test_a_missing_provisioning_evidence_stays_not_run` |
| the recorded gate matches the bytes | `test_the_recorded_provisioning_gate_matches_the_evidence_bytes` |

`test_stage_b_live_runner.py` keeps describing the execution freeze, with two adjustments: the
active freeze may be absent before it is written, and the archive monkeypatches collapsed into
`ARCHIVE_DIR`.

## 6. c3 is prepared, not authorized

`experiments/stage_b_calibration/authorization-c3.json` is a **request** with
`status: PENDING_OWNER_APPROVAL`. It names the cell, the one-attempt contract, the 500 000-token
budget and the `$1.00` ceiling, and it leaves `runner_source` as `PENDING_FREEZE_1_5`. The gate
refuses it as written: `status`, `approved_by`, `approval_commit`, `approved_at` and
`digest_mismatch:runner_source` are all still blocking.

`CALIBRATION.workgrid.c1` and `c2` remain exactly as their own stages left them, and
`experiments/stage_b_calibration/calibration/` does not exist — c3 has never run.

## 7. Offline verification

| Check | Result |
| --- | --- |
| `pytest` (full offline suite) | 1788 passed, 45 failed, 7 skipped, 1 deselected, 1 xfailed |
| `pyright` | 0 errors, 0 warnings |
| `ruff check` / `ruff format --check` on the touched files | clean |

The 45 failures are **not** assertions in this stage's code. Every one is
`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`, raised by this host's delete guard inside
`tests/helpers/fake_sandbox_provider.py` when a fake sandbox workspace is destroyed; the log
contains 88 occurrences and the guard aborts the test process with `SystemExit(1)`.

That was verified rather than assumed: with the pristine `admission.py` and `e2f_provisioning.py`
restored **in the same worktree and the same rendering**, the E2F file fails the same 14 tests with
the same guard message. The affected files pass when run on their own with a fresh delete budget
(`test_sandbox_ownership.py`, `test_stage_d_cleanup.py`, `test_stage_d_recovery_resume.py`,
`test_verified_reattachment.py`: 64 passed; `test_stage_p_boundaries.py` +
`test_stage_p_pipeline.py`: 11 passed, 1 skipped; `test_stage_b_freeze_1_5.py`: 14 passed;
`test_stage_b_live_runner.py`: 28 passed).

Also pre-existing and unrelated: `ruff check .` reports 66 findings and `ruff format --check .`
11 files, all under `experiments/stage_s1_prompt_v2/**` and `experiments/fixtures/**`. The local
toolchain is ruff 0.17.4 while those artifacts were formatted with the 0.9 line, and the fixture
bytes are part of frozen fixture digests, so they are deliberately not reformatted here.

## 8. What is still open

1. **Freeze 1.5 is not written.** It must be generated from a reviewed, merged tree, on a clean
   worktree, through the versioned writer — not before.
2. **c3 is not authorized.** The request above stays `PENDING_OWNER_APPROVAL`.
3. **The S02 evidence is untracked.** `git status` still shows
   `?? experiments/stage_b_smoke/e2f-provisioning/`. It is evidence, not scratch, and it was not
   cleaned. Whether to commit it is an Owner decision; committing it is also what would let freeze
   1.5 record an empty `dirty_paths`.
4. **The exit code stays unobserved.** The exception is recorded once and does not become a
   precedent for later live stages.
