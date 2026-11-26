"""Executor (v2): one attempt with replay, cancellation and deadline checks."""

from __future__ import annotations

from schedkit.clock import Clock
from schedkit.model import AttemptOutcome, JobRecord


class Executor:
    def __init__(self, clock: Clock, cache=None) -> None:
        self.clock = clock
        self.cache = {} if cache is None else cache

    def attempt(self, record: JobRecord, handler, *, cancelled: bool = False) -> AttemptOutcome:
        job = record.job
        if job.id in self.cache:
            return AttemptOutcome(
                state="done",
                attempts=record.attempts,
                result=self.cache[job.id],
                replayed=True,
            )
        if cancelled:
            return AttemptOutcome(state="cancelled", attempts=record.attempts)
        if job.deadline is not None and self.clock.now() >= job.deadline:
            return AttemptOutcome(state="dead", attempts=record.attempts, reason="timeout")
        try:
            result = handler(job)
        except Exception as error:  # noqa: BLE001 - the reason is part of the contract
            return AttemptOutcome(
                state="failed",
                attempts=record.attempts + 1,
                reason=f"{type(error).__name__}: {error}",
            )
        self.cache[job.id] = result
        return AttemptOutcome(state="done", attempts=record.attempts + 1, result=result)
