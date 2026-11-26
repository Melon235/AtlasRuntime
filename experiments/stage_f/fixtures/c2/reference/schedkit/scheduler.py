"""Scheduler (v2): due records, one attempt per tick, capped retries, persistence."""

from __future__ import annotations

from dataclasses import replace

from schedkit import jobspec, metrics
from schedkit.clock import Clock
from schedkit.executor import Executor
from schedkit.machine import STATES, transition
from schedkit.model import TERMINAL, AttemptOutcome, Job, JobRecord
from schedkit.recovery import recover
from schedkit.retry import Policy, next_delay
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
        if not callable(handler):
            raise ValueError("handler must be callable")
        self.store = store
        self.clock = clock
        self.handler = handler
        self.policy = policy or Policy()
        self.events = events
        self.executor = Executor(clock, cache)

    def _log(self, record: JobRecord, event: str) -> None:
        if self.events is not None:
            self.events.record(record, event, self.clock.now())

    def _cap(self, job: Job) -> int:
        requested = job.max_attempts if job.max_attempts is not None else self.policy.max_attempts
        return min(requested, self.policy.max_attempts)

    def submit(self, job: Job) -> JobRecord:
        jobspec.validate(job)
        record = JobRecord(job=job, state="queued", next_run_at=self.clock.now())
        self.store.add(record)
        self._log(record, "submitted")
        return record

    def cancel(self, job_id: str) -> bool:
        record = self.store.get(job_id)
        if record.state in TERMINAL:
            return False
        cancelled = replace(record, state=transition(record.state, "cancel"))
        self.store.update(cancelled)
        self._log(cancelled, "cancelled")
        return True

    def _settle(self, running: JobRecord, outcome: AttemptOutcome) -> JobRecord:
        if outcome.state == "done":
            final = replace(
                running,
                state=transition("running", "finish"),
                attempts=outcome.attempts,
                result=outcome.result,
                error=None,
            )
            event = "done"
        elif outcome.state == "cancelled":
            final = replace(
                running,
                state=transition("running", "cancel"),
                attempts=outcome.attempts,
                error=outcome.reason,
            )
            event = "cancelled"
        elif outcome.state == "dead":
            failed = replace(
                running,
                state=transition("running", "fail"),
                attempts=outcome.attempts,
                error=outcome.reason,
            )
            final = replace(
                failed,
                state=transition("failed", "exhaust"),
                error=outcome.reason,
            )
            event = "dead"
        else:
            failed = replace(
                running,
                state=transition("running", "fail"),
                attempts=outcome.attempts,
                error=outcome.reason,
            )
            if outcome.attempts >= self._cap(running.job):
                final = replace(
                    failed,
                    state=transition("failed", "exhaust"),
                    error=outcome.reason,
                )
                event = "dead"
            else:
                final = replace(
                    failed,
                    next_run_at=self.clock.now() + next_delay(outcome.attempts, self.policy),
                )
                event = "retry"
        self.store.update(final)
        self._log(final, event)
        return final

    def tick(self) -> list[JobRecord]:
        processed: list[JobRecord] = []
        now = self.clock.now()
        for record in self.store.records():
            if record.state not in ("queued", "failed"):
                continue
            if record.next_run_at > now:
                continue
            current = record
            if current.state == "failed":
                current = replace(current, state=transition("failed", "retry"))
            running = replace(current, state=transition("queued", "start"))
            self.store.update(running)
            self._log(running, "started")
            processed.append(self._settle(running, self.executor.attempt(running, self.handler)))
        return processed

    def recover(self) -> int:
        return recover(self.store, self.clock, events=self.events)

    def summary(self) -> dict:
        return metrics.summarize(self.store.records())

    def stats(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for record in self.store.records():
            counts[record.state] += 1
        return counts
