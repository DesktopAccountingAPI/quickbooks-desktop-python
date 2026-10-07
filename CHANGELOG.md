# Changelog

## 0.2.0

Easier porting from Conductor's `conductor-py`; see "Porting from Conductor" in the README.

- `conductor_end_user_id=` is accepted on every QuickBooks Desktop method as an alias of `end_user_id=`. Both with different values raise `DaapiError`.
- Conductor's error names: `ConductorError` (alias of `DaapiError`), `APIStatusError`, `BadRequestError`, `NotFoundError`, `ConflictError`, `UnprocessableEntityError` and `InternalServerError`. Every error from an HTTP response is also an `APIStatusError` and, for those statuses, the matching status class, so `except NotFoundError:` works; `APIError.status_code` is an alias of `status`.
- Client options `default_headers` and `total_timeout` (also per call), and the `DAAPI_LOG` environment variable. A base URL ending in `/v1` no longer produces `/v1/v1/...`.
- Pagination requests the next page only when the iteration needs it, so a loop that stops early sends no extra QuickBooks query. While iterating items, a page held for more than 2 seconds makes the SDK request the next page in the background; `list_all()` always reads ahead.

## 0.1.0

First release, generated from API version 1.0.0 (contract `sha256:6f5ac28d7c33`, 275 operations).

- Synchronous `DesktopAccountingApi` and asyncio `AsyncDesktopAccountingApi` clients on httpx, for Python 3.9 to 3.14.
- Every QuickBooks Desktop and platform operation as a typed method (`client.qbd.invoices.create(...)`, `client.end_users.passthrough(...)`), with request and response models in `desktopaccountingapi.types`.
- Money as `decimal.Decimal`, dates as `datetime.date`, timestamps as timezone-aware `datetime.datetime` with the offset QuickBooks reported.
- Local API key validation, `for_end_user()`, automatic idempotency keys, retries that follow `Daapi-Should-Retry`, and long-polling of requests that are still running after a sync timeout.
- Cursor pagination with one page of read-ahead and `CursorExpiredError` progress fields.
- Async mode through `enqueue` accessors returning request handles.
- Typed errors with every field of the API's error object, plus `ErrorCode` and `ErrorType` constants.
- Raw responses through `with_raw_response`.
- Standard Webhooks signature verification (`client.webhooks.verify` and `desktopaccountingapi.webhooks`).
