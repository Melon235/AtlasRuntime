"""Review and apply a completed sandbox export under an explicit workspace grant."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atlasos.app.workspace import (
    WorkspaceError,
    WorkspaceSeed,
    check_git_directory,
    check_target_path,
    git_status,
    run_git,
    validate_patch_path,
)

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PREVIEW = 120_000
MAX_CHANGES = 500
_DIFF = re.compile(r"diff --git a/([A-Za-z0-9_./-]+) b/([A-Za-z0-9_./-]+)")
_INDEX = re.compile(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?")
_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: .*)?")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class PatchReviewError(ValueError):
    """A review or confirmation that cannot safely be carried out."""


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _changes(text: str, scope: set[str]) -> list[dict[str, Any]]:
    lines = text.removesuffix("\n").split("\n")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    index = 0
    preview = MAX_PREVIEW
    while index < len(lines):
        start = index
        match = _DIFF.fullmatch(lines[index])
        if match is None or match[1] != match[2]:
            raise PatchReviewError("unsupported path, rename or copy in exported patch")
        path = validate_patch_path(match[1])
        if path not in scope:
            raise PatchReviewError(f"path outside the authorized write scope: {path}")
        if path.lower() in seen or len(files) >= MAX_CHANGES:
            raise PatchReviewError("duplicate paths or too many files in exported patch")
        seen.add(path.lower())
        status = "modified"
        index += 1
        if index < len(lines) and lines[index] in (
            "new file mode 100644",
            "deleted file mode 100644",
        ):
            status = "added" if lines[index].startswith("new") else "deleted"
            index += 1
        if index < len(lines) and _INDEX.fullmatch(lines[index]):
            index += 1
        old_path = "/dev/null" if status == "added" else f"a/{path}"
        new_path = "/dev/null" if status == "deleted" else f"b/{path}"
        insertions = deletions = 0
        if lines[index : index + 2] == [f"--- {old_path}", f"+++ {new_path}"]:
            index += 2
            if index >= len(lines) or not _HUNK.fullmatch(lines[index]):
                raise PatchReviewError("patch has no unified diff hunk")
            while index < len(lines) and (hunk := _HUNK.fullmatch(lines[index])) is not None:
                old_count, new_count = (
                    int(value) if value is not None else 1 for value in hunk.groups()
                )
                index += 1
                while index < len(lines) and (old_count or new_count):
                    marker = lines[index][:1]
                    index += 1
                    if marker not in {" ", "+", "-"}:
                        raise PatchReviewError("invalid unified diff hunk")
                    old_count -= marker != "+"
                    new_count -= marker != "-"
                    if old_count < 0 or new_count < 0:
                        raise PatchReviewError("invalid unified diff hunk length")
                    insertions += marker == "+"
                    deletions += marker == "-"
                    if index < len(lines) and lines[index] == r"\ No newline at end of file":
                        index += 1
                if old_count or new_count:
                    raise PatchReviewError("incomplete unified diff hunk")
        elif index < len(lines) and (
            "binary" in lines[index].lower() or lines[index] == "GIT binary patch"
        ):
            raise PatchReviewError(f"unsupported binary change: {path}")
        elif status == "modified" or (
            index < len(lines) and not lines[index].startswith("diff --git ")
        ):
            raise PatchReviewError(f"unsupported file mode, type or patch header: {path}")
        full_diff = "\n".join(lines[start:index]) + "\n"
        shown = full_diff[: min(preview, 24_000)]
        preview -= len(shown)
        files.append(
            {
                "path": path,
                "status": status,
                "insertions": insertions,
                "deletions": deletions,
                "diff": _CONTROLS.sub(lambda match: ascii(match[0])[1:-1], shown),
                "truncated": shown != full_diff,
            }
        )
    return files


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise PatchReviewError(
            "another review operation is in progress or requires inspection"
        ) from error
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()


def _save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PatchReview:
    def __init__(self, run_dir: Path, workspace: dict[str, Any], session: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.workspace = workspace
        self.session = session
        self.decision_path = run_dir / "patch-review.json"

    def _review(self) -> tuple[dict[str, Any], bytes]:
        result: dict[str, Any] = {"state": "UNAVAILABLE", "files": [], "run_id": self.run_dir.name}
        if not self.workspace.get("workspace_id"):
            return {**result, "message": "This run has no interactive workspace snapshot."}, b""
        if (
            self.session.get("status") != "succeeded"
            or self.session.get("execution_succeeded") is not True
        ):
            return {
                **result,
                "message": "A completed successful run is required for patch review.",
            }, b""
        artifacts = self.session.get("artifacts") or {}
        digest = artifacts.get("patch_digest")
        sandbox = self.session.get("sandbox") or {}
        try:
            if (
                not digest
                or digest != sandbox.get("patch_digest")
                or self.session.get("isolation") != "sandbox"
            ):
                raise PatchReviewError("no bound sandbox patch export is available")
            runtime_dir = self.run_dir / f"goal-{self.run_dir.name}"
            check_target_path(self.run_dir, f"{runtime_dir.name}/workspace.patch")
            with (runtime_dir / "workspace.patch").open("rb") as stream:
                patch = stream.read(MAX_PATCH_BYTES + 1)
            if len(patch) > MAX_PATCH_BYTES:
                raise PatchReviewError("exported patch exceeds the review size limit")
            if digest != "sha256:" + hashlib.sha256(patch).hexdigest():
                raise PatchReviewError(
                    "exported patch digest no longer matches the runtime artifact"
                )
            if not patch:
                raise PatchReviewError("the sandbox exported no changes")
            if not patch.endswith(b"\n") or b"\x00" in patch:
                raise PatchReviewError("unsupported patch encoding or format")
            files = _changes(patch.decode("utf-8"), set(self.workspace["write_scope"]))
        except (OSError, WorkspaceError, ValueError) as error:
            return {**result, "message": str(error)}, b""
        acceptance = self.session.get("acceptance")
        verification = acceptance.get("verdict") if isinstance(acceptance, dict) else acceptance
        result.update(
            state="REVIEW_PENDING",
            files=files,
            workspace_id=self.workspace["workspace_id"],
            snapshot_id=self.workspace["snapshot_id"],
            source=self.workspace["source"],
            base_head=self.workspace["head"],
            patch_digest=digest,
            verification=verification or "NOT_CONFIGURED",
            counts={
                status: sum(item["status"] == status for item in files)
                for status in ("added", "modified", "deleted")
            },
            insertions=sum(item["insertions"] for item in files),
            deletions=sum(item["deletions"] for item in files),
        )
        if self.decision_path.is_file():
            decision = json.loads(self.decision_path.read_text(encoding="utf-8"))
            if (
                decision.get("patch_digest") != digest
                or decision.get("workspace_id") != result["workspace_id"]
            ):
                return {
                    **result,
                    "state": "UNAVAILABLE",
                    "message": "review decision provenance is inconsistent",
                }, b""
            result.update(decision)
        return result, patch

    def view(self) -> dict[str, Any]:
        return self._review()[0]

    def decide(
        self,
        action: str,
        binding: dict[str, str],
        *,
        confirm: bool,
        resolve: Callable[[], WorkspaceSeed],
    ) -> dict[str, Any]:
        if action not in {"apply", "reject"} or not confirm:
            raise PatchReviewError("an explicit patch decision is required")
        with _lock(self.run_dir / "patch-review.lock"):
            review, patch = self._review()
            if review["state"] == "UNAVAILABLE":
                raise PatchReviewError(review["message"])
            fields = ("run_id", "workspace_id", "snapshot_id", "patch_digest")
            if any(binding.get(key) != review[key] for key in fields):
                raise PatchReviewError("confirmation does not match the displayed patch")
            state = review["state"]
            if (
                state == "APPLIED"
                and action == "apply"
                or state == "REJECTED"
                and action == "reject"
            ):
                return review
            if state not in {"REVIEW_PENDING", "CONFLICT"}:
                raise PatchReviewError(f"this patch has already reached {state}")
            if action == "reject":
                return self._record(review, "REJECTED", message="Patch rejected.")
            if state == "CONFLICT":
                return review
            try:
                source = self._verify_source(resolve, review)
            except (OSError, WorkspaceError, PatchReviewError, subprocess.SubprocessError) as error:
                return self._record(review, "CONFLICT", message=str(error))
            with _lock(source / ".git" / "atlas-workspace-apply.lock"):
                return self._apply(review, patch, resolve)

    def _verify_source(self, resolve: Callable[[], WorkspaceSeed], review: dict[str, Any]) -> Path:
        source = resolve()
        if source.as_dict() != self.workspace:
            raise PatchReviewError("the original workspace or its base snapshot changed")
        check_git_directory(source.source)
        for item in review["files"]:
            check_target_path(source.source, item["path"])
        return source.source

    def _apply(
        self, review: dict[str, Any], patch: bytes, resolve: Callable[[], WorkspaceSeed]
    ) -> dict[str, Any]:
        try:
            source = self._verify_source(resolve, review)
            checked = run_git(source, "apply", "--check", "--whitespace=nowarn", "-", data=patch)
            if checked.returncode:
                return self._record(
                    review, "CONFLICT", message="Git could not apply the patch to the clean base."
                )
            self._verify_source(resolve, review)
        except (OSError, WorkspaceError, PatchReviewError, subprocess.SubprocessError) as error:
            return self._record(review, "CONFLICT", message=str(error))
        self._record(
            review,
            "APPLY_FAILED",
            message="Apply outcome was interrupted or has not been verified.",
        )
        try:
            applied = run_git(source, "apply", "--whitespace=nowarn", "-", data=patch)
            status = sorted(entry for entry in git_status(source).split("\0") if entry)
            actual = sorted(entry[3:] for entry in status)
            expected_code = {"added": "??", "modified": " M", "deleted": " D"}
            expected = sorted(
                f"{expected_code[item['status']]} {item['path']}" for item in review["files"]
            )
            reverses = run_git(
                source, "apply", "--reverse", "--check", "--whitespace=nowarn", "-", data=patch
            )
            head = run_git(source, "rev-parse", "HEAD")
            if (
                applied.returncode
                or reverses.returncode
                or status != expected
                or head.stdout.strip().decode() != review["base_head"]
            ):
                return self._record(
                    review,
                    "APPLY_FAILED",
                    actual=actual,
                    message="Git apply or post-apply verification failed. Inspect the workspace.",
                )
        except (OSError, WorkspaceError, subprocess.SubprocessError) as error:
            return self._record(review, "APPLY_FAILED", message=str(error))
        return self._record(
            review, "APPLIED", actual=actual, message="Patch applied to the original workspace."
        )

    def _record(
        self, review: dict[str, Any], state: str, *, message: str, actual: list[str] | None = None
    ) -> dict[str, Any]:
        decision = {
            key: review[key]
            for key in ("run_id", "workspace_id", "snapshot_id", "patch_digest", "source")
        }
        decision.update(state=state, message=message, changed_paths=actual, decided_at=_stamp())
        _save(self.decision_path, decision)
        return {**review, **decision}
