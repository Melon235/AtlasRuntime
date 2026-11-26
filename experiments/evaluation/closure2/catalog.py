# ruff: noqa: E501
"""Closure-2A: the frozen final benchmark tasks.

Three tasks, each a real migration/refactor over an existing working v1 codebase:

* **P1** ``closure2-pipeline-migration`` — a multi-module ingest pipeline must move to a frozen v2
  contract. Two independent module owners (parsing/normalisation, aggregation/store) plus one
  integration owner (public API/report). Parallel wave width 2, three workers.
* **P2** ``closure2-plugin-migration`` — two independent plugins migrate to one frozen plugin
  interface, then a third worker owns registration, dispatch and the integration test surface.
  Same shape as P1 but a different problem: a published interface contract, per-plugin edge cases.
* **N1** ``closure2-state-machine-refactor`` — one shared lifecycle state machine plus its callers.
  Deliberately **not** separable: both candidate steps must edit ``lifecycle/state.py``. It is the
  offline negative control for the delegation router.

Every task is authored as data:

``seed``        the v1 repository the agent receives (public)
``reference``   a working v2 implementation, used only to prove the oracle is satisfiable
``public_tests`` ``test_public.py`` — visible, and part of the fixture
``oracle_tests`` ``test_hidden_*.py`` — never staged into the agent workspace

**Release boundary.** The reference implementations and the hidden tests are committed to this
repository, so these are *not* held-out tasks in the secrecy sense. What is held out is the agent
workspace: the sandbox only ever receives ``seed``. That boundary is recorded in
``docs/closure-2-preregistration.md`` §2 and must stay attached to every result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from experiments.evaluation.models import (
    BenchmarkTask,
    WorkerStep,
    canonical_digest,
    effective_public_task_digest,
)

#: New benchmark identity. The Final-4 ids and results are untouched.
BENCHMARK_ID = "closure2-final-benchmark-v1"
DATASET_VERSION = "closure2-final-benchmark-v1"
BENCHMARK_VERSION = "closure2-final-benchmark-v1"

Topology = Literal["single", "multi"]

SHARED_HEADER = "[SHARED TASK CONTEXT — identical for every worker on this task]"


@dataclass(frozen=True)
class Task:
    """One frozen benchmark task."""

    task_id: str
    family: str
    title: str
    prompt: str
    seed: dict[str, str]
    reference: dict[str, str]
    public_tests: dict[str, str]
    oracle_tests: dict[str, str]
    workers: tuple[dict[str, Any], ...]
    deliverables: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    shared_context: str = ""
    frozen_contracts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: str = ""
    research_question: str = ""

    @property
    def write_paths(self) -> dict[str, tuple[str, ...]]:
        return {str(worker["task_id"]): tuple(worker["owned_paths"]) for worker in self.workers}


def _shared(requirement: str, interface: str, constraints: str, acceptance: str) -> str:
    """The block every worker of the sample receives, in one fixed shape."""

    return "\n".join(
        [
            SHARED_HEADER,
            "",
            "PUBLIC REQUIREMENT",
            requirement,
            "",
            "PUBLIC INTERFACE CONTRACT",
            interface,
            "",
            "DATA AND COMPATIBILITY CONSTRAINTS",
            constraints,
            "",
            "PUBLIC ACCEPTANCE",
            acceptance,
            "",
            "WORKSPACE RULES",
            "- The project is at `/workspace/project`. Read any project file you need.",
            "- Do not search hidden tests, scoring output, evaluation artifacts or Git history for a "
            "reference implementation. They are not part of this task.",
            "- Report the files you changed, the commands you ran and their real output.",
        ]
    )


# ====================================================================================== P1


P1_REQUIREMENT = (
    "The ingest pipeline is being moved to the v2 record contract. `pipeline/parsing.py` must "
    "produce validated records, `pipeline/normalise.py` must add the numeric field, "
    "`pipeline/aggregate.py` must sum those fields, `pipeline/store.py` must keep one record per "
    "name, `pipeline/api.py` must compose the whole path and `pipeline/report.py` must render it. "
    "The old v1 shape (`name`, `value`) stays the record shape; v2 adds `count` and fixes ordering, "
    "validation and duplicate handling. No public call site may lose behaviour it has today."
)
P1_INTERFACE = (
    "- `pipeline.parsing.parse_record(line: str) -> dict` with keys `name`, `value`\n"
    "- `pipeline.parsing.parse_records(lines: Iterable[str]) -> list[dict]`\n"
    "- `pipeline.normalise.normalise(record) -> dict` with keys `name`, `value`, `count`\n"
    "- `pipeline.normalise.normalise_all(records) -> list[dict]`\n"
    "- `pipeline.aggregate.aggregate(records) -> list[tuple[str, int]]` sorted by name\n"
    "- `pipeline.store.Store` with `add(record)` and `all() -> list[dict]`\n"
    "- `pipeline.api.build_report(lines) -> dict[str, int]`\n"
    "- `pipeline.report.render(report) -> str`"
)
P1_CONSTRAINTS = (
    "- A record line is `name=value`. Surrounding whitespace is not part of either field.\n"
    "- Field names are lower-cased; field values keep their case.\n"
    "- Blank lines are skipped, not rejected.\n"
    "- A line without exactly one `=`, or with an empty field name, is a `ValueError`.\n"
    "- `count` is `int(value)` when the value is all digits, otherwise `0`.\n"
    "- A record whose value is empty after trimming is dropped by `normalise_all`.\n"
    "- `aggregate` sums `count`, sorts by name, and never depends on input order.\n"
    "- `Store.add` replaces the record with the same name (last write wins).\n"
    "- `Store.all` and `render` are sorted by name; `render({})` is the empty string.\n"
    "- `build_report` propagates `ValueError` from parsing unchanged."
)
P1_ACCEPTANCE = (
    "- `test_public.py` is present in the project and must pass; run it with "
    "`python -m pytest -q test_public.py`.\n"
    "- Do not edit `test_public.py`. Hidden tests exist and are not visible; they check the same "
    "contract more strictly.\n"
    "- `docs/INTERFACE.md` is the frozen contract and must not be edited."
)

P1_SHARED = _shared(P1_REQUIREMENT, P1_INTERFACE, P1_CONSTRAINTS, P1_ACCEPTANCE)

P1_SEED: dict[str, str] = {
    "pipeline/__init__.py": '"""Ingest pipeline package."""\n',
    "pipeline/parsing.py": '''"""Record parsing for the ingest pipeline (v1)."""


def parse_record(line):
    name, _, value = line.partition("=")
    return {"name": name, "value": value}


def parse_records(lines):
    return [parse_record(line) for line in lines if line]
''',
    "pipeline/normalise.py": '''"""Field normalisation (v1)."""


def normalise(record):
    return record


def normalise_all(records):
    return [normalise(record) for record in records]
''',
    "pipeline/aggregate.py": '''"""Aggregation helpers (v1)."""


def aggregate(records):
    counts = {}
    for record in records:
        name = record["name"]
        counts[name] = counts.get(name, 0) + 1
    return list(counts.items())
''',
    "pipeline/store.py": '''"""In-memory record store (v1)."""


class Store:
    def __init__(self):
        self._records = []

    def add(self, record):
        self._records.append(record)

    def all(self):
        return list(self._records)
''',
    "pipeline/api.py": '''"""Public report API (v1)."""

from pipeline.aggregate import aggregate
from pipeline.parsing import parse_records


def build_report(lines):
    return dict(aggregate(parse_records(lines)))
''',
    "pipeline/report.py": '''"""Report rendering (v1)."""


def render(report):
    return "\\n".join(f"{name}={total}" for name, total in report.items())
''',
    "docs/INTERFACE.md": f"""# Ingest pipeline — frozen v2 contract

This document is the contract the migration targets. It is public, it is frozen, and it is not
edited while the migration runs.

## Record shape

```
{{"name": str, "value": str}}            # v1, still the parsed shape
{{"name": str, "value": str, "count": int}}  # v2, after normalisation
```

## Field rules

- `name` is trimmed and lower-cased; `value` is trimmed and keeps its case.
- A line is `name=value` with exactly one `=`. Blank lines are skipped.
- No `=`, more than one `=`, or an empty name is a `ValueError`.
- `count = int(value)` if `value.isdigit()` else `0`.
- `normalise_all` drops records whose value is empty after trimming.

## Ordering

- `aggregate` sums `count` per name and returns a list of `(name, total)` sorted by name.
- `Store` keeps one record per name; `Store.add` replaces (last write wins).
- `Store.all` and `render` are sorted by name. `render({{}})` is `""`.

## Compatibility

- `build_report` keeps its signature and propagates `ValueError` from parsing.
- `parse_records` still accepts a list of raw lines.

{P1_INTERFACE}
""",
    "README.md": """# Ingest pipeline

A small record pipeline: parse `name=value` lines, normalise the fields, aggregate the counts and
render a report.

The project is migrating from the v1 record handling to the v2 contract in `docs/INTERFACE.md`.
Run the public checks with `python -m pytest -q test_public.py`.
""",
    "test_public.py": '''"""Public checks for the v2 ingest contract."""

import pytest

from pipeline.aggregate import aggregate
from pipeline.api import build_report
from pipeline.normalise import normalise, normalise_all
from pipeline.parsing import parse_record, parse_records
from pipeline.report import render
from pipeline.store import Store


def test_parse_trims_and_lowercases_the_name():
    assert parse_record("  Count = 3 ") == {"name": "count", "value": "3"}


def test_parse_skips_blank_lines():
    assert parse_records(["a=1", "", "b=2"]) == [
        {"name": "a", "value": "1"},
        {"name": "b", "value": "2"},
    ]


def test_parse_rejects_a_line_without_a_separator():
    with pytest.raises(ValueError):
        parse_record("novalue")


def test_normalise_adds_the_count():
    assert normalise({"name": "a", "value": "7"}) == {"name": "a", "value": "7", "count": 7}


def test_normalise_all_drops_empty_values():
    records = [{"name": "a", "value": "2"}, {"name": "b", "value": ""}]
    assert [record["name"] for record in normalise_all(records)] == ["a"]


def test_aggregate_sums_and_sorts():
    records = [
        {"name": "b", "value": "1", "count": 1},
        {"name": "a", "value": "2", "count": 2},
        {"name": "a", "value": "3", "count": 3},
    ]
    assert aggregate(records) == [("a", 5), ("b", 1)]


def test_store_keeps_the_last_record_per_name():
    store = Store()
    store.add({"name": "b", "value": "1"})
    store.add({"name": "a", "value": "2"})
    store.add({"name": "b", "value": "3"})
    assert [record["value"] for record in store.all()] == ["2", "3"]


def test_build_report_runs_the_whole_path():
    assert build_report(["a=2", "b=1", "a=3"]) == {"a": 5, "b": 1}


def test_render_is_sorted_and_has_no_trailing_newline():
    assert render({"b": 1, "a": 2}) == "a=2\\nb=1"
''',
}

P1_REFERENCE: dict[str, str] = {
    "pipeline/parsing.py": '''"""Record parsing for the ingest pipeline (v2)."""


def parse_record(line):
    text = line.strip()
    if not text:
        raise ValueError("empty record line")
    if text.count("=") != 1:
        raise ValueError(f"expected exactly one '=' in {line!r}")
    name, _, value = text.partition("=")
    name = name.strip().lower()
    if not name:
        raise ValueError(f"empty field name in {line!r}")
    return {"name": name, "value": value.strip()}


def parse_records(lines):
    return [parse_record(line) for line in lines if line.strip()]
''',
    "pipeline/normalise.py": '''"""Field normalisation (v2)."""


def normalise(record):
    value = record["value"]
    count = int(value) if value.isdigit() else 0
    return {"name": record["name"], "value": value, "count": count}


def normalise_all(records):
    normalised = []
    for record in records:
        value = record["value"].strip()
        if not value:
            continue
        normalised.append(normalise({**record, "value": value}))
    return normalised
''',
    "pipeline/aggregate.py": '''"""Aggregation helpers (v2)."""


def aggregate(records):
    totals = {}
    for record in records:
        name = record["name"]
        totals[name] = totals.get(name, 0) + record["count"]
    return sorted(totals.items())
''',
    "pipeline/store.py": '''"""In-memory record store (v2)."""


class Store:
    def __init__(self):
        self._records = {}

    def add(self, record):
        self._records[record["name"]] = record
        return record

    def all(self):
        return [self._records[name] for name in sorted(self._records)]
''',
    "pipeline/api.py": '''"""Public report API (v2)."""

from pipeline.aggregate import aggregate
from pipeline.normalise import normalise_all
from pipeline.parsing import parse_records


def build_report(lines):
    return dict(aggregate(normalise_all(parse_records(lines))))
''',
    "pipeline/report.py": '''"""Report rendering (v2)."""


def render(report):
    return "\\n".join(f"{name}={report[name]}" for name in sorted(report))
''',
}

P1_ORACLE: dict[str, str] = {
    "test_hidden_pipeline.py": '''"""Hidden checks for the v2 ingest contract (never staged into the agent workspace)."""

import pytest

from pipeline.aggregate import aggregate
from pipeline.api import build_report
from pipeline.normalise import normalise, normalise_all
from pipeline.parsing import parse_record, parse_records
from pipeline.report import render
from pipeline.store import Store


def test_more_than_one_separator_is_rejected():
    with pytest.raises(ValueError):
        parse_record("a=1=2")


def test_an_empty_field_name_is_rejected():
    with pytest.raises(ValueError):
        parse_record("   = 1")


def test_the_value_keeps_its_case_but_loses_whitespace():
    assert parse_record("Name = MixedCase ")[\"value\"] == "MixedCase"


def test_a_non_numeric_value_counts_as_zero():
    assert normalise({"name": "a", "value": "n/a"})["count"] == 0


def test_normalise_all_drops_a_whitespace_only_value():
    records = [{"name": "a", "value": "  "}, {"name": "b", "value": "1"}]
    assert [record["name"] for record in normalise_all(records)] == ["b"]


def test_aggregate_is_order_independent():
    left = [{"name": "a", "value": "1", "count": 1}, {"name": "b", "value": "2", "count": 2}]
    right = list(reversed(left))
    assert aggregate(left) == aggregate(right) == [("a", 1), ("b", 2)]


def test_store_returns_one_record_per_name_in_name_order():
    store = Store()
    store.add({"name": "z", "value": "1"})
    store.add({"name": "a", "value": "2"})
    store.add({"name": "z", "value": "3"})
    assert [(record["name"], record["value"]) for record in store.all()] == [("a", "2"), ("z", "3")]


def test_build_report_propagates_a_parse_error():
    with pytest.raises(ValueError):
        build_report(["a=1", "broken"])


def test_build_report_on_empty_input():
    assert build_report([]) == {}


def test_render_of_an_empty_report_is_empty():
    assert render({}) == ""


def test_the_public_api_still_accepts_a_plain_list_of_lines():
    assert build_report(["A=2", "a=3"]) == {"a": 5}
''',
}

# ====================================================================================== P2


P2_REQUIREMENT = (
    "The plugin system is moving to the frozen v2 plugin interface in `plugins/base.py`. Two "
    "concrete plugins (`csv` and `json`) still implement the v1 `parse`-only shape and must migrate; "
    "the registry and the loader still call v1 and must be brought onto the same contract. The two "
    "plugins have genuinely different inputs — CSV rows with quoting versus nested JSON objects — so "
    "each has its own parsing edge cases, but both must return the same record shape."
)
P2_INTERFACE = (
    "- `plugins.base.Plugin` with `name`, `supports(source) -> bool` and "
    "`transform(record, options=None) -> dict`\n"
    "- `plugins.base.PluginError` for every rejected input\n"
    "- `plugins.csv_plugin.CsvPlugin`, `plugins.json_plugin.JsonPlugin`\n"
    "- `plugins.registry.register(plugin)`, `plugins.registry.get(name)`\n"
    "- `plugins.loader.load(payload, source) -> list[dict]`"
)
P2_CONSTRAINTS = (
    "- `supports(source)` is True only for that plugin's own source name.\n"
    "- `transform` returns `{'key': str, 'value': str}` with both sides trimmed; with "
    "`{'lowercase': True}` the key is also lower-cased.\n"
    "- CSV: one `key,value` row per line, blank lines skipped; a comma inside double quotes is part "
    "of the value; a row with no `key,value` separator is a `PluginError`.\n"
    "- JSON: the payload is one object; nested objects are flattened with dotted keys; numbers and "
    "booleans become their string form; invalid JSON or a non-object root is a `PluginError`.\n"
    "- `register` rejects a duplicate name with `PluginError` and does not overwrite.\n"
    "- `get` raises `PluginError` for an unknown name.\n"
    "- `load` registers each built-in plugin once on first use (never overwriting an existing "
    "registration), then picks the first registered plugin whose `supports` matches, parses, and "
    "transforms each record; no supporting plugin is a `PluginError`.\n"
    "- `plugins/base.py` is frozen and must not be edited."
)
P2_ACCEPTANCE = (
    "- `test_public.py` is present in the project and must pass; run it with "
    "`python -m pytest -q test_public.py`.\n"
    "- Do not edit `test_public.py`. Hidden tests exist and are not visible; they check the same "
    "contract more strictly.\n"
    "- `docs/INTERFACE.md` is the frozen contract and must not be edited."
)

P2_SHARED = _shared(P2_REQUIREMENT, P2_INTERFACE, P2_CONSTRAINTS, P2_ACCEPTANCE)

P2_SEED: dict[str, str] = {
    "plugins/__init__.py": '"""Plugin package."""\n',
    "plugins/base.py": '''"""Frozen plugin contract (v2).

This module is the published interface. It is not edited while the migration runs.
"""


class PluginError(Exception):
    """Raised by a plugin or the registry when an input cannot be handled."""


class Plugin:
    name = ""

    def supports(self, source):
        """Return True when this plugin handles ``source``."""
        raise NotImplementedError

    def transform(self, record, options=None):
        """Return ``{"key": str, "value": str}`` for one parsed record."""
        raise NotImplementedError
''',
    "plugins/csv_fields.py": '''"""CSV field helpers (v1)."""


def split_cells(line):
    return line.split(",")
''',
    "plugins/csv_plugin.py": '''"""CSV plugin (v1)."""

from plugins.base import Plugin
from plugins.csv_fields import split_cells


class CsvPlugin(Plugin):
    name = "csv"

    def parse(self, payload):
        rows = []
        for line in payload.splitlines():
            if not line:
                continue
            cells = split_cells(line)
            rows.append({"key": cells[0], "value": cells[1]})
        return rows
''',
    "plugins/json_fields.py": '''"""JSON field helpers (v1)."""


def flatten(data):
    return dict(data)
''',
    "plugins/json_plugin.py": '''"""JSON plugin (v1)."""

import json

from plugins.base import Plugin
from plugins.json_fields import flatten


class JsonPlugin(Plugin):
    name = "json"

    def parse(self, payload):
        data = flatten(json.loads(payload))
        return [{"key": key, "value": value} for key, value in data.items()]
''',
    "plugins/registry.py": '''"""Plugin registry (v1)."""

PLUGINS = {}


def register(plugin):
    PLUGINS[plugin.name] = plugin
    return plugin


def get(name):
    return PLUGINS[name]
''',
    "plugins/loader.py": '''"""Plugin loader (v1)."""

from plugins.registry import PLUGINS


def load(payload, source):
    for plugin in PLUGINS.values():
        if hasattr(plugin, "parse"):
            return plugin.parse(payload)
    return []
''',
    "docs/INTERFACE.md": f"""# Plugin system — frozen v2 interface

`plugins/base.py` is the published contract. It is already v2 and is not edited during the
migration; the two plugins and the registry/loader still implement v1 and must be moved onto it.

{P2_INTERFACE}

## Parsing differences are real

| | `csv` | `json` |
|---|---|---|
| source | one `key,value` row per line | one object |
| quoting | `"a,b"` is a single value | n/a |
| nesting | none | nested objects become `a.b` keys |
| bad input | a row with no separator | invalid JSON, or a non-object root |

Both plugins return the same record shape, and every rejection is a `PluginError`.

{P2_CONSTRAINTS}
""",
    "README.md": """# Plugin system

Two plugins (`csv`, `json`) registered in a small registry and loaded by a shared loader.

The system is migrating from the v1 `parse`-only plugin shape to the v2 interface in
`plugins/base.py`. Run the public checks with `python -m pytest -q test_public.py`.
""",
    "test_public.py": '''"""Public checks for the v2 plugin interface."""

import pytest

from plugins.base import Plugin, PluginError
from plugins.csv_plugin import CsvPlugin
from plugins.json_plugin import JsonPlugin
from plugins.loader import load
from plugins.registry import get, register


def test_csv_supports_only_its_own_source():
    plugin = CsvPlugin()
    assert plugin.supports("csv") is True
    assert plugin.supports("json") is False


def test_csv_accepts_a_quoted_comma():
    assert CsvPlugin().parse('a,"x,y"') == [{"key": "a", "value": "x,y"}]


def test_csv_rejects_a_row_without_a_separator():
    with pytest.raises(PluginError):
        CsvPlugin().parse("lonely")


def test_json_flattens_nested_objects():
    rows = JsonPlugin().parse('{"a": {"b": 1}}')
    assert rows == [{"key": "a.b", "value": "1"}]


def test_json_rejects_invalid_payloads():
    with pytest.raises(PluginError):
        JsonPlugin().parse("{not json")


def test_transform_trims_and_optionally_lowercases():
    plugin = CsvPlugin()
    assert plugin.transform({"key": " A ", "value": " v "}) == {"key": "A", "value": "v"}
    assert plugin.transform({"key": " A ", "value": " v "}, {"lowercase": True}) == {
        "key": "a",
        "value": "v",
    }


def test_registry_rejects_a_duplicate_name():
    class Probe(Plugin):
        name = "probe-public"

        def supports(self, source):
            return False

        def transform(self, record, options=None):
            return {"key": record["key"], "value": record["value"]}

    register(Probe())
    with pytest.raises(PluginError):
        register(Probe())


def test_loader_dispatches_by_source():
    assert load("a,1", "csv") == [{"key": "a", "value": "1"}]
    assert load('{"a": 1}', "json") == [{"key": "a", "value": "1"}]


def test_loader_rejects_an_unsupported_source():
    with pytest.raises(PluginError):
        load("a,1", "xml")
''',
}

P2_REFERENCE: dict[str, str] = {
    "plugins/csv_fields.py": '''"""CSV field helpers (v2)."""


def split_first_unquoted(line):
    """Split at the first comma that is not inside double quotes, or ``None``."""

    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            return line[:index], line[index + 1 :]
    return None


def unquote(cell):
    text = cell.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text
''',
    "plugins/csv_plugin.py": '''"""CSV plugin (v2)."""

from plugins.base import Plugin, PluginError
from plugins.csv_fields import split_first_unquoted, unquote


class CsvPlugin(Plugin):
    name = "csv"
    sources = ("csv",)

    def supports(self, source):
        return source in self.sources

    def parse(self, payload):
        rows = []
        for line in payload.splitlines():
            if not line.strip():
                continue
            cells = split_first_unquoted(line)
            if cells is None:
                raise PluginError(f"csv row has no key,value separator: {line!r}")
            rows.append({"key": unquote(cells[0]), "value": unquote(cells[1])})
        return rows

    def transform(self, record, options=None):
        options = options or {}
        key = str(record["key"]).strip()
        value = str(record["value"]).strip()
        if options.get("lowercase"):
            key = key.lower()
        return {"key": key, "value": value}
''',
    "plugins/json_fields.py": '''"""JSON field helpers (v2)."""

from typing import Any


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, prefix=f"{name}."))
        else:
            flat[name] = _as_text(value)
    return flat


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
''',
    "plugins/json_plugin.py": '''"""JSON plugin (v2)."""

import json

from plugins.base import Plugin, PluginError
from plugins.json_fields import flatten


class JsonPlugin(Plugin):
    name = "json"
    sources = ("json",)

    def supports(self, source):
        return source in self.sources

    def parse(self, payload):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            raise PluginError(f"json payload is not valid json: {error}") from error
        if not isinstance(data, dict):
            raise PluginError("json payload root must be an object")
        return [{"key": key, "value": value} for key, value in flatten(data).items()]

    def transform(self, record, options=None):
        options = options or {}
        key = str(record["key"]).strip()
        value = str(record["value"]).strip()
        if options.get("lowercase"):
            key = key.lower()
        return {"key": key, "value": value}
''',
    "plugins/registry.py": '''"""Plugin registry (v2)."""

from plugins.base import PluginError

PLUGINS = {}


def register(plugin):
    if plugin.name in PLUGINS:
        raise PluginError(f"plugin {plugin.name!r} is already registered")
    PLUGINS[plugin.name] = plugin
    return plugin


def get(name):
    if name not in PLUGINS:
        raise PluginError(f"unknown plugin {name!r}")
    return PLUGINS[name]
''',
    "plugins/loader.py": '''"""Plugin loader (v2)."""

from plugins.base import PluginError
from plugins.csv_plugin import CsvPlugin
from plugins.json_plugin import JsonPlugin
from plugins.registry import PLUGINS, register


def _ensure_defaults():
    """Register each built-in plugin once; a plugin already present is left alone."""

    for plugin in (CsvPlugin(), JsonPlugin()):
        if plugin.name not in PLUGINS:
            register(plugin)


def load(payload, source):
    _ensure_defaults()
    for plugin in PLUGINS.values():
        if plugin.supports(source):
            return [plugin.transform(record) for record in plugin.parse(payload)]
    raise PluginError(f"no plugin supports source {source!r}")
''',
}

P2_ORACLE: dict[str, str] = {
    "test_hidden_plugins.py": '''"""Hidden checks for the v2 plugin interface (never staged into the agent workspace)."""

import pytest

from plugins.base import Plugin, PluginError
from plugins.csv_plugin import CsvPlugin
from plugins.json_plugin import JsonPlugin
from plugins.loader import load
from plugins.registry import PLUGINS, get, register


def test_a_quoted_key_is_one_cell():
    assert CsvPlugin().parse('"a,b",1') == [{"key": "a,b", "value": "1"}]


def test_blank_csv_lines_are_skipped():
    assert CsvPlugin().parse("a,1\\n\\nb,2\\n") == [
        {"key": "a", "value": "1"},
        {"key": "b", "value": "2"},
    ]


def test_an_empty_csv_payload_is_an_empty_list():
    assert CsvPlugin().parse("") == []


def test_json_numbers_and_booleans_become_text():
    rows = JsonPlugin().parse('{"n": 3, "ok": true}')
    assert rows == [{"key": "n", "value": "3"}, {"key": "ok", "value": "true"}]


def test_json_flattens_two_levels():
    rows = JsonPlugin().parse('{"a": {"b": {"c": 1}}}')
    assert rows == [{"key": "a.b.c", "value": "1"}]


def test_json_rejects_a_list_root():
    with pytest.raises(PluginError):
        JsonPlugin().parse("[1, 2]")


def test_an_unknown_plugin_name_is_rejected():
    with pytest.raises(PluginError):
        get("nope")


def test_a_duplicate_registration_does_not_overwrite():
    class Probe(Plugin):
        name = "probe-hidden"

        def supports(self, source):
            return False

        def transform(self, record, options=None):
            return {"key": record["key"], "value": record["value"]}

    first = register(Probe())
    with pytest.raises(PluginError):
        register(Probe())
    assert get("probe-hidden") is first


def test_the_loader_applies_transform():
    assert load(" A , 1 ", "csv") == [{"key": "A", "value": "1"}]


def test_the_loader_rejects_a_payload_the_plugin_cannot_parse():
    with pytest.raises(PluginError):
        load("no-separator", "csv")


def test_both_plugins_return_the_same_shape():
    csv_rows = load("a,1", "csv")
    json_rows = load('{"a": "1"}', "json")
    assert csv_rows == json_rows == [{"key": "a", "value": "1"}]
    assert all(sorted(row) == ["key", "value"] for row in [*csv_rows, *json_rows])


def test_the_registry_is_shared_across_a_load():
    load("a,1", "csv")
    assert set(PLUGINS) >= {"csv", "json"}
''',
}

# ====================================================================================== N1


N1_REQUIREMENT = (
    "The order lifecycle state machine in `lifecycle/state.py` must grow two terminal states "
    "(`rejected`, `cancelled`) and the events that reach them, and every caller must be moved onto "
    "the same table. Approvals, shipping and the audit trail all read the same `STATES` mapping, so "
    "the machine and its callers are one change: a caller that hard-codes a transition the table "
    "does not contain is a defect, and an illegal transition must be rejected rather than raising "
    "`KeyError`."
)
N1_INTERFACE = (
    "- `lifecycle.state.STATES: dict[str, dict[str, str]]`\n"
    "- `lifecycle.state.transition(state: str, event: str) -> str`, raising `ValueError` for an "
    "unknown state or an illegal event\n"
    "- `lifecycle.approval.approve(state)`, `lifecycle.approval.reject(state)`\n"
    "- `lifecycle.shipping.ship(state)`, `lifecycle.shipping.cancel(state)`\n"
    "- `lifecycle.audit.audit(state, event) -> str`"
)
N1_CONSTRAINTS = (
    "- Legal transitions: `draft --approve--> approved`, `draft --reject--> rejected`, "
    "`draft --cancel--> cancelled`, `approved --ship--> shipped`, `approved --cancel--> cancelled`.\n"
    "- `rejected`, `shipped` and `cancelled` are terminal: no event leaves them.\n"
    "- `approve('shipped')`, `ship('draft')` and `approve('nowhere')` all raise `ValueError`.\n"
    "- Callers must go through `transition`; they must not encode a transition `STATES` lacks.\n"
    "- `audit` records the transition and returns the next state; an illegal transition records "
    "nothing.\n"
    "- The existing public call signatures do not change."
)
N1_ACCEPTANCE = (
    "- `test_public.py` is present in the project and must pass; run it with "
    "`python -m pytest -q test_public.py`.\n"
    "- Do not edit `test_public.py`. Hidden tests exist and are not visible; they check the same "
    "contract more strictly."
)

N1_SHARED = _shared(N1_REQUIREMENT, N1_INTERFACE, N1_CONSTRAINTS, N1_ACCEPTANCE)

N1_SEED: dict[str, str] = {
    "lifecycle/__init__.py": '"""Order lifecycle package."""\n',
    "lifecycle/state.py": '''"""Order lifecycle state machine (v1)."""

STATES = {
    "draft": {"approve": "approved"},
    "approved": {"ship": "shipped"},
    "shipped": {},
}


def transition(state, event):
    return STATES[state][event]
''',
    "lifecycle/approval.py": '''"""Approval caller (v1)."""

from lifecycle.state import transition


def approve(state):
    return transition(state, "approve")
''',
    "lifecycle/shipping.py": '''"""Shipping caller (v1)."""

from lifecycle.state import transition


def ship(state):
    return transition(state, "ship")
''',
    "lifecycle/audit.py": '''"""Audit trail (v1)."""

RECORDS = []


def audit(state, event):
    RECORDS.append((state, event))
    return len(RECORDS)
''',
    "docs/INTERFACE.md": f"""# Order lifecycle — target contract

`lifecycle/state.py` holds one table that every caller reads.

{N1_INTERFACE}

{N1_CONSTRAINTS}
""",
    "README.md": """# Order lifecycle

One state machine (`lifecycle/state.py`) and three callers: approvals, shipping and the audit trail.

The refactor extends the machine with the `rejected` and `cancelled` terminal states and moves every
caller onto the same table. Run the public checks with `python -m pytest -q test_public.py`.
""",
    "test_public.py": '''"""Public checks for the lifecycle refactor."""

import pytest

from lifecycle.approval import approve, reject
from lifecycle.audit import RECORDS, audit
from lifecycle.shipping import cancel, ship
from lifecycle.state import STATES, transition


def test_the_extended_table_has_the_new_terminals():
    assert {"rejected", "cancelled"} <= set(STATES)
    assert STATES["rejected"] == {}
    assert STATES["cancelled"] == {}


def test_transition_follows_the_table():
    assert transition("draft", "approve") == "approved"
    assert transition("approved", "ship") == "shipped"


def test_an_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        transition("nowhere", "approve")


def test_an_illegal_event_is_rejected():
    with pytest.raises(ValueError):
        transition("shipped", "approve")


def test_reject_and_cancel_are_wired():
    assert reject("draft") == "rejected"
    assert cancel("draft") == "cancelled"
    assert cancel("approved") == "cancelled"
    assert ship("approved") == "shipped"


def test_audit_records_the_transition_and_returns_the_new_state():
    RECORDS.clear()
    assert audit("draft", "approve") == "approved"
    assert RECORDS == [("draft", "approve")]


def test_audit_records_nothing_for_an_illegal_transition():
    RECORDS.clear()
    with pytest.raises(ValueError):
        audit("shipped", "approve")
    assert RECORDS == []
''',
}

N1_REFERENCE: dict[str, str] = {
    "lifecycle/state.py": '''"""Order lifecycle state machine (v2)."""

STATES = {
    "draft": {"approve": "approved", "reject": "rejected", "cancel": "cancelled"},
    "approved": {"ship": "shipped", "cancel": "cancelled"},
    "rejected": {},
    "shipped": {},
    "cancelled": {},
}


def transition(state, event):
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    if event not in STATES[state]:
        raise ValueError(f"illegal event {event!r} in state {state!r}")
    return STATES[state][event]
''',
    "lifecycle/approval.py": '''"""Approval caller (v2)."""

from lifecycle.state import transition


def approve(state):
    return transition(state, "approve")


def reject(state):
    return transition(state, "reject")
''',
    "lifecycle/shipping.py": '''"""Shipping caller (v2)."""

from lifecycle.state import transition


def ship(state):
    return transition(state, "ship")


def cancel(state):
    return transition(state, "cancel")
''',
    "lifecycle/audit.py": '''"""Audit trail (v2)."""

from lifecycle.state import transition

RECORDS = []


def audit(state, event):
    next_state = transition(state, event)
    RECORDS.append((state, event))
    return next_state
''',
}

N1_ORACLE: dict[str, str] = {
    "test_hidden_lifecycle.py": '''"""Hidden checks for the lifecycle refactor (never staged into the agent workspace)."""

import pytest

from lifecycle.approval import approve, reject
from lifecycle.audit import RECORDS, audit
from lifecycle.shipping import cancel, ship
from lifecycle.state import STATES, transition


def test_every_terminal_state_is_terminal():
    for state in ("rejected", "shipped", "cancelled"):
        assert STATES[state] == {}


def test_every_reachable_state_is_a_declared_state():
    for state, events in STATES.items():
        for event, target in events.items():
            assert target in STATES, f"{state} --{event}--> {target} is not declared"


def test_every_callable_caller_event_exists_in_the_table():
    assert transition("draft", "reject") == reject("draft")
    assert transition("draft", "cancel") == cancel("draft")
    assert transition("approved", "ship") == ship("approved")


def test_callers_do_not_hard_code_a_missing_transition():
    for state in ("rejected", "shipped", "cancelled"):
        with pytest.raises(ValueError):
            approve(state)
        with pytest.raises(ValueError):
            ship(state)
        with pytest.raises(ValueError):
            cancel(state)


def test_an_unknown_state_is_rejected_by_every_entry_point():
    for call in (approve, reject, ship, cancel):
        with pytest.raises(ValueError):
            call("nowhere")


def test_audit_is_a_faithful_trace():
    RECORDS.clear()
    assert audit("draft", "reject") == "rejected"
    assert audit("draft", "cancel") == "cancelled"
    assert RECORDS == [("draft", "reject"), ("draft", "cancel")]


def test_audit_does_not_record_an_unknown_state():
    RECORDS.clear()
    with pytest.raises(ValueError):
        audit("nowhere", "approve")
    assert RECORDS == []
''',
}


# ====================================================================================== definitions

P1 = Task(
    task_id="closure2-pipeline-migration",
    family="P1",
    title="Multi-module data pipeline migration",
    prompt=P1_REQUIREMENT,
    seed=P1_SEED,
    reference=P1_REFERENCE,
    public_tests={"test_public.py": P1_SEED["test_public.py"]},
    oracle_tests=P1_ORACLE,
    workers=(
        {
            "task_id": "parse",
            "depends_on": (),
            "objective": "parsing and normalisation modules",
            "owned_paths": ("pipeline/parsing.py", "pipeline/normalise.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker parse]\n"
                "You own the parsing and normalisation half of the v2 migration.\n\n"
                "OBJECTIVE: Implement `pipeline/parsing.py` and `pipeline/normalise.py` so the v2 "
                "record contract holds: trimmed and lower-cased names, trimmed values, exactly one "
                "`=` per line, `ValueError` for malformed lines, and a numeric `count` field with "
                "empty values dropped by `normalise_all`. The frozen contract is specified in "
                "`docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `pipeline/parsing.py` and "
                "`pipeline/normalise.py`. The aggregation, storage and public API modules belong to "
                "the other workers; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: none. The integration worker consumes your modules.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes for the parsing and normalisation "
                "checks, and you report what you ran and what it printed."
            ),
        },
        {
            "task_id": "aggregate",
            "depends_on": (),
            "objective": "aggregation and storage modules",
            "owned_paths": ("pipeline/aggregate.py", "pipeline/store.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker aggregate]\n"
                "You own the aggregation and storage half of the v2 migration.\n\n"
                "OBJECTIVE: Implement `pipeline/aggregate.py` and `pipeline/store.py` so the v2 "
                "record contract holds: aggregation sums `count` per name and returns a name-sorted "
                "list, and the store keeps one record per name with last write winning and `all()` "
                "sorted by name. The frozen contract is specified in `docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `pipeline/aggregate.py` and "
                "`pipeline/store.py`. The parsing, normalisation and public API modules belong to "
                "the other workers; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: none. The integration worker consumes your modules.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes for the aggregation and store checks, "
                "and you report what you ran and what it printed."
            ),
        },
        {
            "task_id": "integrate",
            "depends_on": ("parse", "aggregate"),
            "objective": "public API and report integration",
            "owned_paths": ("pipeline/api.py", "pipeline/report.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker integrate]\n"
                "You own the public surface that composes the pipeline.\n\n"
                "OBJECTIVE: Implement `pipeline/api.py` and `pipeline/report.py` so "
                "`build_report` runs parsing, normalisation and aggregation and returns a "
                "name-ordered mapping, and `render` produces sorted `name=total` lines with no "
                "trailing newline and an empty string for an empty report. The frozen contract is "
                "specified in `docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `pipeline/api.py` and "
                "`pipeline/report.py`. The parsing, normalisation, aggregation and storage modules "
                "belong to the other workers; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: workers parse and aggregate write the modules you compose. Their "
                "actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes end to end, and you report what you "
                "ran and what it printed."
            ),
        },
    ),
    deliverables=(
        "pipeline/aggregate.py",
        "pipeline/api.py",
        "pipeline/normalise.py",
        "pipeline/parsing.py",
        "pipeline/report.py",
        "pipeline/store.py",
    ),
    allowed_paths=("pipeline/**",),
    forbidden_paths=("test_public.py", "docs/INTERFACE.md", "README.md"),
    shared_context=P1_SHARED,
    frozen_contracts={
        "parse": ("docs/INTERFACE.md",),
        "aggregate": ("docs/INTERFACE.md",),
        "integrate": ("docs/INTERFACE.md",),
    },
    notes="authentication-free, network-free, deterministic; v1 seed is a working baseline",
    research_question=(
        "does a real two-wave decomposition over an existing codebase beat one worker, and at what "
        "token and integration cost"
    ),
)

P2 = Task(
    task_id="closure2-plugin-migration",
    family="P2",
    title="Plugin interface migration",
    prompt=P2_REQUIREMENT,
    seed=P2_SEED,
    reference=P2_REFERENCE,
    public_tests={"test_public.py": P2_SEED["test_public.py"]},
    oracle_tests=P2_ORACLE,
    workers=(
        {
            "task_id": "csv",
            "depends_on": (),
            "objective": "the CSV plugin",
            "owned_paths": ("plugins/csv_fields.py", "plugins/csv_plugin.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker csv]\n"
                "You own the CSV plugin half of the v2 migration.\n\n"
                "OBJECTIVE: Implement `plugins/csv_plugin.py` and `plugins/csv_fields.py` so "
                "`CsvPlugin` satisfies the published plugin interface: `supports('csv')`, a `parse` "
                "that splits each row at the first comma outside double quotes and unquotes both "
                "cells, a `PluginError` for a row with no separator, and a `transform` that trims "
                "both fields and lower-cases the key on request. The interface is defined in "
                "`plugins/base.py` and described in `docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `plugins/csv_plugin.py` and "
                "`plugins/csv_fields.py`. The JSON plugin and the registry belong to the other "
                "workers; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: none. The integration worker dispatches through your plugin.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes for the CSV checks, and you report what "
                "you ran and what it printed."
            ),
        },
        {
            "task_id": "json",
            "depends_on": (),
            "objective": "the JSON plugin",
            "owned_paths": ("plugins/json_fields.py", "plugins/json_plugin.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker json]\n"
                "You own the JSON plugin half of the v2 migration.\n\n"
                "OBJECTIVE: Implement `plugins/json_plugin.py` and `plugins/json_fields.py` so "
                "`JsonPlugin` satisfies the published plugin interface: `supports('json')`, a "
                "`parse` that flattens nested objects into dotted keys and stringifies scalars, a "
                "`PluginError` for invalid JSON or a non-object root, and a `transform` that trims "
                "both fields and lower-cases the key on request. The interface is defined in "
                "`plugins/base.py` and described in `docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `plugins/json_plugin.py` and "
                "`plugins/json_fields.py`. The CSV plugin and the registry belong to the other "
                "workers; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: none. The integration worker dispatches through your plugin.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes for the JSON checks, and you report "
                "what you ran and what it printed."
            ),
        },
        {
            "task_id": "register",
            "depends_on": ("csv", "json"),
            "objective": "registry, loader and integration",
            "owned_paths": ("plugins/loader.py", "plugins/registry.py"),
            "assignment": (
                "[YOUR ASSIGNMENT — worker register]\n"
                "You own registration, dispatch and the integration surface.\n\n"
                "OBJECTIVE: Implement `plugins/registry.py` and `plugins/loader.py` so registration "
                "rejects a duplicate name with `PluginError` without overwriting, lookup raises "
                "`PluginError` for an unknown name, and `load` selects the first registered plugin "
                "whose `supports` matches, parses and then transforms every record, raising "
                "`PluginError` when no plugin supports the source. The interface is defined in "
                "`plugins/base.py` and described in `docs/INTERFACE.md`.\n\n"
                "OWNED MODULES: you are the only worker who may change `plugins/registry.py` and "
                "`plugins/loader.py`. The two plugins and `plugins/base.py` belong to the other "
                "workers and to the frozen contract; read them, do not edit them.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: workers csv and json write the plugins you dispatch to. Their actual "
                "results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes end to end, and you report what you ran "
                "and what it printed."
            ),
        },
    ),
    deliverables=(
        "plugins/csv_plugin.py",
        "plugins/json_plugin.py",
        "plugins/loader.py",
        "plugins/registry.py",
    ),
    allowed_paths=("plugins/**",),
    forbidden_paths=("plugins/base.py", "test_public.py", "docs/INTERFACE.md", "README.md"),
    shared_context=P2_SHARED,
    frozen_contracts={
        "csv": ("docs/INTERFACE.md",),
        "json": ("docs/INTERFACE.md",),
        "register": ("docs/INTERFACE.md",),
    },
    notes="independent verification task; different problem shape from P1, not a rename",
    research_question=(
        "does a published interface contract reduce cross-worker coordination cost when two plugins "
        "have different local edge cases"
    ),
)

N1 = Task(
    task_id="closure2-state-machine-refactor",
    family="N1",
    title="Coupled state machine refactor",
    prompt=N1_REQUIREMENT,
    seed=N1_SEED,
    reference=N1_REFERENCE,
    public_tests={"test_public.py": N1_SEED["test_public.py"]},
    oracle_tests=N1_ORACLE,
    workers=(
        {
            "task_id": "machine",
            "depends_on": (),
            "objective": "extend the shared state table",
            "owned_paths": ("lifecycle/state.py",),
            "assignment": (
                "[YOUR ASSIGNMENT — worker machine]\n"
                "You own the shared state table.\n\n"
                "OBJECTIVE: Implement `lifecycle/state.py` so it encodes the extended machine: the "
                "`rejected` and `cancelled` terminal states plus the `reject` and `cancel` events, "
                "with `transition` raising `ValueError` for an unknown state or an illegal event.\n\n"
                "OWNED MODULES: `lifecycle/state.py`.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: none. The callers are written against the table you publish, so keep "
                "the shapes exactly as the shared contract states.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes for the table checks, and you report "
                "what you ran and what it printed."
            ),
        },
        {
            "task_id": "callers",
            "depends_on": ("machine",),
            "objective": "wire the callers and the audit trail",
            "owned_paths": (
                "lifecycle/approval.py",
                "lifecycle/audit.py",
                "lifecycle/shipping.py",
                "lifecycle/state.py",
            ),
            "assignment": (
                "[YOUR ASSIGNMENT — worker callers]\n"
                "You own the callers of the shared machine, and you must extend "
                "`lifecycle/state.py` to expose the events they need.\n\n"
                "OBJECTIVE: Implement `lifecycle/approval.py`, `lifecycle/shipping.py`, "
                "`lifecycle/audit.py` and the `reject`/`cancel` entries of `lifecycle/state.py` so "
                "every caller goes through `transition`, illegal transitions are rejected, and "
                "`audit` records a transition only when it succeeds.\n\n"
                "OWNED MODULES: the three caller modules, and the same state module the machine "
                "worker owns.\n\n"
                "Read `docs/INTERFACE.md` before editing.\n\n"
                "DEPENDENCIES: worker machine writes `lifecycle/state.py`. Its actual result is "
                "provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: no `NotImplementedError` is left, "
                "`python -m pytest -q test_public.py` passes end to end, and you report what you ran "
                "and what it printed."
            ),
        },
    ),
    deliverables=(
        "lifecycle/approval.py",
        "lifecycle/audit.py",
        "lifecycle/shipping.py",
        "lifecycle/state.py",
    ),
    allowed_paths=("lifecycle/**",),
    forbidden_paths=("test_public.py", "docs/INTERFACE.md", "README.md"),
    shared_context=N1_SHARED,
    frozen_contracts={},
    notes=(
        "negative control: both candidate steps must edit lifecycle/state.py, so the plan has a real "
        "shared write and no independent wave"
    ),
    research_question="does the frozen router refuse a plan whose two steps share one state table",
)

TASKS: tuple[Task, ...] = (P1, P2, N1)
TASKS_BY_ID: dict[str, Task] = {task.task_id: task for task in TASKS}


def task_by_id(task_id: str) -> Task:
    if task_id not in TASKS_BY_ID:
        raise KeyError(f"unknown closure2 task {task_id!r}")
    return TASKS_BY_ID[task_id]


# ====================================================================================== task building


def single_worker_turn(task: Task) -> str:
    """The whole-task turn: the shared public context plus an explicit whole-task assignment."""

    module_list = ", ".join(sorted(task.deliverables))
    return "\n\n".join(
        [
            task.shared_context,
            "[YOUR ASSIGNMENT]\n"
            "You are the only worker on this task. Implement every module named in the public "
            "requirement above, in the same workspace.\n\n"
            f"DELIVERABLES: {module_list}\n\n"
            "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes, no module is left "
            "half-migrated, and you report what you ran and what it printed.",
        ]
    )


def worker_steps(task: Task) -> tuple[WorkerStep, ...]:
    return tuple(
        WorkerStep(
            task_id=str(worker["task_id"]),
            title=str(worker["objective"])[:80],
            prompt=str(worker["assignment"]),
            depends_on=tuple(worker["depends_on"]),  # type: ignore[arg-type]
            max_turns=int(worker.get("max_turns", 12)),
            shared_context=task.shared_context,
        )
        for worker in task.workers
    )


def single_step(task: Task) -> tuple[WorkerStep, ...]:
    return (WorkerStep(task_id="main", title=task.task_id, prompt=single_worker_turn(task)),)


def build_task(task: Task, *, topology: Topology) -> BenchmarkTask:
    """The one builder both arms use, so the digest cannot drift between them.

    The public suite is part of what the agent is given: a task whose ``public_tests`` are declared
    separately from its seed used to be materialised without them, so the frozen public-test command
    ran against a workspace that had no ``test_public.py`` and every such cell failed with a pytest
    usage error instead of a measurement.
    """

    if topology not in ("single", "multi"):
        raise ValueError(f"unknown topology {topology!r}")
    multi = topology == "multi"
    public = dict(getattr(task, "public_tests", {}) or {})
    return BenchmarkTask(
        sample_id=task.task_id,
        category="feature",
        mode="multi_worker" if multi else "coding",
        prompt=single_worker_turn(task),
        target="",
        visible_files={**dict(task.seed), **public},
        hidden_files=dict(task.oracle_tests),
        workers=worker_steps(task) if multi else single_step(task),
        allowed_paths=tuple(task.allowed_paths),
        forbidden_paths=tuple(task.forbidden_paths),
        required_artifacts=tuple(sorted(task.deliverables)),
        frozen_contracts=dict(task.frozen_contracts) if multi else {},
        # Budget protocol (proposal, see docs/closure-2-preregistration.md §4): one worker gets the
        # whole 36-turn allowance, a three-worker plan gets 12 per worker — the same theoretical
        # ceiling, so neither topology is handicapped.
        max_turns=12 if multi else 36,
        tags=("closure2", task.family, topology),
        notes=task.notes,
    )


# ====================================================================================== digests


def task_digests(task: Task) -> dict[str, str]:
    """Every frozen digest for one task, in one place."""

    multi = build_task(task, topology="multi")
    single = build_task(task, topology="single")
    plan = [
        {
            "task_id": str(worker["task_id"]),
            "depends_on": list(worker["depends_on"]),
            "objective": str(worker["objective"]),
            "owned_paths": list(worker["owned_paths"]),
            "assignment_digest": canonical_digest(str(worker["assignment"])),
        }
        for worker in task.workers
    ]
    return {
        "seed_digest": canonical_digest(dict(sorted(task.seed.items()))),
        "reference_digest": canonical_digest(dict(sorted(task.reference.items()))),
        "public_tests_digest": canonical_digest(dict(sorted(task.public_tests.items()))),
        "oracle_digest": canonical_digest(dict(sorted(task.oracle_tests.items()))),
        "public_requirement_digest": canonical_digest(
            {"prompt": task.prompt, "shared_context": task.shared_context}
        ),
        "candidate_dag_digest": canonical_digest(plan),
        "fixture_digest_single": single.fixture_digest,
        "fixture_digest_multi": multi.fixture_digest,
        "effective_public_task_digest_single": effective_public_task_digest(single),
        "effective_public_task_digest_multi": effective_public_task_digest(multi),
    }
