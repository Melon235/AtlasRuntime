"""Scheduler (v1): one attempt per tick, no retry scheduling, no persistence."""

from __future__ import annotations

from dataclasses import replace

from schedkit.clock import Clock
from schedkit.executor import Executor
from schedkit.machine import STATES, transition
from schedkit.model import TERMINAL, AttemptOutcome, Job, JobRecord
from schedkit.recovery import recover
from schedkit.retry import Policy
from schedkit.store import JobStore


class Scheduler:
    def __init__(
        self,
        store: JobStore,
        clock: Clock,
        handler,
        policy: Policy | None = None,
        cache=None,
        events=None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.handler = handler
        self.policy = policy or Policy()
        self.events = events
        self.executor = Executor(clock, cache)

    def submit(self, job: Job) -> JobRecord:
        record = JobRecord(job=job, state="queued", next_run_at=self.clock.now())
        self.store.add(record)
        return record

    def cancel(self, job_id: str) -> bool:
        record = self.store.get(job_id)
        if record.state in TERMINAL:
            return False
        self.store.update(replace(record, state="cancelled"))
        return True

    def tick(self) -> list[JobRecord]:
        processed: list[JobRecord] = []
        for record in self.store.records():
            if record.state != "queued":
                continue
            if record.next_run_at > self.clock.now():
                continue
            running = replace(record, state=transition(record.state, "start"))
            self.store.update(running)
            outcome: AttemptOutcome = self.executor.attempt(running, self.handler)
            event = "finish" if outcome.state == "done" else "fail"
            final = replace(
                running,
                state=transition("running", event),
                attempts=outcome.attempts,
                result=outcome.result,
                error=outcome.reason,
            )
            self.store.update(final)
            processed.append(final)
        return processed

    def recover(self) -> int:
        return recover(self.store, self.clock)

    def summary(self) -> dict:
        return {"jobs": len(self.store.records())}

    def stats(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for record in self.store.records():
            counts[record.state] += 1
        return counts
