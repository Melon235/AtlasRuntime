"""Recovery (v2): running records go back to queued with their attempts preserved."""

from __future__ import annotations

from dataclasses import replace

from schedkit.machine import transition


def recover(store, clock, events=None) -> int:
    recovered = 0
    for record in store.records():
        if record.state != "running":
            continue
        queued = replace(
            record,
            state=transition("running", "recover"),
            next_run_at=clock.now(),
            error="recovered after restart",
        )
        store.update(queued)
        if events is not None:
            events.record(queued, "recovered", clock.now())
        recovered += 1
    return recovered
