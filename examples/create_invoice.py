"""Create an invoice with decimal amounts, then handle typed errors.

python examples/create_invoice.py <customer-id> <item-id>
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from decimal import Decimal

from desktopaccountingapi import DesktopAccountingApi, ErrorCode, IntegrationError, OutcomeUnknownError
from desktopaccountingapi.types import InvoiceLineCreateInput


def main(customer_id: str, item_id: str) -> None:
    with DesktopAccountingApi(end_user_id=os.environ["DAAPI_END_USER_ID"]) as client:
        try:
            invoice = client.qbd.invoices.create(
                customer_id=customer_id,
                transaction_date=dt.date.today(),
                memo="Created by the Python SDK example",
                lines=[InvoiceLineCreateInput(item_id=item_id, quantity=2, rate=Decimal("52.75"))],
            )
        except IntegrationError as error:
            if error.code == ErrorCode.QBD_REFERENCE_NOT_FOUND:
                print(f"Unknown customer or item ({error.param}): {error.user_facing_message}")
                return
            raise
        except OutcomeUnknownError as error:
            print(f"QuickBooks may or may not have created the invoice; check request {error.request_id}.")
            return
        print(f"Created invoice {invoice.ref_number} ({invoice.id}), subtotal {invoice.subtotal}")

        # Clear the memo: None sends JSON null; omitted fields stay unchanged.
        updated = client.qbd.invoices.update(invoice.id, revision_number=invoice.revision_number, memo=None)
        print(f"Memo cleared, revision {updated.revision_number}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
