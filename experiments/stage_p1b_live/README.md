# Stage P1b — dual-plugin delegation experiment

Harness for `docs/stage-p1b-main-driven-delegation.md`. Nothing here is part of the runtime or the
test suite; the frozen configuration lives in `p1b_contract.py` and `fixture_manifest.json`.

## Layout

| Path | Purpose |
|---|---|
| `../fixtures/stage_p1b_dual_plugin/` | the provisioned fixture (`eventpipe`, 23 files) |
| `p1b_contract.py` | goal text, public suite contract, hidden oracle, acceptance contract, limits |
| `fixture_manifest.json` | frozen digests of the fixture, the goal, both commands and the limits |
| `reference/core/` | reference implementation of the three holes — **harness only**, never provisioned |
| `verify_offline.py` | solvability, oracle discrimination, isolation |
| `check_router_admission.py` | what the router does with this task shape |
| `rehearse_sandbox.py` | real container rehearsal, no model call |
| `run_stage_p1b_live.py` | `--preflight` / `--run`; one attempt marker, no retry |
| `inspect_run.py` | joins the trajectory and the conversation into one evidence object |
| `evidence/run-1/` | the single authorized live run |

## Commands

```sh
# offline, no model, no sandbox
python experiments/stage_p1b_live/verify_offline.py --freeze   # rewrite the manifest
python experiments/stage_p1b_live/verify_offline.py            # expects READY=True
python experiments/stage_p1b_live/check_router_admission.py     # expects READY=True

# real container, no model call
python experiments/stage_p1b_live/rehearse_sandbox.py           # expects READY=True

# live: exactly one attempt
python experiments/stage_p1b_live/run_stage_p1b_live.py --preflight
python experiments/stage_p1b_live/run_stage_p1b_live.py --run

# read the result
python experiments/stage_p1b_live/inspect_run.py --scratch "$TEMP/atlas-p1b-live-1" --out evidence.json
```

`--run` writes a persistent marker at `<scratch>/attempts/attempt-1.json` immediately before
`run_goal` and refuses to start when one already exists. There is no retry loop: a BLOCKED
preflight exits 2, a failed run exits 1, and both leave their evidence in place. Deleting the marker
by hand starts a second live attempt, which the stage rules forbid without a new approval.

## What is frozen

`run_stage_p1b_live.py` refuses to start when any of these differ from `fixture_manifest.json` or the
constants in `p1b_contract.py`: the 23 fixture digests, the goal digest, `MAIN_SYSTEM_PROMPT`,
`src/` cleanliness, the oracle/public separation, and the Main's write scope (`core/`). The runtime
preflight (19 checks) runs on top of that, and `run_goal` re-runs it before it builds a provider.

## Notes

* `python3 -m pytest` (the oracle) writes `__pycache__`; the export happens **before** the oracle
  runs, so the exported patch stays clean. The fixture also ships a `.gitignore` for it.
* The sandbox provider joins command output without newlines
  (`atlasos/sandbox/opensandbox_provider.py::_execution_text`), so `inspect_run.py` parses the
  exported patch with a regular expression and `workspace.patch` is not directly applyable. That is
  a recorded runtime defect (DEV-2), not a harness bug.
