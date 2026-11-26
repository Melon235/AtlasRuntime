# Configuration kit

A configuration stack with a TOML document, an environment layer, an explicit layer, a v1→v2
migration, strict validation and a runtime `Settings` view with provenance.

`docs/INTERFACE.md` is the frozen v2 contract; `configkit/errors.py` and `configkit/spec.py` are
frozen. The rest of the package is still v1.

Run the public checks with `python -m pytest -q test_public.py`.

## Layout

- `configkit/spec.py` — the only field table.
- `configkit/document.py` — TOML parsing and flattening.
- `configkit/loader.py` — file and environment sources.
- `configkit/migrate.py` — v1 → v2.
- `configkit/merge.py` — precedence and origins.
- `configkit/validate.py` — coercion, ranges, choices and cross-field rules.
- `configkit/runtime.py` — the immutable view.
