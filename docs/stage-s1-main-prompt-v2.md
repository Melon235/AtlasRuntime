# Stage S1 — Main System Prompt v2 Real-Model Pilot

Branch `exp/main-prompt-v2-pilot`, based on `exp/stage-b0-controlled-live` at `cb2e8a7`.
One live DeepSeek run, executed once. The runtime change is one opt-in prompt seam.

**Decision: `S1_V2_DELEGATION_PARTIAL`.**

The Main read both subsystem layers itself, then delegated **one** narrow environment probe at turn
17 of 18. The Router admitted it, a real child ran, and the child failed at its turn limit without
producing any of the facts it was asked for. The Main then exhausted the Run Budget on its next
request, never wrote a line of code, and stopped with `budget_exceeded`. The independent oracle
failed. No parallel delegation was observed: `max_observed_concurrency = 1`.

This is one pilot. It is **not** a measurement that v2 is worse or better than v1, and it is not a
benchmark. What it does show is concrete, and §M states the causes separately.

---

## A. Git provenance

| Item | Value |
|---|---|
| Baseline | `exp/stage-b0-controlled-live` @ `cb2e8a74a06021829d24a05b4f90bd1ce68c168e`, worktree clean, `rev-list --left-right --count origin/…...cb2e8a7` = `0 0` |
| Branch | `exp/main-prompt-v2-pilot` |
| Seam commit | `f7518d6` — the prompt becomes a run option |
| Fixture + harness | `7910419` — releaseguard fixture, contract, offline suites, analysis, runner |
| Harness fixes | `81b9274` (allocation-only provisioner), `4a8c301` (content-based reuse check, frozen offline report) |
| Freeze written at | `4a8c301` (recorded inside the freeze; ancestry is enforced, equality is not used) |
| Freeze commit | `4963b22` |
| Run id | `stage-s1-20261119T094539Z` |
| Fixture commit (in sandbox) | `9db0cac4f35a9f170f8c950c9b0eb526cb0c9a51` |
| Attempt marker | `<scratch>/attempts/attempt-1.json`, claimed `2026-11-19T09:45:40.289025+00:00Z`, copied into the evidence bundle |
| Evidence | `experiments/stage_s1_prompt_v2/evidence/run-1/` |
| Live attempts | 1 — no retry path exists, and none was used |

Remote sync caveat, stated rather than glossed: at baseline verification `git fetch origin` failed
with `Empty reply from server` (this environment reaches GitHub only through a proxy). The `0 0`
above is against the already-fetched `origin/exp/stage-b0-controlled-live` ref, not against a fresh
fetch. The push at the end of this stage is the first real network confirmation.

**Runtime delta.** `git diff cb2e8a7 -- src/` names exactly one file:
`src/atlasos/app/main_driven.py`. Router, `DelegationService`, `delegate_tasks`, HandoffStore, the
worker factory, the worker renderer and the routing policy are byte-identical to stage B0; the
preflight check `router_workerfactory_handoff_unmodified` asserts it, and `MAIN_SYSTEM_PROMPT` still
hashes to `sha256:7e6fd829…`.

## B. Prompt v1 / v2 identity and injection evidence

The seam is `GoalRunOptions.main_prompt: MainPromptSpec | None`. `MainPromptSpec` is a frozen
`(version, text)` pair with a `digest`; `None` resolves to `MAIN_PROMPT_V1`, which is the shipped
constant itself. Nothing global is reassigned, so two runs cannot disagree about which prompt they
are using, and the ablation that does not ask for a prompt reads nothing.

| | v1 (default) | v2 (selected) |
|---|---|---|
| Version label | `main-v1.0` | `main-v2.0-rc1` |
| Text digest | `sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a` | `sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323` |
| Characters | 1 388 | 7 095 |
| Source | `MAIN_SYSTEM_PROMPT`, unchanged since stage P0.1 | `experiments/stage_s1_prompt_v2/prompts/main_v2.md`, 7 099 bytes, LF |
| Recorded in `session.json` | `{"version": "main-v1.0", "digest": "sha256:7e6fd829…", "source": "default"}` | `{"version": "main-v2.0-rc1", "digest": "sha256:2bb50b09…", "chars": 7095, "source": "override"}` |

The digest is taken over the prompt **text**, not the file bytes: `AgentConfig.system_prompt` is a
string, and a digest that moves with a working tree's line-ending convention is not an identity. On
this checkout the two coincide (the file is stored with LF), and the file digest is recorded
separately as provenance.

Injection evidence (`offline.json`, suite F2, scripted provider, no model call):

* default run — every provider request's system prompt equals `MAIN_SYSTEM_PROMPT` exactly;
* override run — every provider request's system prompt equals the frozen file text exactly, and the
  session records `source=override` with the frozen digest;
* the goal text is absent from both prompts, and neither contains a controlled-experiment phrase;
* the child's system prompt is byte-identical under v1 and v2
  (`sha256:aaa9d972ede571be47c4e28bcf80f7cbcfb64ba4056f18f0a373c5fb384b9ef1`, starting
  `TRUSTED WORKER PROFILE`), and contains neither prompt;
* `delegate_tasks`, `delegate.read` and `run_public_tests` are on the Main's surface in both runs;
  the Router decision is `m.1.0` in both.

6 offline tests in `tests/integration/test_stage_s1_main_prompt.py` cover the same properties at the
unit level, including that a second run with no override still sends the historical bytes, and that
the override does not survive the run that selected it.

## C. The new fixture: ReleaseGuard

`experiments/fixtures/stage_s1_releaseguard` — 27 provisioned files: 19 Python modules, `README.md`,
`pytest.ini`, one sample manifest, one license file and 4 sample artifacts, about 2 900 lines of text.
Nothing is reused from stage P1/P1b/B0: the goal, the file set and the vocabulary are new, and the
preflight check `fixture_not_reused` compares file *content* against the P1b manifest (0 identical
files) and scans the goal and the paths for earlier task vocabulary (0 hits).

Two independent subsystem packages and one shared layer:

| Layer | Files | What it owns |
|---|---|---|
| `releaseguard/metadata/` | `manifest_parser`, `version_rules`, `license_rules`, `errors` | manifest parsing with documented defaults, a PEP-440 subset grammar, channel rules, SPDX normalization, an allow list |
| `releaseguard/artifacts/` | `scanner`, `checksums`, `path_rules`, `errors` | a deterministic inventory walk, SHA-256 normalization, the single definition of a safe artifact path |
| `releaseguard/audit/` | `contracts`, `report`, `service`, `errors` | the shared workflow: 8 fixed check ids, a policy, report assembly, rendering |
| `releaseguard/tests/` | `test_metadata`, `test_artifacts`, `test_public_audit` | 68 compatibility tests that pass today, 21 acceptance tests that do not |

The two layers are *independently* investigable and mutually independent by construction: neither
imports the other, and neither imports `audit`. They also fail differently — metadata returns typed
findings, artifacts raise typed errors — so the audit layer has to handle both, which is the
integration work.

`releaseguard/audit/service.py` and `releaseguard/audit/report.py` are stubs whose docstrings state
the contract; `contracts.py` and `errors.py` are complete and are the report vocabulary the oracle
asserts. The Main may write inside `releaseguard/audit/` and nowhere else; `metadata/`, `artifacts/`
and `tests/` are refused by the ownership policy, not by a prompt.

## D. Offline validation (F1, F2, F3) — no model called

`--offline` writes `offline.json`; the same three suites are re-executed by every admission run.

**F1 — solvability.** In a scratch copy of the frozen fixture:

| Case | Result |
|---|---|
| Baseline: compatibility suites | **pass** (68 tests), exit 0 |
| Baseline: `tests/test_public_audit.py` | **fail**, 21 failures |
| Baseline: oracle | **rejects** (the suite fails before the hidden program runs) |
| Reference implementation of the two stubs | **pass** (89 tests) and oracle **`HIDDEN_OK`** |
| 5 wrong variants | all **rejected** by the oracle |

The wrong variants are the interesting part, because they are plausible: checks sorted
alphabetically instead of the documented order; warnings failing the release; a missing artifact
never reported; extras never fatal; and a broken manifest reported as passes instead of skips. Each
is a one-line edit to the reference and each is refused.

The reference implementation lives in `experiments/stage_s1_prompt_v2/reference/` and is never
copied into the provisioned fixture (`reference_files_inside_fixture = []`); no oracle marker appears
in any fixture file (`oracle_markers_leaked_into_fixture = []`).

**F2 — prompt injection.** See §B. A scripted provider proves the interface and the bytes. It cannot
prove that a real model decides to delegate, and is not used to argue that.

**F3 — router admission**, offline, against the frozen `m.1.0` router:

| Shape | Result |
|---|---|
| Two independent read-only research tasks with background | **admitted**, `delegate_parallel`, wave width 2 |
| Two tasks writing the same file | refused, `SHARED_WRITE_RISK` |
| A research task with no background | refused (`TRIVIAL_DIRECT_TASK` — the frozen router reaches a cheaper refusal first) |
| A second task declaring `depends_on` the first | refused, `DEPENDENCY_NOT_SUPPORTED` |

F3 says the fixture's structure *can* be delegated. It says nothing about what a real Main will do.

## E. Freeze and preflight

`--freeze` records the runtime source tree, the harness, the fixture manifest, both prompt digests,
the offline report digest and the git provenance:

| Frozen item | Value |
|---|---|
| Runtime source | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae` over 130 files |
| Prompt v2 | `sha256:2bb50b09…` (text), `sha256:2bb50b09…` (file), 7 095 chars |
| Prompt v1 | `sha256:7e6fd829…`, 1 388 chars |
| Fixture | `sha256:2b8edeeb…` over 27 files |
| Goal | `sha256:60cf5c02…` |
| Offline report | `sha256:b739fe2f…` |
| Write scope | `("releaseguard/audit/",)` |
| Budget / turns | 420 000 tokens, Main 24 turns, 1 delegation round, 2 children, 6 child turns |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB, 30 min |
| Tool error mode | `corrected` |
| Live attempts | 1 |

`--preflight` → **READY**: 19 runtime checks + 24 stage checks, 0 blocking failures. Every blocking
check in §G1 of the stage specification is present, including: prompt v2 digest equal to the frozen
one; v2 actually selected and its text equal to the file; no controlled delegation instruction in
either the goal or the prompt; the fixture new and solvable; the oracle invisible; the delegation
machinery unmodified; the source digest matching; `tool_error_mode=corrected`; no prior attempt
marker; the scratch directory outside the worktree.

Two admission defects were found by the first preflight and fixed before any model call, both
recorded in `4a8c301`: fixture reuse was being decided by shared *file names* (a README.md in both
fixtures read as reuse), and every preflight rewrote the repository's `offline.json`, so the digest
the freeze recorded could never match afterwards.

## F. Real Main execution

One run, 18 model requests, 36 tool calls, 63.97 s wall clock, `budget_exceeded` on request 19.

| Turn | Tool calls | Result |
|---|---|---|
| 1 | `bash` (`find . -type f … | grep -v .git`), `list_files` | ok — "I'll start by exploring the workspace to understand the project structure." |
| 2 | `bash` (`cat README.md`, `cat pytest.ini`), `bash` (`for f in releaseguard/audit/*.py; do cat`) | **denied** ×2, `OWNERSHIP_UNCLASSIFIABLE_SHELL` |
| 3 | `file_read` README, `pytest.ini` | ok — "Shell access is restricted. I'll use the file tools instead." |
| 4 | `file_read` × 5 | ok — audit `contracts`, `report`, `service`, `errors`, `__init__` |
| 5 | `file_read` × 2 | ok — `tests/test_public_audit.py`, `metadata/manifest_parser.py` |
| 6 | `file_read` × 3 | ok — `version_rules`, `license_rules`, metadata `errors` |
| 7 | `file_read` × 5 | ok — artifacts `scanner`, `path_rules`, `checksums`, `errors`, `__init__` |
| 8 | `file_read` × 2, `list_files` | ok — sample manifest, metadata `__init__`, `samples/` |
| 9 | `list_files`, `bash` (`ls -la samples/dist`) | ok |
| 10 | `bash` (`ls … ; wc … ; sha256sum …`) | **denied** |
| 11 | `bash` (`cd /workspace; ls -la samples`) | executed, exit 2: `ls: cannot access 'samples'` |
| 12 | `bash` (`pwd && ls -la`) | ok — `/workspace/project` |
| 13 | `bash` (`cd /workspace/project && … && sha256sum …`) | **denied** |
| 14 | `bash` (`ls -la samples`) | ok |
| 15 | `bash` (`wc -c …`) / `bash` (`sha256sum …`) | ok / **denied** |
| 16 | `bash` (`python3 -c "print('ok')"`) | **denied** |
| 17 | **`delegate_tasks`** | admitted — "Shell is limited to simple commands. Let me delegate a read-only environment probe while I write the implementation." |
| 18 | `dispatch_action` | `tool_not_disclosed: 'bash' has not been disclosed by tool.search` |
| — | — | request 19 refused: `budget_exhausted`, estimate 40 508, 22 700 remaining |

The Main's own account of its plan is in the conversation; nothing here is inferred from a private
reasoning channel. Six denials in total, all shell; one executed command failed for the wrong working
directory.

## G. Early delegation decision

| Question | Observation |
|---|---|
| Turn of first `delegate_tasks` | **17** of 18 |
| Tool calls before it | **34** = 19 `file_read` + 12 `bash` + 3 `list_files` |
| Distinct read targets before it | **22**, including every module of both subsystems, the sample manifest, the sample directory and the acceptance suite |
| Main context at that request | **29 045 tokens** (request 17 of 18) |
| Delegated before deep investigation? | **No.** The pattern is the opposite: the Main read the whole repository first and delegated only what it could not do itself |

The trigger was a *capability* limit, not a structural one. Six `bash` calls were refused as
`OWNERSHIP_UNCLASSIFIABLE_SHELL` (programs `cat`, `for`, `sha256sum`, `python3`, and compound
commands), and the Main's own words say so: at turn 3, after the second denial, "Shell access is
restricted. I'll use the file tools instead"; at turn 17, "Shell is limited to simple commands. Let
me delegate a read-only environment probe while I write the implementation." It fell back to file
tools, then delegated the fact-gathering it still could not perform. The two-subsystem structure
that §D shows is delegable was not what it responded to.

v2's `EARLY DELEGATION DECISION` section says to evaluate delegation *before* investigating every
module. On this task it did not: the decision came after 16 turns of investigation and 22 read
targets. That is the single clearest prompt-behaviour finding of the pilot, and it is a finding about
*this* prompt on *this* task, not a measurement.

## H. Router, worker and handoff evidence

One batch, one round:

```json
{"route": "delegate_one", "reason_codes": ["DELEGATION_ADMITTED"], "blocked_gates": [],
 "policy_version": "m.1.0", "max_wave_width": 1, "max_observed_concurrency": 1}
```

The proposal (`reason`: "Need environment facts (sample file digests, Python version) that shell
probing cannot provide safely from Main") was one task:

* key `probe-samples`, profile `research_worker`, `write_paths: []`, no `depends_on`;
* background: the three layers, the fact that the audit stubs raise `NotImplementedError`, exactly
  what the Main is writing, and why Main cannot get the facts itself;
* objective: five numbered deliverables (interpreter version, per-file sizes and SHA-256, whether the
  manifest digests match, whether `releaseguard.audit` imports, and the two test-suite outcomes).

Objective and background are specific and self-contained by the standard the prompt itself sets. The
weakness is scope: it is one probe, not two independent investigations, so the batch could never be
parallel, and `max_observed_concurrency` is 1. Whether it *should* have been two tasks is a judgement
about the task, and the fixture's structure supports it (§D, F3), but the run shows the model chose
otherwise.

The child (`stage-s1-…-main:r1:probe-samples`) ran alone for 8.67 s, 6 turns, 12 tool calls, and
**failed with `stop_reason=max_turns`**. Its whole output is one sentence:

> No shell/execution tool is available in the disclosed catalog. I'll gather what I can through
> read-only file inspection, and clearly flag the parts that require shell execution.

Its conversation shows why: `research_worker` has `tool_search`, `dispatch_action`, `file_read`,
`grep`, `list_files` — and no shell. It spent all six turns looking for one: six `tool_search` calls,
every one of which returned `{"matches": []}`, then three `dispatch_action` calls with tool ids
`shell`, `run_command` and `bash`, every one of which was refused with `tool_not_disclosed`. It listed
the tree once and never read a file. Its own words at the end: "The tool catalog search isn't
returning anything."

Handoff: 1 119 characters returned to the Main (bounded projection, `ref_id
ho-6100eb7b35de38b6a962469c`, full text preserved in `evidence/run-1/handoffs/`). The projection
carried `status: failed`, the child's one-sentence summary, its token counts and its ref.

## I. Handoff utilisation and duplicate investigation

| Question | Observation |
|---|---|
| Tool calls after the handoff | **1** (`dispatch_action`), then the budget refused the next request |
| Re-reads of work a child had done | **none** — there was nothing to re-read: the child read no files |
| `delegate.read` calls | **0** (`read_calls=0, read_success=0, read_denied=0`) |
| Did the Main consume the summary? | It saw the bounded result, including `status: failed` and the child's sentence. It did not page the ref |
| Duplicate investigation | **cannot be assessed on this run** — the Main never inspected anything after the handoff, so the B0 failure mode had no opportunity to appear |

This is a null result, not a good one. The B0 problem this stage was designed to look for — a Main
re-reading everything a worker had already read — did not occur because the run ended one tool call
after the handoff, not because the prompt prevented it. Any claim that v2 fixed the duplication
problem would be unsupported by this run.

## J. Context and cost

Provider-reported, per §5 of `AGENTS.md`; nothing is estimated.

| Metric | Main | Child(ren) | System |
|---|---|---|---|
| Requests | 18 | 6 | 24 |
| Peak context (one request) | 30 005 | — | — |
| Cumulative input (miss + hit) | 380 202 | 14 203 | 394 405 |
| Cache-hit prompt tokens | 350 592 | 11 264 | 361 856 |
| Cache-miss prompt tokens | 29 610 | 2 939 | 32 549 |
| Completion tokens | 2 267 | 628 | 2 895 |
| **Total (prompt + completion)** | 382 469 | 14 831 | **397 300** |
| Cache-hit ratio (hit / cumulative input) | 0.922 | 0.793 | 0.918 |

Main context per request: 3 708 → 4 282 → 4 475 → 5 316 → 10 206 → 17 352 → 22 209 → 26 867 →
27 737 → 28 034 → 28 124 → 28 199 → 28 439 → 28 537 → 28 721 → 28 946 → 29 045 → 30 005.

Handoff payload: 1 119 characters. Budget rejections: 1. Wall clock: 63.97 s. The 420 000 budget was
consumed by 18 requests whose average prompt was 21 122 tokens; the marginal request at the end cost
~30 k, and the guard's estimate for request 19 was 40 508 against 22 700 remaining.

Without a v1 control run on the same task, no statement about v2 reducing or increasing cost is
defensible, and none is made here.

## K. Public tests and the independent oracle

| Observation | Value |
|---|---|
| Files the Main changed | **0** (`workspace.patch` is empty) |
| `run_public_tests` calls | **0** of 4 |
| Static verification | PASS (both stub files present, both parse) |
| Dynamic oracle | **FAIL** — `audit_release` still raises `NotImplementedError` |
| Verdict | `FAIL` |
| Run status | `failed`, `stop_reason=budget_exceeded` |

The Main never reached an edit, so the acceptance suite never had a candidate to judge. The oracle
behaved exactly as designed: it ran after the Main stopped, in the sandbox, and its `HIDDEN_FAIL`
output was not visible to the agent at any point.

## L. Budget, latency, safety, sandbox

| Item | Value |
|---|---|
| Budget mode | `guarded`, limit 420 000, 1 rejection, 0 overrun, 24 requests completed |
| Budget estimate error | 34 319 tokens of cumulative absolute error over 24 samples (≈1 430 per request), learned ratio 1.1493 |
| Ownership | `enforce`: 44 calls checked, 38 allowed, **6 denied** — all `OWNERSHIP_UNCLASSIFIABLE_SHELL` |
| Denied commands | `cat README.md; cat pytest.ini`, `for f in releaseguard/audit/*.py; do cat`, `wc … ; sha256sum …` (×3), `python3 -c "print('ok')"` |
| Sandbox | real OpenSandbox, `atlas-uat-python:3.12`, created, `network_probe=PASS`, host separation PASS, 1 check run, **destroyed** |
| Execution integrity | `record`, 48 payloads, 0 write failures, evidence-only authority |
| Ledger / idempotency | run-scoped SQLite under the run directory; no denials bypassed |

The six denials are the mechanical cause of seven unusable requests, and they are a *correct* policy
outcome: the goal path refuses interpreter invocations by design, and `run_public_tests` exists
precisely so a Main does not need them. The Main never called `run_public_tests`, even though it was
on its tool surface, and kept probing with shell instead.

## M. Failure analysis

Three causes, independent, all observable:

1. **The Main planned a read-everything-first sequence and ran out of budget before writing.**
   Turns 1–16 were investigation (19 file reads, 12 shell attempts, 4 directory listings, no edit),
   the single delegation came at turn 17, and request 19 was refused. With ~30 k context per request
   there is no 24-turn budget in 420 000 tokens: the run's own per-request growth made completion
   impossible regardless of what the delegation returned.
2. **Six of the twelve `bash` calls were denied as unclassifiable shell**, and seven of the eighteen
   requests produced nothing usable (six denials, plus one listing in the wrong directory). The Main
   adapted — it switched to file tools at turn 3 and said so — but never used the tool that exists
   for exactly this purpose, `run_public_tests`.
3. **The delegated child could not do the delegated job.** `research_worker` has no execution tool,
   `tool_search` disclosed nothing from the sandbox catalog, and `dispatch_action` refused three
   tool ids. Six turns produced one status sentence. The Main asked a read-only research worker to
   run `python3`, `sha256sum` and `pytest`.

Not a runtime fault: the sandbox was real and destroyed, the router admitted a legal plan, policy,
ownership and budget all behaved as specified, the child's failure was isolated from the Main's loop,
and every artefact needed to explain the run was written before anything was summarised.

Two evidence-quality defects worth naming:

* `session.json`'s `main.turns` is **0** and `main.tool_calls` is **[]**, while the stored
  conversation holds 18 assistant turns and 36 tool calls. AnyCode returns a result with an empty
  message list when the loop ends on a budget rejection, and the session summariser counts that list.
  This stage's analysis reads the conversation instead; the field is a known reporting defect, not a
  fact about the run.
* `session.json`'s `verification.dynamic.stdout` contains the oracle program text, because the
  sandbox provider echoes the command it ran. That is post-run evidence; the Main had no tool for it
  and the oracle executes only after the Main's loop ends.

## N. Final S1 decision

**`S1_V2_DELEGATION_PARTIAL`.**

The classification rule, applied to observed facts (`verdict_inputs` in
`evidence/run-1/stage_s1_summary.json`): delegation was proposed (`true`), admitted (`true`), a child
executed but failed (0 successes), the oracle failed (`FAIL`), the public suite never ran, and the
run ended by budget, not by a runtime or safety fault.

Not `AUTONOMOUS_DELEGATION_PASS`: no integration, no public PASS, no oracle PASS.
Not `DIRECT_PASS`/`DIRECT_FAIL`: the Main did delegate, unprompted, and the Router admitted it.
Not `RUNTIME_FAILED`: nothing in the runtime, the sandbox or the policy behaved incorrectly.

Concurrency is explicitly **1**. This run is evidence that a real Main, under v2 and with no
instruction to delegate, will propose a delegation and have it admitted. It is **not** evidence of a
parallel delegation strategy, and it is not comparable to B0, where the Host's instruction forced two
concurrent children.

### The four questions

**1. Did the new prompt make the Main consider delegation at a sensible stage?**
No. It considered it at turn 17 of 18, after reading all 22 relevant files, and the trigger was a
tool denial rather than the two-subsystem structure. The prompt's own instruction — evaluate
delegation before extensively investigating every module — was not followed on this task.

**2. If it delegated, was the decomposition reasonable?**
Partly. The proposal is well-formed and satisfies the contract in spirit: one bounded, read-only,
self-contained task with concrete deliverables and useful background, correctly routed, no fabricated
dependencies. But it is a single probe of *environment facts*, not the two independent subsystem
investigations the repository actually offers, and it asks a read-only worker to execute commands it
cannot run. Reasonable in form; poor in target.

**3. Did the Main actually use the handoff?**
It read the bounded result and made one further call, then the run ended; it never paged the
reference and never had to integrate anything. There is no duplicate investigation to report, and
therefore no evidence either way about the B0 re-reading problem.

**4. Was a correct, verifiable coding task completed?**
No. Zero files changed, the public suite was never run, and the independent oracle failed.

No prompt change was made after seeing this result, and the run was not repeated.

## O. Readiness for a formal v1-vs-v2 benchmark

Not ready, for specific and fixable reasons. Each of these is a prerequisite, not a preference:

1. **Budget realism.** 420 000 tokens cannot fund 24 Main turns at ~30 k context per request. Either
   the turn cap or the budget has to describe a reachable trajectory, and the choice must be made
   *before* the run from an offline estimate. The fixture's own reference implementation is 324 lines
   across the two stubs, plus the reading it needs; 16 investigation turns left no room for it.
2. **Shell-vs-tool ergonomics.** Six denied shell calls and eight wasted turns say the Main does not
   know `run_public_tests` is the sanctioned path and keeps reaching for an interpreter. That is a
   prompt/tool-description question, and it must be settled before v1 and v2 are compared, because
   it dominates the cost of both arms.
3. **Child capability vs delegated objective.** `research_worker` cannot execute anything, and
   `tool_search` disclosed nothing in the sandbox catalog. A Main that delegates an execution probe
   to a read-only worker will keep producing `max_turns` failures. Either the delegation contract
   states the worker's limits more sharply, or a profile that can run the approved suite is needed.
   This is a runtime/description decision, not a prompt-only one.
4. **A control arm.** One v2 sample with no v1 run on the same task, the same budget and the same
   fixture supports no comparison at all. A formal stage needs both arms, more than one sample per
   arm, and a pre-registered budget and turn limit — the closure-2 machinery already has the shape
   for that.
5. **Reporting.** `session.json`'s `main.turns`/`tool_calls` are wrong whenever a run ends on a budget
   rejection, and an analysis that trusted them would have reported a Main that did nothing. Fix the
   summariser before it feeds a benchmark table.

What the pilot *did* establish, and can be relied on: the prompt-selection seam is correct and
invisible when unused; the frozen v2 text reaches `AgentConfig` verbatim and never reaches a worker;
a real DeepSeek Main will delegate unprompted under v2 when its own tools fail; the Router admits
such a proposal; and a new fixture with an independent oracle, a reference solution and five rejected
wrong implementations can be produced and frozen cheaply enough to run repeatedly.

## Evidence index

`experiments/stage_s1_prompt_v2/`:

| File | Contents |
|---|---|
| `prompts/main_v2.md` | the frozen candidate prompt |
| `s1_contract.py` | goal, prompt identity, limits, public suite, hidden oracle, acceptance |
| `s1_fixture.py`, `s1_offline.py`, `s1_analysis.py`, `run_stage_s1.py` | fixture, offline suites, behavioural trace, runner |
| `reference/` | the reference implementation of the two stubs (never provisioned) |
| `freeze.json`, `offline.json` | frozen identity and the offline report |
| `evidence/run-1/` | `session.json`, `preflight.json`, `offline.json`, `offline-preflight.json`, `attempt-1.json`, `transcript.jsonl` (149 events), `tool_conversations.json`, `stage_s1_summary.json`, `main_trace.json`, `workers.json`, `workspace.patch` (empty), `handoffs/`, `PRIVATE_PAYLOAD_NOTICE.txt` |
