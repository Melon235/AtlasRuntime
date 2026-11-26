# Scheduler kit

A deterministic job scheduler with a state machine, a persistent job store, a capped retry policy,
an executor and restart recovery. All time comes from an injected clock.

`docs/INTERFACE.md` is the frozen contract; `schedkit/errors.py`, `schedkit/clock.py` and
`schedkit/model.py` are frozen. The rest of the package is still v1.

Run the public checks with `python -m pytest -q test_public.py`.

## Layout

- `schedkit/machine.py` — the transition table.
- `schedkit/retry.py` — the policy and the capped delay.
- `schedkit/store.py` — persisted job records.
- `schedkit/executor.py` — one attempt, with deadline, cancellation and replay.
- `schedkit/scheduler.py` — submit, cancel, tick, stats.
- `schedkit/recovery.py` — a restart moves running jobs back to queued.
