"""Run report rendering (v1)."""

from __future__ import annotations

from ingest.model import RunResult


def render(result: RunResult) -> str:
    """Render the run summary."""

    lines = [
        f"source={result.source}",
        f"records={result.record_count}",
        f"batches={result.batch_count}",
        f"cursor={result.cursor}",
        f"skipped={result.skipped}",
    ]
    for name, total in result.totals.items():
        lines.append(f"total.{name}={total}")
    return "\n".join(lines) + "\n"
