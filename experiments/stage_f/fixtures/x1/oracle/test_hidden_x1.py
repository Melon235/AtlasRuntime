"""Hidden checks for the v2 audit migration (never staged into the agent workspace)."""

import ast
import inspect

import pytest

from billing import charge as charge_module
from billing import ledger, reconciliation, refund as refund_module, settlement
from billing import statements as statements_module
from billing.charge import charge
from billing.reconciliation import reconcile
from billing.refund import refund
from billing.settlement import settle
from billing.statements import statement
from catalog.discounts import discounted_total
from events import subscribers as subscriber_module
from events.bus import EventBus
from events.subscribers import audit_subscriber
from foundation.audit import Receipt, record
from foundation.errors import DomainError
from foundation.models import LineItem, Order
from foundation.money import Money, format_minor, parse_minor
from inventory import reservations as reservations_module
from inventory import transfers as transfer_module
from inventory.reservations import reserve
from inventory.stock import InsufficientStock, StockLedger
from inventory.transfers import transfer
from notifications import service as notifier_module
from notifications.service import Notifier
from notifications.templates import Templates
from reporting import daily as daily_module, export as export_module, monthly as monthly_module
from reporting.daily import daily
from reporting.export import export
from reporting.monthly import monthly
from returns import service as return_module
from returns import workflow as workflow_module
from returns.service import request_return
from returns.workflow import InvalidReturnStep, ReturnWorkflow, open_case
from search.index import index


def make_order(order_id="O-1", quantity=2, currency="EUR", placed_on="2026-01-02") -> Order:
    line = LineItem(sku="notebook", quantity=quantity, unit_price=Money(1250, currency))
    return Order(order_id=order_id, placed_on=placed_on, lines=(line,))


def _positional_audit_calls(module, names: set[str]) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            label = function.id
        elif isinstance(function, ast.Attribute):
            label = function.attr
        else:
            continue
        if label in names and node.args:
            bad.append(ast.unparse(node))
    return bad


def test_no_audit_call_in_a_repaired_module_is_positional():
    modules = (
        charge_module,
        refund_module,
        ledger,
        reconciliation,
        daily_module,
        monthly_module,
        export_module,
        statements_module,
        return_module,
        workflow_module,
        reservations_module,
        transfer_module,
        subscriber_module,
        notifier_module,
    )
    for module in modules:
        assert _positional_audit_calls(module, {"record", "write_audit", "_emit", "_sink"}) == [], (
            module.__name__
        )


def test_settlement_goes_through_the_ledger_wrapper():
    tree = ast.parse(inspect.getsource(settlement))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "foundation.audit":
            imported.update(alias.name for alias in node.names)
    assert "record" not in imported
    receipts = settle((make_order(f"O-{index}") for index in range(3)), actor="alice")
    assert [receipt.data["order_id"] for receipt in receipts] == ["O-0", "O-1", "O-2"]
    assert all(receipt.kind == "settlement" for receipt in receipts)


def test_charge_and_refund_amounts_and_tax():
    assert charge(make_order(quantity=3), actor="alice").data == {
        "order_id": "O-1",
        "amount": 3750,
        "currency": "EUR",
        "tax": 750,
    }
    assert refund(make_order(quantity=3), actor="alice").data == {
        "order_id": "O-1",
        "amount": -3750,
        "currency": "EUR",
        "tax": -750,
    }


def test_ledger_validation_and_actor_forwarding():
    with pytest.raises(ValueError):
        ledger.post("settlement", 0, actor="alice", order_id="O-1")
    with pytest.raises(ValueError):
        ledger.post("settlement", True, actor="alice", order_id="O-1")
    with pytest.raises(ValueError):
        ledger.post("settlement", 10, actor="", order_id="O-1")
    receipt = ledger.post("adjustment", -5, actor="bob", order_id="O-9")
    assert (receipt.kind, receipt.actor) == ("adjustment", "bob")


def test_reconciliation_consumes_receipts():
    charges = [charge(make_order("O-1"), actor="alice"), charge(make_order("O-2"), actor="alice")]
    refunds = [refund(make_order("O-1", quantity=1), actor="alice")]
    receipt = reconcile(charges, refunds, actor="alice")
    assert receipt.kind == "reconciliation"
    assert receipt.data == {"charged": 5000, "refunded": 1250, "net": 3750}


def test_daily_empty_and_multiple_dates():
    empty = daily([], actor="alice")
    assert empty.data == {"date": "", "orders": 0, "total": 0}
    orders = [make_order("O-1", placed_on="2026-02-03"), make_order("O-2", placed_on="2026-02-01")]
    receipt = daily(orders, actor="alice")
    assert receipt.data["date"] == "2026-02-01"
    assert receipt.data["total"] == 5000


def test_monthly_empty_and_multiple_currencies():
    empty = monthly([], actor="alice")
    assert empty.data == {"month": "", "orders": 0, "total": 0, "currencies": []}
    orders = [
        make_order("O-1", currency="USD", placed_on="2026-03-01"),
        make_order("O-2", currency="EUR", placed_on="2026-03-04"),
    ]
    receipt = monthly(orders, actor="alice")
    assert receipt.data == {
        "month": "2026-03",
        "orders": 2,
        "total": 5000,
        "currencies": ["EUR", "USD"],
    }


def test_export_reports_rows_format_and_total():
    receipt = export([make_order("O-1"), make_order("O-2", quantity=1)], actor="alice")
    assert receipt.kind == "export"
    assert receipt.data == {"rows": 2, "format": "csv", "total": 3750}
    assert receipt.actor == "alice"


def test_bus_subscriber_uses_the_payload_and_the_event_actor():
    event = {"channel": "orders", "payload": [1, 2], "actor": "alice"}
    receipt = audit_subscriber(event)
    assert receipt.data == {"channel": "orders", "count": 2}
    assert receipt.actor == "alice"
    anonymous = audit_subscriber({"channel": "orders", "payload": []})
    assert anonymous.data["count"] == 0
    assert anonymous.actor is None


def test_notifier_forwards_keywords_and_supports_sms():
    order = make_order()
    notifier = Notifier(sink=record)
    for channel in ("email", "sms"):
        receipt = notifier.send(order, actor="alice", channel=channel)
        text = Templates().render(order, channel)
        assert receipt.data == {
            "order_id": "O-1",
            "channel": channel,
            "length": len(text),
        }
        assert receipt.actor == "alice"
    with pytest.raises(ValueError):
        notifier.send(order, actor="alice", channel="pigeon")
    with pytest.raises(ValueError):
        notifier.send(order, actor="")


def test_returns_policy_is_enforced_and_fees_are_in_minor_units():
    order = make_order()
    with pytest.raises(DomainError):
        request_return(
            order,
            actor="alice",
            delivered_on="2026-01-01",
            today="2026-03-15",
        )
    with pytest.raises(DomainError):
        request_return(
            order,
            actor="alice",
            delivered_on="2026-01-01",
            today="2026-01-20",
            reason="unknown",
        )
    damaged = request_return(
        order,
        actor="alice",
        delivered_on="2026-01-01",
        today="2026-01-25",
        reason="damaged",
    )
    assert damaged.data == {
        "order_id": "O-1",
        "reason": "damaged",
        "fee": 0,
        "refundable": 2500,
    }


def test_reservation_is_all_or_nothing_and_sums_units_per_sku():
    line = LineItem(sku="notebook", quantity=2, unit_price=Money(1250, "EUR"))
    pen = LineItem(sku="pen", quantity=5, unit_price=Money(199, "EUR"))
    order = Order(order_id="O-2", placed_on="2026-01-02", lines=(line, pen))
    stock = StockLedger({"notebook": 5, "pen": 4})
    with pytest.raises(InsufficientStock):
        reserve(order, actor="alice", stock=stock)
    assert stock.snapshot() == {"notebook": 5, "pen": 4}

    small_pen = LineItem(sku="pen", quantity=4, unit_price=Money(199, "EUR"))
    small = Order(order_id="O-3", placed_on="2026-01-02", lines=(small_pen,))
    receipt = reserve(small, actor="alice", stock=stock)
    assert receipt.data == {"order_id": "O-3", "units": 4, "skus": ["pen"]}
    assert stock.available("pen") == 0


def test_statement_handles_an_empty_run_and_the_discount_floor():
    empty = statement([], actor="alice")
    assert empty.data == {"orders": 0, "gross": 0, "credits": 0, "net": 0}
    receipt = statement([make_order("O-1"), make_order("O-2", quantity=1)], actor="alice")
    assert receipt.data == {"orders": 2, "gross": 3750, "credits": 375, "net": 3375}
    assert receipt.actor == "alice"


def test_the_return_workflow_rejects_illegal_steps_and_records_history():
    workflow = ReturnWorkflow(open_case("R-1", "O-1", "damaged", 2500))
    with pytest.raises(InvalidReturnStep):
        workflow.receive(actor="alice")
    rejected = workflow.reject(actor="alice")
    assert rejected.kind == "return_rejected"
    assert rejected.data["state"] == "rejected"
    assert workflow.case.state == "rejected"
    assert workflow.case.history == [("reject", "rejected")]
    with pytest.raises(InvalidReturnStep):
        workflow.approve(actor="alice")

    second = ReturnWorkflow(open_case("R-2", "O-2", "late", 1000))
    second.approve(actor="alice")
    second.receive(actor="alice")
    assert second.refund(actor="alice").data["amount"] == 1000
    with pytest.raises(InvalidReturnStep):
        second.refund(actor="alice")


def test_return_case_input_is_validated():
    with pytest.raises(DomainError):
        open_case("", "O-1", "damaged", 100)
    with pytest.raises(DomainError):
        open_case("R-1", "", "damaged", 100)
    with pytest.raises(DomainError):
        open_case("R-1", "O-1", "", 100)
    with pytest.raises(DomainError):
        open_case("R-1", "O-1", "damaged", 0)
    with pytest.raises(DomainError):
        open_case("R-1", "O-1", "damaged", True)


def test_a_transfer_validates_both_sides_and_keeps_stock_on_failure():
    stock = StockLedger({"notebook": 5, "pen": 1})
    with pytest.raises(DomainError):
        transfer(stock, actor="alice", from_sku="notebook", to_sku="notebook", units=1)
    with pytest.raises(DomainError):
        transfer(stock, actor="alice", from_sku="notebook", to_sku="pen", units=0)
    with pytest.raises(InsufficientStock):
        transfer(stock, actor="alice", from_sku="notebook", to_sku="pen", units=9)
    assert stock.snapshot() == {"notebook": 5, "pen": 1}
    receipt = transfer(stock, actor="bob", from_sku="notebook", to_sku="pen", units=2)
    assert (receipt.kind, receipt.actor) == ("transfer", "bob")
    assert stock.snapshot() == {"notebook": 3, "pen": 3}


def test_search_and_the_untouched_business_modules_still_work():
    receipt = index("atlas")
    assert receipt.data == {"term": "atlas"}
    order = make_order()
    assert discounted_total(order, "WELCOME10") == 2250
    assert format_minor(-1250, "eur") == "-12.50 EUR"
    assert parse_minor("12.3") == 1230


def test_the_frozen_api_copies_the_data_and_rejects_bad_arguments():
    payload = {"amount": 1}
    receipt = record(kind="probe", data=payload)
    payload["amount"] = 2
    assert receipt.data == {"amount": 1}
    for bad in ({"kind": "", "data": {}}, {"kind": "k", "data": []}, {"kind": "k", "data": {}, "actor": ""}):
        with pytest.raises(ValueError):
            record(**bad)
