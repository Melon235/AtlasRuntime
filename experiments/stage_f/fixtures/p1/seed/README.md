# Connector ingest

Two input connectors (CSV files and newline-delimited JSON) feed one batching pipeline with a
resumable checkpoint and a per-schema total.

The project is migrating from the v1 handling to the v2 contract in `docs/INTERFACE.md`.
`ingest/errors.py`, `ingest/model.py`, `ingest/schemas.py` and `ingest/values.py` are frozen support
code. Run the public checks with `python -m pytest -q test_public.py`.

## Layout

- `ingest/csv_connector.py` — CSV rows, quoting, header projection, conversion.
- `ingest/jsonl_connector.py` — JSON lines, dotted mapping, conversion.
- `ingest/batching.py` — count- and byte-bounded batches.
- `ingest/checkpoint.py` — monotonic, atomically persisted cursors.
- `ingest/registry.py` — source registry.
- `ingest/pipeline.py` — read, batch, total, checkpoint.
- `ingest/report.py` — the rendered run summary.
