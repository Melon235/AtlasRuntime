"""Opt-in OpenSandbox E2E with the actual application and deterministic model adapter."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlasos.app.deterministic import Script
from atlasos.app.web import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("ATLASOS_TEST_H3_SANDBOX") != "1", reason="requires local OpenSandbox"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    root = tmp_path / "workspaces"
    source = root / "demo"
    (source / "solution").mkdir(parents=True)
    (source / "solution/answer.txt").write_bytes(b"original\n")
    (source / "obsolete.txt").write_bytes(b"to remove\n")
    (source / ".env").write_bytes(b"PLACEHOLDER=EXCLUDED\n")
    _git(source, "init", "--quiet", "--template=")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "add", "--", ".env", "solution/answer.txt", "obsolete.txt")
    _git(
        source,
        "-c",
        "user.name=Atlas Test",
        "-c",
        "user.email=test@atlas.local",
        "commit",
        "--quiet",
        "-m",
        "Initial workspace",
    )
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(root))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-no-model")
    app = create_app(runs_root=tmp_path / "runs", provider="deterministic")
    return TestClient(app, base_url="http://127.0.0.1"), source


def _run(client: TestClient, *, mode: str = "main_direct", new_paths: tuple[str, ...] = ()):
    response = client.post(
        "/api/runs",
        json={
            "task": "Run the offline coding verification.",
            "mode": mode,
            "workspace": "demo",
            "interactive": True,
            "new_paths": new_paths,
            "budget_tokens": 100_000,
        },
    )
    assert response.status_code == 200, response.text
    endpoint = f"/api/runs/{response.json()['run_id']}"
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        run = client.get(endpoint).json()
        if run["finished_at"]:
            break
        time.sleep(0.25)
    else:
        pytest.fail(f"run did not finish: {endpoint}")
    return endpoint, run


def _confirm(review: dict) -> dict:
    return {
        **{key: review[key] for key in ("run_id", "workspace_id", "snapshot_id", "patch_digest")},
        "confirm": True,
    }


def test_direct_applies_text_changes_once(workspace, monkeypatch) -> None:
    client, source = workspace
    head = _git(source, "rev-parse", "HEAD")

    def script(**_):
        return Script(
            main_steps=[
                ("file_write", {"path": "solution/answer.txt", "content": "changed\n"}),
                ("file_write", {"path": "new.txt", "content": "added\n"}),
                ("bash", {"command": "rm -- obsolete.txt"}),
            ]
        )

    monkeypatch.setattr("atlasos.app.service.direct_script", script)
    endpoint, run = _run(client, new_paths=("new.txt",))
    assert run["status"] == "COMPLETED", run
    assert run["live"]["delegation"] is None
    assert (source / "solution/answer.txt").read_bytes() == b"original\n"
    assert not (source / "new.txt").exists()
    isolated = Path(client.app.state.runs_root) / run["run_id"] / "workspace"
    assert not (isolated / ".env").exists()
    review = client.get(endpoint + "/patch").json()
    assert review["state"] == "REVIEW_PENDING", review
    assert review["counts"] == {"added": 1, "modified": 1, "deleted": 1}, review
    assert review["verification"] == "NOT_CONFIGURED"
    confirmation = _confirm(review)
    bad = client.post(endpoint + "/patch/apply", json={**confirmation, "patch_digest": "wrong"})
    assert bad.status_code == 409
    unconfirmed = client.post(endpoint + "/patch/apply", json={**confirmation, "confirm": False})
    assert unconfirmed.status_code == 409
    cross_origin = client.post(
        endpoint + "/patch/apply", json=confirmation, headers={"Origin": "https://example.invalid"}
    )
    assert cross_origin.status_code == 403
    assert client.get(endpoint + "/patch").json()["state"] == "REVIEW_PENDING"
    applied = client.post(endpoint + "/patch/apply", json=confirmation)
    assert applied.status_code == 200, applied.text
    assert applied.json()["changed_paths"] == ["new.txt", "obsolete.txt", "solution/answer.txt"]
    assert applied.json()["state"] == "APPLIED"
    assert (source / "solution/answer.txt").read_bytes() == b"changed\n"
    assert (source / "new.txt").read_bytes() == b"added\n"
    assert not (source / "obsolete.txt").exists()
    assert _git(source, "rev-parse", "HEAD") == head
    assert not _git(source, "diff", "--cached", "--name-only")
    second_client = TestClient(
        create_app(runs_root=client.app.state.runs_root), base_url="http://localhost"
    )
    repeat = second_client.post(endpoint + "/patch/apply", json=confirmation)
    assert repeat.json() == applied.json()
    readonly = TestClient(
        create_app(runs_root=client.app.state.runs_root, read_only=True),
        base_url="http://127.0.0.1",
    )
    assert readonly.post(endpoint + "/patch/apply", json=confirmation).status_code == 403
    link = client.get(endpoint + "/replay").json()
    assert link["available"]
    assert client.get(f"/api/replay/runs/{link['replay_run_key']}").status_code == 200


def test_dynamic_routes_and_rejects_real_export(workspace) -> None:
    client, source = workspace
    paths = ("solution/alpha.txt", "solution/beta.txt", "solution/integration.txt")
    endpoint, run = _run(client, mode="main_dynamic", new_paths=paths)
    assert run["status"] == "COMPLETED", run
    assert run["metrics"]["max_observed_child_concurrency"] == 2
    assert len(run["tasks"]) == 2
    assert run["metrics"]["handoff_payload_chars"] > 0
    review = client.get(endpoint + "/patch").json()
    assert sorted(file["path"] for file in review["files"]) == sorted(paths)
    rejected = client.post(endpoint + "/patch/reject", json=_confirm(review))
    assert rejected.json()["state"] == "REJECTED"
    assert not _git(source, "status", "--porcelain")


def test_conflict_preserves_user_changes_and_patch(workspace) -> None:
    client, source = workspace
    endpoint, _ = _run(client)
    review = client.get(endpoint + "/patch").json()
    target = source / "solution/answer.txt"
    target.write_bytes(b"user changes\n")
    response = client.post(endpoint + "/patch/apply", json=_confirm(review))
    assert response.status_code == 409
    assert response.json()["state"] == "CONFLICT"
    assert target.read_bytes() == b"user changes\n"
    saved = client.get(endpoint + "/patch").json()
    assert saved["patch_digest"] == review["patch_digest"] and saved["files"] == review["files"]


def test_provider_failure_and_untrusted_paths(workspace) -> None:
    client, source = workspace
    client.app.state.service.failure_after_request = 2
    endpoint, run = _run(client)
    assert run["status"] == "FAILED" and run["live"]["execution_succeeded"] is False
    assert client.get(endpoint + "/patch").json()["state"] == "UNAVAILABLE"
    proof = Path(client.app.state.runs_root) / run["run_id"] / f"goal-{run['run_id']}"
    journal = json.loads((proof / "provider_requests.json").read_bytes())
    assert journal["records"][-1]["status"] == "FAILED"
    assert journal["records"][-1]["usage"] is None
    assert len(journal["records"]) == 2
    assert not _git(source, "status", "--porcelain")
    invalid = client.post(
        "/api/runs",
        json={
            "task": "x",
            "interactive": True,
            "mode": "main_direct",
            "workspace": "demo",
            "new_paths": ["../outside"],
        },
    )
    assert invalid.status_code == 422
