"""Task ``slugkit`` — a local, single-module change (expected to suit Direct execution).

One public rule module (text folding) is complete, one canonical-identifier rule module is a stub.
The investigation is a handful of reads and the fix is two functions in one file.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "slugkit"

_README = """# slugkit

Canonical document slugs.

`slugkit.text` folds text. `slugkit.core` defines what a slug is and is incomplete.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Canonical document slugs."""

from .core import DEFAULT_MAX_LENGTH, SlugError, is_slug, slugify

__all__ = ["DEFAULT_MAX_LENGTH", "SlugError", "is_slug", "slugify"]
'''

_TEXT = '''"""Text normalisation shared by the slug rules."""

from __future__ import annotations

_ACCENTS = str.maketrans(
    {
        "à": "a",
        "á": "a",
        "â": "a",
        "ã": "a",
        "ä": "a",
        "å": "a",
        "ç": "c",
        "è": "e",
        "é": "e",
        "ê": "e",
        "ë": "e",
        "ì": "i",
        "í": "i",
        "î": "i",
        "ï": "i",
        "ñ": "n",
        "ò": "o",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ö": "o",
        "ù": "u",
        "ú": "u",
        "û": "u",
        "ü": "u",
        "ý": "y",
        "ÿ": "y",
        "ß": "ss",
        "æ": "ae",
        "ø": "o",
        "œ": "oe",
    }
)


def fold(text: str) -> str:
    """Lower-case ``text`` and strip the Latin-1 accents this project supports."""

    return text.casefold().translate(_ACCENTS)


def words(text: str) -> list[str]:
    """Split folded text into runs of ASCII alphanumerics.

    Anything else separates words. Non-ASCII letters are dropped rather than transliterated: a slug
    is an ASCII identifier, and an unrepresentable character is not a word boundary marker.
    """

    current: list[str] = []
    result: list[str] = []
    for char in fold(text):
        if char.isascii() and char.isalnum():
            current.append(char)
        elif current:
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return result
'''

_CORE = '''"""Slug rules.

A slug is the canonical identifier of a document:

* ASCII only, lower case;
* words separated by a single ``-``;
* no leading or trailing ``-``, at most ``max_length`` characters;
* an input with no usable characters is an error, not an empty slug.
"""

from __future__ import annotations

from .text import words

DEFAULT_MAX_LENGTH = 64


class SlugError(ValueError):
    """The input cannot be turned into a slug."""


def slugify(text: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Return the slug for ``text``.

    Raises :class:`SlugError` when nothing usable remains and ``ValueError`` when ``max_length`` is
    not positive.
    """

    raise NotImplementedError("slugify is not implemented yet")


def is_slug(value: str) -> bool:
    """True when ``value`` is already in canonical slug form."""

    raise NotImplementedError("is_slug is not implemented yet")
'''

_TEST_TEXT = """from slugkit.text import fold, words


def test_fold_lowercases() -> None:
    assert fold("Hello") == "hello"


def test_fold_strips_supported_accents() -> None:
    assert fold("Crème Brûlée") == "creme brulee"


def test_words_splits_on_punctuation() -> None:
    assert words("a -- b!") == ["a", "b"]


def test_words_keeps_digits() -> None:
    assert words("v2 release 7") == ["v2", "release", "7"]


def test_words_of_punctuation_is_empty() -> None:
    assert words("---") == []
"""

_TEST_PUBLIC = """import pytest

from slugkit.core import SlugError, is_slug, slugify


def test_a_simple_sentence() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_accents_are_folded() -> None:
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_repeated_separators_collapse() -> None:
    assert slugify("a -- b") == "a-b"


def test_leading_and_trailing_separators_are_removed() -> None:
    assert slugify("--Hello--") == "hello"


def test_an_empty_slug_is_an_error() -> None:
    with pytest.raises(SlugError):
        slugify("!!!")


def test_truncation_does_not_end_with_a_separator() -> None:
    assert slugify("alpha beta gamma", max_length=6) == "alpha"


def test_is_slug_accepts_canonical_and_rejects_others() -> None:
    assert is_slug("hello-world") is True
    assert is_slug("Hello-World") is False
"""

_REFERENCE = '''"""Slug rules.

A slug is the canonical identifier of a document:

* ASCII only, lower case;
* words separated by a single ``-``;
* no leading or trailing ``-``, at most ``max_length`` characters;
* an input with no usable characters is an error, not an empty slug.
"""

from __future__ import annotations

from .text import words

DEFAULT_MAX_LENGTH = 64


class SlugError(ValueError):
    """The input cannot be turned into a slug."""


def slugify(text: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Return the slug for ``text``.

    Raises :class:`SlugError` when nothing usable remains and ``ValueError`` when ``max_length`` is
    not positive.
    """

    if max_length <= 0:
        raise ValueError("max_length must be positive")
    joined = "-".join(words(text))
    if not joined:
        raise SlugError(f"no usable characters in {text!r}")
    if len(joined) > max_length:
        joined = joined[:max_length].rstrip("-")
    if not joined:
        raise SlugError(f"no usable characters in the first {max_length} characters")
    return joined


def is_slug(value: str) -> bool:
    """True when ``value`` is already in canonical slug form."""

    if not isinstance(value, str) or not value:
        return False
    try:
        return slugify(value) == value
    except ValueError:
        return False
'''

_ORACLE = """import traceback

from slugkit.core import SlugError, is_slug, slugify

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def raises(kind, call, label):
    try:
        call()
    except kind:
        return
    except BaseException as error:  # noqa: BLE001
        FAILURES.append(f"{label}: raised {type(error).__name__}, expected {kind.__name__}")
        return
    FAILURES.append(f"{label}: did not raise")


try:
    # Non ASCII letters are not transliterated, so an all-Cyrillic input has no slug.
    raises(SlugError, lambda: slugify("Привет мир"), "cyrillic")
    # A long single word truncates at the limit with no separator left behind.
    long_slug = slugify("a" * 70)
    check(len(long_slug) == 64, "long word is not truncated to 64")
    check(not long_slug.endswith("-"), "truncated long word ends with a separator")
    # Idempotence, which the public suite never states.
    for text in ("Hello, World!", "Crème Brûlée", "a -- b", "2026 Q1 Report"):
        once = slugify(text)
        check(slugify(once) == once, f"slugify is not idempotent for {text!r}")
        check(is_slug(once) is True, f"is_slug rejects its own output for {text!r}")
    # Boundary values of the truncation rule.
    check(slugify("alpha beta", max_length=6) == "alpha", "truncation keeps a trailing separator")
    check(slugify("alpha beta", max_length=10) == "alpha-beta", "exact-length slug is truncated")
    # is_slug is the canonical form, not a character-class check.
    for value in ("-a", "a-", "a--b", "", "a b", "Hello"):
        check(is_slug(value) is False, f"is_slug accepted {value!r}")
    check(is_slug("a-1-b2") is True, "is_slug rejected a valid slug")
    # Digits survive, whitespace does not.
    check(slugify("2026") == "2026", "digits are not preserved")
    check(slugify("  Hello  ") == "hello", "surrounding whitespace is not stripped")
    raises(ValueError, lambda: slugify("a", max_length=0), "max_length=0")
    raises(SlugError, lambda: slugify(""), "empty string")
except BaseException:  # noqa: BLE001
    traceback.print_exc()
    print("HIDDEN_FAIL")
    raise SystemExit(1) from None

if FAILURES:
    for item in FAILURES:
        print(f"FAIL {item}")
    print("HIDDEN_FAIL")
    raise SystemExit(1)
print("HIDDEN_OK")
"""

SPEC = TaskSpec(
    task_id=TASK_ID,
    title="Canonical document slugs",
    structure="LOCAL_DIRECT",
    rationale=(
        "One stub module whose rules are fully stated in its docstring, one complete helper module, "
        "and a reference change of two functions in one file. There is no second investigation to "
        "run in parallel and no cross-package integration to coordinate."
    ),
    goal=(
        "The slugkit project documents what a canonical document slug is, but its rule module "
        "raises NotImplementedError. Implement the slug rules so the public tests pass, keeping the "
        "existing text helpers unchanged."
    ),
    write_scope=("slugkit/core.py",),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "slugkit/__init__.py": _INIT,
        "slugkit/text.py": _TEXT,
        "slugkit/core.py": _CORE,
        "tests/test_text.py": _TEST_TEXT,
        "tests/test_public_slug.py": _TEST_PUBLIC,
    },
    reference={"slugkit/core.py": _REFERENCE},
    wrong=(
        WrongVariant(
            name="no_truncation",
            covers="max_length ignored for long input",
            file="slugkit/core.py",
            edits=(("    if len(joined) > max_length:", "    if len(joined) < 0:"),),
        ),
        WrongVariant(
            name="truncation_keeps_separator",
            covers="a truncated slug ending with '-'",
            file="slugkit/core.py",
            edits=(
                (
                    '        joined = joined[:max_length].rstrip("-")',
                    "        joined = joined[:max_length]",
                ),
            ),
        ),
        WrongVariant(
            name="is_slug_is_charwise",
            covers="is_slug accepting non-canonical values",
            file="slugkit/core.py",
            edits=(
                (
                    "        return slugify(value) == value",
                    '        return value == value.strip("-").lower()',
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_text.py",),
    target_command=("tests/test_public_slug.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 5,
        "target_tests": 7,
        "reference_tests": 12,
        "reference_files": 1,
        "reference_lines": 31,
        "independent_investigations": 0,
        "acceptance_difficulty": "LOW",
    },
    labels={
        "change_surface": "1 file, 2 functions",
        "key_dependencies": "slugkit.text.words",
        "oracle_scope": "idempotence, non-ASCII inputs, truncation boundaries, canonical form",
    },
    notes="Reads: README + core + text is enough. Public suite already states most rules.",
)
