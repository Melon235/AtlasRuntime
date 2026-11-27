"""Run-scoped model-request lifecycle journal.

Why this exists
---------------
AnyCode's ``AgentRunner.run`` consumes the adapter stream and returns an **empty fallback result**
when the stream ends with an ``error`` event instead of a ``done`` one::

    fallback = RunResult(messages=[], output="", tool_calls=[], token_usage=EMPTY_USAGE, turns=0)
    async for event in self.stream(...):
        if event.type == "done":
            return event.data
    return fallback

The provider exception is turned into that ``error`` event inside ``stream`` and then dropped, so a
run whose very first request never completed reached the session as "success, no messages, no
usage". The adapter boundary is the last place that still holds the exception, so the journal is
attached there. It records; it never changes control flow and never retries.

What it deliberately does not do
--------------------------------
* It does not invent usage. A failed request reports ``usage_reported = False`` and no numbers;
  reservations are budget accounting, not measured usage.
* It does not swallow or wrap the exception: the wrapper re-raises the original object unchanged.
* It does not decide anything. The session reports the records; the executor classifies them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from anycode.security.redaction import safe_exception_message

#: Terminal states. ``UNKNOWN_TERMINAL_STATE`` is the honest answer for a request that was started
#: and never observed to finish — it is never upgraded to ``COMPLETED`` at run teardown.
STARTED = "STARTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
UNKNOWN_TERMINAL_STATE = "UNKNOWN_TERMINAL_STATE"

TERMINAL_STATES: tuple[str, ...] = (COMPLETED, FAILED, CANCELLED, UNKNOWN_TERMINAL_STATE)

#: How many records a snapshot keeps. A 40-turn Main with retries stays well inside this; the cap
#: exists so a pathological run cannot write an unbounded session file.
MAX_RECORDS = 500


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _http_status(error: BaseException) -> int | None:
    """The provider's HTTP status when the exception actually carries one, else ``None``."""

    for attribute in ("status_code", "http_status", "status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _error_code(error: BaseException) -> str | None:
    """The provider's own error code when the exception carries one, else ``None``."""

    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        inner = body.get("error")
        if isinstance(inner, Mapping):
            nested = inner.get("code")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _usage_payload(usage: Any) -> dict[str, Any] | None:
    """A provider-reported usage object, normalized to plain integers, or ``None``.

    Only fields the provider actually reported survive. A partially reported usage keeps the
    missing counter as ``None`` — the caller downstream already treats "partial split" as unknown.
    """

    if usage is None:
        return None
    names = (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cache_read_input_tokens",
    )
    payload: dict[str, Any] = {}
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            payload[name] = value
    if isinstance(usage, Mapping):
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                payload[name] = value
    return payload or None


@dataclass
class RequestRecord:
    """One adapter call, from admission to a terminal state."""

    request_id: str
    sequence: int
    run_id: str
    provider: str
    requested_model: str
    phase: str
    status: str = STARTED
    agent: str | None = None
    started_at: str = field(default_factory=_stamp)
    finished_at: str | None = None
    returned_model: str | None = None
    exception_type: str | None = None
    http_status: int | None = None
    provider_error_code: str | None = None
    message: str | None = None
    partial_response: bool = False
    usage_reported: bool = False
    usage: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "agent": self.agent,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "returned_model": self.returned_model,
            "phase": self.phase,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exception_type": self.exception_type,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "message": self.message,
            "partial_response": self.partial_response,
            "usage_reported": self.usage_reported,
            "usage": self.usage,
        }


class RequestJournal:
    """The records of one run. Owned by the goal session, read by its failure record."""

    def __init__(self, *, run_id: str, provider: str, requested_model: str) -> None:
        self._run_id = run_id
        self._provider = provider
        self._model = requested_model
        self._records: list[RequestRecord] = []
        self._open: dict[str, RequestRecord] = {}

    # -- recording ------------------------------------------------------------------------------

    def begin(
        self, *, phase: str, agent: str | None = None, requested_model: str | None = None
    ) -> RequestRecord:
        record = RequestRecord(
            request_id=uuid.uuid4().hex[:16],
            sequence=len(self._records) + 1,
            run_id=self._run_id,
            provider=self._provider,
            requested_model=requested_model or self._model,
            phase=phase,
            agent=agent,
        )
        self._records.append(record)
        self._open[record.request_id] = record
        return record

    def complete(
        self,
        record: RequestRecord,
        *,
        returned_model: str | None = None,
        usage: Any = None,
    ) -> None:
        payload = _usage_payload(usage)
        record.status = COMPLETED
        record.finished_at = _stamp()
        record.returned_model = returned_model if isinstance(returned_model, str) else None
        record.usage = payload
        record.usage_reported = payload is not None
        self._open.pop(record.request_id, None)

    def fail(
        self,
        record: RequestRecord,
        error: BaseException,
        *,
        partial: bool = False,
    ) -> None:
        record.status = FAILED
        record.finished_at = _stamp()
        record.exception_type = type(error).__name__
        record.http_status = _http_status(error)
        record.provider_error_code = _error_code(error)
        record.message = safe_exception_message(error) or str(error)
        record.partial_response = bool(partial)
        self._open.pop(record.request_id, None)

    def cancel(self, record: RequestRecord, *, partial: bool = False) -> None:
        record.status = CANCELLED
        record.finished_at = _stamp()
        record.partial_response = bool(partial)
        record.message = "the caller cancelled the request"
        self._open.pop(record.request_id, None)

    def settle_one(self, record: RequestRecord, *, partial: bool = False) -> None:
        """One request ended without a completion or an error event: say exactly that."""

        record.status = UNKNOWN_TERMINAL_STATE
        record.finished_at = _stamp()
        record.partial_response = bool(partial)
        record.message = "the stream ended without a completion or an error event"
        self._open.pop(record.request_id, None)

    def settle_open(self) -> int:
        """Close requests that never reached a terminal state, as exactly that.

        Called once when the run ends. It is deliberately *not* "completed": a request that was
        started and never observed to finish leaves the usage unknown, and saying so is the point.
        """

        settled = 0
        for record in list(self._open.values()):
            record.status = UNKNOWN_TERMINAL_STATE
            record.finished_at = _stamp()
            record.message = "the run ended before this request reached a terminal state"
            settled += 1
        self._open.clear()
        return settled

    # -- reading --------------------------------------------------------------------------------

    def last_failure(self) -> dict[str, Any] | None:
        for record in reversed(self._records):
            if record.status == FAILED:
                return record.as_dict()
        return None

    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in (STARTED, *TERMINAL_STATES)}
        for record in self._records:
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts

    def snapshot(self) -> dict[str, Any]:
        records = self._records[-MAX_RECORDS:]
        return {
            "run_id": self._run_id,
            "provider": self._provider,
            "requested_model": self._model,
            "requests_total": len(self._records),
            "counts": self.counts(),
            "usage_observed": any(record.usage_reported for record in self._records),
            "records_truncated": len(self._records) > len(records),
            "records": [record.as_dict() for record in records],
        }


class JournaledAdapter:
    """An ``LLMAdapter`` wrapper that journals every call and re-raises unchanged.

    Only the two adapter methods AnyCode calls are implemented (``chat``/``stream``); nothing is
    retried, buffered or reinterpreted. A ``stream`` that ends with an ``error`` event is recorded
    as ``FAILED`` with that event's exception, which is the case the runner silently drops.
    """

    def __init__(self, inner: Any, journal: RequestJournal, *, agent: str | None = None) -> None:
        self._inner = inner
        self._journal = journal
        self._agent = agent

    @property
    def name(self) -> str:
        return str(getattr(self._inner, "name", "unknown"))

    async def chat(
        self,
        messages: list[Any],
        options: Any,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        record = self._journal.begin(
            phase="chat", agent=self._agent, requested_model=getattr(options, "model", None)
        )
        kwargs: dict[str, Any] = (
            {} if response_format is None else {"response_format": response_format}
        )
        try:
            response = await self._inner.chat(messages, options, **kwargs)
        except asyncio.CancelledError:
            self._journal.cancel(record)
            raise
        except BaseException as error:  # noqa: BLE001 - recorded, then re-raised unchanged
            self._journal.fail(record, error)
            raise
        self._journal.complete(
            record,
            returned_model=getattr(response, "model", None),
            usage=getattr(response, "usage", None),
        )
        return response

    def stream(self, messages: list[Any], options: Any) -> AsyncIterator[Any]:
        async def _generate() -> AsyncIterator[Any]:
            record = self._journal.begin(
                phase="stream", agent=self._agent, requested_model=getattr(options, "model", None)
            )
            done = False
            terminal_error: BaseException | None = None
            partial = False
            try:
                async for event in self._inner.stream(messages, options):
                    event_type = getattr(event, "type", None)
                    data = getattr(event, "data", None)
                    if event_type == "text":
                        partial = True
                    elif event_type == "error":
                        terminal_error = (
                            data
                            if isinstance(data, BaseException)
                            else RuntimeError(str(data) or "provider error event")
                        )
                    elif event_type == "done":
                        done = True
                        self._journal.complete(
                            record,
                            returned_model=getattr(data, "model", None),
                            usage=getattr(data, "usage", None),
                        )
                    yield event
            except asyncio.CancelledError:
                self._journal.cancel(record, partial=partial)
                raise
            except BaseException as error:  # noqa: BLE001 - recorded, then re-raised unchanged
                self._journal.fail(record, error, partial=partial)
                raise
            if terminal_error is not None and record.status == STARTED:
                self._journal.fail(record, terminal_error, partial=partial)
            elif not done and record.status == STARTED:
                self._journal.settle_one(record, partial=partial)

        return _generate()


__all__ = [
    "CANCELLED",
    "COMPLETED",
    "FAILED",
    "JournaledAdapter",
    "MAX_RECORDS",
    "RequestJournal",
    "RequestRecord",
    "STARTED",
    "TERMINAL_STATES",
    "UNKNOWN_TERMINAL_STATE",
]
