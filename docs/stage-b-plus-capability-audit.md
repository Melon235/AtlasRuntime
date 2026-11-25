# Stage B+ — Actual Capability Audit (Phases A, B, E)

Status: `STAGE_B_PLUS_OFFLINE_PREPARATION`
Baseline: `e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1` (branch `feat/stage-b-plus-capability`,
worktree `D:\Work\AtlasRuntime-stage-b-plus`)
Mode: offline only. Zero DeepSeek calls, zero sandboxes, zero B+ cells.

This document reports what the code does, not what it is meant to do. Every claim cites the module
that decides it, and the tables distinguish *registered* from *granted* from *offered* — the three
things S06 conflated.

---

## 0. Method and what was actually executed

Read: the worker catalog, the scoped-worker factory, the delegation tool and its wire schema, the
staged delegation service, the admission router, the frozen R0.1.0 gate, the M3 action gate, the R1-1
ownership policy and shell classifier, the handoff reader, the sandbox tool binding, and the Main
prompt of every stage that ran.

Executed (offline, deterministic, no model):

| What | How | Artefact |
|---|---|---|
| The exact Main provider request, per arm | the real `run_goal` with a recording fake provider | `experiments/stage_b_plus/inputs/request-C*.json` |
| The dynamic delegation path with a coding worker | the real `run_goal`, real router, real ownership, scripted proposal | `experiments/stage_b_plus/probe.json` |
| The oracle's discrimination on the three reused tasks | the frozen Closure-2 `task_selftest`, reused unchanged | `experiments/stage_b_plus/workload.json` |
| Freeze 1.6 immutability | `git diff` against the baseline + three byte digests | `offline.json` check `J13` |

Not executed, and therefore not claimed: any Main-driven delegation by a real model on a real task.

---

## A.1 Worker actual capabilities

Sources: `src/atlasos/workers/catalog.py:26` (the four shipped profiles),
`src/atlasos/workers/factory.py:35` (the tools a worker may be built with),
`src/atlasos/workers/factory.py:104` (the child `AgentConfig` is built from `profile.base_tools`),
`src/atlasos/workers/models.py:18` (a profile may not carry a delegation tool),
`src/atlasos/delegation/service.py:77` (what counts as a write tool),
`src/atlasos/ownership/shell.py:41` (the shell classifier's read-only allowlist).

| Capability | `research_worker` | `coding_worker` | `data_worker` | `general_worker` |
|---|---|---|---|---|
| File read | Allowed (`file_read`) | Allowed | Allowed | Allowed |
| File list | Allowed (`list_files`) | Allowed | Allowed | Allowed |
| Search | Allowed (`grep`, `tool_search`) | Allowed | Allowed | Allowed |
| File create | **Denied** (no `file_write`) | Conditional — tool present, every write checked against the Host grant | Denied | Denied |
| File modify | **Denied** (no `file_edit`) | Conditional | Denied | Denied |
| Bash execution | **Denied** (no `bash`) | Conditional — only commands the classifier proves read-only or whose write targets are inside the grant; every interpreter invocation refused | Denied | Denied |
| Test execution | **Denied** | **Denied** — `bash` refuses `python3 -m pytest` as `UNCLASSIFIABLE`; the profile has no `run_public_tests` | Denied | Denied |
| Artifact generation | Conditional — a report in the final message only | Conditional — files inside the grant | Report only | Report only |
| Handoff return | Allowed — bounded summary + `ref_id` | Allowed | Allowed | Allowed |
| Network access | Denied — no network tool exists; the sandbox is `network: none` | Denied | Denied | Denied |
| Recursive delegation | **Denied, twice** — the profile validator refuses a delegate tool, and the runtime refuses a non-Main caller | same | same | same |
| Ownership restriction | Read-only by construction: it declares no paths, so every write is `OWNERSHIP_DENIED_UNDECLARED_PATH` | Writes only the paths the Main declared *and* the Host granted | Read-only | Read-only |

Effective turn caps, which are **not** the run-level configuration: the child agent is built with
`min(profile.max_turns, delegation.child_max_turns)` (`delegation/service.py:333`). The shipped caps
are 6 / 8 / 6 / 4. **S06 requested 6 and got 6; a run that requests 20 for `coding_worker` still gets
8 unless the profile object itself is replaced.** That is the S07 finding, and it is now recorded in
the run's own evidence (`session.json → delegation_controls.profiles.*.effective_max_turns`) rather
than assumed.

Three distinctions that S06 collapsed, restated because the ablation depends on them:

* **Registered** — the tool exists in the run's `ToolRegistry`. All eleven Main-side tools are
  registered in every run.
* **Offered** — the tool appears in this agent's provider request. AnyCode enforces this per agent:
  a child that emits an unoffered tool is refused with `Tool "file_write" is not allowed for this
  agent.` (observed, probe `research_tool_escape`).
* **Granted** — the R1-1 manifest authorises this specific path for this specific worker. This is the
  only one of the three that is a security boundary; the other two are hygiene.

---

## A.2 Main-visible capability surface

Captured from the real composition, not from configuration. The fingerprint of each arm is in
`experiments/stage_b_plus/inputs/diff.json`; the full payload is next to it.

Common to all four arms (byte-identical): system prompt **9,033 chars**
`sha256:76ebb03d51b7d…` (the B+ prompt, §C), ten tool definitions, the `delegate_tasks` input schema
**1,131 chars** `sha256:b76380f3…`, and the first user turn (`Task: main-driven goal\n\n<goal>`).

| Arm | Worker profile | Exposure | `delegate_tasks` description | Tools block |
|---|---|---|---|---|
| C0 | `research_worker` | native | 1,613 chars `8a801b21` | 3,004 chars |
| C1 | `research_worker` | card | 3,030 chars `3f364f23` | 4,421 chars |
| C2 | `coding_worker` | native | 1,805 chars `9fc86fd3` | 3,196 chars |
| C3 | `coding_worker` | card | 3,336 chars `365593c9` | 4,727 chars |

The shipped description is 819 chars. Between any two arms, the **only** fields that differ are
`delegate_tasks.description_*` and the aggregate `tools.chars`/`tools.digest` that contain it
(`offline.json` check `J01`). That is the property the four-arm design needs and S06 did not have.

### What the request actually contains, and what it does not

| Question | Answer | Evidence |
|---|---|---|
| Does the Main know which worker roles exist? | **Only if something names them.** The wire schema has no `enum`, and `profile_id` defaults to `research_worker`. Nothing in the shipped runtime enumerates profiles. | `models.py:41-72`; captured schema `profile_enum = null`, `profile_default = "research_worker"` |
| Does it know what a worker can read, write or execute? | **No**, in the shipped state. The description says only "Subworkers are read-only unless the host granted them writable paths" — a constant statement about all workers, unrelated to which profile is offered | `delegation/tool.py:30-38` |
| Does it know which capabilities are restricted? | Partially and unreliably: two constant claims (read-only by default, cannot delegate) plus, in the *P3 prompt only*, one hand-written paragraph about `research_worker` | `experiments/stage_s2_prompt_v21/prompts/main_v21.md:118-142` |
| Does it know how to assign ownership? | **No.** `write_paths` exists in the schema but nothing says it must be inside a Host grant, nor which grant exists | `models.py:52-54`; `delegation/router.py:193-216` |
| Does it know how a deliverable returns? | **Yes.** The description states a bounded summary plus a `ref_id`, and `delegate.read` is described independently | `delegation/tool.py:30-38`, `delegation/handoff.py:205-221` |
| Can it propose an executable coding task? | **Yes, mechanically.** `profile_id` accepts `coding_worker`, and two disjoint declared paths are admissible. Verified end to end in Phase B | probe `coding_parallel` |
| Can it distinguish `research_worker` from `coding_worker`? | **No**, from the runtime surface alone. Distinguishing them requires either knowing the profile ids or reading a disclosure | captured schema + shipped description |

### The one place a profile *was* described

The P3 prompt (S06's P3 arm, and the S2 pilot) contains a `## WORKER CAPABILITY CONTRACT` section
that enumerates `research_worker`'s tools and states it has no `bash`, `python3`, `pytest` or
`run_public_tests`. It is accurate — and it is hand-written, profile-specific and constant. Under a
different worker profile it is worse than silence: it instructs the Main to use a worker the arm is
trying to replace. That is why the four-arm matrix uses a prompt without it (§C of the design
document), and why the same paragraph is preserved as `p3-literal` for the Owner to compare against.

---

## B. Dynamic coding worker feasibility

Five scripted probes, each a separate `run_goal` against the production composition
(`experiments/stage_b_plus/probe.py`; results in `probe.json`). A scripted proposal is **not** counted
as autonomous delegation anywhere in this stage.

| Probe | Proposal | Router verdict | Children | Outcome |
|---|---|---|---|---|
| `coding_parallel` | two `coding_worker` tasks, paths `package/a.py` and `package/b.py` | `delegate_parallel`, `DELEGATION_ADMITTED` | 2 | both `completed` (5 and 3 turns); both writes landed; the third write outside the declaration was denied; `python3 -m pytest` was denied while `echo` was allowed; the Main read a handoff and then edited `package/integration.py` |
| `research_write` | one `research_worker` task declaring a write | `direct`, `WRITE_SCOPE_NOT_GRANTED` | 0 | refused before a worker was built |
| `shared_write` | two `coding_worker` tasks declaring the same path | `direct`, `SHARED_WRITE_RISK` | 0 | refused |
| `single_coding` | one `coding_worker` task | `direct`, `TRIVIAL_DIRECT_TASK` | 0 | refused |
| `research_tool_escape` | two `research_worker` tasks; the child emits `file_write` anyway | `delegate_parallel`, admitted | 2 | the call was refused by the runner (`not allowed for this agent`), no file created, no ownership denial raised |

### What this establishes

1. **The dynamic coding path works.** Profile resolution, admission, child construction, tool
   enforcement, write confinement, handoff delivery and Main continuation all happen in production
   code. There is no missing wiring between "static DAG has a coding worker" and "dynamic delegation
   can use one".
2. **The enforcement layers are layered, not redundant.** The profile gate refuses at the request
   surface; R1-1 refuses at execution; the router refuses the plan. A child that bypasses the first
   is stopped by the second, and a probe proves it.
3. **A coding worker cannot run tests.** `bash` refuses every interpreter invocation and no profile
   carries `run_public_tests`. In the candidate matrix the Main must therefore run the public suite
   itself, and a coding worker's "verified" claim is unverifiable. This is a **design property that
   the disclosure must state**, not a bug to fix inside this stage.
4. **Single-task coding delegation is impossible by construction.** A one-task proposal is
   `TRIVIAL_DIRECT_TASK`; a one-task *read-only* proposal is admitted only if the evidence is
   complete. So the coding capability is only ever exercised as "two disjoint module edits". Any
   reading of C2/C3 must use that definition of capability, and a Main that wants one coding worker
   gets a refusal it has to interpret.

### Gaps that need an Owner decision

| id | Gap | Consequence | Minimal fix (not applied) |
|---|---|---|---|
| G-B+1 | A worker turn cap is the profile's, not the run's (`min` at `service.py:333`) | Two profiles cannot be given one turn budget without replacing the profile objects; S06 could not tell 6 from 20 | already handled in this stage: `GoalRunOptions.worker_max_turns` refuses a value above the delegation limit and records the effective number |
| G-B+2 | A coding worker cannot execute a test suite | "worker verified its change" is unobtainable from a child | either accept that the Main verifies (current design), or add a Host-fixed test action to the child surface — a permission change requiring an ADR |
| G-B+3 | Single coding delegation is refused as trivial | capability is only reachable as a pair | none proposed: relaxing gate 1 would weaken the router for every caller |
| B-B+4 | The wire schema hardcodes `profile_id="research_worker"` as a default | in a coding-only arm, a proposal that omits `profile_id` is refused `UNKNOWN_PROFILE` — a schema-shaped failure that looks like an exposure failure | do not edit the frozen schema; state the offered profile explicitly in the disclosure and count `UNKNOWN_PROFILE` separately (both done) |

No runtime redesign is proposed. None of these blocks the ablation; G-B+2 and G-B+3 change how the
result must be read.

---

## E. Workload compatibility

Three tasks, reused from Closure-2 unchanged (`experiments/evaluation/closure2/catalog.py`):

| Task | Family | Write scope | Deliverables | Frozen workers (audit only) |
|---|---|---|---|---|
| `closure2-pipeline-migration` | P1 | `pipeline/**` | 6 files | `parse`, `aggregate`, `integrate` |
| `closure2-plugin-migration` | P2 | `plugins/**` | 4 files | `csv`, `json`, `register` |
| `closure2-state-machine-refactor` | N1 | `lifecycle/**` | 4 files | `machine`, `callers` — both own `lifecycle/state.py` |

What the Main receives, and what it does not:

| Input | Given to the Main | Evidence |
|---|---|---|
| Natural-language goal | **yes** — `task.prompt`, the public requirement paragraph | `contract.GOAL_OF` |
| Readable workspace | **yes** — the seed only; `docs/INTERFACE.md` is inside it, so the frozen contract is a repo file rather than a prompt appendix | `workload.json` |
| Tool surface | **yes** — the normal eleven, unchanged | captured request |
| Frozen DAG | **no** | `contract.task_record()["main_visible_inputs"]` |
| Worker assignment | **no** | same |
| Placement decision | **no** | same |
| Hidden oracle | **no** — never staged into the workspace | `preflight.answer_leak_guard`, unchanged |
| Reference implementation | **no** | same |

No task difficulty was changed, no extra subtask was added and no hint was inserted. The only
adaptation is the one Phase E allows: the goal is the requirement paragraph rather than Closure-2's
composed worker assignment, because a Main-driven run has no worker assignment to compose.

**Oracle satisfiability re-proved, not assumed.** `closure2_preflight.task_selftest` is called
unchanged for all three tasks (`workload.json`): the seed fails the hidden suite, the reference
passes it, the hidden suite really executes assertions, and the four sessions use distinct
workspaces. The public and hidden target lists are disjoint for every task.

**Natural delegation opportunity, by task:**

* **P1** — two independent module halves (parsing+normalisation, aggregation+store) plus a downstream
  integration pair. Two disjoint write sets exist, so a coding pair is admissible. Expected to be the
  arm-discriminating task.
* **P2** — two independent plugins plus registration. Same shape, different problem.
* **N1** — both candidate steps must edit `lifecycle/state.py`. A Main that proposes two workers there
  is refused `SHARED_WRITE_RISK`; a Main that proposes one is refused `TRIVIAL_DIRECT_TASK`. This is
  the negative control, and the recorded refusal reason is the evidence, not a failure rate.

---

## H1 / H2 assessment

Both Owner hypotheses are partly true, and they are not symmetric.

**H1 — "worker capability was insufficient" — confirmed as a configuration fact, not a hypothesis.**
In all 36 S06 cells `child_write_grants` was `{}`
(`experiments/stage_benchmark/execution/provision.py:496`) and the only described profile was
`research_worker`, which has no write tool and no shell. S06 therefore offered a worker that could
only read, on five tasks whose deliverable is a written file. Any delegation would have been
read-only research against a task that needed an edit. This alone does not explain a **zero** proposal
rate — a Main could still have delegated reading — but it removes the strongest reason to.

**H2 — "the Main could not see the capability" — confirmed, with a sharper statement than the
hypothesis.** There is no profile enumeration anywhere in the request: not in the schema (no `enum`),
not in the description (a constant sentence), not in the prompt (nothing, except P3's paragraph about
one profile). The Main's information about workers is therefore *independent of which worker exists*.
Changing the available profile does not change the Main's input at all unless the disclosure changes
with it — which is precisely why the two factors cannot be called independent (§D of the design
document).

**What the audit cannot decide.** Whether a Main that *can* see a writable worker will delegate is a
behavioural question. Nothing in this audit answers it, and no number here should be read as a
delegation rate.
