"""AtlasRuntime local CLI over the shared application service."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from atlasos.app.service import (
    ApplicationError,
    ApprovalAuthorityUnavailable,
    AtlasApplicationService,
    RecoveryAuthorityUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNS_ROOT = Path(
    os.environ.get("ATLAS_APP_RUNS_ROOT", str(REPO_ROOT / "experiments" / "runs" / "app"))
)
app = typer.Typer(
    add_completion=False,
    help="AtlasRuntime local execution interface. Python package: atlasos.",
)
console = Console()

#: Statuses a wait loop stops on. ``INTERRUPTED`` is included because a run whose owning process
#: disappeared is terminal for the reader: it will not become RUNNING again.
TERMINAL_STATUS = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
    "SANDBOX_LOST",
    "CLEANUP_UNRESOLVED",
}


def _service(
    runs_root: Path | None = None,
    *,
    provider: str = "deepseek",
    sandbox_enabled: bool = True,
) -> AtlasApplicationService:
    return AtlasApplicationService(
        runs_root=runs_root or DEFAULT_RUNS_ROOT,
        provider=provider,
        sandbox_enabled=sandbox_enabled,
    )


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _error(error: Exception) -> None:
    console.print(f"[red]error:[/red] {error}")
    raise typer.Exit(code=2)


@app.command("run")
def run_task(
    task: str = typer.Argument(..., help="natural-language task"),
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    mode: str = typer.Option(
        "workflow",
        "--mode",
        "-m",
        help="workflow | coding | main_direct | main_dynamic",
    ),
    max_turns: int = typer.Option(12, "--max-turns"),
    budget: int | None = typer.Option(
        None, "--budget", help="run-scoped token budget for a Main-driven mode"
    ),
    interactive: bool = typer.Option(
        False, "--interactive", help="review a sandbox patch for a clean Git workspace"
    ),
    new_paths: list[str] | None = typer.Option(
        None, "--allow-new", help="authorize one new workspace-relative file per flag"
    ),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    provider: str = typer.Option(
        "deepseek",
        "--provider",
        help="deepseek (live) or deterministic (scripted; no network call)",
    ),
    no_sandbox: bool = typer.Option(
        False, "--no-sandbox", help="run on the host without isolation (recorded as host mode)"
    ),
    detach: bool = typer.Option(False, "--detach", help="return without waiting"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Submit a task and, by default, wait for the terminal projection."""
    service = _service(runs_root, provider=provider, sandbox_enabled=not no_sandbox)
    try:
        run = service.submit(
            task,
            mode=mode,
            workspace=workspace,
            max_turns=max_turns,
            budget_tokens=budget,
            interactive=interactive,
            new_paths=tuple(new_paths or ()),
        )
    except ApplicationError as error:
        return _error(error)
    if detach:
        if json_output:
            _print_json(run)
        else:
            console.print(f"[green]submitted[/green] {run['run_id']} ({run['mode']})")
            console.print(f"watch: atlas logs {run['run_id']} --follow")
        return
    seen = 0
    deadline = time.monotonic() + 900
    detail: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            detail = service.detail(run["run_id"])
        except ApplicationError as error:
            return _error(error)
        for event in service.events(run["run_id"], after=seen):
            seen = max(seen, int(event["id"]))
            if not json_output:
                console.print(
                    f"[dim]{event['timestamp']}[/dim] {event['type']} "
                    f"{json.dumps(event.get('data') or {}, ensure_ascii=False)}"
                )
        if detail["status"] in TERMINAL_STATUS:
            break
        time.sleep(0.5)
    if detail is None:
        return _error(ApplicationError("run did not produce a projection"))
    if json_output:
        _print_json(service.result(run["run_id"]))
    else:
        console.print(f"[bold]{detail['status']}[/bold] {run['run_id']}")
        if detail.get("final_output"):
            console.print(detail["final_output"])
        if detail.get("error"):
            console.print(f"[red]{detail['error']}[/red]")
    if detail["status"] != "COMPLETED":
        raise typer.Exit(code=1)


@app.command("runs")
def list_runs(
    limit: int = typer.Option(50, "--limit"),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List persisted runs."""
    items = _service(runs_root).list_runs(limit=limit)
    if json_output:
        _print_json(items)
        return
    table = Table(title="AtlasRuntime runs")
    table.add_column("run")
    table.add_column("mode")
    table.add_column("status")
    table.add_column("created")
    table.add_column("task", overflow="fold")
    for item in items:
        table.add_row(
            item["run_id"], item["mode"], item["status"], item["created_at"], item["task"][:80]
        )
    console.print(table)


@app.command("status")
def status(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one run summary."""
    try:
        detail = _service(runs_root).detail(run_id)
    except ApplicationError as error:
        return _error(error)
    if json_output:
        _print_json({key: detail[key] for key in ("run_id", "mode", "status", "task", "error")})
        return
    console.print(f"[bold]{detail['run_id']}[/bold] {detail['status']}")
    console.print(f"mode: {detail['mode']}  model: {detail['model']}")
    console.print(f"task: {detail['task']}")
    if detail.get("error"):
        console.print(f"[red]{detail['error']}[/red]")
    console.print(f"metrics: {json.dumps(detail.get('metrics') or {}, sort_keys=True)}")


@app.command("logs")
def logs(
    run_id: str,
    follow: bool = typer.Option(False, "--follow", "-f"),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show projected run events; --follow polls until the run is terminal."""
    service = _service(runs_root)
    seen = 0
    while True:
        try:
            events = service.events(run_id, after=seen)
            detail = service.detail(run_id)
        except ApplicationError as error:
            return _error(error)
        for event in events:
            seen = max(seen, int(event["id"]))
            if json_output:
                typer.echo(json.dumps(event, ensure_ascii=False))
            else:
                console.print(
                    f"[dim]{event['timestamp']}[/dim] {event['type']} "
                    f"{json.dumps(event.get('data') or {}, ensure_ascii=False)}"
                )
        if not follow or detail["status"] in TERMINAL_STATUS:
            break
        time.sleep(0.5)


@app.command("cancel")
def cancel(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
) -> None:
    """Report that cancellation is unavailable; this command never fakes a stopped run."""
    try:
        _service(runs_root).cancel(run_id)
    except ApplicationError as error:
        return _error(error)
    console.print("unreachable: cancel always refuses while the thread model has no safe stop")


@app.command("workspaces")
def workspaces(
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the workspace roots and names a Main-driven run may use."""
    value = _service(runs_root).workspaces()
    if json_output:
        _print_json(value)
        return
    table = Table(title="Workspaces")
    table.add_column("root")
    table.add_column("names", overflow="fold")
    for root in value["roots"]:
        table.add_row(root["root"], ", ".join(root["workspaces"]) or "(none)")
    console.print(table)
    console.print(
        f"provider={value['provider']} ({value['provider_kind']}) "
        f"default budget={value['defaults']['budget_tokens']} tokens"
    )


@app.command("workers")
def workers(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show task/worker state projected from the persisted run."""
    try:
        detail = _service(runs_root).detail(run_id)
    except ApplicationError as error:
        return _error(error)
    if json_output:
        _print_json({"run_id": run_id, "workers": detail["workers"], "tasks": detail["tasks"]})
        return
    table = Table(title=f"Workers — {run_id}")
    table.add_column("worker")
    table.add_column("task")
    table.add_column("status")
    table.add_column("title", overflow="fold")
    for item in detail["workers"]:
        table.add_row(
            str(item.get("worker_id") or "-"),
            str(item.get("task_id") or "-"),
            str(item.get("status") or "-"),
            str(item.get("title") or "-"),
        )
    console.print(table)


@app.command("approvals")
def approvals(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List pending approvals from the authoritative M6 store (read-only)."""
    try:
        values = _service(runs_root).list_approvals(run_id)
    except ApplicationError as error:
        return _error(error)
    if json_output:
        _print_json(values)
        return
    if not values:
        console.print("no pending approvals")
        return
    table = Table(title=f"Pending approvals — {run_id}")
    table.add_column("approval")
    table.add_column("tool")
    table.add_column("action")
    table.add_column("status")
    for item in values:
        table.add_row(
            str(item.get("approval_id") or item.get("id") or "-"),
            str(item.get("tool_id") or item.get("tool_name") or "-"),
            str(item.get("action") or "-"),
            str(item.get("status") or "pending"),
        )
    console.print(table)


@app.command("approve")
def approve(
    approval_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
) -> None:
    """Resolve an approval only through a live M6 coordinator; never auto-approve."""
    try:
        result = _service(runs_root).approve(approval_id)
    except ApprovalAuthorityUnavailable as error:
        return _error(error)
    _print_json(result)


@app.command("reject")
def reject(
    approval_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
) -> None:
    """Reject an approval only through a live M6 coordinator; never auto-reject."""
    try:
        result = _service(runs_root).reject(approval_id)
    except ApprovalAuthorityUnavailable as error:
        return _error(error)
    _print_json(result)


@app.command("resume")
def resume(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
) -> None:
    """Resume only a trusted suspension; ordinary failures are not Stage D recoveries."""
    try:
        result = _service(runs_root).resume(run_id)
    except RecoveryAuthorityUnavailable as error:
        return _error(error)
    _print_json(result)


@app.command("result")
def result(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the final result projection."""
    try:
        value = _service(runs_root).result(run_id)
    except ApplicationError as error:
        return _error(error)
    if json_output:
        _print_json(value)
        return
    console.print(value["final_output"] or "(no final output)")
    console.print(f"[dim]status={value['status']} metrics={value['metrics']}[/dim]")
    if tests := value.get("public_tests"):
        console.print(f"Public tests: {tests['status']}")
        for run in tests.get("runs", []):
            console.print(f"  #{run['call_index']} {run['status']} (exit {run['exit_code']})")


@app.command("export")
def export(
    run_id: str,
    output: Path = typer.Option(..., "--output", "-o"),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
) -> None:
    """Export a read-only JSON bundle of run summary, tasks, tools, events, and result."""
    try:
        value = _service(runs_root).detail(run_id)
    except ApplicationError as error:
        return _error(error)
    payload = {
        "run": value,
        "result": _service(runs_root).result(run_id),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"exported {run_id} -> {output}")


@app.command("diff")
def patch_diff(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect a completed sandbox patch and its bound workspace snapshot."""
    try:
        review = _service(runs_root).patch_review(run_id)
    except ApplicationError as error:
        return _error(error)
    if json_output:
        _print_json(review)
        return
    typer.echo(review["state"])
    if review.get("message"):
        typer.echo(review["message"])
    if review.get("patch_digest"):
        typer.echo(f"Original workspace: {review['source']}")
        typer.echo(f"Base snapshot: {review['snapshot_id']}")
        typer.echo(f"Patch digest: {review['patch_digest']}")
        for item in review["files"]:
            typer.echo(f"\n{item['status']} {item['path']}")
            typer.echo(item["diff"], nl=False)
            if item["truncated"]:
                typer.echo("[Diff preview truncated]")


def _patch_decision(
    run_id: str, action: str, runs_root: Path | None, digest: str | None, yes: bool
) -> None:
    service = _service(runs_root)
    try:
        review = service.patch_review(run_id)
        if review["state"] == "UNAVAILABLE":
            raise ApplicationError(review["message"])
        if yes and not digest:
            raise ApplicationError("--yes requires --digest from atlas diff")
        binding = {
            key: review[key] for key in ("run_id", "workspace_id", "snapshot_id", "patch_digest")
        }
        if digest is not None:
            binding["patch_digest"] = digest
        if not yes:
            typer.echo(f"Patch {review['patch_digest']} for {review['source']}")
            typer.echo("Changed files: " + ", ".join(item["path"] for item in review["files"]))
            typer.confirm(f"{action.capitalize()} this patch?", abort=True)
        result = service.decide_patch(run_id, action, binding, confirm=True)
    except ApplicationError as error:
        return _error(error)
    typer.echo(f"{result['state']}  {result['message']}")
    if result["state"] in {"CONFLICT", "APPLY_FAILED"}:
        raise typer.Exit(code=1)


@app.command("apply")
def apply_patch(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    digest: str | None = typer.Option(None, "--digest"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Explicitly apply a reviewed sandbox patch to its original workspace."""
    _patch_decision(run_id, "apply", runs_root, digest, yes)


@app.command("reject-patch")
def reject_patch(
    run_id: str,
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    digest: str | None = typer.Option(None, "--digest"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Reject a sandbox patch and keep the original workspace untouched."""
    _patch_decision(run_id, "reject", runs_root, digest, yes)


@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    replay_roots: list[str] | None = typer.Option(
        None,
        "--replay-roots",
        help=(
            "read-only evidence root for the replay workbench; repeat the flag or use "
            "'alias=path'. Defaults to ATLASOS_REPLAY_ROOTS."
        ),
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="refuse live submission, approval and recovery routes; replay stays available",
    ),
    provider: str = typer.Option(
        "deepseek",
        "--provider",
        help="live provider for new runs: deepseek, or deterministic (scripted local runs)",
    ),
    no_sandbox: bool = typer.Option(
        False,
        "--no-sandbox",
        help="run tasks on the host without isolation; recorded as host mode in every run",
    ),
) -> None:
    """Start the loopback-only FastAPI Web UI, including the read-only replay workbench."""
    import uvicorn

    from atlasos.app.replay import EVIDENCE_ROOT_ENV
    from atlasos.app.replay.sources import ReplayConfigError, parse_root_specs
    from atlasos.app.web import create_app

    try:
        roots = parse_root_specs(
            replay_roots or [],
            env_value=os.environ.get(EVIDENCE_ROOT_ENV),
            base=REPO_ROOT,
        )
    except ReplayConfigError as error:
        return _error(error)

    if roots:
        table = Table(title="replay evidence roots (read-only)")
        table.add_column("alias")
        table.add_column("path")
        for root in roots:
            table.add_row(root.label, str(root.path))
        console.print(table)
    else:
        console.print(
            "[yellow]no replay evidence root configured[/yellow] — the workbench will start "
            "empty; pass --replay-roots or set ATLASOS_REPLAY_ROOTS"
        )

    app_instance = create_app(
        runs_root=runs_root or DEFAULT_RUNS_ROOT,
        replay_roots=roots,
        read_only=read_only,
        provider=provider,
        sandbox_enabled=not no_sandbox,
    )
    console.print(f"console:   http://{host}:{port}/          (live runs, provider={provider})")
    console.print(f"workbench: http://{host}:{port}/replay   (historical + finished live runs)")
    uvicorn.run(app_instance, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
