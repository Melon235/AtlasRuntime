"""Schemas the connectors project onto (frozen)."""

from ingest.model import FieldSpec, Schema

ORDER_SCHEMA = Schema(
    "orders",
    (
        FieldSpec("order_id", "text", required=True),
        FieldSpec("amount", "float", required=True),
        FieldSpec("quantity", "int", default=0),
        FieldSpec("placed_on", "date"),
        FieldSpec("note", "text"),
    ),
)

EVENT_SCHEMA = Schema(
    "events",
    (
        FieldSpec("event_id", "text", required=True),
        FieldSpec("kind", "text", required=True),
        FieldSpec("count", "int", default=0),
        FieldSpec("ratio", "float", default=0.0),
        FieldSpec("flagged", "bool", default=False),
        FieldSpec("occurred_on", "date"),
    ),
)
