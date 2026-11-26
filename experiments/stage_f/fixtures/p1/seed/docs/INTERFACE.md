# Connector ingest — frozen v2 contract

Two source connectors (`csv`, `jsonl`) feed one pipeline. This document is the contract the
migration targets. It is public, it is frozen, and it is not edited while the migration runs.

The support modules `ingest/errors.py`, `ingest/model.py`, `ingest/schemas.py` and `ingest/values.py`
are part of the frozen surface: they are already correct and must not be edited.

## Records

`ingest.model.Record` is `Record(source: str, line: int, values: dict)`. `line` is the 1-based
physical line the record came from. `values` contains **every** field of the target schema; an
optional field that is absent from the input takes its schema default.

`ingest.model.ConnectorResult` is `ConnectorResult(records, cursor, skipped)`:

- `cursor` is the stage cursor: the 0-based data-row index of the first row that was **not**
  consumed. On a full read it equals `start + len(records)`.
- `skipped` counts input rows that were blank and therefore not treated as data.

`ingest.model.Schema` holds `FieldSpec(name, kind, required=False, default=None)`. A record whose
required field is missing raises `SchemaError`. Supported kinds are `text`, `int`, `float`, `bool`
and `date`; conversion is performed by the frozen `ingest.values.convert(kind, value, where=...)`.

## `ingest/csv_connector.py`

`read(raw: str, schema: Schema, *, start: int = 0, mapping=None) -> ConnectorResult`

- The first non-blank line is the header. Blank lines before the header are skipped.
- Header cells are trimmed and lower-cased. A duplicate header cell, an unknown header cell and a
  schema field that has no header cell are each a `SchemaError`.
- A header cell may not be empty (`SchemaError`).
- A data row is one non-blank line after the header. `start` is the number of data rows to skip
  before reading; a negative `start` is a `ValueError`.
- Cells are separated by commas outside double quotes. `"a,b"` is one cell, and `""` inside a
  quoted cell is a literal quote. An unbalanced quote is a `SchemaError`.
- A row whose cell count differs from the header count is a `SchemaError`.
- A cell is trimmed. An empty cell counts as absent: a required field raises `SchemaError`, an
  optional field takes its default.
- `ingest.values.convert` failures are a `ConversionError` naming the line and the field.
- `line` is the physical 1-based line number. Blank lines increment the physical line number but
  not the data-row index.

## `ingest/jsonl_connector.py`

`read(raw: str, schema: Schema, *, start: int = 0, mapping: dict[str, str] | None = None) -> ConnectorResult`

- One JSON object per non-blank line. Blank lines are skipped and counted in `skipped`.
- `start` counts non-blank JSON lines; a negative `start` is a `ValueError`.
- `mapping` maps a schema field name to a dotted path in the payload (`{"amount": "totals.amount"}`).
  A field without a mapping entry uses its own name as the key. Extra payload keys are ignored.
- A missing intermediate object along a dotted path, a missing required field, an invalid JSON
  document or a non-object root is a `SchemaError`.
- The resolved value may be a JSON string, number or boolean; it is passed to
  `ingest.values.convert` (so `3` becomes the text `"3"` for a `text` field and `true` becomes the
  bool `True` for a `bool` field).
- A conversion failure is a `ConversionError` naming the line and the field.

## `ingest/batching.py`

`build_batches(records, max_records: int = 100, max_bytes: int = 4096) -> list[Batch]`

- Batches are contiguous and indexed from 0. A record is never split across batches.
- `max_records` and `max_bytes` must be positive (`ValueError`).
- A record's size is the UTF-8 length of
  `json.dumps({"source": source, "line": line, "values": values}, sort_keys=True, separators=(",", ":"))`.
- A batch is flushed before adding a record that would exceed either limit. A single record larger
  than `max_bytes` forms its own batch.
- Empty input yields no batches; no batch is empty.

## `ingest/checkpoint.py`

`CheckpointStore(path)` persists cursors to one JSON document:

```
{"version": 1, "cursors": {"<source>": <int>}}
```

- `load(source) -> int` returns 0 for an unknown source.
- `save(source, cursor)` is monotonic per source: a cursor lower than the stored one is a
  `CheckpointError` and must leave the stored value unchanged. An equal cursor is accepted and
  changes nothing. A negative cursor is a `CheckpointError`.
- `snapshot() -> dict[str, int]` returns a copy sorted by source.
- A missing file is an empty store. A malformed document, a wrong version, a non-integer cursor or a
  non-object `cursors` value is a `CheckpointError`.
- `save` writes atomically: a temporary file in the same directory, then `os.replace`.

## `ingest/registry.py`

- `register(name: str, factory)` registers a connector factory. A duplicate name is a `ValueError`
  and does not overwrite the existing entry.
- `build(name: str)` returns the factory; an unknown name is a `KeyError`.
- `available() -> list[str]` is sorted.
- Importing the module registers `csv` and `jsonl`.

## `ingest/pipeline.py`

`run(source, raw, schema, *, start=None, store=None, max_records=100, max_bytes=4096, mapping=None) -> RunResult`

- Resolves the connector through the registry, then reads with the effective start: an explicit
  `start` wins; otherwise the checkpoint store's value for `source`; otherwise 0.
- Builds batches with both limits and computes `totals`: for every `int`/`float` schema field, the
  sum of the values in the records. A missing or `None` value contributes 0. Keys are the schema
  field names.
- Saves the checkpoint after a fully successful read only; a connector failure leaves the checkpoint
  untouched.
- `RunResult` carries `source`, `records`, `batches`, `cursor`, `skipped` and `totals`.

## `ingest/report.py`

`render(result) -> str` renders one line per entry, in this order:

```
source=<source>
records=<n>
batches=<n>
cursor=<n>
skipped=<n>
total.<field>=<value>
```

`total.` lines are sorted by field name and use `str(value)`. There is no trailing newline.
