# Closure-1C — Metrics and Historical Evidence Closure

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`.

This document does two things and nothing else: it fixes what the *evidence* is allowed to claim, and
it puts every historical number under one honest reading guide. It does not create performance
results, and nothing here was produced by a live model.

| Item | Value |
|---|---|
| `PREVIOUS_APPROVED_SHA` | `5d353d3f13e32941ff54700d5902b8afdbeccee4` |
| Branch | `feat/final-closure` |
| Design contract | `37eba87` — unchanged |
| AnyCode | 0.9.0, unmodified |
| Live model calls this stage | **0** |
| New sandbox runs this stage | **0** |

---

## 1. Metric dictionary

Two measurement layers exist and they are not interchangeable.

### 1.1 Provider layer — `InferenceObservation.usage` (`atlasos.metrics.models.InferenceUsage`)

The adapter's record of **one** provider call, written from the provider's own `usage` object by
`atlasos.integrations.deepseek.parse_usage`.

| Field | Definition | When the provider omits it |
|---|---|---|
| `prompt_tokens` | the provider's reported input-token total | required; a payload without it is rejected (`UsageParseError`) |
| `completion_tokens` | the provider's reported output tokens | defaults to 0 **only** because the field is absent from a completed response |
| `cache_hit_tokens` | `prompt_cache_hit_tokens`, or `prompt_tokens_details.cached_tokens` | **`None` = unknown**, never 0 |
| `cache_miss_tokens` | `prompt_cache_miss_tokens`, else `prompt_tokens − hit` when a hit count exists | **`None` = unknown**, never 0 |
| `cache_hit_ratio` | `hit / (hit + miss)` per call | `None` |
| `latency_ms` | adapter-measured wall clock for that call | always present |

A reported `hit = 0` with `miss = prompt_tokens` is a **real measured zero**. A missing split is
**unknown**. These are different facts and the code now keeps them different:

```
cache_split_status = COMPLETE | PARTIAL | NONE | NO_OBSERVATIONS
```

`PARTIAL` deliberately reports no aggregate hit/miss at all — a partial sum would look like a total.

If a provider's own `hit + miss` does not equal `prompt_tokens`, the numbers are **recorded, not
corrected**, in `prompt_token_consistency {consistent, hit_plus_miss, prompt_tokens, delta}`.

### 1.2 Normalized layer — AnyCode `TokenUsage`

`atlasos.integrations.deepseek._to_token_usage` converts the provider usage into AnyCode's object:

- split reported → `input_tokens = miss`, `cache_read_input_tokens = hit`;
- split absent → `input_tokens = prompt_tokens`, `cache_read_input_tokens = 0`.

**The absence case is indistinguishable from a reported zero-hit case in that object.** So per-request
evidence (`_RequestEvidence.attach_usage`) now records `prompt_tokens`, `completion_tokens` and the
normalized parts, and reports `cache_hit_tokens = None`, `cache_miss_tokens = None`,
`cache_split_known = false`, with `source = "anycode.TokenUsage (normalized by the provider adapter)"`.
It never reconstructs the provider split from ordering, content similarity or process state.

### 1.3 Derived run-level metrics

| Metric | Definition | Missing-data behaviour |
|---|---|---|
| Total prompt tokens | Σ provider `prompt_tokens` | `None` when there are no calls |
| Cache hit / miss tokens | Σ provider hit/miss | `None` unless `cache_split_status == COMPLETE` |
| Estimated cost | hit·hit_rate + miss·miss_rate + completion·output_rate | **`None`** if any call lacks the split, or pricing is incomplete |
| Model requests | number of adapter calls observed by the wrapper | with index-integrity checks (§2) |
| Tool calls | executed tool calls recorded in the run | 0 is a real value |
| Agent latency | `run_tasks` wall clock | `None` if the run never started |
| Total latency | whole sample wall clock, including provision, export and oracle | `None` on early failure |
| Oracle PASS | independent hidden-test verdict | separate from task success |
| Task success | `failure_class == TASK_SUCCESS` **and** oracle passed | — |

Pricing basis for every live historical result: DeepSeek Flash peak rates snapshot **2026-11-06**
(hit $0.006 / miss $0.30 / output $1.20 per 1M tokens, USD), an **upper bound**; off-peak rates are
half. The AB benchmark resolved `deepseek-chat` to `deepseek-flash`.

### 1.4 What a run's numbers cover

| Scope | Included |
|---|---|
| `metrics.prompt_tokens` / `completion_tokens` | every adapter call in the run, all workers |
| `metrics.latency_ms` | provision + agent + export + oracle |
| `metrics.extra.agent_latency_ms` | the agent phase only (C1-B native arm records it explicitly) |
| `metrics.extra.oracle_latency_ms` | the oracle phase only |
| `model_turns` | number of provider calls, **not** agent turns |

Latency intervals from different stages are **not** comparable unless the interval is stated.

---

## 2. Request-evidence corrections (C1-C Part A)

The closure baseline already replaced content-hash identity with trusted execution context. Three
remaining over-claims are fixed here; all are evidence-only and touch no context-selection code.

| # | Defect | Fix |
|---|---|---|
| A1 | `_request_linkage` checked only the consumer requests that *had* a probe, so a run with a correct first and third request and a missing middle one still read as fully verified | every observed consumer request is checked. A missing probe is `PROBE_MISSING_ON_CONSUMER_REQUEST` with the exact `calls_without_probe` list; absent block, wrong injection count and missing body content are reported per call as well |
| A2 | coverage compared counts only, and `ModelRecordingAdapter` recorded `asyncio.CancelledError` as `FAILED` | `_request_index_integrity` requires unique and contiguous `call_index`, unique and contiguous per-task `request_index`, and one terminal state per request; cancellation is `CANCELLED`, a provider error stays `FAILED`, an unconsumed stream records nothing, and a completed request is never relabelled by later generator cleanup |
| A2 | — | `REQUEST_CONTENT_VERIFIED` now requires every integrity check to pass; failures emit `REQUEST_INDEX_INTEGRITY_FAILED` |

### 2.1 What "reconciled" means, and what it does not

`REQUEST_COVERAGE_RECONCILED` is an **adapter-level** reconciliation: requests counted when the
wrapper issues them equal responses counted by the inner adapter, plus failed and cancelled calls,
with indices intact.

It is **not** a network trace. There is no interception, no OpenTelemetry, no distributed tracing and
no request-tracking service, and none may be added for this. A provider that retried internally, or a
call that never reached the wrapper, is outside what this evidence can bound. Claims must say
"within the instrumented adapter path", never "complete end-to-end call coverage".

---

## 3. Effective public task digest (C1-C Part C1)

`BenchmarkTask.fixture_digest` predates multi-worker tasks and **omits `WorkerStep.shared_context`**,
so a changed public requirement could leave the fixture digest untouched. It is frozen: reworking it
would invalidate every recorded run identity.

Added, alongside it and versioned:

```
effective_public_task_digest(task)  ->  sha256(canonical_json(effective_public_task_payload(task)))
EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION = "effective-public-task-1"
```

Payload: sample id, category, mode, whole-task prompt, target, visible files by content, per-worker
(`task_id`, title, objective, `shared_context`, `depends_on`, `max_turns`), allowed/forbidden paths,
required artifacts, tool expectations, MCP server, task `max_turns`.

Excluded on purpose: `hidden_files` (the oracle), `frozen_contracts` (a private routing contract),
`notes` and `tags`. A private grading artifact must never move a "public input" digest.

Both arms call the same function, and both persist it under `environment.task_identity` next to the
unchanged `fixture_digest`:

```json
"task_identity": {
  "fixture_digest": "…",
  "effective_public_task_digest": "…",
  "effective_public_task_digest_version": "effective-public-task-1"
}
```

Consequence for the final benchmark: **an Atlas-vs-native comparison is only admissible when the two
`effective_public_task_digest` values are equal.** Equal `fixture_digest` alone is not sufficient.

---

## 4. Historical experiment inventory

Nothing in this table was produced by C1-C. Each row names its own source; the numbers are quoted,
not recomputed.

| ID | Experiment | Runtime | Task set / n | Model | Report |
|---|---|---|---|---|---|
| E1 | paired single vs multi | `50759ed` | 12 tasks × 3 reps, A0/A1 n=36 | `deepseek-chat` T=0 | `experiments/results/stage-e1-report.md` |
| E2 | context plane Track A | `85b3918` | 8 workflows × 3 reps | `deepseek-chat` | `experiments/results/stage-e2-report.md` |
| G1 | dual-channel handoff | landed `7bd0965` | 3 × 2 × 2, n=6/arm | `deepseek-chat` | `docs/stage-g1-report.md` |
| G2a | run-scoped budget | worktree | 2 × 2 × 2, n=4/arm | `deepseek-chat` | `docs/stage-g2a-report.md` |
| G2b-1 | rolling compaction | worktree | 3 × 2 × 2, n=6/arm | `deepseek-chat` | `docs/stage-g2b1-report.md` |
| G3-1 | tool-error semantics | worktree | 2 × 2 × 2, n=4/arm | `deepseek-chat` | `docs/stage-g3-1-report.md` |
| G3-2 | ingress offload | worktree | 3 × 2 × 2, n=6/arm | `deepseek-chat` | `docs/stage-g3-2-report.md` |
| G3-4a | rebuilt baseline | worktree | 3 × 2 × 2, n=6/arm | `deepseek-chat` | `docs/stage-g3-4a-report.md` |
| R0 | delegation router | worktree | 3 × 3 × 2 | `deepseek-chat` | `docs/stage-r0-routing-report.md` |
| R0.5 | positive delegation | `2183983` | 18 + 12 runs | `deepseek-chat` | `docs/stage-r0-5-positive-delegation.md` |
| R1-1 | ownership enforcement | `4ad5264` | 2 × 2 × 2 × 4 rounds | `deepseek-chat` | `docs/stage-r1-1-worker-ownership-integrity.md` |
| R1-2A | deliverable verification | `e90d53c` | 58 static + 8 dynamic + 8 live | observation only | `docs/stage-r1-2-deliverable-verification.md` |
| R1-2H | oracle isolation | `facca98` | 28 workspaces re-graded | **no model** | `docs/stage-r1-2h-oracle-isolation.md` |
| AB | 30-task benchmark | `18e37f8` | 90 Atlas + 25 baseline | `deepseek-flash` resolved | `experiments/results/eval-final-report.md` |
| Final-3 | 59-module acceptance | `93cbd98` region | deterministic suites | no model / fake | `docs/final-3-module-acceptance-report.md` |
| Final-4 | corrected live matrix | `93cbd98` + evals | 3 tasks × units | `deepseek-chat` | `docs/final-4-evidence-closure-report.md` |
| C1-A | prompt-layout ablation | `052cd30` | 3 workloads × 2 layouts | **no model** | `docs/closure-1a-prompt-layout.md` |
| C1-B | native sandbox acceptance | `c0b3c6f` | N0 + N1, scripted provider | **no model** | `docs/closure-1b-native-baseline.md` |

### 4.1 What each stage actually measured

| Stage | Real model | Real sandbox | Comparable to |
|---|---|---|---|
| E1, E2, G1, G2a, G2b-1, G3-1..4a, R0, R0.5, R1-1, AB | yes | yes (with the pre-G3-4a provisioning bundle for E1…G3-3) | only within its own stage |
| R1-2A, R1-2H, Final-3, C1-A | **no** (or observation only) | mixed | never as agent capability |
| Final-4 | yes, 10 corrected units | yes | its own corrected manifest |
| C1-B | **no** (scripted) | yes | execution wiring only |

---

## 5. Headline results, positive and negative

**All values are quoted from the cited report. Percentages from different rows must not be added.**

### 5.1 Positive or load-bearing

| Result | Value | Source | What it supports |
|---|---|---|---|
| 30-task benchmark | 84/90 success, 6 failures all `ORACLE_FAILURE`, 0 unauthorized effects, 0 host-isolation violations | AB | the runtime executes a real coding benchmark |
| Trajectory vs `result.json` | 591 vs 365 actions (226, 38 %, missing from `result.json`) | R1-0 | why `trajectory/` is a core component |
| Deterministic verification | 66 deliverables, **0 false positives**, 0 model tokens, 8.1 ms static / 4.99 s dynamic | R1-2A | detection with a measured false-positive rate |
| Sandboxed oracle | 28/28 score agreement, ~6 s, 0 model tokens | R1-2H | grading is reproducible off-host |
| Direct-dependency selection | irrelevant blocks injected 2 → 0 | Final-3 A1-03 | selection does what it says |
| Stable rendering | layout equality 0.0 → 1.0 under inverted completion order | Final-3 A1-04 | determinism, **not** a cache claim |
| Progressive disclosure | native schema 8 796 → 4 207 chars (−52.2 %) | Final-3 A2-06 | the tool surface shrinks; see §6.4 |
| Semantic retrieval | 24 tools: R@1 0.80 → 0.90; 64 tools 0.69 → 0.81; 128 tools 0.78 → 0.91 | Final-3 A2-04, Final-4 §4 | retrieval quality at scale |
| Gate / ledger / idempotency | 0 unauthorized executions, 0 replayed effects, 0 blind retries | control plane, M4/M6 | reliable execution invariants |
| One delegated run | 3 workers, oracle PASS, 103 369 input / 6 857 miss tokens, $0.00934 | Final-4 closure §C | the delegate branch really executes |
| Native baseline wiring | `REAL_SANDBOX_ACCEPTED`, `BASELINE_PARITY_PASS`, both containers destroyed | C1-B | a fair native arm now exists |

### 5.2 Negative or neutral — kept, not softened

| Result | Value | Source |
|---|---|---|
| Static multi-worker | 28/36 vs 3/36 success; 6.59× prompt tokens; 4.12× cost | E1 |
| Multi-worker on rebuilt tasks | 6/6 vs 2/6; $0.0142 vs $0.0461 | G3-4a |
| Positive delegation | Track A +40 % cost / +27 % latency; Track B +269 % / +59 % | R0.5 |
| Router | 12/12 candidates rejected, no live delegation | R0 |
| Rolling compaction | prompt +9.3 %, cache miss **+88 %**, cost +43 % | G2b-1 |
| Ingress offload | prompt +3.7 %, cost −0.7 % (noise), `max_turns` workers 0 → 4 | G3-2 |
| ContextPilot dedup | 0 dedups in 72 calls (E2); 31.84 % only with multi-line payloads (Final-4 D0); cache effect workload-dependent (S6) | E2, Final-4, S6 |
| Ownership enforcement | final round 2/4 vs 1/4; across rounds 7/16 vs 1/16 | R1-1 |
| Per-worker fixed overhead | 1 988–2 085 tokens = 4.5–5.2 % of spend | R1-0 |
| Native vs Atlas capability | 20/25 vs 21/25, one repetition — **not** a capability advantage | AB |
| Coding-run recovery | no checkpoint exists; Atlas advantage is post-crash accounting only | Final-2 pilot |

---

## 6. Module evidence table

"Homepage-worthy" means the claim survives §7. Nothing is claimed beyond its own experiment.

| Module | Problem | Mechanism | Evidence | Metric and scope | Limits | Homepage? |
|---|---|---|---|---|---|---|
| **Context Engineering** — selector | every worker received every completed output | `TaskScopedSelector` injects task + declared dependencies only | Final-3 A1-03 | irrelevant blocks 2 → 0, one workload | one deterministic topology | yes, scoped |
| — stable renderer | layout changed with completion order | canonical order + hashes | Final-3 A1-04 | layout equality 0.0 → 1.0 | determinism, not cache | yes, scoped |
| — effective layout (C1-A) | objective duplicated in system + user turn; renderer's system message discarded | `shared_prefix` layout, opt-in | C1-A | first-request chars −7.7…−8.8 %; est. tokens −8.4…−9.7 %; common prefix +1 492…1 934 chars | **initial request only**, no model, no cache | yes, with the limit stated |
| — dedup (ContextPilot) | repeated context blocks | official `contextpilot` transform | E2, Final-4 D0–D3, S6 | 0/72 calls deduped at 880-char single-line blocks; 31.84 % at multi-line payloads; S6 cache effect workload-dependent | payload-shape dependent, not a cache win | **no** |
| — placement (research) | cross-worker context transmission | context-locality assignment | Final-3 A1-06, S6 Table C | logical handoff refs 3 → 0 | logical refs only; not wired in coding; **paused** | no |
| — history compaction (research) | unbounded history | rolling compaction | G2b-1 | prompt +9.3 %, miss +88 %, cost +43 % | negative; off by default | no |
| — tool-result offload (research) | large tool outputs in the prompt | `history.read` ingress | G3-2 | prompt +3.7 %, cost −0.7 %, more `max_turns` workers | neutral; off by default | no |
| **Reliable Execution** — gate + ledger | unauthorised or replayed side effects | action policy, durable ledger, idempotency | control plane, M4/M6 | 0 unauthorised executions, 0 replayed effects, 0 blind retries | deterministic suites, fake tools | yes |
| — sandbox binding | agent code running on the host | provider create/provision/isolation/export | M5, Stage D | 0 host mutations, cleanup confirmed | no CPU/memory isolation claim | yes |
| — suspended approval | silent approval reuse | durable suspension + revalidation | M6 | 0 stale reuses, 0 lost suspensions | fake approver | yes, scoped |
| — trajectory | `result.json` lost 38 % of actions | recorder + input digests | R1-0, R1-1 | 365 → 591 actions, digest coverage 1.00 | 32 runs | yes |
| — verification | "did the deliverable work" without executing it | static + dynamic checks | R1-2A | 66 deliverables, 0 false positives | ~50 % recall; contract coverage thin | yes, bounded |
| — oracle isolation | graded code imported on the host | disposable grading sandbox | R1-2H | 28/28 agreement, 0 model tokens | one re-grade batch | yes |
| — recovery | post-crash accountability | ledger + binding + trajectory evidence | Stage D, Final-2 pilot | durable evidence; **no automatic resume** | no checkpoint in any coding composition | **no** |
| **Progressive tool disclosure** | every tool schema in every request | `tool_search` + `dispatch_action` | Final-3 A2-06, M1 | 8 796 → 4 207 chars (−52.2 %); 2 599 → 1 399 tokens in M1 | tool-definition budget only | yes, scoped |
| **Semantic tool search** | keyword retrieval at scale | embeddings over the catalog | Final-3 A2-04, Final-4 §4 | R@1 0.80→0.90 (24), 0.69→0.81 (64), 0.78→0.91 (128) | offline retrieval; one catalog | yes |
| **Adaptive delegation** | static decomposition loses | router with frozen policy `r0.1.0` | R0, R0.5, Final-4 | 12/12 rejected (R0); 6/6 admitted, 0 false (R0.5); one real delegate run | no benchmark-level win | yes, honest |
| **Evaluation & observability** | results were not attributable | request evidence, per-call records, metrics contract | Final-4, C1-A, C1-B, this stage | `REQUEST_CONTENT_VERIFIED` on the corrected units; index integrity; versioned public digest | adapter-level, not a network trace | yes |
| **Native baseline** | no fair comparison existed | neutral lifecycle + six neutral tools | C1-B | parity PASS, oracle PASS in both arms, containers destroyed | no model run yet | yes, as method |

Research mechanisms — placement, history compaction, tool-result offload, ownership enforcement —
keep their implementations, their historical evidence and their **off** default. None is described as
an end-to-end performance improvement, because none produced one.

---

## 7. Metric anti-misuse rules

Each of these is a sentence that must never appear in a README, slide or summary.

| Forbidden claim | Why |
|---|---|
| "tool schema −52 % ⇒ agent tokens −52 %" | that is the tool-definition budget only; prompt tokens are dominated by conversation and tool results |
| "dedup −44 % chars ⇒ cost −44 %" | character reduction is not billed-token reduction, and S6 shows the cache effect can invert |
| "C1-A −8 % tokens ⇒ run −8 %" | C1-A measures the **first request** of each worker with a tokenizer estimate; no model, no multi-turn run |
| "stable prefix of N chars ⇒ N chars of KV cache hit" | prefix length is a serialisation property; provider cache state is measured only by provider-reported hit tokens |
| "C1-B scripted PASS ⇒ the model solves the task" | the provider was a script replaying a known solution |
| "placement 3 → 0 handoffs ⇒ cache miss −X" | logical reference counts, not provider cache events |
| "one successful sample ⇒ improvement" | R0.5 and AB both warn against single-repetition conclusions |
| "cache hit ratio 0.90 ⇒ 90 % cheaper" | hit and miss tokens have different prices, and the ratio's denominator differs by stage |

### 7.1 Historical field-name collisions

The same word means different things across stages. Recorded here, not silently normalised:

| Term | Where it differs |
|---|---|
| `prompt_tokens` | G2a includes cache hits; AnyCode `TokenUsage.input_tokens` excludes them; the provider total includes them |
| "cache hit ratio" | E1/E2: run aggregate; G3-2: token-weighted, median-run, and excluding-first-call variants |
| "success" | oracle + harness; a budget stop can fail a run whose artifacts the oracle accepts |
| `latency_ms` | stage-dependent: some rows include provision/export/oracle, some only the agent phase |
| `estimated_cost_usd` | always an upper bound from the 2026-11-06 peak snapshot; AB used the Flash rates |

Where a conversion cannot be made exactly, the row is marked **not comparable** rather than
converted. The §4 table's "comparable to" column is that marking.

### 7.2 Environment differences that block cross-stage comparison

1. Model: `deepseek-chat` everywhere except AB, which resolved to `deepseek-flash`.
2. Sample size: n=2/cell (R0, R0.5, G3-3, G3-4a) to 90 runs (AB).
3. Runtime: before G3-4a the sandbox kept a provisioning bundle and a fixture-identifying commit.
4. Mechanism configuration: handoff truncation vs dual channel; guarded budget; compaction/offload/
   contracts on only in their own stage.
5. Benchmark revisions: two R0.5 versions and two R1-2H batches were voided.
6. V1 context plane (S6, E2) and the V2 coding adapter (E1, G*, R*) are different prompt paths.

---

## 8. What C1-C changed, in one table

| Area | Before | After |
|---|---|---|
| Consumer dependency evidence | requests without a probe were skipped | every observed consumer request must carry the probe; missing ones are named |
| Coverage | count reconciliation only | plus unique/contiguous indices and one terminal state per request |
| Cancellation | `asyncio.CancelledError` recorded as `FAILED` | recorded as `CANCELLED` |
| Per-request cache split | `hit > 0` claimed as "provider reported a split" | always unknown at the normalized layer; the split lives at the provider layer |
| Aggregate cache split | a zero could be read as a measured 0-hit run | `COMPLETE` / `PARTIAL` / `NONE` / `NO_OBSERVATIONS`, with cost `None` when incomplete |
| Public task identity | `fixture_digest` silently omitted `shared_context` | versioned `effective_public_task_digest` alongside it; `fixture_digest` byte-identical |

No prompt layout, worker factory, selector, gate, ledger, sandbox, router or native-baseline
execution semantic was modified.
