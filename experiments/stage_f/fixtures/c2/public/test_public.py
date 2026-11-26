"""Public checks for the v2 scheduler contract."""

from dataclasses import replace

import pytest

from schedkit.clock import Clock
from schedkit.errors import (
    DuplicateJob,
    InvalidJobPayload,
    InvalidTransition,
    StoreError,
    UnknownJob,
    UnknownJobKind,
)
from schedkit.events import EventLog
from schedkit.executor import Executor
from schedkit.jobspec import kinds as job_kinds
from schedkit.machine import STATES, TERMINAL, transition
from schedkit.metrics import attempt_histogram
from schedkit.model import Job, JobRecord
from schedkit.recovery import recover
from schedkit.retry import Policy, next_delay
from schedkit.scheduler import Scheduler
from schedkit.store import JobStore


def make_job(
    job_id="j1",
    *,
    deadline=None,
    max_attempts=None,
    kind="task",
    payload=None,
) -> Job:
    return Job(
        id=job_id,
        kind=kind,
        payload={"n": 1} if payload is None else payload,
        created_at=0.0,
        deadline=deadline,
        max_attempts=max_attempts,
    )


def make_scheduler(tmp_path, handler, *, policy=None, start=0.0, cache=None, events=None):
    clock = Clock(start)
    store = JobStore(tmp_path / "jobs.json")
    return Scheduler(store, clock, handler, policy, cache, events)


def test_machine_covers_cancel_exhaust_recover_and_terminals():
    assert {"queued", "running", "failed", "done", "dead", "cancelled"} == set(STATES)
    assert TERMINAL == {"done", "dead", "cancelled"}
    assert transition("queued", "start") == "running"
    assert transition("running", "fail") == "failed"
    assert transition("failed", "retry") == "queued"
    assert transition("failed", "exhaust") == "dead"
    assert transition("running", "recover") == "queued"
    for state in ("queued", "running", "failed"):
        assert transition(state, "cancel") == "cancelled"
    for state in TERMINAL:
        with pytest.raises(InvalidTransition):
            transition(state, "cancel")
    with pytest.raises(InvalidTransition):
        transition("nowhere", "start")
    with pytest.raises(InvalidTransition):
        transition("queued", "finish")


def test_policy_is_validated_and_the_delay_is_capped():
    with pytest.raises(ValueError):
        Policy(max_attempts=0)
    with pytest.raises(ValueError):
        Policy(base_delay=0)
    policy = Policy(max_attempts=5, base_delay=2.0, max_delay=5.0, multiplier=3.0)
    assert [next_delay(attempt, policy) for attempt in (1, 2, 3)] == [2.0, 5.0, 5.0]
    with pytest.raises(ValueError):
        next_delay(0, policy)


def test_a_successful_job_finishes_once(tmp_path):
    calls = []
    scheduler = make_scheduler(tmp_path, lambda job: calls.append(job.id) or 7)
    scheduler.submit(make_job())
    processed = scheduler.tick()
    assert len(processed) == 1
    assert processed[0].state == "done"
    assert processed[0].attempts == 1
    assert processed[0].result == 7
    assert calls == ["j1"]
    assert scheduler.tick() == []
    assert calls == ["j1"]
    assert scheduler.stats()["done"] == 1


def test_a_transient_failure_is_retried_on_the_injected_clock(tmp_path):
    calls = {"n": 0}

    def handler(job):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("flaky")

    policy = Policy(max_attempts=3, base_delay=1.0, max_delay=8.0)
    scheduler = make_scheduler(tmp_path, handler, policy=policy)
    scheduler.submit(make_job())
    first = scheduler.tick()[0]
    assert (first.state, first.attempts) == ("failed", 1)
    assert first.next_run_at == 1.0
    assert scheduler.tick() == []
    scheduler.clock.advance(1.0)
    second = scheduler.tick()[0]
    assert (second.state, second.attempts) == ("failed", 2)
    assert second.next_run_at == 3.0
    scheduler.clock.advance(2.0)
    third = scheduler.tick()[0]
    assert (third.state, third.attempts) == ("done", 3)
    assert calls["n"] == 3


def test_exhaustion_and_a_per_job_cap_end_in_dead(tmp_path):
    def handler(job):
        raise RuntimeError("always")

    policy = Policy(max_attempts=2, base_delay=1.0, max_delay=4.0)
    scheduler = make_scheduler(tmp_path, handler, policy=policy)
    scheduler.submit(make_job())
    scheduler.tick()
    scheduler.clock.advance(1.0)
    final = scheduler.tick()[0]
    assert (final.state, final.attempts) == ("dead", 2)
    assert final.error


def test_the_deadline_is_checked_before_the_handler(tmp_path):
    calls = []
    scheduler = make_scheduler(tmp_path, lambda job: calls.append(job.id) or 1, start=10.0)
    scheduler.submit(make_job(deadline=5.0))
    final = scheduler.tick()[0]
    assert final.state == "dead"
    assert final.error == "timeout"
    assert calls == []


def test_cancellation_and_unknown_ids(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: 1)
    scheduler.submit(make_job("j1"))
    scheduler.submit(make_job("j2"))
    assert scheduler.cancel("j1") is True
    processed = scheduler.tick()
    assert [record.job.id for record in processed] == ["j2"]
    assert scheduler.stats()["cancelled"] == 1
    assert scheduler.cancel("j2") is False
    with pytest.raises(UnknownJob):
        scheduler.cancel("nope")
    with pytest.raises(DuplicateJob):
        scheduler.submit(make_job("j2"))


def test_the_store_is_persisted_and_validated(tmp_path):
    path = tmp_path / "jobs.json"
    clock = Clock()
    store = JobStore(path)
    scheduler = Scheduler(store, clock, lambda job: "ok")
    scheduler.submit(make_job())
    scheduler.tick()
    reloaded = JobStore(path)
    assert reloaded.get("j1").state == "done"
    assert reloaded.get("j1").result == "ok"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        JobStore(path)


def test_running_records_are_recovered_with_their_attempts(tmp_path):
    path = tmp_path / "jobs.json"
    clock = Clock()
    store = JobStore(path)
    scheduler = Scheduler(store, clock, lambda job: "ok")
    scheduler.submit(make_job())
    store.update(replace(store.get("j1"), state="running", attempts=1))
    restarted = Scheduler(JobStore(path), clock, lambda job: "ok")
    assert restarted.recover() == 1
    record = restarted.store.get("j1")
    assert (record.state, record.attempts, record.next_run_at) == ("queued", 1, 0.0)
    assert restarted.tick()[0].attempts == 2


def test_submit_validates_the_job_payload_spec(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: 1)
    assert job_kinds() == ["email", "maintenance", "report", "task"]
    with pytest.raises(InvalidJobPayload):
        scheduler.submit(make_job(payload={}))
    with pytest.raises(UnknownJobKind):
        scheduler.submit(make_job(kind="mystery"))
    accepted = scheduler.submit(
        make_job("j2", kind="email", payload={"to": "a@b", "subject": "hi", "extra": 1})
    )
    assert accepted.state == "queued"


def test_the_event_log_records_the_lifecycle(tmp_path):
    path = tmp_path / "events.jsonl"
    scheduler = make_scheduler(tmp_path, lambda job: 1, events=EventLog(path))
    scheduler.submit(make_job())
    scheduler.tick()
    entries = EventLog(path).entries()
    assert [entry["event"] for entry in entries] == ["submitted", "started", "done"]
    assert [entry["state"] for entry in entries] == ["queued", "running", "done"]
    assert entries[-1]["attempts"] == 1
    assert EventLog(path).for_job("j1") == entries


def test_the_summary_counts_states_and_attempts(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: 1)
    scheduler.submit(make_job())
    scheduler.tick()
    summary = scheduler.summary()
    assert summary["jobs"] == 1
    assert summary["done"] == 1
    assert summary["attempts"] == 1
    assert summary["max_attempts"] == 1
    assert summary["terminal"] == 1
    assert summary["success_rate"] == 1.0
    assert attempt_histogram(scheduler.store.records()) == {1: 1}


def test_the_executor_replays_from_the_cache(tmp_path):
    calls = []
    executor = Executor(Clock(), cache={"j1": 42})
    record = JobRecord(job=make_job(), state="queued", attempts=1)
    outcome = executor.attempt(record, lambda job: calls.append(job.id) or 0)
    assert (outcome.state, outcome.attempts, outcome.result, outcome.replayed) == (
        "done",
        1,
        42,
        True,
    )
    assert calls == []
