"""The application only exposes unambiguous text changes in the authorized scope."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from atlasos.app.patch_review import PatchReviewError, _changes
from atlasos.app.workspace import WorkspaceError, check_target_path


def _add(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+hello\n"
    )


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "C:/absolute", "safe/../escape", "aux.txt"]
)
def test_unsafe_patch_paths(path: str) -> None:
    with pytest.raises((PatchReviewError, WorkspaceError)):
        _changes(_add(path), {path})


def test_protected_unauthorized_binary_and_mode_changes() -> None:
    with pytest.raises(WorkspaceError, match="protected"):
        _changes(_add("docs/.env.production"), {"docs/.env.production"})
    with pytest.raises(PatchReviewError, match="scope"):
        _changes(_add("unapproved.txt"), {"approved.txt"})
    with pytest.raises(PatchReviewError, match="binary"):
        _changes("diff --git a/file b/file\nGIT binary patch\n", {"file"})
    with pytest.raises(PatchReviewError, match="mode"):
        _changes("diff --git a/file b/file\nold mode 100644\nnew mode 100755\n", {"file"})
    with pytest.raises(PatchReviewError, match="hunk"):
        _changes(_add("file").replace("+1 @@", "+1,2 @@"), {"file"})


def test_target_path_refuses_linked_parent(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = checkout / "link"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)], check=True)
    else:
        linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="unsupported entry"):
        check_target_path(checkout, "link/target.txt")
