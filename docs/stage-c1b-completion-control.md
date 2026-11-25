# Stage C1-B — Worker Execution Efficiency and Completion

Status: **`STAGE_C1B_DISPATCH_ENTRY_CORRECTED_READY_FOR_LIVE_AUTHORIZATION`**

Stage C1 ran the coding-delegation positive control once and ended **A2**: the proposal was real,
the router admitted it, two workers ran in parallel, and both died at the 20-turn cap. This stage
audits that run from its own evidence, fixes two under-specified contracts, repairs one failure
classification gap, and prepares one follow-up cell. No model was called, no sandbox was created, no
claim was written or consumed, and no authorization was signed.

### Dispatch entry correction

The first live attempt of the C1-B plan (`5598dce`, signed authority `sha256:921bc382…`) died at the
first statement of `executor.dispatch`:

```
executor.py:1238  plan = contract.scope_plan(plan_id)
contract.py:786   ValueError: unknown scope 'stage-c1b-coding-delegation-completion-v1'
```

`contract.scope_plan()` registered the Stage C1 plan id and scope but not the C1-B ones, and every
preparation check resolved the plan **by object**, so none of them called the function `dispatch`
calls first. The incident is recorded as `PRE_CLAIM_DISPATCH_ENTRY_FAILURE` in
`D:\Work\AtlasRuntime-runs\stage-c1b-completion\PRE_CLAIM_DISPATCH_ENTRY_FAILURE.json`: dispatch
attempted, claim not consumed, 0 cells, 0 model requests, 0 sandboxes, L1–L6 `NOT_RUN`. The launch
lock, both logs, the preflight scratch and the signed authorization were left untouched.

The correction registers the C1-B id and scope in the same seam and adds two checks that would have
caught it: **C13** resolves the plan through `contract.scope_plan()` (and asserts nothing else moved
and unknown ids still fail), and **C14** calls the real `dispatch(PLAN_ID, mode=MODE_OFFLINE_PROOF)`
by id with a synthetic cell, a throwaway claim root and a temporary signed record — plan-id
resolution → authority verification → offline claim → cell → audit → matrix report. The plan digest
did not move; the executor did, to `bplus-executor-1.5`.

The audit tool is `experiments/stage_c1b/trace_audit.py`; it reads the C1 run tree read-only and
writes `experiments/stage_c1b/trace-audit.json`. Every number below comes from it or from the run's
own recorded files.

---

## A. What each agent actually did

Turns are the assistant turns persisted in the run's tool conversation; model requests are the
runtime's count. They differ by one where the final turn produced no tool call and, for the CSV
worker, where the last request produced nothing at all.

**Main — 17 turns / 17 requests**

| turn | label | what it did |
| --- | --- | --- |
| 1 | discovery | `list_files`, one `bash find` |
| 2 | verification (refused) | bash loop with `cd` + `for` → `OWNERSHIP_UNCLASSIFIABLE_SHELL` |
| 3–5 | discovery | 11 `file_read` calls over the frozen interface, both plugins, registry, loader, public tests |
| 6 | **delegation** | `delegate_tasks`, two tasks |
| 7 | discovery | read the four plugin files the children had just written |
| 8 | **productive** | wrote `plugins/registry.py` and `plugins/loader.py` |
| 9 | verification | `run_public_tests` → PASS |
| 10–12, 15, 16 | verification (refused) | `cd /tmp`, two `python3 -B -c` probes, `python3 -B -m pytest` → all refused |
| 13–14 | verification | `pwd`, `python3 --version` → allowed |
| 17 | discovery + verification | `file_read plugins/__init__.py`, `git status --short` |
| 18 | — | no turn: the budget refused the request |

**CSV worker — 19 persisted turns / 20 requests**

| turn | label | what it did |
| --- | --- | --- |
| 1–3 | discovery | `pwd; ls; find`, then 6 `file_read` calls |
| 4 | **productive** | `file_write plugins/csv_fields.py` |
| 5 | **productive** | `file_write plugins/csv_plugin.py` |
| 6–19 | verification | 10 refused interpreter attempts, interleaved with allowed `pwd`/`ls`/`cat`/`git status` |

**JSON worker — 19 persisted turns / 20 requests**

| turn | label | what it did |
| --- | --- | --- |
| 1–4 | discovery | reads plus one allowed `git log`/`python3 --version` call |
| 5 | **productive** | `file_write plugins/json_fields.py` |
| 6 | **productive** | `file_write plugins/json_plugin.py` |
| 7 | verification (refused) | `python3 <<EOF` |
| 8–9 | verification (refused) | `file_write /tmp/json_v2_check.py`, `file_write plugins/_scratch_check.py` |
| 10–14 | verification | 5 refused `python3 -c` / `py_compile` / `pydoc` / `doctest`, then `ls` + `grep` |
| 15–18 | read / **productive** | read the file, `file_edit` it twice (t16, t18) |
| 19 | narration | the 88-character closing sentence — the whole "handoff" |

## B. Where the turns went

| metric | Main | CSV | JSON |
| --- | --- | --- | --- |
| turns | 17 | 19 | 19 |
| productive (≥1 accepted write) | 1 | 2 | 4 |
| discovery (reads only) | 6 | 3 | 6 |
| verification | 11 | 15 | 8 |
| refused calls | 6 | 10 | 8 |
| exactly repeated call signatures | 4 | 0 | 3 |
| interpreter attempts (python/pytest/py_compile…) | 5 turns | 11 turns | 8 turns |
| first write turn | 8 | 4 | 5 |
| last meaningful write turn | 8 | 5 | 18 |
| turns after the last write | 9 | **14** | 1 |
| input tokens after the last write | **116,664** (67% of 172,869) | not recorded per turn | not recorded per turn |

All 24 ownership denials are attributable: Main 6, CSV 11, JSON 7 (including both `file_write`
attempts: `/tmp/json_v2_check.py` and `plugins/_scratch_check.py`). Twenty-three cite
`OWNERSHIP_UNCLASSIFIABLE_SHELL`; one cites `OWNERSHIP_DENIED_UNDECLARED_PATH`. The trajectory also
records one further policy deny outside the ownership adapter (oracle-side), which the session's
ownership counters do not include.

The workers were **not** looping on identical commands: their repeated attempts are the same *class*
(`python3 -c`, `py_compile`, `compileall`) with different payloads, which is why exact-signature
counting understates it and the interpreter-attempt count is the honest metric.

## C. Root cause, as far as the evidence supports it

The evidence contradicts "the task is too large for 20 turns":

* the CSV worker finished both files at turn 5 and then spent **14 of its remaining 15 turns** on
  refused interpreter commands, never producing a report;
* the JSON worker finished at turn 6, made two targeted refinements at 16 and 18, and lost exactly
  one turn to the cap — the turn whose text became the 88-character "handoff";
* the Main integrated at turn 8, passed the public suite at turn 9, and then spent **9 turns and 67%
  of its input tokens** on refused interpreter commands and re-reads before the budget refused
  request 18.

So the two supported causes are (1) a worker contract that never says what to verify with, what a
refusal means, or what a finished worker must report, and (2) a Main discipline that says "use
`run_public_tests`" and "change approach" but never says that interpreter shells are *refused in this
runtime* or that a passing public suite is where verification ends.

What the evidence does **not** support: raising the turn cap as the primary fix. Only the JSON
worker came close to needing more turns, and even it spent 8 of 19 turns on refused commands.

The CSV worker's assignment already told it "running pytest is not authorized for you: do not claim
test results" — it still tried `python3 -m py_compile` and `compileall` eleven times. The
instruction was in the untrusted assignment; the trusted profile said only "report tests run".

## D. Worker completion contract (change 1)

`src/atlasos/workers/catalog.py::CODING_INSTRUCTIONS`, versioned as `c1b-1.0`:

* implement only inside the authorized files; use only the given tools;
* a deterministic refusal is information — do not retry it or an equivalent form;
* verify with reads, call-site inspection and reasoning; when a command is refused by policy,
  record the requirement as unverified and leave project-level test execution to the Main;
* do not create scratch files outside the authorized paths; never claim a test result not obtained;
* finish with a six-field report: completion status, changed files, behaviour implemented,
  interface/integration notes, verification performed, unverified requirements/limitations.

Why this seam: the completion contract belongs to the trusted profile, not to the untrusted
assignment (which is per-task and written by the Main), and the profile already had an
`instructions` field rendered into the system prompt. Nothing else about the profile changed: the
eight tools, the shipped `max_turns = 8` and the scope are byte-identical, and the run's effective
cap still comes from `child_max_turns = 20`.

Blast radius: every future `coding_worker` renders the new prompt (its `rendered_context_hash`
moves), and `tests/unit/test_worker_prompt_layout.py`'s golden prompt was updated with it. Historical
records keep their own hashes; nothing re-derives them.

## E. Main verification and termination discipline (change 2)

`main-v2.2-rc1-c1b` is `main-v2.1-rc1-bplus` with **exactly one section replaced**
(`## TESTING AND RECOVERY`). Everything before and after it is byte-identical, and the plan checks
that. The new section states:

* `run_public_tests` is the only authorized way to execute the public suite;
* interpreter invocations (`python -c`, `python -m pytest`, heredocs, compile loops) are refused in
  this runtime, a refusal is not transient, and equivalent forms must not be retried;
* a failed public test is diagnostic feedback; a passing one is necessary but not sufficient, and the
  boundary conditions the task names must be reviewed with reads/grep/diffs;
* turns must be reserved for integration, verification and delivery; when no further verification is
  possible, report the limits and finish.

Not touched: identity, early planning checkpoint, delegation policy, context/cost guidance, workspace
safety, final delivery. The stage does not change how the Main decides to delegate.

## F. Failure classification (change 3)

C1's session recorded `main.stop_reason = "budget_exceeded"` and the budget's own rejection text, yet
`session.failure` was `null`, so the audit's `failure_is_measurable` check found `kind=MISSING` and
the executor stopped the run with `UNKNOWN_FAILURE_KIND`.

Root cause: `experiments/stage_b_plus/executor.py` reads `session["failure"]["kind"]`, but
`run_goal` emitted a bare string or nothing at all — the producer never wrote a kind, so **every**
failed Main run would have stopped a dispatch, not just this one.

Fix (minimal, in the producer):

* `run_goal` now emits a structured record:
  `{kind, classified_from, message, main_stop_reason, main_stop_message, budget_stop_reason,
  acceptance_verdict}`, keeping the previous text in `message`;
* `classify_failure` names only three kinds, each of which the runtime recorded as a stop:
  `budget_exhaustion` (stop `budget_exceeded`, message taken from the budget's own rejection),
  `main_max_turns` (stop `max_turns`), `acceptance_failed` (verdict `FAIL`);
* everything else — a raised exception, a provider failure, an unrecognised stop such as the
  doom-loop guard — stays `kind: null` with `classified_from: "unclassified"`, so the audit still
  fails closed.

The C1 evidence is untouched: its `STOPPED` verdict remains what the run earned. Nothing was
re-classified after the fact, and the C1 cell is not retroactively a valid formal sample.

## G. Budget: no change

Kept: Main 20 turns, worker/child 20 turns, shared 500,000 tokens, token stop 500,000, cost stops
0.15 / 0.15, one attempt.

Evidence for keeping them: the token stop was reached, but the turns that consumed it were refused
verification and re-reads — 14 of 19 CSV turns after its last write, 9 Main turns and 116,664 input
tokens after its only write. A larger ceiling would have bought more refused attempts. The remedy
that fits the evidence is the contract fix, not more budget.

Not implemented, recorded only as future design candidates: layered budgets, a Main completion
reserve, dynamic quota. None of these exists in this stage.

## H. The follow-up plan

`stage-c1b-coding-delegation-completion-v1`, scope `C1B_COMPLETION_CONTROL_ONLY`, one cell, one
attempt, `is_benchmark_sample: false`.

| item | value |
| --- | --- |
| cell | `c1b.closure2-plugin-migration.C3.r1` |
| task / arm | `closure2-plugin-migration` / `C3` (coding_worker, exposure `card`) |
| goal | byte-identical to Stage C1 (`sha256:1496055d…`), appendix included |
| Main prompt | `main-v2.2-rc1-c1b`, `sha256:4f742d08c6ed04c6ecd07ac446825e17bc8cdf78ec04e956845d64c3092093af` |
| worker contract | `c1b-1.0`, instructions `sha256:0776e1c6…`, rendered prompt `sha256:5b0fa74e…` (1,958 chars) |
| plan digest | `sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c` |
| effective grant | `plugins/` (raw `plugins/**`, normalised by the repository's own function) |
| budget digest | `sha256:a6ea4367…` (unchanged) |
| protocol | `bplus-2.2.0` / `sha256:65e0b993…` (unchanged) |
| execution contract | `bplus-exec-3.1` / `sha256:242fcef5…` |
| executor | `bplus-executor-1.5` / `sha256:145f6c83822c3d38e21f86d4999057250518b2587d273aaa5451b6f5b2bccba4` |
| candidate envelope | token stop 500,000; cost stops 0.15 / 0.15 (still unapproved) |

The executor identity also covers `src/atlasos/workers/catalog.py` and
`src/atlasos/app/main_driven.py`, because the worker contract and the failure classifier are part of
what a cell executes; 1.5 adds the `contract.scope_plan` registration that the first attempt needed.
The plan digest did not move: the plan record is the same object it was before the correction.

**This is a combined intervention.** One run on this plan exercises the worker completion contract
*and* the Main verification discipline at once; it cannot be read as a single-variable ablation of
either. The plan records `intervention.kind = "combined_mechanism_fix"` and
`single_variable_ablation: false`.

## I. Targeted verification

`python -m experiments.stage_c1b.checks` → **14/14 pass**, status
`STAGE_C1B_DISPATCH_ENTRY_CORRECTED_READY_FOR_LIVE_AUTHORIZATION`:

| check | what it proves |
| --- | --- |
| C01 | the coding profile renders the completion contract, and the plan records the exact bytes |
| C02 | eight tools, profile cap 8 → effective 20, child grant `plugins/`, Main scope `plugins/**`, 20 turns, 500,000 tokens, two children per call — unchanged |
| C03 | the frozen router still admits the P2 pair as a two-wide wave; the raw glob is still refused; all five boundary codes unchanged |
| C04 | on the C1 evidence: 2 failed children, 1 addressable ref whose payload is 88 characters, 0 `delegate.read` calls, 6 changed files consumed from the shared workspace |
| C05 | the production options carry `main-v2.2-rc1-c1b`, derived from the B+ prompt with one replaced section; B+ bytes untouched |
| C06 | the classifier names the three recorded stops and leaves an exception or an unknown stop unclassified |
| C07 | the plan digest recomputes, reuses C1's goal and does not collide with any earlier plan |
| C08 | the pre-claim production admission reports READY for the new cell |
| C09 | the pending authorization authorizes nothing |
| C10 | V1/V2/V3/C1 claims are present and no C1-B claim exists |
| C11 | the plan's own design invariants hold |
| C12 | the C1 evidence index (34 files) still matches `sha256:2e8c7be7…` |
| C13 | the C1-B plan id and scope resolve through `contract.scope_plan()`, C1 and both B+ entries are unchanged, and four ids still fail |
| C14 | the real `dispatch(PLAN_ID, MODE_OFFLINE_PROOF)` runs plan-id resolution → authority verification → offline claim → cell → audit → matrix report |

Tests: 22 C1-B tests (plan, prompt derivation, worker contract, production options, router, artifacts,
authorization, history, classification end-to-end, entry resolution, offline dispatch entry), 7
classifier unit tests, 19 worker prompt tests, 30 Stage C1 tests updated for the post-run state, and
the B+ contract, delegation, worker, goal-path and reliability regressions. Ruff is clean on every
touched file.

## J. Authorization and claims

`experiments/stage_c1b/authorization.json`: `PENDING_OWNER_APPROVAL`, `authorizes_live_execution:
false`, both cost fields null, `token_stop` 500,000, `attempt_limit` 1, twelve stated refusals from
`authority.verify`. It carries `intervention` and a `review_surface` that names the prompt, the worker
contract, the grant and the combined-intervention flag.

Claims: V1, V2, V3 and C1 are present and untouched; C1 is spent (`537508bf….claim`, claimed
2026-11-25T07:43:51Z). No claim exists for `d74c4d6f…`.

## K. Exact execution entry

After the Owner signs **the new head and `bplus-executor-1.5`**, one attempt — in a **new** run
directory with a **new** launch lock and a **new** authorization file, because the previous attempt's
directory already holds `PRE_CLAIM_DISPATCH_ENTRY_FAILURE.json` and its own lock:

```python
import asyncio
import sys
from pathlib import Path
from experiments.stage_b_plus import executor

root = Path(r"D:\Work\AtlasRuntime-runs\stage-c1b-completion-2")
report = asyncio.run(
    executor.dispatch(
        "stage-c1b-coding-delegation-completion-v1",
        runs_root=root / "runs",
        evidence_root=root / "evidence",
        authority_path=Path(r"D:\Work\AtlasRuntime-authorizations\stage-c1b-completion-2.json"),
        mode=executor.MODE_LIVE,
    )
)
print(report.get("stop_reason"), report.get("complete"), report.get("total_cost_usd"))
sys.exit(0 if report.get("complete") else 4)
```

The dispatch enforces: clean worktree at the signed head, no claim-root override, the signed record's
plan/protocol/executor/budget/contract digests, both cost stops positive, and a READY pre-claim
admission. One claim, one attempt, no retry, no resume. The claim namespace stays the production
default, so the plan digest — not the directory — is still the one-shot identity.

## L. Limits and unverified assumptions

1. **Combined intervention.** D and E ship together; a passing run would not attribute the gain to
   either.
2. **The oracle's failing assertions are not in the evidence.** The C1 oracle ran in its own sandbox
   and returned `rc=1` / `HIDDEN_TESTS_FAILED` without retaining output. Static reading of the export
   suggests (a) JSON booleans stringified as `True`/`False` instead of `true`/`false`, and (b) CSV
   rows with a second unquoted comma truncated to the first value. Both are **inferences from code**,
   not recorded test results, and no oracle was re-run.
3. **The worker cap is untouched at 20.** If the fixed contract still runs out of turns, the next
   variable to examine is the cap — but the evidence here does not justify raising it first.
4. **Model identity is only half recorded.** The C1 run records `model: "deepseek-chat"` as
   *requested*; no returned model name is stored in the session, trajectory or preflight. The
   earlier six-token availability probe (made during C1's credential check, not now) answered as
   `deepseek-flash`, so `deepseek-chat` is evidently a provider-side alias. That mapping is **not**
   evidenced by the run record and this stage adds no call to establish it.
5. **The completion contract is one prompt revision, not a mechanism.** There is no completion agent,
   no verifier, no new handoff protocol; the child still ends when its turn budget or its own
   judgement ends it, and a partial output is still addressable but not counted as a delivery.
6. **C04 reads a machine-local evidence tree.** On a machine without it the check reports
   `mounted: false` rather than failing, so the delivery semantics are then asserted by review, not
   by the gate.
7. **C14 proves the entry, not the experiment.** It runs the production path with a synthetic cell in
   a throwaway claim root: it shows plan-id resolution, authority verification, the offline claim, the
   audit and the matrix report are reachable, and nothing about a real model or a real sandbox.
8. **The first attempt is spent as an event, not as a claim.** No claim was written, so the plan's
   one-shot identity is still unspent — but the signed authority `921bc382…` names head `5598dce` and
   executor 1.4, and cannot authorize the corrected identity.
