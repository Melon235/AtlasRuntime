"""Executor (v1): one attempt, no deadline, no cancellation and no replay."""

from __future__ import annotations

from schedkit.clock import Clock
from schedkit.model import AttemptOutcome, JobRecord


class Executor:
    def __init__(self, clock: Clock, cache=None) -> None:
        self.clock = clock
        self.cache = {} if cache is None else cache

    def attempt(self, record: JobRecord, handler, *, cancelled: bool = False) -> AttemptOutcome:
        job = record.job
        try:
            result = handler(job)
        except Exception as error:  # noqa: BLE001 - the reason is part of the contract
            return AttemptOutcome(
                state="failed",
                attempts=record.attempts + 1,
                reason=str(error),
            )
        self.cache[job.id] = result
        return AttemptOutcome(state="done", attempts=record.attempts + 1, result=result)
