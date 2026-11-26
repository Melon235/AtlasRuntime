"""Public checks for the frozen v2 audit migration."""

import pytest

from app.bootstrap import bootstrap
from billing.charge import charge
from billing.ledger import post
from billing.refund import refund
from billing.settlement import settle
from billing.statements import statement
from events.subscribers import audit_subscriber
from notifications.service import Notifier
from notifications.templates import Templates
from foundation.audit import Receipt, record
from foundation.models import LineItem, Order
from foundation.money import Money
from inventory.reservations import reserve
from inventory.stock import StockLedger
from inventory.transfers import transfer
from reporting.daily import daily
from reporting.monthly import monthly
from returns.service import request_return
from returns.workflow import InvalidReturnStep, ReturnWorkflow, open_case
from search.index import index


def make_order(order_id="O-1", quantity=2, currency="EUR", placed_on="2026-01-02") -> Order:
    line = LineItem(sku="notebook", quantity=quantity, unit_price=Money(1250, currency))
    return Order(order_id=order_id, placed_on=placed_on, lines=(line,))


def test_charge_returns_a_receipt_in_minor_units():
    receipt = charge(make_order(), actor="alice")
    assert isinstance(receipt, Receipt)
    assert receipt.kind == "charge"
    assert receipt.data == {
        "order_id": "O-1",
        "amount": 2500,
        "currency": "EUR",
        "tax": 500,
    }
    assert receipt.actor == "alice"


def test_refund_is_the_negative_of_the_charge():
    receipt = refund(make_order(), actor="alice")
    assert receipt.kind == "refund"
    assert receipt.data["amount"] == -2500
    assert receipt.data["tax"] == -500


def test_ledger_rejects_a_zero_entry_and_records_the_rest():
    with pytest.raises(ValueError):
        post("settlement", 0, actor="alice", order_id="O-1")
    receipt = post("settlement", 2500, actor="alice", order_id="O-1")
    assert receipt.data == {"order_id": "O-1", "amount": 2500}


def test_settle_posts_one_entry_per_order():
    orders = [make_order("O-1"), make_order("O-2", quantity=1)]
    receipts = settle(orders, actor="alice")
    assert [receipt.kind for receipt in receipts] == ["settlement", "settlement"]
    assert [receipt.data["amount"] for receipt in receipts] == [2500, 1250]
    assert [receipt.data["order_id"] for receipt in receipts] == ["O-1", "O-2"]


def test_daily_reports_the_date_count_and_total():
    orders = [make_order("O-1", placed_on="2026-01-03"), make_order("O-2", placed_on="2026-01-02")]
    receipt = daily(orders, actor="alice")
    assert receipt.kind == "daily"
    assert receipt.data == {"date": "2026-01-02", "orders": 2, "total": 5000}


def test_monthly_reports_the_month_and_currencies():
    orders = [
        make_order("O-1", placed_on="2026-01-03"),
        make_order("O-2", quantity=1, currency="USD", placed_on="2026-01-09"),
    ]
    receipt = monthly(orders, actor="alice")
    assert receipt.data["month"] == "2026-01"
    assert receipt.data["currencies"] == ["EUR", "USD"]
    assert receipt.data["total"] == 3750


def test_bus_publishes_to_the_audit_subscriber():
    from events.bus import EventBus

    bus = EventBus()
    bus.subscribe(audit_subscriber)
    results = bus.publish({"channel": "orders", "payload": [1, 2, 3], "actor": "alice"})
    assert len(results) == 1
    receipt = results[0]
    assert receipt.kind == "bus"
    assert receipt.data == {"channel": "orders", "count": 3}
    assert receipt.actor == "alice"


def test_notifier_uses_the_injected_sink():
    order = make_order()
    notifier = Notifier(sink=record)
    receipt = notifier.send(order, actor="alice")
    text = Templates().render(order, "email")
    assert receipt.kind == "notification"
    assert receipt.data == {"order_id": "O-1", "channel": "email", "length": len(text)}


def test_bootstrap_wires_the_bus_and_the_notifier():
    parts = bootstrap()
    assert parts["bus"].subscriber_count == 1
    assert isinstance(parts["notifier"], Notifier)


def test_return_uses_the_frozen_policy_fee():
    receipt = request_return(
        make_order(),
        actor="alice",
        delivered_on="2026-01-01",
        today="2026-01-20",
    )
    assert receipt.kind == "return"
    assert receipt.data == {
        "order_id": "O-1",
        "reason": "changed_mind",
        "fee": 375,
        "refundable": 2125,
    }


def test_reservation_counts_units_and_updates_stock():
    stock = StockLedger({"notebook": 5, "pen": 3})
    receipt = reserve(make_order(), actor="alice", stock=stock)
    assert receipt.data == {"order_id": "O-1", "units": 2, "skus": ["notebook"]}
    assert stock.available("notebook") == 3


def test_statement_applies_the_standard_discount():
    receipt = statement([make_order("O-1"), make_order("O-2", quantity=1)], actor="alice")
    assert receipt.data == {"orders": 2, "gross": 3750, "credits": 375, "net": 3375}


def test_the_return_workflow_is_audited():
    workflow = ReturnWorkflow(open_case("R-1", "O-1", "damaged", 2500))
    approved = workflow.approve(actor="alice")
    assert approved.kind == "return_approved"
    assert approved.data == {
        "return_id": "R-1",
        "order_id": "O-1",
        "state": "approved",
        "amount": 2500,
    }
    assert workflow.receive(actor="alice").data["state"] == "received"
    refunded = workflow.refund(actor="alice")
    assert refunded.kind == "return_refunded"
    assert refunded.data["state"] == "refunded"
    with pytest.raises(InvalidReturnStep):
        workflow.approve(actor="alice")


def test_a_transfer_moves_stock_and_is_audited():
    stock = StockLedger({"notebook": 5, "pen": 1})
    receipt = transfer(
        stock,
        actor="alice",
        from_sku="notebook",
        to_sku="pen",
        units=2,
    )
    assert receipt.kind == "transfer"
    assert receipt.data == {"from": "notebook", "to": "pen", "units": 2}
    assert stock.available("notebook") == 3
    assert stock.available("pen") == 3


def test_search_is_already_migrated_and_unchanged():
    receipt = index("atlas")
    assert receipt.kind == "search"
    assert receipt.data == {"term": "atlas"}
