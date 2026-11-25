# Stage B+ — Worker Capability x Capability Exposure (Phases C, D, F–K)

Status: `STAGE_B_PLUS_CAPABILITY_ABLATION_READY_FOR_REVIEW`
Candidate contract: `experiments/stage_b_plus/protocol.json`, digest `sha256:789bce6e…`
No cell has run. The authorization record is `PENDING_OWNER_APPROVAL` and authorizes nothing.

Companion document: `docs/stage-b-plus-capability-audit.md` (Phases A, B, E — what the code does).

---

## C. Capability exposure design

### The channel

The disclosure is appended to the **`delegate_tasks` tool description**, not injected into the Main
system prompt. Three reasons, each falsifiable:

1. The system prompt is the frozen identity of P1/P2/P3. Putting the disclosure there would either
   change one of those digests or force every arm onto a new prompt anyway.
2. `DelegateTasksTool` already takes `description` as a constructor argument, so the disclosure rides
   an existing seam instead of a new one.
3. It keeps the exposure factor on one channel. A reviewer can diff four tool descriptions; diffing
   "prompt plus tool description" across four arms is how a confound hides.

The shipped description stays first and the disclosure is appended after it, so the stable prefix a
prompt cache sees is unchanged in position. `none` returns the shipped string **object**, not a copy
of it (asserted in `tests/unit/test_stage_b_plus_capability_card.py`).

### The card, and where each field comes from

`src/atlasos/delegation/capability_card.py::build_card` is a pure projection of three trusted inputs:
the `WorkerProfile`, the run's registered tool names, and the Host write grant. Nothing is written by
hand, so nothing can drift.

| Field | Derived from | Example of what it prevents |
|---|---|---|
| `role_id`, `role_purpose` | `profile.profile_id`, `profile.role` | a disclosure that names a profile the catalog does not have |
| `allowed_capabilities` | `profile.base_tools ∩ registered` | offering `bash` to a read-only profile |
| `forbidden_capabilities` | the run surface − the profile's tools | omitting that a worker has no `run_public_tests` |
| `conditional_capabilities` | the grant, the presence of `bash`, any withheld tool | claiming writes are allowed when the grant is empty |
| `ownership_requirements` | fixed: declare paths, stay inside the grant | a Main that declares a path it cannot have |
| `task_input_contract` | the wire schema (`models.py:41-72`) | a worker asked to work with no background |
| `deliverable_contract` | `result_preview_chars` | expecting a full report in the summary |
| `verification_contract` | whether `run_public_tests` is in the profile | a Main that trusts a child's unverified test claim |
| `handoff_semantics` | the handoff reader | a Main that expects a child transcript |
| `appropriate_use_cases` | write capability + grant | task-specific, hand-tuned hints |
| `max_turns` | `min(profile.max_turns, child_max_turns)` | reporting the requested cap as the effective one |

`CARD_AUTHORITY = "disclosure_only"` is carried in the card and in the run evidence. **The card is not
a permission source.** No router, gate, policy or factory reads it; the runtime decision is identical
with the card present or absent.

### What the disclosure must never do — and how that is checked

| Prohibition | How it is held |
|---|---|
| promise a tool that does not exist | `allowed` is the profile's own tool list; a unit test asserts the rendered "Can use" line never contains a write tool for a read-only profile |
| widen authority | the card has no field that the runtime reads; the grant is not a card field |
| reveal the frozen DAG | the card is built from the profile, not the task |
| give a standard decomposition | `appropriate_use_cases` is derived from capability, never from a benchmark task |
| contain the hidden oracle | the oracle is not an input to anything in `src/` |
| force delegation | no imperative: the card describes, and P3's delegation language is unchanged |
| be hand-tuned to one experiment | there is no task id, no file name and no goal text anywhere in the derivation |

### The prompt the matrix uses

`main-v2.1-rc1-bplus` = Stage S2's P3 text with the `## WORKER CAPABILITY CONTRACT` section removed:
10,377 → 9,033 chars, `sha256:76ebb03d51b7d…`, removed block 1,344 chars
`sha256:5dd265cc…` (recorded in `protocol.json`).

Why: that section enumerates one profile's tools by hand. Held constant while the profile changes, it
either contradicts the arm (C2/C3) or duplicates the disclosure (C1). Removing it makes the exposure
factor single-channel. `p3-literal` is kept and digest-pinned so the Owner can run the literal-S06
prompt and observe the degeneracy rather than read about it.

This is a protocol change and is reported as such (`R-B+1`). It is a *deletion*, not a rewrite: a test
asserts `bplus == p3[:start] + p3[end:]`.

---

## D. The four arms, and the coupling that must be stated

| Arm | Capability | Exposure | Offered profile | Write grant |
|---|---|---|---|---|
| C0 | research | native | `research_worker` | none |
| C1 | research | card | `research_worker` | none |
| C2 | coding | native | `coding_worker` | the task's `allowed_paths` |
| C3 | coding | card | `coding_worker` | the task's `allowed_paths` |

`native` is a derived prose enumeration — role, usable tools, missing tools, limits — i.e. the same
disclosure *class* as P3's hand-written paragraph, generated instead of maintained. `card` adds the
contract fields. `none` (the shipped 819-char description) remains available and is what a fifth
"no disclosure" arm would use; it is **not** C0 or C2, because it says nothing about the profile.

### Coupling, stated rather than claimed away

* **Capability and disclosure move together in C2.** A run that offers `coding_worker` and discloses
  nothing gives the Main a schema default of `research_worker`; a run that discloses `coding_worker`
  has, by construction, a writable profile. C2 − C0 therefore measures *capability plus its minimal
  disclosure*, not capability alone. The matrix cannot separate them at four arms, and this document
  does not pretend otherwise.
* **The tool schema does not change across arms.** `delegate_tasks`' input schema is 1,131 chars with
  digest `sha256:b76380f3…` in all four arms — `profile_id` is a free string with no `enum`. The arms
  differ only in the description (1,613 / 3,030 / 1,805 / 3,336 chars). The coupling is therefore in
  the *profile set and grant*, not in the schema, which is the opposite of what a four-arm design
  usually has to record.
* **`worker_max_turns` replaces the profile cap.** Both arms are run at `max_turns = 20` through
  `GoalRunOptions.worker_max_turns`, because the shipped caps are 6 and 8 and an unequal turn budget
  would be a second difference between the arms. The substitution is recorded per run
  (`delegation_controls.profiles.*.effective_max_turns`), and a value above `child_max_turns` is
  refused rather than silently clipped.

If the Owner wants a capability-only arm, it is one configuration line (`exposure = "none"`) and a
fifth arm id; the code already supports it. It is not in the 24 cells because a 4x3x2 matrix is
already the smallest design that can show a direction.

---

## F. Candidate experiment plan

`4 arms × 3 tasks × 2 repetitions = 24 cells`, order from `matrix.build_plan` (fixed seed
`20261119`; the order is regenerated from the seed, no RNG is consulted at run time). Plan digest
`sha256:ab4d0e50…` in `plan.json`; identities are unique and the balance report is asserted in
`contract.validate`.

| Property | Promised | Measured |
|---|---|---|
| every task meets every arm once per repetition | yes | yes |
| every arm runs equally often (6 cells) | yes | yes |
| the first cell's arm differs across repetitions | yes | yes (`C0`, then `C1`) |
| every arm appears in every round | **not promised** — four arms, three tasks per round | `null`, deliberately |

Candidate parameters (`protocol.json → run_config`):

```text
provider deepseek · model deepseek-chat · main_max_turns 20 · child_max_turns 20
worker_max_turns 20 · token_budget 500000 · max_concurrent_children 2
max_children_per_call 2 · max_delegation_rounds 3 · result_preview_chars 900
public_test_max_calls 4 · tool_error_mode corrected · router_policy m.1.0
network none · cpu 1 · memory 1Gi · image atlas-uat-python:3.12 · timeout 30 min
matrix_concurrency 1 · repetitions 2 · seed 20261119
```

**C0 is not a reproduction of S06.** S06 ran six different tasks, `child_max_turns=6`, a
research-only catalog and no grants. B+ changes the tasks, the worker turn cap, the prompt and the
grants at once. Only the four arms are comparable to each other.

**Independent identity.** New protocol (`bplus-1.0.0`), new plan digest, new config digest, new claim
namespace (`bplus-`), new evidence root under the Stage B+ directory, and its own pending
authorization record. Freeze 1.6 is neither edited nor referenced as an authority (§M).

---

## G. Delegation effectiveness metrics

The L1–L6 ladder and the five proposal states are reused verbatim from
`experiments/stage_benchmark/reconstruct.py` (imported, not copied), so a B+ row and an S06 row are
read with the same vocabulary.

```text
L1_proposal          the Main called delegate_tasks
L2_admission         the router admitted at least one worker
L3_worker_completion every admitted worker completed
L4_handoff_delivery  a usable handoff reached the Main
L5_main_continuation the Main edited or tested after the handoff
L6_goal_correctness  public suite and host oracle both passed
```

Primary rates and their denominators (`protocol.json → primary_metrics`):

| Rate | Numerator | Denominator | Missing |
|---|---|---|---|
| `proposal_rate` | cells with ≥1 `delegate_tasks` call | cells | 0 only if the tool was on the surface |
| `valid_proposal_rate` | cells whose first proposal was admitted | cells with a proposal | UNKNOWN if none; never a rejection |
| `admission_rate` | proposals admitting ≥1 child | proposals | **N/A when no proposal exists** |
| `worker_completion_rate` | admitted children that succeeded | admitted children | UNKNOWN if none |
| `handoff_delivery_rate` | cells with a usable handoff | cells with a completed child | UNKNOWN if none |
| `main_continuation_rate` | Main edited/tested after handoff | cells with a handoff | UNKNOWN if none |
| `goal_correctness_rate` | public suite **and** oracle pass | cells | counted FAIL, never dropped |

`NO_PROPOSAL` and `PROPOSAL_REJECTED` are separate values of the same field. A cell with no proposal
reports admission as **N/A**, never as a rejection and never as 0. Three proposal kinds stay distinct
in the evidence: natural (the only kind the matrix scores), scripted (Phase B probes, scored nowhere),
and forced (not used at all).

---

## H. Context-engineering metrics

Delegation is supposed to reduce Main context, so both the reduction and its cost are recorded, and
neither is allowed to stand in for the other.

| Group | Metrics |
|---|---|
| Main context | `main_peak_context_tokens`, `main_cumulative_input_tokens`, `main_cache_hit_tokens`, `main_cache_miss_tokens`, `main_completion_tokens`, `main_context_tokens_by_request` |
| Child context | `child_total_prompt_tokens`, `child_completion_tokens`, `worker_tool_calls`, `handoff_payload_chars`, `handoff_reference_count`, `context_projection_chars` |
| System | `system_total_tokens`, `wall_clock_seconds`, `cost_estimate_usd` |
| Behaviour | `first_delegation_turn`, `first_implementation_turn`, `main_continuation_after_handoff` |

Accounting, unchanged from Stage P: a request's *context* is cache-miss + cache-read (+
cache-creation); `system_total_tokens` = Main input + Main completion + child prompt + child
completion, each counted once; a field the provider did not report is `null` and named in
`unavailable_fields`, never zero.

The four readings are kept separate on purpose: *Main context reduction*, *system-wide token
overhead*, *worker work completion*, *end-to-end correctness*. A smaller Main context does not make
the system cheaper (Closure-2B-3 measured −11.4 % per request with +28 % requests), and a larger total
does not make offloading worthless.

`child_cache_visibility` is `UNKNOWN` by design: the provider reports no per-child cache split, so
child tokens are priced as misses and reported as prompt tokens.

---

## I. Interpretation, fixed before any cell runs

| id | Observation | Reading | Limit |
|---|---|---|---|
| I1 | C1 > C0 on `proposal_rate` | disclosure alone changes whether a research worker is used | 6 cells per arm per task; a direction, not significance |
| I2 | C2 > C0 | a writable worker plus its native disclosure changes delegation | capability and disclosure are coupled (§D) |
| I3 | C3 > C2 | the card adds value beyond the prose enumeration | the disclosures differ in contract detail, not in the presence of capability information |
| I4 | only C3 improves | possible capability × exposure interaction | not separable from arm order at 24 cells |
| I5 | proposals rise, admissions do not | the bottleneck is proposal quality or the router | read per-proposal reason codes first |
| I6 | admissions rise, completion does not | the bottleneck is the worker contract, its tools or its budget | check child stop reasons and ownership denials |
| I7 | completion rises, handoff/continuation does not | the bottleneck is how results enter Main context | check payload size against the preview bound |
| I8 | L1–L5 improve, L6 does not | **no claim about final task performance** | L6 is correctness of the run, never attributed to delegation |
| I9 | all arms stay `NO_PROPOSAL` | the capability hypothesis does not explain S06; look at task, model or decision policy next | no arm may be declared best |

No arm is favoured in advance. C3 is not predicted to win.

---

## J. Offline validation (`offline.json`, 17 checks, 17 pass)

| id | What it proves |
|---|---|
| J01 | the four constructed requests differ only in `delegate_tasks.description_*` and its aggregate |
| J02 | every disclosed tool set is the trusted profile's, and the disclosed card digest is that profile's card |
| J03 | a research worker has no route to a write |
| J04 | a coding worker completes a granted coding task |
| J05 | the Main-driven path derives real workers and runs their loops |
| J06 | router and ownership still refuse declared writes, shared writes, single write tasks, and unoffered tools |
| J07 | a handoff reaches the Main and is read with `delegate.read` |
| J08 | the four arms share one budget and one run configuration |
| J09 | system usage includes Main **and** children (`system_total_tokens` identity holds) |
| J10 | L1–L6 are recoverable from a run directory by the Stage B analyzer |
| J11 | 24 unique cell identities |
| J12 | an unsigned record is refused; a signed record bound to another digest is refused too |
| J13 | Freeze 1.6 is unchanged against `e0bc2cc` |
| J14 | no protected Closure-2 / Stage B path changed, and the S07 commits are still reachable |
| X01 | the candidate contract validates its own invariants |
| X02 | every capture completed in one turn against a fake provider: no provider was constructed |
| X03 | the frozen Closure-2 oracle still discriminates seed from reference on all three tasks |

Reused rather than rebuilt: `reconstruct.reconstruct/annotate/aggregate` for L1–L6 and context
metrics; `closure2.preflight.task_selftest` for oracle satisfiability; `stage_benchmark.estimate` for
pricing arithmetic; `matrix.build_plan` for the order. New code is limited to the contract, the
disclosure, the request capture, the five probes, the cost model and the authorization gate.

### Regression status, stated honestly

* New tests: `tests/unit/test_stage_b_plus_capability_card.py` (18) and
  `tests/integration/test_stage_b_plus_ablation.py` (22) — all pass.
* Existing Stage M / Stage P / Stage S1 / Stage S2 / R0 / G3-3 suites: pass.
* `tests/integration/test_stage_b_matrix_executor.py` + `test_stage_b_execution_projection.py`: **50
  failures on this branch and the identical 50 on a pristine `e0bc2cc` worktree** (compared
  line-by-line, zero difference). The cause is the executor's own preflight binding the runtime tree
  digest, which any checkout with different line endings fails; it is not caused by this stage. The
  full suite was not run to completion here, and no "green" claim is made for it.

---

## K. Cost estimate (`cost.json`)

Frozen prices: hit `$0.006`/M, miss `$0.30`/M, output `$1.20`/M (snapshot 2026-11-06). A miss costs
50× a hit, and **a child prompt is priced as a miss** because no child cache split is reported. That
single fact drives every number below.

| Scenario | Assumption | Tokens | Cost |
|---|---|---|---|
| S0 — S06 basis | one B+ cell = one S06 cell (84,227 tokens); S06 delegated in 0 of 36, so a lower bound | 2,021,446 | **$0.121** |
| S1b — delegated, 6-turn child | 60 % of cells delegate 2 children, 6 turns each | 2,662,534 | **$0.354** |
| S1 — delegated, 20-turn child | same with the candidate cap of 20 | 7,787,206 | **$1.986** |
| S2 — budget fill | every cell uses its full 500,000-token admission budget | 12,000,000 | **$0.721** |
| Nominal capacity | 24 × 500,000 = 12 M tokens | 12,000,000 | — |

S1 is **5.6×** S1b and exceeds S2: the token counts order one way and the prices order the other,
because S2 assumes S06's 93.7 % cache mixture while S1 prices child prompts as misses. That inversion
is the cost finding, not an arithmetic slip.

The dominant variable is the **worker turn cap**: the child prompt is re-sent every turn, so 20 turns
cost 5.6× what 6 turns cost. S06 requested 6 for a profile that caps at 6 — the requested and the
effective number agreed by luck. The Owner should set the B+ cap on cost grounds.

Proposed, unsigned: cumulative stop **$3.00**, canary **4 cells** (one per arm on P1, repetition 1)
with a **$0.40** stop. Nothing is approved; no S06 or S08 remainder is carried forward.

The estimate's weak point is stated in the artefact: no B+ child has ever run, so the child shape is
an assumption. If the cells do not delegate, S0 is the forecast; if they do, S1 is.

---

## L. Handoff

| # | Item | Where |
|---|---|---|
| A | Git provenance and worktree isolation | this document, §A below |
| B | Actual worker capability audit | `docs/stage-b-plus-capability-audit.md` §A.1 |
| C | Main-facing provider request audit | same §A.2; `inputs/request-C*.json` |
| D | Dynamic coding worker feasibility | same §B; `probe.json` |
| E | H1/H2 evidence assessment | same §H1/H2 |
| F | Capability card design and implementation diff | §C; `src/atlasos/delegation/capability_card.py`, `exposure.py` |
| G | Four-arm configuration and input diffs | §D; `inputs/diff.json` |
| H | Workload compatibility | audit §E; `workload.json` |
| I | Candidate 24-cell plan | §F; `plan.json` |
| J | L1–L6 and context metrics | §G, §H |
| K | Offline validation | §J |
| L | Expected / HIGH cost estimate | §K |
| M | Historical freeze immutability | §M |
| N | Closure-2 isolation | §N |
| O | Independent pending authorization | §O |

### A. Git provenance

```text
repository  Melon235/AtlasRuntime
baseline    e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1
branch      feat/stage-b-plus-capability
worktree    D:\Work\AtlasRuntime-stage-b-plus   (created by `git worktree add`; no existing
            worktree was overwritten, and the S08 worktree was not touched)
files       src: 3 changed, 2 added · tests: 2 added · experiments/stage_b_plus: 10 added
            docs: 2 added · generated artefacts: protocol, plan, offline, probe, cost,
            workload, authorization, inputs/
```

PR #6 is untouched and unmerged: it stays at `e0bc2cc`, draft.

### M. Freeze immutability

`experiments/stage_benchmark/execution/freeze-1.6.json` is unchanged against `e0bc2cc`. Its bytes
hash differently in this worktree because a fresh checkout materialises LF where the original had
CRLF, so all three digests are recorded: raw `73713095…`, LF `73713095…`, CRLF `004d4b77…`. The
CRLF value is the one Stage B recorded. The authoritative check is the `git diff`, which is empty.

B+ does not read, edit, re-register or extend Freeze 1.6, and the Stage B executor is not used.

### N. Closure-2 isolation

No path under `experiments/evaluation/closure2/` or `experiments/stage_benchmark/` is modified, and
`git status` is clean for both prefixes (check `J14`). The S07 commits `dfd60cd` and `231b5ca` are
still reachable in the repository; the S08 worktree, its authorization and its evidence were not
read or written. The three Closure-2 tasks are *consumed* read-only from `catalog.py`.

### O. Authorization

`experiments/stage_b_plus/authorization.json` is `PENDING_OWNER_APPROVAL` with every signature field
`null`, `authorizes_live_execution: false`, and `reuses_s06_or_s08_balance: false`. The gate
(`offline.approval_gate`) requires all of: status `APPROVED`, this exact protocol digest, an approver,
an approval commit, a timestamp, and a non-null cost ceiling. A pending record and a record signed for
another digest are both refused, and both refusals are asserted.

To authorize four canary cells the Owner must fill: `bound_head`, `approved_by`, `approval_commit`,
`approved_at`, `cost_ceiling_usd`, `cumulative_stop_usd`, and `canary_approved`. Setting a ceiling
without the other fields changes nothing.
