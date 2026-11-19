# Stage S2 — Main System Prompt v2.1 Final Pilot

Branch `exp/main-prompt-v21-final-pilot`, based on `exp/main-prompt-v2-pilot` at `1d03a2f`.
One live DeepSeek run, executed once. No runtime file was modified: this stage is a prompt, a
fixture, an oracle and an analysis layer.

**Decision: `GOAL_PASS` + `DIRECT_EXECUTION`.**

The Main read fourteen targeted files, concluded in its own words that "only
`workgrid/scheduler/engine.py` and `report.py` need implementing (small, tightly coupled work)",
wrote both, ran the public suite twice, and stopped at turn 13 with 62 public tests passing and the
independent oracle passing. It never proposed a delegation. Total cost: 14 requests, 200 744 tokens
of 500 000.

That is a correct result reached the Direct way. It is **not** evidence that v2.1 delegates well —
the delegation path was not exercised at all — and it is not a comparison with v1 or with Stage S1's
task.

---

## A. Git provenance

| Item | Value |
|---|---|
| Baseline | `exp/main-prompt-v2-pilot` @ `1d03a2f6efb1e4ae45ccf8130d57917fe08eae3a`, worktree clean, `rev-list --left-right --count` = `0 0` against the already-fetched ref |
| Branch | `exp/main-prompt-v21-final-pilot` |
| Harness | `76088a0` — fixture, contract, offline suites, analysis, runner |
| Harness fix | `3590348` — a shared `pytest.ini` no longer counts as a reused task (see §F) |
| Freeze written at | `76088a0` (ancestry enforced, equality not used) |
| Freeze commit | `14b118e` |
| Run id | `stage-s2-20261119T103641Z` |
| Fixture commit (in sandbox) | `8acfc46c452d385fdd9b1e906ee9624ec75addb9` |
| Attempt marker | `<scratch>/attempts/attempt-1.json`, claimed `2026-11-19T10:36:41Z`, copied into the evidence |
| Evidence | `experiments/stage_s2_prompt_v21/evidence/run-1/` |
| Live attempts | 1 — no retry path exists, and none was used |

`git diff 1d03a2f -- src/` is empty: S2 added no runtime code. The prompt seam from Stage S1 is used
unchanged, and the preflight check `runtime_unmodified` asserts it rather than trusting it.

Remote sync caveat, stated rather than glossed: `git fetch origin` could not complete from this
environment at baseline time (`schannel: failed to receive handshake`), so the `0 0` above is against
the ref that the Stage S1 push created. The push at the end of this stage is the network confirmation.

## B. Prompt v2.0 → v2.1

`experiments/stage_s2_prompt_v21/prompts/main_v21.md`, label `main-v2.1-rc1`, 10 377 characters,
`sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489`. It was produced by copying
v2.0 verbatim and applying three hunks — 66 lines added, 4 removed, nothing else touched. The diff is
`git diff --no-index` between the two prompt files.

### Hunk 1 — `## EARLY DELEGATION DECISION` becomes `## EARLY PLANNING CHECKPOINT`

*Added:* a checkpoint placed **before** the existing candidate list, with an operational definition of
"lightweight discovery" (task, layout, top-level interfaces, workstreams — not all implementation
files), an explicit list of what to decide at the checkpoint, and four prohibitions: do not defer the
decision until the investigation is done, do not delegate for file count, Direct is valid when work is
small/tightly coupled/coordination costs more, and reconsider later for *remaining* work rather than
repeating completed investigations. The v2.0 candidate list, the "two investigations" preference and
the "delegation is a preference" sentence are kept verbatim below the new text.

*S1 evidence it answers:* the first delegation came at **turn 17 of 18**, after 34 tool calls and 22
read targets, triggered by a tool denial rather than by structure. The old heading said "evaluate
BEFORE extensively investigating"; the new block says what "before" means and forbids the specific
behaviour observed — having already completed the investigation when the decision is made.

### Hunk 2 — `## WORKER CAPABILITY CONTRACT` (new section, after `## DELEGATION CONTRACT`)

*Added:* before delegating, check that the task is executable with the selected profile's actual
tools; `research_worker` is read-only with `file_read`, `grep`, `list_files` and has no bash, Python,
pytest, `sha256sum` or `run_public_tests`; do not assume a worker has Main's tools; do not use
delegation to bypass a refusal; `tool.search` discovers *external* tools and is not the way to find
native ones; do not call a native tool through `dispatch_action` unless it was disclosed as
dispatchable; a valid task must be both Router-authorized and executable; prefer read-only
deliverables; if no profile can do it, do it directly or report the blocker.

*S1 evidence it answers:* the single delegated objective asked a read-only worker to run `python3
--version`, `sha256sum` and `pytest`. The child spent six turns and twelve tool calls searching for a
shell (`tool_search` → `{"matches": []}` six times; three `dispatch_action` refusals), produced one
sentence, and died at `max_turns`. Every clause above is a description of that failure.

### Hunk 3 — `## IMPLEMENTATION AND VERIFICATION DISCIPLINE` (new section, before `## CONTEXT AND COST`)

*Added:* investigation is a means, not the objective; once the contracts are understood, begin the
smallest correct authorized implementation; stop broad exploration when targeted inspection or tests
can settle the remaining doubt; do not repeat refusals of the same kind; use `run_public_tests`
rather than interpreter shell commands; do not delegate test execution to a worker that lacks the
tools; after a useful handoff, move to integration instead of repeating the investigation; reserve
capacity for coding, tests, repair and reporting; when tool use stops producing progress, change
approach according to the tool contract.

*S1 evidence it answers:* S1 spent 397 300 tokens and **changed zero files**: 16 investigation turns,
one late delegation, one futile `dispatch_action`, budget exhausted. It also never called
`run_public_tests` (0 of 4) and kept probing with shell after six identical refusals.

Nothing else changed: identity, execution workflow, when-to-work-directly, delegation contract,
handoff-first integration, context and cost, workspace safety, testing and recovery, and final
delivery are byte-identical to v2.0. No "always delegate", no worker count, no fixture-specific
arrangement, and the prompt names no package or symbol of the task under test (`workgrid`,
`scheduler`, `dependency graph`, `availability window`, `build_schedule` all absent — checked at
admission).

## C. Prompt injection evidence

`offline.json` → suite F2 (scripted provider, no model call):

| Check | Result |
|---|---|
| Default run sends exactly `MAIN_SYSTEM_PROMPT` | yes, `main-v1.0`, `sha256:7e6fd829…` |
| v2.0 file and digest unchanged | yes, `sha256:2bb50b09…` |
| Override run sends exactly the frozen v2.1 text | yes, verbatim on every request |
| Session records version/digest/source | `main-v2.1-rc1`, `sha256:4147f19c…`, `source=override` |
| Prompt contains the goal | no |
| Prompt contains the three revisions | yes |
| Prompt contains a forced-delegation phrase | no |
| Main tool surface identical to the default run | yes |
| Child prompt byte-identical under v1 and v2.1 | yes, `TRUSTED WORKER PROFILE…` |
| `research_worker` has no execution tool | yes — catalog = `{dispatch_action, file_read, grep, list_files, tool_search}` |
| The prompt's claim matches the catalog | yes |

6 unit tests in `tests/integration/test_stage_s2_prompt.py` cover the same seam properties, including
that the v2.1 file still hashes to its frozen digest and that a run with no override is unaffected.

## D. New fixture and oracle

`experiments/fixtures/stage_s2_workgrid` — 27 files, 19 Python modules, ~2 900 lines:

| Package | Contents | State |
|---|---|---|
| `workgrid/dependencies` | `graph`, `ordering`, `validation`, `errors` | complete |
| `workgrid/resources` | `capacity`, `calendar`, `constraints`, `errors` | complete |
| `workgrid/scheduler` | `contracts` (complete), `engine` + `report` (stubs) | incomplete |
| `tests/` | `test_dependencies` (20), `test_resources` (21), `test_public_scheduler` (15) | 47 pass, 15 fail |

The two rule packages are independent by construction: neither imports the other and neither imports
the scheduler, and their failures are different shapes (typed findings vs typed exceptions), so the
shared layer has real integration work. The engine's docstring states the six placement rules
precisely enough to be implemented without guessing, which is what makes the task a coding task rather
than a puzzle.

The hidden program (`reference/oracle_program.py`, `sha256:ff083221…`) travels into the sandbox
base64-encoded inside the verification command and never as a file. It asserts what the public suite
does not: dependency delay with spare capacity, precedence between a blocked dependency and an
impossible demand, cascade blocking, cycle and dangling-reference propagation, plan stability across
task declaration order, graph immutability, canonical key and task order, and the shipped sample.

## E. Offline checks

`--offline` → all three suites pass.

**F1 solvability.**

| Case | Result |
|---|---|
| Baseline compatibility suites | pass (41 tests) |
| Baseline target suite | fail (15 of 15) |
| Baseline oracle | rejects |
| Reference implementation | 62 pass, oracle `HIDDEN_OK` |
| 5 wrong variants | **all rejected by the hidden program alone** |

The variants, and whether the public suite would also have caught them:

| Variant | Defect | Oracle | Public suite |
|---|---|---|---|
| `dependencies_ignored` | work starts before its inputs exist | rejected | also caught |
| `capacity_ignored` | more concurrent work than the pool holds | rejected | also caught |
| `tie_order_follows_declaration` | simultaneous tasks ordered by insertion | rejected | **not caught** |
| `broken_graph_swallowed` | cycle reported as an ordinary empty plan | rejected | **not caught** |
| `windows_ignored` | availability windows treated as always open | rejected | also caught |

Two of five are invisible to the public suite, which is the argument for a separate oracle.

**F2 prompt injection** — §C. **F3 router admission** — the frozen `m.1.0` router admits two
independent read-only investigations as one wave (width 2), and refuses a shared write
(`SHARED_WRITE_RISK`), a task with no background, and a fake "parallel" batch with a declared
dependency (`DEPENDENCY_NOT_SUPPORTED`).

## F. Freeze and preflight

| Frozen item | Value |
|---|---|
| Runtime source | `sha256:4eebcf1e…` over 130 files — identical to the S1 baseline |
| Prompt v1 / v2.0 / v2.1 | `7e6fd829…` / `2bb50b09…` / `4147f19c…` |
| Fixture | `sha256:893238ef…` over 27 files |
| Goal | `sha256:83c72401…` |
| Oracle program | `sha256:ff083221…`, command `sha256:…` |
| Write scope | `("workgrid/scheduler/",)` |
| Budget / turns | 500 000 tokens, Main 20 turns, 3 delegation rounds, 2 children, 6 child turns |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB |
| Tool error mode | `corrected` |

`--preflight` → **READY**: 19 runtime + 27 stage checks, 0 blocking. One defect was found by the first
preflight and fixed before any model call: the reuse check treated a byte-identical two-line
`pytest.ini` as a reused task, so it now ignores named scaffolding and compares everything else.

## G. Real Main trace

14 model requests, 24 tool calls, 13 stored assistant turns, 54.4 s, `stop_reason=success`.

| Turn | Calls | Note |
|---|---|---|
| 1 | `bash` (find), `list_files` | "I'll start with lightweight repository discovery." |
| 2 | `bash` **denied**, `bash` (ls) | `OWNERSHIP_UNCLASSIFIABLE_SHELL` |
| 3 | `list_files`, `file_read` README | "Shell access to files is denied; I'll use dedicated file tools." |
| 4 | `file_read` ×3 | `contracts.py`, `engine.py`, `report.py` |
| 5 | `file_read` ×3 | `scheduler/__init__.py`, `dependencies/graph.py`, `resources/constraints.py` |
| 6 | `file_read` ×3 | `calendar.py`, `capacity.py`, `ordering.py` |
| 7 | `file_read`, `bash` (ls samples) | `resources/errors.py` |
| 8 | `file_read` samples/plan.json | "Clear picture: only `engine.py` and `report.py` need implementing (small, tightly coupled work)" |
| 9 | **`file_edit`** engine | "Implementing the engine exactly per its documented contract." |
| 10 | `file_edit` report | "Now the renderer:" |
| 11 | **`run_public_tests`** | pass |
| 12 | `file_edit` ×2 | typing fix in the placement helper |
| 13 | `run_public_tests` | "All 62 public tests pass." |

The patch touches exactly `workgrid/scheduler/engine.py` and `workgrid/scheduler/report.py`
(5 743 characters of diff). Nothing outside the authorized package was written; nothing outside it
was even attempted.

## H. Early delegation analysis

| Question | Observation |
|---|---|
| First `delegate_tasks` | never — `delegate_turns = []` |
| Investigation before the decision | 14 read targets over turns 1–8, 16 tool calls |
| Decision point | turn 8, stated in the Main's own words as "small, tightly coupled work" |
| Was the decision made before exhaustive reading? | **yes** — it read 14 of the fixture's 27 files and skipped `dependencies/validation.py`, `dependencies/errors.py`, `resources/pool`-style helpers and the compatibility suites |
| Did structure drive it? | it named the two stub files and the coupling, not a subsystem count |

This is a legitimate Direct decision under the checkpoint the prompt defines: two files, a documented
contract, and investigation that was cheap. It is also a decision that never tested the delegation
path, so this run says nothing about whether the checkpoint produces good delegation *proposals*.

## I. Worker capability analysis

No delegation occurred, so there is no objective to judge. What is observable is the behaviour that
Stage S1 got wrong being avoided:

* one shell refusal at turn 2, adapted at turn 3 with an explicit statement of the alternative;
* no attempt to delegate the shell work it could not do;
* `run_public_tests` used twice (2 of 4 allowed) instead of interpreter shell commands;
* no `dispatch_action` on an undisclosed tool, no `tool_search` for native tools.

That is the *Main* respecting the capability contract. It is not evidence that it would have written a
capability-matched task — untested here.

## J. Implementation and testing analysis

| Observation | Value |
|---|---|
| First write turn | **9** of 13 |
| Turns before the first write | 8 (7 of them investigation) |
| Writes | 4 `file_edit` calls in 3 turns |
| `run_public_tests` | 2 calls, both PASS, exit 0, 62 tests |
| Shell refusals | 1 (S1: 6) |
| Files changed | 2 |
| Budget used | 200 744 of 500 000 (40 %) |
| Stop reason | `success` |

The sequence is the one the new section asks for: read the contracts, stop, implement, run the suite,
repair, run again, report. The first test run came at turn 11 — after the implementation, not as a
substitute for it.

## K. Handoff utilisation

Not applicable: no child ran, no handoff existed, `delegate.read` was never called, and there was no
re-reading of worker output because there was none. Recorded as empty rather than described.

## L. Context and budget

| Metric | Main | Children | System |
|---|---|---|---|
| Requests | 14 | 0 | 14 |
| Peak context | 20 044 | — | — |
| Cumulative input | 197 430 | 0 | 197 430 |
| Cache hit / miss | 178 688 / 18 742 | 0 | 178 688 / 18 742 |
| Cache hit ratio | 0.905 | — | 0.905 |
| Completion tokens | 3 314 | 0 | 3 314 |
| **Total** | **200 744** | 0 | **200 744** |
| Budget rejections | 0 | — | 0 |
| Wall clock | 54.4 s | — | — |

Context per request: 4 259 → 4 706 → 7 169 → 8 721 → 10 086 → 13 254 → 16 084 → 17 162 → 17 958 →
19 100 → 19 421 → 19 586 → 19 880 → 20 044.

No comparison with Stage S1 is made: different task, different budget, different turn cap. S1 used
397 300 of 420 000 and finished nothing; S2 used 200 744 of 500 000 and finished. The tasks are not
the same size, and one run of each is not a measurement.

**Statistics note (the §10.6 requirement).** `session.json` reports `main.turns = 14`,
`main.tool_calls` = 24 names. The stored conversation holds **13** assistant messages and the same 24
tool calls, because the final message carried no tool call and the conversation store keeps
tool-bearing turns only; its text survives as `session.main.output`. The counters in this report come
from the stored conversation, cross-checked against `transcript.jsonl` (24 `action_proposed`, 24
`tool_observation`, equal to the conversation), and the one-turn delta is recorded with its reason in
`main_trace.json` rather than smoothed away. `s2_analysis.py` was revised after the run to record that
delta and to accept a collected evidence directory as input; the harness digest in `freeze.json` is
therefore the pre-revision digest, and the revision is analysis-only — no runtime file and no evidence
file was rewritten except `main_trace.json` and the summary's counters, both regenerated from the
stored artefacts (`stage_s2_summary.json.analysis_revision` records it).

## M. Correctness and oracle

| Observation | Value |
|---|---|
| Public suite | PASS, 62 tests, two independent runs (calls 2 of 4) |
| Independent oracle | **PASS** (`HIDDEN_OK`) |
| Files changed | 2, both authorized |
| Acceptance verdict | `PASS` |
| Run status | `succeeded` |

The oracle's own checks include the case the public suite never states: a dependency must delay work
even when capacity is free. The Main's implementation satisfies it, which is why the pass is not a
tautology.

## N. Safety and sandbox

| Item | Value |
|---|---|
| Sandbox | real OpenSandbox, `atlas-uat-python:3.12`, **created and destroyed** |
| Network | `none`, empirical egress probe **PASS** |
| Host separation | `host_only_visible=false`, `sandbox_only_visible_on_host=false`, read-back PASS |
| Ownership | `enforce`: 24 calls checked, 23 allowed, 1 denied (`OWNERSHIP_UNCLASSIFIABLE_SHELL`) |
| Execution integrity | `record`, payloads written, 0 write failures |
| Ledger | run-scoped SQLite, no bypass |
| Budget | guarded, no overrun, no rejections |
| Patch scope | 2 files, both inside `workgrid/scheduler/` |

## O. Final verdict

Two orthogonal labels, applied to observed facts:

* **`GOAL_PASS`** — public suite PASS and independent oracle PASS, with a two-file patch inside the
  authorized scope.
* **`DIRECT_EXECUTION`** — no delegation was proposed; the Router was never called.

`EARLY_EFFECTIVE_DELEGATION` does not apply, and neither does `INVALID_CAPABILITY_DELEGATION`: the
Main did not attempt a delegation at all. The absence of a capability error is a behaviour, not a
non-observation.

### The six questions

**1. Does v2.1 look closer to the design goals than S1 did?** On the two dimensions this stage could
observe — implementation discipline and capability awareness — yes, qualitatively: it wrote at turn 9
instead of never, ran the public suite twice instead of zero times, and adapted to one shell refusal
instead of six. On the delegation dimension the comparison is empty: S1 proposed a late, ill-matched
delegation; S2 proposed none. Different tasks, one run each, no causal claim.

**2. Did the Main decide at a reasonable stage?** Yes. It decided at turn 8, after fourteen targeted
reads, and named the reason ("small, tightly coupled"). It did not read the whole repository first,
which is what S1 did for sixteen turns.

**3. Was a delegated task capability-matched?** No delegation, so nothing to match. What can be said
is that it did not try to delegate the one thing it could not do itself.

**4. Did it reach implementation and testing earlier?** Yes: first edit at turn 9 of 13, first
public-test run at turn 11, second at turn 13, both passing, with 60 % of the budget unspent.

**5. Was the user goal completed?** Yes, verified by the Host oracle, not by the agent's own claim.

**6. What still needs a formal benchmark?** Everything comparative. Whether the three new sections
cause the observed discipline or the fixture was simply tractable; whether the checkpoint increases
*proposal* quality when delegation is the better choice (this run never exercised it); whether a Main
under v1 on this same task behaves differently; whether Direct is chosen systematically or by
accident; and any cost or latency claim. A single Direct pass cannot answer any of those.

## P. Readiness for Stage B

Better than after S1, and still not a benchmark. What this stage establishes as usable:

1. **A fixture that discriminates**: 47 compatibility tests pass, 15 acceptance tests fail, five
   plausible wrong implementations are rejected by a hidden oracle that two of them would have
   escaped.
2. **A prompt seam with evidence**: three prompt identities, byte-exact injection, child isolation,
   and no runtime change.
3. **A trustworthy analysis layer**: counters from artefacts, with the one known summary defect
   explained instead of hidden.
4. **A cost envelope that fits**: 40 % of a 500 k budget for a complete, two-file implementation with
   two test runs. A v1 arm on this same fixture and budget is now affordable.

What a formal stage still needs, all decided before the first call: a v1 control arm on this fixture
(scrolling the same goal, budget and turn cap), more than one repetition per arm, a pre-registered
metric set (goal pass, delegation proposal rate and timing, capability match, tokens, cache split),
and an explicit statement that a Direct pass is a valid outcome rather than a failure to demonstrate
delegation — otherwise the protocol rewards the behaviour the prompt is trying to avoid.

## Evidence index

`experiments/stage_s2_prompt_v21/`:

| File | Contents |
|---|---|
| `prompts/main_v21.md` | the frozen candidate |
| `s2_contract.py`, `s2_fixture.py`, `s2_offline.py`, `s2_analysis.py`, `run_stage_s2.py` | contract, fixture, offline suites, behavioural trace, runner |
| `reference/` | engine and report reference implementations, and the hidden program |
| `freeze.json`, `offline.json` | frozen identity and the offline report |
| `evidence/run-1/` | `session.json`, `preflight.json`, `offline.json`, `offline-preflight.json`, `attempt-1.json`, `transcript.jsonl` (82 events), `tool_conversations.json`, `stage_s2_summary.json`, `main_trace.json`, `workspace.patch`, `PRIVATE_PAYLOAD_NOTICE.txt` |

There is no `workers.json` and no `handoffs/` directory in the bundle: no child ran, so the runtime
never created them. Their absence is the observation, not a collection failure.
