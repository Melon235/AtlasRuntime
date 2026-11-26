# Stage H1 — Read-only Historical Replay Workbench

Status: **`STAGE_H1_READ_ONLY_WORKBENCH_PASS`**

The workbench is a presentation layer over evidence that already exists on disk. It starts no model
request, writes nothing, and holds none of the runtime's authority. This document records what was
reused, what was added, the projection contract, the verified historical cases, and the boundaries
that remain.

Nothing here changes the evaluation record. `STAGE_F_UNIFIED_96_CELL_EVALUATION_STOPPED` and
`STAGE_F_FAILURE_OBSERVABILITY_CLOSURE_PASS` keep their original status.

## 1. Scope

In scope: a local, interactive, read-only replay of real historical execution evidence.

Out of scope and not attempted: any provider call, any new evaluation, any re-run of the 96-cell
matrix, any claim consumption, any change to a frozen prompt, router or context plane, any
multi-provider work, any new recovery mechanism, any merge of `master`, and H2 live execution.

## 2. Git provenance

| Item | Value |
|---|---|
| Repository | `https://github.com/Melon235/AtlasRuntime` |
| Stage branch | `feat/stage-h1-workbench` |
| Parent branch | `feat/stage-f-unified-evaluation` |
| Parent commit (approved baseline) | `7798b8fd096dda64263f02794cb9eca7158ba572` |
| Working tree at stage start | clean, HEAD equal to the approved baseline |

The baseline was confirmed with `git rev-parse HEAD` and `git status --porcelain` in
`D:\Work\AtlasOS`, not inferred from a directory name. `D:\Work\AtlasRuntime` holds an unrelated
older branch (`feat/closure-2-s07-preparation` at `b167672`) and was not touched. The Stage F branch
is retained unchanged and `master` was not merged.

## 3. Existing application audit (Phase A)

| Component | What it already did | Reused how |
|---|---|---|
| `src/atlasos/app/web.py` | FastAPI app, loopback-only, `/api/runs`, SSE stream, approvals, recovery, static index | One extra router, one extra page route, one `read_only` flag. The existing routes are untouched |
| `src/atlasos/app/service.py` | `AtlasApplicationService`, `RunRecord`, event JSONL, approvals/suspension projections | Left in charge of the live app runs; only `ReadOnlyMode` was added |
| `src/atlasos/app/cli.py` | Typer CLI with `run/runs/status/logs/workers/approvals/approve/reject/resume/result/export/web` | `atlas web` extended with `--replay-roots` and `--read-only`; every existing option keeps its default |
| `src/atlasos/app/static/index.html` | Single-file SPA: New Task, History, Approvals, Run detail | One navigation link added. The page, its routes and its API calls are unchanged |
| `src/atlasos/trajectory/` | `TrajectoryEvent` schema, correlation model, event types | The vocabulary and semantics of the recorded transcript drove the timeline/tool projection |
| `src/atlasos/metrics/` | Usage/request accounting models | Read for semantics only: no metric is recomputed from raw provider payloads |
| `src/atlasos/app/main_driven.py` | Writes `session.json` (`main`, `delegation`, `metrics`, `budget`, `ownership`, `public_tests`, `verification`) | The primary evidence source; its field names are the projection's field names |

The audit finding that shaped the design: the existing `RunRecord` answers "what did the app start
and what happened to it". It does not carry Main-driven delegation evidence — no gate decisions, no
child offsets, no handoff references, no per-request context series. Rather than rewrite the app
layer, the workbench adds a separate read-only projection beside it.

## 4. Added components

| Path | Responsibility |
|---|---|
| `src/atlasos/app/replay/sources.py` | Evidence roots, bounded traversal, safe path resolution, artifact classification, run identity, duplicate collapse |
| `src/atlasos/app/replay/projection.py` | The seven views: summary, agents, timeline, delegation, resources, verification, requests (+ tools, artifacts, overview) |
| `src/atlasos/app/replay/service.py` | Index, filters, pagination, per-view accessors, source diagnostics |
| `src/atlasos/app/replay/api.py` | The `/api/replay` GET-only namespace |
| `src/atlasos/app/static/replay.html` | The workbench UI (vanilla JS, no build step, no Node.js at runtime) |
| `tests/unit/test_replay_sources.py` | Discovery, identity, dedupe, read boundary, redaction |
| `tests/unit/test_replay_projection.py` | Main-driven, native, static DAG, UNKNOWN usage, raw vs effective, missing evidence |
| `tests/integration/test_replay_api.py` | Endpoints, filters, pagination, errors, read-only mode, existing-API compatibility |

No parallel web backend, no React/Node build, no MySQL/Redis, no second database. `pyproject.toml`
gained no dependency.

## 5. Data projection architecture

```
browser / CLI
   └── FastAPI  /api/replay/*   (GET only)
        └── ReplayService      index, filters, pagination, caching
             └── projection    RunSummary · AgentView · TimelineEvent · DelegationView
                               ResourceView · VerificationView · RequestView
                  └── EvidenceReader (bounded, root-contained, read-only)
                       └── configured evidence roots on disk
```

### 5.1 Recognised source kinds

| Kind | Recognised by | Example |
|---|---|---|
| `unified_cell` | `cells/<id>/analysis.json` or `evidence.json` **plus** a session, a native result or cell evidence | C1C, C2, Stage F A0/A1 |
| `cell_analysis` | the same analysis file **without** any execution evidence | Stage F preflight dispatch rehearsals |
| `main_session` | a standalone Main-driven `session.json` (`main`+`delegation`+`metrics`+`status`) | C2 budget-proof sessions |
| `native_run` | `result.json` with `worker_results`+`identity` | AnyCode-native A0 results |
| `app_run` | `run.json` with `run_id`+`mode`+`task` | the application layer's own runs |
| `atlas_run` | `result.json` with `run_id`+`status`+`tasks` | static DAG workflow results |

The matrix report is recognised as a source too, but it is never a run: each of its rows is attached
to the cell it describes. With three roots configured, 38 matrix reports and 893 rows were read, and
109 rows matched a materialised cell directory.

### 5.2 Run identity

A run key is `{root-label}.{kind}.{slug}.{sha256(run-dir)[:8]}` — stable for a given directory,
traceable to one place on disk, and never a bare `run_id`. Two experiments that reuse the same
`run_id` therefore produce two keys; the Stage F check
(`test_same_cell_id_in_another_plan_keeps_both_runs`) locks that in.

The same cell can appear in a rehearsal directory and in an execution directory. Those are copies,
not two runs. The copy with the richest evidence becomes the projected run, and every other location
is published under `/api/replay/sources` (`duplicates`) instead of being shown twice. In this
workspace 782 such copies were collapsed, which is why 245 runs are projected from the 257
directories that remained.

### 5.3 Absence is a value

`UNKNOWN`, `NONE`, `NOT_RUN`, `NOT_APPLICABLE`, `NOT_CONFIGURED`, `NOT_OBSERVED`, `UNAVAILABLE` and
`NO_EXECUTION_EVIDENCE` are distinct and are never collapsed into `0`, `false` or an empty string:

- A recorded `0` next to unknown components is reported as `UNKNOWN`, not as zero consumption.
- `known_floor_tokens` sums only the components that were reported; it is never presented as a
  system total.
- The system maximum agent context is taken from the recorded census. A multi-agent run with no
  census reports `UNKNOWN`; the Atlas-main series is never substituted for it.
- Effective agent completion is its own recorded value. It is not replaced by the delivered-success
  flag, and an analysis file with no execution evidence never yields a delivery headline.
- A run with no request journal reports `UNAVAILABLE`, with the reason. No error type, HTTP status
  or usage figure is invented for it.
- A timeline event without a timestamp is rendered in an "order only" section and is never placed
  on the time axis.

## 6. API contract

All routes are `GET`. There is no POST in the namespace.

| Route | Returns |
|---|---|
| `/api/replay/sources` | roots, scan statistics, recognised kinds, matrices, duplicates, discovery problems |
| `/api/replay/runs` | paged `RunSummary` list with facets; filters `q`, `stage`, `source`, `mode`, `status`, `provider`, `completeness`, plus `limit` (1–500) and `offset` |
| `/api/replay/runs/{key}` | overview: identity, goal, topology, counts, verification axes, completeness, sources, warnings |
| `/api/replay/runs/{key}/agents` | agent roster with role, profile, parent, ownership, stop reason, raw/effective success, usage |
| `/api/replay/runs/{key}/timeline` | observed events (real timestamps) then order-only evidence; `limit` (1–2000), `offset` |
| `/api/replay/runs/{key}/delegation` | router decision, gates, refusals, children, handoffs, measured overlap |
| `/api/replay/runs/{key}/context` | token attribution, request series, peak context, cache, budget, cost |
| `/api/replay/runs/{key}/tools` | tool operations: decision, reason codes, outcome, size; payloads withheld |
| `/api/replay/runs/{key}/artifacts` | patch-changed files, export references, handoffs |
| `/api/replay/runs/{key}/verification` | the seven axes as separate values, oracle detail, integrity checks |
| `/api/replay/runs/{key}/requests` | provider request journal, or `UNAVAILABLE` with the reason |

Errors: `404 ReplayRunNotFound`, `409 ReplayNotConfigured` (no evidence root), `400 ReplayError`,
`422` for out-of-range query parameters. The existing `/api/runs*`, `/api/approvals*` and
`/api/health` contracts are unchanged, including their status codes.

## 7. UI pages

`/replay` is an engineering console: a run explorer on the left, a run identity bar on top, and
detail tabs. Screenshots are committed under [`evidence/stage-h1/`](evidence/stage-h1/).

| Page | What it shows |
|---|---|
| Run Explorer | search, stage / source / mode / status / provider / evidence filters, live-vs-scripted and incomplete-evidence markers |
| Overview | goal, identity, execution topology, counts, duration with its derivation basis, tokens, cost, verification summary, completeness facets, sources read, warnings |
| Execution Timeline | parallel lanes from real timestamps, order-only section, full observed event table |
| Agents | role, profile, parent, stop reason, raw vs effective success, usage, objectives, ownership grants, planned-vs-executed topology |
| Delegation | state banner, router decision and gates, limits, child workers with offsets, measured overlap pairs, refusals, handoffs |
| Context & Resources | token KPIs, request series with source, peak context and census, cache and cost, budget record |
| Tools | tool, call id, agent, ALLOW/DENY, reason codes, outcome, result size, refusal note; inputs and outputs withheld |
| Artifacts | patch-changed files, export references and digests, handoff references |
| Verification | the seven axes kept separate, oracle detail, public-test calls, integrity and executor checks |
| Requests | journal rows, or an explicit UNAVAILABLE with the reason |

## 8. Historical replay cases verified

Verified against the real evidence in `D:\Work\AtlasRuntime-runs` and
`D:\Work\AtlasRuntime-archive`, with no synthetic fixture in the path.

| Case | Run | Observed |
|---|---|---|
| C1C | `runs.unified_cell.c1c.closure2-plugin-migration.c3.r1.dcc45e45` | 3 agents (Main + 2 coding workers), both workers on real overlapping windows (27,602 ms measured overlap), 2 handoffs, public suite PASS, independent oracle **FAIL**, execution integrity PASS, delivered **false**, 352,919 tokens, $0.086984 |
| C2 | `runs.unified_cell.c2.closure2-pipeline-migration.c3.b0.r1.dbf5cefe` | direct-only: delegation enabled, **no** admitted batch, `delegate_tasks` never called, no child worker shown, oracle PASS, delivered true |
| Stage F A0 | `runs.unified_cell.f.f-c1-config-lifecycle.a0.r1.e089b14e` | native attribution (1 NATIVE agent), 277,622 provider-reported tokens, requested `deepseek-chat` → returned `deepseek-flash`, oracle **FAIL**, delivered false, integrity PASS |
| Stage F A1 | `runs.unified_cell.f.f-c1-config-lifecycle.a1.r1.15019f6c` | provider usage **UNKNOWN**, known floor **UNKNOWN**, effective completion `UNAVAILABLE`, delivered false, integrity **FAIL**, request journal `UNAVAILABLE` with no invented error |
| Preflight rehearsal | `runs.cell_analysis.f.f-x2-distributed-diagnosis.a3.r3.95c09d57` | analysis only, headline `NO_EXECUTION_EVIDENCE`, 4/8 facets, no agent |

Case 5 of the brief (F2-A / F2-C scripted proof) could not be replayed as a full dynamic-delegation
run: the scripted-provider cells inside these roots are rehearsal records without an execution
directory, and the F2-C closure evidence lives in `experiments/stage_f/` as report material rather
than under a run root. Nothing was reconstructed to stand in for it. This is reported as a limit of
the available evidence, not as a workbench gap.

## 9. Tests

| Suite | Result |
|---|---|
| `tests/unit/test_replay_sources.py` (12) | discovery, matrix attachment, duplicate collapse, plan-scoped identity, rehearsal kind, malformed/unsupported/incomplete reporting, scratch pruning, traversal, symlink escape, oversize, payload-corpus exclusion, root specs and env list |
| `tests/unit/test_replay_projection.py` (11) | main-driven delegation, no-proposal `NOT_OBSERVED`, UNKNOWN usage, raw vs effective completion, native, static DAG, analysis-only, missing optional files, provider journal, redaction, artifacts |
| `tests/integration/test_replay_api.py` (7) | every endpoint, filters, pagination, 404/409/422, read-only refusal, existing-API compatibility, secret exclusion |
| replay total | **29 passed, 1 skipped** — the skip is the Windows symlink test, which needs a privilege this machine does not grant |
| `pytest tests/unit` | **1057 passed, 1 skipped** |
| `pytest tests/integration` | **994 passed, 193 failed** on the committed tree — see §13 for the three pre-existing causes; no failure names a replay module |
| `ruff check` / `ruff format --check` on every new file | clean |
| `pyright` on `src/atlasos/app/replay` and the three test files | 0 errors |

## 10. Browser verification

Driven with `@playwright/cli` against Microsoft Edge on `127.0.0.1:8791`, on the real evidence roots.

- The page loads and the explorer lists 245 runs.
- Clicking a run opens its detail and rewrites the URL (`?run=<key>`), so a run is linkable.
- All nine tabs switch and render real data.
- Console: **0 errors, 0 warnings** (the only earlier entry was a missing `/favicon.ico`, now fixed).
- A run with no execution evidence renders a full page with explicit unknowns; nothing white-screens.
- No layout overflow at 1500×950; the narrow dual-column panels were widened after the first pass.

Screenshots: [`evidence/stage-h1/`](evidence/stage-h1/) —
`01-run-explorer`, `02-c1c-overview`, `03-c1c-timeline`, `04-c1c-agents`, `05-c1c-delegation`,
`06-c1c-context-resources`, `07-c1c-tools`, `08-c1c-verification`,
`09-c2-delegation-not-observed`, `10-a0-native-context`, `11-a1-unknown-usage`,
`12-no-execution-evidence`.

## 11. Launch

PowerShell, from the repository root, in the project `.venv`:

```powershell
# 1. read-only historical replay over one or more evidence roots
.\.venv\Scripts\atlas.exe web --port 8791 --read-only `
  --replay-roots runs=D:\Work\AtlasRuntime-runs `
  --replay-roots archive=D:\Work\AtlasRuntime-archive `
  --replay-roots app=D:\Work\AtlasOS\experiments\runs

# 2. open the workbench
Start-Process http://127.0.0.1:8791/replay
```

Root aliases are optional (`alias=path`); a bare path uses the directory name. Instead of flags the
list can be set once:

```powershell
$env:ATLASOS_REPLAY_ROOTS = "D:\Work\AtlasRuntime-runs;D:\Work\AtlasRuntime-archive"
.\.venv\Scripts\atlas.exe web --port 8791 --read-only
```

`--read-only` refuses `POST /api/runs`, `POST /api/approvals/*/approve|reject` and
`POST /api/runs/*/resume` with `403 ReadOnlyMode`. Without the flag `atlas web` behaves exactly as
before and still serves the live console at `/`. The listener binds `127.0.0.1` by default. No Node
build, no extra service, no database.

Headless check of the same server:

```powershell
Invoke-RestMethod http://127.0.0.1:8791/api/replay/sources | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:8791/api/replay/runs?limit=5 | ConvertTo-Json -Depth 4
```

## 12. Security boundaries

- **Read-only.** The replay layer has no write path: no `open(..., "w")`, no store, no lease, no
  claim. Discovery never creates a directory that does not exist.
- **Root containment.** Every path is relative, `..` and absolute/drive-qualified paths are refused,
  and the resolved path must stay under its root — so a symlink or Windows junction that points
  outside is rejected (`PathOutsideRoot`), not followed.
- **Bounded reads.** 8 MiB per JSON file, 16 MiB / 20 000 lines per JSONL, 256 KiB for text heads.
  Traversal is capped per root and reports truncation rather than silently stopping.
- **Payload corpora stay private.** `private/`, `workspace/`, `checkpoints/`, `fixtures/` and
  `oracle-sandbox/` directories are never walked. Tool inputs and outputs are never returned; only
  a decision, reason codes, an outcome and a size. Denials show the refusal reason, not the command.
- **No secrets.** Every string that can reach the browser passes through a redactor for
  `sk-…`, `Bearer …` and `key|token|secret|password|authorization = …` forms. A test seeds a live
  key into a goal and a provider error message and asserts it never appears in any view.
- **No browser authority.** The page has no approve, reject, resume, retry or submit control, and no
  polling loop that could act. It cannot start, approve or recover anything.
- **Hidden oracle and reference material are not exposed.** The oracle page shows the recorded
  verdict, kind, mode, return code and a bounded diagnostic excerpt; the hidden tests and the
  reference implementation are never read, let alone served.

## 13. Known limitations

1. **No provider request journal exists for any historical run in these roots.** The journal was
   added at `7798b8f`; every run here predates it. The Requests page therefore reports
   `UNAVAILABLE` for all of them. The UI, the projection and a fixture test are ready for a journal;
   no real run can exercise it yet.
2. **`experiments/runs/<app>/coding/<uat>/session.json` uses the coding-composition schema**, not the
   Main-driven schema, so those directories are reported as `INCOMPLETE_EVIDENCE` (52 of them). The
   app layer still projects those runs through the existing `/api/runs` History page. Adding a
   seventh projection kind would be H2 work, not a bug fix.
3. **193 integration failures are pre-existing on this checkout, not caused by H1.** Their causes,
   from the run on the committed tree:
   - the dominant one is `execution_freeze_supersedes_history`: the tracked Stage B freeze files
     record absolute paths under `D:\Work\AtlasRuntime`, while this workspace is `D:\Work\AtlasOS`,
     so the Stage B preflight blocks before any test body runs (the same line also reports
     `runtime_source_unchanged`, which is why the digest looks suspicious);
   - the sandbox-dependent tests block as `PROVISIONING_SMOKE_BLOCKED` / `'BLOCKED' == 'READY'`
     because no OpenSandbox server is running on this machine;
   - 69 failures are the IDE safe-delete guard raising inside `shutil.rmtree` in the fake sandbox
     helper, a known local artefact that disappears when the affected file is re-run alone.

   `grep` over the failure log finds **zero** mentions of `app/replay`. The full unit suite
   (`pytest tests/unit`) is green: 1057 passed, 1 skipped.
4. **Adding app-layer files changes any digest taken over `src/atlasos`.** The Stage B/freeze guards
   that compare such a digest must be re-frozen by the owner before they can pass; H1 did not
   re-freeze anything.
5. **Sort order is "newest recorded start first".** Stage F preflight cells record a synthetic
   `2026-11-26T00:00:00Z` claim, so rehearsals can sort above real runs of the previous day. The
   evidence filter and the `cell_analysis` kind make the distinction visible; the timestamps
   themselves are not rewritten.
6. **No live execution, no H2.** The workbench cannot start a run, and this stage did not attempt to.
7. **Screenshots are from one 1500×950 viewport** on this machine; narrower layouts are handled by
   CSS media queries but were not visually captured.

## 14. Next-stage candidates (H2, not started)

- Live execution console over the same projection contract, reusing `submit` and the SSE stream
  instead of adding a second job model.
- A provider request journal projection exercised against a real post-`7798b8f` run.
- A coding-composition session projection, if those runs are to be replayed beside the Main-driven
  ones.
- Side-by-side comparison of two runs from the same cell (A0 vs A1, C3 vs C4) on the resource and
  verification views.
- An evidence-root manifest with per-file digests, so a replay can prove it read the frozen bytes.

## 15. Boundaries observed

No provider request was made; no claim was consumed or deleted; no historical file was modified; no
frozen prompt, router or context plane was touched; the 96-cell matrix was not re-run; batch 2/3 was
not opened; `master` was not merged; and no existing uncommitted work was overwritten.
