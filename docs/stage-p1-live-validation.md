# Stage P1 — Real DeepSeek Main-driven Pipeline Validation

Branch: `feat/stage-p01-execution-semantics` at `00b6f7aec7a7b83e7768f03a346a26abc797fcc0`
(Stage P0.1, `STAGE_P01_ACCEPTED_OFFLINE`). AnyCode 0.9.0.

**One live run, executed once. Decision: `STAGE_P1_CORE_PASS_NO_DELEGATION`.**

The pipeline ran end to end on real DeepSeek, a real OpenSandbox and real tool calls: the Main read
the workspace, ran the public suite (which failed), implemented `subtract`, re-ran the suite (which
passed) and answered. The independent oracle passed. **The Main chose not to delegate**, so the
multi-agent loop was not exercised by this run. No second run was attempted.

---

## A. Provenance

| Item | Value |
|---|---|
| Repository | `D:\Work\AtlasRuntime`, branch `feat/stage-p01-execution-semantics` |
| Runtime HEAD | `00b6f7aec7a7b83e7768f03a346a26abc797fcc0` (clean tree, local = origin) |
| Fixture | `experiments/fixtures/stage_p1_calculator`, commit `54aa9fbd70c105c149d5d6f023d0018dcc6e2f5d` |
| Run id | `stage-p1-live-20261119T045534Z` |
| Run directory | `%TEMP%\atlas-p1-live-1\runs\stage-p1-live-20261119T045534Z` (scratch, outside the repo) |
| Evidence bundle | `experiments/stage_p1_live/evidence/run-1/` |
| Provider / model | `deepseek` / `deepseek-chat` |
| Config | `tools.mode=progressive`, `sandbox=opensandbox` (`atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB, 20 min) |
| Routing policy | `m.1.0` (delegation) over frozen `R0.1.0` |
| Public command digest | `973146498eddafc565c6384df51bebcab53d636f249c31be20be88ae4763c683` |
| Oracle command digest | `5597ae5b7970d312cb06efa00fe2d93c583c619e6da7295cd9a460acf0d2754f` |
| Acceptance spec digest | `sha256:5327e9961bc5b029f7582049e40194924766b7374d7d3f6963fda27dc3c8ea2f` |
| Budget | `token_budget=240000`, admission-based (`guarded`) |
| Main turns / write scope | 12 / `app/calc.py` only |

The fixture is the Stage P0 calculator project extended to three modules: `app/calc.py`
(`add`, `subtract` missing), `app/text.py` (`normalize`), `app/units.py`
(`to_grams`, `total_grams` built on `calc.add`), plus `tests/test_public.py`, a README stating the
contract and a `.gitignore`. It is provisioned as a clean git repository by
`experiments/stage_p1_live/run_stage_p1_live.py`.

## B. Preflight

`run_live_preflight` returned **READY**, 0 blocking failures, all 19 checks OK
(`evidence/run-1/preflight.json`):

```text
sandbox_configured OK | sandbox configured
sandbox_provider_real OK | sandbox.provider='opensandbox'
sandbox_isolation_required OK | require_real must stay True
sandbox_workspace_root OK | '/workspace/project' is an absolute in-container path
source_repo_clean OK | fixture is clean at 54aa9fbd70c1
main_write_scope OK | ['app/calc.py']
main_write_scope_relative OK | all write paths are workspace-relative
public_tests_configured OK | public suite 'public'
public_tests_separate_from_oracle OK | commands differ
acceptance_configured OK | contract with a dynamic command
acceptance_digest_declared OK | signed by the host
budget_configured OK | token_budget=240000 (estimate-based, not a hard cap)
delegation_bounds OK | max_parallel_children=2
context_metrics OK | per-request main context recorded unconditionally
provider_deepseek OK | provider='deepseek'
model_fixed OK | model='deepseek-chat' == config.provider.model
api_key_present OK | DEEPSEEK_API_KEY is set (read, never sent)
live_provider_factory OK | no injected factory
scratch_run_directory OK | runs_root outside experiments/results and experiments/runs
```

A separate sandbox-only rehearsal (no model call) verified the oracle command both discriminates and
quotes correctly: unsolved → exit 2 (`ImportError: cannot import name 'subtract'`), wrong
implementation → exit 1 (`assert 13 == 5`), correct implementation → exit 0 with `4 passed` and the
hidden assertion's `HIDDEN_OK`.

## C. Execution trace

Times are UTC, from the run's trajectory transcript.

```text
04:55:34  run_goal starts: sandbox provisioned (git bundle + clone + rev-parse in-container)
04:55:43  sandbox READY, network probe PASS, host separation clean
04:55:51  Main turn 1  list_files .            → 4 entries            ALLOW
          Main turn 1  bash "ls -la; git log" → ok                    ALLOW
04:55:54  Main turn 2  file_read ×6            (calc, text, units, tests, README, .gitignore)
04:55:55  Main turn 3  bash "cd … $(pwd); …"   → DENY  OWNERSHIP_UNCLASSIFIABLE_SHELL
04:55:56  Main turn 3  bash "python3 -V…"      → ok                    ALLOW   (recovered)
04:55:58  Main turn 4  grep + bash             → 1061 / 52 bytes
04:56:01  Main turn 5  run_public_tests        → FAIL exit 2, 1132 chars shown
04:56:04  Main turn 6  file_edit app/calc.py   → "1 occurrence replaced"   ALLOW
04:56:04  Main turn 7  file_read + run_public_tests → PASS exit 0, 96 chars
04:56:07  Main turn 8  bash (git status/diff)  → ok
04:56:17  Main turn 9/10: final answer, sandbox CLOSED, run_terminal completed
```

The Main's final answer, verbatim in part:

> No delegation was needed — the task was small and entirely dependent on reading three short
> files, so splitting it would have added coordination cost without parallelism.

## D. Delegation evidence

| Evidence | Value |
|---|---|
| `delegate_tasks` calls | **0** |
| Router decisions | none (the router is only consulted through the tool) |
| Delegated tasks | 0 |
| Child profiles / statuses | none |
| `max_observed_child_concurrency` | 0 |
| Handoff refs / payload | none / 0 chars |
| `delegation_rounds` | 0 |

The delegation plane was available and authorized (`delegate_tasks` was in the Main's tool surface,
`research_worker`/`coding_worker` profiles were registered, `max_parallel_children=2`), and the Main
decided against using it. Its stated reason is quoted above; that is a behavioural observation, not
a runtime fault, and it was **not** corrected by re-running or by editing the prompt.

## E. Context metrics

All values come from real provider usage; `unavailable_fields` is empty.

```json
{
  "main_requests": 10,
  "main_context_tokens_by_request": [2762, 2957, 3810, 3934, 4145, 4586, 4950, 5160, 5430, 5582],
  "main_peak_context_tokens": 5582,
  "main_cumulative_input_tokens": 43316,
  "main_cache_hit_tokens": 37888,
  "main_cache_miss_tokens": 5428,
  "main_completion_tokens": 1382,
  "child_total_prompt_tokens": 0,
  "child_completion_tokens": 0,
  "system_total_tokens": 44698,
  "handoff_payload_chars": 0
}
```

Accounting: a request's context is the full prompt (miss + cache reads); `43316 = 5428 + 37888`
checks out; `system_total = 43316 + 1382`. With no children there is no child column to attribute.
No context-reduction percentage is reported: this run has a single arm and no direct baseline.

## F. Sandbox and safety

| Item | Value |
|---|---|
| Provider | `opensandbox`, `provider_is_real: true`, isolation `container`, networking `allowlist` |
| Sandbox / binding | `b5d0e9fb-33a8-454a-84e6-53eb3c64b1c8` / `348daaad…ef131`, state `READY` |
| Network probe | `PASS` (empirical egress attempt blocked) |
| Host separation | `host_only_visible=false`, `sandbox_only_visible_on_host=false`, `sandbox_only_read_back=true` |
| Provisioning | 4 commands + 1 upload, in-container clone verified by `rev-parse HEAD` |
| Export | patch 1 file, workspace 7 files, 20 480 bytes |
| Ownership | `enforce`, coverage `PARTIAL`, 17 checked / 16 allowed / **1 denied** |
| Denial | `bash` with a `$(` substitution → `OWNERSHIP_UNCLASSIFIABLE_SHELL`; the tool did not run |
| Action safety | every call went through the gate; `run_public_tests` used rule `allow-tests.public` |
| Budget | 10 requests, 0 admission rejections, peak reserved 10 511, remaining 195 302, ratio 1.0988 |
| Cleanup | `destroyed: true`, `retained: false`; no container left in the Docker daemon |

The single denial is the stage-P0.1 semantics working in production: the refused command never
executed, the Main read the reason and immediately retried with a legal command in the same turn.

## G. Verification

**Public suite (the Main's channel)**, contract `python3 -B -m pytest -q tests/test_public.py`:

| Call | Status | Exit | Duration | Output | Reason given by the model |
|---|---|---|---|---|---|
| 1 | FAIL | 2 | 1 370 ms | 884 chars | "Baseline run before implementing subtract(a, b)" |
| 2 | PASS | 0 | 1 335 ms | 96 chars | "Verify subtract(a, b) is implemented and existing public behaviour is preserved" |

**Independent oracle (the harness's channel)**, never registered as a tool:
`python3 -m pytest -q && python3 -c "<hidden assertions>"`

| Item | Value |
|---|---|
| Static checks | PASS (`app/calc.py` present, `app/units.py` present) |
| Dynamic | **PASS**, exit 0, 1 315.9 ms, 105 chars |
| Spec digest | `sha256:5327e996…` |
| Verdict | `PASS` (execution succeeded, acceptance PASS) |

The hidden part of the oracle lives only in the Host-side command string: it is not a tool, and the
exported workspace contains exactly the seven fixture files, so it cannot be read from the sandbox.
Its discriminating power was proven before the run (wrong implementation → exit 1).

**Deliverable**: the patch touches one file.

```diff
--- a/app/calc.py
+++ b/app/calc.py
@@ def add(a, b):
     return a + b
+
+
+def subtract(a, b):
+    """Return the difference of two numbers."""
+    return a - b
```

`add`, `normalize`, `to_grams`, `total_grams` and `GRAMS_PER_KILOGRAM` are unchanged, and the
`app/units.py` dependency on `add` still resolves.

## H. Cost and latency

| Item | Value |
|---|---|
| Main turns / provider requests | 10 / 10 |
| Prompt tokens | 43 316 (5 428 miss + 37 888 cache hit) |
| Completion tokens | 1 382 |
| Total tokens (Main) | 44 698 |
| Cache-hit ratio (prompt) | 87.5 % |
| Budget used | 18.6 % of 240 000 |
| Wall clock, sandbox READY → terminal | 34.5 s |
| Wall clock, run start → terminal | ≈ 43 s |
| Monetary cost | **UNAVAILABLE** — no `pricing` block was configured, so nothing is estimated here. 44 698 tokens were consumed; convert them with the price list in force when you account for them. |

Regression after this stage (no runtime code was touched, only `experiments/` and `docs/`):
`pytest -q` passes on the same tree; `ruff check .`, `ruff format --check .` and `pyright` are clean.

One pre-existing flake surfaced while verifying: `tests/integration/test_context_runtime.py::
test_stable_renderer_ignores_completion_order` asserts that a worker with a 50 ms delay still
finishes second. It failed in 2 of 4 full runs on the *same* tree, passes in isolation, and passes in
the other 2 full runs, so the outcome tracks machine load, not this change. The module and the
renderer are untouched by stage P1; it is recorded here rather than "fixed" by loosening the margin,
which would change a test that guards the stable-rendering property.

## I. Failure analysis

Nothing failed. The run did not meet the `ACCEPTED_LIVE` bar because of what the model *chose*:

1. **No delegation.** Requirement 3 and 4 of `STAGE_P1_ACCEPTED_LIVE` ("at least one legal
   delegation", "two children concurrent") are unmet. The Main's own reasoning is that three short
   files do not benefit from splitting. This is a property of the task/model pair, not an error in
   the runtime: the tool was available, authorized and never refused.
2. Everything else in the loop is real and verified: DeepSeek responses, container isolation,
   sandboxed tools, ownership denial handled without terminating the run, a public FAIL observed and
   repaired, an independent oracle, clean teardown.

What this run therefore proves and does not prove:

* **Proves**: the coding pipeline runs end to end on a live model inside a real sandbox, with the
  safety, budget, evidence and verification planes active; a pre-execution denial is survivable
  (the P0.1 fix); the public/oracle split holds; context metrics are real.
* **Does not prove**: that a live model will use the delegation plane. That question needs a task
  whose parallel structure is genuine, and it must be answered without editing the prompt to force
  the call.

## J. Decision

**`STAGE_P1_CORE_PASS_NO_DELEGATION`**

Per §H of the stage brief: the Main completed the task directly, the oracle passed, but no
delegation was initiated, so this does not demonstrate the multi-agent closed loop under a live
model. No retry was attempted; the run stands as recorded.

Next step (requires a separate approval): either a task whose structure makes parallel investigation
worth its coordination cost, or an explicit decision to accept that a capable Main will often do the
work itself — which is itself an empirical finding about the value of the delegation plane.
