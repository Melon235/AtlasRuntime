# Stage B-E2B — One-Time WorkGrid Calibration

Status: `CALIBRATION_INFRA_FAILED` / `GOAL_UNKNOWN`.

One attempt was authorised and consumed. It failed at the sandbox health probe before the first
provider request. Zero tokens were billed, no model call was made, and no retry was performed.

---

## A. Git Provenance

| Item | Value |
|---|---|
| Branch | `fix/stage-b-calibration-gate` |
| HEAD at attempt | `a5bd3e451418ff2465afaafee223c02bdd706b76` |
| Worktree before run | clean, local == `origin/fix/stage-b-calibration-gate` |
| Frozen protocol | `stage-b-protocol/1.1` |
| Execution freeze | `stage-b-execution/1.1` |
| Analysis adapter | `stage-b-execution-analysis/1.0` |

The calibration ran from the frozen commit. No source file was modified before, during or after the
attempt. This report, the authorisation record and the redacted evidence subset are the only new
files; a later stage must decide any harness repair and re-freeze.

---

## B. Authorization Record

The task description alone was not treated as authorisation. The owner approved in-session:
one `CALIBRATION.workgrid.c1` attempt, real DeepSeek allowed, 500 000 shared tokens, ceiling
1.00 USD, no retry, no benchmark cell, approver `Owner (Melon235)`.

`experiments/stage_b_calibration/authorization.json`:

| Field | Value |
|---|---|
| `status` | `APPROVED` |
| `approved_by` | `Owner (Melon235)` |
| `approved_at` | `2026-11-20T11:18:12.647516+00:00` |
| `approval_commit` | `a5bd3e451418ff2465afaafee223c02bdd706b76` |
| `cells` | `["CALIBRATION.workgrid.c1"]` |
| `cost_ceiling_usd` | `1.0` |
| `protocol` | `sha256:937f925306659ea7c1db194e7083e5234949edc76566a78974bbbc77983a1585` |
| `plan` | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648` |
| `runtime` | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae` |
| `runner_source` | `sha256:2ad509933456e14eecaa37d892872b4d0b5b7e362076ad3b4095835f3fc85d59` |
| record digest (canonical) | `sha256:47f45f746d8ff4981a1f0752057945c9f9541c373e5b2fb8ddbda30046455b25` |

Every digest was read from the preflight result, not typed by hand. The ceiling is an approval
field only: this runtime has no USD hard stop, and the run must not be described as
currency-enforced.

---

## C. Freeze & Preflight

Before the attempt:

| Check | Result |
|---|---|
| E1 `run_stage_b.py --check` | `READY` |
| Execution preflight (with calibration cell) | `READY`, 16/16 checks |
| Preflight digest | `sha256:485db3fe12348bf776cd91c2d44bbcc1042d91600be0809f294f1b2566e70672` |
| Harness source vs `freeze-1.1.json` | match |
| WorkGrid identity (goal / fixture / public tests / oracle) | `[]` problems |
| Prompt actually recorded in session | `main-v1.0`, `sha256:7e6fd829…` |
| Calibration evidence root markers | none |
| Matrix evidence root markers | none |
| OpenSandbox reachable | `GET /health` 200 via curl |
| Credential present | `DEEPSEEK_API_KEY` loaded from `.env` into the process only |

The OpenSandbox server had to be started inside WSL. Windows could not reach it on
`127.0.0.1:8080` (`networkingMode=mirrored` was not effective), so it was bound to `0.0.0.0` and the
run used `OPEN_SANDBOX_ENDPOINT=172.28.248.27:8080`. That is an environment address, not an
experiment parameter, and it is not part of any frozen digest.

**Preflight gap:** the health check used `curl`, which bypasses the Windows system proxy. The
runtime uses `httpx`, which honours it. The blocking condition below therefore passed preflight.

---

## D. Live Execution Trace

| Event | Time (UTC) |
|---|---|
| Attempt marker claimed | `2026-11-20T11:18:26.534836` |
| Run terminal event | `2026-11-20T11:18:31.954090` |

The run ended after roughly five seconds — the httpx health timeout — with:

```text
status:  failed
failure: sandbox provider 'opensandbox' is unavailable: ReadTimeout
```

Sequence: identity verification → marker claim → fixture materialisation → `run_goal()` →
`GoalSandbox` health probe → `SandboxEnvironmentBlocked`. The provider was never constructed, the
sandbox was never created, no prompt was sent, no tool ran.

Root cause, reproduced afterwards outside the harness:

```text
http://172.28.248.27:8080/health  httpx default (trust_env=True)  -> ReadTimeout, 5.08 s
http://172.28.248.27:8080/health  httpx trust_env=False           -> 200, 0.06 s
curl.exe                          same URL                       -> 200
urllib.request.getproxies()       -> http/https/ftp = http://127.0.0.1:12000
```

Windows has a system proxy at `127.0.0.1:12000` that is not listening. `httpx` reads WinINET proxy
settings through `getproxies()` when `trust_env` is on, so the health probe was sent to a dead
proxy. `curl` ignores WinINET, which is why the manual check passed.

---

## E. Provider Usage

| Field | Value |
|---|---|
| `main_requests` | `0` |
| `main_cumulative_input_tokens` | `null` (UNKNOWN) |
| `main_cache_hit_tokens` / `main_cache_miss_tokens` | `null` / `null` |
| `main_completion_tokens` | `null` |
| `child_*` tokens | `0` |
| `system_total_tokens` | `0` |

No request reached DeepSeek. Billed tokens: none. Estimated cost of this attempt: `0.00 USD`.
UNKNOWN values were not converted to zero except where the runtime genuinely recorded zero child
usage and a zero system total.

---

## F. Sandbox Lifecycle

| Field | Value |
|---|---|
| `provider` | `opensandbox` |
| `provider_is_real` | `true` |
| `created` | `false` |
| `destroyed` | `false` |
| `retained` | `false` |
| `network_probe` | `NOT_RUN` |
| `error` | `null` (the block happened in the health probe, before provisioning) |

Nothing was created, so nothing needed destroying; there is no leaked container. The Sandbox
server itself stayed healthy throughout (`/health` 200 from the host after the failure).

---

## G. Main Tool Calls

`tool_conversations.json` was never written, because the run terminated before the agent loop
started. Transcript evidence contains only a single `run_terminal` lifecycle event
(`status: failed`). Recorded calls: `0`. This absence is an evidence gap, not a zero-call agent
session — the distinction is preserved in `run-report.json`.

---

## H. Delegation Evidence

| Field | Value |
|---|---|
| `router_decisions.json` | `status: NOT_APPLICABLE`, `decisions: []` |
| `worker_outcomes.json` | `workers: []` |
| `handoff_references.json` | `refs: []` |
| `delegation_diagnostics` | `NOT_APPLICABLE` |

No proposal existed, so no router gap is asserted. This is the intended Direct-run behaviour.

---

## I. Evidence Completeness

Collected before the collector crashed:

```text
session.json  transcript.jsonl  attempt-1.json  oracle.json  provider_usage.json
public_tests.json  router_decisions.json  worker_outcomes.json
handoff_references.json  sandbox_lifecycle.json  trajectory/
```

Missing: `tool_conversations.json`, `workspace.patch` (the run never produced them).
`analysis.json` and `evidence_manifest.json` were **not** produced.

The collector raised `FileNotFoundError` while the frozen E1 analyser tried to read
`tool_conversations.json`. That exception was then masked: `runner.execute_cell` evaluates
`_views(session, collection, oracle)` after the `except` block that leaves `collection`
unassigned, so an `UnboundLocalError` escaped and the result was labelled `aggregation_failure`
instead of `collector_failure`. Both are harness defects, recorded as `E2B-D2` and `E2B-D3`.

Evidence integrity is therefore **INCOMPLETE**, not PASS.

---

## J. Host Oracle & Public Tests

| Item | Value |
|---|---|
| Host oracle | `source: UNKNOWN`, `ok: null`, `status: UNKNOWN` |
| Sandbox verification | absent (`session.verification = null`) |
| Oracle agreement | `UNKNOWN` |
| Public test calls | `0` |
| Public test contract | `python3 -B -m pytest -q tests`, digest `ec17b2e8…` |

No oracle ran, from either side, because no workspace ever existed. `GOAL_UNKNOWN` is the honest
result. No substitution from another source was made.

---

## K. Raw Evidence Integrity

The private bundle is at `D:\Work\atlas-b2b\evidence\...` (outside the repository). Its `_raw/`
files were hashed and are recorded in `experiments/stage_b_calibration/evidence-digests.json`
together with published digests:

| Set | Count |
|---|---|
| Raw files (`_raw/`) | 5 |
| Published copies | 14 |

Redactions applied by `collector.scrub_evidence` to the published subset: **none** — no oracle or
reference text was present. An explicit leak scan over the published files for all six oracle and
reference fingerprints returned `[]`. Raw digests were computed after the fact and the raw files
were not rewritten at any point.

---

## L. Recovery

`--recover-calibration` was executed once, read-only:

```json
{"status": "BLOCKED", "ok": false,
 "missing_raw": ["tool_conversations.json", "workspace.patch"],
 "reason": "raw evidence is incomplete; nothing may be inferred"}
```

No provider call, no sandbox, no second agent run. The missing artefacts are kept as gaps. The
attempt marker remains `state: RUNNING` because `mark_attempt` was never reached — recorded as
`E2B-D5`; it was not edited or removed.

---

## M. Matrix Isolation

| Check | Result |
|---|---|
| Calibration marker retained | yes, `attempts/attempt-1.json` |
| Second calibration attempt | rejected at preflight (`attempt marker exists`), zero provider calls |
| Matrix evidence markers | `0` |
| Matrix directories under the evidence root | none (only `calibration/`) |
| Protocol / plan / prompt / runtime digests after the run | unchanged |
| E1 `--check` after the run | `READY` |

The CLI surfaces the refusal as an uncaught `ExecutionBlocked` traceback with exit code 1 instead
of a `BLOCKED` JSON — `E2B-D4`, operator experience only, no safety impact. No matrix cell was
started.

---

## N. Remaining Risks

1. **The single authorised attempt is consumed.** A second attempt requires a new explicit owner
   decision, a new authorisation record and a fresh evidence root. The marker must not be deleted
   or edited to reuse this cell.
2. **Harness defects must be repaired under a controlled change and re-freeze** (`stage-b-execution/1.2`):
   - `E2B-D2` collector must degrade to an evidence gap when `tool_conversations.json` or
     `workspace.patch` is absent, instead of raising.
   - `E2B-D3` `collection` must be initialised before the try so a collector fault is reported as
     `collector_failure`, not `aggregation_failure`.
   - `E2B-D4` preflight refusal should exit 3 with a `BLOCKED` JSON body.
   - `E2B-D5` the marker should reach a terminal state on the aggregation path.
   - `E2B-D6` preflight should probe the sandbox with the runtime's own client (`httpx`,
     `trust_env` honoured), not with an out-of-band HTTP check.
3. **Environment blocker.** `NO_PROXY` must cover the sandbox endpoint (or the WinINET proxy must be
   running/bypassed) for `httpx trust_env` to work. This is an environment fix, not a frozen-parameter
   change.
4. **Endpoint is address-bound.** `172.28.248.27` is a WSL NAT address and will change on reboot;
   a stable endpoint or a port proxy is required for a repeatable run.
5. **No live model behaviour was observed.** Prompt adherence, tool use, budget behaviour, token
   accounting and oracle authority remain unverified against a real provider.
6. **Evidence for this attempt cannot be reconstructed into an analysis record.** It can only be
   summarised from `session.json`; the missing analysis is itself the finding.

---

## O. Final Decision

| Axis | Verdict |
|---|---|
| Execution integrity | **FAILED** — sandbox provider unavailable (ReadTimeout) before any model call |
| Evidence integrity | **INCOMPLETE** — collector crashed; analysis/manifest absent; recovery BLOCKED |
| Coding correctness | **UNKNOWN** — no model run, no public tests, no oracle |

**`CALIBRATION_INFRA_FAILED`**, **`GOAL_UNKNOWN`**.

The infrastructure did not pass: the real provider path was never exercised, the evidence chain is
incomplete, and the failure was partly masked by the collector. No retry was made, no benchmark
cell ran, and no prompt was changed to chase a better result. Stage B-E2B stops here and requires a
controlled harness repair, a re-freeze, and a new owner authorisation before any further live
attempt.
