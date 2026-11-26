"""Pipeline error types (frozen)."""


class PipelineError(Exception):
    """Base class for a rejected pipeline operation."""


class QueueFull(PipelineError):
    """The broker cannot accept another message."""


class DuplicateEvent(PipelineError):
    """An event id is already known to the broker."""


class StoreError(PipelineError):
    """The persisted processed set is missing or malformed."""


class JournalError(PipelineError):
    """The delivery journal is malformed."""


class AuditError(PipelineError):
    """The run audit log is malformed."""
