"""Public checks for the event pipeline contract."""

import pytest

from eventsys.audit import AuditLog
from eventsys.broker import Broker
from eventsys.consumer import Consumer
from eventsys.errors import JournalError, QueueFull, StoreError
from eventsys.journal import Journal
from eventsys.model import Event
from eventsys.pipeline import Pipeline
from eventsys.recovery import recover
from eventsys.retry import RetryPolicy, delay
from eventsys.store import EventStore


def event(event_id="e-1", kind="order", payload=None) -> Event:
    return Event(id=event_id, kind=kind, payload=payload or {"n": 1}, created_at=0.0)


def make_pipeline(tmp_path, handler, *, capacity=4, policy=None):
    return Pipeline(
        handler,
        capacity=capacity,
        store_path=tmp_path / "store.json",
        journal_path=tmp_path / "journal.jsonl",
        policy=policy,
    )


def test_a_full_broker_rejects_instead_of_dropping(tmp_path):
    broker = Broker(2)
    broker.publish(event("e-1"))
    broker.publish(event("e-2"))
    with pytest.raises(QueueFull):
        broker.publish(event("e-3"))
    assert broker.pending() == 2
    assert broker.acked_ids() == []


def test_a_taken_message_is_in_flight_until_it_is_acked():
    broker = Broker(2)
    broker.publish(event())
    envelope = broker.take()
    assert broker.in_flight_ids() == ["e-1"]
    assert broker.pending() == 0
    broker.ack(envelope)
    assert broker.in_flight_ids() == []
    assert broker.acked_ids() == ["e-1"]


def test_consumer_processes_and_persists_the_processed_set(tmp_path):
    handled = []
    pipeline = make_pipeline(tmp_path, lambda item: handled.append(item.id))
    pipeline.submit("order", {"n": 1})
    pipeline.submit("order", {"n": 2})
    counts = pipeline.run_once()
    assert counts == {"processed": 2, "duplicate": 0, "retried": 0, "dead": 0}
    assert handled == ["e-1", "e-2"]
    assert EventStore(tmp_path / "store.json").processed_ids() == ["e-1", "e-2"]


def test_a_transient_failure_is_retried_with_capped_backoff(tmp_path):
    calls = {"n": 0}

    def handler(item):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("downstream timeout")

    policy = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=2.0)
    pipeline = make_pipeline(tmp_path, handler, policy=policy)
    pipeline.submit("order", {"n": 1})
    counts = pipeline.run_once()
    assert counts == {"processed": 1, "duplicate": 0, "retried": 2, "dead": 0}
    assert pipeline.clock.now() == 3.0
    assert pipeline.broker.acked_ids() == ["e-1"]


def test_a_permanent_failure_ends_in_the_dead_letter_set(tmp_path):
    def handler(item):
        raise RuntimeError("always down")

    policy = RetryPolicy(max_attempts=2, base_delay=1.0, max_delay=4.0)
    pipeline = make_pipeline(tmp_path, handler, policy=policy)
    pipeline.submit("order", {"n": 1})
    counts = pipeline.run_once()
    assert counts["dead"] == 1
    assert counts["retried"] == 1
    assert pipeline.clock.now() == 1.0
    assert pipeline.broker.dead_letter_ids() == ["e-1"]
    assert EventStore(tmp_path / "store.json").is_dead("e-1")


def test_the_journal_reconstructs_a_taken_delivery(tmp_path):
    path = tmp_path / "journal.jsonl"
    broker = Broker(2, journal=Journal(path))
    broker.publish(event())
    envelope = broker.take()
    pending = Journal(path).pending_deliveries()
    assert [item.event.id for item in pending] == ["e-1"]
    broker.ack(envelope)
    assert Journal(path).pending_deliveries() == []


def test_recovery_requeues_a_delivery_that_was_never_closed(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = Broker(2, journal=Journal(path))
    first.publish(event())
    first.take()
    fresh = Broker(2, journal=Journal(path))
    store = EventStore(tmp_path / "store.json")
    assert recover(fresh, Journal(path), store) == 1
    assert fresh.pending() == 1
    assert fresh.take().event.id == "e-1"


def test_retry_policy_is_validated_and_capped():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=0)
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=8.0)
    assert delay(1, policy) == 1.0
    assert delay(5, policy) == 8.0
    with pytest.raises(ValueError):
        delay(0, policy)


def test_the_pipeline_is_deterministic_across_two_runs(tmp_path):
    def run(root):
        pipeline = make_pipeline(root, lambda item: None, capacity=3)
        for index in range(3):
            pipeline.submit("order", {"n": index})
        counts = pipeline.run_once()
        return counts, pipeline.broker.acked_ids(), pipeline.clock.now()

    assert run(tmp_path / "a") == run(tmp_path / "b")


def test_the_pipeline_audits_each_run_and_reports_metrics(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    pipeline = Pipeline(
        lambda item: None,
        capacity=3,
        store_path=tmp_path / "store.json",
        journal_path=tmp_path / "journal.jsonl",
        audit_path=audit_path,
    )
    pipeline.submit("order", {"n": 1})
    pipeline.submit("order", {"n": 2})
    counts = pipeline.run_once()
    entries = AuditLog(audit_path).entries()
    assert len(entries) == 1
    assert entries[0]["counts"] == counts
    assert entries[0]["broker"]["acked"] == 2
    snapshot = pipeline.metrics()
    assert snapshot["published"] == 2
    assert snapshot["acked"] == 2
    assert snapshot["processed"] == 2
    assert snapshot["delivery_ratio"] == 1.0
    pipeline.run_once()
    assert len(AuditLog(audit_path).entries()) == 2


def test_store_and_journal_reject_malformed_documents(tmp_path):
    store_path = tmp_path / "store.json"
    store_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        EventStore(store_path).processed_ids()
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(JournalError):
        Journal(journal_path).pending_deliveries()
