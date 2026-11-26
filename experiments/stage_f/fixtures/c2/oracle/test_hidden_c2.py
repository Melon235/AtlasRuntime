"""Hidden checks for the scheduler lifecycle (never staged into the agent workspace)."""

import ast
import inspect
import json
from dataclasses import replace

import pytest

from schedkit import executor as executor_module
from schedkit import machine as machine_module
from schedkit import recovery as recovery_module
from schedkit import retry as retry_module
from schedkit import scheduler as scheduler_module
from schedkit import store as store_module
from schedkit.clock import Clock
from schedkit.errors import (
    DuplicateJob,
    EventLogError,
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
from schedkit.metrics import attempt_histogram, summarize
from schedkit.model import Job, JobRecord
from schedkit.recovery import recover
from schedkit.retry import Policy, next_delay, should_retry
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


def make_scheduler(tmp_path, handler, *, policy=None, start=0.0, cache=None, path=None, events=None):
    clock = Clock(start)
    store = JobStore(path or tmp_path / "jobs.json")
    return Scheduler(store, clock, handler, policy, cache, events)


def test_the_transition_table_is_complete_and_terminal_states_are_closed():
    legal = {
        ("queued", "start"): "running",
        ("queued", "cancel"): "cancelled",
        ("running", "finish"): "done",
        ("running", "fail"): "failed",
        ("running", "cancel"): "cancelled",
        ("running", "recover"): "queued",
        ("failed", "retry"): "queued",
        ("failed", "exhaust"): "dead",
        ("failed", "cancel"): "cancelled",
    }
    for (state, event), target in legal.items():
        assert transition(state, event) == target
    for state in TERMINAL:
        for event in ("start", "finish", "fail", "retry", "exhaust", "recover", "cancel"):
            with pytest.raises(InvalidTransition):
                transition(state, event)
    with pytest.raises(InvalidTransition):
        transition("unknown", "start")
    with pytest.raises(InvalidTransition):
        transition("queued", "exhaust")


def test_policy_and_should_retry_edges():
    policy = Policy(max_attempts=3, base_delay=1.0, max_delay=8.0, multiplier=2.0)
    assert should_retry(1, policy) is True
    assert should_retry(2, policy) is True
    assert should_retry(3, policy) is False
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError):
            Policy(max_attempts=bad)
    with pytest.raises(ValueError):
        Policy(multiplier=0)
    assert next_delay(10, Policy(base_delay=1.0, max_delay=8.0)) == 8.0
    assert next_delay(1, Policy(base_delay=0.5, max_delay=8.0, multiplier=2.0)) == 0.5


def test_job_and_record_validation():
    with pytest.raises(ValueError):
        Job(id="", kind="k", payload={}, created_at=0.0)
    with pytest.raises(ValueError):
        Job(id="j", kind="", payload={}, created_at=0.0)
    with pytest.raises(ValueError):
        Job(id="j", kind="k", payload=[], created_at=0.0)
    with pytest.raises(ValueError):
        Job(id="j", kind="k", payload={}, created_at=True)
    with pytest.raises(ValueError):
        Job(id="j", kind="k", payload={}, created_at=0.0, max_attempts=0)
    with pytest.raises(ValueError):
        JobRecord(job=make_job(), state="nowhere")
    with pytest.raises(ValueError):
        JobRecord(job=make_job(), attempts=-1)


def test_store_persists_and_rejects_bad_documents(tmp_path):
    path = tmp_path / "nested" / "jobs.json"
    store = JobStore(path)
    store.add(JobRecord(job=make_job()))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    with pytest.raises(DuplicateJob):
        store.add(JobRecord(job=make_job()))
    with pytest.raises(UnknownJob):
        store.get("nope")
    with pytest.raises(UnknownJob):
        store.update(JobRecord(job=make_job("nope")))
    assert JobStore(path).get("j1").state == "queued"

    path.write_text('{"version": 9, "records": []}', encoding="utf-8")
    with pytest.raises(StoreError):
        JobStore(path)
    path.write_text('{"version": 1, "records": {}}', encoding="utf-8")
    with pytest.raises(StoreError):
        JobStore(path)
    record = JobRecord(job=make_job()).as_dict()
    path.write_text(
        json.dumps({"version": 1, "records": [record, record]}), encoding="utf-8"
    )
    with pytest.raises(StoreError):
        JobStore(path)
    broken = dict(record)
    broken["state"] = "nowhere"
    path.write_text(json.dumps({"version": 1, "records": [broken]}), encoding="utf-8")
    with pytest.raises(StoreError):
        JobStore(path)


def test_due_records_only_and_retry_state_is_persisted(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: (_ for _ in ()).throw(RuntimeError("x")))
    scheduler.submit(make_job())
    record = scheduler.store.get("j1")
    scheduler.store.update(replace(record, next_run_at=5.0))
    assert scheduler.tick() == []
    scheduler.clock.advance(5.0)
    failed = scheduler.tick()[0]
    assert (failed.state, failed.attempts, failed.next_run_at) == ("failed", 1, 6.0)
    reloaded = JobStore(tmp_path / "jobs.json").get("j1")
    assert (reloaded.state, reloaded.attempts, reloaded.next_run_at) == ("failed", 1, 6.0)


def test_cancel_works_from_failed_and_running(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: (_ for _ in ()).throw(RuntimeError("x")))
    scheduler.submit(make_job())
    scheduler.tick()
    assert scheduler.store.get("j1").state == "failed"
    assert scheduler.cancel("j1") is True
    assert scheduler.store.get("j1").state == "cancelled"
    assert scheduler.tick() == []

    scheduler.submit(make_job("j2"))
    scheduler.store.update(replace(scheduler.store.get("j2"), state="running"))
    assert scheduler.cancel("j2") is True
    assert scheduler.store.get("j2").state == "cancelled"


def test_a_deadline_can_end_a_later_attempt(tmp_path):
    calls = {"n": 0}

    def handler(job):
        calls["n"] += 1
        raise RuntimeError("down")

    policy = Policy(max_attempts=5, base_delay=1.0, max_delay=8.0)
    scheduler = make_scheduler(tmp_path, handler, policy=policy)
    scheduler.submit(make_job(deadline=2.0))
    scheduler.tick()
    scheduler.clock.advance(1.0)
    scheduler.tick()
    assert calls["n"] == 2
    scheduler.clock.advance(2.0)
    final = scheduler.tick()[0]
    assert (final.state, final.error, final.attempts) == ("dead", "timeout", 2)
    assert calls["n"] == 2


def test_a_per_job_cap_cannot_exceed_the_policy(tmp_path):
    def handler(job):
        raise RuntimeError("down")

    policy = Policy(max_attempts=5, base_delay=1.0, max_delay=8.0)
    scheduler = make_scheduler(tmp_path, handler, policy=policy)
    scheduler.submit(make_job(max_attempts=1))
    final = scheduler.tick()[0]
    assert final.state == "dead"
    assert final.attempts == 1


def test_every_job_kind_has_a_payload_spec(tmp_path):
    assert job_kinds() == ["email", "maintenance", "report", "task"]
    scheduler = make_scheduler(tmp_path, lambda job: 1)
    scheduler.submit(make_job("j1", kind="maintenance", payload={}))
    scheduler.submit(
        make_job("j2", kind="report", payload={"report_id": "r-1", "extra": True})
    )
    with pytest.raises(InvalidJobPayload):
        scheduler.submit(make_job("j3", kind="email", payload={"to": "a@b"}))
    with pytest.raises(UnknownJobKind):
        scheduler.submit(make_job("j4", kind="mystery"))


def test_the_event_log_records_retries_in_order(tmp_path):
    path = tmp_path / "events.jsonl"
    calls = {"n": 0}

    def handler(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("flaky")
        return "ok"

    policy = Policy(max_attempts=3, base_delay=1.0, max_delay=8.0)
    scheduler = make_scheduler(tmp_path, handler, policy=policy, events=EventLog(path))
    scheduler.submit(make_job())
    scheduler.tick()
    scheduler.clock.advance(1.0)
    scheduler.tick()
    entries = EventLog(path).entries()
    assert [entry["event"] for entry in entries] == [
        "submitted",
        "started",
        "retry",
        "started",
        "done",
    ]
    assert [entry["attempts"] for entry in entries] == [0, 0, 1, 1, 2]
    assert entries[-1]["state"] == "done"


def test_the_event_log_is_persisted_and_validated(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    assert EventLog(path).entries() == []
    scheduler = make_scheduler(tmp_path, lambda job: 1, events=EventLog(path))
    scheduler.submit(make_job())
    scheduler.tick()
    assert len(EventLog(path).entries()) == 3
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(EventLogError):
        EventLog(path).entries()
    path.write_text('{"job_id": "j1", "event": "started"}\n', encoding="utf-8")
    with pytest.raises(EventLogError):
        EventLog(path).entries()


def test_recovery_writes_a_recovered_event(tmp_path):
    path = tmp_path / "events.jsonl"
    store = JobStore(tmp_path / "jobs.json")
    store.add(JobRecord(job=make_job(), state="running", attempts=1))
    scheduler = Scheduler(store, Clock(3.0), lambda job: 1, events=EventLog(path))
    assert scheduler.recover() == 1
    entries = EventLog(path).entries()
    assert [entry["event"] for entry in entries] == ["recovered"]
    assert entries[0]["state"] == "queued"
    assert entries[0]["attempts"] == 1


def test_metrics_summarize_states_attempts_and_success_rate():
    empty = summarize([])
    assert empty["jobs"] == 0
    assert empty["success_rate"] == 0.0
    assert empty["max_attempts"] == 0
    records = [
        JobRecord(job=make_job("j1"), state="done", attempts=1),
        JobRecord(job=make_job("j2"), state="failed", attempts=2),
        JobRecord(job=make_job("j3"), state="dead", attempts=3),
        JobRecord(job=make_job("j4"), state="queued", attempts=0),
    ]
    summary = summarize(records)
    assert summary["jobs"] == 4
    assert summary["attempts"] == 6
    assert summary["max_attempts"] == 3
    assert summary["retrying"] == 1
    assert summary["terminal"] == 2
    assert summary["success_rate"] == 0.25
    assert attempt_histogram(records) == {0: 1, 1: 1, 2: 1, 3: 1}


def test_the_record_is_persisted_as_running_before_the_handler(tmp_path):
    holder: dict = {}

    def handler(job):
        holder["state"] = holder["scheduler"].store.get(job.id).state
        return 1

    scheduler = make_scheduler(tmp_path, handler)
    holder["scheduler"] = scheduler
    scheduler.submit(make_job())
    scheduler.tick()
    assert holder["state"] == "running"


def test_cache_replay_through_the_scheduler(tmp_path):
    calls = []
    cache = {"j1": 99}
    scheduler = make_scheduler(tmp_path, lambda job: calls.append(job.id) or 0, cache=cache)
    scheduler.submit(make_job())
    final = scheduler.tick()[0]
    assert (final.state, final.attempts, final.result) == ("done", 0, 99)
    assert calls == []


def test_recovery_handles_multiple_records_and_preserves_attempts(tmp_path):
    path = tmp_path / "jobs.json"
    clock = Clock(7.0)
    store = JobStore(path)
    store.add(JobRecord(job=make_job("j1"), state="running", attempts=1))
    store.add(JobRecord(job=make_job("j2"), state="running", attempts=2))
    store.add(JobRecord(job=make_job("j3"), state="done", attempts=1))
    assert recover(JobStore(path), clock) == 2
    reloaded = JobStore(path)
    assert reloaded.get("j1").state == "queued"
    assert reloaded.get("j1").attempts == 1
    assert reloaded.get("j2").attempts == 2
    assert reloaded.get("j1").next_run_at == 7.0
    assert reloaded.get("j3").state == "done"
    assert reloaded.get("j3").next_run_at == 0.0


def test_stats_include_every_state(tmp_path):
    scheduler = make_scheduler(tmp_path, lambda job: 1)
    stats = scheduler.stats()
    assert set(stats) == set(STATES)
    assert all(value == 0 for value in stats.values())


def test_two_identical_runs_are_equal(tmp_path):
    def scenario(root):
        scheduler = make_scheduler(root, lambda job: job.payload["n"])
        scheduler.submit(make_job("j1"))
        scheduler.submit(make_job("j2"))
        scheduler.tick()
        records = [
            (record.job.id, record.state, record.attempts)
            for record in scheduler.store.records()
        ]
        return records, scheduler.stats()

    assert scenario(tmp_path / "a") == scenario(tmp_path / "b")


def test_no_module_waits_on_wall_time():
    for module in (
        executor_module,
        machine_module,
        recovery_module,
        retry_module,
        scheduler_module,
        store_module,
    ):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "time" for alias in node.names), module.__name__
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "sleep", module.__name__
