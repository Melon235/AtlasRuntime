"""Scheduler errors (frozen)."""


class SchedulerError(Exception):
    """Base class for a rejected scheduler operation."""


class InvalidTransition(SchedulerError):
    """The state/event pair is not in the table."""


class UnknownJob(SchedulerError):
    """No record exists for this job id."""


class DuplicateJob(SchedulerError):
    """A record with this job id already exists."""


class StoreError(SchedulerError):
    """The persisted job store is missing, malformed or inconsistent."""


class UnknownJobKind(SchedulerError):
    """No payload spec exists for this job kind."""


class InvalidJobPayload(SchedulerError):
    """The job payload is missing a required key for its kind."""


class EventLogError(SchedulerError):
    """The lifecycle event log is malformed."""
