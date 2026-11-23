# Stage B-E2F — GoalSandbox Provisioning Gate

Status: **`STAGE_B_E2F_OFFLINE_READY_AWAITING_AUTHORIZATION`**.

The E2E smoke proved that a bare container can be created, exercised, isolated and destroyed. It
never proved that the **goal** path works, because a bare container has no `/workspace` at all. E2F
is the gate for exactly that gap: a frozen WorkGrid fixture has to travel into the container and
become a clean checkout at `/workspace/project` through the runtime's own provisioning code.

Nothing here ran live. No container was created, no model was called, no calibration attempt was
consumed, no benchmark cell was started, and no E2F authorization exists.

| Question | Answer |
|---|---|
| Is the gate implemented? | **Yes** — `experiments/stage_benchmark/execution/e2f_provisioning.py` |
| Does it reuse the runtime? | **Yes** — `GoalSandbox.start()` / `.close()`, nothing re-implemented |
| Is the one-shot claim atomic across processes? | **Yes** — `os.O_CREAT \| os.O_EXCL`, proven by a two-process test |
| Was a real container created? | **No** — 0 creates |
| Was DeepSeek called? | **No** — 0 requests |
| Is there an E2F authorization? | **No** — the Owner has not issued one |
| Is the live verdict PASS? | **No** — `LIVE_NOT_RUN` |

---

## A. Why E2F exists

`STAGE_B_E2E_SMOKE_PASS_C3_PREPARED` left four things unexercised against real infrastructure:

1. the fixture upload (a Git bundle written into the container),
2. `git clone` inside the container and the `HEAD` verification that follows it,
3. the workspace binding (`/workspace/project`) and its clean-tree state,
4. a complete `GoalSandbox` initialisation, including its own isolation probes.

A coding run cannot start before those work, and a failure there would have been discovered in the
middle of the benchmark instead of in a gate.

---

## B. The two defects this stage fixes

### B1. `run_smoke()` could leave a container behind

The original order was: create → commands → file round trip → isolation probe → destroy. Only the
isolation probe was guarded, so an exception in `_run_commands()` or `_file_round_trip()` escaped
`run_smoke()` and skipped `_destroy()` entirely.

The fix keeps the same evidence schema and the same verdict rules:

* every step after a usable handle is wrapped (`_guarded`); a broken step is recorded as evidence,
  not raised;
* `_destroy()` is called once, from a `finally`-equivalent position, and cannot be skipped;
* the **first** failure in execution order is `primary_failure`; everything later — including a
  cleanup failure — is `secondary_failures`, so cleanup can never overwrite the primary failure;
* `residual` is now three-valued: `true` (the handle still answers), `false` (the destroy was
  confirmed *and* the handle does not answer), `null` (unconfirmed);
* a create that raises **without a handle** is `create.state = "UNKNOWN"`, not "nothing exists".
  The evidence names the resource to look for (`atlas_run_id=...`) instead of guessing.

Nothing about the cleanup is retried. The provider's own timeout and cancellation semantics are
used unchanged; no new wait loop was introduced.

### B2. `max_sandboxes=1` was a scope, not a one-shot approval

`smoke.authorization_gate()` checked a field. Two processes could both present the same record and
both create a container. E2F adds a durable claim, which is the same primitive
`admission.claim_attempt()` already uses for cells:

```text
experiments/stage_b_smoke/e2f-provisioning/claims/attempt-<authorization-digest>.json
```

`os.open(..., O_CREAT | O_EXCL | O_WRONLY)` + `fsync` decides the winner inside the kernel, so the
claim holds across processes, not only across `asyncio` tasks. The claim is written before the first
provider call, is never deleted, and its state only moves forward (`CLAIMED` → `COMPLETED` /
`FAILED`). A second process fails **before** `create`.

---

## C. Authorization

There is **no** `experiments/stage_b_smoke/e2f-authorization.json` in this repository. A missing file
is an absent approval; the gate writes nothing there and cannot approve itself.

The record the Owner would have to issue:

| Field | Requirement |
|---|---|
| `status` | `APPROVED` |
| `smoke_id` | `SANDBOX.SMOKE.stage-b-e2f-provisioning` |
| `approved_by`, `approved_at`, `approval_commit` | present and non-empty |
| `allow_container_create`, `allow_container_destroy` | `true` |
| `deepseek_requests_allowed`, `agent_runner_allowed`, `calibration_allowed`, `benchmark_cells_allowed` | not `true` |
| `max_sandboxes` | `1` (a scope; the claim is what enforces one attempt) |
| `consumed` | not `true` |
| `approved_digests` | must equal the eight digests below |

The digests are derived, never asserted. To read the current ones:

```powershell
.\.venv\Scripts\python.exe -c "import json; from experiments.stage_benchmark.execution import e2f_provisioning as e; print(json.dumps(e.approved_digests(e.experiment_identity()), indent=2, sort_keys=True))"
```

At the time of this document they are:

| Key | Value |
|---|---|
| `smoke` | `SANDBOX.SMOKE.stage-b-e2f-provisioning` |
| `harness_source` | `sha256:5dbadf272f3591fe72af81d502205321b4f13e89bc04251423464c106f7839c2` |
| `gate_source` | `sha256:6c96c248a1ef1d0b42634d413660d17b5f4ebcfbbc894dc10894041a0655d02c` |
| `runtime` | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae` |
| `protocol` | `sha256:937f925306659ea7c1db194e7083e5234949edc76566a78974bbbc77983a1585` |
| `plan` | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648` |
| `fixture` | `sha256:893238efa0d8bb58e835369321d590eafbba2e512add6d41f039b7a394b38828` |
| `git_commit` | the commit the run is checked out at |

`gate_source` is the digest of `e2f_provisioning.py` itself; `harness_source` is
`admission.source_identity()` over the ten execution-harness files. Both change if those files are
edited, so the record must be built **after** the final commit.

`git_commit` is the commit under test, so an approval issued at HEAD `850aef8` cannot run a later
tree.

---

## D. The gate

```text
authorization_gate(record, identity)          no record -> BLOCKED, nothing is contacted
  -> identity_checks(...)                     protocol / plan / runtime / fixture / resources / oracle
  -> claim_once(...)                          durable, atomic, before any provider call
  -> evidence root + run directory            unwritable -> FAILED before create
  -> materialize the frozen WorkGrid fixture  fresh temp git repo, verified against the S2 bytes
  -> provision.sandbox_config()               the frozen cpu/memory/image/network values
  -> health probe                             through the runtime adapter, never a raw socket
  -> GoalSandbox(options, config, run_dir).start(task_id, context, worker_id)
       -> build_sandbox_provider("opensandbox")
       -> GitProjectProvisioner.inspect(fixture)
       -> ProjectSandboxManager.ensure_binding()   create, inspect, upload, clone, verify HEAD
       -> probe_network_none / probe_host_separation
  -> workspace checks (fixed, read-only, Host-owned commands)
  -> GoalSandbox.close() + residual probe
  -> provisioning-result.json
```

`require_real=True` is enforced by `GoalSandboxOptions`; `provider="fake"` cannot be combined with
it. `run_goal()` is never imported: there is no task graph, no Main, no worker, no prompt.

### Workspace checks

One command per fact, because provider stdout is not guaranteed to preserve newlines:

| Check | Command | Required result |
|---|---|---|
| `pwd` | `pwd` | exactly `/workspace/project` |
| `git_dir` | `git rev-parse --git-dir` | non-empty |
| `head` | `git rev-parse HEAD` | the fixture's source commit |
| `status` | `git status --porcelain` | empty |
| `files` | Python one-liner over the 21 frozen paths | `ALL_PRESENT` |
| `leaks` | Python scan for `HIDDEN_OK`, `HIDDEN_FAIL`, `oracle_program`, `oracle_source` | `CLEAN` |

The required commands must have a **transport success and an observed exit code of 0**.
`ok=true, exit_code=null` is a failure, never a pass — the same rule the E2E smoke adopted after its
first real run.

### Isolation

`GoalSandbox.start()` already runs the two probes; the gate reads their results instead of probing
again. `network_probe` must be `PASS` (an `UNKNOWN` probe is recorded as unconfirmed, not as a
pass), and `require_host_separation` must have accepted both sentinels.

### Cleanup

`GoalSandbox.close()` is the only teardown, called with `failed=True` whenever anything failed.
`residual` uses the same three-valued semantics as the smoke: a failed destroy cannot be corrected
by an unreachable handle. When the gate cannot confirm the remote state it reports `UNKNOWN`, names
the `atlas_run_id` label to look for, and refuses to claim `residual=false`.

---

## E. Evidence

`experiments/stage_b_smoke/e2f-provisioning/provisioning-result.json`:

```text
smoke_id, smoke_version, started_at, finished_at
authorization{ready, problems, digest, approved_by, approval_commit, approved_at}
identity{smoke, git, harness_source, gate_source, runtime, protocol, plan, fixture}
gate_checks[]
claim{created, path, digest, state}
environment{endpoint, endpoint_source, bypass, proxy_env}
create_request{provider, endpoint, image, cpu, memory, network, workspace_root, require_real}
fixture{materialized, root, source, source_commit, clean, files, digest, problems}
health{}
sandbox{started, provider_is_real, capabilities, capabilities_digest, binding_id, sandbox_id,
        binding_state, workspace_root, network_probe, host_separation, created, destroyed, ...}
workspace{root, checks[], summary{}, verdict{}}
isolation{network_probe, host_separation}
cleanup{attempted, destroyed, residual, state, probe, error, detail}
checks{}, unconfirmed[], primary_failure, secondary_failures[]
model_requests, calibration_attempts, benchmark_cells, not_run[]
verdict, status, failure_kind, detail
```

Verdicts: `PROVISIONING_SMOKE_PASS`, `..._FAILED`, `..._UNKNOWN`, `..._BLOCKED`. `status` is
`PASSED|FAILED|UNKNOWN|BLOCKED`, or `EVIDENCE_NOT_WRITTEN` when the result file itself could not be
written — a run whose evidence cannot be stored is not allowed to look successful.

Failure kinds are named by layer (`authorization`, `identity`, `duplicate_claim`, `claim_write`,
`fixture`, `sandbox_environment`, `sandbox_capability`, `sandbox_create`, `sandbox_binding`,
`fixture_upload`, `git_clone`, `workspace`, `isolation`, `cleanup`, `evidence_write`, `unexpected`).
A provisioning failure is never reported as a coding-goal failure.

---

## F. How to run it (after a new authorization)

```powershell
.\.venv\Scripts\python.exe -m experiments.stage_benchmark.execution.runner `
  --goal-provisioning-smoke `
  --provisioning-authorization experiments/stage_b_smoke/e2f-authorization.json `
  --sandbox-endpoint <current wsl host>:8080
```

`--sandbox-endpoint` is resolved per run and added to `NO_PROXY` for this process only. No address is
hard-coded, the VPN configuration is untouched, and no container is started by any other command in
this document.

---

## G. Offline verification performed in this stage

The fault matrix lives in `tests/integration/test_stage_b_e2f_provisioning.py` (Task B/C) and
`tests/integration/test_stage_b_sandbox_smoke.py` (Task A). In short:

* authorization, identity, claim, fixture, workspace, isolation, cleanup and evidence faults are all
  injected and classified;
* the two-process claim race is a real operating-system race, not two asyncio tasks;
* the provisioning path runs through the real `GitProjectProvisioner` and `ProjectSandboxManager`
  against a deterministic provider that executes real subprocesses on the Host.

The deterministic provider is **not** isolation evidence: it runs on the Host, so the live isolation
verdict stays `LIVE_NOT_RUN`.

---

## H. Known consequence: freeze 1.4 no longer describes the live harness

`admission.source_identity()` digests `smoke.py` and `runner.py`, both of which this stage edits.
Freeze `stage-b-execution/1.4` is preserved byte-for-byte, so
`runner_source_digest_matches_freeze` now reports the mismatch and a live **cell** run is refused by
preflight before it reaches authorization.

That is the intended behaviour of a freeze, and it is why this stage does not write a freeze
`1.5`: a new execution freeze can only be written from a harness that has passed its own live gate.
Sequence after a successful live E2F run: write freeze 1.5 from the new source digest, then
re-authorise c3.

---

## I. What is still not proven

1. A real container has never run this gate.
2. `atl-uat-python:3.12` has not been asked to clone from `/tmp/project.bundle` in this stage.
3. The isolation probes are the runtime's own, but their last real observation is the E2E smoke.
4. A provisioning failure that happens *after* `create` destroys internally inside
   `ProjectSandboxManager`; the handle is not returned to the caller, so the gate can only say
   `UNKNOWN` and name the label to search for.
5. `GoalSandbox.run_public_command()` reports `ok=True, exit_code=None` as `status="PASS"`. This is
   a defect in the **frozen runtime** and was not patched here. It is reproduced by an
   `xfail(strict=True)` test, and the gate never trusts `status` — it decides from `ok` and
   `exit_code` itself. Suggested one-line patch, pending Owner approval:

   ```python
   if not ok:
       status = "ERROR"
   elif exit_code is None:
       status = "UNKNOWN"
   elif int(exit_code) == 0:
       status = "PASS"
   else:
       status = "FAIL"
   ```

   Exposure today is low: the OpenSandbox adapter coerces a missing exit code to `0` before
   returning, so a live run cannot reach that branch.
