"""Controlled workspaces for product runs.

A Main-driven run writes files. The Host therefore has to hand it a workspace it is allowed to
write, and the sandbox provisioner needs that workspace to be a *clean git repository* — it clones
it and refuses anything else. Neither condition holds for a fixture directory, and the repository
the user is reading this file in is never a candidate.

This module is the only place that turns a workspace *name* into a repository:

```text
name → allowed root → source directory → copy → git init + one commit → seed digest
```

Three rules, none of them negotiable:

* The name is a single path segment under an explicitly allowed root. No separators, no ``..``, no
  drive letters, no symlink or junction that resolves outside the root.
* The AtlasRuntime checkout is refused as a source, even if someone configures its parent as a
  root: the product path must never write into the runtime's own tree.
* The copied tree is bounded (files and bytes) and excludes VCS metadata, caches, virtualenvs and
  ``.env`` files. What is copied is recorded as a digest so a replay can tell two seeds apart.

Nothing here executes a task, and nothing here is authorization: the write scope it derives is
passed to the existing ownership policy, which enforces it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOTS = (REPO_ROOT / "experiments" / "fixtures",)
WORKSPACE_ROOTS_ENV = "ATLASOS_WORKSPACE_ROOTS"

MAX_FILES = 20_000
MAX_BYTES = 64 * 1024 * 1024
MAX_NAME = 64

#: Excluded from every copy. ``.git`` is re-created, never copied: the seed of a run is one commit.
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".idea",
        ".vscode",
    }
)
_EXCLUDED_NAMES = frozenset({".env", ".ds_store", "thumbs.db", "id_rsa", "id_ed25519"})
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".pem", ".key", ".pfx", ".p12"})

#: Fixed identity and date for the seed commit, so the same tree always yields the same commit id.
_SEED_MESSAGE = "atlas: materialised task seed"
_SEED_DATE = "2026-01-01T00:00:00+0000"


class WorkspaceError(RuntimeError):
    """A workspace request the Host refuses; the API maps this to 4xx."""

    status_code = 422


@dataclass(frozen=True)
class WorkspaceSeed:
    """One validated source tree and the write scope derived from it."""

    name: str
    source: Path
    files: int
    size_bytes: int
    digest: str
    write_scope: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": str(self.source),
            "files": self.files,
            "size_bytes": self.size_bytes,
            "seed_digest": self.digest,
            "write_scope": list(self.write_scope),
        }


def workspace_roots() -> tuple[Path, ...]:
    """The allowed roots, from ``ATLASOS_WORKSPACE_ROOTS`` when it is set."""

    configured = os.environ.get(WORKSPACE_ROOTS_ENV, "").strip()
    if not configured:
        return tuple(root.resolve() for root in DEFAULT_WORKSPACE_ROOTS)
    roots: list[Path] = []
    for part in configured.split(os.pathsep):
        if part.strip():
            roots.append(Path(part).expanduser().resolve())
    return tuple(roots)


def validate_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise WorkspaceError("a workspace name is required")
    if len(text) > MAX_NAME:
        raise WorkspaceError(f"workspace name is longer than {MAX_NAME} characters")
    if any(separator in text for separator in ("/", "\\", "\x00")):
        raise WorkspaceError("a workspace name is a single path segment, not a path")
    if text in {".", ".."} or text.startswith("."):
        raise WorkspaceError("a workspace name must not be a hidden or relative directory")
    if Path(text).is_absolute() or (len(text) > 1 and text[1] == ":"):
        raise WorkspaceError("a workspace name must not be an absolute path")
    return text


def _within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:  # different drives
        return False


def resolve_seed(name: str, *, roots: tuple[Path, ...] | None = None) -> WorkspaceSeed:
    """Validate ``name`` and describe the tree it points at. Never copies anything."""

    clean = validate_name(name)
    allowed = roots or workspace_roots()
    if not allowed:
        raise WorkspaceError("no workspace root is configured")
    source: Path | None = None
    for root in allowed:
        root = root.resolve()
        entry = root / clean
        if entry.is_symlink() or entry.is_junction():
            raise WorkspaceError("a workspace must not be a symlink or junction")
        candidate = entry.resolve()
        # ``resolve`` follows symlinks and junctions, so the containment check below is the
        # check on the real target, not on the name that led to it.
        if candidate.is_dir() and _within(candidate, root):
            source = candidate
            break
    if source is None:
        raise WorkspaceError(
            f"workspace {clean!r} is not a directory inside any allowed root "
            f"({', '.join(str(root) for root in allowed)})"
        )
    if _within(source, REPO_ROOT) and not any(
        _within(source, root.resolve()) and source != root.resolve()
        for root in DEFAULT_WORKSPACE_ROOTS
    ):
        raise WorkspaceError("the runtime checkout is never a task workspace")
    files, size = _measure(source)
    return WorkspaceSeed(
        name=clean,
        source=source,
        files=files,
        size_bytes=size,
        digest=_tree_digest(source),
        write_scope=_write_scope(source),
    )


def _excluded(name: str) -> bool:
    name = name.lower()
    return (
        name in _EXCLUDED_DIRECTORIES
        or name in _EXCLUDED_NAMES
        or name.startswith(".env.")
        or Path(name).suffix in _EXCLUDED_SUFFIXES
    )


def _check_entry(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise WorkspaceError(f"workspace contains an unsupported entry: {path.name}")


def _iter_files(root: Path) -> Iterator[Path]:
    _check_entry(root, directory=True)
    for current, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories if not _excluded(name))
        for name in directories:
            _check_entry(Path(current) / name, directory=True)
        for name in sorted(names):
            if _excluded(name):
                continue
            path = Path(current) / name
            _check_entry(path)
            yield path


def _measure(root: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for path in _iter_files(root):
        files += 1
        if files > MAX_FILES:
            raise WorkspaceError(f"workspace holds more than {MAX_FILES} files")
        size += path.stat().st_size
        if size > MAX_BYTES:
            raise WorkspaceError(f"workspace is larger than {MAX_BYTES} bytes")
    if files == 0:
        raise WorkspaceError("workspace is empty")
    return files, size


def _tree_digest(root: Path) -> str:
    """Content digest of the seed: relative path, size and bytes of every included file."""

    digest = hashlib.sha256()
    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _write_scope(root: Path) -> tuple[str, ...]:
    """The Main's Host-owned write scope, derived from the seed tree itself.

    Directories are declared with a trailing slash (the ownership policy reads that as "this
    directory"), top-level files exactly. A model that wants to create a brand-new top-level file
    is refused; everything under a declared directory is allowed. This is a scope the Host can
    state without the model's help, and the runtime still enforces it per call.
    """

    scope: list[str] = []
    for current, directories, names in os.walk(root):
        relative = Path(current).relative_to(root).as_posix()
        directories[:] = sorted(name for name in directories if not _excluded(name))
        for name in directories:
            path = name if relative == "." else f"{relative}/{name}"
            scope.append(path + "/")
        if relative == ".":
            for name in sorted(names):
                if _excluded(name):
                    continue
                scope.append(name)
    return tuple(sorted(set(scope)))


def _git(repo: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "AtlasRuntime",
        "GIT_AUTHOR_EMAIL": "runtime@atlas.local",
        "GIT_COMMITTER_NAME": "AtlasRuntime",
        "GIT_COMMITTER_EMAIL": "runtime@atlas.local",
        "GIT_AUTHOR_DATE": _SEED_DATE,
        "GIT_COMMITTER_DATE": _SEED_DATE,
    }
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    if completed.returncode != 0:
        raise WorkspaceError(
            f"git {args[0]} failed in {repo}: {completed.stderr.strip()[:400] or 'no output'}"
        )
    return completed.stdout.strip()


def materialise(seed: WorkspaceSeed, target: Path) -> dict[str, Any]:
    """Copy the seed into ``target`` and make it a one-commit repository.

    The copy is what the sandbox clones. The source directory is only read.
    """

    target = target.resolve()
    if target.exists():
        raise WorkspaceError(f"workspace target already exists: {target}")
    if _measure(seed.source) != (seed.files, seed.size_bytes):
        raise WorkspaceError("the source workspace changed before materialisation")
    target.mkdir(parents=True)
    for path in _iter_files(seed.source):
        destination = target / path.relative_to(seed.source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination, follow_symlinks=False)
    if _tree_digest(target) != seed.digest:
        raise WorkspaceError("the source workspace changed during materialisation")
    _git(target, "init", "--quiet", "--initial-branch=main")
    _git(target, "config", "core.autocrlf", "false")
    _git(target, "add", "--all")
    _git(target, "commit", "--quiet", "--no-gpg-sign", "-m", _SEED_MESSAGE)
    head = _git(target, "rev-parse", "HEAD")
    status = _git(target, "status", "--porcelain")
    if status:
        raise WorkspaceError("the materialised seed is not clean; refusing to hand it to a sandbox")
    return {
        "path": str(target),
        "commit": head,
        "clean": True,
        "files": seed.files,
        "size_bytes": seed.size_bytes,
        "seed_digest": seed.digest,
        "source": str(seed.source),
        "name": seed.name,
        "write_scope": list(seed.write_scope),
    }


def describe_roots(roots: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
    """The workspace menu: every allowed root and the names a run may use."""

    allowed = roots or workspace_roots()
    items: list[dict[str, Any]] = []
    for root in allowed:
        names: list[str] = []
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if entry.name in _EXCLUDED_DIRECTORIES:
                    continue
                names.append(entry.name)
        items.append({"root": str(root), "exists": root.is_dir(), "workspaces": names})
    return items


__all__ = [
    "DEFAULT_WORKSPACE_ROOTS",
    "MAX_BYTES",
    "MAX_FILES",
    "WORKSPACE_ROOTS_ENV",
    "WorkspaceError",
    "WorkspaceSeed",
    "describe_roots",
    "materialise",
    "resolve_seed",
    "validate_name",
    "workspace_roots",
]
