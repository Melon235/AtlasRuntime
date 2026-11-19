# ReleaseGuard

Offline release readiness auditing.

A release is described by a JSON *release manifest* (name, version, channel, license, and the
artifacts that will be published). The artifacts themselves live in a build directory and are
described by their path, size and SHA-256 digest.

The project is split into three layers, in dependency order:

```text
releaseguard/
├── metadata/    manifest parsing, version rules, license rules   (no filesystem access)
├── artifacts/   artifact inventory, checksums, path safety       (no manifest access)
└── audit/       the shared release readiness audit               (uses both)
```

`metadata` and `artifacts` are independent by construction: neither imports the other, and neither
imports `audit`. Their public functions, defaults and error codes are depended on by other tools and
must keep working exactly as they do today.

`audit` is the shared layer. `audit/contracts.py` fixes the report vocabulary; `audit/report.py`
turns a set of checks into a report; `audit/service.py` runs the checks. The audit layer is the only
writable area in this repository.

## Layers

### metadata

* `manifest_parser.parse_manifest(path)` — read a manifest, apply documented defaults, refuse
  malformed input with a typed error. Paths are kept verbatim: validating them is the artifact
  layer's job.
* `version_rules` — version parsing/comparison and the release-channel rules.
* `license_rules` — SPDX normalization, deprecation and allow-list rules.

### artifacts

* `scanner.scan_artifacts(root)` — a deterministic inventory of a build directory: relative path,
  size, SHA-256, symlink flag. Entries that cannot be represented safely are refused and listed.
* `checksums` — SHA-256 helpers and digest normalization.
* `path_rules` — the single definition of a safe artifact path.

### audit

* `service.audit_release(manifest_path, artifact_root, policy=...)` — the shared workflow.
* `report.build_report(checks, ...)` / `report.render_report(report)` — report assembly and the
  machine-readable rendering.

## Running the tests

```bash
python3 -m pytest -q tests
```

`tests/test_metadata.py` and `tests/test_artifacts.py` cover the two subsystem layers and pass
today. `tests/test_public_audit.py` covers the shared audit workflow and does not pass yet: the
audit layer is incomplete.

## Sample data

`samples/release-manifest.json` describes a healthy release whose artifacts are in `samples/dist`.
The digests in the manifest are the digests of those files, so the sample is a working end-to-end
example once the audit layer is finished.
