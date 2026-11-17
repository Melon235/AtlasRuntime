# Closure-2A — Fairness Protocol

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`.

What must be identical, what is allowed to differ, and what difference is *not* removed. Generated
from `experiments/evaluation/closure2/protocol.py`.

---

## 1. The arm matrix

| Arm | Engine | Topology | Context | Layout | Route | Config |
|---|---|---|---|---|---|---|
| **N0** | Native AnyCode 0.9.0 | single | native | native prompt | off | — |
| **N1** | Native AnyCode 0.9.0 | multi | native prerequisite context | native prompt | off | — |
| **A0** | AtlasRuntime | single | task-scoped | legacy | off | `final4-context.yaml` |
| **A1-L** | AtlasRuntime | multi | task-scoped | legacy | off | `final4-context.yaml` |
| **A1-P** | AtlasRuntime | multi | task-scoped | shared-prefix | off | `closure2-shared-prefix.yaml` |
| **A2-P** | AtlasRuntime | multi | task-scoped | shared-prefix | selective (`r0.1.0`) | `closure2-shared-prefix.yaml` |

Arm config digests (frozen, per arm):

| Arm | `config_digest` |
|---|---|
| N0 | `38ed2c84d7b475c3415517b8f608f237e35d4809adf9ff84cce99482b95df9ce` |
| N1 | `ae513f8c9f5236d193e25a6ec1208292160c5a36f3e85c97753c920e349ad4a3` |
| A0 | `90dd141ed8e749351d7649c2d20fbbd834b091de58fb17f43a066563bc490d18` |
| A1-L | `c1f5a71581158637b00b5a0d55cd8a3d0f01641913c3f84d591a6de39a8ccc9e` |
| A1-P | `25ed19eb4513c6f3d7acf280c18c7e18f71e1f3898b9a91ede82d19f1b54552b` |
| A2-P | `1127fbee99ac1085e43b3f337818b84f934bce2d0425a1d54b6f36190658cc73` |

**A1-L vs A1-P** is the only pair where the difference is one config key: `context.prompt_layout`.
Same policy, renderer, transform, tools, budget, DAG, tasks. A unit test asserts that the two arm
records differ in exactly `prompt_layout` and `config`.

**A1-P vs A2-P** differs in one field (`route`). If the router selects the same DAG, the two cells
are not independent samples and the difference must not be reported as a router benefit.

---

## 2. Digest parity

`effective_public_task_digest` (Closure-1C) is the gate. Per task:

| Group | Rule |
|---|---|
| `N0` = `A0` | same single-worker `BenchmarkTask` → identical digest |
| `N1` = `A1-L` = `A1-P` | same multi-worker `BenchmarkTask` → identical digest |

Both engines call one builder, `catalog.build_task(task, topology=...)`, so the digest cannot drift
between arms by construction. `preflight.digest_parity()` re-derives it per arm and asserts equality
inside each group.

Single vs multi digests **differ on purpose** (mode, worker steps and DAG are part of the public
input). Cross-topology comparison is therefore gated on a separate checklist instead:

| Cross-topology requirement | Checked by |
|---|---|
| same overall business requirement | `single.prompt` contains the shared block; `multi` steps all carry it |
| same initial Git fixture | `same_visible_files` |
| same public interface | `docs/INTERFACE.md` is in both visible sets |
| same hidden oracle | `same_hidden_files` |
| same final deliverables | `same_required_artifacts` |
| same allow/forbid rules | `same_allowed_paths`, `same_forbidden_paths` |

If any of those is false for a task, that task is `NOT_COMPARABLE` across topologies and must not
enter the formal matrix. All three tasks currently pass.

---

## 3. Model and resource contract

Applies to every arm in Closure-2B. Values are a **proposal** — `BUDGET["approval"]` is
`PENDING_OWNER_APPROVAL` and Closure-2B must not start before that is changed by the owner.

| Knob | Single | Multi |
|---|---|---|
| max turns | 36 (one worker) | 12 per worker × 3 = 36 theoretical |
| token budget (run) | 260 000 | 260 000 |
| max tool calls | 90 | 90 |
| wall clock | 1800 s | 1800 s |

Shared, identical in every arm: temperature 0.0, `max_tokens` 8000 per request, image
`atlas-uat-python:3.12`, 1 CPU / 1 Gi, `network=none`, workspace `/workspace/project`, the task seed
staged as a fresh git repository per run, and the isolated sandboxed oracle with the same hidden
tests.

**Why 260k.** The historical multi-worker mean on E1 A1 was ~139k prompt tokens per run over 12
tasks × 3 repetitions; these tasks have three modules and a larger fixture, so 260k is roughly 1.9×
that load and still below the 300k ceiling the owner set for discussion. 90 tool calls keeps the
Final-4 ratio (14 turns / 40 calls) scaled to 36 turns. 1800 s is twice the Final-4 timeout because
each run now provisions, migrates and grades three modules. **No number was tuned against a model
run**; nothing was executed to choose them.

---

## 4. Budget semantics are not equivalent — and are reported separately

| | Atlas | Native |
|---|---|---|
| mechanism | shared run budget guarded **before send** | no in-run guard |
| effect of overrun | the request is refused, the run can end incomplete | the run completes; the same thresholds are applied **after the fact** |
| enforced limit | token budget + wall clock | wall clock only |

Consequence: a `TASK_SUCCESS` comparison alone cannot separate budget effects. Every result must
report, separately:

1. oracle pass,
2. budget overrun (yes/no, with the numbers),
3. requests rejected by the runtime,
4. "deliverable passed but the budget failed".

Claiming the two arms have equivalent hard limits would be false and is forbidden.

---

## 5. Known differences that are **not** removed

| Difference | Why it stays | How it limits the claim |
|---|---|---|
| native has no context plane, no gate, no ledger, no trajectory, no budget guard | those are the mechanisms under test | N-vs-A is a **whole-system** comparison |
| native tool output formatting is plainer | the neutral adapter is the fair minimum, not a clone | a difference in tool ergonomics is part of the system delta |
| dependency delivery: Atlas projection vs AnyCode prerequisite context | each engine's native mechanism | do not attribute the difference to the Context Plane alone |
| prompt layout: native has no shared-prefix layout | the layout is an Atlas mechanism | A1-L vs A1-P isolates it; N1 is not part of that pair |
| budget enforcement (§4) | different runtimes | report budget outcomes separately |
| oracle execution: identical image and tests | — | not a source of difference |

**Attribution rule.** Only `A1-L` vs `A1-P` supports a layout claim. Only the router audit supports a
routing claim. `N0/N1` vs `A0/A1` supports "the Atlas system" as a whole and nothing narrower.

---

## 6. Execution order

Tasks × repetitions are the outer loop, arms the inner loop, and the arm order rotates per
repetition. No arm is run to completion before the next one starts. Three paired repetitions per
pre-registered cell is the minimum; adding repetitions later is allowed only for *all* arms of a
cell, never for one arm that looked bad.
