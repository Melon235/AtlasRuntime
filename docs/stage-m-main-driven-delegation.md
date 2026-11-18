# Stage M — Main-driven Delegation Integration

Branch: `feat/stage-m-main-driven-delegation`, based on `exec/closure-2c2-routing-pair-live`
(`443825d7136180ff9047cfb77bd1949162c70229`), AnyCode 0.9.0.

Decision: **`STAGE_M_ACCEPTED_OFFLINE`**. No live provider call was made, no frozen benchmark was
touched, and every acceptance item in the brief has a deterministic test.

---

## 1. M0 — code audit

Traced call sites, not file names.

| Question | Finding |
|---|---|
| Is there a real Main model loop to hook into? | Yes. `AtlasContextEngine` → `AnyCode.run_tasks` → `_run_wave_task` → `Agent.run` → `AgentRunner.stream`. Tools are dispatched inside the loop (`core/runner.py:620`), the result is appended as a `user` message (`:736`) and the loop continues. |
| Can a tool call await a whole sub-agent? | Yes. `ToolExecutor.execute` is `async`; `_execute_tool_blocks` already runs a turn's tool blocks concurrently behind `DEFAULT_TOOL_CONCURRENCY`. |
| Would a child deadlock on the agent pool? | Only if it took a pool slot. `run_tasks` holds one slot via `AgentPool.run_operation`; a child built directly from `Agent(...)` never calls the pool, so no. Proven with `max_concurrency=1` in the shipped composition. |
| Is there an admission router to reuse? | Yes: `atlasos/workers/routing.py` (`r0.1.0`, `RouteKind.DIRECT/DELEGATE_ONE/DELEGATE_PARALLEL`) with six ordered gates. It is **frozen**; stage M reads it and does not edit it. |
| Is there a handoff store to reuse? | Yes: `atlasos/handoff.py` `HandoffStore` (content-addressed, `ho-` refs, digest-verified). Its `HandoffReader` authorizes *DAG consumer → direct dependency only*, which a runtime-spawned child has no edge for. |
| Are write permissions enforceable by the runtime? | Yes: `atlasos/ownership/policy.py` `WorkspaceOwnershipPolicy` (R1-1) denies any write outside a worker's declared paths, keyed on the trusted `worker_id`. Its manifest is immutable and built from Host declarations. |
| Do children get a trustworthy identity? | Yes: `ScopedWorkerFactory._derive_context` derives `atlas_worker`, `worker_id`, `profile_id`, `task_id`, `parent_agent`. |
| Is token usage attributable per agent? | Partly. `AgentRunResult.token_usage` is produced by `anycode.helpers.usage_tracker.merge_usage`, which sums only `input_tokens`/`output_tokens` — **cache hit/creation counters are dropped**. `TurnHook.after_turn` sees the raw `LLMResponse`, so the split is recoverable there. |
| Does the budget hook reach every request? | Yes, if attached per agent. `AtlasContextEngine.build_agent` attaches it for engine-built agents; a directly built child must attach its own. |

No blocker was found. `AgentRunner`, the DAG scheduler, and the Context Plane needed **no**
modification: the only edits to existing files are additive seams (below).

## 2. Design

`Main → delegate_tasks → DelegationRouter → concurrent children → bounded handoff → Main continues.`

| Component | File | Reuses |
|---|---|---|
| Tool the Main calls | `delegation/tool.py` | `ToolDefinition` + core-tool registration |
| Admission | `delegation/router.py` | `route_delegation`, `RoutingPolicy`, `RouteDecision`, `decision_to_dict` |
| Execution | `delegation/service.py` | `ScopedWorkerFactory`, `AtlasToolAgent`, `RunBudgetHook`, `ConversationSnapshotHook` |
| Isolation | `delegation/usage.py`, `service.py` | `TurnHook` |
| Write scope | `delegation/ownership.py` | `WorkspaceOwnershipPolicy` + `build_manifest` |
| Handoff | `delegation/handoff.py` | `HandoffStore`, `HandoffReadInput` |
| Composition | `app/main_driven.py` | `AtlasContextEngine`, `AtlasToolRuntime`, `SQLiteActionLedgerStore` |

**Admission is not re-implemented.** The router builds *evidence the runtime will enforce* and asks
the frozen gates. The only evidence kinds used are ones the runtime makes true:

* `RISK_CLASS = read_only` when no Host grant applies — because the child then owns no path, so
  R1-1 denies every write;
* `RISK_CLASS = local_reversible_write` + `WRITE_PATHS` when a Host grant covers exactly the
  declared paths — because R1-1 then checks every write against that grant;
* `CONTEXT = PROVEN` only when the Main supplied a background — the child receives nothing else.

Consequence: the Main cannot widen its own authority by proposing paths, and the frozen
`GATE_5_CONTEXT_SUFFICIENCY` decides whether the assignment is sufficient instead of a stage-M
heuristic.

Two plan shapes are refused *before* the router, because the frozen vocabulary cannot express them:
`depends_on` (a delegation call is one wave, so a declared dependency would be silently unmet) and
a declared write path not covered by a Host grant.

## 3. Changes to existing files (additive only)

| File | Change | Why it is required |
|---|---|---|
| `workers/models.py` | `delegate_tasks` added to the delegate-tool denylist; `atlas_role`, `delegation_depth`, `delegation_root`, `parent_task_id` added to the reserved attribute set; `safe_parent_attributes` now takes a `Mapping` | A profile must not carry the delegate tool, and delegation identity must not be inheritable |
| `workers/factory.py` | derives `atlas_role="worker"`, `delegation_depth=parent+1`, `delegation_root`, `parent_task_id` | Runtime-enforced identity: a child cannot claim to be the delegation authority |
| `tools/runtime.py` | optional `core_tools` parameter (empty default) | The delegate tool must be a *native* core tool; searchable dispatch would not be a direct tool call |
| `docs/architecture.md`, `docs/runtime-entrypoints.md` | new section | behaviour changed |

**`config.py` is deliberately untouched.** `AtlasConfig.model_dump()` is hashed into
`experiments/evaluation/runner.py::_config_digest`, i.e. into every recorded experiment and
Closure-2 dry-run identity. Adding a `delegation` field there changed `settings_digest` and broke
two frozen-artifact tests; the field was removed and the limits moved to
`atlasos/delegation/config.py` (+ an optional `delegation:` YAML section read by the goal path only).
Frozen artifacts are unchanged.

## 4. Execution evidence (fake provider, real agent loop)

All numbers from `tests/integration/test_stage_m_main_driven.py`. Only the provider is fake; the
engine, wave scheduler, `AgentRunner`, tool registry, action gate, ledger and ownership policy are
the production ones.

| Acceptance item | Test | Observation |
|---|---|---|
| Main delegates through its own `AgentRunner` | `test_main_delegates_one_task_and_resumes_its_own_loop` | 2 Main provider turns; turn 2 carries the `tool_result` block; route `delegate_one`; child created via `ScopedWorkerFactory` |
| Two children really run concurrently | `test_two_children_run_concurrently_in_isolated_contexts` | both children blocked on an `asyncio.Barrier(2)` (serialized execution would time out), `max_observed_concurrency=2`, monotonic intervals intersect |
| Loop resumes after the tool result | same two tests | the second Main request contains the child summary and a `tool_result` block; the Main then finishes |
| No child history in the Main's requests | `test_two_children...` | child tail sentinels (`ALPHA-TAIL-SECRET`, `BETA-TAIL-SECRET`) absent from every Main request |
| Result is bounded | `test_the_main_tool_result_stays_bounded...` | child output 50 000 chars → summary ≤ 900, tool result ≤ 8 000, `ho-` reference present |
| Child write scope enforced | `test_child_cannot_write_outside_its_declared_scope` | `file_write("secret.txt")` → `OWNERSHIP_DENIED_UNDECLARED_PATH`; file not created; the declared sibling path is still writable |
| Main write scope enforced too | `test_main_can_write_inside_its_host_declared_scope` | declared `notes.md` written, 0 denials |
| No recursive delegation | `test_child_cannot_delegate_recursively`, `test_service_refuses_a_child_caller...` | layer 1: tool absent from the child surface ("is not allowed for this agent"); layer 3: `service.delegate` returns `NESTED_DELEGATION` for a child caller and creates no worker |
| Handoff authorized for the parent only | `test_handoff_reads_are_authorized_for_the_parent_only` | Main read succeeds; a child of the same run is denied; the DAG reader is not registered at all |
| Partial failure | `test_timeout_and_child_error_are_reported...` | one `timeout`, one `failed/child_no_result`, goal still `succeeded` |
| Timeout | same | `asyncio.wait_for` cancels the child; status `timeout` |
| Cancellation | `test_cancelling_the_goal_reaps_every_child` | after `task.cancel()` no `delegate:` task remains alive |
| Budget attribution | `test_budget_admits_and_attributes_each_child_separately` | 4 admitted requests (2 Main turns + 2 children); `RunBudgetState.workers` has exactly three entries; per-child miss/hit tokens exact |
| Budget rejection is a failure, not an empty success | `test_a_rejected_child_request...` | `status=failed`, `stop_reason=budget_exceeded`, `admission_rejections ≥ 1` |
| Ablation | `test_delegation_disabled_removes_the_tool...` | neither tool in `native_tool_names`, `delegation=None`, Main runs alone |

Token attribution: AnyCode's `merge_usage` drops cache counters, so `ChildOutcome` and the Main use
`UsageAttributionHook` (a `TurnHook`) instead of `AgentRunResult.token_usage`. `prompt_tokens` keeps
this repository's historical meaning (cache-miss side); `total_prompt_tokens` is the full prompt.

## 5. Router + unit evidence

`tests/unit/test_stage_m_delegation_router.py` (26 cases):

* `DELEGATE_ONE` for one read-only task with context; `DELEGATE_PARALLEL` for two independent ones;
* refusals: duplicate keys, unknown profile, missing background (gate 1 / gate 5), same write path
  in one wave (`SHARED_WRITE_RISK`), write outside the grant (`WRITE_SCOPE_NOT_GRANTED`), write on a
  read-only profile, path escaping the workspace, wave wider than the concurrency limit;
* `depends_on` refused before the router, with `frozen_decision is None`;
* determinism: identical inputs → identical decision; `policy_digest` moves with the concurrency
  bound; `POLICY_VERSION == "r0.1.0"` asserted and `m.1.0 != r0.1.0`;
* projection: tier 1 keeps summaries, tier 2 drops them and keeps refs, `bound_summary` respects its
  hard cap;
* identity: a profile cannot carry `delegate_tasks`; a child of a "main" parent is `worker`/depth 1
  and the parent context is unmodified; a grandchild is depth 2;
* configuration: `delegation` is absent from `AtlasConfig.model_dump()`; the YAML section is read
  separately; invalid limits are rejected; the default native surface is unchanged and a shadowing
  core tool is refused.

## 6. Regression result

* `pytest -q` — **1477 collected, 0 failures, 0 errors, 6 skipped** (junit XML), exit 0. The skips
  are pre-existing. Includes every workflow, DAG, M1–M7, R0–R1, Closure-1/2 and evaluation test.
* `ruff check src tests` — clean.
* `pyright` — 0 errors, 0 warnings (whole `src` + `tests`).
* New tests: 16 integration + 26 unit.
* Frozen artifacts untouched: `experiments/results/closure2/pilot-dry-run.json`,
  `experiments/results/**`, approval records and the pre-registration digests are unmodified
  (`git status` shows no change to any file under `experiments/`).
* No real DeepSeek call: every test injects a scripted provider; `run_goal` refuses to run without
  `DEEPSEEK_API_KEY` when no factory is injected.

## 7. Known limitations

1. **A single write-capable subtask cannot be delegated.** Frozen `GATE_1_DELEGATION_NECESSITY`
   refuses any single task that is not read-only (`TRIVIAL_DIRECT_TASK`). `DELEGATE_ONE` is reachable
   for read-only, context-supplied subtasks. Lifting this needs an R0.2 policy stage; stage M may not
   change `r0.1.0`.
2. **A provider failure inside a child is classified, not quoted.** `AgentRunner` turns the exception
   into an `error` event and returns without a `done` event, so the parent sees
   `failed/child_no_result` ("the model loop ended without completing a turn"), not the provider's
   message. It is never reported as an empty success.
3. **No checkpoint/resume for a goal run.** Same limitation as the coding compositions; children are
   reaped on cancellation but not persisted.
4. **Children are read-only by default.** A write grant is per profile and Host-owned; a
   `coding_worker` child with a grant is still subject to R1-1 shell classification, so a command it
   cannot prove read-only is denied (`OWNERSHIP_UNCLASSIFIABLE_SHELL`). This is the existing R1-1
   `PARTIAL` coverage, not a stage-M gap.
5. **Child output is summarised, never merged.** The Main must call `delegate.read` to see the whole
   result; the tool result intentionally cannot carry it.
6. **Windows path note.** R1-1 normalizes POSIX-style workspace-relative paths; on a Windows host,
   declared and attempted write paths must be relative (an absolute backslash path is refused as
   `OWNERSHIP_PATH_ESCAPE`). Pre-existing R1-1 behaviour.
7. **`main_driven` is a library entry point**, reachable as `run_goal` and as
   `python -m atlasos.app.main_driven`. It is not wired into the FastAPI/CLI application service,
   which keeps its workflow/coding modes unchanged.

## 8. Acceptance

| # | Requirement | Result |
|---|---|---|
| 1 | Main really delegates via `AgentRunner` | PASS — 2-turn Main loop, tool result fed back |
| 2 | Two subworkers actually concurrent | PASS — barrier + interval overlap + counter |
| 3 | Main resumes its loop after the tool result | PASS |
| 4 | Main's provider requests exclude child history | PASS — tail sentinels absent, result bounded |
| 5 | Worker permissions and handoff authorization enforced | PASS — ownership denials, parent-only reads |
| 6 | No recursion, no deadlock, no leaked tasks | PASS — `max_concurrency=1`, cancellation reaps |
| 7 | Main/child token evidence attributable | PASS — per-agent hook + per-worker budget counters |
| 8 | No DAG/runtime regression | PASS — 1477 tests, ruff, pyright |
| 9 | No live API, no frozen benchmark modification | PASS |

`STAGE_M_ACCEPTED_OFFLINE`. Stage P (live pipeline) and Stage B (final benchmark) remain separately
approvable and were not started.
