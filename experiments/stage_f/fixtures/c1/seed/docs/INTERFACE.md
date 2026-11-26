# Configuration lifecycle — frozen v2 contract

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
  lower-cased). Other environment keys are ignored; the prefix is exactly `CONFIGKIT__`.
  An empty path after the prefix is a `ParseError`.
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

`configkit.profile.PROFILES` holds the `dev`, `staging` and `prod` presets; `names()` is sorted and
`load(name)` returns a copy. An unknown profile is a `ProfileError` with the requested name as its
path. A profile is a layer between the file and the environment, so a file value beats the default,
the profile beats the file, and the environment and explicit layers beat the profile.

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
- Cross-field constraints: `tls.enabled` requires non-empty `tls.cert` and `tls.key`;
  `cache.enabled` requires the `cache` feature flag; `retry.delay * retry.max` must be at most
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
