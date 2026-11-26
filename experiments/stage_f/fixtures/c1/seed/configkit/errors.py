"""Structured configuration errors (frozen)."""

from __future__ import annotations


class ConfigError(Exception):
    """A configuration failure with a stable code and the failing path."""

    code = "config"

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{self.code} at {path}: {message}")


class ParseError(ConfigError):
    code = "parse"


class MigrationError(ConfigError):
    code = "migration"


class ProfileError(ConfigError):
    code = "profile"


class UnknownKey(ConfigError):
    code = "unknown_key"


class TypeMismatch(ConfigError):
    code = "type_mismatch"


class ConstraintViolation(ConfigError):
    code = "constraint"
