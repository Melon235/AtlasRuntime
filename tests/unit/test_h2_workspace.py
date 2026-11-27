"""Workspace materialisation: name validation, containment, seed identity, write scope."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from atlasos.app.workspace import (
    REPO_ROOT,
    WorkspaceError,
    describe_roots,
    materialise,
    resolve_seed,
    validate_name,
    workspace_roots,
)


def _seed(root: Path, name: str = "demo", *, files: dict[str, str] | None = None) -> Path:
    target = root / name
    target.mkdir(parents=True)
    payload = files or {
        "README.md": "# demo\n",
        "solution/keep.txt": "keep\n",
        "notes/todo.md": "todo\n",
    }
    for relative, content in payload.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target


def test_name_must_be_one_plain_segment(tmp_path: Path) -> None:
    for bad in ("", "..", ".", "../etc", "a/b", "a\\b", "C:", "demo/../other", ".hidden"):
        with pytest.raises(WorkspaceError):
            validate_name(bad)
    assert validate_name(" demo ") == "demo"


def test_absolute_paths_and_outside_roots_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(root)

    assert resolve_seed("demo", roots=(root,)).name == "demo"
    for bad in ("missing", "C:/Windows", "../roots/demo"):
        with pytest.raises(WorkspaceError):
            resolve_seed(bad, roots=(root,))


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating a directory symlink here")

    with pytest.raises(WorkspaceError):
        resolve_seed("linked", roots=(root,))


def test_the_runtime_checkout_is_never_a_workspace(tmp_path: Path) -> None:
    """Even if the repository is inside a configured root, its tree is refused."""

    with pytest.raises(WorkspaceError):
        resolve_seed(REPO_ROOT.name, roots=(REPO_ROOT.parent,))
    # The repository's own fixtures are inside it and must not be accepted through that root.
    with pytest.raises(WorkspaceError):
        resolve_seed("experiments", roots=(REPO_ROOT,))


def test_nested_link_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    workspace = _seed(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "solution" / "linked"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="unsupported entry"):
        resolve_seed("demo", roots=(root,))


def test_write_scope_declares_directories_and_top_level_files(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(root)

    seed = resolve_seed("demo", roots=(root,))

    assert "solution/" in seed.write_scope
    assert "notes/" in seed.write_scope
    assert "README.md" in seed.write_scope
    assert all("\\" not in entry for entry in seed.write_scope)
    assert seed.files == 3


def test_seed_digest_is_stable_and_content_addressed(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(root, "one")
    _seed(root, "two")

    first = resolve_seed("one", roots=(root,))
    second = resolve_seed("two", roots=(root,))
    assert first.digest == second.digest, "identical trees produce one seed identity"

    (root / "two" / "README.md").write_text("# changed\n", encoding="utf-8")
    changed = resolve_seed("two", roots=(root,))
    assert changed.digest != first.digest


def test_materialised_seed_is_a_clean_one_commit_repository(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(root)
    seed = resolve_seed("demo", roots=(root,))
    target = tmp_path / "run" / "workspace"

    record = materialise(seed, target)

    assert record["clean"] is True
    assert len(record["commit"]) == 40
    assert (target / "solution" / "keep.txt").is_file()
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.stdout.strip() == ""
    log = subprocess.run(
        ["git", "-C", str(target), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert len(log.stdout.strip().splitlines()) == 1


def test_materialise_refuses_an_existing_target(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(root)
    seed = resolve_seed("demo", roots=(root,))
    target = tmp_path / "workspace"
    target.mkdir()

    with pytest.raises(WorkspaceError):
        materialise(seed, target)


def test_vcs_caches_and_env_files_are_not_copied(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    _seed(
        root,
        files={
            "app.py": "print('x')\n",
            ".env": "SECRET=1\n",
            "__pycache__/app.cpython-312.pyc": "binary",
            ".git/config": "[core]\n",
        },
    )
    seed = resolve_seed("demo", roots=(root,))
    record = materialise(seed, tmp_path / "workspace")

    assert record["files"] == 1
    assert not (tmp_path / "workspace" / ".env").exists()
    assert not (tmp_path / "workspace" / "__pycache__").exists()
    assert (tmp_path / "workspace" / ".git").is_dir(), "the new repository is created, not copied"


def test_environment_roots_are_used_when_configured(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "custom"
    root.mkdir()
    _seed(root)
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(root))

    assert workspace_roots() == (root.resolve(),)
    menu = describe_roots()
    assert menu and menu[0]["workspaces"] == ["demo"]
    assert os.path.isdir(menu[0]["root"])
