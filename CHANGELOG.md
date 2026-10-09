# Changelog

## Unreleased

## 0.5.4 (2026-10-09)

- New error code `ROUTE_NOT_FOUND` (`ErrorCode.ROUTE_NOT_FOUND`): a `404` for a method and path the API has no endpoint for, such as the API root. These answered `RESOURCE_MISSING`, whose cause describes a missing ID.

## 0.5.3 (2026-10-09)

- Releases publish to PyPI through trusted publishing (GitHub OIDC); no PyPI token is used.

## 0.5.2 (2026-10-09)

- Released in lockstep with the other Desktop Accounting API packages; no entries for this package.

## 0.5.1 (2026-10-09)

- `RawResponse.request_id` and `CursorPage.request_id` after a long-polled call are the ID of the request that produced the result (the 504's `details["requestId"]`), which `client.requests.retrieve()` finds. They were the last poll's ID, which answers `404`. The poll's own ID stays in `raw.headers["daapi-request-id"]` (sync and async clients).
- `QBD_OBJECT_IN_USE` errors (QuickBooks status 3175 or 3176, a record open for editing or locked by another user) carry `details.diagnosis` with the new diagnosis cause `record_in_use`: close the edit window in QuickBooks, then retry. The cause's `details.lockedBy` names the user when QuickBooks does. The request's `diagnosis` has the same cause.

## 0.5.0 (2026-10-09)

- A read that hits the server timeout (`504 QBD_REQUEST_TIMEOUT` with outcome `not_applicable`) is long-polled like a write: the SDK waits on `GET /v1/requests/{id}` until the call's deadline and returns the result or raises `RequestPendingError`, instead of raising the 504 at once (sync and async clients). The rule is the same in every SDK: HTTP 504, code `QBD_REQUEST_TIMEOUT` and a `details.requestId`, whatever the outcome. Such a 504 is never retried.
- `end_users.passthrough_xml()` collects a request that hits the server timeout the same way and returns its qbXML result text, instead of raising the 504 (sync and async clients).
- An error that ends the wait for a timed-out write (for example `OutcomeUnknownError` from a request that ended `outcome_unknown`) carries the write's `idempotency_key`; it was `None`.
- Cursor lists accept `cursor=`, so a list can resume from a stored `next_cursor`, for example across HTTP requests: `client.qbd.invoices.list(cursor=saved_cursor, limit=100).first_page()`. Iteration continues from that page.
- **Breaking (types):** Response prices, rates and percentages (for example `QbdInvoiceLine.rate`, `QbdSalesOrPurchaseDetail.price`, `ratePercent`) carry the same decimal pattern as their inputs and as amounts, so they are `decimal.Decimal` instead of `str`.
- The `now=` option of `webhooks.verify` and `verify_signature` is documented as Unix seconds (like `time.time`).

## 0.4.0 (2026-10-08)

- Warning code `QBD_PERSONAL_DATA_WITHHELD`: employee responses carry one warning per `ssn` that QuickBooks withheld because the integration may not read personal data (`ssn` stays `None`; `Daapi-Warnings` counts the warnings).
- `personal_data_access` on an end user's integration connections: `allowed`, `denied` or `unknown`.
- A failed passthrough's error `details["requests"]` lists the status code, severity and message of every qbXML message, so a message skipped by `stopOnError` is visible.

## 0.3.0 (2026-10-08)

- **Breaking:** `qbd.reports.budget_summary()` now requires the `fiscal_year` keyword argument (sync and async clients). The API always rejected a budget report without it (`400 INVALID_PARAMETER`, `param: "fiscalYear"`), so no working call changes behavior; type checkers flag calls that omit it, and such calls raise `TypeError`. Pass the fiscal year, for example `budget_summary(report_type="profit_and_loss_budget_overview", fiscal_year=2026)`.
- `WebhookEventType.CONNECTION_COMPANY_FILE_REMARKED` (`connection.company_file_remarked`): the marker that identifies a connection's company file was created, written back after the file lost it (for example a restored backup) or adopted from the file; `data["reason"]` is `marker_created`, `marker_restored` or `marker_adopted`.
- After `504 QBD_REQUEST_TIMEOUT`, any failure while waiting for the request (a poll answered `429`, `5xx` or `404`, a network error or a timeout) raises `RequestPendingError` with `request_id`, `timeout_error` (the 504), `poll_error` and `idempotency_key`. It never surfaces the poll's own retryable error, which read as "safe to resend" and could duplicate a write. `RequestHandle.wait()` follows the same rule (sync and async clients).
- Waiting for a pending request stays inside the call's deadline (`total_timeout`, else `timeout`): no poll, SDK retry or backoff starts after it. The async client cuts an in-flight poll off at the deadline; the sync client stops it between network operations (see below).
- `idempotency_key` on every error raised for a write (generated or yours), on raw responses and on request handles.
- A request that succeeded in QuickBooks but whose answer the API could not map (`request.error`, for example `QBD_RESPONSE_UNREADABLE` with outcome `applied`) raises that typed error instead of returning `None`.
- Errors raised by `RequestHandle.wait()` and `result()` (sync and async) carry the handle's `idempotency_key`.
- A poll answer that arrives after the deadline (httpx timeouts are per I/O step) is not returned, even a settled one; the call raises `RequestPendingError` with that snapshot.
- The call's deadline also bounds the response body. The async client runs each attempt under `asyncio.wait_for` (a hard bound, headers and body). The sync client enforces it between network operations, without threads: each attempt's connect, read, write and pool timeouts are the time left, the deadline is checked after headers and between body chunks, and the response is closed when it passes. The sync deadline is checked again after the body and its cleanup (chunked trailers, EOF) before a success is returned. One stalled sync read can overrun by at most its read budget, and trickling header bytes can extend a sync attempt. The SDK never starts its own attempt or retry after the deadline. The sync client cannot bound everything: retries inside an injected transport (for example `httpx.HTTPTransport(retries=3)`), synchronous DNS lookups, and your own auth, hook, transport or cleanup code can run longer; how httpx applies its connect, read, write and pool timeouts depends on the transport; and a transport error that arrives after the deadline surfaces as `APIConnectionError` rather than `APITimeoutError`. Sync requests always go through your `httpx.Client`, so auth flows, event hooks, cookies, proxies and `trust_env` behave the same with a deadline.

## 0.2.1 (2026-10-07)

Generated from API contract sha256 `b5774d24bc81`. Documentation only; no API surface change.

- `updatedAt` and `revisionNumber` descriptions say that QuickBooks changes them at most once per second: an incremental sync should overlap `updatedAfter` and deduplicate by `id` and `revisionNumber`.
- The fixes for status 3261 (`QBD_INSUFFICIENT_PERMISSION`) name the personal-data checkbox in QuickBooks and what to do when it is gray: send the end user a new setup link and choose "Enable payroll access".
- Item sites document what QuickBooks returns without Advanced Inventory: an empty list, and `404 QBD_OBJECT_NOT_FOUND` from retrieve, rather than an error.

## 0.2.0 (2026-10-07)

Easier porting from Conductor's `conductor-py`; see "Porting from Conductor" in the README.

- `conductor_end_user_id=` is accepted on every QuickBooks Desktop method as an alias of `end_user_id=`. Both with different values raise `DaapiError`.
- Conductor's error names: `ConductorError` (alias of `DaapiError`), `APIStatusError`, `BadRequestError`, `NotFoundError`, `ConflictError`, `UnprocessableEntityError` and `InternalServerError`. Every error from an HTTP response is also an `APIStatusError` and, for those statuses, the matching status class, so `except NotFoundError:` works; `APIError.status_code` is an alias of `status`.
- Client options `default_headers` and `total_timeout` (also per call), and the `DAAPI_LOG` environment variable. A base URL ending in `/v1` no longer produces `/v1/v1/...`.
- Pagination requests the next page only when the iteration needs it, so a loop that stops early sends no extra QuickBooks query. While iterating items, a page held for more than 2 seconds makes the SDK request the next page in the background; `list_all()` always reads ahead.

## 0.1.1 (2026-10-06)

Generated from API contract sha256 `1cc3058cecb5`, the same contract as 0.1.0. No API surface change.

- The README is rewritten: install with exact package coordinates, authentication, a quickstart, common workflows, errors, async requests and webhooks, versioning and support. Every code sample in it is compiled against the package before release, and the quickstart runs against a mock server.

## 0.1.0 (2026-10-06)

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
