# Event pipeline — frozen contract

A deterministic in-process pipeline: producer → bounded broker → consumer, with a persisted
processed set, a persisted journal, retry and recovery. No network, no threads, no wall-clock waits.

`eventsys/errors.py`, `eventsys/model.py` and `eventsys/clock.py` are frozen and may not be edited.
`diagnostics/` is read-only evidence.

## Model

`Event(id, kind, payload, created_at)` is frozen; `id` and `kind` are non-empty strings, `payload`
is a dict and `created_at` is the injected clock value. `Envelope(event, attempts=0)` adds the
delivery attempt count.

## `eventsys/broker.py`

`Broker(capacity, journal=None)`:

- `capacity` must be positive (`ValueError`).
- `publish(event)` appends one envelope. A full queue raises `QueueFull` and **never drops a
  message**. An id already queued, in flight, acked or dead is a `DuplicateEvent`.
- `take() -> Envelope | None` moves the oldest envelope to the in-flight set and records the take
  in the journal when one is attached.
- `ack(envelope)` removes it from in-flight and records its id once; a repeated ack is harmless.
- `nack(envelope)` removes it from in-flight and puts it back at the **front** with `attempts + 1`.
- `dead_letter(envelope)` removes it from in-flight and records it once.
- `requeue(envelope)` puts it at the front without changing its attempts (used by recovery).
- `pending()` is the queued count; `in_flight_ids()`, `acked_ids()` and `dead_letter_ids()` are
  sorted lists of ids. `stats()` returns `{"pending", "in_flight", "acked", "dead"}` counts.

## `eventsys/retry.py`

`RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0)`; every value must be positive
(`ValueError`). `delay(attempt, policy)` is
`min(base_delay * 2 ** (attempt - 1), max_delay)` for `attempt >= 1` and a `ValueError` otherwise.
`should_retry(failures, policy)` is `failures < max_attempts`: the policy counts the first attempt,
so `max_attempts=3` means three handler calls in total.

## `eventsys/store.py`

`EventStore(path)` persists one JSON document:

```
{"version": 1, "processed": [ids...], "dead": [ids...]}
```

- A missing file is an empty store.
- `mark_processed(id)` returns `True` the first time and `False` afterwards; it is persisted
  immediately. `is_processed(id)`, `dead_letter(id)`, `is_dead(id)`, `processed_ids()` and
  `dead_ids()` behave on the persisted state.
- A malformed document or a wrong version is a `StoreError`.
- Writes are atomic (temporary file + `os.replace`).

## `eventsys/journal.py`

`Journal(path)` is an append-only JSONL file of three records:

```
{"op": "take", "event": {...}, "attempts": 0}
{"op": "ack", "id": "e-1"}
{"op": "dead", "id": "e-2"}
```

- `record_take(envelope)`, `record_ack(id)` and `record_dead(id)` append and flush.
- `pending_deliveries()` returns the envelopes whose last `take` has no later `ack` or `dead`, in
  first-take order, reconstructed with their attempt counts.
- A malformed line or an unknown operation is a `JournalError`.

## `eventsys/consumer.py`

`Consumer(handler, store, policy, clock, broker)`:

- `deliver(envelope)` returns one of `"processed"`, `"duplicate"`, `"retried"`, `"dead"`.
- An id the store has already processed is acked and returns `"duplicate"` without calling the
  handler.
- The handler runs **before** the message is marked processed. On success the id is marked processed,
  then acked.
- On a handler exception the failure count is `envelope.attempts + 1`. If
  `should_retry(failures, policy)` the clock advances by `delay(failures, policy)` and the envelope
  is nacked; otherwise the id is dead-lettered and the envelope is dead-lettered.
- `drain(limit=1000)` takes until the broker is empty or `limit` deliveries were made and returns
  `{"processed", "duplicate", "retried", "dead"}` counts.

## `eventsys/audit.py`

`AuditLog(path=None)` is an append-only JSONL run audit. `append(entry)` requires a dict and appends
one compact line; `entries()` returns the persisted entries (an in-memory list when `path` is
`None`); `last()` is the latest entry or `None`. A malformed line or a non-serialisable entry is an
`AuditError`.

## `eventsys/metrics.py`

`snapshot(broker, store)` derives `published` (acked + dead + pending + in-flight), `acked`, `dead`,
`pending`, `in_flight`, `processed`, `dead_letters` and `delivery_ratio`
(`acked / published`, or `0.0` when nothing was published).

## `eventsys/pipeline.py` — audit and metrics

`Pipeline(..., audit_path=None)` appends one audit record per `run_once`:
`{"counts": ..., "broker": ..., "at": clock.now()}`. `pipeline.metrics()` returns the metrics
snapshot. `stats()` keeps the broker counters plus `processed` / `dead_letters`.

## `eventsys/recovery.py`

`recover(broker, journal, store)` re-queues every journal delivery that is not processed and not
dead, at the front in journal order, and returns the number of envelopes re-queued.

## `eventsys/pipeline.py`

`Pipeline(handler, *, capacity, store_path, journal_path, clock=None, policy=None, audit_path=None)`
wires one producer, broker, store, journal, audit and consumer.

- `submit(kind, payload) -> Event` publishes one event; the id is `e-N` with `N` starting at 1.
- `run_once(limit=1000)` drains, appends one audit record and returns the consumer counts.
- `recover()` forwards to `eventsys.recovery.recover`.
- `metrics()` returns `eventsys.metrics.snapshot(broker, store)`.
- `stats()` returns the broker stats plus `processed` and `dead` counts from the store.

## Determinism

Every wait is an injected `Clock.advance`. A module that imports `time` or calls `sleep` cannot be
scored. Running the same scenario twice must produce the same ids, acked set, dead set and clock.
