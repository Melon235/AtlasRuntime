"""Task ``cascade`` — a parser extension that cannot be made in one file.

The lexer decides which operators exist; the parser decides how they associate. Adding the power
operator requires an edit in both, and the two edits are only correct together: a lexer that emits
``^`` into a left-associative parser produces a wrong tree, and a parser that knows ``^`` never sees
it if the lexer rejects the character. Splitting the work between two workers means splitting one
grammar.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "cascade"

_README = """# cascade

A tiny expression cascade that prints its own parse tree.

`cascade.lexer` turns text into tokens. `cascade.parser` turns tokens into a tree and renders it
fully parenthesised. Only `+ - * /` and unary minus are supported today.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Expression cascade."""

from .errors import LexError, ParseError
from .lexer import OPERATORS, tokenize
from .parser import Node, normalize, render
from .tokens import LPAREN, NUMBER, OP, RPAREN, Token

__all__ = [
    "LPAREN",
    "NUMBER",
    "OP",
    "RPAREN",
    "OPERATORS",
    "LexError",
    "Node",
    "ParseError",
    "Token",
    "normalize",
    "render",
    "tokenize",
]
'''

_ERRORS = '''"""The two failures of the cascade."""

from __future__ import annotations


class LexError(ValueError):
    """A character that is not part of this language."""

    def __init__(self, message: str, position: int = -1) -> None:
        super().__init__(message)
        self.position = position


class ParseError(ValueError):
    """A token sequence that is not an expression."""

    def __init__(self, message: str, position: int = -1) -> None:
        super().__init__(message)
        self.position = position
'''

_TOKENS = '''"""Token kinds and the token value."""

from __future__ import annotations

from dataclasses import dataclass

NUMBER = "NUMBER"
OP = "OP"
LPAREN = "LPAREN"
RPAREN = "RPAREN"


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    position: int = -1
'''

_LEXER = '''"""The tokenizer.

Whitespace separates tokens. A number is a run of digits. The operator set is the single place that
decides which operators this language has; anything else is a :class:`~cascade.errors.LexError`.
"""

from __future__ import annotations

from .errors import LexError
from .tokens import LPAREN, NUMBER, OP, RPAREN, Token

OPERATORS = frozenset({"+", "-", "*", "/"})


def tokenize(text: str) -> tuple[Token, ...]:
    """Every token of ``text``, in order."""

    tokens: list[Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char.isdigit():
            start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            tokens.append(Token(NUMBER, text[start:index], start))
            continue
        if char in OPERATORS:
            tokens.append(Token(OP, char, index))
            index += 1
            continue
        if char == "(":
            tokens.append(Token(LPAREN, char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token(RPAREN, char, index))
            index += 1
            continue
        raise LexError(f"unknown character {char!r}", position=index)
    return tuple(tokens)
'''

_PARSER = '''"""Precedence climbing and canonical rendering.

``normalize(text)`` parses ``text`` and returns the fully parenthesised tree, so the structure is
visible instead of implied: ``"1+2*3"`` becomes ``"(1 + (2 * 3))"``.

Precedence: ``*`` and ``/`` bind tighter than ``+`` and ``-``; both are left-associative. Unary minus
binds looser than the tightest binary operator but tighter than ``*`` and ``/``, so ``-2*3`` renders as
``"((- 2) * 3)"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ParseError
from .lexer import tokenize
from .tokens import LPAREN, NUMBER, OP, RPAREN, Token

_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 2, "/": 2}
_UNARY_PRECEDENCE = 3


@dataclass(frozen=True)
class Node:
    kind: str
    text: str = ""
    left: "Node | None" = None
    right: "Node | None" = None


def render(node: Node) -> str:
    """The canonical text of one node."""

    if node.kind == "number":
        return node.text
    if node.kind == "unary":
        return f"(- {render(node.right)})"
    return f"({render(node.left)} {node.text} {render(node.right)})"


class _Parser:
    def __init__(self, tokens: tuple[Token, ...]) -> None:
        self._tokens = tokens
        self._index = 0

    def _peek(self) -> Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def parse(self) -> Node:
        node = self._binary(1)
        if self._peek() is not None:
            raise ParseError("unexpected trailing tokens", position=self._peek().position)
        return node

    def _binary(self, min_precedence: int) -> Node:
        left = self._unary()
        while True:
            token = self._peek()
            if token is None or token.kind != OP:
                return left
            precedence = _PRECEDENCE.get(token.text)
            if precedence is None or precedence < min_precedence:
                return left
            self._index += 1
            right = self._binary(precedence + 1)
            left = Node("binary", token.text, left, right)

    def _unary(self) -> Node:
        token = self._peek()
        if token is not None and token.kind == OP and token.text == "-":
            self._index += 1
            return Node("unary", "-", None, self._binary(_UNARY_PRECEDENCE))
        return self._primary()

    def _primary(self) -> Node:
        token = self._peek()
        if token is None:
            raise ParseError("unexpected end of input")
        if token.kind == NUMBER:
            self._index += 1
            return Node("number", token.text)
        if token.kind == LPAREN:
            self._index += 1
            node = self._binary(1)
            closing = self._peek()
            if closing is None or closing.kind != RPAREN:
                raise ParseError("missing closing parenthesis", position=token.position)
            self._index += 1
            return node
        raise ParseError(f"unexpected token {token.text!r}", position=token.position)


def normalize(text: str) -> str:
    """Parse ``text`` and return its fully parenthesised form."""

    return render(_Parser(tokenize(text)).parse())
'''

_TEST_LEXER = """import pytest

from cascade.errors import LexError
from cascade.lexer import tokenize
from cascade.tokens import LPAREN, NUMBER, OP, RPAREN


def test_numbers_and_operators() -> None:
    tokens = tokenize("1 + 22")
    assert [(item.kind, item.text) for item in tokens] == [
        (NUMBER, "1"),
        (OP, "+"),
        (NUMBER, "22"),
    ]


def test_parentheses_are_tokens() -> None:
    kinds = [item.kind for item in tokenize("(1)")]
    assert kinds == [LPAREN, NUMBER, RPAREN]


def test_whitespace_is_ignored() -> None:
    assert len(tokenize("  1\\t+\\n2  ")) == 3


def test_positions_are_recorded() -> None:
    assert [item.position for item in tokenize("1+2")] == [0, 1, 2]


def test_an_unknown_character_is_rejected() -> None:
    with pytest.raises(LexError):
        tokenize("1 $ 2")
"""

_TEST_NORMALIZE = """import pytest

from cascade.errors import ParseError
from cascade.parser import normalize


def test_addition() -> None:
    assert normalize("1+2") == "(1 + 2)"


def test_multiplication_binds_tighter() -> None:
    assert normalize("1+2*3") == "(1 + (2 * 3))"


def test_left_associativity() -> None:
    assert normalize("1-2-3") == "((1 - 2) - 3)"


def test_parentheses_are_preserved() -> None:
    assert normalize("(1+2)*3") == "((1 + 2) * 3)"


def test_unary_minus() -> None:
    assert normalize("-2*3") == "((- 2) * 3)"
"""

_TEST_PUBLIC = """import pytest

from cascade.errors import ParseError
from cascade.parser import normalize


def test_a_single_power() -> None:
    assert normalize("2 ^ 8") == "(2 ^ 8)"


def test_power_is_right_associative() -> None:
    assert normalize("2 ^ 3 ^ 2") == "(2 ^ (3 ^ 2))"


def test_power_binds_tighter_than_multiplication() -> None:
    assert normalize("2 * 3 ^ 2") == "(2 * (3 ^ 2))"


def test_multiplication_after_a_power() -> None:
    assert normalize("2 ^ 3 * 4") == "((2 ^ 3) * 4)"


def test_unary_minus_is_looser_than_power() -> None:
    assert normalize("-2 ^ 2") == "(- (2 ^ 2))"


def test_grouping_under_a_power() -> None:
    assert normalize("(1 + 2) ^ 2") == "((1 + 2) ^ 2)"


def test_a_dangling_operator_is_rejected() -> None:
    with pytest.raises(ParseError):
        normalize("2 ^")
"""

_REFERENCE_LEXER = '''"""The tokenizer.

Whitespace separates tokens. A number is a run of digits. The operator set is the single place that
decides which operators this language has; anything else is a :class:`~cascade.errors.LexError`.
"""

from __future__ import annotations

from .errors import LexError
from .tokens import LPAREN, NUMBER, OP, RPAREN, Token

OPERATORS = frozenset({"+", "-", "*", "/", "^"})


def tokenize(text: str) -> tuple[Token, ...]:
    """Every token of ``text``, in order."""

    tokens: list[Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char.isdigit():
            start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            tokens.append(Token(NUMBER, text[start:index], start))
            continue
        if char in OPERATORS:
            tokens.append(Token(OP, char, index))
            index += 1
            continue
        if char == "(":
            tokens.append(Token(LPAREN, char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token(RPAREN, char, index))
            index += 1
            continue
        raise LexError(f"unknown character {char!r}", position=index)
    return tuple(tokens)
'''

_REFERENCE_PARSER = '''"""Precedence climbing and canonical rendering.

``normalize(text)`` parses ``text`` and returns the fully parenthesised tree, so the structure is
visible instead of implied: ``"1+2*3"`` becomes ``"(1 + (2 * 3))"``.

Precedence: ``^`` binds tighter than ``*`` and ``/``, which bind tighter than ``+`` and ``-``. ``^`` is
right-associative; every other binary operator is left-associative. Unary minus binds looser than
``^`` but tighter than ``*`` and ``/``, so ``-2^2`` renders as ``"(- (2 ^ 2))"`` and ``-2*3`` as
``"((- 2) * 3)"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ParseError
from .lexer import tokenize
from .tokens import LPAREN, NUMBER, OP, RPAREN, Token

_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 2, "/": 2, "^": 4}
_UNARY_PRECEDENCE = 3
_RIGHT_ASSOCIATIVE = frozenset({"^"})


@dataclass(frozen=True)
class Node:
    kind: str
    text: str = ""
    left: "Node | None" = None
    right: "Node | None" = None


def render(node: Node) -> str:
    """The canonical text of one node."""

    if node.kind == "number":
        return node.text
    if node.kind == "unary":
        return f"(- {render(node.right)})"
    return f"({render(node.left)} {node.text} {render(node.right)})"


class _Parser:
    def __init__(self, tokens: tuple[Token, ...]) -> None:
        self._tokens = tokens
        self._index = 0

    def _peek(self) -> Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def parse(self) -> Node:
        node = self._binary(1)
        if self._peek() is not None:
            raise ParseError("unexpected trailing tokens", position=self._peek().position)
        return node

    def _binary(self, min_precedence: int) -> Node:
        left = self._unary()
        while True:
            token = self._peek()
            if token is None or token.kind != OP:
                return left
            precedence = _PRECEDENCE.get(token.text)
            if precedence is None or precedence < min_precedence:
                return left
            self._index += 1
            right = self._binary(precedence if token.text in _RIGHT_ASSOCIATIVE else precedence + 1)
            left = Node("binary", token.text, left, right)

    def _unary(self) -> Node:
        token = self._peek()
        if token is not None and token.kind == OP and token.text == "-":
            self._index += 1
            return Node("unary", "-", None, self._binary(_UNARY_PRECEDENCE))
        return self._primary()

    def _primary(self) -> Node:
        token = self._peek()
        if token is None:
            raise ParseError("unexpected end of input")
        if token.kind == NUMBER:
            self._index += 1
            return Node("number", token.text)
        if token.kind == LPAREN:
            self._index += 1
            node = self._binary(1)
            closing = self._peek()
            if closing is None or closing.kind != RPAREN:
                raise ParseError("missing closing parenthesis", position=token.position)
            self._index += 1
            return node
        raise ParseError(f"unexpected token {token.text!r}", position=token.position)


def normalize(text: str) -> str:
    """Parse ``text`` and return its fully parenthesised form."""

    return render(_Parser(tokenize(text)).parse())
'''

_ORACLE = """import traceback

from cascade.errors import LexError, ParseError
from cascade.lexer import tokenize
from cascade.parser import normalize

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def canon(text, expected):
    got = normalize(text)
    check(got == expected, f"normalize({text!r}) == {got!r}, expected {expected!r}")


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
    # Precedence and associativity of the new operator in both directions.
    canon("2 * 3 ^ 4", "(2 * (3 ^ 4))")
    canon("2 ^ 3 * 4", "((2 ^ 3) * 4)")
    canon("2 ^ 3 ^ 4 ^ 5", "(2 ^ (3 ^ (4 ^ 5)))")
    canon("1 + 2 ^ 3", "(1 + (2 ^ 3))")
    canon("2 ^ 3 + 1", "((2 ^ 3) + 1)")
    # Unary minus: looser than power, tighter than multiplication, and usable on the right operand.
    canon("-2 ^ 2", "(- (2 ^ 2))")
    canon("(-2) ^ 2", "((- 2) ^ 2)")
    canon("-2 * 3", "((- 2) * 3)")
    canon("2 ^ -1", "(2 ^ (- 1))")
    # Division stays left-associative.
    canon("2 / 3 * 4", "((2 / 3) * 4)")
    # Whitespace and multi-digit numbers survive unchanged.
    canon("  12 ^  30 ", "(12 ^ 30)")
    canon("12345", "12345")
    # The canonical form is a fixed point: normalising it again changes nothing.
    for text in ("1+2*3", "2^3^2", "-2^2*3", "(1+2)^2"):
        once = normalize(text)
        check(normalize(once) == once, f"normalize is not idempotent for {text!r}")
    # Ill-formed input is rejected, not repaired.
    for text in ("1 2", "2 ^", "^2", "()", "(1", "2 ** 3"):
        raises((ParseError, LexError), lambda t=text: normalize(t), f"malformed {text!r}")
    # The lexer exposes the operator it now supports.
    check([item.text for item in tokenize("1^2")] == ["1", "^", "2"], "lexer does not emit ^")
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
    title="Power operator in a coupled lexer and parser",
    structure="COUPLED_UNCERTAIN",
    rationale=(
        "The lexer owns the operator set and the parser owns precedence and associativity. Neither "
        "file can be changed correctly without the other, and the sharp edges are exactly where they "
        "meet: a token the lexer emits but the parser associates wrongly, or a grammar rule the parser "
        "supports but the lexer never delivers. Delegating the two files to two workers splits one "
        "grammar and the integration cost is the work."
    ),
    goal=(
        "The cascade project renders the parse tree of arithmetic expressions but only knows "
        "+ - * / and unary minus. Extend it with the power operator ^, right-associative and binding "
        "tighter than multiplication, so the public tests pass. Keep the existing behaviour for every "
        "other operator unchanged."
    ),
    write_scope=("cascade/lexer.py", "cascade/parser.py"),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "cascade/__init__.py": _INIT,
        "cascade/errors.py": _ERRORS,
        "cascade/tokens.py": _TOKENS,
        "cascade/lexer.py": _LEXER,
        "cascade/parser.py": _PARSER,
        "tests/test_lexer.py": _TEST_LEXER,
        "tests/test_normalize_basic.py": _TEST_NORMALIZE,
        "tests/test_public_power.py": _TEST_PUBLIC,
    },
    reference={"cascade/lexer.py": _REFERENCE_LEXER, "cascade/parser.py": _REFERENCE_PARSER},
    wrong=(
        WrongVariant(
            name="power_left_associative",
            covers="2^3^2 grouped to the left",
            file="cascade/parser.py",
            edits=(
                (
                    "            right = self._binary(precedence if token.text in _RIGHT_ASSOCIATIVE else precedence + 1)",
                    "            right = self._binary(precedence + 1)",
                ),
            ),
        ),
        WrongVariant(
            name="power_lowest_precedence",
            covers="power evaluated after multiplication",
            file="cascade/parser.py",
            edits=(
                (
                    '_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 2, "/": 2, "^": 4}',
                    '_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 2, "/": 2, "^": 0}',
                ),
            ),
        ),
        WrongVariant(
            name="unary_tighter_than_power",
            covers="-2^2 parsed as (-2)^2",
            file="cascade/parser.py",
            edits=(("_UNARY_PRECEDENCE = 3", "_UNARY_PRECEDENCE = 5"),),
        ),
        WrongVariant(
            name="trailing_tokens_ignored",
            covers="two expressions accepted as one",
            file="cascade/parser.py",
            edits=(
                (
                    '        if self._peek() is not None:\n            raise ParseError("unexpected trailing tokens", position=self._peek().position)',
                    '        if False:\n            raise ParseError("unexpected trailing tokens", position=self._peek().position)',
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_lexer.py", "tests/test_normalize_basic.py"),
    target_command=("tests/test_public_power.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 10,
        "target_tests": 7,
        "reference_tests": 17,
        "independent_investigations": 0,
        "acceptance_difficulty": "HIGH",
    },
    labels={
        "change_surface": "2 files, 3 edits; one shared grammar",
        "key_dependencies": "OPERATORS set, _PRECEDENCE, _RIGHT_ASSOCIATIVE, _UNARY_PRECEDENCE",
        "oracle_scope": "precedence both directions, unary interaction, idempotence, malformed input",
    },
    notes="The lexer change alone makes every test worse, not better: the parser then sees '^'.",
)
