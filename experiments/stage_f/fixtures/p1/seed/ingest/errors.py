"""Error types shared by every connector (frozen)."""


class IngestError(Exception):
    """Base class for every error this package raises deliberately."""


class SchemaError(IngestError):
    """The input does not match the declared schema."""


class ConversionError(IngestError):
    """A value cannot be converted to its declared kind."""


class CheckpointError(IngestError):
    """A persisted checkpoint is missing, malformed or not monotonic."""
