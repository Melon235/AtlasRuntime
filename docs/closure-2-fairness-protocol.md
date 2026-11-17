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

Arm config digests (frozen, per arm, revision `closure-2a-r1`). Each digest covers the arm record,
the topology budget slice, the shared resource contract, the repository-relative config path **and
the effective content of that YAML**:

| Arm | `config_digest` |
|---|---|
| N0 | `e74f8e36fafb531091233ab99da17e7696b437ada36343150b6b1f2c14a670ac` |
| N1 | `aadc7f129ce23239dfc80d07c100373ebf44055337976e61ef9b42cce0bda746` |
| A0 | `4ae0f21142df3ff6b0b9046a92c191be3ac144380648dce1b4115e3dda5de1f1` |
| A1-L | `296a1db1e70124e92121ab1cb390cebe97d5043404c0c4d7a55dbffe67ac0a53` |
| A1-P | `fb1a2dff282e84bd855d7647d1599c9bff497463c7052436639c36e6858a3633` |
| A2-P | `a1bc879ad7c1227a4c8a83746ad284fc1850157a4a5891bfb46b68e5350fe8b2` |

Config semantic digests (content only, path included, formatting ignored):

| Config (repository-relative) | `config_semantic_digest` |
|---|---|
| `experiments/configs/final4-context.yaml` | `16f587b3696e5d4e9f9a7868aa0859c89da876975ef7060f8a2936ce72899eb8` |
| `experiments/configs/closure2-shared-prefix.yaml` | `751beb1506602e250f240f69771514c7c2b569e3903c01060a554455dcfc549a` |

**A1-L vs A1-P** is the only pair where the difference is one config key: `context.prompt_layout`.
Same policy, renderer, transform, tools, budget, DAG, tasks. A unit test asserts that the two arm
records differ in exactly `prompt_layout` and `config`; `preflight.layout_ablation_delta()` flattens
both YAML files and requires `context.prompt_layout` to be the **only** differing effective key.
Mutating any other key — `provider.model`, `tools.max_results`, `sandbox.image` — makes
`only_layout_differs` false and fails the preflight.

Because the digest is derived from the parsed YAML, renaming or reformatting a file cannot disguise
a value change, and a comment-only edit cannot fake one.

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

### 2.1 Oracle independence, answer containment and routing evidence

Three properties must hold *per task*, and each is a gate rather than a recorded observation:

| Property | Mechanism | Negative control |
|---|---|---|
| the hidden oracle is verified independently of the public tests | four separate pytest sessions — seed+public, seed+hidden, reference+public, reference+hidden — each in its own disposable workspace with an explicit target list | `test_a_failing_public_test_does_not_turn_into_a_failing_oracle` |
| an unusable session is not reported as a defect | session kinds `PASSED` / `TEST_FAILURE` / `COLLECT_ERROR` / `NO_TESTS_COLLECTED` / `NO_TARGETS` / `USAGE_ERROR` / `INTERRUPTED` / `INTERNAL_ERROR` / `TIMEOUT`; only `TEST_FAILURE` and `COLLECT_ERROR` count as the seed failing the contract | `test_an_empty_target_list_is_refused` |
| the workspace contains no answer | visible files must equal the frozen seed byte for byte; every hidden-test body and every answer-bearing reference body is searched for in the visible files; live task digests are compared with the C2-A table | `test_a_reference_body_copied_into_the_workspace_is_caught`, `test_a_renamed_hidden_oracle_copied_into_the_workspace_is_caught`, `test_a_seed_edit_breaks_the_identity_against_the_frozen_baseline` |
| the router decides what the registration says | route, required and forbidden reason codes, required and forbidden blocked gates, wave-0 width and wave-0 write disjointness are all asserted | `test_a_drifted_route_fails_the_gate`, `test_a_drifted_reason_code_fails_the_gate`, `test_a_collapsed_first_wave_fails_the_gate` |

Sharing a *file name* between the seed and the reference is by design and is not leakage: every
reference module migrates a seeded file. Only a body that differs from its frozen seed counterpart
can carry an answer. Snippet-level leakage is **not** detected — the documented boundary is whole
files and whole bodies.

N1's seed cannot import the v2 interface, so its seed-leg hidden session is a `COLLECT_ERROR`, not
an assertion failure. The preflight reports it as such (`oracle_independence[...].seed_hidden_cause`)
and proves the oracle is real through the reference leg, which executes 7 assertions. This is the
Closure-2A false positive that R1 removed.

---

## 3. Model and resource contract

Applies to every arm in Closure-2B. The parameter values are frozen inside the registration payload.
The **approval** is a separate record (`protocol.APPROVAL_RECORD`, currently
`PENDING_OWNER_APPROVAL`) and is deliberately outside the digest, so approving the budget cannot
change the identity of what was approved. Closure-2B must not start before
`protocol.require_execution_approval()` accepts: status `APPROVED`, `approved_digest` equal to the
frozen digest, plus approver, approval commit and timestamp. See
`docs/closure-2-preregistration.md` §4.

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
