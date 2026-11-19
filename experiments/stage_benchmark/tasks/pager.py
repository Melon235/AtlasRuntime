"""Task ``pager`` — cursor pagination arithmetic in one module (expected to suit Direct execution).

The cursor codec and the error type are complete; the page arithmetic is a stub. Two functions, one
file, and a rule set that is fully stated.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "pager"

_README = """# pager

Offset pagination with an opaque cursor.

`pager.cursor` encodes and decodes the cursor. `pager.page` turns a cursor into a slice and a
follow-up cursor, and is incomplete.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Cursor pagination."""

from .cursor import decode_cursor, encode_cursor
from .errors import InvalidCursorError
from .page import next_cursor, page_bounds

__all__ = ["InvalidCursorError", "decode_cursor", "encode_cursor", "next_cursor", "page_bounds"]
'''

_ERRORS = '''"""The pagination failure this package exposes."""

from __future__ import annotations


class InvalidCursorError(ValueError):
    """A cursor that this package did not issue, or one that no longer parses."""

    code = "invalid_cursor"
'''

_CURSOR = '''"""The opaque cursor codec.

The cursor is the base64 of the decimal offset, and decoding is strict: a token that does not
re-encode to itself is rejected rather than repaired.
"""

from __future__ import annotations

import base64
import binascii

from .errors import InvalidCursorError


def encode_cursor(offset: int) -> str:
    """Encode a non-negative offset."""

    if not isinstance(offset, int) or isinstance(offset, bool):
        raise TypeError("offset must be an int")
    if offset < 0:
        raise ValueError("offset must not be negative")
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def decode_cursor(token: str) -> int:
    """Decode a cursor this package issued."""

    if not isinstance(token, str) or not token:
        raise InvalidCursorError(f"not a cursor: {token!r}")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("ascii")
    except (UnicodeDecodeError, binascii.Error, ValueError) as error:
        raise InvalidCursorError(f"not a cursor: {token!r}") from error
    try:
        offset = int(raw)
    except ValueError as error:
        raise InvalidCursorError(f"not a cursor: {token!r}") from error
    if raw != str(offset) or offset < 0:
        raise InvalidCursorError(f"not a cursor: {token!r}")
    return offset
'''

_PAGE = '''"""Slice arithmetic.

``page_bounds(total, cursor, limit)`` returns the half-open ``(start, stop)`` slice for one page:

* ``cursor is None`` means the first page;
* ``limit`` must be positive, ``total`` must not be negative;
* a start past the end is clamped to ``total``, so the last page may be empty;
* a cursor this package did not issue raises :class:`~pager.errors.InvalidCursorError`.

``next_cursor(total, start, limit)`` returns the cursor of the following page, or ``None`` when the
current page already reached ``total``.
"""

from __future__ import annotations

from .cursor import decode_cursor, encode_cursor


def page_bounds(total: int, cursor: str | None, limit: int) -> tuple[int, int]:
    """The ``(start, stop)`` slice of the requested page."""

    raise NotImplementedError("page_bounds is not implemented yet")


def next_cursor(total: int, start: int, limit: int) -> str | None:
    """The cursor of the page after ``(start, start + limit)``, or ``None`` at the end."""

    raise NotImplementedError("next_cursor is not implemented yet")
'''

_TEST_CURSOR = """import pytest

from pager.cursor import decode_cursor, encode_cursor
from pager.errors import InvalidCursorError


def test_round_trip() -> None:
    assert decode_cursor(encode_cursor(7)) == 7


def test_zero_round_trips() -> None:
    assert decode_cursor(encode_cursor(0)) == 0


def test_negative_offset_cannot_be_encoded() -> None:
    with pytest.raises(ValueError):
        encode_cursor(-1)


def test_garbage_is_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-base64!!")


def test_empty_is_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("")
"""

_TEST_PUBLIC = """import pytest

from pager.cursor import encode_cursor
from pager.errors import InvalidCursorError
from pager.page import next_cursor, page_bounds


def test_first_page() -> None:
    assert page_bounds(10, None, 4) == (0, 4)


def test_a_follow_up_cursor_selects_the_next_page() -> None:
    cursor = next_cursor(10, 0, 4)
    assert cursor is not None
    assert page_bounds(10, cursor, 4) == (4, 8)


def test_last_page_may_be_short() -> None:
    assert page_bounds(10, encode_cursor(8), 4) == (8, 10)


def test_a_start_past_the_end_is_clamped() -> None:
    assert page_bounds(10, encode_cursor(50), 3) == (10, 10)


def test_a_foreign_cursor_is_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        page_bounds(10, "!!!", 4)


def test_a_non_positive_limit_is_an_error() -> None:
    with pytest.raises(ValueError):
        page_bounds(10, None, 0)
"""

_REFERENCE = '''"""Slice arithmetic.

``page_bounds(total, cursor, limit)`` returns the half-open ``(start, stop)`` slice for one page:

* ``cursor is None`` means the first page;
* ``limit`` must be positive, ``total`` must not be negative;
* a start past the end is clamped to ``total``, so the last page may be empty;
* a cursor this package did not issue raises :class:`~pager.errors.InvalidCursorError`.

``next_cursor(total, start, limit)`` returns the cursor of the following page, or ``None`` when the
current page already reached ``total``.
"""

from __future__ import annotations

from .cursor import decode_cursor, encode_cursor


def page_bounds(total: int, cursor: str | None, limit: int) -> tuple[int, int]:
    """The ``(start, stop)`` slice of the requested page."""

    if total < 0:
        raise ValueError("total must not be negative")
    if limit <= 0:
        raise ValueError("limit must be positive")
    start = 0 if cursor is None else decode_cursor(cursor)
    if start > total:
        start = total
    return start, min(start + limit, total)


def next_cursor(total: int, start: int, limit: int) -> str | None:
    """The cursor of the page after ``(start, start + limit)``, or ``None`` at the end."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    stop = min(start + limit, total)
    return encode_cursor(stop) if stop < total else None
'''

_ORACLE = """import traceback

from pager.cursor import decode_cursor, encode_cursor
from pager.errors import InvalidCursorError
from pager.page import next_cursor, page_bounds

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
    # An empty collection has exactly one, empty page and no follow-up cursor.
    check(page_bounds(0, None, 5) == (0, 0), "empty collection is not an empty page")
    check(next_cursor(0, 0, 5) is None, "empty collection has a follow-up cursor")
    # A limit larger than the collection is not an error.
    check(page_bounds(3, None, 99) == (0, 3), "oversized limit is not clamped")
    check(next_cursor(3, 0, 99) is None, "exact-end page has a follow-up cursor")
    # Walking every page must tile the collection exactly once and terminate.
    seen = []
    cursor = None
    for _ in range(50):
        start, stop = page_bounds(10, cursor, 3)
        seen.extend(range(start, stop))
        cursor = next_cursor(10, start, 3)
        if cursor is None:
            break
    else:
        FAILURES.append("cursor chain did not terminate")
    check(seen == list(range(10)), f"pages do not tile the collection: {seen}")
    # A start past the end is clamped into an empty page, never inverted.
    check(page_bounds(10, encode_cursor(50), 3) == (10, 10), "a start past the end is not clamped")
    raises(ValueError, lambda: page_bounds(10, None, 0), "page_bounds with limit 0")
    # The functions are pure: the same inputs give the same answer, and no cursor is consumed.
    check(page_bounds(10, encode_cursor(3), 3) == (3, 6), "cursor 3 does not select the third page")
    check(page_bounds(10, encode_cursor(3), 3) == (3, 6), "page_bounds is not repeatable")
    # Decoding is strict: a token that is not exactly what encode produces is rejected.
    mangled = encode_cursor(12)[:-1] + "!"
    raises(InvalidCursorError, lambda: decode_cursor(mangled), "mangled cursor")
    raises(InvalidCursorError, lambda: page_bounds(10, mangled, 2), "mangled cursor in page")
    # A zero limit would make the chain stand still, which the contract forbids.
    raises(ValueError, lambda: next_cursor(10, 0, 0), "next_cursor with limit 0")
    check(page_bounds(10, None, 10) == (0, 10), "the whole collection is not one page")
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
    title="Cursor pagination arithmetic",
    structure="LOCAL_DIRECT",
    rationale=(
        "Two arithmetic functions in a single module, with the codec and the error type already "
        "complete. The investigation is one read of the contract plus one of the codec; there is no "
        "independent second area to delegate."
    ),
    goal=(
        "The pager project has a working cursor codec, but the page arithmetic in pager/page.py is "
        "unimplemented. Implement page_bounds and next_cursor so the documented contract holds and "
        "the public tests pass. Do not change the cursor codec or the error type."
    ),
    write_scope=("pager/page.py",),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "pager/__init__.py": _INIT,
        "pager/errors.py": _ERRORS,
        "pager/cursor.py": _CURSOR,
        "pager/page.py": _PAGE,
        "tests/test_cursor.py": _TEST_CURSOR,
        "tests/test_public_page.py": _TEST_PUBLIC,
    },
    reference={"pager/page.py": _REFERENCE},
    wrong=(
        WrongVariant(
            name="start_not_clamped",
            covers="a start past the end producing an inverted slice",
            file="pager/page.py",
            edits=(
                (
                    "    if start > total:\n        start = total\n",
                    "    if False:\n        start = total\n",
                ),
            ),
        ),
        WrongVariant(
            name="cursor_never_ends",
            covers="next_cursor returning a cursor on the last page",
            file="pager/page.py",
            edits=(
                (
                    "    return encode_cursor(stop) if stop < total else None",
                    "    return encode_cursor(stop)",
                ),
            ),
        ),
        WrongVariant(
            name="zero_limit_allowed",
            covers="a limit of zero accepted, so paging cannot advance",
            file="pager/page.py",
            edits=(
                (
                    '    if limit <= 0:\n        raise ValueError("limit must be positive")\n    start =',
                    '    if limit < 0:\n        raise ValueError("limit must be positive")\n    start =',
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_cursor.py",),
    target_command=("tests/test_public_page.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 5,
        "target_tests": 6,
        "reference_tests": 11,
        "independent_investigations": 0,
        "acceptance_difficulty": "LOW",
    },
    labels={
        "change_surface": "1 file, 2 functions",
        "key_dependencies": "pager.cursor codec and its strict decoding",
        "oracle_scope": "empty collection, page tiling, purity, strict decoding, zero limit",
    },
    notes="The public suite states the clamp and the error; the tiling property is oracle-only.",
)
