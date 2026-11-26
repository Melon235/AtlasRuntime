"""Stage H1 replay API tests: read-only endpoints and existing-contract compatibility."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from atlasos.app.replay.sources import EvidenceRoot
from atlasos.app.web import create_app

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz"


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _seed_evidence(root: Path) -> str:
    cell = root / "stage-c1c" / "evidence" / "cells" / "c01"
    _write(
        cell / "analysis.json",
        {
            "cell_id": "c1c.demo.C3.r1",
            "plan_id": "stage-c1c-demo",
            "scope": "TEST_SCOPE",
            "arm_id": "C3",
            "task_id": "demo-task",
            "repetition": 1,
            "outcome": {
                "cell_id": "c1c.demo.C3.r1",
                "arm_id": "C3",
                "task_id": "demo-task",
                "profile_id": "coding_worker",
                "status": "succeeded",
                "agent_success": True,
                "public_test_status": "PASS",
                "static_acceptance": "PASS",
                "oracle_passed": False,
                "oracle_status": "FAIL",
                "goal_correctness": False,
                "execution_integrity": "PASS",
                "executor_verdict": "MEASURED_FAILURE",
                "checks": [],
                "stops": [],
                "cost_usd": 0.01,
                "duration_s": 2.0,
                "usage": {
                    "main_cumulative_input_tokens": 1000,
                    "main_completion_tokens": 100,
                    "child_total_prompt_tokens": 600,
                    "child_completion_tokens": 60,
                    "system_total_tokens": 1760,
                },
            },
            "oracle": {
                "status": "FAIL",
                "passed": False,
                "kind": "HIDDEN_TESTS_FAILED",
                "execution_mode": "sandbox",
                "returncode": 1,
            },
            "duration_s": 2.0,
        },
    )
    _write(
        cell / "attempt.json",
        {
            "cell_id": "c1c.demo.C3.r1",
            "plan_id": "stage-c1c-demo",
            "state": "CLAIMED",
            "attempt_limit": 1,
            "claimed_at": "2026-11-25T11:32:00Z",
        },
    )
    agent = cell / "agent" / "bplus-c01"
    _write(
        agent / "session.json",
        {
            "run_id": "bplus-c01",
            "mode": "coding",
            "goal": f"replay goal key={SECRET}",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "status": "succeeded",
            "execution_succeeded": True,
            "failure": None,
            "main": {
                "agent": "atlas-main",
                "worker_id": "w-main",
                "task_id": "main",
                "success": True,
                "stop_reason": "success",
                "turns": 3,
                "tool_calls": ["bash", "delegate_tasks"],
                "requests": 2,
                "total_prompt_tokens": 1000,
                "completion_tokens": 100,
                "peak_context_tokens": 500,
            },
            "delegation": {
                "run_id": "bplus-c01",
                "enabled": True,
                "limits": {"child_max_turns": 20},
                "batches": [
                    {
                        "round": 1,
                        "route": "delegate_parallel",
                        "decision": {
                            "route": "delegate_parallel",
                            "reason_codes": ["DELEGATION_ADMITTED"],
                            "blocked_gates": [],
                            "policy_version": "m.1.0",
                        },
                        "children": [
                            {
                                "key": "t1",
                                "worker_id": "w-1",
                                "profile_id": "coding_worker",
                                "status": "completed",
                                "success": True,
                                "stop_reason": "success",
                                "turns": 4,
                                "tool_calls": 5,
                                "started_offset_ms": 10.0,
                                "finished_offset_ms": 2000.0,
                                "total_prompt_tokens": 600,
                                "completion_tokens": 60,
                                "ref_id": "ho-1",
                            }
                        ],
                        "totals": {"children_started": 1},
                        "batch_span_ms": 2000.0,
                        "max_observed_concurrency": 1,
                    }
                ],
                "totals": {"children_started": 1},
                "started_at": "2026-11-25T11:32:36Z",
                "finished_at": "2026-11-25T11:32:38Z",
            },
            "delegation_enabled": True,
            "child_tokens": {
                "children_started": 1,
                "total_prompt_tokens": 600,
                "completion_tokens": 60,
            },
            "metrics": {
                "main_requests": 2,
                "main_peak_context_tokens": 500,
                "main_cumulative_input_tokens": 1000,
                "main_context_tokens_by_request": [400, 500],
                "main_cache_hit_tokens": 800,
                "main_cache_miss_tokens": 200,
                "main_completion_tokens": 100,
                "child_total_prompt_tokens": 600,
                "child_completion_tokens": 60,
                "system_total_tokens": 1760,
                "delegation_rounds": 1,
                "max_observed_child_concurrency": 1,
                "unavailable_fields": [],
            },
            "sandbox": {"provider": "opensandbox"},
            "verification": {"verdict": "PASS", "static_status": "PASS"},
            "public_tests": {"runs": [{"call_index": 1, "status": "PASS", "exit_code": 0}]},
            "budget": {"mode": "guarded", "limit_tokens": 10000, "used_tokens": 1760},
            "ownership": {"mode": "enforce", "workers": {"w-1": {"paths": ["src/a.py"]}}},
            "tool_surface": ["bash", "delegate_tasks"],
        },
    )
    (agent / "workspace.patch").write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n", encoding="utf-8"
    )
    return "c1c.demo.C3.r1"


def _client(tmp_path: Path, *, roots: bool = True, read_only: bool = False) -> TestClient:
    runs = tmp_path / "app-runs"
    runs.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir(exist_ok=True)
    replay_roots = [EvidenceRoot(label="runs", path=evidence)] if roots else []
    return TestClient(create_app(runs_root=runs, replay_roots=replay_roots, read_only=read_only))


def _key(client: TestClient) -> str:
    listing = client.get("/api/replay/runs").json()
    assert listing["total"] == 1
    return listing["runs"][0]["run_key"]


def test_replay_sources_and_run_list(tmp_path: Path) -> None:
    _seed_evidence(tmp_path / "evidence")
    client = _client(tmp_path)

    sources = client.get("/api/replay/sources")
    assert sources.status_code == 200
    payload = sources.json()
    assert payload["mode"] == "READ_ONLY_HISTORICAL_REPLAY"
    assert payload["roots"][0]["label"] == "runs"
    assert payload["stats"]["runs"] == 1

    listing = client.get("/api/replay/runs")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    run = body["runs"][0]
    assert run["cell_id"] == "c1c.demo.C3.r1"
    assert run["status"]["headline"] == "NOT_DELIVERED"
    assert run["completeness"]["level"] in {"COMPLETE", "PARTIAL"}
    assert body["facets"]["stages"] == ["runs"]


def test_every_view_endpoint_answers(tmp_path: Path) -> None:
    _seed_evidence(tmp_path / "evidence")
    client = _client(tmp_path)
    key = _key(client)

    for suffix, expected in (
        ("", "run_key"),
        ("/agents", "agents"),
        ("/timeline", "events"),
        ("/delegation", "state"),
        ("/context", "tokens"),
        ("/tools", "rows"),
        ("/artifacts", "patch"),
        ("/verification", "axes"),
        ("/requests", "state"),
    ):
        response = client.get(f"/api/replay/runs/{key}{suffix}")
        assert response.status_code == 200, suffix
        assert expected in response.json(), suffix

    page = client.get("/replay")
    assert page.status_code == 200
    assert "AtlasRuntime" in page.text
    assert "/api/replay/runs" in page.text
    # The workbench must not contain a submission form or an auto-approval loop.
    assert "setInterval" not in page.text
    assert "<form" not in page.text


def test_filters_pagination_and_errors(tmp_path: Path) -> None:
    _seed_evidence(tmp_path / "evidence")
    client = _client(tmp_path)
    key = _key(client)

    assert client.get("/api/replay/runs", params={"stage": "nope"}).json()["total"] == 0
    assert client.get("/api/replay/runs", params={"mode": "coding"}).json()["total"] == 1
    assert client.get("/api/replay/runs", params={"q": "demo-task"}).json()["total"] == 1
    paged = client.get("/api/replay/runs", params={"limit": 1, "offset": 5}).json()
    assert paged["runs"] == [] and paged["truncated"] is False
    assert client.get("/api/replay/runs", params={"limit": 0}).status_code == 422

    missing = client.get("/api/replay/runs/not-a-run")
    assert missing.status_code == 404
    assert missing.json()["kind"] == "ReplayRunNotFound"
    assert client.get("/api/replay/runs/not-a-run/agents").status_code == 404

    # A traversal attempt is not a run key and cannot reach the filesystem.
    traversal = client.get("/api/replay/runs/..%2F..%2Fsecret")
    assert traversal.status_code == 404

    timeline = client.get(
        f"/api/replay/runs/{key}/timeline", params={"limit": 1, "offset": 0}
    ).json()
    assert len(timeline["events"]) == 1
    assert timeline["total"] >= 1


def test_no_evidence_root_is_an_explicit_409(tmp_path: Path) -> None:
    client = _client(tmp_path, roots=False)
    response = client.get("/api/replay/runs")
    assert response.status_code == 409
    assert response.json()["kind"] == "ReplayNotConfigured"
    assert client.get("/replay").status_code == 200


def test_secrets_are_not_exposed_through_the_api(tmp_path: Path) -> None:
    _seed_evidence(tmp_path / "evidence")
    client = _client(tmp_path)
    key = _key(client)
    for suffix in (
        "",
        "/agents",
        "/timeline",
        "/delegation",
        "/context",
        "/tools",
        "/artifacts",
        "/verification",
        "/requests",
    ):
        text = client.get(f"/api/replay/runs/{key}{suffix}").text
        assert SECRET not in text, suffix


def test_existing_app_api_is_unchanged(tmp_path: Path) -> None:
    run = tmp_path / "app-runs" / "app-web"
    _write(
        run / "run.json",
        {
            "run_id": "app-web",
            "mode": "workflow",
            "task": "existing task",
            "created_at": "2026-11-09T00:00:00+00:00",
            "updated_at": "2026-11-09T00:00:01+00:00",
            "status": "COMPLETED",
        },
    )
    _write(run / "result.json", {"final_output": "web answer", "metrics": {"tool_calls": 0}})
    (run / "events.jsonl").write_text(
        json.dumps({"id": 1, "type": "run_submitted", "timestamp": "2026-11-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/runs").json()[0]["run_id"] == "app-web"
    assert client.get("/api/runs/app-web").json()["final_output"] == "web answer"
    assert client.get("/api/runs/app-web/events").json()[0]["id"] == 1
    assert client.get("/").status_code == 200
    assert client.get("/history").status_code == 200
    assert client.get("/approvals").status_code == 200
    assert client.get("/api/approvals").status_code == 200
    assert client.post("/api/approvals/none/approve").status_code == 409


def test_read_only_mode_refuses_mutating_routes(tmp_path: Path) -> None:
    _seed_evidence(tmp_path / "evidence")
    client = _client(tmp_path, read_only=True)
    key = _key(client)

    refused = client.post("/api/runs", json={"task": "x", "mode": "workflow"})
    assert refused.status_code == 403
    assert refused.json()["kind"] == "ReadOnlyMode"
    assert client.post("/api/approvals/none/approve").status_code == 403
    assert client.post("/api/approvals/none/reject").status_code == 403
    assert client.post("/api/runs/app-x/resume").status_code == 403
    # Replay stays available in read-only mode.
    assert client.get("/api/replay/runs").status_code == 200
    assert client.get(f"/api/replay/runs/{key}").status_code == 200
