"""Hidden checks for the v2 wire protocol contract (never staged into the agent workspace)."""

import pytest

from wire import codecs, dispatch, frame, json_message
from wire.checksum import crc32, verify
from wire.errors import (
    ChecksumError,
    InvalidPayload,
    ProtocolError,
    Truncated,
    UnknownType,
    VersionError,
)
from wire.model import Frame, Message
from wire.gateway import Gateway
from wire.selector import Selector
from wire.service import StreamingService, handle
from wire.validation import validate


# -- JSON message protocol ---------------------------------------------------------------------


def test_json_body_crc_ignores_key_order_and_nesting():
    left = json_message.encode("k", {"z": {"b": 2, "a": 1}}, identifier="i")
    right = json_message.encode("k", {"z": {"a": 1, "b": 2}}, identifier="i")
    assert json_message.decode(left).checksum == json_message.decode(right).checksum
    assert json_message.decode(left).body == {"z": {"b": 2, "a": 1}}


def test_json_rejects_malformed_documents():
    with pytest.raises(ProtocolError):
        json_message.decode(b"\xff\xfe")
    with pytest.raises(ProtocolError):
        json_message.decode(b"[]")
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 2, "type": "", "id": "i", "body": {}, "crc": 0}')
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 2, "type": "k", "id": "i", "body": [], "crc": 0}')
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 2, "type": "k", "body": {}, "crc": 0}')
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 2, "type": "k", "id": "", "body": {}, "crc": 0}')


def test_json_v2_crc_must_be_a_uint32():
    body = b'{"a":1}'
    checksum = crc32(body)
    template = '{{"v": 2, "type": "k", "id": "i", "body": {{"a": 1}}, "crc": {crc}}}'
    assert json_message.decode(template.format(crc=checksum).encode()).checksum == checksum
    with pytest.raises(ChecksumError):
        json_message.decode(template.format(crc=(checksum + 1) & 0xFFFFFFFF).encode())
    with pytest.raises(ChecksumError):
        json_message.decode(template.format(crc="true").encode())
    with pytest.raises(ChecksumError):
        json_message.decode(template.format(crc=-1).encode())


def test_json_version_must_be_an_integer():
    with pytest.raises(VersionError):
        json_message.decode(b'{"v": true, "type": "k", "body": {}}')
    with pytest.raises(VersionError):
        json_message.decode(b'{"v": "2", "type": "k", "body": {}}')
    with pytest.raises(VersionError):
        json_message.decode(b'{"v": 3, "type": "k", "body": {}}')


def test_json_v1_is_preserved_and_may_carry_an_id():
    message = json_message.decode(b'{"v": 1, "type": "old", "id": "legacy", "body": {"a": 1}}')
    assert (message.version, message.identifier, message.checksum) == (1, "legacy", None)


def test_json_rejects_duplicate_keys_at_any_depth():
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 1, "type": "k", "body": {"a": 1, "a": 2}}')
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 1, "v": 1, "type": "k", "body": {}}')


def test_json_decode_rejects_a_second_message_or_a_blank_document():
    with pytest.raises(ProtocolError):
        json_message.decode(b'{"v": 1, "type": "k", "body": {}}\n{"v": 1, "type": "k", "body": {}}')
    with pytest.raises(ProtocolError):
        json_message.decode(b"   ")


def test_json_stream_handles_blank_lines_crlf_and_pending():
    data = (
        b"\r\n"
        + json_message.encode("a", {"n": 1}, identifier="i").rstrip(b"\n")
        + b"\r\n\n"
        + json_message.encode("b", {"n": 2}, identifier="j")
    )
    decoder = json_message.Decoder()
    received = decoder.feed(data)
    assert [message.kind for message in received] == ["a", "b"]
    assert decoder.pending == 0
    decoder.finish()


def test_json_stream_limit_is_enforced_on_complete_and_partial_lines():
    decoder = json_message.Decoder(limit=16)
    payload = json_message.encode("k", {"long": "x" * 40}, identifier="i")
    with pytest.raises(ProtocolError):
        decoder.feed(payload)
    with pytest.raises(ProtocolError):
        json_message.Decoder(limit=4).feed(b"12345")
    with pytest.raises(ValueError):
        json_message.Decoder(limit=0)


def test_json_stream_mixes_v1_and_v2():
    decoder = json_message.Decoder()
    received = decoder.feed(b'{"v": 1, "type": "old", "body": {}}\n')
    received.extend(decoder.feed(json_message.encode("new", {}, identifier="i")))
    assert [(message.version, message.kind) for message in received] == [(1, "old"), (2, "new")]


def test_json_finish_clears_the_truncated_buffer():
    decoder = json_message.Decoder()
    decoder.feed(b'{"v": 1, "type": "k", "body": {}')
    with pytest.raises(Truncated):
        decoder.finish()
    assert decoder.pending == 0


# -- binary frame protocol ---------------------------------------------------------------------


def test_checksum_verify_names_the_mismatch():
    verify(b"abc", crc32(b"abc"))
    with pytest.raises(ChecksumError):
        verify(b"abc", 0)


def test_frame_encoding_layout_and_empty_payload():
    data = frame.encode("put", b"")
    assert data[:4] == b"ATL2"
    assert data[4] == 2
    assert data[5] == 3
    assert data[6:9] == b"put"
    assert data[9:13] == b"\x00\x00\x00\x00"
    assert data[13:17] == b"\x00\x00\x00\x00"
    assert frame.decode(data).payload == b""


def test_frame_rejects_bad_kinds_and_payloads():
    for kind in ("", "a:b", "with space", "x" * 33, "é"):
        with pytest.raises(ProtocolError):
            frame.encode(kind, b"x")
    with pytest.raises(ProtocolError):
        frame.encode("ok", "text")
    with pytest.raises(ProtocolError):
        frame.encode("ok", b"x" * (frame.MAX_PAYLOAD + 1))


def test_frame_rejects_trailing_bytes_and_bad_magic():
    data = frame.encode("a", b"x")
    with pytest.raises(ProtocolError):
        frame.decode(data + b"extra")
    with pytest.raises(ProtocolError):
        frame.decode(b"XXXX" + data[4:])


def test_frame_rejects_an_invalid_declared_header():
    bad_kind_len = b"ATL2" + bytes([2, 0]) + b"\x00" * 8
    with pytest.raises(ProtocolError):
        frame.decode(bad_kind_len)
    huge = (
        b"ATL2"
        + bytes([2, 1])
        + b"a"
        + (frame.MAX_PAYLOAD + 1).to_bytes(4, "big")
        + b"\x00\x00\x00\x00"
    )
    with pytest.raises(ProtocolError):
        frame.decode(huge)


def test_frame_decoder_handles_magic_split_over_chunks():
    data = frame.encode("a", b"one") + frame.encode("b", b"two")
    decoder = frame.Decoder()
    received = []
    for index in range(0, len(data), 3):
        received.extend(decoder.feed(data[index : index + 3]))
    assert [item.kind for item in received] == ["a", "b"]
    assert decoder.pending == 0


def test_frame_decoder_rejects_bad_magic_version_and_kind_length():
    with pytest.raises(ProtocolError):
        frame.Decoder().feed(b"X")
    with pytest.raises(VersionError):
        frame.Decoder().feed(b"ATL2" + bytes([9, 1]))
    with pytest.raises(ProtocolError):
        frame.Decoder().feed(b"ATL2" + bytes([2, 0]))
    with pytest.raises(ValueError):
        frame.Decoder(limit=0)


def test_frame_decoder_finish_reports_and_clears_a_partial_frame():
    decoder = frame.Decoder()
    decoder.feed(frame.encode("a", b"one")[:7])
    with pytest.raises(Truncated):
        decoder.finish()
    assert decoder.pending == 0


def _v1_frame(kind: str, payload: bytes) -> bytes:
    encoded = kind.encode("ascii")
    return (
        b"ATL1"
        + bytes([1, len(encoded)])
        + encoded
        + len(payload).to_bytes(4, "big")
        + payload
    )


def test_frame_v1_is_read_only_and_mixes_with_v2_in_one_stream():
    v1 = _v1_frame("old", b"one")
    v2 = frame.encode("new", b"two")
    decoder = frame.Decoder()
    received = []
    for byte in v1 + v2:
        received.extend(decoder.feed(bytes([byte])))
    assert [(item.version, item.kind, item.payload) for item in received] == [
        (1, "old", b"one"),
        (2, "new", b"two"),
    ]
    assert [item.checksum for item in received] == [0, crc32(b"two")]


def test_frame_v1_still_enforces_length_and_trailing_bytes():
    v1 = _v1_frame("old", b"abc")
    with pytest.raises(Truncated):
        frame.decode(v1[:-1])
    with pytest.raises(ProtocolError):
        frame.decode(v1 + b"x")
    broken = bytearray(v1)
    broken[4] = 2
    with pytest.raises(VersionError):
        frame.decode(bytes(broken))


def test_selector_validates_the_first_bytes_and_is_sticky():
    with pytest.raises(ProtocolError):
        Selector().feed(b"<xml>")
    with pytest.raises(ProtocolError):
        Selector().feed(b"AX")
    selector = Selector()
    selector.feed(json_message.encode("a", {}, identifier="i"))
    with pytest.raises(ProtocolError):
        selector.feed(frame.encode("b", b"x") + b"\n")


def test_selector_finish_reports_an_undetected_partial_stream():
    empty = Selector()
    empty.feed(b"   ")
    empty.finish()
    partial = Selector()
    partial.feed(b"AT")
    with pytest.raises(Truncated):
        partial.finish()
    assert partial.pending == 0


# -- codecs, dispatch and service ---------------------------------------------------------------


def test_codec_registry_rejects_duplicates_without_overwriting():
    class Probe:
        name = "p2-probe"

        def decode(self, data):
            return data

        def decoder(self, limit=None):
            return None

    first = Probe()
    codecs.register(first)
    with pytest.raises(ProtocolError):
        codecs.register(Probe())
    assert codecs.get("p2-probe") is first
    assert codecs.available() == sorted(codecs.available())


def test_codec_decoder_forwards_the_limit():
    decoder = codecs.decoder("json", 4)
    with pytest.raises(ProtocolError):
        decoder.feed(b"12345")
    binary = codecs.decoder("binary", 2)
    with pytest.raises(ProtocolError):
        binary.feed(frame.encode("a", b"123"))


def test_dispatch_distinguishes_body_from_payload_and_validates_registration():
    dispatch.register("p2-json", lambda payload: ("json", payload))
    dispatch.register("p2-bin", lambda payload: ("bin", payload))
    message = json_message.decode(json_message.encode("p2-json", {"a": 1}, identifier="i"))
    binary = frame.decode(frame.encode("p2-bin", b"raw"))
    assert dispatch.dispatch(message) == ("json", {"a": 1})
    assert dispatch.dispatch(binary) == ("bin", b"raw")
    with pytest.raises(ProtocolError):
        dispatch.register("", lambda payload: payload)
    with pytest.raises(ProtocolError):
        dispatch.register("p2-new", "not callable")
    with pytest.raises(UnknownType):
        dispatch.dispatch(Frame(2, "p2-nope", b"", 0))


def test_payload_validation_rules():
    validate("unknown-kind", {"anything": 1})
    validate("greet", {"name": "atlas"})
    validate("greet", {"name": "atlas", "loud": True, "extra": 1})
    with pytest.raises(InvalidPayload):
        validate("greet", {"loud": True})
    with pytest.raises(InvalidPayload):
        validate("greet", {"name": "atlas", "loud": "yes"})
    with pytest.raises(InvalidPayload):
        validate("put", {"key": "k", "value": True})
    with pytest.raises(InvalidPayload):
        validate("echo", ["not", "an", "object"])


def test_the_gateway_handles_both_protocols_and_a_partial_stream():
    dispatch.register("echo", lambda payload: payload["message"])
    dispatch.register("p2-gateway-bin", lambda payload: payload)
    message = json_message.encode("echo", {"message": "atlas"}, identifier="i")
    gateway = Gateway()
    assert gateway.feed(b"\n" + message[:3]) == []
    assert gateway.feed(message[3:]) == ["atlas"]
    assert gateway.protocol == "json"
    gateway.finish()

    binary = frame.encode("p2-gateway-bin", b"raw")
    other = Gateway()
    received = []
    for index in range(0, len(binary), 4):
        received.extend(other.feed(binary[index : index + 4]))
    assert received == [b"raw"]
    assert other.protocol == "binary"
    other.finish()

    invalid = json_message.encode("echo", {"nope": 1}, identifier="j")
    with pytest.raises(InvalidPayload):
        Gateway().feed(invalid)
    partial = Gateway()
    partial.feed(b"ATL")
    with pytest.raises(Truncated):
        partial.finish()


def test_the_gateway_can_be_bound_to_one_codec():
    dispatch.register("put", lambda payload: payload["value"])
    gateway = Gateway("json")
    assert gateway.protocol == "json"
    body = {"key": "k", "value": 7}
    assert gateway.feed(json_message.encode("put", body, identifier="i")) == [7]
    with pytest.raises(InvalidPayload):
        gateway.feed(json_message.encode("put", {"key": "k", "value": True}, identifier="j"))
    gateway.finish()


def test_service_handles_binary_and_a_split_stream():
    dispatch.register("p2-binary", lambda payload: payload.decode("ascii"))
    data = frame.encode("p2-binary", b"hello")
    assert handle("binary", data) == "hello"
    service = StreamingService("binary", limit=1024)
    assert service.feed(data[:5]) == []
    assert service.feed(data[5:]) == ["hello"]
    service.finish()
    with pytest.raises(ProtocolError):
        handle("xml", b"x")
