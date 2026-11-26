"""Protocol error types (frozen)."""


class ProtocolError(Exception):
    """A message or frame violates the wire contract."""


class ChecksumError(ProtocolError):
    """The declared checksum does not match the payload."""


class VersionError(ProtocolError):
    """The wire version is not one this codec implements."""


class Truncated(ProtocolError):
    """The input ended in the middle of a message or frame."""


class UnknownType(ProtocolError):
    """No handler is registered for this message kind."""


class InvalidPayload(ProtocolError):
    """A decoded payload does not match its registered schema."""
