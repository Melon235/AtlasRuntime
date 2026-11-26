"""Load task fixtures from ``experiments/stage_f/fixtures/<task>/``.

The six expanded Stage F tasks are authored as real files, not as string literals: business source,
docs and tests have to be readable and countable to be reviewed. The frozen ``Task`` dataclass still
receives plain ``dict[str, str]`` content, so the digests, the fixture writer and the sandboxed
oracle keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"

PARTS = ("seed", "reference", "public", "oracle")


def _read_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = path.read_text(encoding="utf-8")
    return files


def load(short_name: str) -> dict[str, dict[str, str]]:
    """Return the four content maps for one task directory."""

    root = FIXTURE_ROOT / short_name
    if not root.exists():
        raise FileNotFoundError(f"no fixture directory for {short_name!r}: {root}")
    loaded = {part: _read_tree(root / part) for part in PARTS}
    for part in ("seed", "public", "oracle"):
        if not loaded[part]:
            raise FileNotFoundError(f"{short_name}/{part} is empty")
    return loaded


__all__ = ["FIXTURE_ROOT", "PARTS", "load"]
