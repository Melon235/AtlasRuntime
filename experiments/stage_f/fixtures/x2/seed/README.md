# Event pipeline

A deterministic in-process pipeline: producer → bounded broker → consumer with a retry policy, a
persisted processed set, an append-only delivery journal and recovery.

Messages are being lost under a burst and after a handler failure. `diagnostics/` holds the incident
report, the captured trace and the deployment history. `docs/INTERFACE.md` is the frozen contract.

Run the public checks with `python -m pytest -q test_public.py`.

## Layout

- `eventsys/broker.py` — bounded queue and in-flight tracking.
- `eventsys/retry.py` — capped exponential backoff.
- `eventsys/store.py` — processed and dead set.
- `eventsys/journal.py` — append-only take/ack/dead log.
- `eventsys/consumer.py` — the delivery loop.
- `eventsys/recovery.py` — restart recovery.
- `eventsys/pipeline.py` — wiring and counters.
- `diagnostics/` — read-only evidence and a reproducer.
