# Stage B-E1 — Benchmark Metric and Estimate Closure

Status: `STAGE_B_METRIC_CLOSURE_ACCEPTED_OFFLINE`.

Branch `fix/stage-b-metric-closure`, based on the frozen Stage B protocol commit
`42244a17efad45c8ee9523789a0218ac70b00f55`. This stage changes how the benchmark is *measured*.
It does not change a single model-facing input: the six tasks, the three prompts, the 36-cell plan
and the run configuration are byte-identical to protocol 1.0 and are asserted so by `--check`.

No model was called. No sandbox was created. No benchmark cell was executed. There is no `--run`.

---

## A. Git Provenance and Resume State

The session that opened this stage was interrupted; its work was still on disk. The branch
`fix/stage-b-metric-closure` already existed at `42244a1` with uncommitted changes to eleven files
and three untracked paths. Nothing was reset, cleaned or checked out over it.

What was found, and what was done with it:

| Path | State found | Decision |
|---|---|---|
| `experiments/stage_benchmark/estimate.py` | rewritten decomposition, `assert_once` | kept, extended with cache-split identity and honest `None` usage |
| `experiments/stage_benchmark/reconstruct.py` | admission states, ladder, classifier | kept, extended with cache identity, `observed_use`, `benefit` |
| `experiments/stage_benchmark/schema.py` | `metric_status`, pair metrics | kept unchanged |
| `experiments/stage_benchmark/protocol.py` | version 1.1.0, revisions | kept, extended with per-metric definitions and counterexamples |
| `experiments/stage_benchmark/run_stage_b.py` | `--coverage`, archive check | kept, extended with archive invariants and a counterexample audit |
| `experiments/stage_benchmark/archive/v1.0/` | verbatim 1.0 freeze and protocol | kept |
| `experiments/stage_benchmark/coverage.json` | 31-row matrix | kept, widened to the full column set |
| `tests/integration/test_stage_b_metric_closure.py` | 26 counterexample tests | kept, extended to 45 |
| `tests/integration/test_stage_b_protocol.py` | record shape updated to 1.1 | kept; every assertion is at least as strong as before |
| `tests/integration/test_stage_s2_analysis.py` | one `pyright` ignore comment | kept |

Local Git at the start of this session:

```text
HEAD    42244a17efad45c8ee9523789a0218ac70b00f55
branch  fix/stage-b-metric-closure
parent  chore(stage-b): re-freeze on a clean worktree
```

Remote verification: the first `git fetch --prune origin` was attempted through the environment's
proxy (`http://127.0.0.1:7890`) and failed — `Failed to connect to 127.0.0.1 port 7890`. A direct
connection to `github.com:443` was then reachable, and the branch was pushed without a proxy. The
remote head of `origin/exp/stage-b-benchmark-protocol` was **not** re-verified before the push; the
value used for the baseline is the already-cached ref at `42244a1`, which equals the local baseline.

The stage's own work is one commit, `1138b8d`, on top of `42244a1`; the freeze commit `52d4e8d`
follows it, and `origin/fix/stage-b-metric-closure` is at `52d4e8d`.

---

## B. Baseline and Frozen Invariants

Every invariant below was re-derived from bytes in this workspace, not from a commit name.

| Invariant | Value | How it is checked |
|---|---|---|
| Runtime source | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae`, 130 files under `src/atlasos` | `source_identity()`, re-hashed on every `--check` |
| Prompt P1 `main-v1.0` | `sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a` | `MAIN_SYSTEM_PROMPT` bytes |
| Prompt P2 `main-v2.0-rc1` | `sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323` | `experiments/stage_s1_prompt_v2/prompts/main_v2.md` |
| Prompt P3 `main-v2.1-rc1` | `sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489` | `experiments/stage_s2_prompt_v21/prompts/main_v21.md` |
| Plan | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648`, 36 cells, seed 20261119 | rebuilt from the seed and compared to the published digest |
| Tasks | `slugkit`, `pager` (LOCAL_DIRECT); `zonelint`, `ledgerclose` (INDEPENDENT_INVESTIGATION); `retrygate`, `cascade` (COUPLED_UNCERTAIN) | fixture, goal, oracle, reference and wrong-variant digests compared to the 1.0 freeze |
| Run configuration | DeepSeek / `deepseek-chat`, main 20 turns, child 6, 2 concurrent children, 3 delegation rounds, 500 000 shared tokens, corrected tool errors, 4 public-test calls, `research_worker`, router `m.1.0`, OpenSandbox `atlas-uat-python:3.12`, network none, 1 CPU, 1 GiB | field-by-field comparison to the 1.0 freeze |

The comparison is a check of its own: `inputs_unchanged_since_1_0`. It compares the current run
configuration, the three prompt digests, the plan digest, the five digests of each of the six tasks
and the runtime tree digest against `archive/v1.0/freeze.json`. It says:

> config, prompts, tasks and plan identical to 1.0

That check is what allows the revision to be called a measurement change. If any of those digests
had moved, the revision would have been an experiment change and this stage would be blocked.

`src/atlasos` was not modified. Its 130 files hash to the same tree digest the S2 stage froze.

---

## C. Original Statistical Defects

Six defects, each found by holding a published number against the evidence it came from.

**C1 — the child prompt was counted twice.** `estimate.py` added the child prompt tokens into the
cache-miss bucket for pricing *and* into the token total as its own field. EXPECTED read 306 124
per run; the same two pilot runs sum to 299 022. The gap, 7 102, is exactly the mean child prompt
(14 203 in S1, 0 in S2). The total was never wrong in the evidence — only in the model of it.

**C2 — a non-empty route was read as an admission.** Admission was inferred from "the router
returned something". `route="direct"` is a decision that admits nobody, and it was being counted as
a successful delegation. The denominator was runs, not proposals, so a run with three proposals
(two admitted, one refused) could only contribute one bit.

**C3 — effectiveness proved ordering, not use.** The old definition was *child succeeded and the
Main edited files afterwards*. That is a temporal conjunction. It cannot distinguish an integration
edit from an unrelated one, and it scored every Direct run as a failed delegation by construction —
S2, which passed its oracle directly, was recorded as zero effective delegation.

**C4 — failure defaulted to `goal`.** Any failed run that had edited a file, or that had invoked a
public suite at all, was classified `goal`. A run whose suite never ran was indistinguishable from
a run whose suite failed, because both produce `public_tests_passed = False`. Sandbox provisioning
failures, runtime faults and unknown tool outcomes had no class at all. This is the defect the
audit of `_failure_class()` was asked to look for, and it was real.

**C5 — a resolver was mistaken for coverage.** Every declared metric had a function, so the protocol
validated. Several of those functions read a field no record carried, and `repair_attempts` named a
quantity ("a repair") that no artefact can establish. A computable column is not a measured one.

**C6 — tool results were attached to copies.** `walk_conversation` extended the flat tool-call list
with the same dicts as the turn list, then attached results to the turn list — or, in the version
that shipped, to a copy. `tool_errors` and `tool_denials` read zero on runs that had both.

A seventh defect was found while writing the tests for C4 and is fixed here: `estimate.py` coerced
an unreported usage field to `0`. A run with no reported cache split and a run with a 100% miss rate
became the same number.

---

## D. Token and Cost Corrections

`estimate.py` now works on a five-field decomposition, and the identity is enforced rather than
described:

```text
main input        = main cache hit + main cache miss      (checked when both halves are reported)
child input       = child cache hit + child cache miss    (the split is UNKNOWN: see below)
system total      = main input + child input + main completion + child completion
```

`assert_once` refuses a usage that carries a field outside the decomposition or lacks one of the
five. `total_tokens` is the sum of the five and nothing else. `run_cost` refuses a usage with a
`None` field instead of pricing zero.

Observed evidence, re-derived from the two real pilot sessions:

| Run | main input | hit | miss | child prompt | main completion | child completion | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| S1 `stage-s1-20261119T094539Z` | 380 202 | 350 592 | 29 610 | 14 203 | 2 267 | 628 | 397 300 |
| S2 `stage-s2-20261119T103641Z` | 197 430 | 178 688 | 18 742 | 0 | 3 314 | 0 | 200 744 |

Both runs satisfy `hit + miss = input` and `system_total = sum of the five fields`. The check
`s1_usage_identity_holds` re-derives it on every `--check`.

Per-run envelope over the 36 cells, from the frozen price snapshot (`2026-11-06`,
`experiments/configs/eval-benchmark.yaml`, 0.006 / 0.3 / 1.2 USD per million):

| Scenario | Tokens per run | Tokens, 36 cells | USD per run | USD, 36 cells |
|---|---:|---:|---:|---:|
| LOW (0.75 × EXPECTED) | 224 266.5 | 8 073 594 | 0.011022 | 0.3968 |
| EXPECTED (the two pilots, averaged) | 299 022.0 | 10 764 792 | 0.014696 | 0.5291 |
| HIGH (a run consumes its 500 000 admission budget) | 500 000.0 | 18 000 000 | 0.024574 | 0.8847 |

Historical comparison: 229 592 / 306 124 / 514 832. LOW moved because it is defined as 0.75 of
EXPECTED and EXPECTED moved; HIGH moved because it is now exactly the declared budget instead of
that budget plus a duplicated child prompt (514 832 = 500 000 + 14 832).

**The HIGH scenario is an assumption, not a ceiling.** `token_budget = 500 000` is an admission
limit the guardrail enforces before a request; it is not a measurement of what a run spent and not
an invoice. The HIGH usage is spread over the five fields in the observed mixture and sums to
exactly 500 000. It contains no double count.

**Child cache visibility is `UNKNOWN`.** The provider reports no per-child cache split. Child prompt
tokens are therefore *priced* at the cache-miss rate — the conservative choice — and are never
*reported* as misses. The estimate carries `child_cache_visibility`, `child_miss_share_assumption`
and `child_pricing_rule` so the assumption cannot be read as a measurement. A record-level
`usage.child_cache_visibility` says the same thing about a reconstructed run.

Four quantities are named separately in the output, because conflating them is how a model becomes a
fact:

| Field | Meaning |
|---|---|
| `provider_reported_usage` | prompt/completion tokens and the main cache split, as reported |
| `budget_admission_estimate` | the 500 000 guard limit; an admission threshold, not a bill |
| `pricing_estimate` | USD against the frozen per-million snapshot |
| `actual_billing` | `UNKNOWN` — the provider exposes no per-run invoice |

---

## E. Router Admission Semantics

A proposal is the Main's `delegate_tasks` call. A decision is the router's answer. Admission is at
least one child the router admitted. Execution is an admitted child that actually ran. The four are
now separate facts on the record.

`reconstruct.proposal_state` classifies one router result into one of five states:

| State | Evidence |
|---|---|
| `NO_PROPOSAL` | no `delegate_tasks` call in the Main's stored conversation |
| `PROPOSAL_REJECTED` | a decision with `route = direct`, or an admitting route whose declared children were all refused |
| `DELEGATE_ONE_ADMITTED` | `route = delegate_one` and at least one declared child with `admitted = true` |
| `DELEGATE_PARALLEL_ADMITTED` | `route = delegate_parallel`, same admission evidence |
| `ROUTER_RESULT_UNKNOWN` | a proposal with no answer, or an answer whose route is not one this runtime emits |

Proposals and decisions are paired by index. When the counts disagree the record says so
(`proposal N has no Router result`, `K Router results for N proposals`) and
`consistency.proposals_match_decisions` is false. Nothing is inferred from absence.

Per-proposal evidence is stored, not summarised away:

```json
{
  "proposal": 2,
  "turn": 9,
  "state": "PROPOSAL_REJECTED",
  "route": "direct",
  "reason_codes": ["SHARED_WRITE_RISK"],
  "blocked_gates": ["GATE_3_WRITE_SCOPE"],
  "admitted_children": 0,
  "declared_children": 1,
  "matched_batch": true
}
```

The rate is `admitted proposals / proposed ones`. Three proposals with two admissions is `2/3`, and
`test_15_multiple_proposals_are_counted_one_by_one` pins it. A Direct run contributes nothing to
the denominator — its resolver returns `None`, not `0` — so it cannot dilute the rate into
"delegation mostly failed".

---

## F. Delegation Effectiveness Model

Six levels, each with its own evidence source. A level is a separate fact; none implies the next.

| Level | Statement | Evidence source |
|---|---|---|
| L1 proposal | the Main called `delegate_tasks` | Main conversation, tool call by name |
| L2 admission | the router admitted at least one worker | `delegation.batches[].decision.children[].admitted` |
| L3 worker completion | **every** admitted worker completed | `batches[].children[].success` |
| L4 handoff delivery | a usable handoff reached the Main | the delegate tool result's `handoff.refs` or child `summary`, *and* at least one child succeeded |
| L5 main continuation | the Main edited or tested after the proposal turn | Main conversation turns after `first_delegation_turn` |
| L6 goal correctness | public suite and oracle passed | `public_tests` statuses and the host-side oracle verdict |

The distinctions the old definition blurred are now structural:

- L3 ≠ L4. A failed child still returns a payload; a status sentence is delivery without use. S1 is
  exactly this case: `delivered = true`, `usable = false`, ladder stops at 2.
- L4 ≠ use. `handoff.observed_use` is `None` and says why: no stored artefact distinguishes an
  integration read from a duplicated one.
- L5 ≠ L6. `test_6` is a run that admitted, completed, delivered, integrated — and failed its goal.
  It reaches 5, and its `effective_delegation_rate` is 0.
- L6 ≠ benefit. `delegation.benefit.attribution` is `UNKNOWN` on every record, including a passing
  one, with the reason recorded.

Metric names and their meanings:

| Metric | Denominator | What it is |
|---|---|---|
| `delegation_mechanism_completion_rate` | runs with a proposal | L1–L5: the mechanism ran end to end |
| `effective_delegation_rate` | runs with a proposal | L1–L6: **an observable proxy**, labelled as such in the protocol |
| `delegation_level_reached` | runs with a proposal | the highest contiguous level |
| `delegation_mechanism_completion_rate` per level | `levels[name]` | numerator and denominator per level in `aggregate()` |

`effective_delegation_rate` was not renamed: renaming it would break the comparison to 1.0 that the
archive exists to preserve. Instead its semantics are versioned: the 1.1 protocol carries an
`interpretation` string that says it is a proxy and that L6 is not caused by the delegation, and the
`revisions` block records the change. A 1.0 analysis still reads 1.0 semantics from
`archive/v1.0/protocol.json`.

---

## G. Failure Classification

`reconstruct.classify_failure` returns a primary class, a confidence, contributing diagnostics and
the evidence it used. Nine classes:

| Class | Trigger (in precedence order) |
|---|---|
| `none` | `acceptance = PASS` |
| `blocked` | the run never started (`status = blocked`) |
| `sandbox` | `sandbox.created = false` |
| `budget` | `stop_reason = budget_exceeded` |
| `turn_limit` | `stop_reason = max_turns` |
| `tool` | `stop_reason ∈ {side_effect_unknown, tool_error, tool_failure}` |
| `runtime` | `status = failed` and an execution-integrity or internal-failure signature |
| `goal` | the last public suite status is `FAIL`, or the oracle is `FAIL` |
| `unknown` | none of the above decided |

Two behaviours this stage had to fix or defend:

**A suite that never ran is not a suite that failed.** The classifier now reads the last recorded
status, not the derived boolean. `public_tests_passed = False` because nothing was recorded is a
gap; `public_tests_passed = False` because a run exited non-zero is a goal failure. And editing a
file is no longer evidence about the goal: a run that wrote code and then died without verification
is `unknown`, which is a value.

**One denial is not a run failure.** A `bash` call refused by the ownership policy, followed by a
successful `file_read` and a passing oracle, is `none` with a contributing note ("1 tool calls were
denied"). `test_11b` pins it. The denial stays in `counts.tool_denials` and in
`failure.contributing`; it does not become the cause.

`runtime` and `sandbox` are aggregated into `runtime_failure_rate`; `tool` deliberately is not,
so a tool fault cannot hide inside an infrastructure number. `unknown_failure_rate` is a metric, not
an embarrassment.

---

## H. Metric Coverage Matrix

`experiments/stage_benchmark/coverage.json`, written by `--coverage` and digested into the freeze.
It has one row per declared metric and every column the stage asked for:

| Column | Content |
|---|---|
| `metric` | metric id |
| `group` | primary / delegation / context / verification |
| `definition` | the statistical definition, in one sentence |
| `numerator`, `denominator` | the exact fraction |
| `evidence_source` | the record path the value is read from |
| `resolver` | whether `schema.RESOLVERS` implements it |
| `returns` | `value` or `pair` (a pair carries its own denominator) |
| `missing_policy` | what happens when the evidence is absent |
| `counterexample_test` | the test that would catch a regression |
| `status_on_complete_record` | `MEASURED` / `UNKNOWN` / `UNSUPPORTED` |

The protocol declares 31 metrics: the 28 of 1.0, three added and one renamed.

| Change | Metric | Why |
|---|---|---|
| added | `unknown_failure_rate` | a classifier that must guess is worse than one that says so |
| added | `delegation_mechanism_completion_rate` | separates "the mechanism ran" from "the goal passed" |
| added | `delegation_level_reached` | the ladder needs a position, not only rates |
| renamed | `repair_attempts` → `post_failure_edits` | the old name claimed a repair the evidence cannot show |

On a complete Direct-PASS record: **23 MEASURED, 8 UNKNOWN, 0 UNSUPPORTED**. The eight are UNKNOWN
by construction, because a Direct run has no proposal and no child:

```text
router_admission_rate  child_completion_rate  delegation_mechanism_completion_rate
effective_delegation_rate  invalid_capability_delegation_rate  late_delegation_rate
first_delegation_turn  duplicate_investigation_files
```

`UNSUPPORTED` is reserved for a schema defect — a metric whose evidence path the record does not
carry at all — and `--check` refuses it (`a_complete_record_supports_every_metric`). `--check` also
refuses a counterexample that does not exist as a test in
`tests/integration/test_stage_b_metric_closure.py` (`every_metric_names_a_real_counterexample`), so
the matrix cannot cite documentation that was never written.

Four of the metrics needed more than a resolver:

**Invalid capability delegation (I1).** Two things together are required: the delegated objective
names a command the profile cannot run, *and* the child's own conversation shows refusals with no
successful file read. `_EXECUTION_MARKERS` (python3, pytest, sha256sum, bash, run_public_tests, …)
are matched against the Main's own `delegate_tasks` arguments; `_REFUSAL_MARKERS`
(`tool_not_disclosed`, `action_denied`) against the child's tool results. A task that mentions
`python3` while the child reads the module and answers is `invalid = false`
(`test_8b`). No proposal at all is `invalid = null`, not `false`. `WorkerCatalog` was not touched:
the defect is in the assignment, and the catalogue is where it would be hidden.

**Duplicate investigation (I2).** Three quantities, kept apart: `handoff.child_files` (what each
child read), `handoff.main_reads_after_handoff`, `handoff.overlap_files` (the raw intersection).
`handoff.redundancy` is `NOT_ASSESSED` when a child read something and `UNKNOWN` when none did.
`duplicate_investigation_files` counts the raw overlap and its protocol entry says a shared path is
not redundancy — a necessary integration read looks identical.

**Repair attempts (I3).** `post_failure_edits` counts edits after the first failing public suite run
and nothing more. `test_17` asserts two edits after a `FAIL`, and the protocol's `interpretation`
says causality between a failure and an edit is not observable.

**Requests, turns and tools (I4).** Three counters, reconstructed separately and never derived from
one another: `counts.provider_requests` (from `metrics.main_requests`), `counts.assistant_turns`
(from the stored conversation), `counts.tool_calls` (from tool-use blocks). `counts.session_turns`
is kept beside them only so a disagreement can be explained. S1 is the proof: 18 requests,
0 session turns, 36 tool calls. S2 is the other: 14 requests, 13 stored turns, 24 calls.

---

## I. Synthetic Counterexample Tests

`tests/integration/test_stage_b_metric_closure.py`, 45 tests. Every case writes synthetic evidence
into `tmp_path`; none reads or writes real evidence, and none calls a model.

The twenty-item checklist, one to one:

| # | Case | Test |
|---:|---|---|
| 1 | Direct + oracle PASS | `test_1_direct_pass_is_not_scored_as_failed_delegation` |
| 2 | Direct + oracle FAIL | `test_2b_a_direct_run_that_fails_its_goal_is_a_goal_failure` |
| 3 | router returns `direct` | `test_2_a_rejected_proposal_is_not_an_admission` |
| 4 | router admits one | `test_3_admitted_but_failed_worker_stops_the_ladder_at_l2` |
| 5 | router admits two | `test_4_two_children_complete_in_parallel` |
| 6 | child succeeded, Main never consumed the handoff | `test_5_child_succeeded_but_main_never_continued` |
| 7 | child succeeded, Main continued, oracle FAIL | `test_6_delegation_completed_but_the_goal_failed` |
| 8 | child succeeded, integrated, oracle PASS | `test_7_delegation_completed_and_the_goal_passed` |
| 9 | research worker given a task it cannot execute | `test_8_a_read_only_worker_asked_to_execute_is_an_invalid_assignment` |
| 10 | budget exhaustion with no session turns | `test_9_budget_exhaustion_with_missing_session_turns` |
| 11 | sandbox provisioning failure | `test_10_sandbox_that_never_started_is_a_sandbox_failure` |
| 12 | runtime internal failure | `test_12_runtime_internal_failure_is_not_a_goal_failure` |
| 13 | tool error / unknown execution outcome | `test_11_an_unknown_tool_side_effect_is_a_tool_failure` |
| 14 | provider usage without a cache split | `test_12_missing_usage_fields_stay_missing`, `test_an_unreported_cache_half_stays_unknown` |
| 15 | both trajectory shapes | `test_13_two_transcript_shapes_describe_the_same_events`, `test_18b_a_nested_transcript_agrees_with_the_same_conversations` |
| 16 | necessary integration read vs overlap | `test_14_a_shared_read_is_counted_raw_not_called_redundant` |
| 17 | edits after a failing public suite | `test_17_edits_after_a_failing_suite_are_post_failure_edits` |
| 18 | several proposals, mixed admission and refusal | `test_15_multiple_proposals_are_counted_one_by_one` |
| 19 | handoff record missing | `test_17_a_missing_handoff_record_is_not_fabricated` |
| 20 | child conversation and trajectory disagree | `test_18_child_conversation_and_trajectory_disagree_is_a_gap` |

Beyond the checklist: a proposal with no router answer is `ROUTER_RESULT_UNKNOWN` with a gap, not a
rejection (`test_a_proposal_without_a_router_result_is_unknown_not_rejected`); a rate with no
denominator is `None`, not 0 (`test_a_rate_with_no_denominator_is_unknown_not_zero`); one denial is
not a failure (`test_11b_a_single_ownership_denial_is_not_a_run_failure`); an unexplained stop is
`unknown` (`test_12b_an_unexplained_stop_is_unknown_not_goal`); a proposal after the first edit is
late (`test_16_a_proposal_after_the_first_edit_is_late`).

The estimate is tested on its own terms: no child, one child, two children, a complete cache split,
an absent one, all-hit, all-miss, non-zero output, the HIGH assumption, the 36-cell totals, and the
price snapshot. The decisive one is
`test_one_child_and_two_children_add_their_usage_once`: adding a second child prompt must move the
token total by exactly the child prompt and the cost by exactly one miss-priced input. A double
count fails it in two places.

`test_the_price_snapshot_has_not_moved` asserts 0.006 / 0.3 / 1.2, the 2026-11-06 date and the
config path. The frozen price file was not edited to make any expectation come true.

---

## J. S1/S2 Reconstruction Regression

The two real runs are the only ground truth available, and both are reproduced by `--check`.

**S1 (`stage-s1-20261119T094539Z`)** — 18 provider requests, 36 tool calls, `session.main.turns = 0`,
turn delta `-18` explained by the budget rejection, system total 397 300. One proposal, admitted,
one child. The ladder reaches 2: the child was refused tools its profile did not have
(`capability.invalid = true`) and never completed, so L3 fails. The handoff was delivered and not
usable. Primary failure `budget`.

Its child conversation holds 10 tool calls while its trajectory proposes 12. That difference is
recorded, not smoothed: `transcript proposes 48 actions, the conversations hold 46 (main 36 +
children 10)`. The analyser reports the mismatch and keeps both numbers.

**S2 (`stage-s2-20261119T103641Z`)** — 14 provider requests, 13 stored turns (delta `+1`, explained
by the final text-only message that the conversation store does not keep), 24 tool calls, system
total 200 744. No proposal, `NO_PROPOSAL`, public suite and oracle PASS, primary failure `none`.
A Direct PASS is a legitimate result and is scored as one.

Neither `session.json` nor any evidence file was modified. Both reconstruction checks
(`s1_evidence_reconstructs`, `s1_main_counts_explained`, `s1_semantics_reconstruct`,
`s1_usage_identity_holds`, `s2_evidence_reconstructs`, `s2_direct_pass_reconstructs`) run on every
`--check`.

---

## K. Protocol v1.0 → v1.1 Changes

`protocol.json` is now `1.1.0` and declares `supersedes: 1.0.0`. The `revisions` block records six
semantic changes, each with an id, an area, the change and the rationale:

| Id | Area | Change |
|---|---|---|
| E1-1 | token and cost accounting | five-field decomposition, counted once |
| E1-2 | router admission | five states, per proposal, `direct` is a rejection |
| E1-3 | delegation effectiveness | L1–L6 ladder, proxy labelled |
| E1-4 | failure classification | nine classes, `unknown` kept, no goal by default |
| E1-5 | metric coverage | evidence path and status per metric, `post_failure_edits` |
| E1-6 | proposal matching | the flat call list shares the turn's objects |

Versioned artifacts:

| Artifact | Version |
|---|---|
| `protocol.json` | `1.1.0` |
| `plan.json` | `1.1.0` (plan block unchanged) |
| result schema | `stage-b-result/1.1` |
| analyser | `stage-b-reconstruct/1.1` |

The 1.0 artifacts are preserved verbatim in `experiments/stage_benchmark/archive/v1.0/`
(`protocol.json`, `freeze.json`) with a `manifest.json` that records their digests, the source
commit and what supersedes them:

| Archived file | Digest |
|---|---|
| `protocol.json` | `sha256:8bcedf7510411ca8b682e0ef4a6627ceb5cab373df8b7b94f14525194d133857` |
| `freeze.json` | `sha256:a37e37b7fe87df7d5fb03487b535e7f31f37d3307c9d1480633cfee7a318f2a5` |

`--check` re-hashes them on every run (`archive_matches_published_v1_0`). A published 1.0 result can
therefore still be re-derived with 1.0 semantics; nothing was overwritten to hide what 1.0 got wrong.

What did **not** change is asserted, not promised: `inputs_unchanged_since_1_0` compares the run
configuration, the three prompt digests, the 36-cell plan digest, five digests per task and the
runtime tree digest against `archive/v1.0/freeze.json`.

---

## L. Freeze Identity

`experiments/stage_benchmark/freeze.json`, written by `--freeze` on a clean worktree
(`git_status` empty, 0 dirty paths) at HEAD `1138b8d533f4aeafaee2f8f169b2888caf0cebf2`:

| Field | Value |
|---|---|
| `protocol_version` | `1.1.0` |
| `frozen_at` | `2026-11-20T05:44:12.404388+00:00` |
| runtime tree | `sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae`, 130 files |
| `protocol.json` | `sha256:937f925306659ea7c1db194e7083e5234949edc76566a78974bbbc77983a1585` |
| plan | `sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648`, 36 cells |
| `estimate.json` | `sha256:55b826a1f3f70e75cae881ee27e2c2e4366eb55fbcfe62f060bac3e03f07db13` |
| `coverage.json` | `sha256:b404e6ce8364c95c4797a5cf14de72f34204b8f3447031abcaaeacf658f9815b`, 31 rows |
| `offline.json` | `sha256:cdff8aeca2959290098a4c444f262a085ea972b4ce04b3709856dd9ad7bf3b1e` |
| v1.0 archive manifest | `sha256:a321f07a5cda9b0ebd511905e6b1c665bf5543f16efcd94b54cced29a0faaea0` |
| prompts | P1 `7e6fd829…`, P2 `2bb50b09…`, P3 `4147f19c…` |
| schema / analyser | `stage-b-result/1.1` / `stage-b-reconstruct/1.1` |
| harness | all 14 files digested, including `protocol.py`, `reconstruct.py`, `schema.py`, `estimate.py` |

The manifest has no self-reference: `freeze.json` is not among the harness files it digests, and it
does not digest itself. It is written *after* `protocol.json`, `plan.json`, `estimate.json`,
`coverage.json` and `offline.json` are final, and `--freeze` refuses to run without a passing
`offline.json` and a written `coverage.json`.

Ordering, for reproducibility: commit the stage, then `--freeze` on the resulting clean tree, then
commit the manifest. `freeze.json` therefore records the HEAD it was derived from, exactly as the
1.0 freeze did. The working tree is clean at the moment it was written — which is the property the
record is for.

---

## M. Full Regression

Run in this workspace, with the repository's own `.venv` (Python 3.12.15, pytest 9.2.0, ruff
0.17.4, pyright 1.1.414):

| Command | Result |
|---|---|
| `python experiments/stage_benchmark/run_stage_b.py --offline` | `passed: true`; 6 tasks, 22 wrong variants, 22 rejected |
| `python experiments/stage_benchmark/run_stage_b.py --plan` | 36 cells, plan digest `c6914474…` unchanged |
| `python experiments/stage_benchmark/run_stage_b.py --estimate` | written; EXPECTED 299 022 |
| `python experiments/stage_benchmark/run_stage_b.py --coverage` | 31 metrics: 23 MEASURED, 8 UNKNOWN, 0 UNSUPPORTED |
| `python experiments/stage_benchmark/run_stage_b.py --check` | `READY`, 21 checks, 0 failures |
| `python experiments/stage_benchmark/run_stage_b.py --freeze` | written on a clean tree |
| `pytest tests/integration/test_stage_b_protocol.py` | 29 passed |
| `pytest tests/integration/test_stage_b_metric_closure.py` | 45 passed |
| `pytest` (whole suite) | **1651 tests, 0 failures, 0 errors, 7 skipped**, 312.6 s |
| `pyright` | 0 errors, 0 warnings |
| `ruff check .` | 66 errors — **all pre-existing**; 0 introduced by this stage |
| `ruff format --check .` | 11 files would be reformatted — **all pre-existing**; 0 introduced |

The lint result needs to be stated precisely rather than rounded up. `ruff check .` fails on this
repository at the baseline commit `42244a1`, not because of this stage: a detached worktree was
created at `42244a1` and the same command there reported the same 66 errors. They are all in
`experiments/stage_s1_prompt_v2/**` and `experiments/fixtures/stage_s1_releaseguard/**` — frozen
Stage S1 artefacts carrying `E501` lines of 101–105 characters. The 11 format diffs are in the same
frozen files. Modifying them to satisfy a newer ruff would change a published stage's artefacts,
which this stage is forbidden to do and which would be a worse outcome than a red lint run.

Scoped to everything this stage owns — `experiments/stage_benchmark/**` and the two Stage B test
files — `ruff check` and `ruff format --check` both pass. This is a pre-existing repository
condition, recorded in `N` as a risk, not a stage result.

No original assertion was deleted, weakened or skipped. The 1.1 record shape is stricter than the
1.0 one (`delegation.proposals`, `delegation.children_admitted`, `delegation.admission`,
`delegation.levels`, `handoff`, `capability`, `failure`, `usage`), and
`test_aggregation_counts_every_attempt_in_the_denominator` gained four assertions rather than
losing any.

---

## N. Remaining Risks

1. **No live evidence exists yet.** Every semantic here is proven against two runs that were not
   benchmark cells. The first 36-cell execution may still surface a field the runtime does not
   write, and that will be a schema gap to record, not a statistic to invent.
2. **The admission evidence shape is inferred from what the runtime writes today.** `proposal_state`
   reads `delegation.batches[].decision.{route,children}`. If the live runner writes admission
   elsewhere, every proposal becomes `ROUTER_RESULT_UNKNOWN` — visible, because the state is
   recorded, but the benchmark would produce no admission rate.
3. **Child cache split is permanently UNKNOWN at this provider.** Child tokens are priced
   conservatively at the miss rate. Real cost may be lower. The direction of the error is known and
   stated; the magnitude is not.
4. **A two-run observed mixture is a weak prior.** LOW, EXPECTED and HIGH bracket it, but a
   delegation-heavy task may exceed HIGH in completion tokens even under the shared budget —
   `token_budget` limits admission, not spend.
5. **`effective_delegation_rate` remains a proxy.** Its name is preserved for comparability; its
   meaning depends on every reader seeing the `interpretation` field. It should not be quoted alone.
6. **Pre-existing lint failure.** `ruff check .` and `ruff format --check .` fail on frozen Stage S1
   artefacts (66 and 11 respectively), identically at the baseline commit. CI runs `ruff check .`
   and would be red for that reason. Fixing it requires a decision about editing frozen stage
   artefacts, which is outside this stage's authority.
7. **The baseline remote ref was not re-verified.** The `fetch` fell back to the already-cached
   `origin/exp/stage-b-benchmark-protocol`. The stage's own branch was pushed and read back at
   `52d4e8d`, so the publication of this work is confirmed; the *baseline's* remote state is not.
8. **`observed_handoff_use` is knowingly absent.** It would require the runtime to record why the
   Main read a file. The record says `UNKNOWN` and explains itself rather than inferring use from
   ordering.

---

## O. Final Decision

`STAGE_B_METRIC_CLOSURE_ACCEPTED_OFFLINE`.

Acceptance conditions, one by one:

| # | Condition | State |
|---|---|---|
| M1 | baseline intact, historical artifacts unmodified, runtime/prompt/task/plan digests unchanged, worktree clean | met — `inputs_unchanged_since_1_0`, `archive_matches_published_v1_0`, `plan_digest_unchanged`, `runtime_matches_last_frozen_stage` |
| M2 | no double count, identities hold, LOW/EXPECTED/HIGH correct, child cache gap explicit, budget not described as a bill | met — `assert_once`, `s1_usage_identity_holds`, `value_types`, `budget_semantics` |
| M3 | `direct` is not an admission, proposals counted one by one, worker success ≠ effective delegation, missing handoff stays UNKNOWN, Direct PASS valid | met — five states, pair metrics, ladder, `observed_use = null` |
| M4 | 31 metrics have a coverage matrix, resolver matches evidence, gaps recorded, S1/S2 untouched, both trajectory shapes parse | met — `coverage.json`, `every_metric_names_its_evidence`, `a_complete_record_supports_every_metric` |
| M5 | ≥15 counterexample classes, old tests still pass, ruff, pyright, full regression recorded | met except repository-wide ruff, which is pre-existing and reported in `N.6` |

Nothing above was reached by relaxing a metric or rewriting a historical run. The one condition that
is not green is a lint failure that predates the stage and lives in files this stage may not edit;
it is disclosed rather than absorbed.

---

## P. Stage B-E2 Readiness

What B-E2 can rely on, and what it must still build:

**Ready.** The protocol is frozen at `1.1.0` with an archived `1.0.0`. The analyser reconstructs a
run from `session.json`, `tool_conversations.json` and either transcript shape. The result schema is
validated, and every metric declares where its value comes from and what happens when it is absent.
The estimate is decomposed and priced. The freeze records the harness, the runtime, the prompts, the
tasks and the plan. `--check` is a 21-item gate that ends in `READY` or `BLOCKED`.

**Not built, and deliberately so.** There is no `--run`, no live runner, no model call, no
calibration. The workbench has never produced a benchmark cell.

**Prerequisites for B-E2, in order.**

1. An explicit authorization for a bounded live budget, with the cell count and the USD ceiling in
   writing.
2. A runner that writes the evidence bundle `reconstruct.py` expects and the freeze's `calibration`
   block lists: `session.json`, `attempt-1.json`, `transcript.jsonl`, `tool_conversations.json`,
   `workspace.patch`, `trajectory/`, and an analysis record carrying `counts`, `consistency` and
   `gaps`.
3. One calibration cell on `workgrid` — outside the matrix, unscored — to prove the bundle is
   complete before the first scored cell. Its status is `PREPARED_NOT_RUN`.
4. A single check that the live admission record matches `proposal_state`'s expectation, because
   risk `N.2` would otherwise turn into thirty-six `ROUTER_RESULT_UNKNOWN` rows.
5. The decision on the pre-existing lint condition (`N.6`), which belongs to whoever owns the frozen
   S1 artefacts.

Until those exist, this stage stops here. No further stage was started.
