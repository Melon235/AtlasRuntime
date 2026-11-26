"""Stage F — family P: two parallel implementation tasks.

P1 is a two-connector migration with a shared batch/checkpoint pipeline. P2 is a two-protocol
adapter upgrade with a shared codec/dispatch surface. In both, the two halves own disjoint files and
the integration node owns the compatibility rules that only make sense once both halves exist.
"""

from __future__ import annotations

from experiments.stage_f.tasks.common import Task, shared_block
from experiments.stage_f.tasks.fixtures import load

_P1 = load("p1")
_P2 = load("p2")

# ====================================================================================== P1


P1 = Task(
    task_id="f-p1-connector-migration",
    family="P",
    title="Multi-connector ingest migration",
    prompt=(
        "The ingest layer is moving to the frozen v2 connector contract in `docs/INTERFACE.md`. "
        "Two independent connectors (CSV with quoting and header projection, JSONL with a dotted "
        "field mapping) must be migrated, and the shared batching, checkpoint, registry, pipeline "
        "and report modules must be brought onto the same contract. `ingest/errors.py`, "
        "`ingest/model.py`, `ingest/schemas.py` and `ingest/values.py` are frozen."
    ),
    seed=_P1["seed"],
    reference=_P1["reference"],
    public_tests=_P1["public"],
    oracle_tests=_P1["oracle"],
    workers=(
        {
            "task_id": "csv",
            "objective": "Migrate the CSV connector and its quoting/header helpers",
            "assignment": (
                "[YOUR ASSIGNMENT — worker csv]\n"
                "You own the CSV half of the v2 connector migration: `ingest/csv_connector.py` and "
                "`ingest/columns.py`.\n\n"
                "OBJECTIVE: implement header projection (trimmed, lower-cased, duplicate/unknown/"
                'missing cells rejected), quoted cells with `""` escapes and unbalanced-quote '
                "rejection, per-row cell-count validation, required/optional field rules, strict "
                "conversion through the frozen `ingest.values.convert`, the data-row cursor and the "
                "blank-line skip count. The exact rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `ingest/columns.py` and `ingest/csv_connector.py`. The JSONL "
                "connector and the shared pipeline belong to the other workers; read them, do not "
                "edit them. `ingest/values.py` is frozen.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the CSV checks, "
                "no module is left half-migrated, and you report what you ran and what it printed."
            ),
            "owned_paths": ("ingest/columns.py", "ingest/csv_connector.py"),
            "depends_on": (),
            "max_turns": 14,
        },
        {
            "task_id": "jsonl",
            "objective": "Migrate the JSONL connector and its dotted-path helper",
            "assignment": (
                "[YOUR ASSIGNMENT — worker jsonl]\n"
                "You own the JSONL half of the v2 connector migration: `ingest/jsonl_connector.py` "
                "and `ingest/paths.py`.\n\n"
                "OBJECTIVE: implement one-object-per-line parsing, blank-line skipping, the dotted "
                "`mapping` resolution (with a non-object intermediate rejected), required/optional "
                "field rules, strict conversion through the frozen `ingest.values.convert`, mapping "
                "validation (a mapping may only name schema fields) and the line cursor. The exact "
                "rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `ingest/paths.py` and `ingest/jsonl_connector.py`. The CSV connector "
                "and the shared pipeline belong to the other workers; read them, do not edit them. "
                "`ingest/values.py` is frozen.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the JSONL "
                "checks, and you report what you ran and what it printed."
            ),
            "owned_paths": ("ingest/paths.py", "ingest/jsonl_connector.py"),
            "depends_on": (),
            "max_turns": 14,
        },
        {
            "task_id": "integrate",
            "objective": "Batch, checkpoint, registry, pipeline and report integration",
            "assignment": (
                "[YOUR ASSIGNMENT — worker integrate]\n"
                "You own the shared surface: `ingest/batching.py`, `ingest/checkpoint.py`, "
                "`ingest/registry.py`, `ingest/pipeline.py` and `ingest/report.py`.\n\n"
                "OBJECTIVE: implement count- and byte-bounded contiguous batches with the canonical "
                "record size; an atomically persisted, monotonic checkpoint store with typed "
                "corruption errors; a registry that rejects duplicates and knows both sources; a "
                "pipeline that resumes from the checkpoint, computes numeric totals and saves only "
                "after a successful read; and the exact report ordering without a trailing newline. "
                "The rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the five modules above. The two connectors and the frozen support "
                "modules belong to the other workers; read them, do not edit them.\n\n"
                "DEPENDENCIES: workers csv and jsonl implement the connectors you dispatch to. Their "
                "actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report what you ran and what it printed."
            ),
            "owned_paths": (
                "ingest/batching.py",
                "ingest/checkpoint.py",
                "ingest/registry.py",
                "ingest/pipeline.py",
                "ingest/report.py",
            ),
            "depends_on": ("csv", "jsonl"),
            "max_turns": 18,
        },
    ),
    deliverables=(
        "ingest/batching.py",
        "ingest/checkpoint.py",
        "ingest/columns.py",
        "ingest/csv_connector.py",
        "ingest/jsonl_connector.py",
        "ingest/paths.py",
        "ingest/pipeline.py",
        "ingest/registry.py",
        "ingest/report.py",
    ),
    allowed_paths=("ingest/",),
    forbidden_paths=("docs/INTERFACE.md", "README.md", "test_public.py", ".git/"),
    shared_context=shared_block(
        requirement=(
            "Move two independent input connectors and the shared batching/checkpoint/pipeline "
            "surface to the frozen v2 connector contract in `docs/INTERFACE.md`."
        ),
        interface=(
            "- `ingest.csv_connector.read(raw, schema, *, start=0, mapping=None)`\n"
            "- `ingest.jsonl_connector.read(raw, schema, *, start=0, mapping=None)`\n"
            "- `ingest.batching.build_batches(records, max_records, max_bytes)`\n"
            "- `ingest.checkpoint.CheckpointStore(path)`\n"
            "- `ingest.registry.register/build/available`\n"
            "- `ingest.pipeline.run(source, raw, schema, *, start, store, max_records, max_bytes, "
            "mapping)`\n"
            "- `ingest.report.render(result)`"
        ),
        constraints=(
            "- `ingest/errors.py`, `ingest/model.py`, `ingest/schemas.py` and `ingest/values.py` are "
            "frozen.\n"
            "- The public suite is visible; hidden tests check the same contract more widely.\n"
            "- Only `ingest/` is writable for this task."
        ),
        acceptance=(
            "`python -m pytest -q test_public.py` passes and every module follows the v2 contract."
        ),
    ),
    frozen_contracts={
        "csv": ("docs/INTERFACE.md",),
        "jsonl": ("docs/INTERFACE.md",),
        "integrate": ("docs/INTERFACE.md",),
    },
    notes=(
        "two independent connectors with disjoint write sets plus a real integration node whose "
        "batch, checkpoint, resume and total rules only make sense once both connectors exist"
    ),
    research_question=(
        "does a two-connector migration with a shared resumable pipeline reward a parallel "
        "decomposition, and what does the integration node cost"
    ),
)


# ====================================================================================== P2


P2 = Task(
    task_id="f-p2-protocol-adapter",
    family="P",
    title="Local wire protocol adapter upgrade",
    prompt=(
        "Two independent local protocols (line-delimited JSON messages and a binary length/CRC frame "
        "format) must be upgraded to the frozen v2 contract in `docs/INTERFACE.md`, including v1 "
        "read compatibility, streaming decoders and typed errors. The codec registry, the handler "
        "dispatch table, the decode-then-dispatch service and the protocol selector must then be "
        "completed. `wire/errors.py` and `wire/model.py` are frozen."
    ),
    seed=_P2["seed"],
    reference=_P2["reference"],
    public_tests=_P2["public"],
    oracle_tests=_P2["oracle"],
    workers=(
        {
            "task_id": "json",
            "objective": "Upgrade the JSON message protocol and its field helpers",
            "assignment": (
                "[YOUR ASSIGNMENT — worker json]\n"
                "You own the JSON message protocol: `wire/json_message.py` and "
                "`wire/json_fields.py`.\n\n"
                "OBJECTIVE: implement the v2 envelope with a canonical body CRC, v1 read "
                "compatibility, duplicate-key rejection, strict document validation, the line "
                "stream decoder with its limit and truncation behaviour, and CRLF/blank-line "
                "handling. The exact rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: those two modules only. The binary frame protocol and the shared "
                "codec/dispatch surface belong to the other workers; read them, do not edit them. "
                "`wire/errors.py` and `wire/model.py` are frozen.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the JSON "
                "checks, and you report what you ran and what it printed."
            ),
            "owned_paths": ("wire/json_fields.py", "wire/json_message.py"),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "binary",
            "objective": "Upgrade the binary frame protocol",
            "assignment": (
                "[YOUR ASSIGNMENT — worker binary]\n"
                "You own the binary frame protocol: `wire/frame.py`.\n\n"
                "OBJECTIVE: implement the v2 writer and the v1/v2 reader: magic and version "
                "consistency, kind validation, length bounds, CRC32, trailing-byte and truncation "
                "errors, and the streaming decoder that accepts any split and enforces its limit. "
                "The exact layout and error mapping are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: `wire/frame.py`. The JSON protocol and the shared codec/dispatch "
                "surface belong to the other workers; read them, do not edit them. `wire/errors.py` "
                "and `wire/model.py` are frozen.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes for the binary "
                "checks, and you report what you ran and what it printed."
            ),
            "owned_paths": ("wire/frame.py",),
            "depends_on": (),
            "max_turns": 16,
        },
        {
            "task_id": "integrate",
            "objective": "Codec registry, dispatch, service and protocol selector",
            "assignment": (
                "[YOUR ASSIGNMENT — worker integrate]\n"
                "You own the shared surface: `wire/codecs.py`, `wire/dispatch.py`, "
                "`wire/service.py` and `wire/selector.py`.\n\n"
                "OBJECTIVE: implement the codec registry (both protocols, duplicate rejection, "
                "sorted listing, decoder factory), the handler dispatch table (duplicate rejection, "
                "typed unknown kind, JSON body vs binary payload), the decode-then-dispatch service, "
                "the whitespace-tolerant, prefix-safe protocol selector, the payload schemas and the "
                "decode-validate-dispatch gateway. The exact rules are in `docs/INTERFACE.md`.\n\n"
                "OWNED FILES: the six modules above. The two protocol modules and the frozen "
                "support modules belong to the other workers; read them, do not edit them.\n\n"
                "DEPENDENCIES: workers json and binary implement the codecs you register. Their "
                "actual results are provided in this prompt when available.\n\n"
                "DEFINITION OF DONE: `python -m pytest -q test_public.py` passes end to end, and you "
                "report what you ran and what it printed."
            ),
            "owned_paths": (
                "wire/codecs.py",
                "wire/dispatch.py",
                "wire/gateway.py",
                "wire/selector.py",
                "wire/service.py",
                "wire/validation.py",
            ),
            "depends_on": ("json", "binary"),
            "max_turns": 16,
        },
    ),
    deliverables=(
        "wire/codecs.py",
        "wire/dispatch.py",
        "wire/frame.py",
        "wire/json_fields.py",
        "wire/json_message.py",
        "wire/gateway.py",
        "wire/selector.py",
        "wire/service.py",
        "wire/validation.py",
    ),
    allowed_paths=("wire/",),
    forbidden_paths=(
        "wire/errors.py",
        "wire/model.py",
        "docs/INTERFACE.md",
        "test_public.py",
        ".git/",
    ),
    shared_context=shared_block(
        requirement=(
            "Upgrade two independent local protocols, their registry/dispatch surface and the "
            "protocol selector to the frozen v2 contract in `docs/INTERFACE.md`."
        ),
        interface=(
            "- `wire.json_message.encode/decode/Decoder`\n"
            "- `wire.frame.encode/decode/Decoder` (v1 and v2 reads)\n"
            "- `wire.codecs.register/get/available/decode/decoder`\n"
            "- `wire.dispatch.register/dispatch/kinds`\n"
            "- `wire.service.handle/StreamingService`\n"
            "- `wire.selector.Selector`\n"
            "- `wire.validation.validate` and `wire.gateway.Gateway`"
        ),
        constraints=(
            "- `wire/errors.py` and `wire/model.py` are frozen.\n"
            "- Error types are part of the contract, not an implementation detail.\n"
            "- Hidden tests check the same rules as the visible ones, more widely."
        ),
        acceptance=(
            "`python -m pytest -q test_public.py` passes and the v2 contract holds for both "
            "protocols."
        ),
    ),
    frozen_contracts={
        "json": ("docs/INTERFACE.md", "wire/errors.py"),
        "binary": ("docs/INTERFACE.md", "wire/errors.py"),
        "integrate": ("docs/INTERFACE.md", "wire/errors.py", "wire/model.py"),
    },
    notes=(
        "two protocol families with genuinely different framing, one read-compatibility version, "
        "streaming decoders and a shared registry/dispatch/selector integration node"
    ),
    research_question=(
        "does a two-protocol upgrade with a shared dispatch surface produce a genuine parallel "
        "wave, at medium size"
    ),
)

TASKS: tuple[Task, ...] = (P1, P2)

__all__ = ["P1", "P2", "TASKS"]
