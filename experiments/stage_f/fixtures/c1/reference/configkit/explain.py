"""Human and machine views of an effective configuration (v2)."""

from __future__ import annotations

from configkit.runtime import Settings

PREFIX = "CONFIGKIT__"


def _text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def render(settings: Settings) -> str:
    """One ``key=value [origin]`` line per field, sorted by key."""

    lines = []
    for key in sorted(settings.values):
        lines.append(f"{key}={_text(settings.values[key])} [{settings.explain(key)}]")
    return "\n".join(lines)


def as_env(settings: Settings) -> dict[str, str]:
    """Round-trip an effective configuration to ``CONFIGKIT__`` environment variables."""

    return {
        f"{PREFIX}{key.upper().replace('.', '__')}": _text(value)
        for key, value in sorted(settings.values.items())
    }


def diff(left: Settings, right: Settings) -> dict[str, tuple[object, object]]:
    """Keys whose effective values differ, with the left and right values."""

    changes: dict[str, tuple[object, object]] = {}
    for key in sorted(set(left.values) | set(right.values)):
        before = left.values.get(key)
        after = right.values.get(key)
        if before != after:
            changes[key] = (before, after)
    return changes


__all__ = ["PREFIX", "as_env", "diff", "render"]
