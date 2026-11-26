"""Retry policy (v1): exponential growth without a ceiling and an off-by-one limit."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0


def delay(attempt: int, policy: RetryPolicy) -> float:
    return policy.base_delay * 2.0**attempt


def should_retry(failures: int, policy: RetryPolicy) -> bool:
    return failures <= policy.max_attempts
