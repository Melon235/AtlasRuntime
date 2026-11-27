# Configuration lifecycle — v2 API, contract revision 2

One configuration stack: a TOML-style document, an environment layer, an explicit layer, a
v1→v2 migration, strict validation and an immutable runtime view with provenance.

`configkit/errors.py` and `configkit/spec.py` are frozen. They are the shared semantic: every other
module must agree with them and may not edit them.

## Sources and precedence

Highest layer first: **explicit > environment > profile > file > default**. Every layer is flattened
to dotted paths (`retry.max`, `tls.cert`). A nested table in the file and a dotted key in the
environment address the same field.

- `configkit.document.parse(text: str) -> dict[str, object]` parses TOML into a flat mapping.
  An empty document is `{}`. An invalid document, or a list of tables, is a `ParseError`.
- `configkit.loader.read_env(env_values: Mapping[str, str]) -> dict[str, str]` maps
  `CONFIGKIT__RETRY__MAX` to `retry.max` (double underscore separates path segments; the key is
  lower-cased after the prefix). Match the case-sensitive prefix `CONFIGKIT__` exactly. Ignore
  non-matching keys regardless of their value. A matched key with an empty path, an empty path
  segment or a non-string value raises `ParseError` with `code="parse"` and `path` equal to
  the original environment key, preserving its prefix and case.
- `configkit.loader.load(file_text: str, env_values) -> dict` parses the file and overlays the
  environment mapping on top.

## Migration

`configkit.migrate.upgrade(document) -> dict` moves a v1 document to v2:

- The optional `version` metadata key must be the integer `1` or `2` (anything else is a
  `MigrationError`); a document without it is v1. `version` is consumed and not returned.
- v1 renames: `max_retries` → `retry.max`, `retry_delay` → `retry.delay`, `tls_cert` → `tls.cert`,
  `tls_key` → `tls.key`.
- Every key whose name starts with `legacy_` is dropped.
- Other keys are kept so that validation can reject them; already-v2 keys win over a migrated v1 key
  only if both cannot address the same path (a rename never overwrites an existing v2 key — the
  existing key is kept).
- The migration is idempotent.

## Profiles

`configkit.profile.PROFILES` holds these presets. A dash means the profile omits that field
and leaves the other layers to supply it. Lists are ordered.

| Path | dev | staging | prod |
|---|---|---|---|
| `log.level` | `debug` | `info` | `warning` |
| `logging.format` | `text` | `json` | `json` |
| `cache.enabled` | `false` | `true` | `true` |
| `cache.ttl` | — | `300.0` | `900.0` |
| `database.url` | `sqlite:///dev.db` | `postgresql://staging/app` | `postgresql://prod/app` |
| `database.pool` | `2` | `5` | `20` |
| `timeout` | `30.0` | — | `60.0` |
| `features` | — | `[cache, metrics]` | `[audit, cache, metrics, tracing]` |

`names()` returns sorted names. `load(name)` returns an independent copy, including list values.
An unknown profile raises `ProfileError` with `code="profile"` and `path` set to the requested
name. Apply `explicit > environment > profile > file > default` and record `profile` as the origin
when a preset supplies the winning value.

## Merge

`configkit.merge.resolve(defaults, file_values, env_values, explicit, profile_values=None) -> Resolved`
where `Resolved` has `values` and `origins`. `origins[key]` is one of `default`, `file`, `profile`,
`env`, `explicit`. Keys absent from every layer and from the defaults stay absent. Unknown keys
supplied by a layer are carried through so validation can reject them.

## Validation

`configkit.validate.check(values) -> dict` returns the full configuration:

- Missing fields take their `spec.FIELDS` default.
- Unknown paths are an `UnknownKey`.
- `str` requires a string and is stripped; non-empty fields reject `""`.
- `int` rejects `bool` and accepts an integer or a decimal string (`"12"`); it rejects `"12.5"` and
  `"12x"`. `float` rejects `bool` and non-finite values and accepts an int, float or numeric string.
- `bool` accepts a bool or the strings `"true"` / `"false"` (case-insensitive).
- `list` accepts a list/tuple of strings or a comma-separated string; elements are stripped, empty
  elements are dropped, order is preserved.
- `minimum` / `maximum` and `choices` come from the frozen spec. `features` accepts only values from
  `spec.FEATURE_FLAGS`.
- After field validation, if `tls.enabled` is true, report `ConstraintViolation` with path
  `tls.cert` when the cert is missing or empty, and otherwise with path `tls.key` when the
  key is missing or empty. Check the cert first when both are missing. When both are present
  the TLS constraint passes. When TLS is disabled, skip this cross-field check.
- `cache.enabled` requires the `cache` feature flag; `retry.delay * retry.max` must be at most
  `timeout`.
- Every error is a `ConfigError` with `code` and `path`:

| code | meaning |
|---|---|
| `parse` | the document or an environment key cannot be parsed |
| `migration` | the document version is not supported |
| `unknown_key` | a path is not in the spec |
| `type_mismatch` | a value cannot be coerced to its kind |
| `constraint` | a range, choice or cross-field rule failed |

## Runtime

`configkit.runtime.Settings.from_sources(*, file_text="", env_values=None, explicit=None, profile=None)`
runs parse → profile → env → migrate → merge → validate and keeps the origins.
`Settings.from_profile(name, **sources)` is the same call with `profile=name`.

- `settings[key]`, `settings.get(key, default)`, `settings.as_dict()` (sorted), `settings.explain(key)`
  returns the origin string. An unknown key in `explain` is a `KeyError`.
- Defaults come from the frozen spec; the runtime view is immutable.

## Explanation

- `configkit.explain.render(settings) -> str` is one `key=value [origin]` line per field, sorted by
  key. Lists are comma-joined and booleans render as `true` / `false`.
- `configkit.explain.as_env(settings) -> dict[str, str]` maps the effective configuration back to
  `CONFIGKIT__` variables, so loading those variables reproduces the same values.
- `configkit.explain.diff(left, right)` returns `{key: (left_value, right_value)}` for the keys whose
  effective values differ, sorted by key.
