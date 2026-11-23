# Stage B-E2E — OpenSandbox Smoke Test and c3 Preparation

Status: **`STAGE_B_E2E_SMOKE_PASS_C3_PREPARED`**.

A temporary container was created through the real adapter, exercised, isolated and destroyed.
No model was called, no calibration attempt was consumed, and no benchmark cell ran.

| Question | Answer |
|---|---|
| Was a sandbox really created? | **Yes** — `7b3edc91-6e8d-4473-a21a-39b8f642957b` |
| Was it destroyed? | **Yes** — handle unreachable after destroy, no residual container |
| Was DeepSeek called? | **No** — 0 requests |
| Was a calibration executed? | **No** — 0 attempts, no marker in the calibration root |
| Is c3 a prepared identity? | **Yes** — `CALIBRATION.workgrid.c3`, outside the 36-cell plan |
| Does c3 have its own authorization? | **No** — preparation is not authorization |

---

## A. Git Provenance

| Item | Value |
|---|---|
| Branch | `fix/stage-b-sandbox-smoke` |
| Base | `0f80180d01377ed7290bf13184ba52be6e0ca970` |
| Harness commit | `c94559a40bb08317dfad0d286a97972bee1f4582` |
| Freeze written at | clean tree, `git_status: ""`, 0 dirty paths |
| Protocol | `stage-b-protocol/1.1` |
| Execution freeze | `stage-b-execution/1.4` |
| Analysis adapter | `stage-b-execution-analysis/1.1` |

No frozen runtime, prompt, fixture, oracle, plan, earlier freeze or earlier calibration evidence was
modified.

---

## B. Historical Calibration Evidence

| Attempt | State | Marker hash |
|---|---|---|
| c1 | consumed, `RUNNING` (E2B), evidence preserved | `DEAEF36A…751C6FB` |
| c2 | consumed, `FAILED`, evidence preserved | unchanged since E2C |

Neither marker was deleted, renamed or reused. c3 starts from a new identity and a new evidence
root; nothing about c1 or c2 is reinterpreted by this stage.

---

## C. Frozen Configuration Audit

The final values were read from the built objects, not from the protocol alone:

| Field | Protocol | `SandboxConfig` | `OpenSandboxOptions` | Create request |
|---|---|---|---|---|
| Provider | `opensandbox` | `opensandbox` | endpoint-based | accepted |
| CPU | `1` | `"1"` | `"1"` | accepted |
| Memory | `1.0` GiB | `"1Gi"` | `"1Gi"` | accepted |
| Image | `atlas-uat-python:3.12` | same | same | accepted |
| Network | `none` | `none` | sandbox spec `none` | isolation probe PASS |
| Workspace | — | `/workspace/project` | — | provisioned later by GoalSandbox |

`provision.sandbox_config()` is now the single constructor used by `build_options()`, the preflight
audit and the smoke test, so the three cannot drift.

---

## D. Memory Quantity Validation

`provision.memory_quantity()` uses `Decimal`, not float formatting:

| Input | Result |
|---|---|
| `1.0`, `1` | `1Gi` |
| `0.5` | `0.5Gi` |
| `NaN`, `Infinity`, `-Infinity` | `ValueError` (non-finite) |
| `"1Gi"`, `None` | `ValueError` (not a number) |
| `True` | `ValueError` (bool is not a quantity) |
| `0`, negative | `ValueError` (not positive) |
| `0.0001`, `1.0004` | `ValueError` (would lose precision; never silently `0Gi` or rounded) |

The frozen 1 GiB parameter is unchanged; only its rendering is defined.

---

## E. OpenSandbox SDK Parameter Path

```text
protocol.run_config
  -> provision.sandbox_config()          SandboxConfig(cpu="1", memory="1Gi", image=..., network="none")
  -> provision.build_options()           AtlasConfig.sandbox = that config
  -> atlasos.app.goal_sandbox            OpenSandboxOptions(endpoint, cpu=sandbox.cpu, memory=sandbox.memory)
  -> atlasos.sandbox.opensandbox_provider  resource={"cpu": "1", "memory": "1Gi"}
  -> SDK create                          accepted (real container)
```

The smoke test builds its options through the same helper and asserts the same three values before
create; a mismatch is a failed verdict, not a warning.

---

## F. Runtime Preflight

`preflight` gained `sandbox_resource_configuration`, executed before any marker and without creating
anything. Measured:

```text
status  READY
checks  20
sandbox_resource_configuration  ok
  cpu=1 memory=1Gi image=atlas-uat-python:3.12 network=none
sandbox_health  SANDBOX_HEALTH_READY endpoint=172.21.123.198:8080 status=200
```

Health and create are deliberately separate checks: a healthy `/health` says nothing about the
create contract, which is what failed in c2.

---

## G. VPN / NO_PROXY / WSL Diagnostics

| Item | Value |
|---|---|
| WSL address at run time | `172.21.123.198` (re-read, not historical) |
| OpenSandbox | `0.0.0.0:8080` inside WSL |
| Bypass written | `NO_PROXY=172.21.123.198,localhost,127.0.0.1,::1` |
| VPN port `127.0.0.1:12000` | listening, **untouched** |
| `HTTP_PROXY` / `HTTPS_PROXY` | not modified; `trust_env` never disabled |
| Unrelated containers | 5 (atlasrag stack), left alone |

The harness contains no reference to port 12000, enforced by a test.

---

## H. Smoke Test Authorization

`experiments/stage_b_smoke/authorization.json`, digest
`sha256:f24cc3635fd40fd1b295b1f38becd3355a2a6438a7a6da11d2cf7997b3235ebe`:

| Field | Value |
|---|---|
| `smoke_id` | `SANDBOX.SMOKE.stage-b-e2e` |
| `approved_by` | `Owner (Melon235)` |
| create / destroy | allowed |
| model / agent / calibration / benchmark | explicitly forbidden |
| `max_sandboxes` | 1 |

A missing or partial record produces `SANDBOX_SMOKE_BLOCKED` before any create.

---

## I. Sandbox Create Evidence

| Field | Value |
|---|---|
| Sandbox ID | `7b3edc91-6e8d-4473-a21a-39b8f642957b` |
| Started / finished | `06:04:41.212Z` / `06:04:44.659Z` |
| Result | `ok: true`, no error |
| Image / CPU / Memory | `atlas-uat-python:3.12` / `1` / `1Gi` |

---

## J. Command Execution Evidence

| Command | Exit | Output |
|---|---:|---|
| `pwd` | 0 | `/` |
| `python3 --version` | 0 | `Python 3.12.16` |
| `id` | 0 | `uid=0(root) gid=0(root) groups=0(root)` |
| `ls -la /workspace` | 2 | informational only — a bare sandbox has no workspace; GoalSandbox provisions it |

File round trip: wrote `/tmp/atlas-smoke-probe.txt` (22 bytes), read it back, bytes matched.

**Correction found by this run.** The first verdict logic treated the provider's transport `ok` as
command success, and `ls /workspace` exited 2 while `ok` stayed true. The logic now records `ok` and
`exit_ok` separately and requires a zero exit for the three required commands. The raw evidence of
this run was **not** rewritten; `rescore-under-1.1.json` re-scores it from the recorded exit codes
(`pwd`, `python`, `id` all 0; `workspace` informational) and reaches the same verdict.

---

## K. Network Isolation Evidence

`probe_network_none(provider, handle)` — the runtime's own egress probe, not a configuration read —
returned **PASS**: the container could not reach the network with `network="none"`.

---

## L. Sandbox Cleanup Evidence

| Field | Value |
|---|---|
| Attempted | yes |
| Destroyed | yes |
| Residual | false |
| Post-destroy probe | handle unreachable |
| Docker container count before / after | 5 / 5 |

No other container was touched and no global Docker cleanup was run.

---

## M. Smoke Test Verdict

| Dimension | Result |
|---|---|
| Health | PASS |
| Create | PASS |
| Resources | PASS (`cpu=1`, `memory=1Gi`, image, `network=none`) |
| Execution | PASS (required commands exit 0, file round trip) |
| Isolation | PASS |
| Cleanup | PASS (no residual) |
| Agent requests | 0 |
| DeepSeek requests | 0 |
| Calibration attempts | 0 |

**`SANDBOX_SMOKE_PASS`**.

---

## N. c3 Preparation

`CALIBRATION.workgrid.c3` with `previous_attempts = (c1, c2)`:

- same WorkGrid goal, fixture, public tests, Host Oracle, P1 prompt, `deepseek-chat`, 500 000 tokens,
  cpu 1, memory 1 GiB, 36-cell plan unchanged;
- **not** in the 36-cell plan;
- `verify_calibration_identity() == []`;
- new identity, new evidence root (`D:\Work\atlas-b2e\evidence`), new authorization required.

c3 preparation is **not** execution authorization. No c3 marker exists.

---

## O. Execution Freeze

`stage-b-execution/1.4`, written on a clean tree at HEAD `c94559a`:

| Field | Value |
|---|---|
| harness source (10 files) | `sha256:50c0e5ce8d23160860284b02b530b6d5bafa481bf9410406655cc359bdd00b2d` |
| supersedes | `1.3`, `sha256:37506684d630daf2fdd664cb87e9aa353bef6f848cb1fbaad5d4b7129ecbfee1` |
| history | 1.0 `7bf630e5…`, 1.1 `606c7d4c…`, 1.2 `7b262d89…`, 1.3 `37506684…` |
| smoke verdict | `SANDBOX_SMOKE_PASS`, created/destroyed true, 0 model, 0 calibration |
| smoke evidence digest | `sha256:19caf46eb605183c7793243a4e7a677f2470a37dc1e19361d7eae5807509def9` |
| calibration | `CALIBRATION.workgrid.c3`, `PREPARED_NOT_RUN`, 0 authorised this stage |
| runtime / protocol / plan | unchanged |

The freeze does not digest itself, and it does not describe the smoke as a coding benchmark.

---

## P. Regression Tests

| Command | Result |
|---|---|
| E1 `run_stage_b.py --check` | `READY` |
| execution `--offline` | `READY`, 23/23 cases |
| execution `--preflight` (real probe) | `READY`, 20 checks |
| smoke suite | 21 passed |
| execution-layer suites (4 files) | 108 passed |
| full `pytest` | **1742 passed, 0 failed, 7 skipped**, 375.6 s |
| `pyright` | 0 errors |
| Ruff check / format on changed files | clean |
| `ruff check .` / `format --check .` | 66 / 11 pre-existing, unchanged |

---

## Q. Remaining Risks

1. **The smoke proves the transport, not the benchmark.** GoalSandbox provisioning (fixture upload,
   git repo, workspace path) is still unexercised against a real container.
2. **`/workspace/project` was absent** in the bare container; the real run creates it. A future
   smoke could pre-provision a dummy workspace to test that path too.
3. **Isolation depends on the egress sidecar.** The probe passed once; a per-run check is still the
   only evidence.
4. **The WSL address changes on restart.** `--sandbox-endpoint` must be supplied from the current
   host each time.
5. **Single-attempt discipline remains.** c3 has no authorization; a live run needs a new owner
   decision with fresh digests.
6. **Repository-wide Ruff is still red** on frozen S1 artefacts (66 check / 11 format), unchanged
   from the E1 baseline.

---

## R. Final Decision

**`STAGE_B_E2E_SMOKE_PASS_C3_PREPARED`.**

The OpenSandbox create/execute/isolate/destroy path is proven with real infrastructure, without
spending a single token or calibration attempt. c3 is prepared but unauthorised. No 36-cell cell was
started.
