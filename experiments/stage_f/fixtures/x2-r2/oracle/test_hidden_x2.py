"""Hidden checks for the event pipeline repair (never staged into the agent workspace)."""

import ast
import inspect
import json

import pytest

from eventsys import broker as broker_module
from eventsys import consumer as consumer_module
from eventsys import journal as journal_module
from eventsys import metrics as metrics_module
from eventsys import pipeline as pipeline_module
from eventsys import producer as producer_module
from eventsys import recovery as recovery_module
from eventsys import retry as retry_module
from eventsys import store as store_module
from eventsys.audit import AuditLog
from eventsys.broker import Broker
from eventsys.clock import Clock
from eventsys.consumer import Consumer
from eventsys.errors import AuditError, DuplicateEvent, JournalError, QueueFull, StoreError
from eventsys.journal import Journal
from eventsys.model import Envelope, Event
from eventsys.pipeline import Pipeline
from eventsys.producer import Producer
from eventsys.recovery import recover
from eventsys.retry import RetryPolicy, delay, should_retry
from eventsys.store import EventStore


def event(event_id="e-1", kind="order", payload=None, created_at=0.0) -> Event:
    return Event(id=event_id, kind=kind, payload=payload or {"n": 1}, created_at=created_at)


def make_pipeline(tmp_path, handler, *, capacity=4, policy=None):
    return Pipeline(
        handler,
        capacity=capacity,
        store_path=tmp_path / "store.json",
        journal_path=tmp_path / "journal.jsonl",
        policy=policy,
    )


# -- broker ------------------------------------------------------------------------------------


def test_broker_rejects_overflow_without_reordering_the_queue():
    broker = Broker(3)
    for index in range(1, 4):
        broker.publish(event(f"e-{index}"))
    with pytest.raises(QueueFull):
        broker.publish(event("e-4"))
    assert [broker.take().event.id for _ in range(3)] == ["e-1", "e-2", "e-3"]
    assert broker.pending() == 0


def test_broker_rejects_duplicate_ids_in_every_state():
    broker = Broker(4)
    broker.publish(event("e-1"))
    with pytest.raises(DuplicateEvent):
        broker.publish(event("e-1"))
    envelope = broker.take()
    with pytest.raises(DuplicateEvent):
        broker.publish(event("e-1"))
    broker.ack(envelope)
    with pytest.raises(DuplicateEvent):
        broker.publish(event("e-1"))
    broker.publish(event("e-2"))
    dead = broker.take()
    broker.dead_letter(dead)
    with pytest.raises(DuplicateEvent):
        broker.publish(event("e-2"))


def test_broker_validates_capacity_and_publish_type(tmp_path):
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError):
            Broker(bad)
    for full in (False, True):
        path = tmp_path / f"journal-{full}.jsonl"
        broker = Broker(1, journal=Journal(path))
        if full:
            broker.publish(event("e-1"))
        before = broker.stats()
        before_journal = path.read_bytes() if path.exists() else b""
        with pytest.raises(ValueError):
            broker.publish({"id": "e-1"})
        assert broker.stats() == before
        assert (path.read_bytes() if path.exists() else b"") == before_journal
        if full:
            assert broker.take().event.id == "e-1"
        else:
            assert broker.take() is None


def test_nack_puts_the_message_at_the_front_with_one_more_attempt():
    broker = Broker(3)
    broker.publish(event("e-1"))
    broker.publish(event("e-2"))
    broker.publish(event("e-3"))
    first = broker.take()
    broker.nack(first)
    assert broker.pending() == 3
    retried = broker.take()
    assert retried.event.id == "e-1"
    assert retried.attempts == 1
    assert broker.take().event.id == "e-2"


def test_ack_is_idempotent_and_journaled_once(tmp_path):
    path = tmp_path / "journal.jsonl"
    broker = Broker(2, journal=Journal(path))
    broker.publish(event())
    envelope = broker.take()
    broker.ack(envelope)
    broker.ack(envelope)
    assert broker.acked_ids() == ["e-1"]
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [line["op"] for line in lines].count("ack") == 1


# -- retry and consumer ------------------------------------------------------------------------


def test_retry_policy_counts_the_first_attempt_and_caps_the_delay():
    policy = RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=5.0)
    assert [delay(attempt, policy) for attempt in (1, 2, 3, 4)] == [2.0, 4.0, 5.0, 5.0]
    assert should_retry(1, policy) is True
    assert should_retry(2, policy) is True
    assert should_retry(3, policy) is False
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=bad)
    with pytest.raises(ValueError):
        RetryPolicy(max_delay=0)
    with pytest.raises(ValueError):
        delay(0, policy)


def test_a_processed_event_is_acked_without_running_the_handler(tmp_path):
    store = EventStore(tmp_path / "store.json")
    store.mark_processed("e-1")
    journal = Journal(tmp_path / "journal.jsonl")
    broker = Broker(2, journal=journal)
    calls = []
    consumer = Consumer(lambda item: calls.append(item.id), store, RetryPolicy(), Clock(), broker)
    broker.publish(event())
    assert consumer.deliver(broker.take()) == "duplicate"
    assert calls == []
    assert broker.acked_ids() == ["e-1"]


def test_retry_schedule_uses_the_injected_clock(tmp_path):
    calls = {"n": 0}

    def handler(item):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("flaky")

    policy = RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=5.0)
    clock = Clock()
    store = EventStore(tmp_path / "store.json")
    broker = Broker(2)
    consumer = Consumer(handler, store, policy, clock, broker)
    broker.publish(event())
    counts = consumer.drain()
    assert counts == {"processed": 1, "duplicate": 0, "retried": 3, "dead": 0}
    assert clock.now() == 11.0
    assert store.processed_ids() == ["e-1"]


def test_a_single_attempt_policy_dead_letters_immediately(tmp_path):
    policy = RetryPolicy(max_attempts=1)
    clock = Clock()
    store = EventStore(tmp_path / "store.json")
    broker = Broker(2)
    consumer = Consumer(lambda item: (_ for _ in ()).throw(RuntimeError("no")), store, policy, clock, broker)
    broker.publish(event())
    counts = consumer.drain()
    assert counts == {"processed": 0, "duplicate": 0, "retried": 0, "dead": 1}
    assert clock.now() == 0.0
    assert store.dead_ids() == ["e-1"]


# -- persistence and recovery ------------------------------------------------------------------


def test_store_is_monotonic_and_persisted(tmp_path):
    path = tmp_path / "nested" / "store.json"
    store = EventStore(path)
    assert store.processed_ids() == []
    assert store.mark_processed("e-1") is True
    assert store.mark_processed("e-1") is False
    store.dead_letter("e-2")
    reopened = EventStore(path)
    assert reopened.processed_ids() == ["e-1"]
    assert reopened.dead_ids() == ["e-2"]
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {"version": 1, "processed": ["e-1"], "dead": ["e-2"]}


def test_store_rejects_corruption_and_wrong_versions(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        EventStore(path).processed_ids()
    path.write_text('{"version": 2, "processed": [], "dead": []}', encoding="utf-8")
    with pytest.raises(StoreError):
        EventStore(path).processed_ids()
    path.write_text('{"version": 1, "processed": [1], "dead": []}', encoding="utf-8")
    with pytest.raises(StoreError):
        EventStore(path).processed_ids()


def test_journal_reconstructs_order_and_attempts(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)
    broker = Broker(3, journal=journal)
    broker.publish(event("e-1"))
    broker.publish(event("e-2"))
    first = broker.take()
    second = broker.take()
    broker.nack(first)
    pending = Journal(path).pending_deliveries()
    expected = [("e-1", 1), ("e-2", 0)]
    assert [(item.event.id, item.attempts) for item in pending] == expected
    fresh = Broker(3)
    assert recover(fresh, Journal(path), EventStore(tmp_path / "store.json")) == 2
    recovered = [fresh.take(), fresh.take()]
    assert [(item.event.id, item.attempts) for item in recovered] == expected
    broker.ack(broker.take())
    broker.ack(second)
    broker.dead_letter(Envelope(event("e-9"), 0))
    assert Journal(path).pending_deliveries() == []


def test_journal_rejects_malformed_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"op": "take"}\n', encoding="utf-8")
    with pytest.raises(JournalError):
        Journal(path).pending_deliveries()
    path.write_text('{"op": "nope"}\n', encoding="utf-8")
    with pytest.raises(JournalError):
        Journal(path).pending_deliveries()
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(JournalError):
        Journal(path).pending_deliveries()
    assert Journal(tmp_path / "missing.jsonl").pending_deliveries() == []


def test_recovery_skips_processed_dead_and_acked_ids(tmp_path):
    path = tmp_path / "journal.jsonl"
    store = EventStore(tmp_path / "store.json")
    journal = Journal(path)
    broker = Broker(4, journal=journal)
    for index in range(1, 5):
        broker.publish(event(f"e-{index}"))
    broker.take()  # e-1: pending
    second = broker.take()
    broker.ack(second)  # e-2: acked
    broker.take()  # e-3: pending
    fourth = broker.take()
    broker.dead_letter(fourth)  # e-4: dead
    store.mark_processed("e-3")

    fresh = Broker(4, journal=Journal(path))
    assert recover(fresh, Journal(path), store) == 1
    assert fresh.pending() == 1
    assert fresh.take().event.id == "e-1"


def test_recovery_requeues_multiple_deliveries_in_journal_order(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)
    broker = Broker(4, journal=journal)
    broker.publish(event("e-1"))
    broker.publish(event("e-2"))
    broker.take()
    broker.take()
    fresh = Broker(4, journal=Journal(path))
    assert recover(fresh, Journal(path), EventStore(tmp_path / "store.json")) == 2
    assert [fresh.take().event.id for _ in range(2)] == ["e-1", "e-2"]


# -- producer, pipeline and determinism --------------------------------------------------------


def test_producer_assigns_ids_and_copies_the_payload():
    clock = Clock(4.0)
    broker = Broker(4)
    producer = Producer(broker, clock)
    payload = {"n": 1}
    first = producer.emit("order", payload)
    payload["n"] = 2
    second = producer.emit("order", {"n": 3})
    assert (first.id, second.id) == ("e-1", "e-2")
    assert first.payload == {"n": 1}
    assert first.created_at == 4.0
    assert broker.pending() == 2
    with pytest.raises(ValueError):
        producer.emit("", {})


def test_pipeline_stats_and_determinism(tmp_path):
    def run(root):
        pipeline = make_pipeline(root, lambda item: None, capacity=3)
        for index in range(3):
            pipeline.submit("order", {"n": index})
        counts = pipeline.run_once()
        return counts, pipeline.broker.acked_ids(), pipeline.clock.now(), pipeline.stats()

    first = run(tmp_path / "a")
    second = run(tmp_path / "b")
    assert first == second
    counts, acked, now, stats = first
    assert counts == {"processed": 3, "duplicate": 0, "retried": 0, "dead": 0}
    assert acked == ["e-1", "e-2", "e-3"]
    assert now == 0.0
    assert stats == {
        "pending": 0,
        "in_flight": 0,
        "acked": 3,
        "dead": 0,
        "processed": 3,
        "dead_letters": 0,
    }


def test_the_audit_log_is_persisted_and_validated(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    log = AuditLog(path)
    assert log.entries() == []
    assert log.last() is None
    log.append({"counts": {"processed": 1}, "at": 0.0})
    log.append({"counts": {"processed": 0}, "at": 1.0})
    assert len(AuditLog(path).entries()) == 2
    assert AuditLog(path).last() == {"counts": {"processed": 0}, "at": 1.0}
    with pytest.raises(AuditError):
        log.append({"bad": object()})
    with pytest.raises(AuditError):
        log.append(["not", "a", "dict"])
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(AuditError):
        AuditLog(path).entries()
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(AuditError):
        AuditLog(path).entries()


def test_metrics_cover_empty_success_and_dead_letters(tmp_path):
    broker = Broker(4)
    store = EventStore(tmp_path / "store.json")
    empty = metrics_module.snapshot(broker, store)
    assert empty == {
        "published": 0,
        "acked": 0,
        "dead": 0,
        "pending": 0,
        "in_flight": 0,
        "processed": 0,
        "dead_letters": 0,
        "delivery_ratio": 0.0,
    }

    policy = RetryPolicy(max_attempts=1)
    pipeline = Pipeline(
        lambda item: (_ for _ in ()).throw(RuntimeError("down")),
        capacity=4,
        store_path=tmp_path / "store.json",
        journal_path=tmp_path / "journal.jsonl",
        policy=policy,
    )
    pipeline.submit("order", {"n": 1})
    pipeline.submit("order", {"n": 2})
    counts = pipeline.run_once()
    assert counts["dead"] == 2
    snapshot = pipeline.metrics()
    assert snapshot["published"] == 2
    assert snapshot["acked"] == 0
    assert snapshot["dead"] == 2
    assert snapshot["processed"] == 0
    assert snapshot["dead_letters"] == 2
    assert snapshot["delivery_ratio"] == 0.0


def test_no_module_waits_on_wall_time():
    modules = (
        broker_module,
        consumer_module,
        journal_module,
        pipeline_module,
        producer_module,
        recovery_module,
        retry_module,
        store_module,
    )
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "time" for alias in node.names), module.__name__
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "sleep", module.__name__
