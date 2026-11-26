# Scheduler — frozen v2 contract

A deterministic job scheduler: state machine, injected clock, persistent job store, retry policy,
executor, recovery. Nothing waits on wall time.

`sqlite` is not used; the store is one JSON document. `schedkit/errors.py`, `schedkit/clock.py` and
`schedkit/model.py` are frozen and may not be edited.

## States and transitions — `schedkit/machine.py`

`STATES = {"queued", "running", "failed", "done", "dead", "cancelled"}`,
`TERMINAL = {"done", "dead", "cancelled"}`.

| from | event | to |
|---|---|---|
| `queued` | `start` | `running` |
| `queued` | `cancel` | `cancelled` |
| `running` | `finish` | `done` |
| `running` | `fail` | `failed` |
| `running` | `cancel` | `cancelled` |
| `running` | `recover` | `queued` |
| `failed` | `retry` | `queued` |
| `failed` | `exhaust` | `dead` |
| `failed` | `cancel` | `cancelled` |

Every other pair, and every unknown state, raises `InvalidTransition`. `done`, `dead` and
`cancelled` accept no event. `transition` is the only place that knows the table.

## Retry — `schedkit/retry.py`

`Policy(max_attempts=3, base_delay=1.0, max_delay=8.0, multiplier=2.0)`; `max_attempts` must be a
positive integer and the delays/multiplier positive numbers, otherwise `ValueError`.
`next_delay(attempt, policy=None)` is
`min(base_delay * multiplier ** (attempt - 1), max_delay)` for `attempt >= 1`, otherwise
`ValueError`. `should_retry(failures, policy)` is `failures < max_attempts` (the first attempt is
counted).

## Job payload specs — `schedkit/jobspec.py`

`JOB_SPECS` maps `task` → `("n",)`, `email` → `("to", "subject")`, `report` → `("report_id",)` and
`maintenance` → `()`. `kinds()` is sorted. `validate(job)` raises `UnknownJobKind` for a kind outside
the table and `InvalidJobPayload` when a required key is absent; extra payload keys are allowed.
`Scheduler.submit` validates before it stores anything.

## Lifecycle event log — `schedkit/events.py`

`EventLog(path=None)` appends one JSON line per transition:
`{"job_id", "event", "state", "attempts", "at"}`. Events are `submitted`, `started`, `retry`, `done`,
`dead`, `cancelled` and `recovered`. `record(record, event, at)` appends and flushes;
`entries()` returns the persisted entries (an in-memory list when `path` is `None`);
`for_job(id)` filters. A malformed line or a missing field is an `EventLogError`.

## Metrics — `schedkit/metrics.py`

`summarize(records)` returns `jobs`, one count per state, `attempts` (sum), `max_attempts`,
`retrying` (the `failed` count), `terminal` (done + dead + cancelled) and `success_rate`
(`done / jobs`, or `0.0` when empty). `attempt_histogram(records)` maps an attempt count to the
number of jobs with that count, sorted by count.

## Store — `schedkit/store.py`

`JobStore(path)` keeps `JobRecord` values in one JSON document:

```
{"version": 1, "records": [{"job": {...}, "state": "...", "attempts": 0,
                            "next_run_at": 0.0, "result": ..., "error": ...}]}
```

- `add(record)` rejects a duplicate id with `DuplicateJob`; `get(id)` raises `UnknownJob`;
  `update(record)` raises `UnknownJob` when the id is absent; `records()` is sorted by id.
- Every mutation is persisted atomically (temporary file + `os.replace`).
- A missing file is an empty store; a malformed document or a wrong version is a `StoreError`.
- A record must carry a known state and a non-negative attempt count.

## Executor — `schedkit/executor.py`

`Executor(clock, cache=None)`; `attempt(record, handler, *, cancelled=False) -> AttemptOutcome`.

- If the job id is in the cache, return `done` with `replayed=True` and the record's attempt count,
  without calling the handler.
- `cancelled=True` returns `cancelled` with the record's attempt count.
- A job whose `deadline` is not `None` and `clock.now() >= deadline` returns `dead` with reason
  `timeout`, without calling the handler.
- A handler exception returns `failed` with one more attempt and an error string. A success stores
  the result in the cache and returns `done` with one more attempt.

## Scheduler — `schedkit/scheduler.py`

`Scheduler(store, clock, handler, policy=None, cache=None, events=None)`.

- `submit(job)` validates the payload spec, then adds a `queued` record with
  `next_run_at = clock.now()` and logs `submitted`.
- When `events` is not `None`, every transition is logged: `submitted`, `started`, `retry`, `done`,
  `dead`, `cancelled`, and `recovered` through recovery.
- `cancel(job_id)` moves a live record (`queued`, `running`, `failed`) to `cancelled` and returns
  `True`; a terminal record returns `False`; an unknown id is an `UnknownJob`.
- `tick()` processes every record in `queued`/`failed` whose `next_run_at <= clock.now()`, in sorted
  id order, and returns the updated records:
  - `failed` first transitions `retry` to `queued`; the record is persisted as `running` before the
    handler runs (crash recovery depends on it).
  - `done` → `finish`; `cancelled` → `cancel`; `failed` → `fail`, then `exhaust` → `dead` when the
    attempt count reaches the effective maximum, otherwise `next_run_at = now + next_delay(attempts)`.
  - A `dead` outcome from the executor (deadline) transitions `running` → `failed` → `dead` and
    keeps the reason.
- The effective maximum is `min(job.max_attempts or policy.max_attempts, policy.max_attempts)`.
- `recover()` forwards to `schedkit.recovery.recover` (including the event log) and returns the
  recovered count.
- `stats()` counts records per state and includes every state with zero.
- `summary()` returns `schedkit.metrics.summarize(store.records())`.

## Recovery — `schedkit/recovery.py`

`recover(store, clock, events=None)` moves every `running` record back to `queued` through the table,
preserves its attempt count, sets `next_run_at = clock.now()`, records the reason and logs
`recovered` when an event log is supplied. It returns the number of records recovered.
