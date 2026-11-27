"""Stage H2: the Main-driven path through the real application API and the replay index.

Every test here submits through the application service, waits for the runtime's own terminal
evidence, and then reads the *same* run back through the replay projection. The provider is the
deterministic one, so no network client is built; the sandbox is off, so no container is created.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlasos.app.replay.service import ReplayService
from atlasos.app.replay.sources import EvidenceRoot
from atlasos.app.service import AtlasApplicationService
from atlasos.app.web import create_app

TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}


def _workspace(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures" / "demo"
    (target / "solution").mkdir(parents=True, exist_ok=True)
    (target / "solution" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (target / "README.md").write_text("# demo\n", encoding="utf-8")
    return target


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(tmp_path / "fixtures"))
    _workspace(tmp_path)
    return create_app(
        runs_root=tmp_path / "runs",
        provider="deterministic",
        sandbox_enabled=False,
    )


def _submit(client: TestClient, **body) -> dict:
    payload = {
        "task": "Write the deliverable for the goal.",
        "mode": "main_direct",
        "workspace": "demo",
        "max_turns": 8,
        "budget_tokens": 50_000,
        "owner_note": "ignored",
    }
    payload.update(body)
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _wait(client: TestClient, run_id: str, *, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["status"] in TERMINAL:
            return detail
        time.sleep(0.25)
    raise AssertionError(f"{run_id} did not finish in {timeout}s")


# ------------------------------------------------------------------------- direct


def test_direct_run_completes_and_is_the_same_run_in_replay(app) -> None:
    client = TestClient(app)
    submitted = _submit(client, mode="main_direct")
    detail = _wait(client, submitted["run_id"])

    assert detail["status"] == "COMPLETED", detail["error"]
    assert detail["config"]["delegation"]["enabled"] is False
    # With delegation disabled the runtime builds no delegation plane at all (not an empty one).
    assert detail["live"]["delegation"] is None
    assert "solution/answer.txt" in _workspace_files(app, submitted["run_id"])
    assert any(
        event["type"] == "runtime_event"
        and (event["data"] or {}).get("event_type") == "action_proposed"
        for event in detail["events"]
    )

    link = client.get(f"/api/runs/{submitted['run_id']}/replay").json()
    assert link["available"] is True
    overview = client.get(f"/api/replay/runs/{link['replay_run_key']}").json()
    assert overview["execution"]["mode"] == "main_direct"
    assert overview["model"]["provider_kind"] == "SCRIPTED"
    assert overview["verification_summary"]["axes"]["independent_oracle"] == "NOT_CONFIGURED"
    assert overview["verification_summary"]["headline"] == "COMPLETED_UNVERIFIED"

    listing = client.get("/api/replay/runs?source=app_run&limit=50").json()
    assert listing["total"] == 1, "the live run must not appear twice in the replay index"


# ------------------------------------------------------------------------ dynamic


def test_dynamic_run_delegates_two_children_and_keeps_its_handoffs(app) -> None:
    client = TestClient(app)
    submitted = _submit(client, mode="main_dynamic", max_turns=12)
    detail = _wait(client, submitted["run_id"])

    assert detail["status"] == "COMPLETED", detail["error"]
    live = detail["live"]
    batches = live["delegation"]["batches"]
    assert batches and batches[0]["route"] == "delegate_parallel"
    children = batches[0]["children"]
    assert [child["key"] for child in children] == ["alpha", "beta"]
    assert all(child["success"] is True for child in children)
    assert len({child["ref_id"] for child in children}) == 2, "one handoff per child"

    files = _workspace_files(app, submitted["run_id"])
    assert {"solution/alpha.txt", "solution/beta.txt", "solution/integration.txt"} <= set(files)

    link = client.get(f"/api/runs/{submitted['run_id']}/replay").json()
    delegation = client.get(f"/api/replay/runs/{link['replay_run_key']}/delegation").json()
    assert delegation["state"] == "DELEGATION_OBSERVED"
    assert delegation["router"]["route"] == "delegate_parallel"
    assert len(delegation["children"]) == 2
    timeline = client.get(f"/api/replay/runs/{link['replay_run_key']}/timeline?limit=2000").json()
    event_types = {event["event_type"] for event in timeline["events"]}
    assert "action_proposed" in event_types
    assert any(event["agent_id"] for event in timeline["events"]), "worker lanes are real"


def _workspace_files(app, run_id: str) -> list[str]:
    workspace = Path(app.state.runs_root) / run_id / "workspace"
    return sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file() and ".git/" not in path.relative_to(workspace).as_posix()
    )


# ------------------------------------------------------------------ provider failure


def test_provider_failure_is_journaled_without_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(tmp_path / "fixtures"))
    _workspace(tmp_path)
    service = AtlasApplicationService(
        runs_root=tmp_path / "runs",
        provider="deterministic",
        sandbox_enabled=False,
        failure_after_request=2,
    )
    submitted = service.submit(
        "Write the deliverable.",
        mode="main_direct",
        workspace="demo",
        max_turns=8,
        budget_tokens=50_000,
    )
    detail = _wait_service(service, submitted["run_id"])

    assert detail["status"] == "FAILED"
    assert detail["live"]["execution_succeeded"] is False
    journal = _journal(service.runs_root / submitted["run_id"])
    records = journal["records"]
    assert journal["counts"]["FAILED"] >= 1
    assert records[-1]["status"] != "COMPLETED", "the run ended on a failed request"
    assert all(entry["exception_type"] for entry in records if entry["status"] == "FAILED")
    # The application submits one run. The loop's own retries live inside that run and are
    # visible in the journal; nothing resubmits the task.
    assert len([path for path in (service.runs_root).iterdir() if path.is_dir()]) == 1
    failure = detail["live"]["failure"] or {}
    assert failure.get("kind"), "the failure is classified, not left as a bare string"

    replay = ReplayService([EvidenceRoot(label="runs", path=service.runs_root)])
    key = replay.find_run_key("runs", submitted["run_id"])
    assert key is not None
    axes = replay.verification(key)["axes"]
    assert axes["effective_agent_completion"] is False
    assert axes["independent_oracle"] == "NOT_CONFIGURED"


def _wait_service(service: AtlasApplicationService, run_id: str, *, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = service.detail(run_id)
        if detail["status"] in TERMINAL:
            return detail
        time.sleep(0.25)
    raise AssertionError(f"{run_id} did not finish in {timeout}s")


def _journal(run_dir: Path) -> dict:
    import json

    matches = sorted(run_dir.glob("goal*/provider_requests.json"))
    assert matches, "the request journal was not written"
    return json.loads(matches[0].read_text(encoding="utf-8"))


# ------------------------------------------------------------------------ safety


def test_read_only_mode_refuses_live_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(tmp_path / "fixtures"))
    _workspace(tmp_path)
    app = create_app(
        runs_root=tmp_path / "runs",
        provider="deterministic",
        sandbox_enabled=False,
        read_only=True,
    )
    client = TestClient(app)

    assert client.post("/api/runs", json={"task": "x", "mode": "main_direct"}).status_code == 403
    assert client.post("/api/runs/app-1/cancel").status_code == 403
    assert client.get("/api/workspaces").status_code == 200


def test_cancel_is_refused_with_an_explanation(app) -> None:
    client = TestClient(app)
    submitted = _submit(client, mode="main_direct")
    response = client.post(f"/api/runs/{submitted['run_id']}/cancel")
    _wait(client, submitted["run_id"])
    assert response.status_code == 409
    assert "CANCEL_UNAVAILABLE" in response.json()["error"]


def test_no_secret_reaches_the_run_projection(app, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-should-never-appear")
    client = TestClient(app)
    submitted = _submit(client, mode="main_direct")
    detail = _wait(client, submitted["run_id"])
    import json

    assert "sk-live-should-never-appear" not in json.dumps(detail)


def test_existing_api_contracts_are_unchanged(app) -> None:
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/approvals").status_code == 200
    assert client.get("/replay").status_code == 200
    assert client.get("/").status_code == 200
