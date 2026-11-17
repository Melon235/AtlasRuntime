"""Closure-2B-0-R2: the durable, one-shot execution lease for an approved pilot plan.

What this module is: a **claim ledger**. An approved plan may be executed once. The claim is written
to a local SQLite database inside the same transaction that proves nobody has claimed it, so two
processes cannot both succeed, and a crash does not quietly return the right to execute.

What this module is not: an approval system, an agent loop, a scheduler or a run store. It does not
start anything; ``run_live_pilot`` claims here **before** it talks to a provider or a sandbox.

The trust boundary is local and must be stated plainly:

* it protects processes that share one ledger file on one machine;
* it does not protect against someone deleting the database, and it does not coordinate two
  machines;
* it is **not** a global exactly-once guarantee.

Nothing here ever releases a claim. A consumed plan stays consumed; a new execution needs a new
approval, which is a decision this module does not make.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Bumped when the schema changes; stored in ``ledger_meta`` and checked on open.
LEDGER_SCHEMA_VERSION = "closure-2b-0-r2"

#: How long a claim attempt waits for the SQLite write lock before failing closed.
DEFAULT_BUSY_TIMEOUT_MS = 5_000

#: Cell states. ``CLAIMED`` means "reserved but not dispatched yet".
CELL_STATES: tuple[str, ...] = (
    "CLAIMED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "INTERRUPTED_UNKNOWN",
)

#: Allowed transitions. Terminal states never move again, and nothing ever returns to ``CLAIMED``:
#: a consumed claim is not reusable, which is the whole point of the ledger.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CLAIMED": frozenset({"RUNNING", "FAILED", "INTERRUPTED_UNKNOWN"}),
    "RUNNING": frozenset({"COMPLETED", "FAILED", "INTERRUPTED_UNKNOWN"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "INTERRUPTED_UNKNOWN": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_claims (
    plan_digest TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    runner_revision TEXT,
    git_commit TEXT,
    claimed_at TEXT NOT NULL,
    claim_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cell_claims (
    plan_digest TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    repetition_id INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_path TEXT,
    failure_reason TEXT,
    PRIMARY KEY (plan_digest, cell_id, repetition_id)
);
"""


class PilotLedgerError(RuntimeError):
    """Base class: every ledger refusal carries a stable ``reason_code``."""

    reason_code = "PILOT_LEDGER_ERROR"


class PilotAlreadyClaimed(PilotLedgerError):
    """This plan digest has already been executed (or attempted). Never retried automatically."""

    reason_code = "PILOT_ALREADY_CLAIMED"


class PilotLedgerLocked(PilotLedgerError):
    """The write lock could not be taken in time. Failing closed is the only safe answer."""

    reason_code = "PILOT_LEDGER_LOCKED"


class PilotLedgerUnavailable(PilotLedgerError):
    """The ledger cannot be opened, created, read or written."""

    reason_code = "PILOT_LEDGER_UNAVAILABLE"


class PilotLedgerStateInvalid(PilotLedgerError):
    """An illegal state transition or an unknown state was asked for."""

    reason_code = "PILOT_LEDGER_STATE_INVALID"


def default_ledger_path() -> Path:
    """The production ledger location: per-user state, not a results directory.

    Deriving this from ``--runs-root`` would be a bypass — point the runner at a fresh directory and
    the approval would look unused again. This path is stable across checkouts of the same user on
    the same machine, which is exactly the protection this round claims.
    """

    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "AtlasRuntime" / "closure2" / "pilot-ledger.sqlite"
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path.home() / ".local" / "state"
    return root / "atlasruntime" / "closure2" / "pilot-ledger.sqlite"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_lock_error(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


class ExecutionLease:
    """Handle for one claimed plan. Writes cell progress; never releases the claim."""

    def __init__(self, ledger: PilotLedger, plan_digest: str, attempt_id: str) -> None:
        self._ledger = ledger
        self.plan_digest = plan_digest
        self.attempt_id = attempt_id

    @property
    def path(self) -> Path:
        return self._ledger.path

    def mark(self, cell_id: str, repetition_id: int, status: str, **fields: Any) -> None:
        self._ledger.set_cell_state(self.plan_digest, cell_id, repetition_id, status, **fields)

    def mark_running(self, cell_id: str, repetition_id: int) -> None:
        self.mark(cell_id, repetition_id, "RUNNING", started_at=_now())

    def mark_completed(
        self, cell_id: str, repetition_id: int, *, result_path: str | None = None
    ) -> None:
        self.mark(cell_id, repetition_id, "COMPLETED", finished_at=_now(), result_path=result_path)

    def mark_failed(
        self,
        cell_id: str,
        repetition_id: int,
        *,
        failure_reason: str,
        result_path: str | None = None,
    ) -> None:
        self.mark(
            cell_id,
            repetition_id,
            "FAILED",
            finished_at=_now(),
            failure_reason=failure_reason,
            result_path=result_path,
        )


class PilotLedger:
    """One SQLite file, one claim per plan digest, one row per (plan, cell, repetition)."""

    def __init__(self, path: Path | None = None, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.path = Path(path) if path is not None else default_ledger_path()
        self.busy_timeout_ms = int(busy_timeout_ms)

    # ------------------------------------------------------------------ connection and schema

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path), timeout=self.busy_timeout_ms / 1000, isolation_level=None
            )
        except (OSError, sqlite3.Error) as error:
            raise PilotLedgerUnavailable(
                f"PILOT_LEDGER_UNAVAILABLE: cannot open {self.path}: {error}"
            ) from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            row = connection.execute(
                "SELECT value FROM ledger_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO ledger_meta (key, value) VALUES ('schema_version', ?)",
                    (LEDGER_SCHEMA_VERSION,),
                )
            elif row["value"] != LEDGER_SCHEMA_VERSION:
                raise PilotLedgerUnavailable(
                    f"PILOT_LEDGER_UNAVAILABLE: ledger schema {row['value']!r} is not "
                    f"{LEDGER_SCHEMA_VERSION!r}"
                )
        except PilotLedgerUnavailable:
            connection.close()
            raise
        except sqlite3.OperationalError as error:
            connection.close()
            if _is_lock_error(error):
                raise PilotLedgerLocked(
                    f"PILOT_LEDGER_LOCKED: {self.path} is locked by another process: {error}"
                ) from error
            raise PilotLedgerUnavailable(
                f"PILOT_LEDGER_UNAVAILABLE: {self.path} is not a usable ledger: {error}"
            ) from error
        except sqlite3.DatabaseError as error:
            connection.close()
            raise PilotLedgerUnavailable(
                f"PILOT_LEDGER_UNAVAILABLE: {self.path} is not a usable ledger: {error}"
            ) from error
        return connection

    # ------------------------------------------------------------------------------ claiming

    def claim_plan(
        self,
        *,
        plan_digest: str,
        plan_id: str,
        cells: list[tuple[str, int]],
        runner_revision: str,
        git_commit: str | None = None,
    ) -> ExecutionLease:
        """Atomically claim the whole plan. One winner, every loser told why.

        ``BEGIN IMMEDIATE`` takes the write lock before the existence check, so the check and the
        inserts are one transaction. A check followed by a separate insert would let two processes
        both observe "unclaimed".
        """

        if not plan_digest:
            raise PilotLedgerStateInvalid("PILOT_LEDGER_STATE_INVALID: empty plan digest")
        if not cells:
            raise PilotLedgerStateInvalid("PILOT_LEDGER_STATE_INVALID: empty cell list")
        keys = [(cell_id, int(repetition)) for cell_id, repetition in cells]
        if len(set(keys)) != len(keys):
            raise PilotLedgerStateInvalid(
                "PILOT_LEDGER_STATE_INVALID: the claim lists the same cell twice"
            )

        attempt_id = uuid.uuid4().hex
        claimed_at = _now()
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if _is_lock_error(error):
                    raise PilotLedgerLocked(
                        f"PILOT_LEDGER_LOCKED: {self.path} stayed locked for "
                        f"{self.busy_timeout_ms} ms"
                    ) from error
                raise PilotLedgerUnavailable(
                    f"PILOT_LEDGER_UNAVAILABLE: cannot begin a claim transaction: {error}"
                ) from error
            try:
                existing = connection.execute(
                    "SELECT attempt_id, claimed_at, claim_status FROM pilot_claims "
                    "WHERE plan_digest = ?",
                    (plan_digest,),
                ).fetchone()
                if existing is not None:
                    raise PilotAlreadyClaimed(
                        f"PILOT_ALREADY_CLAIMED: plan {plan_digest} was already claimed by "
                        f"attempt {existing['attempt_id']} at {existing['claimed_at']} "
                        f"({existing['claim_status']})"
                    )
                connection.execute(
                    "INSERT INTO pilot_claims (plan_digest, plan_id, attempt_id, runner_revision, "
                    "git_commit, claimed_at, claim_status) VALUES (?, ?, ?, ?, ?, ?, 'CONSUMED')",
                    (plan_digest, plan_id, attempt_id, runner_revision, git_commit, claimed_at),
                )
                connection.executemany(
                    "INSERT INTO cell_claims (plan_digest, cell_id, repetition_id, attempt_id, "
                    "status, claimed_at) VALUES (?, ?, ?, ?, 'CLAIMED', ?)",
                    [
                        (plan_digest, cell_id, repetition, attempt_id, claimed_at)
                        for cell_id, repetition in keys
                    ],
                )
                connection.execute("COMMIT")
            except PilotAlreadyClaimed:
                connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as error:
                connection.execute("ROLLBACK")
                raise PilotAlreadyClaimed(
                    f"PILOT_ALREADY_CLAIMED: {self.path} already holds a claim for one of these "
                    f"cells: {error}"
                ) from error
            except sqlite3.OperationalError as error:
                connection.execute("ROLLBACK")
                if _is_lock_error(error):
                    raise PilotLedgerLocked(
                        f"PILOT_LEDGER_LOCKED: {self.path} could not be written: {error}"
                    ) from error
                raise PilotLedgerUnavailable(
                    f"PILOT_LEDGER_UNAVAILABLE: the claim on {self.path} was rolled back: {error}"
                ) from error
            except sqlite3.DatabaseError as error:
                connection.execute("ROLLBACK")
                raise PilotLedgerUnavailable(
                    f"PILOT_LEDGER_UNAVAILABLE: the claim on {self.path} was rolled back: {error}"
                ) from error
        finally:
            connection.close()
        return ExecutionLease(self, plan_digest, attempt_id)

    # ------------------------------------------------------------------------ state updates

    def set_cell_state(
        self,
        plan_digest: str,
        cell_id: str,
        repetition_id: int,
        status: str,
        **fields: Any,
    ) -> None:
        if status not in CELL_STATES:
            raise PilotLedgerStateInvalid(f"PILOT_LEDGER_STATE_INVALID: unknown state {status!r}")
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if _is_lock_error(error):
                    raise PilotLedgerLocked(
                        f"PILOT_LEDGER_LOCKED: {self.path} stayed locked for "
                        f"{self.busy_timeout_ms} ms"
                    ) from error
                raise
            row = connection.execute(
                "SELECT status FROM cell_claims WHERE plan_digest = ? AND cell_id = ? "
                "AND repetition_id = ?",
                (plan_digest, cell_id, int(repetition_id)),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise PilotLedgerStateInvalid(
                    f"PILOT_LEDGER_STATE_INVALID: no claim for {cell_id}@{repetition_id} "
                    f"in plan {plan_digest}"
                )
            current = row["status"]
            if status not in ALLOWED_TRANSITIONS.get(current, frozenset()):
                connection.execute("ROLLBACK")
                raise PilotLedgerStateInvalid(
                    f"PILOT_LEDGER_STATE_INVALID: {current} -> {status} is not allowed for "
                    f"{cell_id}@{repetition_id}"
                )
            assignments = ["status = ?"]
            values: list[Any] = [status]
            for column in ("started_at", "finished_at", "result_path", "failure_reason"):
                if column in fields:
                    assignments.append(f"{column} = ?")
                    values.append(fields[column])
            values.extend([plan_digest, cell_id, int(repetition_id)])
            connection.execute(
                f"UPDATE cell_claims SET {', '.join(assignments)} "
                "WHERE plan_digest = ? AND cell_id = ? AND repetition_id = ?",
                values,
            )
            connection.execute("COMMIT")
        except (PilotLedgerError, sqlite3.DatabaseError) as error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            if isinstance(error, PilotLedgerError):
                raise
            raise PilotLedgerUnavailable(
                f"PILOT_LEDGER_UNAVAILABLE: cannot update {cell_id}@{repetition_id}: {error}"
            ) from error
        finally:
            connection.close()

    # ------------------------------------------------------------------------------ auditing

    def get_claim(self, plan_digest: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM pilot_claims WHERE plan_digest = ?", (plan_digest,)
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None

    def cell_rows(self, plan_digest: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM cell_claims WHERE plan_digest = ? ORDER BY rowid", (plan_digest,)
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def recover_interrupted(self, plan_digest: str) -> list[str]:
        """Mark cells that were running when the process died. Never releases the claim.

        This is an operator action, not something the runner does on its own: a second process that
        reclaimed while the first was still alive would otherwise mark live work as interrupted.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT cell_id, repetition_id FROM cell_claims WHERE plan_digest = ? "
                "AND status = 'RUNNING'",
                (plan_digest,),
            ).fetchall()
            changed: list[str] = []
            for row in rows:
                connection.execute(
                    "UPDATE cell_claims SET status = 'INTERRUPTED_UNKNOWN', finished_at = ? "
                    "WHERE plan_digest = ? AND cell_id = ? AND repetition_id = ?",
                    (_now(), plan_digest, row["cell_id"], row["repetition_id"]),
                )
                changed.append(f"{row['cell_id']}@{row['repetition_id']}")
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise PilotLedgerUnavailable(
                f"PILOT_LEDGER_UNAVAILABLE: cannot recover {plan_digest}: {error}"
            ) from error
        finally:
            connection.close()
        return changed
