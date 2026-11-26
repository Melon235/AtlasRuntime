"""Stage F — family C: complex lifecycles.

C1 is a configuration stack whose single semantic table (the frozen spec) makes a sequential
decomposition the honest one. C2 is a scheduler whose state table, store, executor and recovery are
separate concerns but share one transition table and one injected clock.
"""

from __future__ import annotations

from experiments.stage_f.tasks.common import Task, shared_block
from experiments.stage_f.tasks.fixtures import load

_C1 = load("c1")
_C2 = load("c2")

# ====================================================================================== C1


C1 = Task(
    task_id="f-c1-config-lifecycle",
    family="C",
    title="Configuration lifecycle",
    prompt=(
        "The configuration stack still applies v1 rules. Implement the frozen v2 contract in "
        "`docs/INTERFACE.md`: a TOML document flattened to dotted paths, the `CONFIGKIT__` "
        "environment mapping, named profiles between the file and the environment, "
        "`explicit > env > profile > file > default` precedence with origins, a v1→v2 migration, "
        "strict typed validation with unknown-key rejection and cross-field rules, an immutable "
        "`Settings` view and an explanation/round-trip surface. `configkit/errors.py` and "
        "`configkit/spec.py` are frozen."
    ),
    seed=_C1["seed"],
    reference=_C1["reference"],
    public_tests=_C1["public"],
    oracle_tests=_C1["oracle"],
    workers=(
        {
            "task_id": "sources",
            "objective": "Parse the document, map the environment and migrate v1",
            "assignment": (
                "[YOUR ASSIGNMENT — worker sources]\n"
                "You own `configkit/document.py`, `configkit/loader.py` and `configkit/migrate.py`.\n\n"
                "OBJECTIVE: flatten TOML tables to arbitrary depth (rejecting arrays of tables and "
                "invalid documents), map `CONFIGKIT__A__B` to `a.b` with empty-path rejection, "
                "overlay the environment on the file, and migrate a v1 document to v2 (version "
                "metadata consumed, renames applied, `legacy_*` dropped, v2 collisions won, "
                "idempotent). The rules and error codes are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the three modules above. `configkit/spec.py` and "
                "`configkit/errors.py` are frozen; `merge.py`, `validate.py` and `runtime.py` belong "
                "to the resolved worker.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the parsing, "
                "environment and migration checks, and you report what you ran."
            ),
            "owned_paths": (
                "configkit/document.py",
                "configkit/loader.py",
                "configkit/migrate.py",
            ),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "resolved",
            "objective": "Merge with provenance, validate and expose Settings",
            "assignment": (
                "[YOUR ASSIGNMENT — worker resolved]\n"
                "You own `configkit/merge.py`, `configkit/validate.py` and `configkit/runtime.py`.\n\n"
                "OBJECTIVE: merge `default > file > profile > env > explicit` with per-key origins, "
                "coerce strictly (no bool-as-int, no float-as-int, finite floats, choices, ranges, "
                "lists), fill every default, reject unknown keys with their path, enforce the TLS, "
                "cache/feature and retry-budget cross-field rules, and expose an immutable "
                "`Settings` with `as_dict`/`explain`, the profile layer plus its explanation and "
                "round-trip helpers. The rules and error codes are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the five modules above. The frozen spec/errors and the source modules "
                "belong to the sources worker; read them, do not edit them.\n\n"
                "DEPENDENCIES: worker sources implements parsing, the environment mapping and the "
                "migration. Its actual result is provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report what you ran."
            ),
            "owned_paths": (
                "configkit/explain.py",
                "configkit/merge.py",
                "configkit/profile.py",
                "configkit/runtime.py",
                "configkit/validate.py",
            ),
            "depends_on": ("sources",),
            "max_turns": 18,
        },
    ),
    deliverables=(
        "configkit/document.py",
        "configkit/explain.py",
        "configkit/loader.py",
        "configkit/merge.py",
        "configkit/migrate.py",
        "configkit/profile.py",
        "configkit/runtime.py",
        "configkit/validate.py",
    ),
    allowed_paths=("configkit/",),
    forbidden_paths=(
        "configkit/errors.py",
        "configkit/spec.py",
        "docs/INTERFACE.md",
        "test_public.py",
        "README.md",
        ".git/",
    ),
    shared_context=shared_block(
        requirement=(
            "Implement the frozen configuration lifecycle: document → environment → migration → "
            "merge with provenance → strict validation → immutable runtime view."
        ),
        interface=(
            "- `configkit.document.parse(text)` / `flatten(data)`\n"
            "- `configkit.loader.read_env(env_values)` / `load(file_text, env_values)`\n"
            "- `configkit.migrate.upgrade(document)`\n"
            "- `configkit.profile.names/load`\n"
            "- `configkit.merge.resolve(defaults, file, env, explicit, profile=None) -> Resolved`\n"
            "- `configkit.validate.check(values)`\n"
            "- `configkit.runtime.Settings.from_sources(..., profile=None)`\n"
            "- `configkit.explain.render/as_env/diff`"
        ),
        constraints=(
            "- `configkit/spec.py` is the only field table and is frozen; every module must agree "
            "with it.\n"
            "- Hidden tests check precedence permutations, error codes and paths the public suite "
            "does not cover.\n"
            "- This task has one shared semantic table, so the decomposition is sequential."
        ),
        acceptance="`python -m pytest -q test_public.py` passes and the v2 contract holds.",
    ),
    frozen_contracts={
        "sources": ("docs/INTERFACE.md", "configkit/spec.py", "configkit/errors.py"),
        "resolved": ("docs/INTERFACE.md", "configkit/spec.py", "configkit/errors.py"),
    },
    notes=(
        "one shared schema/spec table with six modules around it; the sequential DAG is the honest "
        "one, not a forced parallel split"
    ),
    research_question=(
        "does a configuration lifecycle with one shared semantic table punish a parallel "
        "decomposition, as the coupled control predicts"
    ),
)


# ====================================================================================== C2


C2 = Task(
    task_id="f-c2-scheduler-retry",
    family="C",
    title="Scheduler lifecycle and retry",
    prompt=(
        "The scheduler still follows v1 rules. Implement the frozen v2 contract in "
        "`docs/INTERFACE.md`: the complete transition table with cancellation, exhaustion and "
        "recovery; a validated, capped retry policy; per-kind payload validation; an atomically "
        "persisted job store; an executor with replay, cancellation and deadline checks; a "
        "scheduler that runs due records one attempt per tick; a lifecycle event log and metrics; "
        "and restart recovery. `schedkit/errors.py`, `schedkit/clock.py` and `schedkit/model.py` "
        "are frozen. Nothing may wait on wall time."
    ),
    seed=_C2["seed"],
    reference=_C2["reference"],
    public_tests=_C2["public"],
    oracle_tests=_C2["oracle"],
    workers=(
        {
            "task_id": "queue",
            "objective": "Complete the transition table and the retry policy",
            "assignment": (
                "[YOUR ASSIGNMENT — worker queue]\n"
                "You own `schedkit/machine.py` and `schedkit/retry.py`.\n\n"
                "OBJECTIVE: encode every legal transition (start, finish, fail, retry, exhaust, "
                "cancel from every live state, recover) and reject every other pair with "
                "`InvalidTransition`; validate the policy and cap the delay at `max_delay` with the "
                "first attempt counted; and validate the per-kind payload spec. The table and rules "
                "are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the three modules above. The store, executor, recovery and scheduler "
                "belong to the other workers; read them, do not edit them. `schedkit/model.py` is "
                "frozen.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the machine and "
                "policy checks, and you report what you ran."
            ),
            "owned_paths": (
                "schedkit/jobspec.py",
                "schedkit/machine.py",
                "schedkit/retry.py",
            ),
            "depends_on": (),
            "max_turns": 14,
        },
        {
            "task_id": "storage",
            "objective": "Persist job records atomically",
            "assignment": (
                "[YOUR ASSIGNMENT — worker storage]\n"
                "You own `schedkit/store.py`.\n\n"
                "OBJECTIVE: one JSON document with version, sorted records and atomic replacement; "
                "dependency-free duplicate/unknown errors; corruption, wrong-version and malformed "
                "record rejection with `StoreError`; every mutation persisted. The format is in "
                "`docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `schedkit/store.py`. The other modules belong to the other workers; "
                "read them, do not edit them.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the persistence "
                "checks, and you report what you ran."
            ),
            "owned_paths": ("schedkit/store.py",),
            "depends_on": (),
            "max_turns": 14,
        },
        {
            "task_id": "executor",
            "objective": "One attempt with replay, cancellation and deadline",
            "assignment": (
                "[YOUR ASSIGNMENT — worker executor]\n"
                "You own `schedkit/executor.py`.\n\n"
                "OBJECTIVE: implement cache replay without calling the handler, the cancellation "
                "outcome, the deadline check before the handler, the success cache write and the "
                "typed failure with one more attempt. The rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `schedkit/executor.py`. The machine, retry policy and store belong to "
                "the other workers; read them, do not edit them. `schedkit/clock.py` and "
                "`schedkit/model.py` are frozen.\n\n"
                "DEPENDENCIES: workers queue and storage implement the table and the store you "
                "consume. Their actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the executor "
                "checks, and you report what you ran."
            ),
            "owned_paths": ("schedkit/executor.py",),
            "depends_on": ("queue", "storage"),
            "max_turns": 14,
        },
        {
            "task_id": "recovery",
            "objective": "Re-queue running records after a restart",
            "assignment": (
                "[YOUR ASSIGNMENT — worker recovery]\n"
                "You own `schedkit/recovery.py`.\n\n"
                "OBJECTIVE: move every `running` record back to `queued` through the table, preserve "
                "its attempt count, make it due immediately and record the reason; leave every other "
                "state untouched. Return the recovered count.\n\n"
                "OWNED FILES: `schedkit/recovery.py`. The store belongs to the storage worker; read "
                "it, do not edit it.\n\n"
                "DEPENDENCIES: worker storage implements the store you read. Its actual result is "
                "provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the recovery "
                "checks, and you report what you ran."
            ),
            "owned_paths": ("schedkit/recovery.py",),
            "depends_on": ("storage",),
            "max_turns": 12,
        },
        {
            "task_id": "scheduler",
            "objective": "Run due records, settle retries and expose stats",
            "assignment": (
                "[YOUR ASSIGNMENT — worker scheduler]\n"
                "You own `schedkit/scheduler.py`.\n\n"
                "OBJECTIVE: submit and cancel records, run only due `queued`/`failed` records in "
                "sorted id order, persist `running` before the handler, settle done/cancelled/dead/"
                "failed outcomes through the table, schedule the capped retry delay on the injected "
                "clock, apply the per-job cap under the policy cap, log the lifecycle, and expose "
                "per-state stats plus the metrics summary. The exact rules are in "
                "`docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `schedkit/scheduler.py`, `schedkit/events.py` and "
                "`schedkit/metrics.py`. The other modules belong to the other workers; read them, do "
                "not edit them.\n\n"
                "DEPENDENCIES: workers executor and recovery implement what this module calls. Their "
                "actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report what you ran."
            ),
            "owned_paths": (
                "schedkit/events.py",
                "schedkit/metrics.py",
                "schedkit/scheduler.py",
            ),
            "depends_on": ("executor", "recovery"),
            "max_turns": 18,
        },
    ),
    deliverables=(
        "schedkit/events.py",
        "schedkit/executor.py",
        "schedkit/jobspec.py",
        "schedkit/machine.py",
        "schedkit/metrics.py",
        "schedkit/recovery.py",
        "schedkit/retry.py",
        "schedkit/scheduler.py",
        "schedkit/store.py",
    ),
    allowed_paths=("schedkit/",),
    forbidden_paths=(
        "schedkit/clock.py",
        "schedkit/errors.py",
        "schedkit/model.py",
        "docs/INTERFACE.md",
        "test_public.py",
        "README.md",
        ".git/",
    ),
    shared_context=shared_block(
        requirement=(
            "Move the scheduler to the frozen v2 contract: complete transitions, capped retries, a "
            "persisted store, an executor with replay/cancel/deadline, a per-tick scheduler and "
            "restart recovery."
        ),
        interface=(
            "- `schedkit.machine.transition(state, event)` and `InvalidTransition`\n"
            "- `schedkit.retry.Policy/next_delay/should_retry`\n"
            "- `schedkit.jobspec.kinds/validate`\n"
            "- `schedkit.store.JobStore(path)`\n"
            "- `schedkit.executor.Executor.attempt(record, handler, *, cancelled=False)`\n"
            "- `schedkit.scheduler.Scheduler.submit/cancel/tick/recover/stats/summary`\n"
            "- `schedkit.events.EventLog` and `schedkit.metrics.summarize/attempt_histogram`\n"
            "- `schedkit.recovery.recover(store, clock, events=None)`\n"
            "- `schedkit.clock.Clock` is frozen"
        ),
        constraints=(
            "- The clock is injected; a task that sleeps on wall time cannot be scored.\n"
            "- Hidden tests advance the clock explicitly and check the exact delay and attempts.\n"
            "- The transition table is one shared semantic; the DAG is layered, not concurrent "
            "across the table."
        ),
        acceptance="`python -m pytest -q test_public.py` passes and the v2 contract holds.",
    ),
    frozen_contracts={
        "queue": ("docs/INTERFACE.md", "schedkit/model.py"),
        "storage": ("docs/INTERFACE.md", "schedkit/model.py"),
        "executor": ("docs/INTERFACE.md", "schedkit/model.py", "schedkit/clock.py"),
        "recovery": ("docs/INTERFACE.md", "schedkit/model.py"),
        "scheduler": ("docs/INTERFACE.md", "schedkit/model.py", "schedkit/clock.py"),
    },
    notes=(
        "one transition table with five layered owners; the executor and recovery are independent "
        "given the store, and the scheduler is the only node that settles every outcome"
    ),
    research_question=(
        "does a scheduler lifecycle with a shared transition table and a frozen clock reward a "
        "legal layered split plus one settling node"
    ),
)

TASKS: tuple[Task, ...] = (C1, C2)

__all__ = ["C1", "C2", "TASKS"]
