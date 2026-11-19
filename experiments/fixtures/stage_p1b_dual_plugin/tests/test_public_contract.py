"""The unified contract.

Whatever channel an event came from, a consumer must receive one shape: an identifier, a channel
name, a lowercase kind, an aware UTC timestamp, one of four severities, string attributes and a
sorted tag tuple. These assertions describe the behaviour consumers already depend on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from core.adapter import normalize_event
from core.contracts import NormalizedEvent, Severity
from core.errors import UnknownSourceError
from core.registry import PluginRegistry, build_default_registry

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

SHAPE = {"event_id", "source", "kind", "occurred_at", "severity", "attributes", "tags"}


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return build_default_registry()


@pytest.fixture(scope="module")
def file_text() -> str:
    return (SAMPLES / "events.jsonl").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def message_text() -> str:
    return (SAMPLES / "messages.json").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def file_records(registry: PluginRegistry, file_text: str) -> tuple[dict[str, Any], ...]:
    return registry.parse("file_events", file_text)


@pytest.fixture(scope="module")
def message_records(registry: PluginRegistry, message_text: str) -> tuple[dict[str, Any], ...]:
    return registry.parse("message_events", message_text)


def test_a_file_record_becomes_a_normalized_event(file_records: tuple[dict[str, Any], ...]) -> None:
    event = normalize_event("file_events", file_records[1])

    assert isinstance(event, NormalizedEvent)
    assert event.event_id == "ev-5e6f7a8b"
    assert event.source == "file_events"
    assert event.kind == "build.retry"
    assert event.severity is Severity.WARNING
    assert event.occurred_at == datetime(2026, 11, 19, 2, 5, 9, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.tags == ("build", "ci")
    assert event.attributes == {"retries": "2"}


def test_a_message_record_becomes_a_normalized_event(
    message_records: tuple[dict[str, Any], ...],
) -> None:
    event = normalize_event("message_events", message_records[0])

    assert isinstance(event, NormalizedEvent)
    assert event.event_id == "msg-1041"
    assert event.source == "message_events"
    assert event.kind == "user_join"
    assert event.severity is Severity.WARNING
    assert event.occurred_at == datetime(2026, 11, 19, 2, 5, 7, tzinfo=UTC)
    assert event.tags == ("onboarding",)
    assert event.attributes == {"channel": "#general", "invited": "true"}


def test_both_channels_produce_the_same_shape(
    file_records: tuple[dict[str, Any], ...], message_records: tuple[dict[str, Any], ...]
) -> None:
    from_file = normalize_event("file_events", file_records[0]).as_dict()
    from_message = normalize_event("message_events", message_records[0]).as_dict()

    assert set(from_file) == SHAPE
    assert set(from_message) == SHAPE
    assert isinstance(from_file["occurred_at"], str)
    assert isinstance(from_message["tags"], list)


def test_an_absent_level_and_an_absent_priority_are_both_info(
    file_records: tuple[dict[str, Any], ...], message_records: tuple[dict[str, Any], ...]
) -> None:
    assert normalize_event("file_events", file_records[3]).severity is Severity.INFO
    assert normalize_event("message_events", message_records[1]).severity is Severity.INFO


def test_absent_optional_fields_become_empty_values(
    file_records: tuple[dict[str, Any], ...],
) -> None:
    event = normalize_event("file_events", file_records[3])
    assert event.tags == ()
    assert event.attributes == {}


def test_message_attributes_are_strings(
    message_records: tuple[dict[str, Any], ...],
) -> None:
    event = normalize_event("message_events", message_records[1])
    assert event.attributes == {"attempts": "3"}
    assert all(isinstance(value, str) for value in event.attributes.values())


def test_an_unknown_channel_is_refused() -> None:
    with pytest.raises(UnknownSourceError) as raised:
        normalize_event("carrier_pigeon", {"event_id": "x"})
    assert raised.value.code == "unknown_source"


def test_a_timestamp_without_an_offset_is_refused(
    file_records: tuple[dict[str, Any], ...],
) -> None:
    naive = dict(file_records[0])
    naive["ts"] = "2026-11-19T02:05:06"
    with pytest.raises(Exception) as raised:  # noqa: B017 - the code is the contract
        normalize_event("file_events", naive)
    assert getattr(raised.value, "code", None) == "naive_timestamp"


def test_an_unknown_level_is_refused(file_records: tuple[dict[str, Any], ...]) -> None:
    unknown = dict(file_records[0])
    unknown["level"] = "critical"
    with pytest.raises(Exception) as raised:  # noqa: B017 - the code is the contract
        normalize_event("file_events", unknown)
    assert getattr(raised.value, "code", None) == "unknown_level"


def test_dispatch_returns_events_in_event_order(registry: PluginRegistry, file_text: str) -> None:
    events = registry.dispatch("file_events", file_text)

    assert [event.event_id for event in events] == [
        "ev-1a2b3c4d",
        "ev-5e6f7a8b",
        "ev-3a4b5c6d",
        "ev-9c0d1e2f",
    ]
    occurred = [event.occurred_at for event in events]
    assert occurred == sorted(occurred)


def test_dispatch_is_deterministic(registry: PluginRegistry, message_text: str) -> None:
    first = [event.as_dict() for event in registry.dispatch("message_events", message_text)]
    second = [event.as_dict() for event in registry.dispatch("message_events", message_text)]

    assert first == second
    assert [item["event_id"] for item in first] == ["msg-1041", "msg-1042", "msg-1043"]


def test_the_plugins_still_expose_their_native_records(
    registry: PluginRegistry, file_text: str, message_text: str
) -> None:
    file_record = registry.parse("file_events", file_text)[0]
    message_record = registry.parse("message_events", message_text)[0]

    assert file_record["ts"] == "2026-11-19T04:05:06+02:00"
    assert file_record["level"] == "info"
    assert message_record["sent_at"] == 1795053907000
    assert message_record["type"] == "USER_JOIN"
