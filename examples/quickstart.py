"""Quickstart: check the QuickBooks Desktop connection and list the first invoices.

export DAAPI_SECRET_KEY=sk_test_...
export DAAPI_END_USER_ID=eu_...
python examples/quickstart.py
"""

from __future__ import annotations

import os

from desktopaccountingapi import DesktopAccountingApi


def main() -> None:
    client = DesktopAccountingApi(end_user_id=os.environ["DAAPI_END_USER_ID"])
    with client:
        health = client.qbd.health_check()
        print(f"Connected: {health.quickbooks.company_name} ({health.quickbooks.product}), {health.duration} ms")

        page = client.qbd.invoices.list(limit=10).first_page()
        for invoice in page.data:
            customer = invoice.customer.full_name if invoice.customer else "-"
            print(f"{invoice.ref_number}  {invoice.transaction_date}  {customer}  {invoice.subtotal}")
        print(f"{len(page.data)} invoices on the first page, more: {page.has_more}")


if __name__ == "__main__":
    main()
