# Stage H2 — Live Execution Workbench (offline verification)

Status: **`STAGE_H2_LIVE_WORKBENCH_OFFLINE_PASS`**

The product entry point now runs the **real Main-driven runtime**: the same `run_goal` the research
stages used, with the same router, ownership policy, ledger, sandbox binding, handoff store and
budget. What H2 adds is the way in: an application-service mode, a workspace the Host can hand over
safely, a lifecycle the reader can trust, real events on the existing SSE stream, and a UI that
never turns a scripted run into a live one.

"Offline" here means **no external model request was made**. It does not mean a mock UI: the runs in
§12 executed the agent loop, the tool registry, the delegation plane and (twice) a real OpenSandbox
container. The only substitute is the provider, and every record names it.

## A. Git provenance

| Item | Value |
|---|---|
| Repository | `https://github.com/Melon235/AtlasRuntime` |
| Branch | `feat/stage-h2-live-workbench` |
| Baseline | `feat/stage-h1-workbench` at `b86fc60881095e8371d0a00df48f1e2f9443a2e0` |
| Working tree at start | clean, HEAD equal to the baseline, `origin` equal to the baseline |
| `master` | not merged, not touched |
| Stage F / H1 branches | retained unchanged |

## B. The two H1 projection defects

### B1. Logical cell identity is not execution identity

H1 collapsed every directory sharing `(cell_id, plan_id)`. Two different executions of one planned
cell — a scripted proof and a live model run, or eight smoke attempts of one arm — were therefore
shown as one run with the richest copy winning. The rule is now:

```text
run identity   = (cell_id, plan_id, provider band, execution token)
execution token = execution_id  |  evidence provenance source_digest
                | content digest | claimed verdict digest (rehearsals only)
```

The token is taken from what the artefact itself recorded: a session's `run_id`, a native result's
`identity.run_id`, an application record's `run_id`, or the `source_digest` the Stage F adapter wrote
into `evidence.provenance`. A rehearsal (analysis only, no execution evidence) falls back to the
verdict it claims, so two copies of the *same* rehearsal still collapse while a rehearsal claiming a
different verdict stays its own row. Nothing is merged on `(cell_id, plan_id)` alone any more.

Consequences on the real evidence, measured:

| | H1 (before) | H2 (after) |
|---|---|---|
| runs projected from 3 roots | 245 | 251 |
| copies collapsed | 782 | 776 |
| directories that are separate executions | hidden | 6 more rows, all distinct executions |

The six newly visible rows are the scripted/live pairs the brief named: for example
`f.f-p1-connector-migration.A1.r1` now shows the **scripted** copies (`provider: scripted`) and the
**live** copy (`provider: deepseek`) as separate runs, with `provider_kind` SCRIPTED and LIVE_MODEL
respectively.

Regression tests: `test_scripted_and_live_records_of_the_same_cell_stay_separate`,
`test_two_executions_of_one_cell_are_not_collapsed`,
`test_rehearsal_copies_collapse_only_when_they_claim_the_same_verdict`,
`test_duplicate_cell_identity_is_one_run_and_copies_are_reported`.

### B2. Raw success and effective completion

The axes were read through one precedence chain that could take `agent_success` for the raw loop
outcome and leave effective completion `UNAVAILABLE` even when the session recorded it. Now:

| Axis | Field order |
|---|---|
| raw agent success | `evidence.correctness.raw_agent_success` → `correctness.agent_success` → `analysis.outcome.raw_agent_success` → `outcome.agent_success` → `session.main.success` → native → app status |
| effective agent completion | `evidence.correctness.primary_agent_completion` → `session.execution_succeeded` → `analysis.outcome.execution_succeeded` → `UNAVAILABLE` |
| delivered success | `correctness.delivered_success` → `outcome.delivered_success` → `outcome.goal_correctness` → native success |

The projection also returns `axes.axis_sources`, naming the field each value came from, and the UI
prints it in the note column, so a reader never has to guess. `stop_reason` is never used as a
boolean, `delivered_success` is never substituted for completion, and an unknown usage figure is
still `UNKNOWN`.

Stage F A1 now reads: raw `true` (from `correctness.raw_agent_success`), effective `false` (from
`session.execution_succeeded`), delivered `false`, integrity `FAIL`.

Regression tests: `test_raw_success_uses_the_split_field_and_effective_uses_the_session`,
`test_effective_completion_is_never_the_stop_reason`,
`test_raw_success_and_effective_completion_stay_separate`.

## C. Main-driven application gateway

`AtlasApplicationService.submit(..., mode=...)` accepts four modes:

| Mode | Path | Delegation tool |
|---|---|---|
| `workflow` | `AtlasRuntime.run` (static two-agent DAG) | none |
| `coding` | `run_coding_session` (UAT composition) | none |
| `main_direct` | `atlasos.app.main_driven.run_goal` | **not built** |
| `main_dynamic` | `atlasos.app.main_driven.run_goal` | built, router decides admission |

The two Main-driven modes are one call to `run_goal` on a worker thread with
`asyncio.run`. Nothing is re-implemented: the Main is the same `AtlasContextEngine` task, children
are the same `DelegationService` + `ScopedWorkerFactory`, tool calls pass the same `ActionSafetyGate`,
ledger and R0.1.0-compatible router, and the workspace is the same `GoalSandbox`.

`main_direct` passes `DelegationConfig(enabled=False)`: the runtime builds no `delegate_tasks` tool at
all, so a "direct" run cannot delegate even by accident. `main_dynamic` passes `enabled=True` with
two children per wave and the shipped limits.

The product path imports nothing from `experiments/`. There is no benchmark task, oracle, claim or
authority in it: those remain properties of the evaluation harness.

### C1. The admission conflict, stated

`run_live_preflight` (Stage P1) was written for evaluation runs: it requires an independent oracle
(`acceptance_configured`), a public suite separate from that oracle, and a declared acceptance digest.
An ordinary task has none of these, so a product run would have been blocked for lacking an
evaluation contract it does not have. The check set is unchanged; a `preflight_profile` now decides
which checks block:

| Profile | Blocks on |
|---|---|
| `benchmark` (default, unchanged) | every check, including the evaluation contract |
| `product` | isolation, real provider, clean source, write scope, budget, delegation bounds, provider/model identity, API key, scratch run directory |

The evaluation-contract checks are still evaluated and still reported — they are marked
`blocking: false` with the detail prefix `product profile: not required for an ordinary task`. No
safety check was dropped, weakened or skipped, and a dirty source tree still blocks a product run
(regression test: `test_a_dirty_source_repository_blocks_even_in_product_mode`).

## D. Execution configuration contract

Every Main-driven run records exactly what will execute, in `run.json` under `config`:

```json
{
  "goal": "...", "mode": "main_dynamic",
  "workspace": {"name": "main_driven_demo", "seed_digest": "sha256:...", "write_scope": [...]},
  "provider": "deterministic", "provider_kind": "DETERMINISTIC",
  "model": "deterministic-scripted",
  "budget_tokens": 120000, "max_turns": 16,
  "delegation": {"enabled": true, "max_parallel_children": 2, "children_per_call": 2, "child_max_turns": 12},
  "sandbox": {"enabled": true, "provider": "opensandbox", "image": "atlas-uat-python:3.12"},
  "runtime_run_id": "goal-app-5741b48baf11"
}
```

Limits: budget 1 … 2 000 000 tokens (default 500 000, never unbounded), Main turns 1 … 40
(default 16), child turns 12. The API cannot inject a provider factory, a prompt path, a shell
command or a workspace path: `provider` is a server-level decision (`--provider deepseek`, the only
live provider, or `--provider deterministic`), and `workspace` is a **name**, not a path.

## E. Workspace and budget safety

`src/atlasos/app/workspace.py` is the only module that turns a name into a repository:

- the name must be one plain segment (no `/`, `\`, `..`, drive letter, hidden directory);
- the resolved directory must live under an explicit root (`ATLASOS_WORKSPACE_ROOTS`, default
  `experiments/fixtures`); symlinks and junctions are resolved *before* the containment check;
- the AtlasRuntime checkout is refused as a task workspace even if its parent is a configured root;
- the tree is copied (`__pycache__`, `.venv`, `node_modules`, `.git`, `.env`, `*.pyc` excluded),
  capped at 20 000 files / 64 MiB, then committed as **one commit with a fixed identity and date**,
  so the same tree always yields the same seed commit;
- a dirty result is refused before the sandbox sees it;
- the Main's write scope is derived from the seed itself (every directory as `dir/`, every top-level
  file exactly) and passed to the existing ownership policy. The policy still enforces it per call:
  a write outside the scope is denied, and the denial appears in the run's ownership evidence.

Budget: `token_budget` is passed to `RunGoalOptions` and becomes the shared `RunBudgetState` the Main
and every child draw from. The run's `budget` block records limit, used, remaining and the peak.

## F. Run lifecycle

| Status | Meaning |
|---|---|
| `SUBMITTED` | accepted, workspace not materialised yet |
| `RUNNING` | the worker thread is executing the goal |
| `COMPLETED` / `FAILED` | the runtime wrote a terminal session; the app projects its verdict |
| `INTERRUPTED` | the owning process is gone and no terminal evidence exists |
| `CANCELLED` | never set: cancellation is not implemented (see below) |

The rule that matters: a record read from disk in a new process carries `orphaned = True`, and a
`SUBMITTED`/`RUNNING` record with no live thread is set to `INTERRUPTED` with an explanatory error
and a `run_interrupted` event. It is not re-run, not resumed, and not retried. A run is never shown
as `RUNNING` because the process that owned it is gone.

Cancellation: `POST /api/runs/{id}/cancel` and `atlas cancel` return **409 `CANCEL_UNAVAILABLE`**
with the reason ("the execution happens on a worker thread that owns the sandbox and the provider
stream; this version will not display a cancelled state while the work continues"). There is no
UI-only cancel. The page shows the same sentence and disables nothing else.

Concurrency: one Main-driven run at a time. A second submission while the slot is held is refused
with **409** and the id of the run that holds it (`active_run` is also in `/api/workspaces`).

## G. Real-time events

Two sources, both real:

1. **Application events** written to `events.jsonl` with a monotonic integer id:
   `run_submitted` (mode, workspace, budget, provider kind), `run_started`, `workspace_ready` (seed
   commit and digest), `run_completed` / `run_failed` (status, failure class, budget),
   `run_interrupted`.
2. **Runtime events** tailed from the run's own trajectory transcript
   (`goal-*/trajectory/*/transcript.jsonl`, appended and flushed by the recorder). They are emitted as
   `runtime_event` with the *original* `event_type`, a summary, the trusted `agent_id` and
   `task_id`, and the recorder's `seq`. Observed in a real run: `action_proposed`,
   `action_decision` (ALLOW/DENY with reason codes), `tool_observation`, `execution_outcome`,
   `sandbox_lifecycle`, `worker_lifecycle`, `run_terminal`,`tool_disclosed`.

Consequences of using the existing seams: an event appears only when the runtime writes it, the
agent that produced it is named by the runtime's own correlation, and a field the runtime never
records simply never appears. No timeline is pre-generated, and model reasoning is not reconstructed
as an event.

SSE reuses `GET /api/runs/{run_id}/stream`: stable ids (`id:` field), ordered delivery, reconnect via
`Last-Event-ID`, a terminal event, and no re-execution on reconnect. A client that disconnects and
returns gets only the events it missed.

## H. Live workbench UI

Same static console (`/`), extended; no Node build, no second front end.

| Screen | Content |
|---|---|
| New Run | mode (four), task, workspace selector from the server, Main turns, token budget, a configuration panel naming provider/model/sandbox/budget/concurrency/cancel, and a scripted-provider banner when the server is in that mode |
| Live detail | lifecycle status, Main-driven panel (router route, children executed, budget, started/finished, per-child table with worker id, status, tokens and handoff ref), live events, metrics, task, final result |
| Terminal | the same page ends at `COMPLETED`/`FAILED`; **Open in Replay** resolves the run's replay key and navigates to the identical run |
| Replay | H1's explorer, now showing the finished live run once, with `mode main_dynamic`, `provider SCRIPTED` and headline `COMPLETED_UNVERIFIED` |

The page keeps no authority: no approve/reject/resume control for Main-driven runs, no client-side
scheduling, and the only buttons that can change anything are Submit (through the service) and
Cancel (which the backend refuses).

### Verification semantics for a product run

| Axis | Value for a product run |
|---|---|
| raw agent success | from `session.main.success` |
| effective agent completion | from `session.execution_succeeded` |
| public test | `NOT_CONFIGURED` (no host-fixed suite) |
| static acceptance | `NOT_CONFIGURED` |
| independent oracle | `NOT_CONFIGURED` |
| execution integrity | `RECORDED` (the corpus is evidence capture, not a verdict) |
| delivered success | `UNKNOWN` |
| headline | `COMPLETED_UNVERIFIED` |

`NOT_CONFIGURED` is never rendered as `PASS`, and the missing oracle never becomes a fabricated
success or a fabricated failure.

## I. CLI integration

```powershell
# live Main-driven run through the same service the Web UI uses
.\.venv\Scripts\atlas.exe run "Write the deliverable for the demo goal." `
  --mode main_dynamic --workspace main_driven_demo --budget 200000 --max-turns 12

# deterministic (no network) run of the same path
.\.venv\Scripts\atlas.exe run "Write the deliverable for the demo goal." `
  --mode main_direct --workspace main_driven_demo --provider deterministic --no-sandbox

.\.venv\Scripts\atlas.exe workspaces          # allowed roots and names
.\.venv\Scripts\atlas.exe status <run_id>
.\.venv\Scripts\atlas.exe logs <run_id> --follow
.\.venv\Scripts\atlas.exe result <run_id>
.\.venv\Scripts\atlas.exe export <run_id> --output out.json
.\.venv\Scripts\atlas.exe cancel <run_id>     # reports CANCEL_UNAVAILABLE
```

Every command constructs the same `AtlasApplicationService`; there is no second runtime. Existing
commands and their defaults are unchanged, and `--mode workflow|coding` still behaves exactly as
before.

## J. Deterministic end-to-end verification

Provider: `atlasos.app.deterministic.DeterministicAdapter` — a scripted adapter that answers from a
per-agent step list, estimates usage as `chars/4`, reports a structural-zero cache split, and records
every request. It is reachable only when the server was started with `--provider deterministic`, so
a request body cannot select it.

### Case 1 — Direct

Submitted through `POST /api/runs` (`mode=main_direct`, host mode). Result: `COMPLETED`;
`solution/answer.txt` written; `file_write` was the Main's only tool call; **no delegation plane was
constructed** (`live.delegation == null`); `run_started`, `workspace_ready`, `action_proposed`,
`action_decision`, `tool_observation`, `run_completed` all present; the replay key resolves and shows
`main_direct` / `COMPLETED_UNVERIFIED`.

### Case 2 — Dynamic

Same entry point (`mode=main_dynamic`). Result: `COMPLETED`; `delegate_tasks` proposed and admitted
(`route=delegate_parallel`); **two children executed** (`alpha`, `beta`) with distinct worker ids
(`goal-<run>-main:r1:alpha|beta`), distinct handoffs, and distinct files (`solution/alpha.txt`,
`solution/beta.txt`); the Main then wrote `solution/integration.txt` (integration) and answered.
Replay: `DELEGATION_OBSERVED`, two children, real per-agent timeline lanes.

Run twice in a real OpenSandbox (`isolation: sandbox`, provider `opensandbox`, `require_real: true`):
the export contained all three files, the patch listed them, the sandbox was created and destroyed,
and the Main's `file_write` returned `Completed` (no tool error).

### Case 3 — Provider failure

The same service with `failure_after_request=2` (a documented seam that only the deterministic
provider consults): request 2 onwards raises. Result: `FAILED`; the request journal records the
failure with `exception_type` and the run ends on a failed request; `execution_succeeded` is
`false`; the failure is classified (not a bare string); the replay axes show
`effective_agent_completion=false`; the application does not resubmit the task and there is no
automatic retry.

This case also fixed a real defect in the goal path: a provider failure *after* a successful turn
used to be reported as `COMPLETED`, because the loop swallowed the error and kept its `success` flag.
`run_goal` now treats a journaled failure whose last request is not `COMPLETED` as a failed
execution. A failure followed by a completed request is a recovered hiccup and does not relabel the
run.

### Case 4 — Safety

| Check | Result |
|---|---|
| unknown workspace name | `422 WorkspaceRejected` |
| path-like name (`../x`, `C:\`, `a/b`) | `422` |
| repo checkout as workspace | `422` |
| budget 0 / negative / above the ceiling | `400` |
| turns above the ceiling | `400` |
| second Main-driven run while one is active | `409` with the active run id |
| read-only server | `403` on submit, cancel, approve, reject, resume |
| cancel | `409 CANCEL_UNAVAILABLE` |
| live run of an interrupted process | `INTERRUPTED`, never `RUNNING` |
| secret in the environment | absent from every projection (JSON-asserted) |
| the same run in Replay | exactly one row (`total == 1`) |

### Case 5 — UI

Real FastAPI server on `127.0.0.1:8792`, Microsoft Edge via `@playwright/cli`. Submitted from the
form, watched the run, opened Replay from the run page. Screenshots in
[`evidence/stage-h2/`](evidence/stage-h2/): `01-new-run`, `02-live-running`, `03-live-done`,
`04-replay-same-run`, `05-replay-explorer`, `06-live-events`. Browser console: **0 errors,
0 warnings**. The scripted-provider banner is visible on every screen of a scripted run.

## K. Product / experiment boundary

`STAGE_F_UNIFIED_96_CELL_EVALUATION_STOPPED` and `STAGE_F_FAILURE_OBSERVABILITY_CLOSURE_PASS` are
untouched. No manifest, claim or historical evidence was modified. The seven historical claims, the
consumed Stage F B1 claim and the unspent B2/B3 claims are unchanged. No product run is part of the
96-cell matrix, and no benchmark condition gates the product entry point.

## L. Deliverables index

| Deliverable | Where |
|---|---|
| A Git provenance | §A |
| B projection fixes | §B + `tests/unit/test_replay_*` |
| C application gateway | §C + `src/atlasos/app/service.py` |
| D configuration contract | §D |
| E workspace/budget safety | §E + `src/atlasos/app/workspace.py` |
| F lifecycle | §F |
| G SSE sources | §G |
| H live UI | §H + `src/atlasos/app/static/index.html` |
| I CLI | §I + `src/atlasos/app/cli.py` |
| J direct / dynamic / failure E2E | §J |
| M replay compatibility | §J + `tests/integration/test_h2_main_driven_api.py` |
| N screenshots | `docs/evidence/stage-h2/` |
| O launch commands | §M below |
| P limitations | §N |
| Q H3 candidates | §O |
| R external model requests | **0** |
| S sandbox create/destroy | **2 / 2**, none retained |
| T production claims consumed | **0** |

## M. Launch

```powershell
# scripted demo server: no model call, no container
.\.venv\Scripts\atlas.exe web --port 8792 --provider deterministic --no-sandbox
Start-Process http://127.0.0.1:8792/

# live server (needs DEEPSEEK_API_KEY and a running OpenSandbox)
.\.venv\Scripts\atlas.exe web --port 8792
```

`ATLASOS_DETERMINISTIC_DELAY=1.2` paces the scripted provider so a live timeline is watchable; it
changes nothing about the runtime and is recorded in the run configuration when used.

## N. Known limitations

1. **No live model run was executed in this stage.** The DeepSeek path is wired, preflighted and
   unit-tested, but the only provider that answered here was the deterministic one. A real run is
   the first thing H3 should do.
2. **Cancellation is unavailable.** No safe stop exists for the current thread model, so the API
   refuses instead of pretending.
3. **One Main-driven run at a time.** The slot is process-wide; a queue is not implemented.
4. **The write scope derives from the seed.** A Main that wants a brand-new top-level file is refused
   by the ownership policy; a task that must create one needs a fixture with a directory for it.
5. **Content-addressed handoffs collapse identical results.** Two children whose reports are
   byte-identical share one `ho-…` ref (the store keys by digest, and the index keeps the last
   writer). The workbench shows what the store recorded; it does not rewrite the store. The demo
   script avoids the collision by naming each file.
6. **A sandbox `file_write` logs a swallowed 404 traceback** from a post-write read-back; the write
   itself lands (verified in the export and the patch). It is printed, not returned, and does not
   affect the tool result.
7. **The Replay `Requests` page is still empty for historical runs** (no journal before `7798b8f`).
   Live runs do write `provider_requests.json`, but the replay request view reads the *cell evidence*
   journal, not the goal session's; wiring that view to `goal-*/provider_requests.json` is H3 work.
8. **`experiments/runs/<app>/coding/<uat>/session.json`** (the coding composition) is still reported
   as `INCOMPLETE_EVIDENCE` in the replay index, unchanged from H1.
9. **The UI is verified at one viewport** (1500×950) on one browser (Edge); the mode select truncates
   its long option labels at that width.

## O. H3 candidates

- The first real DeepSeek Main-driven run (direct and dynamic) with the same acceptance and budget
  discipline as the research arms.
- Replay support for the goal session's request journal, so `Requests` is populated for product runs.
- A real cancellation path (cooperative stop through the engine and the sandbox binding).
- A run queue with a bounded number of active Main-driven runs.
- Workspace templates with an explicit "may create new top-level files" grant.
- PR / diff export for a finished run, linking the patch to the replay artifacts page.

## P. Boundaries observed

No external model request; no new evaluation; no 96-cell re-run; no claim consumed or deleted; no
historical evidence modified; no frozen prompt, router or context plane touched; no planner, no
distributed task system, no `master` merge, no automatic retry or resume.
