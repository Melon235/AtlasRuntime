"""Public checks for the v2 wire protocol contract."""

import pytest

from wire import codecs, dispatch, frame, json_message
from wire.checksum import crc32
from wire.errors import (
    ChecksumError,
    InvalidPayload,
    ProtocolError,
    Truncated,
    UnknownType,
    VersionError,
)
from wire.model import Message
from wire.gateway import Gateway
from wire.selector import Selector
from wire.service import StreamingService, handle


def test_crc32_is_the_standard_value():
    assert crc32(b"123456789") == 0xCBF43926


def test_json_v2_round_trip_and_identifier():
    data = json_message.encode("greet", {"name": "atlas"}, identifier="m-1")
    assert data.endswith(b"\n")
    message = json_message.decode(data)
    assert (message.version, message.kind, message.identifier) == (2, "greet", "m-1")
    assert message.body == {"name": "atlas"}
    assert message.checksum == crc32(b'{"name":"atlas"}')


def test_json_crc_does_not_depend_on_body_key_order():
    left = json_message.encode("k", {"a": 1, "b": 2}, identifier="i")
    right = json_message.encode("k", {"b": 2, "a": 1}, identifier="i")
    assert json_message.decode(left).checksum == json_message.decode(right).checksum


def test_json_v1_message_still_decodes():
    message = json_message.decode(b'{"v": 1, "type": "old", "body": {"a": 1}}')
    assert (message.version, message.kind, message.identifier) == (1, "old", "")
    assert message.checksum is None


def test_json_version_and_checksum_are_enforced():
    with pytest.raises(VersionError):
        json_message.decode(b'{"v": 9, "type": "k", "body": {}}')
    document = b'{"v": 2, "type": "k", "id": "i", "body": {"a": 1}, "crc": 0}'
    with pytest.raises(ChecksumError):
        json_message.decode(document)


def test_json_stream_decoder_accepts_any_split():
    first = json_message.encode("alpha", {"x": 1}, identifier="a")
    second = json_message.encode("beta", {"y": 2}, identifier="b")
    decoder = json_message.Decoder()
    messages = []
    for byte in first + second:
        messages.extend(decoder.feed(bytes([byte])))
    assert [message.kind for message in messages] == ["alpha", "beta"]
    decoder.finish()


def test_json_stream_decoder_reports_a_truncated_message():
    data = json_message.encode("alpha", {"x": 1}, identifier="a")
    decoder = json_message.Decoder()
    decoder.feed(data[:-1])
    with pytest.raises(Truncated):
        decoder.finish()


def test_binary_frame_round_trip_and_checksum():
    data = frame.encode("put", b"hello")
    assert data[:4] == b"ATL2" and data[4] == 2
    decoded = frame.decode(data)
    assert (decoded.version, decoded.kind, decoded.payload) == (2, "put", b"hello")
    assert decoded.checksum == crc32(b"hello")
    with pytest.raises(ChecksumError):
        frame.decode(data[:-1] + b"x")


def test_binary_frame_rejects_truncation_and_version():
    data = frame.encode("put", b"hello")
    with pytest.raises(Truncated):
        frame.decode(data[:-1])
    broken = bytearray(data)
    broken[4] = 3
    with pytest.raises(VersionError):
        frame.decode(bytes(broken))


def test_binary_stream_decoder_accepts_any_split():
    data = frame.encode("a", b"one") + frame.encode("b", b"two")
    decoder = frame.Decoder()
    received = []
    received.extend(decoder.feed(data[:3]))
    received.extend(decoder.feed(data[3:13]))
    received.extend(decoder.feed(data[13:]))
    assert [item.payload for item in received] == [b"one", b"two"]
    decoder.finish()


def test_binary_decoder_enforces_its_payload_limit():
    data = frame.encode("a", b"12345")
    decoder = frame.Decoder(limit=4)
    with pytest.raises(ProtocolError):
        decoder.feed(data)


def test_codec_registry_knows_both_protocols():
    assert codecs.available() == ["binary", "json"]
    assert codecs.decode("json", json_message.encode("k", {}, identifier="i")).kind == "k"
    assert codecs.decode("binary", frame.encode("k", b"x")).kind == "k"
    with pytest.raises(ProtocolError):
        codecs.get("xml")
    with pytest.raises(ProtocolError):
        codecs.register(codecs.get("json"))


def test_dispatch_routes_both_decoded_types():
    dispatch.register("p2-echo", lambda payload: payload)
    dispatch.register("p2-bytes", lambda payload: payload)
    message = json_message.decode(json_message.encode("p2-echo", {"a": 1}, identifier="i"))
    assert dispatch.dispatch(message) == {"a": 1}
    binary = frame.decode(frame.encode("p2-bytes", b"raw"))
    assert dispatch.dispatch(binary) == b"raw"
    with pytest.raises(ProtocolError):
        dispatch.register("p2-echo", lambda payload: payload)
    with pytest.raises(UnknownType):
        dispatch.dispatch(Message(2, "p2-missing", "i", {}, 0))
    kinds = dispatch.kinds()
    assert kinds == sorted(kinds)
    assert {"p2-echo", "p2-bytes"} <= set(kinds)


def test_duplicate_json_object_keys_are_rejected():
    data = b'{"v": 1, "type": "k", "body": {"a": 1, "a": 2}}'
    with pytest.raises(ProtocolError):
        json_message.decode(data)


def test_binary_v1_frames_still_decode():
    kind = b"old"
    header = b"ATL1" + bytes([1, len(kind)]) + kind + (3).to_bytes(4, "big")
    decoded = frame.decode(header + b"abc")
    assert (decoded.version, decoded.kind, decoded.payload, decoded.checksum) == (
        1,
        "old",
        b"abc",
        0,
    )


def test_selector_detects_both_protocols_across_a_split_magic():
    message = json_message.encode("sel-json", {"n": 1}, identifier="i")
    selector = Selector()
    assert selector.feed(b"\n" + message[:4]) == []
    received = selector.feed(message[4:])
    assert [item.kind for item in received] == ["sel-json"]
    assert selector.protocol == "json"
    selector.finish()

    binary = frame.encode("sel-bin", b"xyz")
    other = Selector()
    assert other.feed(b"ATL") == []
    received = other.feed(binary[3:])
    assert [item.payload for item in received] == [b"xyz"]
    assert other.protocol == "binary"
    other.finish()


def test_the_gateway_validates_before_dispatch():
    dispatch.register("greet", lambda payload: str(payload["name"]).upper())
    gateway = Gateway()
    assert gateway.feed(json_message.encode("greet", {"name": "atlas"}, identifier="i")) == [
        "ATLAS"
    ]
    with pytest.raises(InvalidPayload):
        gateway.feed(json_message.encode("greet", {"loud": True}, identifier="j"))
    with pytest.raises(InvalidPayload):
        gateway.feed(json_message.encode("greet", {"name": 3}, identifier="k"))
    gateway.finish()


def test_service_decodes_and_dispatches_a_stream():
    dispatch.register("p2-json", lambda payload: payload["n"])
    data = json_message.encode("p2-json", {"n": 7}, identifier="i")
    assert handle("json", data) == 7
    service = StreamingService("json")
    assert service.feed(data[:5]) == []
    assert service.feed(data[5:]) == [7]
    service.finish()
