"""Smoke program for an installed package (no API key, no network).

    python scripts/smoke.py <expected-version>

Run by ``scripts/publish.py`` against the freshly built wheel in a clean virtual environment and,
after a release, against the version installed from PyPI.
"""

from __future__ import annotations

import sys
from decimal import Decimal


def main(expected_version: str) -> None:
    import desktopaccountingapi as daapi
    from desktopaccountingapi import webhooks
    from desktopaccountingapi.types import Invoice, InvoiceLineCreateInput

    assert daapi.__version__ == expected_version, f"version {daapi.__version__} != {expected_version}"
    assert "site-packages" in (daapi.__file__ or ""), f"imported from {daapi.__file__}, not an installed package"

    # A valid-format test key constructs a client; nothing is sent.
    client = daapi.DesktopAccountingApi(api_key="sk_test_Conformance0Key0For0SDK0Tests000010nQFLR", end_user_id="eu_1")
    assert client.qbd.invoices.list is not None
    client.close()
    try:
        daapi.DesktopAccountingApi(api_key="sk_test_Conformance0Key0For0SDK0Tests000010nQFLA")
    except daapi.DaapiError:
        pass
    else:
        raise AssertionError("an invalid key was accepted")

    # Standard Webhooks reference vector.
    webhooks.verify_signature(
        '{"test": 2432232314}',
        {
            "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
            "webhook-timestamp": "1614265330",
            "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        },
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        now=lambda: 1614265330,
    )

    line = InvoiceLineCreateInput(item_id="1", rate=Decimal("5.00"))
    assert line.to_dict() == {"itemId": "1", "rate": "5.00"}
    invoice = Invoice.from_wire({"id": "1", "subtotal": "105.50", "transactionDate": "2026-10-05"})
    assert invoice.subtotal == Decimal("105.50")
    print(f"smoke ok: desktopaccountingapi {daapi.__version__} (API {daapi.API_VERSION}) from {daapi.__file__}")


if __name__ == "__main__":
    main(sys.argv[1])
