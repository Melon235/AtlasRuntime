# Stage B0 — Controlled Real Delegation Validation (live)

Branch `exp/stage-b0-controlled-live`, based on `fix/reliability-closure-b0` at `6a89dd1`.
One live DeepSeek run, executed once. No runtime source was modified in this stage.

**Decision: `B0_DELEGATION_MECHANISM_PASS_INTEGRATION_FAILED`.**

The delegation plane was exercised end to end by a real model: the Main called `delegate_tasks`
itself on its third turn, the Router admitted both children in one `delegate_parallel` batch, the two
research workers ran **concurrently** in the same sandbox, produced bounded handoffs with refs, and
the Main resumed its own loop and wrote the integration. It then ran out of Run Budget on its
fourteenth turn — one `run_public_tests` call short of finishing — and stopped with
`budget_exceeded`. The exported work passes 34 of 35 public tests; the independent oracle failed.

Every artefact is labelled `CONTROLLED_DELEGATION`. This run is **not** evidence that an
unconstrained agent chooses to delegate.

---

## A. Git provenance

| Item | Value |
|---|---|
| Baseline verified | `fix/reliability-closure-b0` @ `6a89dd1`, worktree clean, local = remote (`0 0`, re-fetched through the proxy) |
| Branch | `exp/stage-b0-controlled-live` |
| Harness fixes | `991c806` (source identity, attempt protection), `5203150` (freeze records no self-dirt) |
| Freeze commit | `fecf02a` — `source sha256:275e7f0a…` over 130 files, goal `sha256:5995bcaa…`, fixture 23 byte digests equal to stage P1b, `tool_error_mode=corrected` |
| Run id | `stage-b0-20261119T080450Z` |
| Fixture commit (in sandbox) | `9eb8c57e904ec093048bb65de22593d47a996863` (same 23 files, same digests as P1b) |
| Attempt marker | `<scratch>/attempts/attempt-1.json`, claimed `08:04:50Z`, copied to the evidence bundle |
| Evidence | `experiments/stage_b0_controlled/evidence/run-1/` |
| Post-run commit | fixes the reporting bug found after the run and adds the evidence — see §L; the freeze was deliberately **not** regenerated |

Untouched: `src/atlasos` (source digest identical before and after), AnyCode, Router m.1.0,
WorkerFactory, HandoffStore, the context plane, `MAIN_SYSTEM_PROMPT`, the fixture, the public suite,
the oracle, and every earlier stage artefact.

## B. B0 harness fixes (before the run)

**B1 — freeze identity.** The old freeze recorded a Git commit that could never equal the branch head
(the freeze file is itself a commit), so "is the runtime the frozen one?" had no answer. It now
records three separate things:

* `source` — a byte digest over every file under `src/atlasos` plus the per-file map.
  **Authoritative and blocking.**
* `harness` — digests of `b0_contract.py` and `run_stage_b0.py`. Blocking.
* `git` — head, branch, worktree status, and **ancestry** of the freeze commit (`merge-base
  --is-ancestor`), which is enforced. Equality is not used, and the mismatch is not ignored: a
  rewritten history fails the check.

`tool_error_mode` is frozen as `corrected` and re-checked against the runtime option.

**B2 — one-shot protection.** `--freeze` used to `rmtree` the scratch directory, attempt marker
included. Now:

* the marker is claimed with `O_CREAT | O_EXCL` **after** a READY admission and **before** `run_goal`
  — a BLOCKED preflight cannot consume the attempt;
* a marker, *or* any collected `evidence/run-*` directory, makes `--freeze` and `--run` refuse;
* `--freeze` no longer deletes anything; evidence directories are created fresh and never reused;
* collection runs before reporting, so a reporting bug cannot lose a finished run (this is exactly
  what §L records);
* no retry path exists.

12 offline tests (`tests/integration/test_stage_b0_harness.py`) cover these properties; they call no
model and write no repository evidence.

## C. Freeze and preflight

`--preflight` → **READY**: 19 runtime checks + 14 stage checks, 0 blocking. Recorded in
`evidence/run-1/preflight.json`.

| Stage check | Result |
|---|---|
| `runtime_source_frozen` | OK — `sha256:275e7f0a…`, 130 files |
| `harness_frozen` | OK |
| `freeze_commit_is_ancestor` | OK — `5203150` |
| `worktree_clean` | OK |
| `tool_error_mode_frozen` | OK — `corrected`, runtime option `corrected` |
| `goal_frozen` / `instruction_frozen` | OK — `sha256:5995bcaa…` / separate instruction digest |
| `delegation_mode_labelled` / `instruction_is_host_authored` | OK — `CONTROLLED_DELEGATION` |
| `main_prompt_unchanged` | OK — `sha256:7e6fd829…` (stage P0.1) |
| `goal_passed_to_runtime` / `fixture_matches_p1b` / `oracle_not_in_fixture` / `single_live_attempt` | OK |

OpenSandbox health was probed separately (`200`), and no container was running before the attempt.

## D. Real execution trace

08:05:00.438Z → 08:06:17.964Z, **77.5 s**. 165 trajectory events: 51 `action_proposed` / 51
`action_decision` / 51 `tool_observation` (51 tool calls across the Main and both children),
9 `execution_outcome` (side-effecting actions only), 1 `run_terminal`.

```text
08:04:50Z  attempt marker claimed (O_EXCL), before any provider call
08:05:00   sandbox ready (real OpenSandbox, network probe PASS, host separation clean)
turn  1-2  bash find/ls · list_files                      ALLOW  ok
turn  3    delegate_tasks  ────────────────┐              ALLOW  ok
                                           │ r1: DELEGATION_ADMITTED, delegate_parallel
                        ┌──────────────────┴──────────────────┐
             file-plugin-research                     message-plugin-research
             6 turns / 14 tool calls                   5 turns / 12 tool calls
             2.6 ms → 33 229 ms                        16.8 ms → 31 252 ms
                        └──────────────────┬──────────────────┘
                                           │ 2 handoff refs, 900-char previews
turn  4-25 22 tool calls: 5 bash (2 refused, OWNERSHIP_UNCLASSIFIABLE_SHELL) · 12 file_read · 5 file_edit
turn 14    request 25 rejected: needs ~28 294 tokens, 23 099 of 300 000 left
08:06:17   run_terminal: status=failed, stop_reason=budget_exceeded
08:0x      oracle: static PASS, dynamic FAIL; sandbox destroyed
```

The Main's own words (from `tool_conversations.json`):

> Per the run instruction, I'll delegate the two read-only research tasks before editing anything.
> Delegation succeeded. Now let me read the actual source myself (I must verify), starting with core and tests.
> The shell refused those (loop + interpreter shape). I'll read files directly instead.
> Both research reports agree with my own reading of the sources.
> No conflicts with the plugin suites. Now I'll implement the adapter (both normalizers) and the registry dispatch.

## E. Main delegation proposal

`delegate_tasks` was called **by the Main**, on its third turn, with its own two tasks
(`tool_conversations.json`, call 3):

* `reason`: "Two independent read-only research tasks, one per plugin package, to inform the core
  integration."
* tasks: `file-plugin-research` and `message-plugin-research`, both `research_worker`, both read-only,
  each with its own objective and background written by the Main — no shared write target and no
  dependency between them.

The Host instruction asked for exactly this call; the *content* of the two tasks, the decision to
verify the reports independently, and the integration are the model's own. Labelled
`CONTROLLED_DELEGATION`.

## F. Router admission

| Item | Value |
|---|---|
| Rounds | 1 |
| Route | `delegate_parallel` |
| Reason codes | `DELEGATION_ADMITTED` |
| Blocked gates | none |
| Children admitted | 2 of 2 proposed |
| Policy | m.1.0, digest `c6c29faf…` (unchanged) |

The Router was not bypassed: the Main's tool call went through `DelegationService` →
`ScopedWorkerFactory`, and the workers carry the run's `research_worker` profile.

## G. Concurrent worker evidence

| Child | Profile | Turns | Tool calls | Interval (ms, run-relative) | Status |
|---|---|---|---|---|---|
| `file-plugin-research` | `research_worker` | 6 | 14 | 2.596 → 33 229.207 | completed, success |
| `message-plugin-research` | `research_worker` | 5 | 12 | 16.758 → 31 251.953 | completed, success |

`max_observed_concurrency = 2`; the intervals overlap for ≈ **31.2 s** of the 77.5 s run, so the
children genuinely ran at the same time, not one after the other. Both were read-only: the two
ownership denials in the run belong to the Main, and no child requested a write.

## H. Handoff and Main resume

| Item | Value |
|---|---|
| Handoff refs | `ho-4091f043613e57a2059a845e` (file channel), `ho-4fbf145becaa635a1d7fd46b` (message channel) |
| Full payload | 21 309 and 19 375 characters, stored under the refs |
| Preview injected into the Main | 900 characters each |
| `handoff_payload_chars` | 3 434 |
| `delegate.read` calls | 0 — the Main worked from the previews plus its own reading |

The children produced structured reports (e.g. "# Research Report — `plugins/file_events`
(eventpipe) … Scope note: Read-only. No files were modified."), verified by digest in the handoff
index.

The Main resumed the same `AtlasAgentRunner` loop: after the tool result it made **22 further tool
calls** (5 `bash` of which 2 were refused, 12 `file_read`, 5 `file_edit`) across the remaining
requests, and stated that the reports agreed with its own reading. It never left the loop and no
second agent was created outside it.

## I. Context metrics

Provider-reported, `unavailable_fields: []`:

```json
{"main_requests": 13,
 "main_context_tokens_by_request": [2921,3580,5417,11220,11428,14828,18418,18994,19167,20215,20437,20586,20869],
 "main_peak_context_tokens": 20869,
 "main_cumulative_input_tokens": 188080,
 "main_cache_hit_tokens": 171520,
 "main_cache_miss_tokens": 16560,
 "main_completion_tokens": 3484,
 "child_total_prompt_tokens": 72793,
 "child_completion_tokens": 12544,
 "system_total_tokens": 276901,
 "handoff_payload_chars": 3434,
 "delegation_rounds": 1,
 "max_observed_child_concurrency": 2}
```

Accounting: `188 080 = 171 520 + 16 560`; system `= 188 080 + 3 484 + 72 793 + 12 544 = 276 901`.
The children's share of the run is 85 337 tokens (30.8 %).

**No context-reduction claim is made.** There is no direct baseline for this fixture in this stage,
so a comparison would be measurement without a control.

## J. Public tests and oracle

| Item | Result |
|---|---|
| `run_public_tests` calls by the Main | **0** — the budget ran out first |
| Independent oracle (in sandbox) | static **PASS** (4/4), dynamic **FAIL**, exit 1, 1 243 chars, 1 577 ms → verdict **FAIL** |
| Host-side replay of the public suite on the exported workspace | **34 passed, 1 failed** |

The single failing assertion (`evidence/run-1/host_public_test.txt`):

```text
tests/test_public_contract.py::test_a_message_record_becomes_a_normalized_event
assert event.kind == "user_join"
E  AssertionError: assert 'USER_JOIN' == 'user_join'
```

So the Main's implementation was one lower-casing call from passing the public suite; the oracle
result stands as recorded and was not re-run. This replay is analysis of an exported artefact, not a
sandbox execution and not a re-execution of the experiment.

## K. Budget, safety, sandbox

| Item | Value |
|---|---|
| Budget mode | `guarded`, limit 300 000 |
| Used | 276 901 (92.3 %) — prompt 260 873, completion 16 028 |
| Remaining at stop | 23 099 |
| Admission rejections | **1** — `budget_exhausted: worker atlas-main needs ~28294 tokens (used 276901, reserved 0), 23099 of 300000 left` |
| Peak reservation | 34 530; overrun 0; written off 0; `learned_ratio` 1.0764 over 24 samples |
| Per-worker tokens | main 191 564 · file worker 49 787 · message worker 35 550 |
| Sandbox | `opensandbox`, real provider, network probe PASS, host separation clean, `destroyed=true`, `retained=false` |
| Export | 23 files; patch 2 files (`core/adapter.py`, `core/registry.py`), 4 879 bytes, digest `sha256:b8a214c0…` |
| Ownership | `enforce`, 51 checked / 49 allowed / **2 denied**, both `OWNERSHIP_UNCLASSIFIABLE_SHELL` (`for … do … done`); neither executed |
| M4 ledger | 9 side-effecting actions (4 `bash`, 5 `file_edit`), all `COMPLETED` / `EXECUTED_SUCCESS`, **0** `REPLAYED` |
| Action safety | every call through M3; no write outside `core/` |
| Execution integrity | `record`, 51 payloads, 0 write failures — 51 tool inputs are recoverable from the private corpus (not published) |
| Residual containers | none |

## L. Remaining issues

1. **The Run Budget, not the delegation plane, ended this run.** 300 000 tokens covers 13 Main turns
   plus two 5–6-turn research workers on a fixture that asks both the Main and the children to read
   the same ten files. The rejection needed ~28 294 tokens with 23 099 left — about 5 200 short. This
   is a real property of the frozen configuration; it was **not** changed and the run was **not**
   repeated.
2. **Harness reporting bug, found after the run.** `summarize()` read
   `preflight_report["fixture_commit"]`, a key `evaluate()` never returns. The `KeyError` fired after
   `run_goal` had returned, so the run finished but the summary and collection steps did not. Fixed
   afterwards (commit explicitly passed; collection now runs before reporting). The evidence bundle
   was recovered from the run directory with the same `_collect()` path; the recovered
   `stage_b0_summary.json` is byte-identical to what the fixed summariser produces from the stored
   session.
   The freeze was **not** regenerated: `--freeze` refuses once an attempt exists, so the frozen
   digests remain the contract this run executed under. `harness_change_after_run.json` records both
   digest sets, and `--preflight` on this branch now reports `harness_frozen` BLOCKED **by design**.
3. **Session reporting gap on failure.** `session["main"]["turns"]` and `tool_calls` are `0`/`[]` when
   the goal path stops on budget, even though the transcript holds the full sequence. The trace above
   was reconstructed from `tool_conversations.json` and `transcript.jsonl`.
4. **`run_public_tests` was never exercised live** in this run (0 calls), so the tool's live path
   remains covered only by the stage P0.1 / P1 evidence.
5. **A `.pytest_cache/` directory** appeared in the exported workspace because the host-side replay ran
   there; it was removed and is not part of the evidence bundle.

## M. Final decision

**`B0_DELEGATION_MECHANISM_PASS_INTEGRATION_FAILED`**

| Acceptance criterion | Result |
|---|---|
| Main really called `delegate_tasks` | ✅ turn 3, two tasks with its own objectives and background |
| Router admitted two independent children | ✅ `delegate_parallel`, `DELEGATION_ADMITTED`, both admitted |
| Children really ran concurrently | ✅ `max_observed_concurrency=2`, ≈31.2 s overlap |
| Children completed independent investigations | ✅ 6/14 and 5/12 turns/calls, both `completed` |
| Main received bounded handoff | ✅ 900-char previews + 2 refs, full payloads 21 309 / 19 375 chars |
| Main resumed and completed integration | ❌ resumed and edited, but stopped on budget before finishing |
| Public tests PASS | ❌ never run by the Main (0 calls); host replay 34/35 |
| Independent oracle PASS | ❌ static PASS, dynamic FAIL |
| Sandbox cleaned up | ✅ destroyed, no residue |

The mechanism layers — Main proposal, Router admission, worker execution, handoff, Main resume —
all passed. The failure is at **integration**, caused by **budget admission**, not by the delegation
plane, the Router, the workers or the handoff path.

## N. Stage B readiness

What this run establishes with real evidence:

1. A real DeepSeek Main **does** call `delegate_tasks` when the run is framed as a delegation
   validation, and it writes substantive, channel-specific research tasks itself.
2. The Router admits exactly the intended shape (`delegate_parallel`, two read-only children) and the
   frozen policy needed no change.
3. Two real children execute concurrently inside one sandbox with isolated contexts and no write
   conflicts.
4. Handoffs are bounded previews plus refs, and the Main integrates from them without paging the full
   payload (`delegate.read` 0) — and it verified the reports against its own reading, which is the
   behaviour the design intends.
5. The Main resumes the same loop and edits only inside its granted scope.

What is still not established:

1. Whether an **unconstrained** Main delegates: stage P1b (autonomous) produced zero delegation, and
   this stage is explicitly controlled. Stage B must keep the two apart in any report.
2. Any cost or context benefit. This run has one arm, no direct baseline, and it did not finish; the
   children consumed 30.8 % of the tokens for two research tasks, which is a cost observation, not a
   benefit measurement.
3. That the frozen 300 000-token budget is sufficient for this fixture under delegation. It is not,
   for a Main that also reads everything itself: the run needed roughly 5 200 more admission tokens
   than remained.

**Blocking point for Stage B:** the budget has to be decided on purpose for a delegated run (either a
larger admission limit for this fixture class, or a run policy that makes the Main's own re-reading
cheaper), and that decision belongs to the benchmark design, not to a silent retry. No Stage B work
was started, and no second B0 attempt was made.

---

## Evidence index

```text
experiments/stage_b0_controlled/
  b0_contract.py               frozen goal, controlled instruction, limits (imported P1b suite/oracle)
  freeze.json                  source + harness + fixture + git identity used by this run
  run_stage_b0.py              --freeze | --preflight | --run, O_EXCL attempt marker, no retry
  evidence/run-1/
    preflight.json             READY admission (19 runtime + 14 stage checks)
    attempt-1.json             the one authorized attempt, with the frozen digests
    session.json               full run session (delegation, budget, verification, sandbox)
    stage_b0_summary.json      summary rebuilt by the fixed summariser
    transcript.jsonl           165 trajectory events
    tool_conversations.json    verbatim Main and child conversations
    workers.json               per-worker assignment and outcome
    handoffs/                  the two bounded reports (full payloads) + index
    workspace.patch            4 879 bytes, core/adapter.py + core/registry.py
    ledger_export.json         9 side-effecting actions, 0 replays
    host_public_test.txt       host-side replay: 34 passed, 1 failed
    harness_change_after_run.json  frozen vs post-run harness/source digests
    PRIVATE_PAYLOAD_NOTICE.txt the payload corpus is private and not published
```
