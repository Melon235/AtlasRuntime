"""Representative business mutations for the eight Stage F tasks.

Every mutation starts from the *reference* workspace and breaks one published business rule while
keeping the program importable and runnable. The public suite may or may not catch it; the
independent oracle must catch every mutation listed here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mutation:
    mutation_id: str
    task_id: str
    rule: str
    semantics: str
    edits: tuple[tuple[str, str, str], ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(edit[0] for edit in self.edits)


MUTATIONS: tuple[Mutation, ...] = (
    # -- S1 / S2: the reused Closure-2 fixtures ------------------------------------------------
    Mutation(
        mutation_id="S1-M1-empty-values",
        task_id="closure2-pipeline-migration",
        rule="`normalise_all` drops records whose value is empty after trimming",
        semantics="keeps an empty-valued record, so the v2 record stream contains a row the "
        "contract drops",
        edits=(
            (
                "pipeline/normalise.py",
                '        value = record["value"].strip()\n'
                "        if not value:\n"
                "            continue\n",
                '        value = record["value"].strip()\n',
            ),
        ),
    ),
    Mutation(
        mutation_id="S2-M1-missing-reject",
        task_id="closure2-state-machine-refactor",
        rule="`draft --reject--> rejected` is part of the frozen table",
        semantics="removes the reject transition, so the documented caller path no longer works",
        edits=(
            (
                "lifecycle/state.py",
                '    "draft": {"approve": "approved", "reject": "rejected", '
                '"cancel": "cancelled"},',
                '    "draft": {"approve": "approved", "cancel": "cancelled"},',
            ),
        ),
    ),
    # -- P1 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="P1-M1-unknown-header",
        task_id="f-p1-connector-migration",
        rule="an unknown CSV header cell is a SchemaError",
        semantics="silently accepts unknown header cells, so a schema mismatch is ingested as if "
        "it were valid",
        edits=(
            (
                "ingest/columns.py",
                "    known = set(schema.names)\n"
                "    unknown = sorted(set(names) - known)\n"
                "    if unknown:\n"
                '        raise SchemaError(f"unknown header cells: {unknown}")\n',
                "    known = set(schema.names)\n    _ = known\n",
            ),
        ),
    ),
    Mutation(
        mutation_id="P1-M2-backwards-cursor",
        task_id="f-p1-connector-migration",
        rule="checkpoint saves are monotonic per source",
        semantics="accepts a cursor that moves backwards, so a resumed run silently reprocesses "
        "already-consumed rows",
        edits=(
            (
                "ingest/checkpoint.py",
                "        previous = cursors.get(source)\n"
                "        if previous is not None and cursor < previous:\n"
                "            raise CheckpointError(\n"
                '                f"cursor for {source!r} would move backwards '
                'from {previous} to {cursor}"\n'
                "            )\n",
                "",
            ),
        ),
    ),
    Mutation(
        mutation_id="P1-M3-unbounded-batch",
        task_id="f-p1-connector-migration",
        rule="a batch is flushed before it would exceed `max_bytes`",
        semantics="ignores the byte ceiling, so batches exceed the transport limit whenever record "
        "counts stay under `max_records`",
        edits=(
            (
                "ingest/batching.py",
                "        over_bytes = bool(current) and current_bytes + size > max_bytes\n",
                "        over_bytes = False\n",
            ),
        ),
    ),
    # -- P2 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="P2-M1-crc-not-verified",
        task_id="f-p2-protocol-adapter",
        rule="a v2 JSON message whose declared CRC differs is a ChecksumError",
        semantics="accepts a corrupt body instead of rejecting it",
        edits=(
            (
                "wire/json_message.py",
                "    verify(canonical_body(body), declared)\n",
                "    _ = canonical_body(body)\n",
            ),
        ),
    ),
    Mutation(
        mutation_id="P2-M2-limit-ignored",
        task_id="f-p2-protocol-adapter",
        rule="a declared binary payload above the decoder limit is a ProtocolError",
        semantics="ignores the decoder limit, so a hostile declared length is buffered until the "
        "stream ends",
        edits=(
            (
                "wire/frame.py",
                "            if payload_len > self.limit:\n"
                "                raise ProtocolError("
                '"declared payload exceeds the decoder limit")\n',
                "",
            ),
        ),
    ),
    Mutation(
        mutation_id="P2-M3-selector-no-whitespace",
        task_id="f-p2-protocol-adapter",
        rule="the selector skips leading ASCII whitespace while detecting the protocol",
        semantics="refuses a JSON stream with a leading blank line instead of detecting it",
        edits=(
            (
                "wire/selector.py",
                "        stripped = self._buffer.lstrip(_SPACE)\n",
                "        stripped = self._buffer\n",
            ),
        ),
    ),
    # -- X1 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="X1-M1-refund-tax-sign",
        task_id="f-x1-api-compat-audit",
        rule="the refund tax mirrors the charge sign",
        semantics="reports a positive refund tax, so the refund event no longer mirrors the charge",
        edits=(
            (
                "billing/refund.py",
                '            "tax": -tax_for(amount),',
                '            "tax": tax_for(amount),',
            ),
        ),
    ),
    Mutation(
        mutation_id="X1-M2-reservation-not-atomic",
        task_id="f-x1-api-compat-audit",
        rule="a failed reservation leaves the stock ledger unchanged",
        semantics="reserves line by line without a pre-check, so a later shortage leaves earlier "
        "lines decremented",
        edits=(
            (
                "inventory/reservations.py",
                "    required = _requirements(order)\n"
                "    for sku in sorted(required):\n"
                "        if stock.available(sku) < required[sku]:\n"
                "            raise InsufficientStock(\n"
                '                f"{sku}: need {required[sku]}, have {stock.available(sku)}"\n'
                "            )\n"
                "    for sku in sorted(required):\n"
                "        stock.reserve(sku, required[sku])\n",
                "    required = _requirements(order)\n"
                "    for sku in sorted(required):\n"
                "        stock.reserve(sku, required[sku])\n",
            ),
        ),
    ),
    Mutation(
        mutation_id="X1-M3-statement-no-credits",
        task_id="f-x1-api-compat-audit",
        rule="`statement` sums the WELCOME10 credit per order",
        semantics="always reports zero credits, so the net amount is overstated",
        edits=(
            (
                "billing/statements.py",
                "    credits = sum(discount_minor(order, DISCOUNT_CODE) for order in items)\n",
                "    credits = 0\n",
            ),
        ),
    ),
    # -- X2 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="X2-M1-mark-before-handler",
        task_id="f-x2-distributed-diagnosis",
        rule="a message is marked processed only after the handler succeeded",
        semantics="marks it before the handler, so a transient failure can never be retried and is "
        "acked as a duplicate",
        edits=(
            (
                "eventsys/consumer.py",
                "        try:\n            self.handler(event)\n",
                "        self.store.mark_processed(event.id)\n"
                "        try:\n"
                "            self.handler(event)\n",
            ),
            (
                "eventsys/consumer.py",
                "        self.store.mark_processed(event.id)\n"
                "        self.broker.ack(envelope)\n"
                '        return "processed"\n',
                '        self.broker.ack(envelope)\n        return "processed"\n',
            ),
        ),
    ),
    Mutation(
        mutation_id="X2-M2-broker-drops",
        task_id="f-x2-distributed-diagnosis",
        rule="a full broker raises QueueFull and never drops",
        semantics="evicts the oldest queued message on overflow, exactly the incident behaviour",
        edits=(
            (
                "eventsys/broker.py",
                "        if len(self._items) >= self.capacity:\n"
                '            raise QueueFull(f"broker is full ({self.capacity})")\n',
                "        if len(self._items) >= self.capacity:\n"
                "            self._items.popleft()\n",
            ),
        ),
    ),
    Mutation(
        mutation_id="X2-M3-journal-ignores-acks",
        task_id="f-x2-distributed-diagnosis",
        rule="`pending_deliveries` excludes every later ack/dead record",
        semantics="reports acknowledged deliveries as pending, so recovery redelivers them",
        edits=(
            (
                "eventsys/journal.py",
                "        return [envelope for event_id, envelope in taken.items() "
                "if event_id not in closed]\n",
                "        return list(taken.values())\n",
            ),
        ),
    ),
    # -- C1 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="C1-M1-precedence-swapped",
        task_id="f-c1-config-lifecycle",
        rule="precedence is explicit > environment > file > default",
        semantics="lets the file override the environment",
        edits=(
            (
                "configkit/merge.py",
                '        ("file", file_values),\n'
                '        ("profile", profile_values or {}),\n'
                '        ("env", env_values),\n',
                '        ("env", env_values),\n'
                '        ("profile", profile_values or {}),\n'
                '        ("file", file_values),\n',
            ),
        ),
    ),
    Mutation(
        mutation_id="C1-M2-unknown-keys-ignored",
        task_id="f-c1-config-lifecycle",
        rule="an unknown configuration path is an UnknownKey",
        semantics="silently accepts unknown keys, so a typo never fails a deployment",
        edits=(
            (
                "configkit/validate.py",
                "    unknown = sorted(set(flat) - set(FIELDS))\n"
                "    if unknown:\n"
                '        raise UnknownKey(unknown[0], "key is not part of the frozen spec")\n',
                "",
            ),
        ),
    ),
    Mutation(
        mutation_id="C1-M3-legacy-keys-kept",
        task_id="f-c1-config-lifecycle",
        rule="migration drops every `legacy_*` key",
        semantics="keeps a legacy key, which then reaches validation as an unknown path",
        edits=(
            (
                "configkit/migrate.py",
                "        if key.startswith(LEGACY_PREFIX):\n            continue\n",
                "",
            ),
        ),
    ),
    # -- C2 ------------------------------------------------------------------------------------
    Mutation(
        mutation_id="C2-M1-cancel-from-failed",
        task_id="f-c2-scheduler-retry",
        rule="a failed record can be cancelled",
        semantics="removes the cancel edge from `failed`, so a retryable failure cannot be stopped",
        edits=(
            (
                "schedkit/machine.py",
                '    "failed": {"retry": "queued", "exhaust": "dead", "cancel": "cancelled"},',
                '    "failed": {"retry": "queued", "exhaust": "dead"},',
            ),
        ),
    ),
    Mutation(
        mutation_id="C2-M2-running-not-persisted",
        task_id="f-c2-scheduler-retry",
        rule="the record is persisted as `running` before the handler runs",
        semantics="runs the handler without persisting the running state, so a crash leaves no "
        "recoverable record",
        edits=(
            (
                "schedkit/scheduler.py",
                "            self.store.update(running)\n"
                '            self._log(running, "started")\n',
                '            self._log(running, "started")\n',
            ),
        ),
    ),
    Mutation(
        mutation_id="C2-M3-uncapped-delay",
        task_id="f-c2-scheduler-retry",
        rule="the retry delay is capped by `max_delay`",
        semantics="lets the delay grow without a ceiling",
        edits=(
            (
                "schedkit/retry.py",
                "    return min(active.base_delay * active.multiplier ** (attempt - 1), "
                "active.max_delay)\n",
                "    return active.base_delay * active.multiplier ** (attempt - 1)\n",
            ),
        ),
    ),
)

MUTATIONS_BY_TASK: dict[str, tuple[Mutation, ...]] = {}
for _mutation in MUTATIONS:
    MUTATIONS_BY_TASK.setdefault(_mutation.task_id, ())
    MUTATIONS_BY_TASK[_mutation.task_id] = (
        *MUTATIONS_BY_TASK[_mutation.task_id],
        _mutation,
    )

__all__ = ["MUTATIONS", "MUTATIONS_BY_TASK", "Mutation"]
