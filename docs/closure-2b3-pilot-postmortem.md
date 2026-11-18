# Closure-2B-3 — Evidence-Driven Postmortem of the 8-Cell Live Pilot

Status: analysis only. No provider call, no sandbox, no re-run, no protocol change.
Evidence base: the single authorized execution, attempt `f862981c20a24fdf881884b9fbd891cc`,
plan `0f61f84a…b2e32bb`, run at commit `18646fd`.

Every claim below carries its basis: **CONFIRMED** (read directly from persisted evidence),
**INFERRED** (mechanism follows from several confirmed facts but is not directly recorded),
**UNKNOWN** (not observable from the evidence that exists).

---

## 0. Provenance

| Item | Value |
|---|---|
| Analysis branch | `analysis/closure-2b3-pilot-postmortem` off `7ee9bef` |
| Pilot branch / execution commit | `exec/closure-2b2-official-8cell-pilot` / `18646fd` |
| Summary | `experiments/results/closure2/pilot-live-summary.json` (commit `7ee9bef`) |
| Raw runs | `experiments/runs/closure2/closure2-final-benchmark-v1/<task>/r0-<run_id>/` (git-ignored) |
| Claim ledger | `%LOCALAPPDATA%\AtlasRuntime\closure2\pilot-ledger.sqlite`, 1 claim `CONSUMED`, 8 cells `COMPLETED` |

## 1. Evidence inventory

| Cell | Raw result | Oracle log | Worker trace | Provider usage | Completeness |
|---|---|---|---|---|---|
| P1 N0 `native-9d338af9d6f9` | result.json | oracle.log + oracle_sandbox.json | inline `worker_results` (1) | aggregate only, `provider_calls=null` | thin (native shape) |
| P1 N1 `native-ced3e3e00c56` | result.json | yes | inline `worker_results` (3) | aggregate only | thin (native shape) |
| P1 A0 `eval-f5cdfa39b334` | result.json, run.json | yes | `workers.json`, `stage_e_evidence.json`, `tool_conversations.json`, `trajectory/` | `provider_calls.jsonl` (7) + `context.json` | full |
| P1 A1-L `eval-2fd62b944d76` | yes | yes | yes | `provider_calls.jsonl` (25) | full |
| P1 A1-P `eval-84bb349e25dd` | yes | yes | yes | `provider_calls.jsonl` (32) | full |
| P1 A2-P `eval-5780b6436b8e` | yes | yes | yes | `provider_calls.jsonl` (26) + `context.json.routing` | full |
| P2 A1-P `eval-4b200d5e3e68` | yes | yes (1 failed / 20 passed) | yes | `provider_calls.jsonl` (33) | full |
| N1 A2-P `eval-103a525745e8` | yes | yes | yes | `provider_calls.jsonl` (8) + `context.json.routing` | full |

Native arms write `result.json`, `workspace.patch`, `fixture/`, `workspace/`, `oracle.*` only. They have no
`run.json`, `context.json`, `provider_calls.jsonl`, `stage_e_evidence.json`, `workers.json`,
`tool_conversations.json`, `trajectory/` or `state/`. Nothing was fabricated to fill the gap.

## 2. Results verification

Aggregates reproduce exactly from per-request evidence: 8 cells, 162 requests, 898,759 prompt /
42,694 completion, 805,504 hit / 93,255 miss (0.8962), USD 0.08404231, integrity `COMPLETE`,
`not_started=[]`, `stopped_reason=null`, one attempt id across all eight cells.

Per-call cost recomputed from the frozen price table (hit 0.006/M, miss 0.30/M, output 1.20/M)
matches the recorded `cost_usd` to 1e-8 for both layout arms, and both arms' sums match the summary.
`hit + miss == prompt` holds on all 131 Atlas request records (the remaining 31 of the 162 counted
requests are native `model_turns`, which have no request records). (CONFIRMED)

## 3. Investigation A — `closure2-plugin-migration::A1-P`

### 3.1 What the worker was given (CONFIRMED)

`workers.json` → `eval-4b200d5e3e68-csv.assignment.task` carries an explicit assignment: objective,
owned modules (`plugins/csv_plugin.py`, `plugins/csv_fields.py`), "read them, do not edit them" for the
other modules, and a definition of done ("no `NotImplementedError`, `pytest -q test_public.py` passes
for the CSV checks, report what you ran"). The worker contract is therefore explicit about deliverables;
`metrics.worker_contract` records `mode=legacy`, `overhead_chars=0`, `shared_context_chars=2562`,
`task_text_chars=3581` for this worker.

### 3.2 Timeline (CONFIRMED from `tool_conversations.json`)

| Turn | Tools | What happened |
|---|---|---|
| 1 | bash, file_read | explore project |
| 2–3 | 4 + 6 file_read | read the whole fixture |
| 4 | bash | `git status`, python version |
| 5 | file_write | implement `csv_fields.py` |
| 6 | file_write | implement `csv_plugin.py` |
| 7 | bash | ad-hoc CSV edge-case checks — all correct |
| 8 | bash | run public suite |
| 9 | bash | "All 4 CSV-specific tests pass; 3 remaining failures are registry/loader" |
| 10 | bash | check `git diff` — other workers' files visible, mine confined |
| 11 | bash | clean `__pycache__`, re-verify |
| — | — | run terminated: `stop_reason = max_turns` (12), `output = ""` |

11 assistant turns, 20 persisted `tool_use` blocks, and a trailing tool result with no answering
assistant message. **No invalid loop, no drift**: the last turns are verification and workspace hygiene,
not thrash. Every tool result in the transcript is error-free. (CONFIRMED)

Accounting discrepancy: the runtime counted 21 tool calls for this worker, the transcript persists 20.
The missing call has no assistant block and no result — consistent with the turn that was cut off.
(INFERRED)

### 3.3 Why there was no output (CONFIRMED)

The worker spent its final turns on verification and cleanup and never emitted a terminal message; the
runtime stopped it at the turn cap. The deliverable itself was written at turns 5–6 and was complete:
`required_artifacts_present = true` in the oracle, and the workspace contains both owned files
(`csv_fields.py` 1,213 B, `csv_plugin.py` 1,247 B).

### 3.4 Other workers and integration (CONFIRMED)

- `json`: success, 2,424 output chars. `register`: success, 3,706 chars.
- The integration worker never mentions a missing worker; it reports "the full public suite passes"
  (its own run: `9 passed`) and its ad-hoc transcript even prints
  `json multi: … {'key': 'b', 'value': 'True'} …` — it observed the offending value and did not flag it.
- `oracle.checks.all_workers_produced_output = false` and
  `details.worker_contract = {expected 3, successful [json, register], csv output_chars 0}`: the oracle
  **did** detect the missing output.

### 3.5 Root cause of the oracle failure (CONFIRMED)

`plugins/json_fields.py::_stringify` returns `"True"` / `"False"` for booleans:

```15:18:experiments/runs/closure2/closure2-final-benchmark-v1/closure2-plugin-migration/r0-eval-4b200d5e3e68/workspace/plugins/json_fields.py
    if value is True:
        return "True"
    if value is False:
        return "False"
```

The hidden test requires JSON-style lowercase (`{"key": "ok", "value": "true"}`). The workbook's
`docs/INTERFACE.md` says only "numbers and booleans become their string form" — it does **not**
disambiguate Python `str(True)` from JSON `true`, and `test_public.py` contains **no** boolean or
JSON-value-stringification case at all. The worker made a deliberate, documented choice
(its own report lists `'True'`/`'False'` as expected output) that the hidden suite rejects.

### 3.6 Causal relationship to the turn cap (CONFIRMED)

None. Two independent defects, each sufficient to fail the cell:

1. `all_workers_produced_output = false` — csv's report is missing (turn cap).
2. `hidden_tests_passed = false` — `json_fields._stringify` violates the hidden expectation, in a file
   owned by the `json` worker, which succeeded and reported the behaviour as correct.

`failure_reasons` lists both. Fixing either alone would not have passed the cell. Treating
`max_turns` as the root cause of the `True`/`true` divergence would be wrong.

## 4. Investigation B — shared-prefix cost decomposition

### 4.1 Per-request evidence (CONFIRMED)

| | A1-L | A1-P |
|---|---:|---:|
| requests | 25 | 32 |
| prompt tokens | 146,730 | 199,442 |
| hit / miss | 126,720 / 20,010 | 182,144 / 17,298 |
| completion | 6,327 | 8,333 |
| distinct `stable_prefix_hash` | **3** | **1** |
| mean prompt per request | 5,869 | 6,233 |
| mean miss per request | 800 | 541 |
| miss share of prompt | 13.64% | 8.67% |
| mean provider latency | 1,546 ms | 1,673 ms |
| cost per request | $0.000574 | $0.000509 |

The layout took effect exactly as frozen: legacy produced three distinct stable prefixes (each worker
paid its own cold start — first calls at request 0, 1 and 18 with hit = 128), while shared-prefix
produced one prefix for all 32 calls, so workers 2 and 3 started warm (2,176 hit tokens on their first
call, requests 1 and 24). `context.json.resolved.workers` confirms it statically: three distinct
`original_worker_context_hash` values but one identical `effective_system_prompt_hash`.

### 4.2 Cost decomposition from frozen prices (CONFIRMED)

`Total = hit·0.006 + miss·0.30 + completion·1.20` per million tokens:

| Component | A1-L | A1-P | Δ |
|---|---:|---:|---:|
| cache-hit input | 0.00076032 | 0.00109286 | **+0.00033254** |
| cache-miss input | 0.00600300 | 0.00518940 | **−0.00081360** |
| completion | 0.00759240 | 0.00999960 | **+0.00240720** |
| total | 0.01435572 | 0.01628186 | **+0.00192614** |

At the same request count, the shared-prefix arm would have cost
`0.000508 × 25 = $0.012720` — **11.4% cheaper** than the legacy arm. It cost more only because the run
issued 28% more requests.

### 4.3 Where the extra tokens came from (CONFIRMED)

Per-worker `stage_e_evidence.json` counters reconcile exactly: per-worker `input_tokens` sums to the
cell's cache-**miss** tokens and per-worker `output_tokens` sums to completion tokens in all five Atlas
cells (and in native N1). Per-worker `cache_read_input_tokens` is always 0, i.e. that counter does not
see provider cache reads.

| Worker | A1-L miss / completion | A1-P miss / completion |
|---|---|---|
| parse | 5,888 / 2,059 | 6,732 / 3,194 |
| aggregate | 6,750 / 2,405 | 4,700 / 2,918 |
| integrate | 7,372 / 1,863 | 5,866 / 2,221 |
| total | 20,010 / 6,327 | 17,298 / 8,333 |

The miss saving is concentrated in `aggregate` and `integrate`; the completion increase is spread over
all three workers (+1,135 / +513 / +358). Output tokens alone (+$0.00241) are 1.25× the entire total
delta.

### 4.4 Answer

**Shared-prefix lowered cache miss because it collapsed three cold prefixes into one and cut
miss-per-request by a third (800 → 541 tokens) — and it did make each request 11.4% cheaper. It did
not lower total cost, because the same task was solved with 32 requests instead of 25 and 31.7% more
output tokens. The saved miss cost was $0.00081; the added output cost was $0.00241.**

Context inflation across turns is present in both arms and is not a layout effect: legacy ramps
2,844 → 8,033 tokens per worker (three independent ramps), shared-prefix ramps 2,579 → 8,680 over the
interleaved series. (CONFIRMED for the values; the causal split is UNKNOWN.)

Caveat held: with one run per arm the 28% request spread cannot be attributed to the layout. Prompt
layout changes all three workers' system prompt, so it *could* change behaviour; nothing in this
evidence separates that from ordinary model variance. Treat the mechanism (miss collapse) as
established and the magnitude (cost) as not yet reproducible.

## 5. Investigation C — selective routing

### 5.1 Was the router called, and on what evidence (CONFIRMED)

Yes. `context.json.routing` persists the full decision: `mode=selective`, `proposal_id`,
`candidate_worker_count`, every gate with status and reason codes, `policy_version=r0.1.0`,
`policy_digest=5e13c910…` (the frozen digest), `route_digest`, and `execution_mode`.

| | P1 pipeline | N1 state-machine |
|---|---|---|
| candidates | 3 (parse, aggregate, integrate) | 2 (machine, callers) |
| GATE_1 necessity | pass, `INDEPENDENT_DELIVERABLES` | info, `NO_PARALLEL_WAVE` |
| GATE_2 coupling | pass, `DATA_DEPENDENCY` | **block**, `DATA_DEPENDENCY`, `SHARED_WRITE_RISK`, `UNSTABLE_INTERFACE` |
| route | `delegate_parallel` | `direct` |
| max parallel wave width | 2 | 1 |
| executed | 3 workers, `execution_mode=a1` | 1 worker, `execution_mode=a0` |

Both reason-code sets match the frozen offline expectation exactly.

### 5.2 Why delegation was admitted on P1 and refused on N1 (CONFIRMED)

The distinction is the coupling gate, not the task size: P1's three tasks own disjoint modules
(`DATA_DEPENDENCY` recorded but passing), while N1's two candidate tasks both write the lifecycle state
module (`SHARED_WRITE_RISK`) through an interface that is still moving (`UNSTABLE_INTERFACE`), and it
has no parallel wave.

### 5.3 What `direct` did and did not avoid (CONFIRMED / UNKNOWN)

- Avoided: fan-out to 2 candidate workers; one `solo` worker took the union assignment (2,524 chars,
  both module regions mentioned) instead of two partial assignments.
- Measured cost of the direct cell: 8 requests, 34,068 prompt, 3,348 miss, $0.00371 — the cheapest cell
  in the pilot, but there is **no A1-P cell on the same task**, so no counterfactual. The saving is
  UNKNOWN.
- Note: direct did **not** remove the assignment framing — the solo worker still received a
  `[YOUR ASSIGNMENT]` block. Routing changed the fan-out, not the prompt shape.

### 5.4 Does routing cost prompt tokens or latency (CONFIRMED)

No measurable cost. A1-P and A2-P on P1 have byte-identical resolved configs apart from run-scoped
worker ids (`workers | same hashes: True`, same layout `shared_prefix`, same policy `task_scoped`), and
the route resolved to the same 3-worker path. The 32 → 26 request difference is therefore variance, not
routing, and must not be reported as a routing gain.

### 5.5 Concurrency and scheduling timing (CONFIRMED / INFERRED / UNKNOWN)

- CONFIRMED: all three workers of every multi-worker cell were created within 3–4 ms of each other
  (`workers.json.created_at`) — one dispatch wave, no serial spawn.
- INFERRED: request interleaving. In A1-P the request series advances in near-identical pairs
  (2,579/2,567, 3,519/3,507, 4,639/4,627 …), and the third worker joins at request 24 — the signature of
  two workers alternating under one prefix.
- UNKNOWN: actual overlap fraction. `provider_calls.jsonl` has `latency_ms` but **no timestamps**, and
  `worker_id`/`task_id` are `UNAVAILABLE` for all 90 records. Per-worker start/end times do not exist.

## 6. Investigation D — native vs Atlas measurement audit

| Dimension | Native (N0/N1) | Atlas (A0/A1-L) | Verdict |
|---|---|---|---|
| request count | `model_turns` only; `provider_calls=null` | `model_turns` **and** `provider_calls` equal (7/7, 25/25) | Comparable for single-worker; for multi-worker native cannot be reconciled per request |
| cache split | hit/miss present and summing to prompt, `cache_split_status=null`, `cache_hit_ratio=null` | split `COMPLETE`, ratio present | Comparable within a caveat: native's split is unvalidated by the runtime |
| latency boundary | `latency_ms` == `finished−started`, contains `provision_latency_ms` (12,114 ms N0) | same boundary, `provision` 7,261 ms (A0) | Comparable, but provisioning differs by ~4.8 s and is inside the measured latency |
| worker count | `environment.worker_count` + `worker_results` (3 for N1) | `context.json.resolved.worker_count` + `worker_results` | Comparable |
| per-worker tokens | present, sums to miss/completion | present, sums to miss/completion | Comparable |
| budget | `post_hoc` verdict, no guard | `guarded`, admission rejections recorded (all 0) | **Not comparable**: native enforces nothing, Atlas gates each request |
| oracle | `oracle_contract_digest 53e4cd1a…`, `scorer_version oracle-1`, `execution_mode sandbox` | identical contract digest, same scorer, same isolation | Comparable; multi-worker arms add two checks |
| model provenance | `resolved_models=['deepseek-flash']`, no `requested_model`, no `pricing_basis` | same resolved model plus `requested_model=deepseek-chat` and the *"upper-bound estimate"* pricing note | Comparable model, asymmetric provenance |
| cost | reproduced from frozen prices, but no pricing-basis string | reproduced, basis string present | Comparable with caveat |

Overall: **Comparable With Caveats**. Single-vs-single (N0/A0) and multi-vs-multi (N1/A1-L) are
comparable on success, tokens, cache split and cost; latency comparisons carry a provisioning
difference of ~5 s; budget verdicts are not comparable; native per-request traces do not exist.

## 7. Evidence gaps and methodological risks

1. No per-request worker attribution and no request timestamps → parallel overlap, per-worker cost and
   handoff timing are UNKNOWN (P1).
2. Multi-worker oracle adds `all_workers_produced_output` and `distinct_workers_executed`, so
   `task_success` is not the same predicate across topologies (P0 for cross-topology success claims).
3. `docs/INTERFACE.md` does not disambiguate boolean stringification and no public test covers it — the
   only oracle failure in the pilot is partly a specification gap (P0 for benchmark fairness).
4. n = 1 per cell: no significance, no intervals. Every difference above is a single observation.
5. Cost is an upper-bound estimate (flash peak rates, `deepseek-chat` alias resolution), and billed
   cost is not exposed (`observed_cost_usd_total=null`).
6. Native arms lack the runtime's own split validation, so a native cache-split error would go
   unnoticed.
7. One tool call (21 counted vs 20 persisted) is unattested in the csv transcript.
8. Five unit tests still assert a pending approval default and fail against the now-approved history;
   they are stale assertions, not runtime defects (they never reached a real engine — every
   `run_live_pilot` call in the suite passes a stub executor).

## 8. Next experiment design

Two options. Neither may reuse the pilot's plan digest by editing frozen content.

### Plan A — minimal paired supplement (8 cells, ~$0.10)

Purpose: make the three comparison pairs that the pilot left unpaired, at the lowest cost.

| # | Task | Arm | Rep | Why |
|---|---|---|---|---|
| 1 | plugin-migration | A1-L | 0 | the missing legacy counterpart of the failed cell |
| 2 | plugin-migration | A1-P | 1 | paired re-test of the same layout question with a second observation |
| 3 | state-machine | A1-P | 0 | the missing multi counterpart for the `direct` decision |
| 4 | state-machine | A2-P | 1 | direct again, second observation |
| 5 | pipeline | A1-L | 1 | layout pair, second observation |
| 6 | pipeline | A1-P | 2 | layout pair, second observation |
| 7 | plugin-migration | A1-P | 2 | third observation on the only failing task |
| 8 | pipeline | A2-P | 1 | router delegate again, to see request-count spread on an identical config |

Answers: is the A1-L/A1-P cost relationship stable across two observations? does `direct` beat multi on
the same task? how much does request count vary on a byte-identical config?
Cost basis: 3-worker cells observed at $0.0128–$0.0163, single-worker at $0.0037–$0.0052 → **$0.08–$0.15**.
Needs a new plan id, new digest and new owner approval; cannot be presented as a reproduction of the
frozen 8-cell pilot.

### Plan B — full formal benchmark (54 cells, ~$0.45–$0.90)

3 tasks × 6 arms × 3 repetitions, executed under one new plan digest and one new one-shot claim.
Answers: the frozen research questions with n = 3 per cell (direction and spread, not significance).
Cost basis: per-cell means from the pilot (single $0.0050, legacy multi $0.0125, shared-prefix multi
$0.0155, A2-P mix $0.0098) → **≈$0.56** central estimate, $0.45–$0.90 allowing 1.5× spread.
Pairing: within each task and repetition, all six arms are paired by construction; `A2-P` on
state-machine is a single-worker cell, so any multi-vs-direct claim must use that cell's own topology.
Statistics: report per-cell values, medians across three repetitions and paired directions; no p-values.
Isolation: new plan id, new digest, new claim, `--runs-root` under a new benchmark id, so the pilot
directories and summary are never overwritten.

## 9. Recommendations

P0 (credibility of the experiment):
1. Disambiguate boolean/scalar stringification in `docs/INTERFACE.md` and add one public test per
   hidden expectation class, so a hidden test cannot penalise a defensible reading of the public
   contract. Requires a new preregistration digest — do not silently edit the frozen doc.
2. Record topology alongside `task_success`, or drop the two extra multi-worker checks from the pass
   predicate, so cross-topology success rates are comparable.

P1 (next round's measurement):
3. Emit a timestamp per provider call and a real `worker_id`, so parallel overlap and per-worker cost
   become measurable instead of inferred.
4. Make per-worker `token_usage.cache_read_input_tokens` either populated or explicitly `UNAVAILABLE`
   rather than 0.
5. Persist `requested_model` and `pricing_basis` in the native arm too.
6. Repair the five stale pending-approval tests.

DEFER: handoff accounting (never engaged: `mode=legacy`, all counters zero), routing latency
(no measurable cost observed), context compaction (off in every arm).

## 10. Decision

`POSTMORTEM_COMPLETE` — the pilot's data is internally consistent and its one failure is explained by
two independent mechanical causes. The layout question is answered mechanistically but not yet
reproducibly, and the routing question is not answerable without the missing pairs. No further live
execution in this round.
