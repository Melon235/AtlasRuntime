"""Stage F — family X: audit/repair and diagnosis/repair.

X1 spreads the same frozen API change across direct calls, a wrapper chain, an event callback, an
alias/partial and an injected dependency. X2 spreads one incident across a broker, a persisted set,
a journal, a consumer and recovery. In both, most of the cost is reading the system, not typing the
patch.
"""

from __future__ import annotations

from experiments.stage_f.tasks.common import Task, shared_block
from experiments.stage_f.tasks.fixtures import load

_X1 = load("x1")
_X2 = load("x2")

# ====================================================================================== X1


X1 = Task(
    task_id="f-x1-api-compat-audit",
    family="X",
    title="Cross-subsystem compatibility audit and repair",
    prompt=(
        "`foundation/audit.py` was migrated to the keyword-only v2 audit API returning a `Receipt`, "
        "and only part of the platform was updated. Find every live call path — direct calls, the "
        "ledger wrapper and its consumers, the event subscriber, the alias/partial export path and "
        "the notifier's injected sink — repair the broken ones, fix the business assumptions the new "
        "return object exposes, and leave the already-migrated search site alone. "
        "`docs/INTERFACE.md` is the frozen contract."
    ),
    seed=_X1["seed"],
    reference=_X1["reference"],
    public_tests=_X1["public"],
    oracle_tests=_X1["oracle"],
    workers=(
        {
            "task_id": "billing",
            "objective": "Repair the billing call chain",
            "assignment": (
                "[YOUR ASSIGNMENT — worker billing]\n"
                "You own the billing chain: `billing/charge.py`, `billing/refund.py`, "
                "`billing/ledger.py`, `billing/settlement.py` and `billing/reconciliation.py`.\n\n"
                "OBJECTIVE: audit every audit-API call reachable from these modules (direct calls, "
                "the ledger wrapper and both of its consumers) and repair them to the frozen v2 "
                "contract: keyword-only calls, `Receipt` consumers, minor-unit integers, the charge "
                "and refund tax rules, the zero-amount ledger rule and the reconciliation totals. "
                "Do not change the frozen support modules or the platform contract.\n\n"
                "OWNED FILES: the five modules above. `billing/statements.py` belongs to the "
                "surfaces worker; `reporting/` belongs to the reports worker; read them, do not edit "
                "them.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the billing "
                "behaviour, and you report the call sites you found and what you ran."
            ),
            "owned_paths": (
                "billing/charge.py",
                "billing/ledger.py",
                "billing/reconciliation.py",
                "billing/refund.py",
                "billing/settlement.py",
            ),
            "depends_on": (),
            "max_turns": 18,
        },
        {
            "task_id": "reports",
            "objective": "Repair the reporting call sites",
            "assignment": (
                "[YOUR ASSIGNMENT — worker reports]\n"
                "You own the reporting call sites: `reporting/daily.py`, `reporting/monthly.py` and "
                "`reporting/export.py`.\n\n"
                "OBJECTIVE: repair them to the frozen v2 contract, including the business data the "
                "contract now requires (integer minor totals, order counts, the month prefix, the "
                "sorted currency list) and the export path that reaches `record` only through an "
                "alias and a `functools.partial`. `reporting/export.py` must keep its alias/partial "
                "shape; only the call must be keyword-based.\n\n"
                "OWNED FILES: the three modules above. `billing/` and `events/`/`notifications/` "
                "belong to the other workers; read them, do not edit them.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the reporting "
                "behaviour, and you report the call sites you found and what you ran."
            ),
            "owned_paths": (
                "reporting/daily.py",
                "reporting/export.py",
                "reporting/monthly.py",
            ),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "surfaces",
            "objective": "Repair the callback, injected sink and remaining surfaces",
            "assignment": (
                "[YOUR ASSIGNMENT — worker surfaces]\n"
                "You own the remaining surfaces: `events/subscribers.py`, "
                "`notifications/service.py`, `returns/service.py`, `returns/workflow.py`, "
                "`inventory/reservations.py`, `inventory/transfers.py` and "
                "`billing/statements.py`.\n\n"
                "OBJECTIVE: repair the event callback (count and actor from the event), the "
                "notifier's injected sink (keyword call; the API is not imported here), the return "
                "request (policy fee and refundable in minor units), the all-or-nothing reservation "
                "(units summed per SKU) and the statement (gross, WELCOME10 credits, net). All of "
                "them must end in the frozen v2 API. `app/` already injects the correct callable and "
                "needs no change.\n\n"
                "OWNED FILES: the five modules above. `returns/policy.py`, `inventory/stock.py`, "
                "`foundation/`, `catalog/` and `app/` are read-only for this task.\n\n"
                "DEPENDENCIES: the billing and reports workers repair their own call sites. Their "
                "actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report the call paths you found and what you ran."
            ),
            "owned_paths": (
                "billing/statements.py",
                "events/subscribers.py",
                "inventory/reservations.py",
                "inventory/transfers.py",
                "notifications/service.py",
                "returns/service.py",
                "returns/workflow.py",
            ),
            "depends_on": ("billing", "reports"),
            "max_turns": 20,
        },
    ),
    deliverables=(
        "billing/charge.py",
        "billing/ledger.py",
        "billing/reconciliation.py",
        "billing/refund.py",
        "billing/settlement.py",
        "billing/statements.py",
        "events/subscribers.py",
        "inventory/reservations.py",
        "inventory/transfers.py",
        "notifications/service.py",
        "reporting/daily.py",
        "reporting/export.py",
        "reporting/monthly.py",
        "returns/service.py",
        "returns/workflow.py",
    ),
    allowed_paths=(
        "app/",
        "billing/",
        "events/",
        "inventory/",
        "notifications/",
        "reporting/",
        "returns/",
    ),
    forbidden_paths=(
        "catalog/",
        "docs/INTERFACE.md",
        "foundation/",
        "inventory/stock.py",
        "returns/policy.py",
        "search/",
        "test_public.py",
        "README.md",
        ".git/",
    ),
    shared_context=shared_block(
        requirement=(
            "A frozen shared API moved to a keyword-only v2 signature returning a `Receipt`. Audit "
            "every live call path across the platform, repair the broken ones, fix the business "
            "assumptions the new return object exposes, and leave already-correct sites alone."
        ),
        interface=(
            "- `foundation.audit.record(*, kind, data, actor=None) -> Receipt` (frozen)\n"
            "- `foundation.audit.Receipt(kind, data, actor)` (frozen)\n"
            "- billing emits `charge` / `refund` / `settlement` / `reconciliation` / `statement`\n"
            "- reporting emits `daily` / `monthly` / `export`\n"
            "- events emit `bus`; notifications emit `notification`\n"
            "- returns emit `return`; inventory emits `reservation`\n"
            "- `search/index.py` already emits `search` and must keep working"
        ),
        constraints=(
            "- `foundation/`, `catalog/`, `returns/policy.py`, `inventory/stock.py` and `app/` are "
            "read-only.\n"
            "- Hidden tests parse the call sites, so a positional call is a failure even if it "
            "happens to run.\n"
            "- The contract table in `docs/INTERFACE.md` names the exact `data` of every event."
        ),
        acceptance=("`python -m pytest -q test_public.py` passes and no legacy call path remains."),
    ),
    frozen_contracts={
        "billing": ("docs/INTERFACE.md", "foundation/audit.py"),
        "reports": ("docs/INTERFACE.md", "foundation/audit.py"),
        "surfaces": ("docs/INTERFACE.md", "foundation/audit.py"),
    },
    notes=(
        "five call-chain types (direct, wrapper, callback, alias/partial, injected dependency) over "
        "the same frozen API change; most modules are read-only business context"
    ),
    research_question=(
        "does an audit-and-repair task reward splitting the investigation by subsystem while the "
        "shared contract stays frozen"
    ),
)


# ====================================================================================== X2


X2 = Task(
    task_id="f-x2-distributed-diagnosis",
    family="X",
    title="Event pipeline failure diagnosis and repair",
    prompt=(
        "The event pipeline loses messages: a full broker silently drops, a transient handler "
        "failure is never retried, a taken delivery is not recoverable after a restart and the retry "
        "policy has no ceiling. `diagnostics/` holds the incident report, the captured trace and the "
        "deployment history; `docs/INTERFACE.md` is the frozen contract. Diagnose the causes, repair "
        "the broker, retry, store, journal, consumer and recovery, and keep the normal path intact."
    ),
    seed=_X2["seed"],
    reference=_X2["reference"],
    public_tests=_X2["public"],
    oracle_tests=_X2["oracle"],
    workers=(
        {
            "task_id": "queue",
            "objective": "Repair the bounded broker and the retry policy",
            "assignment": (
                "[YOUR ASSIGNMENT — worker queue]\n"
                "You own `eventsys/broker.py` and `eventsys/retry.py`.\n\n"
                "OBJECTIVE: a full broker must raise `QueueFull` and never drop; duplicate ids must "
                "be `DuplicateEvent`; `take` must move the envelope into the in-flight set and "
                "journal the take; `ack` must be idempotent; `nack` must return to the front with "
                "one more attempt; `dead_letter` must record once; and the retry policy must "
                "validate its values and cap the delay at `max_delay`, counting the first attempt. "
                "The rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the two modules above. `store.py`, `journal.py`, `consumer.py`, "
                "`recovery.py` and `pipeline.py` belong to the other workers; read them, do not edit "
                "them.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the broker and "
                "retry checks, and you report what you ran."
            ),
            "owned_paths": ("eventsys/broker.py", "eventsys/retry.py"),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "storage",
            "objective": "Persist the processed set and the delivery journal",
            "assignment": (
                "[YOUR ASSIGNMENT — worker storage]\n"
                "You own `eventsys/store.py` and `eventsys/journal.py`.\n\n"
                "OBJECTIVE: the processed/dead set is one atomically written JSON document that "
                "survives a new instance and rejects corruption; the journal is an append-only JSONL "
                "log whose `pending_deliveries()` reconstructs every take without a later ack/dead, "
                "in first-take order, with attempt counts; the run audit is an append-only JSONL log "
                "that rejects a malformed line. The formats and error rules are in "
                "`docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the three modules above. The broker and consumer belong to the other "
                "workers; read them, do not edit them.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the persistence "
                "and journal checks, and you report what you ran."
            ),
            "owned_paths": (
                "eventsys/audit.py",
                "eventsys/journal.py",
                "eventsys/store.py",
            ),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "consumer",
            "objective": "Repair the delivery loop and pipeline wiring",
            "assignment": (
                "[YOUR ASSIGNMENT — worker consumer]\n"
                "You own `eventsys/consumer.py` and `eventsys/pipeline.py`.\n\n"
                "OBJECTIVE: process before ack, persist only after a successful handler, skip "
                "already-processed ids, retry a failure on the injected clock with the capped delay, "
                "dead-letter only when the attempt limit is reached, and keep the drain counters "
                "exact. The pipeline wiring, its per-run audit record, the derived metrics snapshot "
                "and its stats must follow the contract.\n\n"
                "OWNED FILES: the three modules above. The broker, store, journal and recovery "
                "belong to the other workers; read them, do not edit them.\n\n"
                "DEPENDENCIES: workers queue, storage and recovery implement the broker, the "
                "persistence and the recovery entry you call. Their actual results are provided in "
                "this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report what you ran."
            ),
            "owned_paths": (
                "eventsys/consumer.py",
                "eventsys/metrics.py",
                "eventsys/pipeline.py",
            ),
            "depends_on": ("queue", "recovery", "storage"),
            "max_turns": 18,
        },
        {
            "task_id": "recovery",
            "objective": "Re-queue unclosed deliveries after a restart",
            "assignment": (
                "[YOUR ASSIGNMENT — worker recovery]\n"
                "You own `eventsys/recovery.py`.\n\n"
                "OBJECTIVE: re-queue every journal delivery that is not processed and not dead, at "
                "the front and in journal order, without duplicating an id that the broker already "
                "acknowledged, dead-lettered or holds in flight. Return the recovered count.\n\n"
                "OWNED FILES: `eventsys/recovery.py`. The journal and store are implemented by the "
                "storage worker; read them, do not edit them.\n\n"
                "DEPENDENCIES: worker storage implements the journal and store you read. Its actual "
                "result is provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the recovery "
                "checks, and you report what you ran."
            ),
            "owned_paths": ("eventsys/recovery.py",),
            "depends_on": ("storage",),
            "max_turns": 14,
        },
    ),
    deliverables=(
        "eventsys/audit.py",
        "eventsys/broker.py",
        "eventsys/consumer.py",
        "eventsys/journal.py",
        "eventsys/metrics.py",
        "eventsys/pipeline.py",
        "eventsys/recovery.py",
        "eventsys/retry.py",
        "eventsys/store.py",
    ),
    allowed_paths=("eventsys/",),
    forbidden_paths=(
        "diagnostics/",
        "docs/INTERFACE.md",
        "eventsys/clock.py",
        "eventsys/errors.py",
        "eventsys/model.py",
        "test_public.py",
        "README.md",
        ".git/",
    ),
    shared_context=shared_block(
        requirement=(
            "Messages are lost under a burst, on a transient handler failure and after a restart. "
            "Diagnose from the incident record, the captured trace and the history file, then repair "
            "the broker, persistence, journal, consumer and recovery to the frozen contract."
        ),
        interface=(
            "- `eventsys.broker.Broker(capacity, journal=None)` with publish/take/ack/nack/"
            "dead_letter/requeue/stats\n"
            "- `eventsys.retry.RetryPolicy/delay/should_retry`\n"
            "- `eventsys.store.EventStore(path)`\n"
            "- `eventsys.journal.Journal(path)`\n"
            "- `eventsys.audit.AuditLog(path)`\n"
            "- `eventsys.metrics.snapshot(broker, store)`\n"
            "- `eventsys.consumer.Consumer(...).deliver/drain`\n"
            "- `eventsys.recovery.recover(broker, journal, store)`\n"
            "- `eventsys.pipeline.Pipeline`"
        ),
        constraints=(
            "- The pipeline is in-process and deterministic; the clock is injected, never `sleep`.\n"
            "- `diagnostics/` is read-only evidence and must not be edited.\n"
            "- Hidden tests replay the incident scenario and the recovery scenario."
        ),
        acceptance=("`python -m pytest -q test_public.py` passes and no message is lost."),
    ),
    frozen_contracts={
        "queue": ("docs/INTERFACE.md",),
        "storage": ("docs/INTERFACE.md",),
        "consumer": ("docs/INTERFACE.md",),
        "recovery": ("docs/INTERFACE.md",),
    },
    notes=(
        "the diagnosis comes from the incident record, the captured deterministic trace and the "
        "history file; the repair spans queue, persistence, journal, consumer and recovery"
    ),
    research_question=(
        "does a diagnosis task whose evidence is spread across logs and code reward delegating the "
        "forensics while the repair stays with its owners"
    ),
)

TASKS: tuple[Task, ...] = (X1, X2)

__all__ = ["TASKS", "X1", "X2"]
