"""Public contract for the calculator project: this suite must pass."""

from app.calc import add, subtract
from app.text import normalize
from app.units import GRAMS_PER_KILOGRAM, to_grams, total_grams


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(9, 4) == 5
    assert subtract(0, 0) == 0


def test_normalize():
    assert normalize("  a  ") == "a"


def test_units():
    assert GRAMS_PER_KILOGRAM == 1000
    assert to_grams(1.5) == 1500
    assert total_grams(1, 2) == 3000
