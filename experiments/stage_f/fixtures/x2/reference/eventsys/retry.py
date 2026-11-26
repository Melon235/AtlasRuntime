"""Retry policy (v2): validated values, capped backoff, first attempt counted."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        for name in ("base_delay", "max_delay"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")


def delay(attempt: int, policy: RetryPolicy) -> float:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt starts at 1")
    return min(policy.base_delay * 2.0 ** (attempt - 1), policy.max_delay)


def should_retry(failures: int, policy: RetryPolicy) -> bool:
    return failures < policy.max_attempts
