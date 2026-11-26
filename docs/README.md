# Documentation Index

Three classes of document live here. They are not interchangeable.

## ACTIVE — current architecture and development constraints

Read these before changing code. They describe what exists today in this branch.

| Document | Purpose |
|---|---|
| [`architecture-design-contract.md`](architecture-design-contract.md) | **Layer 1** — the owner-confirmed design baseline: positioning, the two cores, the Adaptive Delegation extension, claim boundaries, change control. Read before any architecture, run-path or evaluation task |
| [`project-status.md`](project-status.md) | Current baseline commit, architecture status per pillar, mechanism status, current work, closed directions |
| [`architecture.md`](architecture.md) | Layers, normal execution path, persisted state, recovery/verification paths, mechanism switchboard |
| [`anycode-delta.md`](anycode-delta.md) | What AnyCode 0.9.0 provides vs what AtlasRuntime adds (provenance basis for every claim) |
| [`runtime-entrypoints.md`](runtime-entrypoints.md) | Real call chains for the workflow, application-coding, evaluation, recovery and router paths, plus the capability matrix |
| [`design-decisions.md`](design-decisions.md) | Trade-offs, rejected designs, why defaults are what they are |
| [`limitations.md`](limitations.md) | Existing constraints; nothing here is a backlog promise |
| [`final-closure-roadmap.md`](final-closure-roadmap.md) | **The only forward plan from now on**: the closure stage, the frozen core scope, the paused Placement Slot route, the four closure stages and the module metric principle |
| [`final-roadmap.md`](final-roadmap.md) | The earlier Track A/B/C plan. Historical from the closure stage onwards; superseded by the closure roadmap |
| [`evaluation/`](evaluation/) | The evaluation protocol (Final-2): module matrix, Track B/C specs, integration contract, metrics contract, holdout design, environment protocol |
| [`stage-h1-readonly-workbench.md`](stage-h1-readonly-workbench.md) | **Stage H1** — the local read-only historical replay workbench: application audit, recognised evidence kinds, the projection contract, the seven views, the verified historical cases, the `/api/replay` contract, launch and security boundaries |
| [`decisions/`](decisions/) | ADRs 0001–0004 (AnyCode base, ContextPilot backend, cache metrics, no LangGraph) |

The `evaluation/` bundle is normative for any measurement. Read
[`evaluation/module-evaluation-matrix.md`](evaluation/module-evaluation-matrix.md) before claiming a
module works, [`evaluation/metrics-contract.md`](evaluation/metrics-contract.md) before reporting a
number, and [`evaluation/environment-protocol.md`](evaluation/environment-protocol.md) before running
anything live.

## EVIDENCE — measured results

Real runs. Numbers are frozen at the commit and task set named in each document; they are not
results of the current branch unless the document says so.

| Document | Content |
|---|---|
| [`evaluation-summary.md`](evaluation-summary.md) | Consolidated evidence inventory with comparability warnings |
| [`../../experiments/results/`](../../experiments/results/) | Per-stage reports and published figures |
| [`../../experiments/evidence/`](../../experiments/evidence/) | Sanitized public aggregates only (no raw payloads) |
| [`final-consolidation-report.md`](final-consolidation-report.md) | Final-0/1 repository consolidation audit, 66 removed files, regression record |
| [`final-1-5-repository-baseline.md`](final-1-5-repository-baseline.md) | Final-1.5 baseline: Git state, Recovery-Pilot review, regression record, readiness |
| [`final-1-6-evaluation-baseline.md`](final-1-6-evaluation-baseline.md) | Final-1.6: the ported attribution fix, dependency inventory, measured regression, readiness matrix |
| [`final-2-evaluation-design-report.md`](final-2-evaluation-design-report.md) | Final-2: the evaluation design itself — coverage, integration, workloads, budgets, required changes |
| [`final-3-integration-report.md`](final-3-integration-report.md) | Final-3: what was integrated (R-4/R-5/R-6/R-7), with the real call chain and the parity blocker |
| [`final-3-module-acceptance-report.md`](final-3-module-acceptance-report.md) | Final-3: 59-module acceptance status, measured metrics, negative and blocked results |
| [`final-4-module-evidence-supplement.md`](final-4-module-evidence-supplement.md) | Final-4: evidence levels, D0-D3 dedup diagnosis, 64/128-tool retrieval, topology study |
| [`final-4-integration-and-router-pilot.md`](final-4-integration-and-router-pilot.md) | Final-4: neutral backend, frozen configs, and the honest NOT_RUN live matrix |
| [`final-4-architecture-coverage.md`](final-4-architecture-coverage.md) | Final-4: design → implementation → execution matrix for the three entry points, and the six functional gaps |
| [`final-4-legacy-pilot-reclassification.md`](final-4-legacy-pilot-reclassification.md) | Final-4: the three 2026-11-14 runs reclassified as `EXPLORATORY_LEGACY_PILOT`, and what they may no longer be used for |
| [`final-4-placement-integration-proposal.md`](final-4-placement-integration-proposal.md) | Final-4: context-locality placement support boundary and the minimal integration proposal awaiting approval |
| [`final-4-context-restoration-report.md`](final-4-context-restoration-report.md) | Final-4: the corrected evaluation matrix, the X0/X1 context comparison, and the honest Track C `NOT_VALIDATED` result |
| [`final-4-evidence-closure-report.md`](final-4-evidence-closure-report.md) | Final-4 closure: router metadata fix and the real delegate run, request-content-verified context delivery, blocked native arm, degenerate disclosure ablation |
| [`final-4-placement-core-integration-approval.md`](final-4-placement-core-integration-approval.md) | `PLACEMENT_CORE_INTEGRATION_APPROVAL_REQUIRED` — what a real pooled-worker integration would change, and the two options |
| [`final-4-evidence-handoff.md`](final-4-evidence-handoff.md) | Final-4 handoff for independent review, including the G0 corrections to three earlier evidence claims |
| [`closure-1a-prompt-layout.md`](closure-1a-prompt-layout.md) | Closure-1A: the duplicated task objective, the discarded Atlas system message, the resulting `shared_prefix` layout, its deterministic comparison, and what it does not measure |
| [`closure-1b-native-baseline.md`](closure-1b-native-baseline.md) | Closure-1B: N0/N1, the native AnyCode baseline on the shared OpenSandbox environment — lifecycle, parity table, real-container acceptance, lifecycle failure handling |
| [`closure-1c-metrics-evidence.md`](closure-1c-metrics-evidence.md) | Closure-1C: metric dictionary, provider-vs-normalized usage, cache-split status, request-evidence and index integrity fixes, the effective public task digest, the full historical inventory and the anti-misuse rules |
| [`closure-2-benchmark-design.md`](closure-2-benchmark-design.md) | Closure-2A: the three frozen tasks (P1 pipeline migration, P2 plugin migration, N1 coupled refactor), their seed/reference/oracle, the candidate DAGs and why they are not rigged |
| [`closure-2-fairness-protocol.md`](closure-2-fairness-protocol.md) | Closure-2A: the six arms, digest parity, the model/resource contract, the two budget semantics and every difference that is deliberately not removed |
| [`closure-2-preregistration.md`](closure-2-preregistration.md) | Closure-2A-R1: frozen digests, revision history, release boundary, planned cells, repetitions, exclusions and the cells the design does not support |
| [`closure-2a-r1-review-repair.md`](closure-2a-r1-review-repair.md) | Closure-2A-R1: the six review findings, each reproduced on the C2-A commit and repaired — approval/definition separation, checkout-independent digest, content-bound config digest, independent oracle sessions, content-based leak detection, router decision gate |
| [`closure-2b-pilot-runner-design.md`](closure-2b-pilot-runner-design.md) | Closure-2B-0: the pilot runner — six-arm adapter over the existing engines, frozen-contract checks, approval gate before any side effect, 18-cell dry-run manifest, evidence schema, and the unapproved live pilot proposal |
| [`stage-f-f2c-closure.md`](stage-f-f2c-closure.md) | Stage F F2-C: production measurement, common integrity and delivered-success definition, the cross-batch settlement in the pre-claim path, the eight-task real environment, the new P1 four-arm smoke, the frozen 96-cell identity and the three pending authorizations — `READY_FOR_LIVE_AUTHORIZATION`, nothing run against a provider |
| [`placement-coding-integration-design.md`](placement-coding-integration-design.md) | **PAUSED RESEARCH** — `DESIGN_PROPOSED`, then G1 landed on `feat/placement-coding-integration` (`0252bdb`). Stopped by the closure decision; the proposal is archived, not withdrawn from the record |
| [`research-history.md`](research-history.md) | Which branch/SHA holds which historical stage |

The Final-2 Recovery Pilot report (`docs/final-2-recovery-pilot.md`) lives on
`experiment/final-2-recovery-pilot` at `c5fce5cc5d3d96251968b264e8113685711ffc8e`. It is cited by
branch and SHA and deliberately not copied into this branch; no pilot experiment code entered the
stable baseline.

## ARCHIVED — historical stage specifications and old plans

Historical references, not current implementation instructions. Nothing here authorises new work.

| Path | Content |
|---|---|
| [`stages/`](stages/) | V1 Stage 0–6 and V2 M1–M7 stage notes, acceptance records, migration notes |
| `stage-*.md` (this directory) | Stage G/R report set (G1, G2a, G2b-1, G3-x, R0, R0.5, R1-x) |
| [`verification/`](verification/) | Historical system-verification write-up |

ARCHIVED documents are historical references, not current implementation instructions. Their
recorded measurements are preserved verbatim; do not edit them to match a newer result.

## Repository entry points

- `README.md` — public project overview.
- `AGENTS.md` — the harness development contract.
- `THIRD_PARTY.md` — third-party acknowledgements and licences.
