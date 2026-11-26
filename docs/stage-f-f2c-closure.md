# Stage F — F2-C Final Production Closure and Freeze

**Status:** `STAGE_F_UNIFIED_96_CELL_EVALUATION_READY_FOR_LIVE_AUTHORIZATION`

Baseline: `feat/stage-f-unified-evaluation` @ `0b466ba991023987c68b00792494c7e97bfe903a`.
This stage runs nothing against a provider: no model call, no probe, no production claim, no live
cell. The real sandboxes it opened were local OpenSandbox containers used to confirm the eight-task
environment and the four-arm compatibility smoke, and every one of them was destroyed.

The record of the gate is `experiments/stage_f/preflight.json`; the identities are in
`experiments/stage_f/freeze.json`, `manifest.json`, `plan-b{1,2,3}.json` and
`authorization-b{1,2,3}.json`.

---

## A. Git provenance

| | |
|---|---|
| Branch | `feat/stage-f-unified-evaluation` |
| Head at the start of the stage | `0b466ba991023987c68b00792494c7e97bfe903a` |
| F2-A proof head | `8bbe7a73194a7f97ea692482c70022db4cd58d5d` |
| F2-A evidence commit | `56f5b5784c1a69085d1d21129e0c5e871180f71e` |
| F2-B construction commit | `315bc0d705f6c2a4a071451ba7a289ffc064f27d` |
| F2-B report commit | `0b466ba991023987c68b00792494c7e97bfe903a` |
| F2-C code and evidence | this commit (see §S below for the SHA) |

No history was rewritten, rebased or force-pushed. The only pre-existing files touched outside
`experiments/stage_f/` are the executor, the shared fixture builder and two test files (see §I).

## B. F2-A / F2-B evidence identity

| Record | Identity | What it proves |
|---|---|---|
| `experiments/stage_f/runner-proof.json` | plan `stage-f-runner-proof-v1`, digest `sha256:18c288973afe21fb9b09f4c78593c7cac8b7d272be4b1f7f473b1c31a670bfb7`, head `8bbe7a7` | four arms through the real entries on real OpenSandbox containers with a deterministic provider: 4/4 PASS, 27,376 tokens, USD 0.011317, 0 external requests, 0 claims |
| `experiments/stage_f/task-qualification.json` | `STAGE_F_TASK_QUALIFICATION_PASS` at `315bc0d` | eight tasks, 20/20 mutations detected by the independent oracle, static-DAG and leak checks |
| `docs/stage-f-f2b-task-qualification.md` | report at `0b466ba` | the task construction and its declared exception |

Both records are read as they are. F2-A's task identity is the P1 fixture that existed at
`8bbe7a7`; the F2-C compatibility smoke in §H is a **separate** record of the new formal P1 and does
not overwrite it.

## C. Production provider usage: where the numbers come from

| Arm | Source of the counters | Attribution |
|---|---|---|
| A0 native | `result.metrics.per_request_usage` (per request, from `InferenceObservation.usage`) plus `metrics.prompt_tokens/completion_tokens/cache_*` | `single_agent`; Main fields `NOT_APPLICABLE` |
| A1 / A3 | `session.metrics.main_context_tokens_by_request`, `main_*` counters from `UsageAttributionHook`, child totals from `delegation.totals` | `main_child`; Main and children separately |
| A2 static DAG | `metrics.request_evidence.records` (one row per observed request, with `trusted_task_id`) plus `metrics.prompt_tokens/completion_tokens/cache_*` | `dag_all_children`; no Main exists |

Rules that are enforced in code, not just documented:

- the cache-hit tokens are part of the prompt total and are never added a second time;
- a counter the record does not carry stays `None` and becomes `UNKNOWN`, which stops the cell;
- `system_max_agent_context` is the maximum over **every** agent, from a per-agent census. A3 with
  a child whose peak was not observed reports `UNKNOWN` and names the child; the Main peak is never
  promoted to a system peak. A0's single agent is attributed explicitly (the native provider does
  not carry a trusted task id, and there is exactly one agent);
- a request count that exists but was not recorded is `UNKNOWN`; a role the arm does not have is
  `NOT_APPLICABLE`.

The per-request cache split is **not** reconstructed. `TokenUsage` cannot distinguish a reported
zero-hit split from an unreported one, so the per-request row says `cache_split: UNKNOWN` and the
run-level split comes from the provider observation where it exists.

## D. Pricing contract

| Field | Value |
|---|---|
| Model | `deepseek-chat`, which resolves to `deepseek-flash` |
| Currency | USD |
| Cache-hit input | 0.006 per million tokens |
| Cache-miss input | 0.3 per million tokens |
| Output | 1.2 per million tokens |
| Snapshot date | 2026-11-06 (peak rate; off-peak is half) |
| Source | `experiments/configs/eval-benchmark.yaml` (`pricing:` block) |
| Identity | `pricing.digest = sha256:9ac617968f995d902359408362e1bf669abf6ea3dc3a7520483121692f6e0dd6` |

Two distinctions the report keeps: this is an **estimate from a frozen snapshot**, not a bill; and
when a run carries no cache split, the whole input is priced as a miss. Nothing here is carried over
from an earlier DeepSeek Flash price list by assumption — the block above is the file the repository
already froze, read through `cost.stage_b_estimate.pricing()`.

## E. Four-arm execution integrity

The same eleven checks run for every arm and decide the verdict:

`run_identity`, `frozen_fixture`, `raw_evidence`, `resource_accounting`, `final_export`,
`path_policy`, `host_separation`, `oracle_binding`, `oracle_isolation`, `cleanup`,
`failure_measurable`.

Every one is `PASS` for the four smoke cells. Architecture-specific rows
(`atlas_ownership_enforcement`, `atlas_delegation_plane`) are recorded as `NOT_APPLICABLE` where the
mechanism does not exist, and they never cancel a common check. A common check that cannot be
evaluated is `UNKNOWN`, which is a stop (`EXECUTION_INTEGRITY_FAILED`), not a pass.

Delivered success is redefined accordingly:

```
delivered_success = oracle PASS
                    AND normal agent completion
                    AND execution integrity PASS
                    AND public tests not FAIL
```

Consequences visible in this stage's own records:

- A0 no longer reports `execution_integrity = NOT_APPLICABLE`; it reports the common verdict, and a
  missing integrity report cannot produce a delivered cell.
- A2 records `ownership = off` and the plan-level disjointness check; the report never says the DAG
  enforced write ownership at run time.
- A2's public suite is `NOT_CONFIGURED` on that path. It is reported, not hidden: the static-DAG
  entry runs no public-test tool.
- `duration_s` is now a real measurement on all four arms, and the latency block separates agent
  execution, provisioning, export, oracle, cleanup and end-to-end where the arm records them;
  a stage the arm does not time is `UNKNOWN`.

## F. Cross-batch guard: wiring and verification

`experiments.stage_f.cumulative.settlement` is now consulted by `executor.dispatch` **before the
one-shot claim** for the three Stage F batch plans. The canonical layout is one root:

```
<stage-f-root>/<plan_id>/matrix-report.json
<stage-f-root>/<plan_id>/cumulative-settlement.json
<stage-f-root>/<plan_id>/{cells,ledger.jsonl,dispatch.jsonl}
```

The settlement refuses when a prior report is missing, a prior claim was never consumed, the prior
report names another plan digest, the prior batch ran another executor identity, the usage is not
`COMPLETE`, the prior batch stopped instead of completing, or the global remainder is exhausted. The
effective ceiling is

```
cost_stop  = min(frozen batch allowance, owner authorization, global remaining)
token_stop = min(frozen batch allowance, owner authorization, global remaining)
```

and it is the value the dispatch loop compares against, not a number that only appears in the
report (`report.effective_stops.applied`).

`experiments/stage_f/settlement-check.json` records ten scenarios, each through the real dispatcher:

| # | Scenario | Result |
|---|---|---|
| 1 | b1 → b2 → b3 in one root | 32/32 each, each later batch settled on its predecessors |
| 2 | prior report missing | refused, `CUMULATIVE_SETTLEMENT_REFUSED`, no claim written |
| 3 | prior claim never consumed | refused |
| 4 | prior report from another plan digest | refused |
| 5 | prior usage `UNKNOWN` | refused, `unknown_consumption = true` |
| 6 | prior batch stopped (`COST_STOP_REACHED`) | refused |
| 7 | 90 M of 96 M consumed | batch 2's ceiling becomes 6 M, not 32 M |
| 8 | refusal is the dispatcher's own | stop reason present in the refused report |
| 9 | refusal consumes no production claim | claim path absent |
| 10 | effective allowance reaches the loop | `effective_stops.applied.token_stop == 6,000,000` |

A measured task failure in a prior batch does **not** block the next batch; the frozen stop
conditions, unknown consumption, identity mismatch and missing evidence do.

## G. Eight tasks: final environment qualification

`experiments/stage_f/environment-qualification.json` — one real container per task, created and
destroyed:

| Task | Seed public | Reference public | Reference hidden |
|---|---|---|---|
| `closure2-pipeline-migration` | 1 (fails) | 0 | 0 |
| `closure2-state-machine-refactor` | 2 (declared collection failure) | 0 | 0 |
| `f-p1-connector-migration` | 1 (fails) | 0 | 0 |
| `f-p2-protocol-adapter` | 1 (fails) | 0 | 0 |
| `f-x1-api-compat-audit` | 1 (fails) | 0 | 0 |
| `f-x2-distributed-diagnosis` | 1 (fails) | 0 | 0 |
| `f-c1-config-lifecycle` | 1 (fails) | 0 | 0 |
| `f-c2-scheduler-retry` | 1 (fails) | 0 | 0 |

Eight containers created, eight destroyed, no cleanup failure. The same record also verifies the
frozen seed digest, that the seed contains no hidden suite and no reference body, and that every
path the reference writes is allowed and none is forbidden.

The first attempt found a real defect, not a test problem: `prepare_fixture` materialised only
`task.seed`, but six formal tasks keep `test_public.py` in `task.public_tests`. The public suite was
therefore absent from the agent's workspace and the frozen public-test command exited with a pytest
usage error (`file or directory not found: test_public.py`) — a cell could never run the public
suite it is required to run. The fixture now materialises the seed **plus** the declared public
suite, and `prepare_fixture`'s frozen digest covers the same set. The visible set for the two
Closure-2 tasks is unchanged, because their seed already carries `test_public.py`.

## H. New P1 four-arm compatibility smoke

`experiments/stage_f/p1-compat-smoke.json` — `STAGE_F_P1_FOUR_ARM_SMOKE_PASS`.

One dispatch, four cells, `stage-f-p1-compat-smoke`, deterministic provider injected, every arm
through its production runner: raw result → workspace export → independent sandboxed oracle →
Unified Cell Evidence → audit → ledger → matrix report.

| Arm | Entry | Requests | Agents | Public suite | Oracle | Integrity | Delivered | Verdict |
|---|---|---|---|---|---|---|---|---|
| A0 | native loop, 40 turns, 1 M budget | 12 | 1 | N/A (no tool) | PASS | PASS | yes | PASS |
| A1 | `run_goal`, delegation compiled out | 13 | 1 (Main) | PASS | PASS | PASS | yes | PASS |
| A2 | static DAG: `csv` ‖ `jsonl` → `integrate` | 18 | 3 | NOT_CONFIGURED | PASS | PASS | yes | PASS |
| A3 | `run_goal`, delegation enabled | 20 | 3 (Main + 2 children) | PASS | PASS | PASS | yes | PASS |

- A3's one `delegate_tasks` proposal was admitted by router `m.1.0`; both coding children
  (`csv-unit`, `jsonl-unit`) completed, `max_observed_concurrency = 2`, and the Main continued to
  integrate the five shared modules and run the public suite.
- A1 recorded zero proposals and no delegation tool in its surface.
- 63 scripted provider requests, 157,356 system tokens, USD 0.063391 (scripted estimate, not a
  bill), 0 external model requests, 0 production claims.

This is a compatibility confirmation for the new P1 only. It adds no cell to the 96-cell matrix and
does not reopen F2-A.

## I. Regression and the historical failures

Executed in chunks with an **external** `--basetemp` (an in-repo basetemp makes
`test_git_commit_is_none_outside_a_repository` find the repository and fail for a test-harness
reason, which is how part of the earlier "158 failures" arose):

| Group | Files | Result |
|---|---|---|
| `tests/unit` | 1 failure, only with an in-repo basetemp | green with an external basetemp |
| `tests/integration` (non-stage) | 45 files, 505 tests | green |
| `tests/integration/test_stage_{d..z}*` | 17 files | green (2 skips) |
| `tests/integration/test_stage_{a..c}*` | 21 files | **158 failures, all pre-existing** |

The 158 are not new and none of them touches Stage F, the executor's cell path, the evidence schema
or the settlement. They fall into three reproducible classes:

1. **Worktree path.** The Stage-B execution tree is frozen against
   `D:\Work\AtlasRuntime`; the preflight compares
   `supersedes.live_path` with the current checkout and refuses
   (`test_stage_b_publication_gate`, `test_stage_b_matrix_executor`,
   `test_stage_b_e2f_provisioning`, `test_stage_b_sandbox_smoke`, `test_stage_b_live_runner`).
2. **Frozen Stage-B identity.** `runtime tree digest`, `protocol digest`, `freeze raw digest` and
   the calibration fixture digests were captured in that other worktree, so
   `test_stage_b_calibration_{gate,retry}`, `test_stage_b_freeze_1_5`,
   `test_stage_b_protocol`, `test_stage_b_s06_plan` and `test_stage_b_execution_projection` fail.
3. **Historical frozen identity.** `test_stage_c2_natural_delegation` asserts the C2-era executor
   digest (`e7d7b…`) and that no C2 claim exists; C2 ran, its claim is consumed, and the executor
   has since moved to 1.9.

They were recorded, not deleted and not re-baselined. Two Stage F tests were updated because the
contract they asserted was deliberately strengthened (an unevaluated integrity report no longer
passes an audit; a prior report without plan/claim identity no longer settles); the new assertions
are stricter, and a new test file covers the new contract.

`pyright` (project config: `include = ["src", "tests"]`) reports the same four pre-existing errors
in `tests/unit/test_c1b_failure_classification.py`. `ruff check` on the touched files is clean.

## J. Master manifest

```
master_id        stage-f-96-manifest-v1
manifest_digest  sha256:230135f0b55aede38390ff216be1cc2dc98bd03e51cb723f72c4d7cd3d17680f
cells_total      96
task_order_seed  20261125
```

It binds the eight task facts (goal, grant, static-DAG and **seed** digests), the four arm
configurations, the order and sequence assignment, the three batch plan digests, the budget identity,
the pricing identity, the adapter identity, the executor identity and the settlement contract.

## K. Three batch plans

| Batch | Plan id | Plan digest | Cells | Token stop | Cost stop (candidate) |
|---|---|---|---|---|---|
| 1 | `stage-f-96-b1` | `sha256:6fcfd69d74d1ad0158d162e0dd42cd710df90fbb7bf60acbc37ab520f99a8587` | 32 | 32,000,000 | USD 32.00 |
| 2 | `stage-f-96-b2` | `sha256:23bd5635e8f0246fe1578b283f9cd8c2477e31621a8ae5715a7b775adc041c2b` | 32 | 32,000,000 | USD 32.00 |
| 3 | `stage-f-96-b3` | `sha256:949882e0827a3302f51419ec6fcb019b3e8dc6f81a6f3e79028c7872d0f662e1` | 32 | 32,000,000 | USD 32.00 |

Each plan also carries the executor, adapter, pricing and settlement digests. The checked-in files
equal the recomputed plans (`preflight.json` F07/F08/F12).

## L. Executor / adapter / budget / pricing identity

| Identity | Value |
|---|---|
| Executor | `bplus-executor-1.9`, `sha256:9ca0a48d3e467edff4c1d75baaf7363a64243f08bdcc1fb3c6f00e1666941f08` |
| Adapter | `stage-f-cell-adapter/1.0`, schema `stage-f-cell-evidence/1.2`, `sha256:8b6a93d6e6caf6e050896aab7f47c9941826633cf2f04b2a11d142b640a48c4b` |
| Budget | `sha256:fc61bc3dacc971c087fa0a61c97ac75bc31ae817cff4b3d7c3ee5bfca10def82` (1,000,000 tokens, 40/20/40 turns, 2 concurrent children, 3 rounds) |
| Pricing | `sha256:9ac617968f995d902359408362e1bf669abf6ea3dc3a7520483121692f6e0dd6` |
| Settlement contract | `sha256:9616c114ae235cd2a264659c4f7f377ed6689030c520591f5d9e7199c986df9e` |
| Execution contract | `sha256:242fcef58a1f8e8b54a3caa48ebff958c85536bf3d4ce56339dc4d54e2b9e36e` |
| Main prompt | `sha256:32e082a7116ba0b3756176b0d8590103cc223c7420b91aaad3580216c28bf71c` |
| Worker contract | `sha256:5b0fa74e9ccdeadbe3a61e41d96ef0608bdc961606397070f25043cb42c45b4a` |

`EXECUTOR_FILES` now also covers the shared fixture builder (`closure2/catalog.py`,
`evaluation/fixtures.py`) and the preparation gate (`stage_f/preflight.py`): the bytes that decide
what a cell starts from, and the gate that decides whether the matrix is ready, are part of what the
Owner signs.

## M. Eight frozen task identities

| Task | Family | Seed | Goal | Grant | Static DAG |
|---|---|---|---|---|---|
| closure2-pipeline-migration | S | `sha256:f9378c44a0ee564962e0a56ac7172ad616904d098fbc8dabb0806c4cdbbef515` | `sha256:4b97784ec44dc64ae2672fbc627c1bb6783e36ddf10610bed1e7465be53c02aa` | `sha256:0758dec7b37f85e3da4da0032707ca507cd83d029cc2589cc8e952e1cd0f12e4` | `sha256:93264d94ab061a1c451655d02ec298265125f1cc3173994d3b056978706aa1c8` |
| closure2-state-machine-refactor | S | `sha256:400f56d38c1425757835701e73db4feb8ee3beea5764f008006058bd6a9e4012` | `sha256:cdb4cc158cc67f95b14b8048eb40529d2b6f9bef9cec45c45c97388f2ec93538` | `sha256:5ec73b17d7cc93c4ce0ae0f6d5619a04cedcd7761c6766d3c51f8928d188f469` | `sha256:033cb2dfe64d9104bbfbdd6c8d53b9f74bfba507bee59e5ac15f89fe038be0bb` |
| f-p1-connector-migration | P | `sha256:dc5ec2ffc2f25b79f2fcf4e0ddff00e6afe22f2476f5ae694e4b4d976e098fbc` | `sha256:e38b06ad3bdb3dcd198b66da3c296ad3fc61f0aa8db2fc4806c04f425ed1d24f` | `sha256:9e111b36c41838ad1aa2c3cefb0d1e2152ed98950bf9fd938d81afcf3ebacd8e` | `sha256:2505f5b8f44995ef79b0363bf530e0ec57ce2180bf47b4b405b60aea11686283` |
| f-p2-protocol-adapter | P | `sha256:dc24cb9ad9ff58668420e0b1e34839ef2d866c22671439653ad30d6571132467` | `sha256:fb78d058868fb44de34fdbfe046dc287b8656019476c4ffbf8dcaa0b66e7960b` | `sha256:a678e59ca2fb8b9a4a053f9e52f4869be6bcc7bba5cbaf089c091b802f7b34ed` | `sha256:cb6007b502ee740d35356615a0d547252a2076c1912937bb1853a0324c5a6ce9` |
| f-x1-api-compat-audit | X | `sha256:caca39ed50a11b1c2fb034a79751eec899e6e8238ebf81d5f3bcec6218693f64` | `sha256:837bcaa3e3ffe13726cddfa71a602200dede98acfaeceebcc10d15a90ef6e998` | `sha256:98ad0cb4fb03eaff96160a7943b009fea1887bc314f4dc3857afd4c999ac54be` | `sha256:25ba628705e331441be37e545ecabfabce4d7bdb9adf25bae3eef206dde7fc42` |
| f-x2-distributed-diagnosis | X | `sha256:337ba76be494ec9676c40df98bdca12ccb3d9c4ab4626b60b4b086280915b7d2` | `sha256:d1a05d90c059d8e9a4e6559d0b2e256bda84b310ba1e30a574c7fd21a9acc55a` | `sha256:04f8ad1d69289f1a62e9094165e9a857d4ec34cffba1abeb4924db0faebf9aaf` | `sha256:dcbde35e31ba3ed18adeeb7a2c81a54a884c8ad45ac54e46e5917a9ddadd89e4` |
| f-c1-config-lifecycle | C | `sha256:ef5914cff9e862d6309da867d9b76a3fc3469e7b61d8ceab2d0d6f45094d850d` | `sha256:0517748fd4cc80a694fc12d2cc75c5d892428240dfe81c88611e177e5de7af06` | `sha256:a9d737d7f1ecfb9e81d909229a98eccc84b58c0ffcd39f37ee6e0260277d5139` | `sha256:61cfe587d36d56de5e85e94e6024d777a5350eb8b8856547628d17e536c6334c` |
| f-c2-scheduler-retry | C | `sha256:4430f70dbcaee08069503d4f9188266677da14f94c655f67f7d1fcaddfeb9adb` | `sha256:256dc1db3dbd5df16367e99b2562844b66ddc197953cbdae7466ca30efbde7cb` | `sha256:b93f794e171e5332ae7667eda95ca0cce0bdc83a0d6fe842f2d36111fac034bd` | `sha256:0ee10d0d45ef0e59a580622b14c415d0947d3df732c9d276b845d2bae4f31a0a` |

## N. Three pending authorizations

`experiments/stage_f/authorization-b1.json`, `-b2.json`, `-b3.json`.

Each one: `status = PENDING_OWNER_APPROVAL`, `authorizes_live_execution = false`, `approved_by`,
`approved_at`, `owner_statement`, `approval_provenance`, `reviewed_head`, `execution_head`,
`cost_stop_usd` and `per_cell_cost_stop_usd` all `null`, `token_stop = 32,000,000`, and the exact 32
cell ids of its plan. The live verifier refuses all three
(`preflight.json` F11: `ready=false`, 12 problems each). A signature for one batch cannot authorize
another: the record names the plan digest, the scope, the cell list and the executor digest.

## O. Claims

Seven historical production claims, byte-identical and untouched:

| Claim | Plan digest |
|---|---|
| B+ v1 | `sha256:7ae71273eea29f489edc657ada9a26c834e178289ac6744807350d674dc53575` |
| B+ v2 | `sha256:41fc36da78feb51a4fa8462b7901bd3f41c6573105ad0d93486bafde4ddd5046` |
| B+ v3 | `sha256:0b58ab2500325d11cf27763dd0d8dde8498d5bca9ca11f602e6e410a37e572f6` |
| C1 | `sha256:537508bf0fc10056987ef9342d9899008609aa9ba9c1734f7de53a3d3bc7ecd2` |
| C1-B | `sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c` |
| C1-C | `sha256:4481bba2d3a0a9e0e3b7b04c5de3c851c70f1d4d03892043e23046782ab727bb` |
| C2 | `sha256:d0a4c212fda8bf76b20efd995115474b326cf78f787bd162b05c75829bc16038` |

No Stage F claim exists for any of the three batches (`F10_history_preserved`).

## P. The production entry point

A live batch is one call, with the Owner-signed record for that batch:

```python
import asyncio
from pathlib import Path
from experiments.stage_b_plus import executor

root = Path(r"D:\Work\AtlasRuntime-runs\stage-f")
asyncio.run(
    executor.dispatch(
        "stage-f-96-b1",                  # or b2, b3
        runs_root=root / "runs",
        evidence_root=root,
        stage_f_root=root,               # one root for the three batches
        authority_path=Path("experiments/stage_f/authorization-b1.json"),  # signed by the Owner
        mode=executor.MODE_LIVE,
    )
)
```

Before the claim, the dispatcher checks the head, the worktree, the default claim root, the live
runner, the authority record, the six-cell admission and the cross-batch settlement. A refusal
writes `dispatch-refused.json` and leaves no claim. The evidence lands in
`<root>/<plan_id>/`: `matrix-report.json`, `cumulative-settlement.json`, `ledger.jsonl`,
`dispatch.jsonl`, `cells/**`.

## Q. Known limitations

1. **A2 has no Main** and no public-test tool: `main_*` metrics are `NOT_APPLICABLE` and the public
   suite is `NOT_CONFIGURED` on that arm. Its integration node is reported separately.
2. **A2 ownership is `off`**; the DAG's concurrent write sets are disjoint by plan, which is a
   planning property, not run-time enforcement.
3. **A3's child peak context is not recorded** by the delegation evidence, so a delegated cell
   reports `system_max_agent_context = UNKNOWN` and names the child. The Main peak is exact.
4. **A0 carries no trusted task id**; its single agent's requests are attributed to
   `primary_agent` by the one-agent rule, not by an identity match.
5. **Per-request cache split is unknown** for every arm; the run-level split is provider-reported
   where available.
6. **Turn caps are not equivalent** (40 Main vs 40 single agent vs 20 worker + 40 integration).
7. **No cold-cache guarantee** across cells; cache hit/miss is reported as measured.
8. **Cost is an estimate** from the frozen snapshot, not a bill; the stops are candidate ceilings,
   not an approval.
9. **The settlement is a pre-claim read**, not a real-time budget scheduler: it decides whether a
   batch may start and with what ceiling, and post-run stops are still the freeze-time rules.
10. **The matrix is 24 observations per arm**, three per task; it is not a sample of anything, and
    the report must not generalise it.

## R. Real counts for this session

| Counter | Value |
|---|---|
| Scripted provider requests (final smoke) | 63 |
| External model requests | **0** |
| Production claims consumed | **0** |
| Containers in the recorded final runs | environment 8 created / 8 destroyed; smoke 4 agent + 4 oracle (7 ids recorded; A2's agent container records created/destroyed without an id), all destroyed |
| Distinct sandbox ids across all session scratch records | 72 (preparation re-runs included) |
| Cleanup failures in the recorded runs | 0 |

The preparation re-runs are not hidden: the environment gate ran 6 times and the smoke 6 times, and
each discarded run created and destroyed its own containers. Two of them found real defects (the
Host-side path policy on the reference overlay, and the missing public suite in the fixture), which
is what they were run for.

---

## S. Commit

The code, the evidence and this report are committed together on
`feat/stage-f-unified-evaluation`. The freeze records `repo_head = 0b466ba…` because a commit
cannot record its own hash; a re-freeze at the new head would reproduce every digest except
`repo_head`, which is not part of any identity.

Readiness is accepted only because every one of these is true in `experiments/stage_f/preflight.json`:

- F2-A and F2-B records still carry their own identities, unchanged;
- eight tasks qualified, 20 mutations, real environment, public and hidden suites in a container;
- the new P1 passed the four-arm smoke through the real entries;
- provider usage, request accounting and the context census are wired for all four arms;
- delivery requires orchestration, oracle and integrity together;
- the cross-batch settlement runs in the pre-claim path and refuses what it must;
- the three batch plans resolve through the real entry and pass 32/32 synthetic cells in order;
- every frozen identity recomputes, and the three pending records refuse a live run;
- no relevant production regression; 0 external requests; 0 claims consumed.
