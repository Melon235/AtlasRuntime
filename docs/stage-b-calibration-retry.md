# Stage B-E2C — OpenSandbox Proxy Fix and Controlled Calibration Retry

Status: Phase A reached `STAGE_B_E2C_RETRY_READY_OFFLINE`; Phase B was then authorised and ran
exactly once, and failed at sandbox creation.

Final verdict for the live retry: **`CALIBRATION_INFRA_FAILED` / `GOAL_UNKNOWN`**. The
authorization consumed in Stage B-E2B was not reused; a new one was granted for
`CALIBRATION.workgrid.c2`.

The Phase A engineering outcome stands: the proxy conflict is fixed, the preflight now fails closed
on a real SDK probe, a pre-agent failure produces an INCOMPLETE analysis instead of a crash, the
primary failure survives downstream faults, and the marker reaches a terminal state. What failed is
one layer further in: the execution layer renders the 1 GiB memory limit as `1.0Gi`, which this
OpenSandbox server rejects. That rendering is fixed in harness **1.3** (section R); no live attempt
is authorised for 1.3 yet.

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
verifies both archives on every preflight. It records the calibration as `PREPARED_NOT_RUN`
because that was its state when the harness was frozen; the attempt was consumed afterwards.

The authorization record is not rewritten after use. Consumption is proven by the attempt marker
(`authorization_digest` + `state: FAILED` + `retries_allowed: false`), and a second run for c2 is
refused at preflight. A future harness should flip `consumed` at claim time instead of relying on
the marker alone.

---

## M. New Owner Authorization

Granted once, in-session, for `CALIBRATION.workgrid.c2` at a 0.50 USD ceiling, approver
`Owner (Melon235)`, commit `6cf4699`, digests read from a live preflight:

| Field | Value |
|---|---|
| authorization file | `experiments/stage_b_calibration/authorization-c2.json` |
| record digest | `sha256:ec3d2bb946352b55d741762604c0363daa392c1a2f4d08c3de6889b74dcb41df` |
| `cells` | `["CALIBRATION.workgrid.c2"]` |
| `cost_ceiling_usd` | `0.5` |
| protocol / plan / runtime / runner | `937f9253…` / `c6914474…` / `4eebcf1e…` / `bd127674…` |

The ceiling is an approval field; this runtime has no USD hard stop.

---

## N. Live Retry Evidence

One attempt, `2026-11-22T10:05:12Z`, consumed. No retry.

```text
status:         failed
failure:        sandbox create failed; sandbox task fails closed:
                code='sandbox_create_failed'
                message="Create sandbox failed: Invalid resourceLimits.memory:
                '1.0Gi'; must resolve to a positive resource limit."
provider requests: 0
sandbox created:   false
tokens:            null / 0
marker:            FAILED, retries_allowed=false
```

Stage reached: **sandbox creation**, i.e. after the health probe and marker claim, before the model
loop. The session records the prompt as `main-v1.0` / `sha256:7e6fd829…`, so the arm was correct.

Root cause is in this stage's own execution layer, not in the runtime:

```python
# experiments/stage_benchmark/execution/provision.py:371
memory = (f"{config['memory_gib']}Gi",)  # 1.0 -> "1.0Gi"
```

`memory_gib` is a float, so the rendered quantity is `1.0Gi`. This OpenSandbox server accepts an
integer quantity (`1Gi`) and rejects `1.0Gi` with `SANDBOX::INVALID_PARAMETER`. The frozen 1 GiB
experiment parameter is correct; only its rendering is wrong. The fault was not detectable by the
preflight, because the preflight probes `/health` and does not create a sandbox.

What the E2C repairs did prove, on a real attempt:

| Repair | Observed |
|---|---|
| collector survives a pre-agent run | `analysis.json` and `evidence_manifest.json` written |
| analysis is honest | `status: INCOMPLETE`, `record: null`, `degraded.stage: PRE_AGENT` |
| primary failure survives | `failure_kind: sandbox_creation`, `secondary_failures: []` |
| marker lifecycle | `state: FAILED` (E2B left `RUNNING`) |
| no model call on failure | `main_requests: 0` |
| recovery is read-only | `INCOMPLETE`, `raw_digests_unchanged: true`, no provider |

Not validated: model loop, tool execution, public tests, host oracle.

Note: the runtime error object carries `retryable=True`. That is the runtime's generic transport
hint; the calibration policy is stricter and the attempt was **not** retried.

---

## O. Evidence Integrity

Private raw evidence (outside the repository): `session.json`, `transcript.jsonl`, `attempt-1.json`,
`trajectory/`. Published redacted subset: 16 files in `experiments/stage_b_calibration/retry/`,
with `retry-digests.json` recording 5 raw and 16 published digests.

| Check | Result |
|---|---|
| raw digests before/after recovery | identical |
| redactions | none needed (no oracle text present) |
| manifest | 14 published entries, 5 raw entries, `immutable_raw: true` |
| oracle | `UNKNOWN`, no substitution |
| public tests | 0 calls |
| matrix markers | 0 |
| c1 marker | unchanged, `DEAEF36A…` |

Recovery produced `status: INCOMPLETE` rather than pretending the run completed. Evidence integrity
is INCOMPLETE by construction: no conversation, no patch, no analysis record.

---

## P. Final Decision

| Axis | Verdict |
|---|---|
| Phase A (offline readiness) | `STAGE_B_E2C_RETRY_READY_OFFLINE` |
| Execution integrity (live retry) | **FAILED** — sandbox rejected the memory quantity before any model call |
| Evidence integrity | **INCOMPLETE** — correct and honest, but the run produced no conversation |
| Coding correctness | **UNKNOWN** |

**`CALIBRATION_INFRA_FAILED` / `GOAL_UNKNOWN`.**

The proxy defect that caused E2B is fixed and the new failure is a different, narrower one in a
layer this stage owns. Under the one-attempt rule the retry was not repeated.

---

## Q. Network Policy (port 12000 is reserved)

`127.0.0.1:12000` belongs to the user's VPN client. The harness treats it as read-only external
state:

| Rule | Enforcement |
|---|---|
| never bind, close or rebind 12000 | no listener is created; `test_the_execution_layer_never_mentions_a_vpn_port` asserts the literal never appears in the harness |
| never change Windows system proxy / VPN config | the harness only writes process-level `NO_PROXY` / `no_proxy` |
| never disable proxying globally | `HTTP_PROXY` / `HTTPS_PROXY` are untouched and `trust_env` is never set to `False` |
| sandbox traffic goes direct | the WSL address is added to `NO_PROXY` per process, before the probe |
| DeepSeek traffic keeps its proxy | the provider client continues to read the existing proxy configuration |
| WSL address is re-read every run | `sandbox_endpoint()` reads `OPEN_SANDBOX_ENDPOINT` (or accepts `--sandbox-endpoint`) on each call; no historical address is compiled in |
| an unreachable VPN is reported, not repaired | the probe classifies the failure; no restart or reconfiguration is attempted |

A port scan is never used to decide what the harness may occupy; 12000 being idle would not make it
available.

---

## R. Harness 1.3 — resource quantity closure

The one-line cause of the c2 failure is fixed in the execution layer:

| Before | After |
|---|---|
| `memory=f"{config['memory_gib']}Gi"` → `1.0Gi` | `provision.memory_quantity(1.0)` → `1Gi` |

`memory_quantity()` renders a whole number without a decimal suffix, keeps fractional values as
`0.5Gi`, and raises on a non-positive limit. The frozen 1 GiB experiment parameter is unchanged.

New tests: `test_memory_quantity_renders_an_integer_without_a_decimal`,
`test_memory_quantity_rejects_a_non_positive_limit`,
`test_built_options_carry_the_integer_memory_quantity`, and
`test_the_execution_layer_never_mentions_a_vpn_port` (the reserved 12000 port never appears in the
harness).

Freeze `stage-b-execution/1.3`, written on a clean worktree (HEAD `55d4fdb`, `git_status: ""`,
0 dirty paths):

| Field | Value |
|---|---|
| harness source (9 files) | `sha256:0355da165f60727b2b99d72d5840465829e817e4f293d4f1d095c850ea67fdf8` |
| supersedes | `1.2`, `sha256:7b262d89c2782442286940c9705601982f9c1fe79ea449b0cd8fc9b3ddfb6b18` |
| history | 1.0 `7bf630e5…`, 1.1 `606c7d4c…`, 1.2 `7b262d89…` |
| runtime / protocol / plan | unchanged (`4eebcf1e…` / `937f9253…` / `c6914474…`) |
| calibration | `CONSUMED_FAILED`, `authorised_this_stage: 0`, `next_identity_required: true` |

The WSL host had restarted between stages, which changed the address from `172.28.248.27` to
`172.21.123.198` and stopped the server. That is the case the design expects: the endpoint was
re-read, the server was started on its own 8080 port, and the runtime probe then reported

```text
READY 18 checks
SANDBOX_HEALTH_READY endpoint=172.21.123.198:8080 status=200
bypass=172.21.123.198,localhost,127.0.0.1,::1  used_proxy_bypass=true
```

No historical address was reused, and port 12000 was neither touched nor probed for availability.

Regression for 1.3: execution `--offline` 23/23; execution-layer suites 70 passed; full `pytest`
**1721 passed, 0 failed, 7 skipped** (354.8 s); `pyright` 0 errors; changed files Ruff/format clean;
repository-wide Ruff still the pre-existing 66 / 11.

**No attempt is authorised for this harness version.** The c2 marker forbids reuse, so a further
live run requires a new identity (c3), a fresh authorization record and a new owner decision.

---

## S. Remaining Risks

1. **The memory rendering must be fixed in `execution/provision.py`** (`str(int(...))` or an integer
   contract), then the harness re-frozen and a new authorization granted. This is a one-line
   execution-layer fix, not a runtime change.
2. **Preflight cannot detect create-time parameter rejection.** A future probe could attempt a
   dry-run create or validate the resource envelope against the server schema before a marker.
3. **The endpoint is a WSL NAT address** (`172.28.248.27`) and changes on reboot; pass
   `--sandbox-endpoint` each time.
4. **The VPN proxy is external state.** `NO_PROXY` handles the sandbox host, but the DeepSeek path
   still depends on the proxy being healthy.
5. **No live model behaviour has been observed yet.** Prompt adherence, tool use, budget accounting
   and oracle authority remain unverified.
6. **The runtime marks this error `retryable=True`.** Policy, not runtime, is the authority; a future
   operator must not read that flag as permission.
7. **Repository-wide Ruff is still red** on frozen S1 artefacts (66 check / 11 format), unchanged
   from the E1 baseline.
