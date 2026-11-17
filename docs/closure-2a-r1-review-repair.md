# Closure-2A-R1 — Preregistration and Evidence Integrity Repair

Status: `READY_FOR_INDEPENDENT_REVIEW` (offline only; live pilot still blocked).

This stage repairs six defects found by an independent review of Closure-2A. It changes **no task,
no oracle, no requirement, no DAG, no arm, no budget parameter and no router policy**. What changes
is how the registration is *formed* and how the offline preflight *proves* things.

Model calls: **0**. Sandbox runs: **0**. Budget approval: **still `PENDING_OWNER_APPROVAL`**.

---

## 1. Baseline and identity

| Item | Value |
|---|---|
| `PREVIOUS_APPROVED_SHA` | `d1f53d747b9f50ca531b7382f5796b4c368a6ce3` |
| C2-A task commit | `7f5c1eae12e466e6a37ce2a7d77608ba65a009a5` |
| `C2_A_HEAD_SHA` (branch point) | `ac7af16b4506afafe6e932e2cc351b04354b9f57` |
| Repair branch | `fix/closure-2a-r1-review` |
| Repair commit | `71f5c8e0784c281e23b5c1b37d31640f8f9c50cd` |
| R2 baseline | `9a7767aff233397a4cb200bed24a842cf2f49bbd` |
| R2 repair commit | `PENDING_R2_COMMIT` |
| C2-A preregistration digest | `f7bbdfb8350589a959d78be00a9dc150c3b12fd6d277cf0fe2cee8a8b536b058` |
| R1 preregistration digest | `3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765` |
| Router policy | `r0.1.0`, `5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244` (unchanged) |
| C2-A artifact | `experiments/results/closure2/preflight.json` — 34 checks, untouched |
| R1 artifact | `experiments/results/closure2/preflight-r1.json` — 60 checks |

The C2-A artifact and its digest remain in the repository as history. The R1 artifact has a different
`stage` (`closure-2a-r1`), a different digest and a `supersedes` block, so the two cannot be confused.

### 1.1 Why the digest changed

Measured by diffing the C2-A payload (dumped from a checkout of `ac7af16`) against the R1 payload:

| Field | C2-A | R1 |
|---|---|---|
| `tasks` | — | **byte-identical** (`tasks_identical: true`) |
| `benchmark_id`, `benchmark_version`, `dataset_version`, `routing_policy_version` | — | identical |
| `comparisons`, `budget_semantics`, `execution_order`, `metrics`, `exclusions`, `unsupported`, `release_boundary`, `digest_groups`, `same_dag_groups` | — | identical |
| `budget.approval` | `"PENDING_OWNER_APPROVAL"` | **removed** |
| `arms[*].config` | absolute path on the authoring machine | repository-relative POSIX path |
| `arms[*].config_semantic_digest` | — | **added** (content digest of the YAML) |
| `arms[*].config_digest` | file name only | recomputed over content + budget + contract |
| `protocol_revision` | — | **added**, `"closure-2a-r1"` |
| `supersedes` | — | **added**, names the C2-A digest and both C2-A commits |
| `layout_ablation` | — | **added**, the flattened A1-L/A1-P difference |

Task content digests are unchanged: the preflight compares them against the C2-A table
(`protocol.C2A_FROZEN_TASK_DIGESTS`) and every key matches for all three tasks. The digest moved
because the registration *structure* moved, not because the benchmark moved.

---

## 2. Modified files

| File | Why |
|---|---|
| `experiments/evaluation/closure2/protocol.py` | relative paths, content-bound config digests, approval separation, pinned R1 identity, YAML value normalisation |
| `experiments/evaluation/closure2/preflight.py` | four independent oracle sessions, failure classification, content-based leak detection, router gate, config-binding probe, approval enforcement check |
| `tests/unit/test_closure2_benchmark.py` | rewritten for R1 (52 tests) |
| `tests/integration/test_closure2_preflight.py` | rewritten for R1 (26 tests, incl. negative controls) |
| `docs/closure-2-preregistration.md` | R1 identity, approval separation, verification checklist, revision history |
| `docs/closure-2-fairness-protocol.md` | new arm/config digests, oracle-independence and routing-evidence section |
| `docs/closure-2-benchmark-design.md` | per-task session classification table, new check names |
| `docs/closure-2a-r1-review-repair.md` | this report |
| `experiments/results/closure2/preflight-r1.json` | R1 preflight artifact |

Not modified: `src/atlasos/**`, `catalog.py`, `experiments/configs/*.yaml`, the router policy,
`docs/architecture-design-contract.md`, AnyCode.

---

## 3. The six repairs

Each defect below was first reproduced on the C2-A commit, not inferred from the review text.

### R1-01 — Budget approval was inside the frozen definition

**Reproduced.** On `ac7af16`, setting `BUDGET["approval"] = "APPROVED"` changes the registration
digest `58cb24a3… → c35a686f…`, and `budget_protocol()` in the same preflight fails any value other
than `PENDING_OWNER_APPROVAL`. Approving therefore both invalidated the registration and failed the
preflight: a closed loop with no legal exit.

**Fix.** `BUDGET` keeps parameters only. `APPROVAL_RECORD` is a separate mutable record with
`status`, `approved_digest`, `approved_by`, `approval_commit`, `approved_at`; it is not in the
payload. `approval_gate()` returns `execution_ready` only when the record is `APPROVED`, names the
**current** digest, and carries approver, commit and timestamp. `require_execution_approval()` raises
`PermissionError` otherwise and is the only sanctioned entry point for a live run.

**Negative tests.** pending record → `execution_ready` false and `PermissionError`; approval of
`"0"*64` → fail closed; missing approver / commit / timestamp → fail closed; flipping the record to
a correct approval leaves the digest unchanged; changing `BUDGET["single"]["token_budget"]` moves
both the registration digest and the affected arm digests.

### R1-02 — Registration digest depended on the checkout path

**Reproduced.** The same commit `ac7af16`, checked out at `D:\Work\_atlas_c2a_audit` instead of the
authoring directory, produces `58cb24a3573a69…` while the committed artifact records
`f7bbdfb8350589…`. The payload embedded `str(LEGACY_CONTEXT_CONFIG)`, an absolute host path.

**Fix.** `ARMS` carries repository-relative POSIX paths. `resolve_repo_path()` joins them onto the
repository root at execution time and refuses absolute paths, drive letters and `..` segments. The
payload contains no host path (`checks.no_absolute_path_in_frozen_payload`).

**Negative tests.** the digest computed from two independent temporary checkouts of the same YAML is
equal to each other and to the pinned R1 constant; `/etc/passwd`, `C:\Windows\win.ini`,
`../outside.yaml`, `experiments/../../x.yaml` and `""` are all refused; the payload contains no
string starting with `/` or a drive letter.

### R1-03 — Arm digest was bound to the config file name, not its content

**Reproduced.** On `ac7af16`, rewriting `provider.model: deepseek-chat` to `deepseek-reasoner` inside
`closure2-shared-prefix.yaml` leaves `arm_config_digest("A1-P")` at `25ed19eb…` — unchanged.

**Fix.** `config_semantics()` parses the YAML, normalises it to canonical-JSON-safe data (dates →
ISO strings, integral floats → ints) and `config_semantic_digest()` digests `{path, content}`.
`arm_config_digest()` includes it, together with the arm record, the topology budget slice and the
shared resource contract. `layout_ablation_delta()` flattens both files and requires
`context.prompt_layout` to be the only differing key.

**Negative tests.** a value change moves the arm, config and registration digests; a comment-only
edit keeps them; `0.006` → `0.006000` and `0.0` → `0` keep them; changing `tools.max_results`,
`sandbox.image` or `tools.search_backend` in either file makes `only_layout_differs` false and is
named in `differing_keys`; `preregistration_digest` changes when a budget parameter changes.

### R1-04 — Hidden oracle was not verified independently of the public tests

**Reproduced.** The C2-A `_run_pytest(root)` takes no target list: it always runs the whole
workspace. The committed `preflight.json` for N1 records `seed_oracle_fails: true` with
`"Interrupted: 2 errors during collection"` and `ERROR test_hidden_lifecycle.py` / `ERROR
test_public.py`. The hidden module never imported, so no assertion in it ever ran. The flag was true
because *some* test failed, not because the oracle caught a defect.

**Fix.** `task_selftest()` runs four independent sessions — seed+public, seed+hidden,
reference+public, reference+hidden — each in its own disposable workspace with an explicit target
list. `_run_pytest()` refuses an empty target list (`NO_TARGETS`) because bare `pytest` would collect
the workspace, and maps `TimeoutExpired` to `TIMEOUT`. `classify_pytest()` separates `PASSED`,
`TEST_FAILURE`, `COLLECT_ERROR`, `NO_TESTS_COLLECTED`, `NO_TARGETS`, `USAGE_ERROR`, `INTERRUPTED`,
`INTERNAL_ERROR` and `TIMEOUT`; only `TEST_FAILURE` and `COLLECT_ERROR` count as the seed failing the
contract, and every harness-only kind is recorded as a harness problem.

**Result on the frozen tasks.** P1 and P2 seed legs fail by assertion (`TEST_FAILURE`). N1's seed
legs are `COLLECT_ERROR` with the cause recorded verbatim:
`ImportError: cannot import name 'reject' from 'lifecycle.approval'`. It is reported as
`seed_hidden_failed_without_executing_assertions: true`, never as a failed assertion. The reference
legs prove the oracles are real and satisfiable: P1 11 tests, P2 12 tests, N1 7 tests executed.

**Negative tests.** a synthetic task whose seed fails a public test but passes the hidden probe
yields `seed_public_fails: true`, `seed_hidden_fails: false` — the exact Closure-2A false positive,
now impossible; an empty target list yields `NO_TARGETS` and is not counted as a defect; the
preflight asserts that each session's target list is exactly its own file set, that the two target
sets are disjoint, and that the four workspaces are distinct.

### R1-05 — Leak detection compared file names, not content

**Reproduced.** The C2-A `workspace_isolation()` flagged `reference-only file staged` only when a
reference file name was **absent** from the visible set. Every reference file is a migration of a
seeded file, so the branch could not fire; the detector never looked at reference bodies at all and
never checked that the visible content equals the frozen seed.

**Fix.** `workspace_isolation()` now asserts (a) the visible file set is exactly the seed key set,
(b) every visible body equals its seed body, (c) no hidden-test body appears in a visible file under
any name, (d) no *answer-bearing* reference body — one that differs from its seed counterpart —
appears in a visible file. `frozen_identity()` compares the live task against the C2-A table, so the
comparison target is a stored baseline rather than the object under test.

**Negative tests.** a changed reference body copied in under a new name is caught and reported with
the source module; a renamed hidden oracle copied in is caught by body match; a single edited seed
file breaks `seed_digest` against the frozen table and fails `answer_leak_guard`; a reference module
that legitimately equals its seed is **not** reported.

Documented boundary: whole files and whole bodies only; snippet-level leakage is not detected.

### R1-06 — Router decisions were recorded, never gated

**Reproduced.** The C2-A preflight stored `router` output and checked only
`policy.digest() == 5e13c910…`. A changed decision under an unchanged policy digest would have passed.

**Fix.** `ROUTER_EXPECTATIONS` and `router_gate()` assert, per family: route, required reason codes,
forbidden reason codes, required and forbidden blocked gates, wave-0 worker count, wave-0 write
disjointness and declared-vs-owned write paths. Observed route, reason codes and blocked gates are
retained in the artifact. Each task registers a `router_decision_expected` check, so the overall
`status` depends on the decisions.

**Negative tests.** a stubbed `plan_route` returning `direct` while the policy digest is unchanged
fails the gate naming the route; a stubbed `delegate_parallel` with no reason codes fails naming the
missing codes; a collapsed wave-0 fails naming the width; overlapping wave-0 write sets fail naming
the overlap.

---

## 4. Freeze integrity

### 4.1 Task identities (unchanged)

| Task | seed | reference | oracle | requirement | DAG |
|---|---|---|---|---|---|
| P1 | `f9378c44…` | `80706d09…` | `95174a0d…` | `06064001…` | `5795f390…` |
| P2 | `cdc114a9…` | `6e907eb9…` | `a82b6add…` | `4886c52b…` | `58281468…` |
| N1 | `400f56d3…` | `5a04ee0c…` | `a6f05cfc…` | `fcc12d5d…` | `66eb7577…` |

Full values in `docs/closure-2-preregistration.md` §1. All match the C2-A preregistration table and
are asserted by `test_the_frozen_task_content_matches_the_independent_c2a_identity`.

### 4.2 Protocol fields that changed

See §1.1. The task section is byte-identical; only registration structure changed.

### 4.3 Unchanged frozen objects

Benchmark id/version, dataset version, router policy and its digest, the six arms, the arm matrix,
comparison cells, execution order, metrics, exclusions, unsupported cells, release boundary, all
three seeds, references, public tests, hidden oracles, requirements, candidate DAGs, deliverables,
allowed and forbidden paths, and both YAML config files.

---

## 5. Preflight evidence

`experiments/results/closure2/preflight-r1.json`, produced by:

```bash
python -m experiments.evaluation.closure2.preflight \
  --out experiments/results/closure2/preflight-r1.json
```

| Item | Value |
|---|---|
| `stage` | `closure-2a-r1` |
| `status` | `PASSED` |
| Checks | 60 / 60 at R1; **62 / 62** after R2 (§R2.5 — same file, two checks added) |
| `preregistration_digest` | `3080f313…` (equals `frozen_preregistration_digest`) |
| `execution_ready` | `false` |
| `approval.status` | `PENDING_OWNER_APPROVAL` |
| `model_calls` / `sandbox_runs` | `0` / `0` |
| `absolute_paths_in_payload` | `[]` |
| `router_policy.digest` | `5e13c910…` (frozen) |

### 5.1 Oracle independence

| Task | seed+public | seed+hidden | ref+public | ref+hidden | oracle tests executed | seed hidden failed without executing assertions |
|---|---|---|---|---|---|---|
| P1 | `TEST_FAILURE` | `TEST_FAILURE` | `PASSED` | `PASSED` | 11 | no |
| P2 | `TEST_FAILURE` | `TEST_FAILURE` | `PASSED` | `PASSED` | 12 | no |
| N1 | `COLLECT_ERROR` | `COLLECT_ERROR` | `PASSED` | `PASSED` | 7 | **yes** (reported, not hidden) |

N1's seed cannot import the v2 interface; the artifact records the cause string, and the reference
leg proves the oracle executes.

### 5.2 Router decisions

| Task | Route | Reason codes | Blocked gates | Wave 0 |
|---|---|---|---|---|
| P1 | `delegate_parallel` | `DELEGATION_ADMITTED` | — | `aggregate`, `parse` (2, disjoint) |
| P2 | `delegate_parallel` | `DELEGATION_ADMITTED` | — | `csv`, `json` (2, disjoint) |
| N1 | `direct` | `SHARED_WRITE_RISK`, `UNSTABLE_INTERFACE` | `GATE_2_DEPENDENCY_COUPLING` | `machine` (1) |

Identical to the C2-A registration; now enforced by `router_gate`.

### 5.3 Isolation and identity

`workspace_isolation` and `frozen_identity` pass for all three tasks; every visible file set equals
its seed, no hidden body and no answer-bearing reference body is present.

### 5.4 Config binding

`config_binding.facts` — content change moves the digest, comment change does not, two checkouts
agree, a second effective key breaks layout parity, path escapes are refused: all true.

### 5.5 Approval gate

`approval_outside_frozen_identity` (a simulated approval leaves the digest unchanged),
`wrong_digest_approval_fails_closed`, `approval_record_consistent` and
`execution_blocked_while_not_approved` all pass. The live record stays pending: this stage does not
approve anything.

---

## 6. Regression

| Command | Result |
|---|---|
| `python -m pytest` | `1138 passed, 6 skipped, 1 deselected` (0 failed) |
| `python -m ruff check .` | `All checks passed!` |
| `python -m ruff format --check .` | `423 files already formatted` |
| `python -m pyright` | `0 errors, 0 warnings, 0 informations` |
| `python -m experiments.evaluation.closure2.preflight` | `PASSED`, 60/60, exit 0 |

Closure-2 subset: `tests/unit/test_closure2_benchmark.py` (52 tests) and
`tests/integration/test_closure2_preflight.py` (26 tests), 78 passing.

The 6 skips are pre-existing optional-dependency/platform skips, unchanged by this stage.

**Environment note.** On this machine the default `pytest` run can exit `1` at session teardown after
reaching `100%` with no failure recorded: the shell's bulk-delete guard blocks pytest's garbage
collection of its own temporary directories (`[safe-delete]
[SAFE_DELETE_BULK_CONFIRM_REQUIRED] … pytest-of-86180/garbage-…`). Running with an isolated
`--basetemp` gives `exit 0` and the summary above. This is a host artifact, not a test outcome; it is
recorded here so a reviewer who sees `exit 1` does not mistake it for a regression.

---

## 7. Scope audit

- `src/atlasos/**` — untouched.
- `experiments/evaluation/closure2/catalog.py` — untouched (task content frozen).
- `experiments/configs/*.yaml` — untouched.
- Router policy `r0.1.0` — untouched.
- AnyCode `0.9.0` — untouched, dependency pin unchanged.
- `docs/architecture-design-contract.md` — untouched.
- `master` — untouched.
- `feat/final-closure` — untouched at `ac7af16`; the repair branch was cut from it.
- `feat/placement-coding-integration` (`0252bdb`) — untouched.
- Model calls: 0. Sandbox runs: 0. Network calls to a provider: 0.

---

## 8. Remaining risks

1. **Budget approval is still pending.** `execution_ready` is false by construction; Closure-2B
   cannot start until the owner writes an approval naming `3080f313…`. Offline `PASSED` must not be
   reported as permission to run.
2. **The approval record is a source constant**, not a signed artifact. It is traceable to a commit
   and a digest, but someone with write access can edit it; reviewers must check the commit, not the
   field.
3. **Leak detection is whole-body only.** A paraphrase of the reference, or a partial snippet, is not
   detected. The boundary is stated in the artifact and in the fairness protocol.
4. **N1's seed-leg oracle evidence is a collection error.** The task contract is genuinely
   unimplemented, and the reference leg proves the oracle is satisfiable, but the seed leg never
   asserts. Any future N1 claim must repeat that distinction.
5. **Preflight cost.** Each run spawns 12 pytest subprocesses (about 10 s total on this machine) and
   five temporary checkouts for the config probe. Acceptable offline, not something to run in a loop.
6. **No CI workflow** invokes the Closure-2 preflight. Adding one is out of scope for this stage and
   would need a decision about running subprocess-spawning tests in CI; the command is documented so
   it can be added later.
7. **The budget's absolute numbers remain unvalidated by any run.** Nothing here measured turns,
   tokens, cache behaviour or cost.

---

## 9. How to re-verify

```bash
python -m experiments.evaluation.closure2.preflight \
  --out experiments/results/closure2/preflight-r1.json
python -m pytest tests/unit/test_closure2_benchmark.py tests/integration/test_closure2_preflight.py
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
```

A reviewer should see: `PASSED`, 62/62, digest `3080f313…`, `execution_ready false`. A different
digest means the frozen content moved; stop and cut a new version instead of re-freezing.

---

# R2 — Final Preregistration and Execution Integrity Repair

Status: `READY_FOR_FINAL_INDEPENDENT_REVIEW` (offline only; live pilot still blocked).

A second review of R1 found three boundary defects. Same rules as R1: no task, seed, reference,
oracle, requirement, DAG, arm, budget parameter or router policy changed, and the **R1
preregistration digest is unchanged** (`3080f313…`). Model calls: 0. Sandbox runs: 0. Budget
approval: still `PENDING_OWNER_APPROVAL`.

## R2.1 Root cause

Each defect was reproduced on the R1 commit (`9a7767a`) before it was fixed, in a detached worktree
of that commit, not inferred from the review text.

### R2-01 — the approval gate bound the *current* digest, not the frozen one

`approval_gate()` computed `target = preregistration_digest() if digest is None else digest` and
compared the approval against `target`. Two consequences:

* a caller could pass `digest=<anything>`, so the gate never had to look at the repository;
* with no override, the gate accepted a **self-consistent drift**: change
  `BUDGET["single"]["token_budget"]` from 260 000 to 280 000, sign the new digest, and
  `execution_ready` became `True`.

Reproduced on `9a7767a`: drift to 280 000 → digest `3080f313…` → `7f0eafa8…`; a record naming
`7f0eafa8…` yielded `execution_ready = True` and `require_execution_approval()` **returned
normally**. A benchmark nobody registered could have been executed with a valid-looking approval.

### R2-02 — POSIX-only traversal validation, Windows path resolution

`resolve_repo_path()` validated `PurePosixPath(text).parts` and then handed the same string to
`Path.joinpath`. On Windows `\` is a separator for the filesystem but an ordinary character for
`PurePosixPath`, so `experiments\..\..\outside.yaml` was one harmless-looking component during
validation and three components — the last two of them escaping — during the file access.

Reproduced on `9a7767a`: `resolve_repo_path(r"experiments\..\..\outside.yaml")` returned
`…\experiments\..\..\outside.yaml`, which the OS resolves to a directory above the checkout. The
same hole admitted `..\outside.yaml` and `\\server\share\file.yaml`.

### R2-03 — exit code 1 was read as "assertion failed"

`classify_pytest()` returned `TEST_FAILURE` for every `returncode == 1`. pytest also uses 1 for a
fixture setup error, a fixture teardown error, and a session that has both. Because the seed gates
counted `TEST_FAILURE` as a valid defect, a broken fixture or a mixed session would have been
reported as "the seed does not satisfy the contract" — a false positive of exactly the class R1 was
supposed to eliminate.

Reproduced on `9a7767a`: `classify_pytest(1, <setup-error output>)` → `TEST_FAILURE`;
`classify_pytest(1, <failure + setup-error output>)` → `TEST_FAILURE`.

## R2.2 Implementation

| File | Change |
|---|---|
| `protocol.py::resolve_repo_path` | two layers — lexical (`\\` refused, `/`-only, no drive/UNC/stream prefix, no `..`) then `resolve()`-based containment via `Path.is_relative_to`; no `startswith` string comparison |
| `protocol.py::approval_gate` | target is the constant `C2A_R1_PREREGISTRATION_DIGEST`; the current digest is checked *against* it; the `digest` override is removed; `root` is added so a test can point at another checkout (it can only make the gate stricter); result now reports `frozen_digest` and `current_digest` |
| `protocol.py::require_execution_approval` | takes no path or digest argument at all, so the execution path always evaluates the live repository |
| `preflight.py::classify_pytest` | phase markers (`ERROR at setup of`, `ERROR at teardown of`) plus the summary counts; `TEST_FAILURE`, `SETUP_ERROR`, `TEARDOWN_ERROR`, `MIXED_ERROR`, `UNKNOWN_ERROR` and the pre-existing kinds are distinct; anything unreadable is `UNKNOWN_ERROR` |
| `preflight.py::collect_cause_class` | a `COLLECT_ERROR` becomes `INTERFACE_MISMATCH` only when the missing name belongs to a module the workspace ships; otherwise `ENVIRONMENT` |
| `preflight.py::session_is_valid_defect` / `harness_blocked` | one place decides "this proves a defect" versus "this blocks the run"; the seed gates and the `no_harness_failure` check both use it |
| `preflight.py::approval_binding_probe` | new check: a faithful checkout can be approved, a drifted checkout approved with its own digest cannot |
| `preflight.py::run_preflight` | two new checks (below), `approval_binding` section, `repair_revision: closure-2a-r2`, `cause_class` in `oracle_independence` |

`PROTOCOL_REVISION` stays `closure-2a-r1`: the frozen definition did not move, only the code that
enforces it. The artifact carries a separate `repair_revision` field so the evidence says which
build produced it without touching the identity.

### New preflight checks (60 → 62)

| Check | Why |
|---|---|
| `approval_gate_binds_frozen_digest` | proves, in throwaway checkouts, that a drifted registration cannot be approved by signing the drift |
| `approval_gate_has_no_digest_override` | proves the bypass parameter is gone from both gate functions |

Every R1 check is retained unchanged.

## R2.3 Negative evidence

| Defect | Control | Result |
|---|---|---|
| R2-01 | budget drift to 280 000 + approval naming the drifted digest | `execution_ready is False`, `PermissionError`, problem text names the frozen definition |
| R2-01 | YAML drift in a temporary checkout + approval naming the drifted digest | refused |
| R2-01 | faithful copy in another checkout + approval of `3080f313…` | accepted (the check is content-bound, not path-bound) |
| R2-01 | `approval_gate(digest=…)` / `require_execution_approval(digest=…)` | `TypeError`; the parameter does not exist |
| R2-02 | `..\outside.yaml`, `experiments\..\..\outside.yaml`, `\\server\share\file.yaml`, `experiments\configs\x.yaml` | `ValueError: … must use '/' as the only separator` |
| R2-02 | `C:/Windows/win.ini`, `C:relative/inside.yaml` | `ValueError: … drive or stream prefix` |
| R2-02 | `/etc/passwd`, `//server/share/file.yaml`, empty | refused |
| R2-02 | directory link pointing outside the root; sibling tree `repo-other` reached through a link | `ValueError: … resolves outside the repository root` (executed via a Windows junction, not skipped) |
| R2-03 | real pytest session per shape | `TEST_FAILURE` / `SETUP_ERROR` / `TEARDOWN_ERROR` / `COLLECT_ERROR`+`INTERFACE_MISMATCH` / `COLLECT_ERROR`+`ENVIRONMENT` / `MIXED_ERROR`, each with the expected `valid_defect` / `harness_blocked` |
| R2-03 | a seed whose hidden oracle has a broken fixture | `seed_hidden_fails is False`, `harness_blocked.seed_hidden is True` |
| R2-03 | a seed whose hidden oracle imports a missing third-party package | `COLLECT_ERROR`/`ENVIRONMENT`, `seed_hidden_fails is False` |

`collect_cause_class` is conservative on purpose: without a workspace to compare against, a
collection error is `ENVIRONMENT`, never a mismatch. That direction can only cause the preflight to
fail, never to pass.

## R2.4 Freeze preservation

| Object | Evidence |
|---|---|
| R1 preregistration digest | `3080f313…` — unchanged; asserted by `preregistration_digest_matches_frozen_r1` and pinned by unit test |
| Task content | live `task_digests` still equal the C2-A table for all three tasks |
| Router policy | `r0.1.0` / `5e13c910…`; decisions unchanged (P1, P2 `delegate_parallel`; N1 `direct`) |
| Configs | `experiments/configs/*.yaml` untouched; semantic digests `16f587b3…` / `751beb15…` unchanged and pinned by unit test |
| Budget parameters | untouched; still inside the frozen payload |
| N1 collection error | still reported as `COLLECT_ERROR`, now with `cause_class: INTERFACE_MISMATCH` and the original `ImportError` text |

The repairs live entirely in the code that *evaluates* the registration. Nothing inside
`preregistration_payload()` changed, which is why the digest cannot have moved.

## R2.5 Regression

| Command | Exit | Result |
|---|---|---|
| `python -m experiments.evaluation.closure2.preflight --out …/preflight-r1.json` | 0 | `PASSED`, 62/62, digest `3080f313…`, `execution_ready false` |
| `python -m pytest tests/unit/test_closure2_benchmark.py` | 0 | 69 passed |
| `python -m pytest tests/integration/test_closure2_preflight.py` | 0 | 40 passed |
| `python -m pytest` (with an isolated `--basetemp`) | 0 | 1169 passed, 6 skipped, 1 deselected |
| `python -m ruff check .` | 0 | `All checks passed!` |
| `python -m ruff format --check .` | 0 | `424 files already formatted` |
| `python -m pyright` | 0 | `0 errors, 0 warnings, 0 informations` |

The R1 test count was 78 (52 + 26); R2 raises it to 109 (69 + 40). The 6 skips are the pre-existing
optional-dependency/platform skips; the two link tests are **not** among them — the host accepted
`mklink /J`, so they executed. The `--basetemp` note in §6 still applies: a default run can end
with a non-zero exit at teardown when the shell's bulk-delete guard blocks pytest's own temporary
directory cleanup, after the run has already finished.

## R2.6 Limitations

1. **No Closure-2B live run has been executed.** Nothing here measured turns, tokens, cache
   behaviour, latency or cost, and no such claim is made.
2. **The approval remains pending.** `execution_ready` is `false` by construction; this stage did not
   approve anything.
3. **Verifying the gate is not verifying a runner.** There is no Closure-2B runner yet. When one is
   written it must call `require_execution_approval()` before any model request or sandbox
   provisioning; nothing in this repository can enforce that for code that does not exist.
4. **Path safety covers the frozen-config loader only.** It is not a general filesystem sandbox: it
   does not stop an already-running process from opening arbitrary paths, does not validate symlinks
   *inside* the root that stay inside it, and does not resolve Windows short (8.3) names beyond what
   `Path.resolve()` does.
5. **The pytest classifier is text-based.** It recognises the phase markers and summary lines of
   pytest 7/8/9 as printed by this environment (9.2.0 verified). A future pytest that renames those
   markers degrades to `UNKNOWN_ERROR`, which fails the preflight — fail closed, but the preflight
   would need updating.
6. **The collection-cause heuristic is about locality, not intent.** A missing *local* module is
   classified as a workspace mismatch only when a matching file or package exists in the workspace;
   otherwise it is `ENVIRONMENT`. That under-counts exotic cases and never over-counts.
7. **Documentation drift, deliberately not fixed here.** `docs/closure-2-preregistration.md` §8 still
   says "all 60 checks"; the count is 62 at this revision. The file is outside this stage's declared
   change scope, so it is reported instead of edited. The digest and the four conditions in that
   section are unaffected.
8. **No CI workflow** runs the preflight; the command is documented so it can be added later.
