"""Retry policy (v1): no validation, no ceiling, an off-by-one limit."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    multiplier: float = 2.0


def next_delay(attempt: int, policy: Policy | None = None) -> float:
    active = policy or Policy()
    return active.base_delay * active.multiplier ** (attempt - 1)


def should_retry(failures: int, policy: Policy) -> bool:
    return failures <= policy.max_attempts
