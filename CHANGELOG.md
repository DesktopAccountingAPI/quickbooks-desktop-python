# Changelog

## 0.1.0

First release, generated from API version 1.0.0 (contract `sha256:1cc3058cecb5`, 275 operations).

- Synchronous `DesktopAccountingApi` and asyncio `AsyncDesktopAccountingApi` clients on httpx, for Python 3.9 to 3.14.
- Every QuickBooks Desktop and platform operation as a typed method (`client.qbd.invoices.create(...)`, `client.end_users.passthrough(...)`), with request and response models in `desktopaccountingapi.types`.
- Money as `decimal.Decimal`, dates as `datetime.date`, timestamps as timezone-aware `datetime.datetime` with the offset QuickBooks reported.
- Local API key validation, `for_end_user()`, automatic idempotency keys, retries that follow `Daapi-Should-Retry`, and long-polling of requests that are still running after a sync timeout.
- Cursor pagination with one page of read-ahead and `CursorExpiredError` progress fields.
- Async mode through `enqueue` accessors returning request handles.
- Typed errors with every field of the API's error object, plus `ErrorCode` and `ErrorType` constants.
- Raw responses through `with_raw_response`.
- Standard Webhooks signature verification (`client.webhooks.verify` and `desktopaccountingapi.webhooks`).
