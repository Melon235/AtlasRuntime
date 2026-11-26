"""The frozen field table and migration map."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MISSING = object()

VERSION = 2


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    default: Any = MISSING
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    non_empty: bool = False


FIELDS: dict[str, FieldSpec] = {
    "host": FieldSpec("host", "str", default="localhost", non_empty=True),
    "port": FieldSpec("port", "int", default=8080, minimum=1, maximum=65535),
    "timeout": FieldSpec("timeout", "float", default=30.0, minimum=1.0, maximum=300.0),
    "retry.max": FieldSpec("retry.max", "int", default=3, minimum=1, maximum=10),
    "retry.delay": FieldSpec("retry.delay", "float", default=0.5, minimum=0.1, maximum=30.0),
    "retry.jitter": FieldSpec("retry.jitter", "bool", default=False),
    "tls.enabled": FieldSpec("tls.enabled", "bool", default=False),
    "tls.cert": FieldSpec("tls.cert", "str", default=""),
    "tls.key": FieldSpec("tls.key", "str", default=""),
    "log.level": FieldSpec(
        "log.level",
        "str",
        default="info",
        choices=("debug", "info", "warning", "error"),
    ),
    "logging.format": FieldSpec(
        "logging.format",
        "str",
        default="text",
        choices=("json", "text"),
    ),
    "database.url": FieldSpec("database.url", "str", default="sqlite:///app.db", non_empty=True),
    "database.pool": FieldSpec("database.pool", "int", default=5, minimum=1, maximum=50),
    "cache.enabled": FieldSpec("cache.enabled", "bool", default=False),
    "cache.ttl": FieldSpec("cache.ttl", "float", default=60.0, minimum=1.0, maximum=3600.0),
    "cache.namespace": FieldSpec("cache.namespace", "str", default="app", non_empty=True),
    "features": FieldSpec("features", "list", default=()),
}

FEATURE_FLAGS: tuple[str, ...] = ("audit", "cache", "metrics", "tracing")

DEFAULTS: dict[str, Any] = {
    name: spec.default for name, spec in FIELDS.items() if spec.default is not MISSING
}

RENAMES_V1: dict[str, str] = {
    "max_retries": "retry.max",
    "retry_delay": "retry.delay",
    "tls_cert": "tls.cert",
    "tls_key": "tls.key",
}

LEGACY_PREFIX = "legacy_"
