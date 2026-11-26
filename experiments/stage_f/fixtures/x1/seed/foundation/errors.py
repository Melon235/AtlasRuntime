"""Domain errors (frozen)."""


class DomainError(Exception):
    """Base class for a rejected business operation."""


class CurrencyError(DomainError):
    """Two monetary values in different currencies were combined."""


class PriceError(DomainError):
    """A price or quantity is not usable."""
