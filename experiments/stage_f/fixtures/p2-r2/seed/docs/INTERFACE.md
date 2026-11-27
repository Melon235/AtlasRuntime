# Local wire protocols — v2 API, contract revision 2

Two independent local protocols share one codec registry and one dispatch table. No network is
involved: every codec works on `bytes` in-process.

`wire/errors.py` and `wire/model.py` are frozen support modules. They are already correct and must
not be edited.

## Errors

`ProtocolError` is the base type. `ChecksumError`, `VersionError`, `Truncated` and `UnknownType` are
its subclasses. Every rejection below names the most specific of them.

## Model

- `Message(version, kind, identifier, body, checksum)` — a decoded JSON message. `body` is a dict;
  `checksum` is `None` for a v1 message.
- `Frame(version, kind, payload, checksum)` — a decoded binary frame. `payload` is `bytes`.

## JSON message protocol — `wire/json_message.py`

One UTF-8 JSON object per line (`\n`). A trailing `\r` is accepted, so CRLF input works. Blank lines
are ignored by the stream decoder.

A v2 document is:

```
{"v": 2, "type": "<kind>", "id": "<identifier>", "body": {...}, "crc": <uint32>}
```

A v1 document is `{"v": 1, "type": "<kind>", "body": {...}}`. If `id` is absent, decode its
`identifier` as `""`. If present, preserve the string exactly, including `""` and whitespace.
A non-string `id` raises `ProtocolError`. A v1 message always has `checksum=None`.

- `encode(kind, body, *, identifier) -> bytes` always writes v2, compact, one trailing `\n`.
  `crc` is the CRC32 of
  `json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.
  A non-string or empty `kind`, a non-dict `body`, or a non-string or empty `identifier` is a
  `ProtocolError`.
- `decode(data: bytes) -> Message` accepts exactly one message (an optional trailing newline is
  allowed; any other second message is a `ProtocolError`).
  - invalid UTF-8, invalid JSON, a non-object root, a non-string `type`, a non-dict `body` is a
    `ProtocolError`.
  - `v` must be the integer `1` or `2` (a bool is not an integer); anything else is a
    `VersionError`.
  - A v2 message requires a non-empty string `id` and an integer `crc`; a different CRC is a
    `ChecksumError`.
- A duplicate object key anywhere in the document (top level or nested) is a `ProtocolError`.
- `Decoder(limit: int = 65536)` is the streaming decoder:
  - `feed(chunk: bytes) -> list[Message]` accepts any split of the input, keeps the incomplete tail
    and returns every message completed by this chunk. A line longer than `limit` before its newline
    is a `ProtocolError`.
  - `finish()` raises `Truncated` when a non-blank incomplete message is buffered, and clears the
    buffer.
  - `pending` is the number of buffered bytes.

## Binary frame protocol — `wire/frame.py`

v2 layout:

```
magic "ATL2" | version 0x02 | kind_len (1 byte) | kind (ASCII) | payload_len (4 bytes, big endian)
             | crc32 (4 bytes, big endian) | payload
```

v1 layout (read-only compatibility): magic `"ATL1"`, version `0x01`, the same `kind_len` / `kind` /
`payload_len` header and **no CRC**. A v1 frame decodes with `version=1` and `checksum=0`.
`encode` always writes v2.

- `kind` matches `[A-Za-z0-9_-]{1,32}`; `payload` is at most `1 MiB` (`MAX_PAYLOAD`).
- `encode(kind: str, payload: bytes) -> bytes` validates both and appends the CRC32 of the payload.
  An invalid kind, a non-bytes payload, an empty kind or an oversized payload is a
  `ProtocolError`.
- `decode(data: bytes) -> Frame` accepts exactly one complete frame.
  - a wrong magic, an invalid kind, or trailing bytes is a `ProtocolError`;
  - a version byte that does not match the magic is a `VersionError`;
  - fewer bytes than the header or payload declares is a `Truncated`;
  - an oversized declared payload is a `ProtocolError`;
  - a CRC mismatch is a `ChecksumError`.
- `Decoder(limit: int = MAX_PAYLOAD)` mirrors the JSON stream decoder: `feed(chunk) -> list[Frame]`
  accepts any split, `finish()` raises `Truncated` on a partial frame, `pending` is the buffer size.
  A declared payload larger than `limit` is a `ProtocolError` as soon as the length is readable.
  Both magics are accepted.

## `wire/checksum.py`

`crc32(data: bytes) -> int` is the standard CRC-32 (the same value `zlib.crc32` returns), and
`verify(data, expected)` raises `ChecksumError` when it differs. `crc32(b"123456789")` is
`0xCBF43926`.

## `wire/codecs.py`

- A codec has a `name`, `decode(data) -> Message | Frame` and `decoder(limit=None) -> Decoder`.
- `register(codec)` rejects a duplicate name with `ProtocolError` and does not overwrite.
- `get(name)` raises `ProtocolError` for an unknown name; `available()` is sorted.
- `decode(name, data)` decodes through the registered codec.
- Importing the module registers `json` and `binary`.

## `wire/dispatch.py`

- `register(kind, handler)` rejects a duplicate kind with `ProtocolError`; a handler is called with
  the decoded payload (`Message.body` or `Frame.payload`).
- `dispatch(decoded)` looks up `decoded.kind`, raises `UnknownType` when it is missing, and returns
  the handler result.
- `kinds()` is sorted.

## `wire/validation.py`

`PAYLOADS` declares the payload schema for `greet` (`name: str`, optional `loud: bool`), `echo`
(`message: str`) and `put` (`key: str`, `value: int`). `validate(kind, payload)`:

- a kind without a schema is not validated;
- a non-object payload is an `InvalidPayload`;
- a missing required key and a value of the wrong type are an `InvalidPayload` with the path
  `kind.key`; extra keys are allowed; a bool is never an int.

`InvalidPayload` is a `ProtocolError`.

## `wire/gateway.py`

`Gateway(codec_name=None, limit=None)` decodes, validates a JSON body and dispatches:

- with no codec name it auto-detects through `wire.selector.Selector`; with a name it owns that
  codec's decoder;
- `feed(chunk) -> list` returns the dispatched results for every completed value and accepts any
  split; binary frame payloads are dispatched without schema validation;
- `protocol` exposes the detected/declared protocol; `finish()` forwards to the decoder.

## `wire/service.py`

- `handle(codec_name, data)` decodes through `wire.codecs` and dispatches the result.
- `StreamingService(codec_name, limit=None)` owns one decoder; `feed(chunk)` returns the dispatched
  results for the messages completed by that chunk and `finish()` forwards to the decoder.

## `wire/selector.py`

`Selector(limit=None)` auto-detects which protocol a byte stream carries:

- Leading ASCII whitespace is skipped while detecting. A stream whose first non-whitespace byte is
  `{` is JSON; a stream whose first four non-whitespace bytes are `ATL1` or `ATL2` is binary.
- `feed(chunk) -> list[Message | Frame]` returns every value completed by the chunk and accepts any
  split, including a split inside the binary magic. A magic prefix that cannot become either magic,
  or any other first byte, is a `ProtocolError`.
- Once selected, the stream stays on that protocol; leading whitespace is only skipped for JSON.
- `finish()` raises `Truncated` when a value is incomplete and clears the buffer.
- `protocol` is `None` before detection, then `"json"` or `"binary"`.
