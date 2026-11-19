"""Mass helpers. This module builds on ``app.calc`` and must keep working."""

from app.calc import add

GRAMS_PER_KILOGRAM = 1000


def to_grams(kilograms):
    """Convert kilograms to grams."""
    return int(kilograms * GRAMS_PER_KILOGRAM)


def total_grams(*kilograms):
    """Sum several masses given in kilograms, and report the total in grams."""
    return to_grams(add(*kilograms))
