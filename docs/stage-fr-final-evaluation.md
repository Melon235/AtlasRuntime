# Stage F-R final evaluation candidate

Stage F-R uses a separate candidate identity, evidence root and claim keys. The original Stage F remains `STAGE_F_UNIFIED_96_CELL_EVALUATION_STOPPED`. Its Batch 1 claim stays consumed and Batches 2 and 3 stay outside this candidate. H4 remains `STAGE_H4_REAL_PRODUCT_DEMO_STOPPED`.

## Offline preparation

From a clean candidate checkout, run the following with the project's Python environment. Use a new output directory outside the source tree.

```powershell
& ./.venv/Scripts/python.exe -m experiments.stage_f.release_candidate --out D:/Work/atlas-fr/evaluation-candidate
```

This writes three 32-cell plans, the task and executor manifest, unsigned authorization records and the actual production pre-claim admission output for each plan. It does not instantiate a Provider, create a Sandbox or write a claim. The executor identity binds the execution HEAD, the existing tracked executor inputs, Python sources across the runtime and the benchmark modules, including the Request Journal and DeepSeek metering. The candidate reuses the original task order, task bytes, source seeds, public-test and Oracle contents, grants, four arms and three repetitions. The generator verifies the task facts against the original qualification manifest, including the Oracle and reference digests. The three repetitions retain the original incomplete counterbalance.

The output is a review candidate. The current live dispatcher resolves the original plan IDs only and the existing frozen settlement contract prices the old model configuration. The current default Main options still select `deepseek-chat`. Native and Static DAG read the declared model at execution time, but their admission check expects `deepseek-chat`. Review the actual per-cell admission evidence together with the model mismatch. A paid launch command for the candidate is unavailable until a consistent model contract, pricing identity and scope registration have passed pre-claim checks, after which the Owner can review and authorize the new execution identity.

## Execution paths and measurement

| Arm | Production path | Key evidence and scope |
| --- | --- | --- |
| A0 Native | `native_cell_runner` → AnyCode Native baseline, neutral engine and Sandbox tools | Single-agent inference observations, exported workspace, independent Sandbox Oracle. Public Test Channel is not applicable. |
| A1 Direct | `main_driven_cell_runner` → benchmark `GoalRunOptions` → `run_goal` | No delegation tool. Main uses benchmark Ownership grants and the existing Host-declared `PublicTestContract`. |
| A2 Static DAG | `static_dag_cell_runner` → evaluation runner and static scheduler | Per-node inference observations and integration node. The human-provided decomposition is prior information. This path does not expose the Main-driven Public Test Channel. |
| A3 Dynamic | Same Main-driven benchmark entry with selective delegation enabled | Observe proposal, Router verdict, Ownership grants, actual Worker starts and overlap, Handoff reads and Main integration. Natural Direct is valid. |

All four use benchmark admission and the independent Oracle on the exported workspace. The product profile is outside the qualification path. The declared envelope is Main 40 turns, Worker 20 turns, parallel children 2 and 1,000,000 nominal system tokens per cell. Publish the effective configuration per cell, including the A2 per-node turn caps and the integration-node budget.

The default A1/A3 path wraps the DeepSeek adapter in `JournaledAdapter`. It records STARTED and terminal COMPLETED/FAILED states. The journal preserves known partial usage and represents missing usage as unknown. Effective completion uses the classified provider stop and journal evidence alongside the raw engine result. A0/A2 use their actual inference records. Request Journal coverage and cache attribution differ across arms. Settlement must retain agent-level and system-level usage, unknown fields and the pricing identity. Missing cache split uses the miss rate for the estimate. Missing usage cannot be settled as zero.

## Stop and analysis contract

Continue after a measured Oracle FAIL, legal max-turn stop, Worker failure, Direct or Router rejection when the execution, export and resource evidence is intact. Stop on unknown or illegal usage, unclassified provider errors, isolation or Oracle infrastructure failures, invalid export or evidence, budget or cost stops and claim conflict. Store each attempted cell once. Keep raw Agent success, effective completion, Oracle verdict and delivered success separate.

Report Oracle pass, delivered success, effective completion, public-test pass among applicable runs, and failure counts. Report Main peak context and cumulative input separately from system cumulative input and maximum agent context. Include Cache hit/miss, tool result offloads, Handoff inline/reference/read and repeated file reads where measured. Include prompt/output/system tokens, estimated cost, elapsed time and P50/P95, tool calls, budget refusals and policy refusals. Report proposal, Direct, unobserved decision, Router admission/rejection, Worker starts and completion, measured overlap, Handoff consumption, Main integration and Oracle outcome.

Pair by task and repetition for A0–A1, A1–A2, A1–A3 and A2–A3. A1–A3 is the primary comparison. Show all attempted samples and the common successful subset separately, with denominators and missing evidence. Repetitions of the same task are clustered observations. Exclude historical and deterministic data from the new paid result.

## Pricing proposal

The official DeepSeek V4.1 Flash pricing at review time is USD 0.006 / 0.30 / 1.20 per million Cache-hit input / Cache-miss input / output at standard rates and 0.003 / 0.15 / 0.60 at off-peak rates. The H4 record requested `deepseek-chat` and observed `deepseek-flash`. DeepSeek announced retirement of the old alias on July 24, 2026. The candidate proposes `deepseek-flash` for all four arms. Returned model and actual billed usage can only be checked in a later authorized run. [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) · [DeepSeek V4 announcement](https://api-docs.deepseek.com/news/news260424/)

The original cost-stop proposal is USD 1 per cell, 32 per batch and 96 cumulative, with 96M nominal tokens. Using H4's 29,811 input and 708 output tokens and pricing all input as a Cache miss, 96 H4-sized cells cost roughly USD 0.94 at the standard rate or 0.47 off-peak. Scaling that same token mix to the historical A0 277,622-token cell size yields about USD 8.55 standard or 4.28 off-peak. These are scenario estimates for size and mix, and the eight tasks and four arms do not have measured final-version workloads. At nominal capacity and entirely output the standard-rate scenario is USD 115.20. The cheaper alternative is off-peak scheduling with the same resource limits, while recording each request's applicable price window. Token admission estimates can be exceeded by actual usage, and post-request cost settlement can overshoot a stop threshold. Keep all proposed limits pending Owner review.

## Context Engineering ablation design

The Main-driven guard requires `context.policy=upstream_default` because its Main task has no dependency edges for structured incoming context. The separate DAG evaluation runner has settings for ingress offload and history compaction. Switching from Main-driven to that runner would also vary the execution architecture.

First verify toggle exposure on a shared Direct-only path and add observability for whether each mechanism actually activates. C0 is upstream context. C1 adds structured context and stable rendering on an eligible common path. C2 additionally enables ingress offload and C3 adds history compaction. Keep the model, input bytes, budgets, tool surface, routing and ordering constant. If structured context remains inactive, omit C1 from inference and evaluate offload/compaction only if the shared path supports those interventions. Compare paired Main and system input, peak context, cache split, rereads, latency, correctness and costs. This is a design only.
