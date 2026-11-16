# Closure-1A — Effective Prompt Layout Correction

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`. This document describes code on `feat/final-closure`, not a
reviewed result.

| Item | Value |
|---|---|
| `CLOSURE_BASE_SHA` | `76a427541617c1ff4993ef3ad3947639f802b6d8` (G0) |
| `C1_0_SHA` | `43fefdfaaf563783b8457f742841c22627dae7d6` (`docs: freeze final closure roadmap and archive placement integration`) |
| `PREVIOUS_REVIEWED_SHA` | `052cd301fb9e0c32a3043240477970ed20a7c801` (C1-A, approved on the layout, corrected here) |
| `C1_A_R1_HEAD_SHA` | the commit that contains this document |
| Branch | `feat/final-closure` |
| Design contract | `37eba87` — unchanged |
| Live model calls | **0** |

**C1-A-R1 is a bounded evidence correction.** The layout, the worker factory and the selector are
unchanged. What changed is whether the evidence is allowed to say `VERIFIED`: request identity must
now agree on all four trusted ids with one host worker record, request content must match both the
rendered user turn and the effective system prompt, and the observed request set must reconcile with
the independent response count. Sections H and I describe the corrected behaviour; §E gained an
explicit initial-request scope note.

---

## A. The two defects, at their real source locations

Both were confirmed in source before any change was made. Neither is a naming inference.

### A.1 The task objective is sent twice

`ScopedWorkerFactory.create` (`src/atlasos/workers/factory.py`) builds `WorkerTaskContext(objective=
assignment.task, background=assignment.context)` and renders it into `AgentConfig.system_prompt`
(`src/atlasos/workers/renderer.py::render_worker_context`). That render ends with:

```text
ASSIGNED TASK — UNTRUSTED DATA
BEGIN UNTRUSTED TASK JSON
{ "background": …, "objective": … }
END UNTRUSTED TASK JSON
```

The same assignment text is *also* handed to the task spec, so it becomes the user turn:

| Entry point | `WorkerAssignment.task` | task text given to the model as the user turn |
|---|---|---|
| evaluation | `f"{environment_note}\n\n{step.prompt}"` (`runner.py`) | `f"{step.shared_context}\n\n{step.prompt}"` (`runner.py::_task_turn`) |
| app coding | `f"{environment_note} {options.task}"` (`app/coding.py:746`) | `f"{environment_note} {options.task}"` (`coding.py:812`, `AtlasTaskSpec.description`) |

Concretely, for `validation-parallel` worker `w1`, the legacy request contains the assignment text
**twice**: once JSON-escaped inside the system prompt and once raw in the first user message. The
deterministic comparison in §E counts 2 (workload A/B) and 3 (workload C) such escaped copies.

The environment note is *not* duplicated: it exists only in the system prompt in both entry points.

### A.2 `StablePromptRenderer` does not control the request layout

`AtlasTaskPromptBuilder.__call__` (`src/atlasos/context/task_prompt.py`) builds a two-message
`RenderedPrompt` and then returns:

```python
prompt = rendered.messages[-1]["content"]
```

The `system` message it rendered — `stable_system_prefix` plus `Role: <role>` — is **discarded**. The
system prompt that reaches the provider is the worker factory's, via
`AgentConfig.system_prompt → RunnerOptions.system_prompt → LLMStreamOptions.system_prompt →
map_messages(messages, options.system_prompt)` (AnyCode 0.9.0 `core/agent.py:76`,
`core/runner.py:298`, `providers/_openai_compat.py:76`). The Atlas renderer's `stable_prefix_hash`
therefore described a string that was never sent.

### A.3 The title interrupts the common prefix

`_render_body` (`src/atlasos/context/renderer.py`) began with `f"Task: {task.title}"`. Two workers of
one profile therefore diverged at the first line of the user turn — before any shared public text —
even when the public requirement was byte-identical.

---

## B. Real call chain, both layouts

```text
Task objective / shared public context
  └─ experiments/evaluation/final4/validation_tasks.py  Task.shared_context, workers[i]["assignment"]
      ├─ runner_entry.build_task → BenchmarkTask(workers=[WorkerStep(shared_context=…, prompt=…)])
      └─ runner.run_sample
          ├─ ScopedWorkerFactory.create(assignment=…, prompt_layout=…)
          │     └─ render_worker_context(profile, task, layout=…)
          │           └─ AgentConfig.system_prompt            <- effective system prompt
          ├─ _task_turn_parts(step)  -> (shared public block, worker's own body)
          ├─ _context_prompt_builder(...)
          │     └─ AtlasTaskSpec(description=…, shared_context=…)
          ├─ AtlasContextEngine(prompt_builder=…).run_tasks
          │     └─ _build_task_prompt → AtlasTaskPromptBuilder
          │           ├─ _project        → task-output:<id> blocks in ContextStore
          │           ├─ TaskScopedSelector.select
          │           ├─ transform (identity)
          │           └─ StablePromptRenderer.render(..., shared_context=…)
          │                 └─ builder returns rendered.messages[-1]["content"]  <- user turn
          └─ AnyCode AgentRunner.stream
                └─ map_messages(messages, options.system_prompt)
                      └─ client.chat.completions.create(**kwargs)
```

| | legacy | shared_prefix |
|---|---|---|
| system message | trusted profile + scope + tools + rules + completion + **untrusted task JSON** | trusted profile + scope + tools + rules + completion + a task-delivery note |
| user turn order | `Task: <title>` → task body → dependencies | **shared public block** → `Task: <title>` → task body → dependencies |
| environment note | system message | user turn, inside the shared block |
| objective | system (escaped) **and** user | user only, once |
| prepared by | `render_worker_context` + `_render_body` | `render_worker_context(layout=…)` + `_render_body(shared_context=…)` |

Selection is identical in both layouts: the same `TaskScopedSelector` over the same declared
dependencies. Only membership-independent ordering and the system/user split differ.

---

## C. Interfaces changed

`src/`

| File | Change |
|---|---|
| `context/renderer.py` | `PromptLayout` (`legacy` / `shared_prefix`); `render(..., shared_context="")`; `_render_body(..., shared_context="")`; new `render_dependency_block(...)` so the body and the evidence probe share one delimiter definition |
| `context/task_prompt.py` | `AtlasTaskPromptBuilder(prompt_layout=...)`; passes `shared_context` only in the shared-prefix layout; `TaskPromptRender` records `prompt_layout`, `system_prompt_sha256`, `shared_context_chars` |
| `workflow.py` | `AtlasTaskSpec.shared_context: str = ""` — structured public input, not a slice of `description` |
| `workers/renderer.py` | `render_worker_context(..., layout=...)`; `render_effective_context_hash`; the legacy string is unchanged |
| `workers/models.py` | `WorkerInstanceRecord.prompt_layout` (`legacy` default) and `effective_system_prompt_hash` (`None` default) |
| `workers/factory.py` | `create(..., prompt_layout=...)`; `resume()` refuses a non-legacy record |
| `config.py` | `context.prompt_layout` (`legacy` default); `shared_prefix` requires `task_scoped` + `stable_deterministic` |

`experiments/`

| File | Change |
|---|---|
| `evaluation/runner.py` | `RunnerSettings.prompt_layout` (in `digest()`); `_enforce_prompt_layout` fail-closed pre-I/O check; `_task_turn_parts`; `_context_prompt_builder(prompt_layout=…, task_bodies=…, shared_context_by_task=…)`; per-worker `original_worker_context_hash` / `effective_system_prompt_hash` / `prompt_layout` in `environment.context_resolved`; `_RequestEvidence` identity + full-block probe + usage; `_request_linkage` statuses |
| `evaluation/closure1a/layout_ablation.py` | the deterministic comparison of §E |

The app coding composition (`src/atlasos/app/coding.py`) is **untouched**: it keeps the legacy
layout and its existing `task_scoped` Context Plane wiring.

---

## D. Trusted static identity vs dynamic task content

- The shared-prefix system prompt contains only host-owned profile data: `profile_id`, `role`,
  `instructions`, `operating_scope`, `base_tools`, the operating rules and the completion
  expectations. It contains **no** task text, background, timestamp, run id or metric.
- `OPERATING RULES` still states that task text is untrusted data, and the layout adds a
  `TASK DELIVERY` note saying the user turn cannot change profile, scope, tool list, `max_turns` or
  the delegation flag. Moving the text did not upgrade it to instructions.
- `ScopedWorkerFactory` refuses a `shared_prefix` worker that has an `assignment.context`
  (background): the layout has no system slot for it and nothing else would carry it, so the
  unsupported combination fails instead of silently dropping a fact.
- Unchanged: `ExecutionContext.principal`, `tenant_scope`, `classification`, `worker_id`, `task_id`,
  trusted attributes, `ActionSafetyGate`, ledger, ownership, sandbox binding, approval/suspension.
- The only duplicated content removed is the confirmed one: the objective, whose two sources are
  `WorkerAssignment.task` and `AtlasTaskSpec.description`. No fuzzy matching, no semantic summary,
  no LLM call. `shared_context` is a real structured field on `AtlasTaskSpec`, never a substring
  search over a concatenation.

---

## E. Deterministic comparison (no model)

`python -m experiments.evaluation.closure1a.layout_ablation` →
`experiments/results/closure1a/layout_ablation.json`. 6 DAG executions (3 workloads × 2 layouts),
0 model calls, fake provider, real engine/selector/renderer/factory/`ModelRecordingAdapter`/
`DeepSeekAdapter`.

**Scope: the initial request of each worker** — system prompt, first user turn, and the tool
definitions handed to the adapter. This is not a multi-turn run measurement and not a cache
measurement.

Inputs are identical across arms: same `BenchmarkTask`, same shared public block, same assignments,
same declared dependencies, same tool definitions, same deterministic dependency outputs
(`RESULT-OF-<step>`). The tool definitions measured are the ones captured in `options.tools` at the
adapter boundary, not rebuilt from a second registry.

Tokenizer: `tiktoken cl100k_base` 0.14.0 over the JSON serialisation of the mapped API messages plus
tool definitions. It excludes the provider chat template and is a content estimate, **not** billed
tokens.

| Workload | Arm | system chars | user chars | request chars | est. tokens | common prefix (chars) |
|---|---|---|---|---|---|---|
| A pair (w1,w2) | legacy | 3 856 | 4 294 | 17 972 | 4 592 | 1 288 |
| A pair | shared_prefix | **2 222** | 4 602 | **16 594** | **4 207** | **2 780** |
| B serial (w1→w2) | legacy | 4 203 | 5 581 | 19 626 | 5 002 | 1 288 |
| B serial | shared_prefix | **2 222** | 5 889 | **17 899** | **4 519** | **3 222** |
| C join (w1,w2,w3) | legacy | 5 920 | 6 709 | 27 378 | 7 007 | 1 288 |
| C join | shared_prefix | **3 333** | 7 171 | **25 175** | **6 394** | **2 780** |

| Workload | request chars | est. tokens | common prefix | escaped objective copies in system |
|---|---|---|---|---|
| A | −1 378 (−7.7 %) | −385 (−8.4 %) | +1 492 | 2 → 0 |
| B | −1 727 (−8.8 %) | −483 (−9.7 %) | +1 934 | 2 → 0 |
| C | −2 203 (−8.0 %) | −613 (−8.8 %) | +1 492 | 3 → 0 |

The user turn grows by 308–462 characters because the environment note moved into it. The total
request still shrinks. **The character common prefix is a serialisation property, not a provider
KV-cache prefix**, and nothing here claims a cache or cost effect.

### E.1 What is retained, per workload

| Check | A | B | C |
|---|---|---|---|
| shared public block verbatim in every request | yes | yes | yes |
| declared dependency present exactly once, both arms | yes (none declared) | yes | yes |
| unrelated dependency absent from a worker's turn | yes | yes | yes |
| objective still present in the request | yes | yes | yes |
| legacy system prompt differs between workers | yes | yes | yes |
| shared-prefix system prompt identical between workers | yes | yes | yes |
| every request's content matches its render, both arms | yes | yes | yes |
| every request identity trusted, both arms | yes | yes | yes |
| request set reconciled against the response count | yes | yes | yes |
| `REQUEST_CONTENT_VERIFIED` in both arms | yes | yes | yes |

Workload A is measured on `w1`/`w2` (the independent pair); workload C on `w1`,`w2`,`w3` (the pair
plus the join). The DAG itself is identical in both cases, which is why the raw totals differ.

Coverage, per arm: A `3 observed / 3 completed / 3 responses`, B `2 / 2 / 2`, C `3 / 3 / 3`, all
`RECONCILED`, all completed requests carrying usage. `VERIFIED` is granted only when every one of
those holds — a task being observed at least once is not the same claim as the request set being
reconciled.

---

## F. WorkerRecord vs Effective Request Hash

The persisted record keeps its historical meaning and the resume contract does not move:

| Field | Meaning |
|---|---|
| `WorkerInstanceRecord.rendered_context_hash` | always the **legacy** render of `(profile, worker task context)` — the value `resume()` verifies |
| `WorkerInstanceRecord.prompt_layout` | which layout actually produced the running system prompt |
| `WorkerInstanceRecord.effective_system_prompt_hash` | `sha256` of the bytes handed to AnyCode; `None` on records written before this stage |
| `context.json → tasks[].task_prompt_sha256` | the effective user turn of one render (unchanged field) |
| `run.json → environment.context_resolved.workers[]` | `original_worker_context_hash`, `effective_system_prompt_hash` and `prompt_layout` side by side |

`ScopedWorkerFactory.resume()` **refuses** a `shared_prefix` record: worker resume is defined for the
legacy layout only. Coding crash recovery for the new layout is not implemented and is not claimed.

---

## G. Configuration and supported scope

```yaml
context:
  policy: task_scoped            # required
  renderer: stable_deterministic # required
  transform: identity            # the validated control this stage uses
  prompt_layout: shared_prefix   # new, default legacy
```

- `legacy` is the default. Every shipped config (`experiments/configs/*.yaml`) still resolves
  `prompt_layout: legacy`; `tests/integration/test_closure1a_prompt_layout.py` asserts it.
- `prompt_layout` is part of `RunnerSettings.digest()`, so two arms that behave differently cannot
  share an effective configuration digest. A unit that declares one layout and loads a YAML that
  resolves another is refused before any I/O, mirroring the Final-4 G-1 fix.
- Supported first scope: the `task_scoped` + `stable_deterministic` + `identity` coding evaluation
  path. **Every other combination is refused, not ignored:**
  - a non-identity transform with `shared_prefix` fails configuration validation, the prompt builder
    and the evaluation pre-flight check;
  - an unknown layout name raises instead of defaulting to `legacy`;
  - the coding app (`_build_context_plane`) and the workflow entry point
    (`_configure_context_mode`) raise if a config resolves `shared_prefix`, because neither
    implements it and silently rendering legacy would make the run's layout record untrue;
  - workflow-path recovery is unaffected: it never used the worker factory.
- Historical experiment digests and raw run data were not modified.

---

## H. Request-boundary evidence

Observed inside the provider wrapper on the outgoing message list
(`experiments/evaluation/runner.py::_RequestEvidence`). The observation now happens **inside** the
stream generator, i.e. when the request is actually issued: a stream that is created and never
consumed produces no record, a stream abandoned after it started is `CANCELLED`, a call that raises
is `FAILED`, and a request is `COMPLETED` only when the provider answered. `ModelRecordingAdapter`
wraps the real adapter and the inner `stream` delegates to the inner `chat`, so one request is
counted once.

| Question | Before | Now |
|---|---|---|
| Who sent this request? | `sha256(system_prompt)` | all four `options.execution_context.attributes` ids (`task_id`, `worker_id`, `profile_id`, `run_id`) must agree with **the same** host-owned worker record |
| Did the content match? | not checked | `sha256(first message)` must equal the render's `task_prompt_sha256`, **and** `sha256(system prompt)` must equal that worker's effective system prompt hash |
| Injected in the initial prompt? | first 120 characters of the block | the **complete structured** `<dependency task_id=… version=…>` block, produced by the same function the renderer uses |
| Later turns | not distinguished | `count` is measured on `messages[0]`; `later_history_mentions` records a later assistant/tool echo as redundancy, never as a second injection |
| Provider usage | run-level aggregate only | attached per completed request from the response's `TokenUsage` (`prompt_tokens`, `cache_miss_tokens`, `cache_hit_tokens`, `completion_tokens`) |
| Request-set coverage | "each task was seen at least once" | reconciled against the inner adapter's response count: `observed == responses + failed + cancelled`, with no unfinished request |
| Status | `VERIFIED` if **any** request matched | see below |

Statuses:

| Status | Condition |
|---|---|
| `REQUEST_CONTENT_NOT_VERIFIED` | no request observed, or none linked to a render |
| `REQUEST_CONTENT_PARTIAL` | some requests linked, but any of: a rendered task never observed; a request without a host worker record; an identity field mismatch; a first-message or system-prompt digest mismatch; a declared dependency not delivered as a complete block exactly once; a failed/cancelled/unfinished request; an unreconciled request set |
| `REQUEST_CONTENT_VERIFIED` | all of the above clean, in every observed request |

Reason codes emitted: `TASK_NEVER_OBSERVED`, `REQUEST_WITHOUT_A_TASK_IDENTITY`,
`IDENTITY_NOT_TRUSTED`, `IDENTITY_MISMATCH`, `REQUEST_CONTENT_MISMATCH`, `SYSTEM_PROMPT_MISMATCH`,
`DEPENDENCY_NOT_FULLY_DELIVERED`, `REQUEST_NOT_COMPLETED`, `REQUEST_COVERAGE_UNVERIFIED`.

An empty dependency body is still probed as a structured block (`content_empty: true`,
`content_present: null`), and a consumer request with no probe for a dependency the store holds is
reported as `NO_PROBE_ON_CONSUMER_REQUEST` rather than skipped.

Concurrency: `validation-parallel` gives three workers one identical system prompt. The integration
test asserts the three observed requests carry one `system_prompt_digest` and are still attributed
to `w1`, `w2`, `w3` by trusted identity.

Content persisted: digests, character counts and booleans only. No prompt, transcript, completion or
tool output.

---

## I. Tests

New: `tests/unit/test_worker_prompt_layout.py` (25), `tests/integration/test_closure1a_prompt_layout.py`
(25). Modified: two expectations in `tests/integration/test_final4_context_plane.py` — the dependency
probe is now the complete structured block (the old test encoded the 120-character head assumption),
and an unconsumed stream is no longer counted as a sent request (the old test asserted eager
observation).

Coverage of the required list: legacy bytes and resume; shared-prefix system prompt without
objective/background; identical system prompt for two workers of one profile; objective retained in
the request once; duplicate removed; public block before the title and the dynamic body; selector
unchanged; unrelated dependency excluded; trusted/untrusted boundary and `ExecutionContext`
unchanged; real `AgentConfig.system_prompt` verified through the engine; captured mapped messages
verified through `DeepSeekAdapter.chat`; stream path observed; concurrent identical prompts attributed
correctly; partial linkage not reported as verified; missing task detected; head-identical /
tail-different dependency rejected; legacy vs shared-prefix configuration identity distinguishable;
unsupported layout + resume combination refused.

C1-A-R1 counterexamples, one test each: a modified user turn with a correct task id; a modified
system prompt; a wrong `worker_id`, `profile_id` and `run_id`; a missing host worker record; a second
turn of one task whose record is dropped (task coverage still complete, coverage unreconciled); a
consumer request with the probe removed; an empty dependency body; a dependency quoted in the
assistant history; `ModelRecordingAdapter → DeepSeekAdapter.stream → chat → stub client` end to end
(system message, first user turn, mapped tools, one observation, identity, usage, and an unconsumed
stream producing nothing); an unknown layout name; `shared_prefix` with a non-identity transform, in
the config validator, the prompt builder and the coding app.

Results are in the final report; the commands were `pytest -q`, `ruff check .`,
`ruff format --check .`, `pyright`.

---

## J. Not established by this stage

- Provider cache-hit, cache-miss, prefill, latency or cost of the new layout. **Not measured**: no
  model was called, and X0/X1 already showed that provider cache state dominates at that sample
  size.
- Any task-success or quality difference. The inputs are informationally equivalent by construction;
  that is an argument, not a measurement.
- Behaviour on the app coding path, on non-identity transforms, or under crash recovery.
- A fair comparison against native AnyCode (C1-B) and the consolidated historical table (C1-C).
