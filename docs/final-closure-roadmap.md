# Final Closure Roadmap — AtlasRuntime

Status: `FROZEN_BY_PROJECT_OWNER`. This document is the only forward plan for the remaining closure
work. It narrows scope; it does not change the architecture.

Baseline it was written against:

| Item | Value |
|---|---|
| Repository | `Melon235/AtlasRuntime` |
| Closure base (`CLOSURE_BASE_SHA`) | `76a427541617c1ff4993ef3ad3947639f802b6d8` (G0 — Final-4 evidence handoff corrections) |
| Final-4 closure | `428706908056200e06b35bddad7511c2e60e6deb` |
| Frozen runtime | `93cbd98f9407e7358e997878238520dcac15827a` (`git diff 93cbd98..76a4275 -- src` is empty) |
| Design contract | `37eba87df2aa188d16d24d0c31bd00412d154276` — **unchanged** |
| Paused research branch | `feat/placement-coding-integration` = `0252bdb8ea82007bc484a08fb4b0fd507999d5d7` (retained, not merged) |

---

## 1. The project has entered the closure stage

AtlasRuntime is, and remains:

> **AtlasRuntime — a context-aware and reliable agent runtime built on AnyCode.**

Two cores, one extension, one evaluation obligation:

- **Context Engineering**
- **Reliable Execution**
- *Adaptive Delegation* (extension)
- *Evaluation and Observability* (the evidence obligation)

`docs/architecture-design-contract.md` is unchanged by this roadmap. Per that contract §10, a stage
may narrow the work but never the contract: nothing here removes a mechanism from the design, and
nothing here claims an unwired mechanism runs.

**No new core architecture module is added from this point on.** The remaining work is repair of
confirmed defects, fair baselines, evidence closure, and packaging.

---

## 2. Core scope is frozen

Frozen and not to be extended:

- the Context Plane (`ContextBlock` / `ContextRef` / `ContextStore` / `TaskScopedSelector` /
  transform / renderer);
- the scoped worker, action safety gate, durable ledger, sandbox binding, suspension/approval;
- the shared run budget, dual handoff, deliverable verification and the isolated oracle;
- the adaptive delegation router, as designed and frozen at policy `r0.1.0`.

The only code changes permitted are those that repair a defect which affects final delivery, or
that are required to make an already-designed mechanism honest about what it actually does. A
change that would need a new mechanism, a new subsystem or a new benchmark family is out of scope
and belongs in a later decision by the project owner, not in closure.

---

## 3. Stopped work

### 3.1 Placement Slot integration is paused

The B′ placement route (`feat/placement-coding-integration`, G1: `0252bdb`) is **stopped** here.

- Its G1 commit is **retained on its branch**. It is not deleted, reverted, force-pushed or merged.
- It is not part of the final stable version, and no slot, worker pool or pooled-worker scheduler
  enters `src/` during closure.
- G1-R1, G2, G3, G4 and G5 are not continued.
- Connecting Placement into the coding runtime is **not** required for closure.

What is retained is the original V1 **context-locality placement** mechanism and its logical
handoff evidence (`docs/final-4-placement-integration-proposal.md`,
`docs/final-4-placement-core-integration-approval.md`, S6 Table C in
`docs/evaluation-summary.md`). The G0 proposal documents stay in the repository as **paused
research**; they are not a backlog promise and they are not deleted.

### 3.2 Explicitly out of scope for closure

- cross-task conversation reuse;
- slot-local caching;
- physical KV-cache control, eviction, prefetch or serving-engine work (V2 scope);
- a dynamic or LLM-driven scheduler;
- a new recovery framework.

### 3.3 The multi-agent performance finding is not re-diagnosed

Stage E1 measured static multi-worker decomposition at 6.59× the accumulated input tokens and
3.84× the model requests of the single-worker arm, with 48.5 % of multi-worker input being tool
results and roughly 0.5 % the old handoff channel. Rolling compaction raised accumulated input by
9.3 %, cache miss by 88 % and cost by 43 % (G2b-1). Final-4 X0/X1 did **not** establish a stable
end-to-end token or cache benefit. All of these keep their original experiment scope; closure does
not re-run or re-interpret them, and does not add a module to answer them.

---

## 4. Stages

### Closure-1 — core repair and a fair baseline

| Sub-stage | Content | Gate |
|---|---|---|
| **C1-0** | Freeze this roadmap; archive the placement integration decision | docs only |
| **C1-A** | Effective prompt layout correction: the worker system prompt and the rendered task body must describe the request that is actually sent | deterministic comparison + tests |
| **C1-B** | Native AnyCode fair baseline: one neutral lifecycle composition so the native arm can run in the same image | owner approval, then live runs |
| **C1-C** | Historical metric consolidation: one comparable table with per-run reconciliation and the request-evidence statuses | docs, no new runs |

### Closure-2 — final benchmark and module evidence loop

A **small number** of complex tasks that have a *natural* parallel structure and *large independent
contexts* — the structure multi-worker execution can actually help — plus the per-module evidence
table. Closure-2 does not resurrect unbounded multi-worker optimisation, and it does not claim a
cache benefit that the provider state cannot separate.

### Closure-3 — Web Workbench

Front end over the **existing** backend. No new runtime capability is introduced to support the UI.

### Closure-4 — final regression, documentation, packaging, version freeze

Full offline regression, public documentation, the job-search packaging (README, architecture
summary, evidence summary, demo script), and the version freeze.

---

## 5. Module metric principle

Every headline module must carry **quantitative evidence that matches its own responsibility**.
The obligation is per module, not per project.

1. **Not every module has to reduce end-to-end cost.** A reliability module is judged by the
   failures it removes; an observability module by the actions it recovers; a context module by what
   it selects and delivers. Demanding a cost reduction from every module is how a negative result
   gets quietly relabelled as a positive one.
2. **A logical handoff reduction is not a KV-cache benefit.** Fewer cross-worker context references
   is a property of the assignment plan. It says nothing about provider prefix caching.
3. **A tool-schema reduction is not a whole-agent token reduction.** The 8 558 → 4 207 character
   schema result describes tool definitions only; it is not an end-to-end coding measurement.
4. **Historical negative experiments are kept.** E1, G2b-1, G3-2, R0/R0.5, R1-1 and the Final-4
   X0/X1 pair stay in `docs/evaluation-summary.md` with their original scope. Deleting or softening
   them would make the positive results unreadable.
5. **A deterministic mechanism comparison is not a benchmark.** Content-level before/after evidence
   must never be presented as a model-behaviour result.

Preserved research mechanisms — kept with their existing implementation and historical evidence,
each switchable, none of them an automatic default:

| Mechanism | Status after closure |
|---|---|
| V1 context-locality placement | retained research (`placement.policy`, workflow path) |
| History compaction | retained research (evaluation path, `compaction_mode`) |
| Tool-result offload | retained research (evaluation path, `tool_result_mode`) |
| Worker ownership enforcement | retained research (evaluation path, `ownership_mode`, default off) |

---

## 6. What closure may and may not claim

May be claimed, naming the entry point, when a status document says it is wired:

- the Context Plane reaches a real provider request, with request-level evidence;
- deterministic layout and direct-dependency selection;
- trusted worker identity, action gate, durable ledger, sandbox binding, suspension, shared budget;
- a router that can reject a candidate decomposition and did delegate once.

May **not** be claimed:

- any cache-hit, cache-miss, prefill or cost benefit that has not been measured end to end;
- a fair comparison against native AnyCode before the C1-B composition exists;
- that Placement, compaction or offload improves anything by default;
- that a mechanism ran because it appears in a manifest.

---

## 7. Document control

This roadmap is referenced by `docs/README.md` and `AGENTS.md`. Changing it requires the project
owner's decision. Stage reports may add detail; they may not widen the scope defined here.
