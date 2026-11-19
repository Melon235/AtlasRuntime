"""Native behaviour of the message channel.

Same contract as the file channel's suite: this package is vendored and every value asserted here is
relied on by its callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from plugins.message_events import (
    MessageEventError,
    coerce_priority,
    normalize_labels,
    parse_document,
    parse_timestamp,
    severity_of,
    stringify_payload,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture(scope="module")
def document() -> str:
    return (SAMPLES / "messages.json").read_text(encoding="utf-8")


def test_the_document_decodes_every_message(document: str) -> None:
    messages = parse_document(document)
    assert [message.id for message in messages] == [1041, 1042, 1043]
    assert [message.type for message in messages] == ["USER_JOIN", "USER_LEFT", "BOT_ERROR"]


def test_native_records_keep_the_raw_values(document: str) -> None:
    first, second, third = parse_document(document)
    assert first.sent_at == 1795053907000
    assert first.priority == 4
    assert first.labels == ("Onboarding", "onboarding")
    assert first.payload == {"channel": "#general", "invited": True}
    assert second.priority is None
    assert third.payload == {}


def test_a_bare_list_of_messages_is_accepted() -> None:
    messages = parse_document('[{"id": 7, "sent_at": 1795053907000, "type": "USER_JOIN"}]')
    assert len(messages) == 1
    assert messages[0].priority is None


def test_priority_defaults_and_bounds() -> None:
    assert coerce_priority(None) == 3
    assert severity_of(None) == "info"
    assert severity_of(1) == "debug"
    assert severity_of(4) == "warning"
    assert severity_of(5) == "error"


def test_priority_outside_the_range_is_refused() -> None:
    with pytest.raises(MessageEventError) as raised:
        coerce_priority(9)
    assert raised.value.code == "priority_out_of_range"

    with pytest.raises(MessageEventError) as wrong_type:
        coerce_priority(True)
    assert wrong_type.value.code == "wrong_type"


def test_epoch_milliseconds_become_utc() -> None:
    assert parse_timestamp(1795053907000) == datetime(2026, 11, 19, 2, 5, 7, tzinfo=UTC)
    assert (
        parse_timestamp(1795053907000).utcoffset() == datetime(2026, 11, 19, tzinfo=UTC).utcoffset()
    )


def test_labels_are_normalized() -> None:
    assert normalize_labels(["B", "a", " b ", ""]) == ("a", "b")
    assert normalize_labels([]) == ()
    assert normalize_labels(None) == ()


def test_payload_values_are_rendered_as_strings() -> None:
    rendered = stringify_payload(
        {"n": 3, "ratio": 0.5, "ok": True, "skip": None, "name": "x", "nested": {"b": 1, "a": 2}}
    )
    assert rendered == {
        "name": "x",
        "n": "3",
        "nested": '{"a":2,"b":1}',
        "ok": "true",
        "ratio": "0.5",
    }


def test_a_lowercase_type_is_refused() -> None:
    with pytest.raises(MessageEventError) as raised:
        parse_document('[{"id": 7, "sent_at": 1, "type": "user_join"}]')
    assert raised.value.code == "wrong_type"


def test_a_non_integer_identifier_is_refused() -> None:
    with pytest.raises(MessageEventError) as raised:
        parse_document('[{"id": "7", "sent_at": 1, "type": "USER_JOIN"}]')
    assert raised.value.code == "bad_message_id"
