# Stage B0 — controlled real delegation

Harness for **Controlled Real Delegation Mechanism Validation**. One live attempt has been made
(`evidence/run-1/`, run `stage-b0-20261119T080450Z`); the experiment is closed and cannot be run
again from this branch. Read `docs/stage-b0-controlled-live.md` for the result and
`docs/stage-reliability-closure-b0.md` §H for the design.

Result: `B0_DELEGATION_MECHANISM_PASS_INTEGRATION_FAILED` — the Main delegated, the Router admitted
two concurrent children, the handoffs came back, the Main resumed and edited; the Run Budget then
rejected its next request and the integration stopped one assertion short of the public suite.

Because an attempt marker exists, `--freeze` and `--run` both refuse here by design. A future
authorized stage must start from a new branch with a fresh freeze, not by deleting the marker.

## What it asks

One question, mechanism only:

> With a Host-authored instruction that this run validates the delegation tool, does a real DeepSeek
> Main call `delegate_tasks`, does the Router admit two independent read-only children, do their
> executions overlap, does the Main get bounded summaries plus refs and resume its own loop, and does
> the integrated result pass the public suite and the independent oracle?

It does **not** ask whether a capable Main delegates on its own. Every artefact is labelled
`CONTROLLED_DELEGATION`; reporting any B0 result as `AUTONOMOUS_MAIN_DELEGATION` would be a
misrepresentation. Stage P1b remains the only autonomous sample, and it produced no delegation.

## Provenance of the parts

| Part | Source | Why |
|---|---|---|
| Fixture (23 files) | `experiments/fixtures/stage_p1b_dual_plugin`, digests equal to the P1b freeze | one fixture, one manifest implementation (`p1b_runner.fixture_manifest()`) |
| Public suite | `p1b_contract.PUBLIC_TESTS` | the same authorized command the Main may run |
| Oracle | `p1b_contract.ORACLE_COMMAND` | one oracle, not a copy; never registered as a tool |
| Goal text | `b0_contract.GOAL` (**B0's only new text**) | the controlled instruction is part of what the Main is told |
| System prompt | `atlasos.app.main_driven.MAIN_SYSTEM_PROMPT`, unchanged | delegation may not be forced by prompt surgery |

## Commands

```sh
python experiments/stage_b0_controlled/run_stage_b0.py --freeze     # write freeze.json
python experiments/stage_b0_controlled/run_stage_b0.py --preflight  # no model, no provider
python experiments/stage_b0_controlled/run_stage_b0.py --run        # ONE live attempt
```

`--run` writes `<scratch>/attempts/attempt-1.json` before `run_goal` and refuses to start when the
marker exists. There is no retry loop; a BLOCKED preflight exits 2 and a failed run exits 1.

The scratch directory defaults to `./.stage-b0-live` and may be overridden with `ATLAS_B0_SCRATCH`.
Run artifacts must stay outside `experiments/results` (the preflight checks this).

## Frozen (see `freeze.json`)

| Item | Value |
|---|---|
| Goal digest | `sha256:5995bcaa3395510896017bbd9e43466afc4482ee7387e61a0ee0e4c5285bb483` |
| Controlled instruction digest | recorded separately in the same file |
| Fixture | 23 byte digests, equal to `experiments/stage_p1b_live/fixture_manifest.json` |
| Model / budget / turns | `deepseek-chat`, 300 000 tokens, 20 Main turns |
| Delegation | 2 parallel children, 2 per call, 6 child turns, 300 s, 4 rounds |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, network none, 1 CPU / 1 GiB, 20 min |
| Write scope | Main `core/`; children read-only (no write grant) |
| Runtime SHA | recorded at freeze and again in the attempt marker |

Changing any of these after a run invalidates the run; re-freeze before the first attempt only.

## What the run must prove to be accepted

1. The Main called `delegate_tasks` (a real tool call in its own loop).
2. The Router admitted two children in one batch (`route=delegate_parallel`).
3. `max_observed_concurrency >= 2` with overlapping execution intervals.
4. Both children completed with distinct read-only investigations.
5. The Main received bounded summaries plus `ref_id`s and read back at most what it needed.
6. The Main resumed its own loop and made the integration edit itself.
7. Public suite and independent oracle both PASS.
8. Context, budget, ownership and sandbox evidence complete; sandbox destroyed.

If any of 1–3 fails, the stage is `B0_INCONCLUSIVE` (mechanism not exercised) rather than a
mechanism failure — and the reason must be attributed to the layer where it stopped: Main proposal,
Router admission, child execution, handoff, integration or verification.
