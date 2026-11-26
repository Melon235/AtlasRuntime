"""Named environment profiles (v2).

A profile is a small, flattened overlay between the file and the environment. It exists so that a
deployment names one preset instead of repeating six overrides, while the environment and the
explicit layer still win.
"""

from __future__ import annotations

from configkit.errors import ProfileError

PROFILES: dict[str, dict[str, object]] = {
    "dev": {
        "log.level": "debug",
        "logging.format": "text",
        "cache.enabled": False,
        "database.url": "sqlite:///dev.db",
        "database.pool": 2,
        "timeout": 30.0,
    },
    "staging": {
        "log.level": "info",
        "logging.format": "json",
        "cache.enabled": True,
        "cache.ttl": 300.0,
        "database.url": "postgresql://staging/app",
        "database.pool": 5,
        "features": ["cache", "metrics"],
    },
    "prod": {
        "log.level": "warning",
        "logging.format": "json",
        "cache.enabled": True,
        "cache.ttl": 900.0,
        "database.url": "postgresql://prod/app",
        "database.pool": 20,
        "timeout": 60.0,
        "features": ["audit", "cache", "metrics", "tracing"],
    },
}


def names() -> list[str]:
    return sorted(PROFILES)


def load(name: str) -> dict[str, object]:
    if name not in PROFILES:
        raise ProfileError(name, f"unknown profile {name!r}")
    return dict(PROFILES[name])


__all__ = ["PROFILES", "load", "names"]
