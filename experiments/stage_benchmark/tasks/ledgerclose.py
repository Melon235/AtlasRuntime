"""Task ``ledgerclose`` — accounting rules and FX rules behind one closing statement.

``ledgerclose.ledger`` (double-entry validation over a chart of accounts) and ``ledgerclose.rates``
(as-of quote lookup and half-even conversion) are complete, independent, and share no types. The
shared ``close`` layer must use both. Two genuinely different investigations, each with its own
failure vocabulary.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "ledgerclose"

_README = """# ledgerclose

Month-end close for a small multi-currency book.

* `ledgerclose.ledger` validates double-entry transactions against the chart of accounts.
* `ledgerclose.rates` resolves an FX quote as of a date and converts cents.
* `ledgerclose.close` turns validated transactions into a closing statement. It is incomplete.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Month-end close."""

from .close import Line, Statement, closing_statement
from .errors import FxError, LedgerError
from .ledger import ACCOUNTS, Entry, Transaction, validate_entries
from .rates import RateTable, convert

__all__ = [
    "ACCOUNTS",
    "Entry",
    "FxError",
    "LedgerError",
    "Line",
    "RateTable",
    "Statement",
    "Transaction",
    "closing_statement",
    "convert",
    "validate_entries",
]
'''

_ERRORS = '''"""The two failure vocabularies of this package."""

from __future__ import annotations


class LedgerError(ValueError):
    """A transaction that cannot be posted."""

    def __init__(self, message: str, code: str = "invalid_entry") -> None:
        super().__init__(message)
        self.code = code


class FxError(ValueError):
    """A conversion that cannot be performed."""

    def __init__(self, message: str, code: str = "missing_rate") -> None:
        super().__init__(message)
        self.code = code
'''

_LEDGER = '''"""Double-entry validation.

A transaction is a set of entries that sums to zero in its own currency. Every account must exist in
:data:`ACCOUNTS`, and an amount is a non-zero integer number of cents.

Validation raises on the first problem found, in row order. It never repairs a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LedgerError

ACCOUNTS: dict[str, str] = {
    "cash": "asset",
    "receivable": "asset",
    "payable": "liability",
    "equity": "equity",
    "revenue": "revenue",
    "expense": "expense",
}

CURRENCIES = frozenset({"USD", "EUR", "GBP"})


@dataclass(frozen=True)
class Entry:
    account: str
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class Transaction:
    txn: str
    entries: tuple[Entry, ...]

    @property
    def total_cents(self) -> int:
        return sum(item.amount_cents for item in self.entries)


def validate_entries(rows: list[dict[str, object]] | tuple[dict[str, object], ...]) -> tuple[Transaction, ...]:
    """Group rows into transactions and validate every one of them."""

    grouped: dict[str, list[Entry]] = {}
    for row in rows:
        txn = row.get("txn")
        account = row.get("account")
        amount = row.get("amount")
        currency = row.get("currency")
        if not isinstance(txn, str) or not txn:
            raise LedgerError("every row needs a transaction id", code="invalid_entry")
        if account not in ACCOUNTS:
            raise LedgerError(f"unknown account {account!r}", code="unknown_account")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0:
            raise LedgerError(f"amount must be a non-zero int, got {amount!r}", code="invalid_amount")
        if currency not in CURRENCIES:
            raise LedgerError(f"unknown currency {currency!r}", code="unknown_currency")
        grouped.setdefault(txn, []).append(
            Entry(account=str(account), amount_cents=amount, currency=str(currency))
        )
    transactions: list[Transaction] = []
    for txn in sorted(grouped):
        entries = tuple(grouped[txn])
        if sum(item.amount_cents for item in entries) != 0:
            raise LedgerError(f"transaction {txn!r} does not balance", code="unbalanced")
        transactions.append(Transaction(txn=txn, entries=entries))
    return tuple(transactions)
'''

_RATES = '''"""FX quotes and conversion.

A quote is ``(currency, date, rate)``: the value of one unit of ``currency`` in the table's base
currency on ``date``. A lookup takes the latest quote with ``date <= on``; quoting a currency on a
later date must not make that quote usable early. Converting an amount already in the base currency
never needs a quote.

Conversion rounds to the nearest cent with half-to-even, the rounding a ledger uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from .errors import FxError


@dataclass(frozen=True)
class RateTable:
    base: str
    quotes: tuple[tuple[str, str, float], ...] = ()

    def rate_on(self, currency: str, on: str) -> float:
        """The rate for ``currency`` as of ``on``."""

        if currency == self.base:
            return 1.0
        usable = sorted(
            (date, rate) for code, date, rate in self.quotes if code == currency and date <= on
        )
        if not usable:
            raise FxError(f"no {currency}/{self.base} quote on or before {on}", code="missing_rate")
        return usable[-1][1]


def convert(amount_cents: int, currency: str, table: RateTable, on: str) -> int:
    """``amount_cents`` in ``currency`` expressed in the table's base currency."""

    if currency == table.base:
        return amount_cents
    rate = table.rate_on(currency, on)
    scaled = Decimal(amount_cents) * Decimal(str(rate))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
'''

_CLOSE = '''"""The closing statement.

A statement reports, in the table's base currency, the net balance of every account that appears in
at least one transaction, plus debit and credit totals.

Rules:

* the transactions are validated first, so an unbalanced book raises before any conversion;
* every entry is converted as of the statement date;
* an entry already in the base currency is not converted, and needs no quote;
* balances are ordered by account name, ascending;
* ``totals["debit"]`` is the sum of the positive balances and ``totals["credit"]`` the sum of the
  absolute negative balances; neither is negative, and for a balanced book they are equal;
* the statement is a function of the transaction set, not of row order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rates import RateTable


@dataclass(frozen=True)
class Line:
    account: str
    amount_cents: int


@dataclass(frozen=True)
class Statement:
    base: str
    on: str
    lines: tuple[Line, ...]
    totals: dict[str, int]


def closing_statement(rows: list[dict[str, object]] | tuple[dict[str, object], ...], table: RateTable, on: str) -> Statement:
    """The statement for ``rows`` as of ``on``."""

    raise NotImplementedError("closing_statement is not implemented yet")
'''

_TEST_LEDGER = """import pytest

from ledgerclose.errors import LedgerError
from ledgerclose.ledger import validate_entries


def _rows():
    return [
        {"txn": "t1", "account": "cash", "amount": 500, "currency": "USD"},
        {"txn": "t1", "account": "revenue", "amount": -500, "currency": "USD"},
    ]


def test_a_balanced_transaction_passes() -> None:
    transactions = validate_entries(_rows())
    assert [(item.txn, item.total_cents) for item in transactions] == [("t1", 0)]


def test_transactions_are_returned_in_id_order() -> None:
    rows = _rows() + [
        {"txn": "t0", "account": "cash", "amount": 1, "currency": "USD"},
        {"txn": "t0", "account": "equity", "amount": -1, "currency": "USD"},
    ]
    assert [item.txn for item in validate_entries(rows)] == ["t0", "t1"]


def test_an_unbalanced_transaction_is_rejected() -> None:
    rows = _rows()
    rows[1]["amount"] = -400
    with pytest.raises(LedgerError) as error:
        validate_entries(rows)
    assert error.value.code == "unbalanced"


def test_an_unknown_account_is_rejected() -> None:
    rows = _rows()
    rows[0]["account"] = "petty"
    with pytest.raises(LedgerError) as error:
        validate_entries(rows)
    assert error.value.code == "unknown_account"


def test_a_zero_amount_is_rejected() -> None:
    rows = _rows()
    rows[0]["amount"] = 0
    with pytest.raises(LedgerError) as error:
        validate_entries(rows)
    assert error.value.code == "invalid_amount"


def test_an_unknown_currency_is_rejected() -> None:
    rows = _rows()
    rows[0]["currency"] = "JPY"
    with pytest.raises(LedgerError) as error:
        validate_entries(rows)
    assert error.value.code == "unknown_currency"
"""

_TEST_RATES = """import pytest

from ledgerclose.errors import FxError
from ledgerclose.rates import RateTable, convert

TABLE = RateTable(
    base="USD",
    quotes=(("EUR", "2026-01-01", 1.10), ("EUR", "2026-06-01", 1.20)),
)


def test_the_base_currency_is_identity() -> None:
    assert convert(1234, "USD", TABLE, "2026-03-01") == 1234


def test_the_latest_usable_quote_is_used() -> None:
    assert convert(100, "EUR", TABLE, "2026-03-01") == 110


def test_a_later_quote_becomes_usable_on_its_date() -> None:
    assert convert(100, "EUR", TABLE, "2026-07-01") == 120


def test_a_date_before_every_quote_is_an_error() -> None:
    with pytest.raises(FxError) as error:
        convert(100, "EUR", TABLE, "2025-12-31")
    assert error.value.code == "missing_rate"


def test_a_currency_without_quotes_is_an_error() -> None:
    with pytest.raises(FxError):
        convert(100, "GBP", TABLE, "2026-03-01")
"""

_TEST_PUBLIC = """import pytest

from ledgerclose import FxError, LedgerError, RateTable, closing_statement

TABLE = RateTable(base="USD", quotes=(("EUR", "2026-01-01", 1.10),))


def _usd_book():
    return [
        {"txn": "t1", "account": "cash", "amount": 1000, "currency": "USD"},
        {"txn": "t1", "account": "revenue", "amount": -1000, "currency": "USD"},
    ]


def _balances(statement):
    return {line.account: line.amount_cents for line in statement.lines}


def test_a_single_transaction_closes() -> None:
    statement = closing_statement(_usd_book(), TABLE, "2026-03-01")
    assert statement.base == "USD"
    assert _balances(statement) == {"cash": 1000, "revenue": -1000}


def test_foreign_entries_are_converted() -> None:
    book = [
        {"txn": "t1", "account": "cash", "amount": 100, "currency": "EUR"},
        {"txn": "t1", "account": "revenue", "amount": -100, "currency": "EUR"},
    ]
    statement = closing_statement(book, TABLE, "2026-03-01")
    assert _balances(statement) == {"cash": 110, "revenue": -110}


def test_balances_are_ordered_by_account() -> None:
    book = _usd_book() + [
        {"txn": "t2", "account": "equity", "amount": 5, "currency": "USD"},
        {"txn": "t2", "account": "cash", "amount": -5, "currency": "USD"},
    ]
    statement = closing_statement(book, TABLE, "2026-03-01")
    names = [line.account for line in statement.lines]
    assert names == sorted(names)


def test_an_unbalanced_book_raises_before_conversion() -> None:
    with pytest.raises(LedgerError):
        closing_statement([{"txn": "t1", "account": "cash", "amount": 5, "currency": "USD"}], TABLE, "2026-03-01")


def test_a_missing_rate_raises() -> None:
    book = [
        {"txn": "t1", "account": "cash", "amount": 100, "currency": "EUR"},
        {"txn": "t1", "account": "revenue", "amount": -100, "currency": "EUR"},
    ]
    with pytest.raises(FxError):
        closing_statement(book, TABLE, "2025-01-01")


def test_debit_and_credit_totals_are_equal() -> None:
    book = [
        {"txn": "t1", "account": "cash", "amount": 100, "currency": "EUR"},
        {"txn": "t1", "account": "revenue", "amount": -100, "currency": "EUR"},
        {"txn": "t2", "account": "expense", "amount": 7, "currency": "USD"},
        {"txn": "t2", "account": "cash", "amount": -7, "currency": "USD"},
    ]
    totals = closing_statement(book, TABLE, "2026-03-01").totals
    assert totals["debit"] == totals["credit"]


def test_row_order_does_not_change_the_statement() -> None:
    book = _usd_book()
    assert closing_statement(book, TABLE, "2026-03-01").lines == closing_statement(
        list(reversed(book)), TABLE, "2026-03-01"
    ).lines
"""

_REFERENCE = '''"""The closing statement.

A statement reports, in the table's base currency, the net balance of every account that appears in
at least one transaction, plus debit and credit totals.

Rules:

* the transactions are validated first, so an unbalanced book raises before any conversion;
* every entry is converted as of the statement date;
* an entry already in the base currency is not converted, and needs no quote;
* balances are ordered by account name, ascending;
* ``totals["debit"]`` is the sum of the positive balances and ``totals["credit"]`` the sum of the
  absolute negative balances; neither is negative, and for a balanced book they are equal;
* the statement is a function of the transaction set, not of row order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import validate_entries
from .rates import RateTable, convert


@dataclass(frozen=True)
class Line:
    account: str
    amount_cents: int


@dataclass(frozen=True)
class Statement:
    base: str
    on: str
    lines: tuple[Line, ...]
    totals: dict[str, int]


def closing_statement(rows: list[dict[str, object]] | tuple[dict[str, object], ...], table: RateTable, on: str) -> Statement:
    """The statement for ``rows`` as of ``on``."""

    transactions = validate_entries(rows)
    balances: dict[str, int] = {}
    for transaction in transactions:
        for entry in transaction.entries:
            converted = convert(entry.amount_cents, entry.currency, table, on)
            balances[entry.account] = balances.get(entry.account, 0) + converted
    debit = sum(value for value in balances.values() if value > 0)
    credit = -sum(value for value in balances.values() if value < 0)
    lines = tuple(
        Line(account=account, amount_cents=amount) for account, amount in sorted(balances.items())
    )
    return Statement(
        base=table.base,
        on=on,
        lines=lines,
        totals={"debit": debit, "credit": credit},
    )
'''

_ORACLE = """import traceback

from ledgerclose.close import Line
from ledgerclose.errors import FxError, LedgerError
from ledgerclose.rates import RateTable
from ledgerclose.ledger import validate_entries

from ledgerclose import closing_statement

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


def balances(statement):
    return {line.account: line.amount_cents for line in statement.lines}


TABLE = RateTable(base="USD", quotes=(("EUR", "2026-01-01", 1.10),))

try:
    # A base-currency book closes with no quote at all: conversion must not be attempted.
    empty_table = RateTable(base="USD")
    book = [
        {"txn": "t1", "account": "cash", "amount": 250, "currency": "USD"},
        {"txn": "t1", "account": "revenue", "amount": -250, "currency": "USD"},
    ]
    statement = closing_statement(book, empty_table, "2030-01-01")
    check(balances(statement) == {"cash": 250, "revenue": -250}, "base currency needed a quote")

    # Half-even rounding, not half-up: 5 -> 6 but 15 -> 16, and 25 -> 28.
    two_quote = RateTable(base="USD", quotes=(("EUR", "2026-01-01", 1.10),))
    for amount, expected in ((5, 6), (15, 16), (25, 28)):
        pair = [
            {"txn": "t1", "account": "cash", "amount": amount, "currency": "EUR"},
            {"txn": "t1", "account": "revenue", "amount": -amount, "currency": "EUR"},
        ]
        got = balances(closing_statement(pair, two_quote, "2026-03-01"))["cash"]
        check(got == expected, f"conversion of {amount} EUR gave {got}, expected {expected}")

    # The as-of lookup must not use a quote dated after the statement date.
    late = RateTable(
        base="USD", quotes=(("EUR", "2026-01-01", 1.10), ("EUR", "2026-12-31", 9.99))
    )
    pair = [
        {"txn": "t1", "account": "cash", "amount": 100, "currency": "EUR"},
        {"txn": "t1", "account": "revenue", "amount": -100, "currency": "EUR"},
    ]
    check(balances(closing_statement(pair, late, "2026-06-01"))["cash"] == 110, "future quote used")
    check(
        balances(closing_statement(pair, late, "2027-01-01"))["cash"] == 999, "latest quote unused"
    )

    # Totals are non-negative and equal for any balanced book.
    totals = closing_statement(pair, TABLE, "2026-03-01").totals
    check(totals["debit"] >= 0 and totals["credit"] >= 0, "a total is negative")
    check(totals["debit"] == totals["credit"], "debit and credit totals differ")

    # Validation runs first: an unbalanced book raises LedgerError even when a rate is missing.
    raises(
        LedgerError,
        lambda: closing_statement(
            [{"txn": "t1", "account": "cash", "amount": 5, "currency": "EUR"}],
            RateTable(base="USD"),
            "2026-03-01",
        ),
        "validation order",
    )
    raises(
        FxError,
        lambda: closing_statement(pair, RateTable(base="USD"), "2026-03-01"),
        "missing rate",
    )

    # Zero-amount rows never reach the statement, in validation or through the close.
    raises(
        LedgerError,
        lambda: validate_entries([{"txn": "t1", "account": "cash", "amount": 0, "currency": "USD"}]),
        "zero amount",
    )
    raises(
        LedgerError,
        lambda: closing_statement(
            [
                {"txn": "t1", "account": "cash", "amount": 0, "currency": "USD"},
                {"txn": "t1", "account": "revenue", "amount": 0, "currency": "USD"},
            ],
            TABLE,
            "2026-03-01",
        ),
        "zero amount through the close",
    )

    # Lines are values, not tuples or dicts, and the set is stable under repeated calls.
    first = closing_statement(pair, TABLE, "2026-03-01")
    second = closing_statement(pair, TABLE, "2026-03-01")
    check(first.lines == second.lines, "statement is not reproducible")
    check(all(isinstance(item, Line) for item in first.lines), "lines are not Line values")
    check(first.base == "USD" and first.on == "2026-03-01", "header is wrong")
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
    title="Multi-currency closing statement",
    structure="INDEPENDENT_INVESTIGATION",
    rationale=(
        "The accounting rules and the FX rules are separate packages with separate error types, "
        "separate value objects and no shared code. Either can be read and summarised in isolation; "
        "the closing layer is where both meet, and a mistake in either is invisible in the other's "
        "tests."
    ),
    goal=(
        "The ledgerclose project validates double-entry transactions and resolves FX quotes, but the "
        "closing statement in ledgerclose/close.py is unimplemented. Implement it so a validated book "
        "becomes a deterministic statement in the base currency, and verify with the public tests. "
        "Leave the ledger and rates packages unchanged."
    ),
    write_scope=("ledgerclose/close.py",),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "ledgerclose/__init__.py": _INIT,
        "ledgerclose/errors.py": _ERRORS,
        "ledgerclose/ledger.py": _LEDGER,
        "ledgerclose/rates.py": _RATES,
        "ledgerclose/close.py": _CLOSE,
        "tests/test_ledger.py": _TEST_LEDGER,
        "tests/test_rates.py": _TEST_RATES,
        "tests/test_public_close.py": _TEST_PUBLIC,
    },
    reference={"ledgerclose/close.py": _REFERENCE},
    wrong=(
        WrongVariant(
            name="no_conversion",
            covers="foreign amounts summed as if they were base currency",
            file="ledgerclose/close.py",
            edits=(
                (
                    "            converted = convert(entry.amount_cents, entry.currency, table, on)",
                    "            converted = entry.amount_cents",
                ),
            ),
        ),
        WrongVariant(
            name="rounds_half_up",
            covers="money rounded away from even on a half cent",
            file="ledgerclose/close.py",
            edits=(
                (
                    "            converted = convert(entry.amount_cents, entry.currency, table, on)",
                    "            converted = _half_up(entry.amount_cents, entry.currency, table, on)",
                ),
                (
                    "def closing_statement(",
                    "def _half_up(amount: int, currency: str, table: RateTable, on: str) -> int:\n"
                    "    from decimal import ROUND_HALF_UP, Decimal\n"
                    "\n"
                    "    if currency == table.base:\n"
                    "        return amount\n"
                    "    scaled = Decimal(amount) * Decimal(str(table.rate_on(currency, on)))\n"
                    "    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))\n"
                    "\n"
                    "\n"
                    "def closing_statement(",
                ),
            ),
        ),
        WrongVariant(
            name="credits_are_negative",
            covers="the credit total reported as a negative number",
            file="ledgerclose/close.py",
            edits=(
                (
                    "    credit = -sum(value for value in balances.values() if value < 0)",
                    "    credit = sum(value for value in balances.values() if value < 0)",
                ),
            ),
        ),
        WrongVariant(
            name="zero_amounts_skipped",
            covers="a zero-amount row dropped instead of rejected",
            file="ledgerclose/close.py",
            edits=(
                (
                    "    transactions = validate_entries(rows)",
                    '    transactions = validate_entries([row for row in rows if row.get("amount") != 0])',
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_ledger.py", "tests/test_rates.py"),
    target_command=("tests/test_public_close.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 11,
        "target_tests": 7,
        "reference_tests": 18,
        "independent_investigations": 2,
        "acceptance_difficulty": "MEDIUM",
    },
    labels={
        "change_surface": "1 file, 1 function; reads two packages",
        "key_dependencies": "validate_entries, RateTable.rate_on, convert (half-even)",
        "oracle_scope": "base-currency shortcut, half-even ties, as-of lookup, validation order, totals",
    },
    notes="The public suite states neither the base-currency shortcut nor the rounding mode.",
)
