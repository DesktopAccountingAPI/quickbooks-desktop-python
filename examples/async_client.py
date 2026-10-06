"""The asyncio client: auto-pagination with ``async for`` and an async-mode request handle.

python examples/async_client.py <customer-id>
"""

from __future__ import annotations

import asyncio
import os
import sys

from desktopaccountingapi import AsyncDesktopAccountingApi, CursorExpiredError


async def main(customer_id: str) -> None:
    async with AsyncDesktopAccountingApi(end_user_id=os.environ["DAAPI_END_USER_ID"]) as client:
        count = 0
        try:
            async for customer in client.qbd.customers.list(limit=50):
                count += 1
                if count <= 5:
                    print(customer.full_name, customer.balance)
        except CursorExpiredError as error:
            print(
                f"Cursor expired after {error.items_yielded} customers; resume with updated_after={error.last_updated_at}"
            )
        print(f"{count} customers")

        handle = await client.qbd.invoices.enqueue.create(customer_id=customer_id, memo="Queued from asyncio")
        print(f"Queued request {handle.id}")
        invoice = await handle.wait(timeout=300)
        print(f"Created invoice {invoice.ref_number}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
