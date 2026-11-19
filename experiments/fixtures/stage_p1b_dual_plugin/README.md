# eventpipe

`eventpipe` ingests events from two independent channels and is expected to expose one normalized
event to its consumers.

| Channel | Package | Transport | Native record |
|---|---|---|---|
| `file_events` | `plugins/file_events/` | JSON Lines document | `FileEventRecord` |
| `message_events` | `plugins/message_events/` | JSON document of messages | `MessageRecord` |

Both plugins are **vendored**: their modules, public functions, error codes, defaults and native
field names are a compatibility surface. Consumers already depend on `parse_document`,
`parse_line`, `coerce_level`, `split_tags`, `parse_timestamp`, `coerce_priority`,
`normalize_labels` and `stringify_payload` exactly as they behave today.

`core/` is the shared layer:

| Module | Responsibility |
|---|---|
| `core/contracts.py` | The unified event model every consumer sees: `NormalizedEvent`, `Severity`, `Plugin` |
| `core/errors.py` | The shared error vocabulary (`AdapterError` and subclasses) |
| `core/adapter.py` | Turns one channel-native mapping into a `NormalizedEvent` |
| `core/registry.py` | Registers the plugins and dispatches a payload to the right one |

The two channels do not share field names, timestamp units or severity vocabularies: every rule a
channel uses is defined inside that channel's own package, and the unified model lives in `core`.
`core/` is the only package that is expected to change; the plugin packages and `core/contracts.py`
describe behaviour that other code already relies on.

## Layout

```text
core/
  contracts.py   errors.py   adapter.py   registry.py
plugins/
  file_events/      schema.py  parser.py  converters.py  errors.py
  message_events/   schema.py  parser.py  converters.py  errors.py
samples/
  events.jsonl   messages.json
tests/
  test_file_plugin.py        native behaviour of the file channel
  test_message_plugin.py     native behaviour of the message channel
  test_public_contract.py    the unified contract
```

## Running the suite

```sh
python3 -B -m pytest -q tests
```

The plugin suites pass. The unified contract suite does not: the shared normalization path is
unfinished.
