# Closure-2A — Final Benchmark Design

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`.

Three tasks, one benchmark version, no model run. Everything here is generated from
`experiments/evaluation/closure2/catalog.py`; the executable proof is
`python -m experiments.evaluation.closure2.preflight`.

| Item | Value |
|---|---|
| Benchmark | `closure2-final-benchmark-v1` |
| Tasks | P1 `closure2-pipeline-migration`, P2 `closure2-plugin-migration`, N1 `closure2-state-machine-refactor` |
| Families | P1 multi-module migration, P2 interface migration, N1 coupled refactor (negative control) |
| Model calls to author | **0** |
| Sandbox runs to author | **0** |

---

## 1. What each task is

Every task ships a **working v1 repository** that a reader can understand, and asks for a migration
to a frozen v2 contract published in `docs/INTERFACE.md`. Nothing is padded: there is no filler
document, no duplicated prose and no irrelevant module. The context load comes from real code that
has to be read before it can be changed.

### P1 — multi-module data pipeline migration

```
pipeline/parsing.py     name=value line parsing          (v1: no validation, no trim)
pipeline/normalise.py   field normalisation              (v1: identity, no count)
pipeline/aggregate.py   counting and ordering            (v1: occurrence count, unsorted)
pipeline/store.py       one record per name              (v1: append-only list)
pipeline/api.py         build_report(lines) -> dict      (v1: compose without normalise)
pipeline/report.py      render(report) -> str            (v1: insertion order)
docs/INTERFACE.md       frozen v2 contract (public)
test_public.py          9 visible checks
```

The v1 code runs and is internally consistent; it simply implements the old contract. The migration
adds validation, normalisation, a numeric field, deterministic ordering and last-write-wins storage —
six coupled decisions spread across six modules, none of which can be made without reading the
others.

**Candidate DAG**

| step | owns | depends on | wave |
|---|---|---|---|
| `parse` | `pipeline/parsing.py`, `pipeline/normalise.py` | — | 0 |
| `aggregate` | `pipeline/aggregate.py`, `pipeline/store.py` | — | 0 |
| `integrate` | `pipeline/api.py`, `pipeline/report.py` | `parse`, `aggregate` | 1 |

Parallel width 2, three workers, two waves. `parse` and `aggregate` touch **disjoint** files; only
`integrate` composes them, which is exactly the dependency the public requirement states.

### P2 — plugin interface migration

```
plugins/base.py         frozen v2 interface (Plugin.supports / Plugin.transform, PluginError)
plugins/csv_plugin.py   CSV plugin                        (v1: parse-only, no supports/transform)
plugins/csv_fields.py   CSV cell splitting                (v1: naive split)
plugins/json_plugin.py  JSON plugin                       (v1: parse-only, no flattening)
plugins/json_fields.py  JSON flattening                   (v1: dict() no-op)
plugins/registry.py     register / get                    (v1: silent overwrite, KeyError)
plugins/loader.py       load(payload, source)             (v1: "has a .parse attribute")
docs/INTERFACE.md       frozen v2 contract (public)
test_public.py          9 visible checks
```

This is deliberately **not** P1 with new file names. The two plugins have different inputs — CSV
quoting versus nested JSON objects — and different failure modes, so each worker needs its own local
reading. The shared file (`plugins/base.py`) is already v2 and is forbidden to edit, which is what
makes the two halves independent rather than speculative.

**Candidate DAG**

| step | owns | depends on | wave |
|---|---|---|---|
| `csv` | `plugins/csv_plugin.py`, `plugins/csv_fields.py` | — | 0 |
| `json` | `plugins/json_plugin.py`, `plugins/json_fields.py` | — | 0 |
| `register` | `plugins/registry.py`, `plugins/loader.py` | `csv`, `json` | 1 |

### N1 — coupled state machine refactor (offline negative control)

```
lifecycle/state.py      STATES + transition             (v1: three states, KeyError on bad input)
lifecycle/approval.py   approve(state)                  (v1: one event)
lifecycle/shipping.py   ship(state)                     (v1: one event)
lifecycle/audit.py      audit(state, event)             (v1: appends without transitioning)
docs/INTERFACE.md       target contract (public)
test_public.py          7 visible checks
```

The refactor adds two terminal states and two events, and every caller reads the same table. The
**candidate** plan is the honest naive decomposition: one worker extends the table, one worker wires
the callers — and both must edit `lifecycle/state.py`, because the new events are table entries that
the callers immediately use. There is therefore a real shared write and no independent wave.

N1 exists to check that the frozen router refuses this plan. It is not designed to make Atlas look
good: it is the case where decomposition is *not* available.

---

## 2. Seed, reference and oracle

Each task carries four artifacts, all in `catalog.py`:

| Artifact | Role | Where it lives |
|---|---|---|
| `seed` | the v1 repository the agent receives | staged into the sandbox, and nothing else is |
| `reference` | a working v2 implementation | repository only — used to prove the oracle is satisfiable |
| `public_tests` | `test_public.py`, visible | part of the seed |
| `oracle_tests` | `test_hidden_*.py`, hidden | repository only, uploaded to the oracle sandbox at grading time |

Verified by the preflight, per task — four **independent** pytest sessions, each with its own
disposable workspace and an explicit target list, so a public-test failure can never stand in for a
hidden-oracle failure (`docs/closure-2a-r1-review-repair.md`, R1-04):

| Check | P1 | P2 | N1 |
|---|---|---|---|
| seed + `test_public.py` → fails | `TEST_FAILURE` | `TEST_FAILURE` | `COLLECT_ERROR` |
| seed + hidden oracle → fails | `TEST_FAILURE` | `TEST_FAILURE` | `COLLECT_ERROR` |
| reference + `test_public.py` → passes | yes (9 tests) | yes (9 tests) | yes (7 tests) |
| reference + hidden oracle → passes and executes | yes (11 tests) | yes (12 tests) | yes (7 tests) |
| hidden test file/body absent from the workspace | yes | yes | yes |
| visible files equal the frozen seed, byte for byte | yes | yes | yes |
| live task digests equal the C2-A table | yes | yes | yes |
| reference replaces only seeded files | yes | yes | yes |

The N1 seed cannot *import* the v2 interface:
`ImportError: cannot import name 'reject' from 'lifecycle.approval'`. That is a real unimplemented
contract, and it is classified as `COLLECT_ERROR` — never reported as "the hidden assertion failed",
because no assertion in `test_hidden_lifecycle.py` ran against the seed. The reference session, which
does run them, is what proves the oracle tests are satisfiable and non-empty.

### 2.1 Oracle scope

The hidden tests check: the published interface shapes, functional behaviour, boundary inputs (empty
payload, no separator, multiple separators, whitespace-only values), error handling (`ValueError` /
`PluginError`), ordering and duplicate rules, cross-module integration (the composed entry point, not
just the leaf functions), and the compatibility clauses the public requirement states ("the public
call signatures do not change", "`build_report` propagates `ValueError`").

They do **not** require a particular implementation: no test inspects source text, class layout or
helper names. The one structural requirement — `report.clean_rows`-style monkeypatching — is absent
from these tasks on purpose, because that would constrain the implementation rather than the
behaviour.

### 2.2 Release boundary

The hidden tests and the reference implementations are committed to this repository
(`experiments/evaluation/closure2/catalog.py`). These tasks are therefore **not** secret held-out
data; only the agent workspace is held out. Every published result must carry that sentence. The
alternative — a private task store — would make the benchmark unverifiable by a reviewer, which is a
worse trade for a portfolio project.

---

## 3. Why the tasks are not rigged

| Risk | How it is avoided | How it is checked |
|---|---|---|
| the single worker is starved | the single arm gets the whole public requirement, the full shared block and the entire repository; `single_worker_turn` names every deliverable | `test_the_two_topologies_describe_the_same_task` |
| multi is handed extra information | both topologies carry the same `shared_context`, `visible_files`, `hidden_files`, deliverables, allowed and forbidden paths | same test, plus `task_taskspec_parity()` |
| the task is padding | every file is a real module in the dependency chain; no filler documents or duplicated fixtures | `test_every_task_is_a_migration_of_a_working_seed` |
| the oracle encodes the reference | hidden tests assert behaviour the public contract states; the reference is only used to prove satisfiability | §2.1 |
| the router is tuned to admit | the policy is `r0.1.0` unchanged, its digest is frozen in the preflight, and N1 is refused by the same untouched policy | `test_the_router_policy_is_the_frozen_one`, `test_the_router_decisions_are_the_registered_ones` |
| the router decision drifts while the policy digest stays put | the expected route, reason codes, blocked gate and wave-0 width are asserted as a gate, not recorded as an observation | `test_a_drifted_route_fails_the_gate`, `test_a_collapsed_first_wave_fails_the_gate` |
| the write sets are claimed but not true | the router extracts paths from the assignment text; the preflight asserts that extraction equals the owned tuple | `test_the_declared_write_sets_are_the_owned_write_sets` |
| the answer leaks into the workspace under another name | every hidden-test body and every answer-bearing reference body is searched for in the visible files | `test_a_reference_body_copied_into_the_workspace_is_caught`, `test_a_renamed_hidden_oracle_copied_into_the_workspace_is_caught` |

---

## 4. Failure modes this design accepts

1. **N1 may never be exercised live.** If the router refuses the plan, A2-P collapses to a single
   worker. That is the honest outcome and is preregistered as an unsupported cell, not a result.
2. **P1/P2 may be small enough for a single worker.** They are deliberately readable; the interesting
   question is cost and integration overhead, not whether one worker *can* solve them.
3. **Migration tasks are easier than greenfield work.** That is the point: an existing codebase is
   what makes the reading cost real and the decomposition meaningful.
4. **The reference is one of many valid solutions.** Nothing is scored against its text.
