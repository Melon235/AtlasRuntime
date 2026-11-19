# Stage B — Benchmark Protocol

Branch `exp/stage-b-benchmark-protocol`, based on `exp/main-prompt-v21-final-pilot` at `6bc5723`.
**No model was called. No benchmark was executed.** This stage designs the comparison, proves the
fixtures offline, and freezes the plan.

**Verdict: `STAGE_B_PROTOCOL_READY_OFFLINE`.**

Six new tasks, three frozen prompt arms, two repetitions, 36 cells in a balanced order, a metric
catalogue where every metric has a resolver and a stated denominator, a reconstruction layer that
reproduces both earlier pilots from their own evidence, and a freeze that refuses if any of it moves.
The remaining blockers are not design problems: they are an execution authorization and a runner,
neither of which this stage was allowed to create.

---

## A. Git provenance

| Item | Value |
|---|---|
| Baseline | `exp/main-prompt-v21-final-pilot` @ `6bc57233bc3056db545e42d8c0c70deb6b3ddff9`, worktree clean, `rev-list --left-right --count` = `0 0` |
| Branch | `exp/stage-b-benchmark-protocol` |
| Runtime change | **none** — `src/atlasos` is byte-identical to the S2 freeze (`sha256:4eebcf1e…`, 130 files), and the check `runtime_matches_last_frozen_stage` enforces it |
| Historical artefacts | digests recorded in `freeze.json.historical`; no earlier fixture, prompt, oracle or evidence file was modified |
| Deliverables | `docs/stage-b-benchmark-protocol.md`, `experiments/stage_benchmark/{protocol.json,plan.json,estimate.json,freeze.json,offline.json}`, the harness, 29 offline tests |

## B. Research questions

The protocol exists to answer five questions, and a metric that answers none of them is not in it.

| Id | Question |
|---|---|
| RQ1 | Does a later prompt produce more appropriate delegation on tasks that have independent work? |
| RQ2 | Does a later prompt reduce inappropriate delegation and capability mismatch? |
| RQ3 | Does delegation reduce Main peak context? |
| RQ4 | Do child tokens, total cost and latency justify the delegation? |
| RQ5 | What do the prompts do to correctness, test completion and budget exhaustion? |

A Direct run that passes the oracle is a **valid result**, not a failure of the prompt. The protocol
records `direct_execution_rate` next to `effective_delegation_rate` precisely so that a prompt which
correctly declines to delegate is not scored as broken.

## C. Arms

| Arm | Label | Digest | Source |
|---|---|---|---|
| P1 | `main-v1.0` | `sha256:7e6fd829…` | `src/atlasos/app/main_driven.py::MAIN_SYSTEM_PROMPT` |
| P2 | `main-v2.0-rc1` | `sha256:2bb50b09…` | `experiments/stage_s1_prompt_v2/prompts/main_v2.md` |
| P3 | `main-v2.1-rc1` | `sha256:4147f19c…` | `experiments/stage_s2_prompt_v21/prompts/main_v21.md` |

All three load through the existing `MainPromptSpec` seam; `protocol.prompt_spec(arm)` returns the
spec a runner would pass to `GoalRunOptions`. Nothing else about a run differs between arms: same
goal text, same tool descriptions, same worker profile, same router, same limits. A run whose prompt
differs from the arm's frozen digest is not that arm.

`deepseek-chat` accepts no sampling seed, so `run_config.sampling_seed_supported` is `false` and the
two repetitions are the only control over sampling. That limitation is pre-registered rather than
discovered in the write-up.

## D. The task set

Six tasks, none of them Calculator, `eventpipe`, ReleaseGuard or WorkGrid, and none sharing a byte
with an earlier fixture (checked by content digest against both earlier manifests).

| Task | Structure | Files | Reference change | Compat / target / total tests | Variants |
|---|---|---|---|---|---|
| `slugkit` | `LOCAL_DIRECT` | 7 | 1 file, +16/−2 | 5 / 7 / 12 | 3 |
| `pager` | `LOCAL_DIRECT` | 8 | 1 file, +12/−2 | 5 / 6 / 11 | 3 |
| `zonelint` | `INDEPENDENT_INVESTIGATION` | 11 | 1 file, +16/−3 | 11 / 7 / 18 | 4 |
| `ledgerclose` | `INDEPENDENT_INVESTIGATION` | 10 | 1 file, +19/−2 | 11 / 7 / 18 | 4 |
| `retrygate` | `COUPLED_UNCERTAIN` | 10 | 1 file, +22/−1 | 10 / 8 / 18 | 4 |
| `cascade` | `COUPLED_UNCERTAIN` | 10 | **2 files**, +7/−5 | 10 / 7 / 17 | 4 |

The structure label is an argument, not a count of files, and each task records the argument in
`protocol.json.tasks[].rationale`:

* **`LOCAL_DIRECT`** — one stub module whose rules are fully stated, one complete helper, and a
  reference change of one or two functions. No second area to investigate.
* **`INDEPENDENT_INVESTIGATION`** — two complete rule packages that share no types, no imports and no
  failure vocabulary (`records`/`certs`, `ledger`/`rates`), with a shared layer that needs both. Each
  package can be summarised in a few hundred tokens on its own.
* **`COUPLED_UNCERTAIN`** — the failure modes live *between* modules: whether a refusal consumes an
  attempt, whether a zero hint is a hint, whether `^` is right-associative and whether the lexer emits
  it at all. `cascade` has the smallest diff in the set and is still the hardest, because two files
  and one grammar must change together.

Every task ships: a public acceptance suite that fails today, a compatibility suite that passes, a
Host-only reference implementation, a Host-only oracle, and at least three wrong variants.

### What the oracle must catch that the public suite does not

The point of a hidden oracle is not redundancy. Two examples, from the offline proof:

* `zonelint` — a wildcard `*.example.com` covers `a.example.com`, never `example.com`, and never
  `a.b.example.com`. The public suite tests none of the three.
* `cascade` — `-2^2` is `(- (2 ^ 2))` while `(-2)^2` is `((- 2) ^ 2)`, and the canonical form must be
  a fixed point of `normalize`. The public suite states only the first.

## E. Offline proof

`--offline` runs, for every task: the compatibility suite on the frozen fixture, the target suite on
the same fixture, the oracle on the same fixture, the reference implementation's full suite and
oracle, and every wrong variant applied on top of the reference.

| Task | Baseline compat | Baseline target | Baseline oracle | Reference suite | Reference oracle | Variants rejected |
|---|---|---|---|---|---|---|
| `slugkit` | 5 pass | 7 fail | rejects | 12 pass | passes | 3/3 |
| `pager` | 5 pass | 6 fail | rejects | 11 pass | passes | 3/3 |
| `zonelint` | 11 pass | 7 fail | rejects | 18 pass | passes | 4/4 |
| `ledgerclose` | 11 pass | 7 fail | rejects | 18 pass | passes | 4/4 |
| `retrygate` | 10 pass | 8 fail | rejects | 18 pass | passes | 4/4 |
| `cascade` | 10 pass | 7 fail | rejects | 17 pass | passes | 4/4 |

**22 of 22 wrong variants are rejected by their own oracle.** They are one- or two-line regressions
that a plausible solution could contain: ignoring `max_length`, a cursor chain that never ends,
warnings treated as blockers, counts that omit an empty severity, base-currency conversion attempted,
half-up rounding, backoff without a cap, the last attempt spent on an unknown error, power treated as
left-associative, `-2^2` parsed as `(-2)^2`.

Two variants in the first draft were caught only by the public suite, which is the same defect in a
different place: the oracle was not asserting the property it claimed to. Both were fixed by adding
the assertion, not by weakening the variant.

## F. The matrix

36 cells = 6 tasks × 3 arms × 2 repetitions, generated from `seed = 20261119` by a Latin rectangle:

* inside a repetition each task is run once per arm, so the pairing is complete;
* inside a **round** (one cell per task) each arm appears exactly twice, so no arm is systematically
  earlier;
* a task's arm order rotates with its position and the repetition, so the same arm is not always
  first for the same task, and the first cell of repetition 1 is `P1` while the first cell of
  repetition 2 is `P2`.

`plan.json` records the cells in execution order; `plan.digest` is
`sha256:c6914474…`. `matrix.balance_report` measures the properties on the plan itself, and the
protocol validator refuses a document whose plan is unbalanced, incomplete or not reproducible.

## G. Run configuration (frozen)

| Parameter | Value |
|---|---|
| Provider / model | `deepseek`, `deepseek-chat` |
| Main / child turns | 20 / 6 |
| Children per call / concurrent | 2 / 2 |
| Delegation rounds | 3 |
| Shared token budget | 500 000 |
| Tool error mode | `corrected` |
| Public test calls | 4 |
| Sandbox | real OpenSandbox, `atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB |
| Worker profile | `research_worker` (unchanged) |
| Router / handoff | `m.1.0`, unchanged |

Each cell gets its own sandbox, run directory, freshly provisioned fixture and one-shot attempt
marker. There is no retry path and no `--run` command in the harness: execution needs a separate
authorized stage and a runner that does not exist yet.

## H. Metrics

28 metrics in four groups, each with a numerator, a denominator and a missing-value policy, and each
with a resolver in `schema.py`. The catalogue is not prose: `--check` fails if any declared metric has
no resolver, and `validate_record` fails if a record is missing a field a metric needs.

**Primary** — `oracle_pass_rate`, `goal_success_rate`, `budget_exhaustion_rate`,
`runtime_failure_rate`. Every attempt enters the denominator; a failure is never dropped.

**Delegation** — proposal, admission, child completion, effective delegation, direct execution,
invalid capability delegation, late delegation, first delegation turn, first implementation turn.
"Late" is pre-registered: past turn 6, or after the first file edit. Stage S1 delegated on turn 17 and
would score 1; that threshold was chosen before any Stage B data exists.

**Context** — Main peak context, cumulative input, cache hit and miss (UNKNOWN, never 0, when the
provider does not report them), child prompt tokens, system total, completion, wall clock, handoff
payload, duplicate investigation files.

**Verification** — public test calls, pass, repair attempts, changed files, ownership denials.

Three quantities are counted separately and never conflated: **provider requests**, **assistant
turns**, **tool calls**.

## I. Statistics and reconstruction

`reconstruct.py` rebuilds a run from `session.json`, `tool_conversations.json` and `transcript.jsonl`.
It does not read a counter and believe it. Every disagreement is either explained or recorded as a gap.

Three real defects motivate this:

1. **Stage S1** reported `main.turns = 0` for 18 requests, because a run that ends on a budget
   rejection returns an agent result with no messages. The reconstruction reports 18 turns, a delta of
   −18, and the explanation.
2. **Stage S2** reported 14 turns against 13 stored assistant messages, because the final text-only
   message is not persisted. The reconstruction reports the delta of 1 with its reason.
3. **Found while building this stage, from S1's evidence**: the transcript proposes 12 actions for the
   child while the child's stored conversation holds 10. The reconstruction scores that as an
   **evidence gap**, not as a number to trust either way.

The analyser also had to learn two transcript shapes: the runtime recorder writes `{"event": …}`,
while the collected M5 transcript writes `{"payload": {"atlas_trajectory": {"event_type": …}}}`.
Reading only the first silently counts zero of everything — which is how a benchmark ends up
publishing a table of zeros.

Aggregation is deliberately boring: rates over all attempts, medians for distributions, and a paired
view by `(task, repetition)` for the same tasks under different arms. No test of significance is
implemented, because 2 repetitions per cell cannot support one; the protocol says so in
`analysis.exploratory_note` and the report must repeat it.

## J. Scale and cost

From the frozen price block (`experiments/configs/eval-benchmark.yaml`, DeepSeek peak-rate snapshot
2026-11-06, labelled an upper bound) and the two real pilots as the per-run token mixture:

| Scenario | Per run | 36 cells | Cost |
|---|---|---|---|
| LOW (0.75× observed) | 229 592 tokens | 8.27 M | $0.40 |
| EXPECTED (observed) | 306 124 tokens | 11.02 M | $0.53 |
| HIGH (every run spends its 500 k budget) | 514 832 tokens | 18.53 M | $0.90 |

Wall clock: the pilots took 54 s and 64 s of model loop; 180 s per cell including provisioning gives
≈ 1.8 h sequential. The money is not the constraint; the token quota, the wall clock and the operator's
attention are. The price is a frozen snapshot, not a live quote, and the estimate changes linearly
with it.

## K. Freeze

`freeze.json` records the git provenance, the protocol digest, the plan digest and its 36 cells, the
runtime tree digest, the three prompt digests, every task's fixture/goal/oracle/reference/variant
digests and public suite, the run configuration, the harness digests, the analysis schema and
reconstructor versions, the offline report digest, the estimate digest, the digests of eight earlier
stage artefacts, and the calibration plan.

It refuses to run if `--offline` has not passed. It contains no timestamp-derived digest, so
re-deriving it changes only `frozen_at`.

Leak rules, also frozen: the oracle and the reference live on the Host and are never provisioned; the
oracle runs after the agent stops, from a command the agent never sees; the goal text carries no
statement about the experiment, no worker count and no delegation instruction; the private payload
corpus is not published; raw evidence is collected before any summarising.

## L. Calibration

A task outside the matrix — the Stage S2 `workgrid` fixture, already solved, already has an oracle —
is the calibration target. Its purpose is to prove the **evidence collector** works on a live run
before 36 cells are spent: the bundle must contain `session.json`, the attempt marker, the transcript,
the tool conversations, the workspace patch, the trajectory, and an analysis record with counts,
consistency and gaps.

Status: `PREPARED_NOT_RUN`. This stage may not call the model, and a calibration run is a model call.

## M. What is not ready

| Item | State | Why |
|---|---|---|
| Execution authorization | **absent** | not granted for this stage, and not requested |
| Live runner (sandbox + `run_goal` + evidence collection per cell) | **not implemented** | deliberately out of scope; building it before the protocol is frozen would have been the wrong order |
| Calibration run | **not run** | it is a model call |
| Owner approval of the 36-cell spend | **absent** | the estimate is here to be approved or refused |

None of these is a defect in the protocol. They are the boundary of a stage that was told, in one
sentence, not to call the model.

## N. Readiness

`STAGE_B_PROTOCOL_READY_OFFLINE` means: the comparison can be *run* without further design work.

What a formal execution stage would have to do, in order:

1. obtain authorization and an approved spend;
2. implement the runner: for each cell, provision the fixture in a fresh sandbox, call `run_goal`
   with the arm's `MainPromptSpec`, collect the full bundle **before** summarising, run the oracle
   inside the sandbox, destroy the sandbox, write one result record;
3. run the calibration cell and check the bundle against §L;
4. execute the 36 cells in plan order, with the batch stop rules in §G;
5. analyse with `reconstruct.py` and `aggregate()`, and publish every attempt.

What it must not do: retry a cell because the result is unhelpful, read `session.main.turns` as truth,
drop a failed attempt from a denominator, or describe three arms × six tasks × two repetitions as
anything more than exploratory.

## Evidence

| File | Contents |
|---|---|
| `experiments/stage_benchmark/protocol.json` | the frozen document: arms, tasks, config, metrics, plan, rules |
| `experiments/stage_benchmark/plan.json` | the 36 cells in execution order with the balance report |
| `experiments/stage_benchmark/offline.json` | the six-task quality suite, per task and per variant |
| `experiments/stage_benchmark/estimate.json` | the scale and cost envelopes with their assumptions |
| `experiments/stage_benchmark/freeze.json` | every identity the execution stage must re-derive |
| `experiments/stage_benchmark/{protocol,matrix,reconstruct,schema,estimate}.py` | the harness |
| `experiments/stage_benchmark/tasks/*.py` | the six tasks as data: fixture, reference, variants, oracle |
| `tests/integration/test_stage_b_protocol.py` | 29 offline checks, including both earlier pilots |

Reproduce everything with:

```text
python experiments/stage_benchmark/run_stage_b.py --offline
python experiments/stage_benchmark/run_stage_b.py --plan
python experiments/stage_benchmark/run_stage_b.py --estimate
python experiments/stage_benchmark/run_stage_b.py --freeze
python experiments/stage_benchmark/run_stage_b.py --check     # READY
```
