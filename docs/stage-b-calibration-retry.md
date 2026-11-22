# Stage B-E2C — OpenSandbox Proxy Fix and Controlled Calibration Retry

Status (Phase A): `STAGE_B_E2C_RETRY_READY_OFFLINE`.

Phase B (the live retry) has **not** run. It still requires a new, explicit owner authorization; the
authorization consumed in Stage B-E2B is not reused.

---

## A. Git Provenance

| Item | Value |
|---|---|
| Branch | `fix/stage-b-calibration-retry` |
| Base | `f899a29efcaf9606f8822eb5ed95c39e66df9946` (`fix/stage-b-calibration-gate`) |
| Harness commit | `9f33ca7103dbd7c6df1a7dc7e4ec865e9ce34f20` |
| Worktree at freeze | clean, zero dirty paths |
| Protocol | `stage-b-protocol/1.1` |
| Execution freeze | `stage-b-execution/1.2` |
| Execution analysis | `stage-b-execution-analysis/1.1` |

No frozen runtime, prompt, fixture, oracle or plan file was modified. The E2B report, its
authorization and its evidence are untouched.

---

## B. Previous Calibration Failure Evidence

`CALIBRATION.workgrid.c1` remains exactly as recorded in `docs/stage-b-calibration-run.md`:

| Fact | Value |
|---|---|
| Attempts | 1, consumed |
| Marker | present, `state: RUNNING`, `retries_allowed: false` |
| Provider requests | 0 |
| Sandbox created | false |
| Tokens | `null` / `0` |
| Host Oracle | UNKNOWN |
| Verdict | `CALIBRATION_INFRA_FAILED` / `GOAL_UNKNOWN` |

Its marker hash was re-checked during this stage and still matches the committed copy
(`DEAEF36A…751C6FB`). Nothing about c1 was rewritten.

---

## C. Proxy Root Cause

Two independent conditions had to be true for the E2B failure:

1. Windows publishes a WinINET system proxy at `http://127.0.0.1:12000`. Python's
   `urllib.request.getproxies()` reads it from the registry, and httpx honours it by default
   (`trust_env=True`). `curl` does not, which is exactly why the manual health check passed while
   the runtime probe timed out.
2. Windows could not reach the WSL server on `127.0.0.1:8080`; the server had been bound to
   `0.0.0.0` and was reachable only at the WSL address.

The fix therefore is not "disable the proxy". It is: **derive the sandbox host from the current
endpoint and add only that host to `NO_PROXY`**, leaving `HTTP_PROXY` / `HTTPS_PROXY` untouched so
DeepSeek keeps its existing path.

---

## D. Windows / WSL Network Diagnostics

Measured before the fix:

```text
Windows 127.0.0.1:12000            listened (::)
Windows 127.0.0.1:8080             not listening
WSL 0.0.0.0:8080                   listening (opensandbox-server, docker backend)
WSL address                        172.28.248.27
urllib.request.getproxies()        http/https/ftp -> http://127.0.0.1:12000
HTTP_PROXY / HTTPS_PROXY env       unset
NO_PROXY env                       unset
```

So the proxy came from WinINET, not from the environment. That distinction is why setting
`NO_PROXY` in the process is sufficient and local: httpx and the OpenSandbox SDK both evaluate it
when they build a client.

The WSL address is read fresh on every call. No address is compiled into the harness.

---

## E. NO_PROXY and HTTPX Fix

New `experiments/stage_benchmark/execution/network.py`:

- `sandbox_endpoint()` reads `OPEN_SANDBOX_ENDPOINT` on every call; the default is
  `localhost:8080` and it is never cached.
- `endpoint_host()` accepts `host:port`, `scheme://host:port` and bracketed IPv6.
- `merge_bypass()` preserves the user's existing `NO_PROXY` entries, appends only hosts (never a
  port), and is idempotent.
- `apply_proxy_bypass()` writes both `NO_PROXY` and `no_proxy` and returns what it changed. It does
  not touch `HTTP_PROXY` / `HTTPS_PROXY`.
- `probe_health()` uses `httpx.Client` with default environment trust — the same trust model as the
  runtime — and classifies the result.
- `probe_runtime_health()` goes one step further and calls the frozen
  `OpenSandboxProvider.health()` adapter, so the preflight exercises the SDK path, not just an
  ad-hoc HTTP request. It works both from a sync CLI and from the async live path (private loop in
  a worker thread).

Applied at three points: CLI start, `execute_cell()` before preflight, and inside every probe.

---

## F. Runtime-equivalent Health Probe

`admission.preflight(..., probe_sandbox=True)` adds a `sandbox_health` check **before** any attempt
marker is created and before any provider client exists. Classification:

| Code | Meaning |
|---|---|
| `SANDBOX_HEALTH_READY` | 200 with `{"status": "healthy"}` |
| `SANDBOX_ENDPOINT_UNREACHABLE` | connection refused / DNS / protocol |
| `SANDBOX_HTTP_PROXY_MISROUTED` | `ProxyError`, or 407/502/504 answered by a proxy |
| `SANDBOX_HEALTH_TIMEOUT` | connect/read timeout |
| `SANDBOX_HEALTH_BAD_RESPONSE` | any other status or payload |

The probe creates no sandbox and calls no model. A failing probe turns preflight `BLOCKED` and adds
the code to `problems`.

Measured in this environment after the fix:

```text
python -m experiments.stage_benchmark.execution.runner --preflight \
  --sandbox-endpoint 172.28.248.27:8080
status        READY
sandbox_health SANDBOX_HEALTH_READY  endpoint=172.28.248.27:8080  status=200
bypass        172.28.248.27,localhost,127.0.0.1,::1
used_proxy_bypass true
model requests 0
sandboxes created 0
```

---

## G. Collector Pre-agent Failure Handling

`collector.analyze()` no longer requires a conversation. When `tool_conversations.json` is absent it
writes a **degraded** analysis:

```json
{"status": "INCOMPLETE", "valid": false, "record": null,
 "degraded": {"stage": "PRE_AGENT", "session_status": "failed",
              "session_failure": "sandbox provider ... ReadTimeout",
              "not_applicable": ["tool_conversations.json", "workspace.patch"]},
 "execution_view": {...}}
```

`completeness()` treats those two artefacts as *explained* for a `PRE_AGENT` run: they stay in
`not_applicable` and out of `missing`, and `ok` remains `false` because the analysis is not valid.
Nothing is synthesised — no empty conversation, no empty patch. A normal run that loses its
conversation is still an error, because its stage is `AGENT`.

`recover_evidence()` now requires only `session.json`. A pre-agent bundle recovers to
`status: INCOMPLETE`, with `degraded: true`, unchanged raw digests and the analyzer version
recorded. Only a missing session is `BLOCKED`.

---

## H. Runner and Adapter Failure Classification

The E2B report attributed the final `aggregation_failure` to an uninitialised `collection`
variable. That variable was in fact initialised; the real cause was `adapter.build_view()` doing
`(record.get("failure") or {}).get("primary")` on a **string** failure copied from the raw session.
That raised `AttributeError`, and the outer guard replaced the sandbox failure.

Fixes:

- `adapter.build_view()` accepts either a reconstructed record or a raw session. `failure` may be a
  string, a mapping or absent; `verification`, `delegation`, `handoff` and `sandbox` are read
  defensively.
- `_integrity_kind()` classifies stop reason, sandbox lifecycle and pre-agent sandbox failure
  without touching the analyser, so a broken analyser cannot change an integrity verdict.
- `execute_cell()` keeps a `secondary_failures` list. The primary failure is decided first and is
  never overwritten by a collector, aggregation or marker-write failure.
- `_terminal_kind()` gained `sandbox_creation` for a real provider whose sandbox was never created.

Ordering: integrity → goal → diagnostics. A collector crash after a sandbox failure yields
`failure_kind = sandbox_creation` plus `secondary_failures = ["collector_failure: ..."]`.

---

## I. Attempt Marker Lifecycle

`execute_cell()` now finalises the marker in a guarded `finally`:

| Situation | Marker |
|---|---|
| normal completion | `COLLECTED` |
| sandbox/provider/goal failure | `FAILED` |
| collector failure | `FAILED` |
| aggregation failure | `FAILED` |
| marker write itself fails | previous claim kept, `marker_update_failure` recorded |

The claim is written with `O_EXCL` and `fsync` before this block and is never deleted or reissued.
The initial `RUNNING` write is also guarded: a read-only disk degrades to `CLAIMED`, not to an
aborted attempt.

---

## J. New Calibration Identity

`CALIBRATION.workgrid.c2`, `P1`, `in_matrix: false`. The previous identity is recorded as
`previous_attempt` and keeps its own evidence directory. Nothing else changed: same WorkGrid goal,
fixture, public tests, oracle, model, budget and turn limits, all re-verified against the frozen S2
identity (`verify_calibration_identity() == []`).

The retry uses a new run root and evidence root; the c1 marker is untouched and cannot authorize
c2.

---

## K. Offline Tests

`tests/integration/test_stage_b_calibration_retry.py`: 25 tests, all local. The network tests use a
loopback health server and a loopback "proxy" that answers 502.

| # | Case | Test |
|---:|---|---|
| 1 | A live proxy is used without a bypass (the E2B failure in miniature) | `test_a_live_proxy_is_used_when_no_bypass_is_configured` |
| 2 | Bypass makes the probe succeed | `test_bypassing_the_proxy_makes_the_health_probe_succeed` |
| 3 | Unreachable endpoint is classified | `test_an_unreachable_endpoint_is_classified_not_raised` |
| 4 | Existing NO_PROXY entries preserved | `test_no_proxy_merges_existing_entries` |
| 5 | Provider proxy variables untouched | `test_provider_proxy_entries_are_not_disabled` |
| 6 | Changed WSL address updates the bypass | `test_a_changed_wsl_address_updates_the_bypass` |
| 7 | Endpoint read fresh from the environment | `test_endpoint_is_read_fresh_from_the_environment` |
| 8 | Probe failure blocks before the marker | `test_health_probe_failure_blocks_before_the_marker` |
| 9 | Probe never reaches the model | `test_health_probe_never_reaches_the_model` |
| 10 | Preflight reports endpoint and bypass | `test_preflight_reports_the_bypass_and_endpoint` |
| 11 | Pre-agent failure yields INCOMPLETE analysis | `test_pre_agent_failure_produces_an_incomplete_analysis` |
| 12 | Missing patch is a gap, not a fabrication | `test_missing_workspace_patch_is_a_gap_not_a_fabrication` |
| 13 | String `session.failure` is safe | `test_string_session_failure_does_not_raise` |
| 14 | Collector failure keeps the primary | `test_collector_failure_does_not_overwrite_the_primary_failure` |
| 15 | Aggregation failure keeps the primary | `test_aggregation_failure_does_not_overwrite_the_primary_failure` |
| 16 | Marker reaches a terminal state | `test_marker_reaches_a_terminal_state_on_failure` |
| 17 | Marker write failure keeps the claim | `test_a_marker_write_failure_keeps_the_original_diagnostic` |
| 18 | Retry identity is new and outside the plan | `test_the_retry_identity_is_new_and_outside_the_plan` |
| 19 | Two identities get separate paths | `test_two_calibration_identities_get_separate_evidence_paths` |
| 20 | Consumed authorization cannot authorize the retry | `test_a_consumed_authorization_cannot_authorize_the_retry` |
| 21 | Recovery needs no provider | `test_recovery_still_returns_without_a_provider` |
| 22 | Host oracle stays UNKNOWN | `test_host_oracle_stays_unknown_for_the_retry` |
| 23 | Prompt and runtime digests unchanged | `test_frozen_prompt_and_runtime_digests_are_unchanged` |
| 24 | Protocol and plan digests unchanged | `test_protocol_and_plan_digests_are_unchanged` |
| 25 | Schema still validates | `test_schema_still_validates_a_reconstructed_record` |

The existing E2A/E2B suites were updated only where semantics changed: the calibration cell is now
c2, the live-path tests inject a passing sandbox probe, and the freeze test accepts 1.2 while
asserting the archived originals. No assertion was dropped.

Regression measured in this workspace:

| Command | Result |
|---|---|
| execution `--offline` | `READY`, 23/23 cases, 0 network calls |
| execution `--preflight` (real probe) | `READY`, 17 checks |
| three execution-layer test files | 66 passed |
| full `pytest` | **1717 passed, 0 failed, 7 skipped**, 366.8 s |
| `pyright` | 0 errors |
| `ruff check` / `format --check` on changed files | clean |
| `ruff check .` / `format --check .` | 66 / 11 pre-existing, unchanged |

---

## L. Execution Freeze v1.2

`experiments/stage_benchmark/execution/freeze-1.2.json`, written on a clean worktree
(`git_status: ""`, 0 dirty paths, HEAD `9f33ca7`):

| Field | Value |
|---|---|
| `freeze_version` | `stage-b-execution/1.2` |
| harness source (9 files) | `sha256:bd1276746da7e747e7ace1700bc8b44dd79ab8bdd3fcfa7c9ec5d099eca1bcc0` |
| supersedes | `1.1`, digest `sha256:606c7d4c1d9019cd2f89d0c846b438d15e94eb3a00ffaa7d33fece7355e6a05f` |
| archived 1.0 | `archive/freeze-1.0.json`, `7bf630e5…` |
| archived 1.1 | `archive/freeze-1.1.json`, `606c7d4c…` |
| execution analysis | `stage-b-execution-analysis/1.1` |
| calibration | `CALIBRATION.workgrid.c2`, `PREPARED_NOT_RUN`, 1 attempt authorised |
| runtime / protocol / plan | `4eebcf1e…` / `937f9253…` / `c6914474…` (unchanged) |

The freeze does not digest itself. The earlier freezes are archived byte-for-byte and `--check`
verifies both archives on every preflight.

---

## M. New Owner Authorization

**Not yet granted.** The E2B authorization is consumed and cannot be reused; its cell list names
c1, and `authorization_gate()` rejects it for c2 in a test.

A Phase B authorization must contain, with values read from a fresh preflight (not typed by hand):

```json
{
  "status": "APPROVED",
  "approved_by": "...",
  "approval_commit": "<HEAD>",
  "approved_at": "<ISO-8601>",
  "cells": ["CALIBRATION.workgrid.c2"],
  "approved_digests": {"protocol": "...", "plan": "...", "runtime": "...", "runner_source": "..."},
  "consumed": false,
  "cost_ceiling_usd": <owner choice>
}
```

The ceiling is an approval field. This runtime has no USD hard stop; it must not be described as
currency enforcement.

---

## N. Live Retry Evidence

Not produced. Phase B has not started. No DeepSeek request, no sandbox creation, no run.

---

## O. Evidence Integrity

No new run evidence exists in this phase. What was verified:

| Check | Result |
|---|---|
| c1 marker unchanged | SHA-256 matches the committed copy |
| Matrix markers | 0 |
| New evidence root | no marker |
| Raw evidence of c1 | untouched, private, outside the repository |
| Real health probe | PASS through the runtime adapter, 0 model requests, 0 sandboxes |

---

## P. Final Decision

`STAGE_B_E2C_RETRY_READY_OFFLINE`.

The proxy conflict is fixed at the correct layer (host-scoped `NO_PROXY`, provider proxy intact),
preflight now fails closed on a real sandbox probe before a marker exists, a pre-agent failure
produces an honest INCOMPLETE analysis instead of a crash, the primary failure survives downstream
faults, the marker reaches a terminal state, and the harness is frozen as 1.2 with the retry
identity `CALIBRATION.workgrid.c2`.

Phase B remains blocked on a new owner authorization.

---

## Q. Remaining Risks

1. **The endpoint is a WSL NAT address** (`172.28.248.27`) and will change on reboot. It must be
   re-read and passed with `--sandbox-endpoint`; there is no stable DNS name yet.
2. **The VPN proxy is external state.** If it is reconfigured to intercept private ranges despite
   `NO_PROXY`, the probe will now catch it, but the environment still needs care.
3. **The preflight probe runs on the operator's machine.** It proves reachability from that host,
   not from any other.
4. **No live model behaviour has been observed yet.** Prompt adherence, tool use, budget accounting
   and oracle authority remain unverified against DeepSeek.
5. **Pre-agent analysis has no reconstructed record.** It is intentionally `record: null`; any
   benchmark statistic that needs a record must treat it as INCOMPLETE.
6. **Repository-wide Ruff is still red** on frozen S1 artefacts (66 check / 11 format), exactly as
   at the E1 baseline. Scoped checks on changed files pass.
