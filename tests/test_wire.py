from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from desktopaccountingapi import NOT_GIVEN
from desktopaccountingapi._wire import (
    body,
    format_date_or_datetime,
    format_datetime,
    format_decimal,
    parse_date,
    parse_datetime,
    parse_decimal,
    query_items,
)
from desktopaccountingapi.types import (
    CustomFieldCreateInput,
    Invoice,
    InvoiceLineCreateInput,
    Reference,
)


def test_decimal_preserves_scale() -> None:
    assert format_decimal(Decimal("52.75")) == "52.75"
    assert format_decimal(Decimal("5.00")) == "5.00"
    assert format_decimal(Decimal("5")) == "5"
    assert format_decimal(Decimal("-0.10")) == "-0.10"
    assert format_decimal(Decimal("1E+3")) == "1000"
    with pytest.raises(ValueError):
        format_decimal(Decimal("NaN"))


def test_parse_decimal_never_uses_binary_float() -> None:
    assert parse_decimal("105.50") == Decimal("105.50")
    assert str(parse_decimal("105.50")) == "105.50"
    assert parse_decimal(None) is None
    assert parse_decimal("not a number") == "not a number"


def test_dates() -> None:
    assert parse_date("2026-10-05") == _dt.date(2026, 10, 5)
    assert parse_date(None) is None
    assert parse_date("2026-10") == "2026-10"
    assert format_date_or_datetime(_dt.date(2026, 1, 2)) == "2026-01-02"
    assert format_date_or_datetime("2026-01-02T03:04:05") == "2026-01-02T03:04:05"


def test_timestamps_keep_offset_and_accept_z() -> None:
    local = parse_datetime("2026-10-05T09:14:03-07:00")
    assert local.utcoffset() == _dt.timedelta(hours=-7)
    assert format_datetime(local) == "2026-10-05T09:14:03-07:00"
    utc = parse_datetime("2026-10-05T16:03:59.002Z")
    assert utc.tzinfo is not None and utc.utcoffset() == _dt.timedelta(0)
    assert utc.microsecond == 2000
    assert format_datetime(utc) == "2026-10-05T16:03:59.002Z"
    # Fractions of any length (Python 3.9/3.10 fromisoformat accepts only 3 or 6 digits).
    assert parse_datetime("2026-10-05T16:03:59.1Z").microsecond == 100000
    assert parse_datetime("2026-10-05T16:03:59.123456789+01:00").microsecond == 123456
    assert parse_datetime("garbage") == "garbage"
    naive_seconds = _dt.datetime(2026, 1, 2, 3, 4, 5)
    assert format_datetime(naive_seconds) == "2026-01-02T03:04:05"


def test_query_repeated_keys_and_omissions() -> None:
    items = query_items(
        {
            "customerIds": ["a", "b"],
            "limit": 10,
            "includeLineItems": False,
            "updatedAfter": _dt.date(2026, 1, 1),
            "memo": NOT_GIVEN,
            "waitSeconds": None,
        }
    )
    assert items == [
        ("customerIds", "a"),
        ("customerIds", "b"),
        ("limit", "10"),
        ("includeLineItems", "false"),
        ("updatedAfter", "2026-01-01"),
    ]


def test_body_omits_not_given_and_keeps_null() -> None:
    line = InvoiceLineCreateInput(item_id="80000005-1", quantity=2, rate=Decimal("52.75"))
    sent = body(
        {
            "customerId": "80000001-1",
            "memo": None,
            "transactionDate": _dt.date(2026, 10, 5),
            "dueDate": NOT_GIVEN,
            "lines": [line],
        }
    )
    assert sent == {
        "customerId": "80000001-1",
        "memo": None,
        "transactionDate": "2026-10-05",
        "lines": [{"itemId": "80000005-1", "quantity": 2, "rate": "52.75"}],
    }


def test_input_model_repr_eq_and_nested() -> None:
    a = InvoiceLineCreateInput(item_id="x", custom_fields=[CustomFieldCreateInput(owner_id="0", name="n", value="v")])
    b = InvoiceLineCreateInput(item_id="x", custom_fields=[CustomFieldCreateInput(owner_id="0", name="n", value="v")])
    assert a == b
    assert "item_id='x'" in repr(a)
    assert "description" not in repr(a)
    assert a.to_dict() == {"itemId": "x", "customFields": [{"ownerId": "0", "name": "n", "value": "v"}]}


def test_response_model_round_trip_and_tolerance() -> None:
    wire = {
        "id": "7-1700000000",
        "objectType": "qbd_invoice",
        "createdAt": "2026-10-05T09:14:03-07:00",
        "updatedAt": "2026-10-05T09:17:03-07:00",
        "revisionNumber": "1700000007",
        "customer": {"id": "80000001-1", "fullName": "Acme Supply"},
        "transactionDate": "2026-10-05",
        "subtotal": "105.50",
        "lines": [],
        "aFieldAddedLater": {"x": 1},
    }
    invoice = Invoice.from_wire(wire)
    assert invoice.subtotal == Decimal("105.50")
    assert invoice.transaction_date == _dt.date(2026, 10, 5)
    assert isinstance(invoice.created_at, _dt.datetime)
    assert invoice.customer == Reference(id="80000001-1", full_name="Acme Supply")
    assert invoice.memo is None  # missing fields read as None
    out = invoice.to_dict()
    assert out["subtotal"] == "105.50"
    assert out["createdAt"] == "2026-10-05T09:14:03-07:00"
    assert out["transactionDate"] == "2026-10-05"
    assert out["customer"] == {"id": "80000001-1", "fullName": "Acme Supply"}
    assert "aFieldAddedLater" not in out
    with pytest.raises(AttributeError):
        invoice.memo = "x"  # type: ignore[misc]
