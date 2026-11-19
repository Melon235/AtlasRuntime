"""The audit layer's own failures.

These are *programming* failures, not release findings. A release problem is data and belongs in the
report; a duplicate check id or an impossible policy is a bug in the caller, and hiding it inside a
report would make an audit that silently did not run look like an audit that passed.
"""

from __future__ import annotations


class AuditError(RuntimeError):
    """Base class for audit-layer failures."""

    default_code = "audit_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class AuditContractError(AuditError):
    """The caller asked for a report that cannot be built as specified."""

    default_code = "audit_contract"


class AuditNotImplementedError(AuditError):
    """A part of the shared audit is not implemented yet."""

    default_code = "audit_not_implemented"


__all__ = ["AuditContractError", "AuditError", "AuditNotImplementedError"]
