# Desktop Accounting API Python SDK

The Python client for [Desktop Accounting API](https://www.desktopaccountingapi.com/), a REST API for QuickBooks Desktop and QuickBooks Enterprise. Your server makes typed calls such as `client.qbd.invoices.create(...)`, and Desktop Accounting API delivers them to your customer's company file through the QuickBooks Web Connector.

- Covers all 275 operations of API 1.0.0: QuickBooks objects and reports (`client.qbd`), end users, auth sessions, request tracking, qbXML passthrough and webhook verification.
- Frozen dataclass models, keyword-only arguments, `Literal` enums, `mypy --strict` clean, a synchronous and an asyncio client.
- Amounts are `decimal.Decimal`, never `float`.
- Every write carries an idempotency key, retries happen only where they cannot duplicate data, and an expired QuickBooks cursor never restarts a list silently.

[Documentation](https://www.desktopaccountingapi.com/docs/) · [API reference](https://www.desktopaccountingapi.com/docs/api/reference/) · [Every SDK method](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/api.md) · [Examples](https://github.com/DesktopAccountingAPI/examples/tree/main/python) · [Changelog](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/CHANGELOG.md) · [Status](https://status.desktopaccountingapi.com)

## Install

```sh
pip install desktopaccountingapi-quickbooks-desktop
```

The current version is **0.5.3**. To pin it exactly:

```sh
pip install "desktopaccountingapi-quickbooks-desktop==0.5.3"
uv add "desktopaccountingapi-quickbooks-desktop==0.5.3"
poetry add "desktopaccountingapi-quickbooks-desktop==0.5.3"
```

The distribution is `desktopaccountingapi-quickbooks-desktop`; the import package is `desktopaccountingapi`.

## Requirements

- Python 3.9 to 3.14.
- Dependencies: [httpx](https://www.python-httpx.org/) and `typing_extensions`.
- A server-side runtime. Secret keys must never reach a browser or a desktop app you distribute (see [Authentication](#authentication)).

## Authentication

1. Sign in to the [dashboard](https://www.desktopaccountingapi.com/dashboard) and open **API keys**.
2. Create a secret key. Test projects issue `sk_test_...` keys; production projects issue `sk_live_...` keys. Choose **Read-only** for reporting jobs and AI agents that must never change data. The full key is shown once.
3. Put it in the `DAAPI_SECRET_KEY` environment variable of your server:

```sh
export DAAPI_SECRET_KEY="sk_test_..."
```

`DesktopAccountingApi()` reads `DAAPI_SECRET_KEY` (and `DAAPI_BASE_URL`, if set). You can also pass `api_key=...`. The SDK checks the key's format and checksum locally, so a mistyped key fails before any network call.

A secret key can read and write every connected company file in its project. Keep it on your server, in a secret manager or environment variable. Never ship it to a browser, never commit it. The API refuses browser requests from other origins on purpose. If a key leaks, revoke it in the dashboard and create a new one. See [Authentication and API keys](https://www.desktopaccountingapi.com/docs/get-started/authentication/).

## Quickstart

Each of your customers is an **end user** (`eu_...`) with one QuickBooks Desktop company file, connected through the Web Connector. Copy an end user ID from the dashboard's **End users** page and set it as `DAAPI_END_USER_ID` (`export DAAPI_END_USER_ID="eu_..."`), then:

```python run=quickstart harness=none
import os

from desktopaccountingapi import DesktopAccountingApi

# Reads DAAPI_SECRET_KEY. for_end_user sends Daapi-End-User-Id on every QuickBooks call.
client = DesktopAccountingApi().for_end_user(os.environ["DAAPI_END_USER_ID"])

health = client.qbd.health_check()
print(f"QuickBooks connection: {health.status}")

# The loop fetches further pages as needed (10 invoices per request); stop after the first 10.
for shown, invoice in enumerate(client.qbd.invoices.list(limit=10), start=1):
    print(invoice.ref_number, invoice.subtotal)  # subtotal is a Decimal, for example Decimal("105.50")
    if shown == 10:
        break
```

With asyncio:

```python harness=none
import asyncio

from desktopaccountingapi import AsyncDesktopAccountingApi


async def main() -> None:
    async with AsyncDesktopAccountingApi(end_user_id="eu_01j9x4m6v4c8k2t7q0r5s3w1zb") as client:
        async for customer in client.qbd.customers.list():
            print(customer.full_name, customer.balance)


asyncio.run(main())
```

Methods mirror the API's operation IDs in snake_case: `qbd.invoices.create` is `client.qbd.invoices.create(...)`, `endUsers.passthrough` is `client.end_users.passthrough(...)`, `qbd.reports.generalSummary` is `client.qbd.reports.general_summary(...)`. Path parameters are positional; everything else is a keyword argument.

## End users

QuickBooks Desktop operations (`client.qbd.*`) act on one end user's company file and send the `Daapi-End-User-Id` header. Set the end user on the client, scope a client with `for_end_user`, or pass it per call:

```python
acme = client.for_end_user("eu_01j9x4m6v4c8k2t7q0r5s3w1zb")  # shares the connection pool
acme.qbd.invoices.retrieve("7-1700000000")
client.qbd.invoices.retrieve("7-1700000000", end_user_id="eu_01j9x4m6v4c8k2t7q0r5s3w1zb")
```

A QuickBooks call without an end user raises `DaapiError` before anything is sent. Platform operations (`client.end_users`, `client.auth_sessions`, `client.requests`) never send the header; passthrough takes the end user from its path. Create end users and their setup links with `client.end_users.create` and `client.auth_sessions.create`; see [End users](https://www.desktopaccountingapi.com/docs/connect/end-users/).

## Common workflows

### List records with auto-pagination

Iterating a list walks every page. The next page is requested only when the loop needs it, so a loop that stops early never runs an extra QuickBooks query. If you hold a page for more than 2 seconds, the SDK requests the next one in the background, so slow loop bodies stay inside the QuickBooks cursor's idle window.

```python
import datetime

for customer in client.qbd.customers.list(limit=100, updated_after=datetime.date(2026, 1, 1)):
    print(customer.id, customer.full_name, customer.balance)

page = client.qbd.customers.list(limit=100).first_page()  # only the first page
print(len(page.data), page.has_more, page.next_cursor)
```

More options, and what to do when a cursor expires, are in [Pagination](#pagination).

### Create a record with an idempotency key

Every write sends an `Idempotency-Key`. Pass your own, derived from your data, so a retry after a crash or timeout returns the first result instead of creating a duplicate:

```python
import datetime
from decimal import Decimal

from desktopaccountingapi.types import InvoiceLineCreateInput

invoice = client.qbd.invoices.create(
    customer_id="80000001-1700000000",
    transaction_date=datetime.date(2026, 10, 5),
    ref_number="WEB-8812",
    lines=[InvoiceLineCreateInput(item_id="80000005-1700000000", quantity=2, rate=Decimal("52.75"))],
    idempotency_key="order-8812-invoice",
)
print(invoice.id, invoice.ref_number, invoice.subtotal)  # subtotal Decimal("105.50")
```

### Update a record with its revision number

QuickBooks rejects an update unless it carries the object's current `revision_number`, so concurrent edits are never overwritten. Read the object, then send its `revision_number` with only the fields you change:

```python
from desktopaccountingapi import ErrorCode, IntegrationError

current = client.qbd.invoices.retrieve("7-1700000000")
try:
    updated = client.qbd.invoices.update(current.id, revision_number=current.revision_number, memo="Paid by card")
    print(updated.revision_number)  # the new revision
except IntegrationError as error:
    if error.code != ErrorCode.QBD_REVISION_NUMBER_STALE:
        raise
    # Someone changed the invoice after you read it. Retrieve it again, reapply your change,
    # and update with the new revision_number.
```

A stale revision is a `409` `INTEGRATION_ERROR` with code `QBD_REVISION_NUMBER_STALE`. Nothing was changed (`outcome: "not_applied"`):

```json
{
  "error": {
    "type": "INTEGRATION_ERROR",
    "code": "QBD_REVISION_NUMBER_STALE",
    "message": "The object changed since you read it; revisionNumber is out of date.",
    "userFacingMessage": "This record changed in QuickBooks Desktop after it was loaded. Reload it and try again.",
    "httpStatusCode": 409,
    "integrationCode": "3200",
    "requestId": "req_01j9x4m6v4c8k2t7q0r5s3w1zd",
    "cause": "QuickBooks rejects updates that do not carry the current revision number, so concurrent edits are not lost.",
    "fixes": [{ "actor": "developer", "action": "Retrieve the object, merge your change, and update with the new revisionNumber." }],
    "docsUrl": "https://www.desktopaccountingapi.com/docs/errors/#qbd_revision_number_stale",
    "retryable": false,
    "outcome": "not_applied",
    "param": null,
    "details": {}
  }
}
```

### Handle errors

Errors are typed by the API's error `type`, and every API error carries the request ID, a message you can show your end user, the cause, concrete fixes and a link to its documentation:

```python
from desktopaccountingapi import APIError, IntegrationConnectionError

try:
    client.qbd.customers.retrieve("80000099-1700000000")
except IntegrationConnectionError as error:
    # QuickBooks is closed, a dialog is open, or the Web Connector is not running: the end user has to act.
    show_to_end_user(error.user_facing_message or error.message)
except APIError as error:
    print(error.status, error.code, error.message, error.request_id)
    print(error.cause, error.docs_url)
    for fix in error.fixes:
        print(f"{fix.actor}: {fix.action}")
```

Every class and field is listed in [Errors](#errors). The [error catalog](https://www.desktopaccountingapi.com/docs/errors/) documents every code.

### Run a request asynchronously and get a webhook

QuickBooks only processes requests while the end user's Web Connector is running. `enqueue` queues a request and returns at once with a handle; the API also sends a `request.succeeded` or `request.failed` webhook when it finishes:

```python
handle = client.qbd.invoices.enqueue.create(
    customer_id="80000001-1700000000",
    queue_ttl=3600,
    idempotency_key="order-8813-invoice",
)
print(handle.id)  # req_...; the request is queued
invoice = handle.wait(timeout=120)  # the typed Invoice, or the typed error
print(invoice.ref_number)
```

Verify each webhook delivery with the endpoint's signing secret before you trust it. Pass the raw body, not parsed JSON:

```python harness=none
import os
from collections.abc import Mapping

from desktopaccountingapi import WebhookVerificationError, webhooks


def handle_webhook(raw_body: bytes, headers: Mapping[str, str]) -> int:
    """Verifies one delivery and returns the HTTP status to answer with."""
    try:
        event = webhooks.verify(raw_body, headers, os.environ["DAAPI_WEBHOOK_SECRET"])
    except WebhookVerificationError:
        return 400
    if event.type == webhooks.EventType.REQUEST_SUCCEEDED:
        print("request", event.data["id"], "succeeded")
    return 204
```

Call `handle_webhook` from your web framework's route with the raw request body and headers, and answer with the status it returns.

Create webhook endpoints and copy their `whsec_...` signing secrets in the dashboard under **Webhooks**. Details: [Async mode](#async-mode), [Webhooks](#webhooks), and the [webhooks guide](https://www.desktopaccountingapi.com/docs/guides/webhooks/).

### Set timeouts and retries

```python
patient = DesktopAccountingApi(timeout=30, max_retries=4, server_timeout=25)
patient.qbd.invoices.retrieve("7-1700000000", end_user_id="eu_01j9x4m6v4c8k2t7q0r5s3w1zb", max_retries=0)
```

`timeout` is the client's limit per HTTP attempt in seconds; `total_timeout` caps a whole call including retries; `server_timeout` is how long the API waits for QuickBooks. Reads and writes retry only when it is safe; see [Retries and idempotency](#retries-and-idempotency) and [Timeouts](#timeouts).

## Configuration

| Argument | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `api_key` | `DAAPI_SECRET_KEY` | required | Secret key, `sk_live_...` or `sk_test_...`. Checked locally (format and checksum) before any request; a missing or malformed key raises `DaapiError`. |
| `base_url` | `DAAPI_BASE_URL` | `https://api.desktopaccountingapi.com` | API host. May include a path prefix. A trailing `/v1` is removed, so `https://api.desktopaccountingapi.com/v1` works too. |
| `end_user_id` | | `None` | Default end user for QuickBooks Desktop operations. |
| `timeout` | | `100` | Client-side timeout in seconds for each HTTP attempt; each retry gets a fresh one. Without `total_timeout`, also the time budget for waiting on requests that are still running in QuickBooks. |
| `total_timeout` | | `None` | Time budget in seconds for a whole call: attempts, retry backoff and the wait for a pending request. |
| `max_retries` | | `2` | Retries for network errors, `429` and retryable `5xx` responses. |
| `server_timeout` | | API default (90) | Seconds the API waits for QuickBooks before answering (`Daapi-Timeout-Seconds`, 1-300). |
| `default_headers` | | none | Headers sent with every request. The headers the SDK manages (`Authorization`, `Accept`, `Content-Type`, `User-Agent`, `Daapi-End-User-Id`, `Idempotency-Key`, `Daapi-Timeout-Seconds`, `Prefer`) are ignored here. |
| `http_client` | | new client | Your own `httpx.Client` (`httpx.AsyncClient` for the async client) for proxies, custom transports or connection limits. You close it yourself. |
| `logger` | `DAAPI_LOG` | `logging.getLogger("desktopaccountingapi")` | Receives request and retry diagnostics at `DEBUG`/`INFO`. It never logs keys, headers or bodies. `DAAPI_LOG=debug`, `info`, `warn`, `error` or `off` sets that logger's level and adds a stderr handler if it has none. |

Every method also accepts `timeout`, `total_timeout` and `max_retries`, QuickBooks Desktop operations accept `end_user_id` (or Conductor's `conductor_end_user_id`) and `server_timeout`, and writes accept `idempotency_key`. `client.with_options(...)` returns a copy with other defaults. Close the client with `client.close()` (`await client.close()`) or use it as a context manager.

## Models, money and dates

Models live in `desktopaccountingapi.types`. Responses are frozen dataclasses; `to_dict()` turns one back into the API's JSON, and `from_wire()` parses it. Request models are keyword-only classes.

- Money and prices are `decimal.Decimal` and are sent as decimal strings with their scale (`Decimal("5.00")` is sent as `"5.00"`); binary floating point is never used for them. Quantities and percentages are `float`.
- Dates are `datetime.date`. Timestamps are timezone-aware `datetime.datetime` values that keep the offset QuickBooks reported (the PC's local offset).
- Date-range filters such as `updated_after` take a `date`, a `datetime` or an ISO string.
- Enums are `Literal` types. Open enums (most response enums) also accept any `str`, so values added to the API later arrive unchanged.
- Responses tolerate fields added later and fields that are missing.
- Optional request fields default to `NOT_GIVEN` and are not sent. `None` sends JSON `null`, which clears a field where the API allows it:

```python
invoice = client.qbd.invoices.retrieve("7-1700000000")
client.qbd.invoices.update(invoice.id, revision_number=invoice.revision_number, memo=None)
```

## Pagination

Cursor lists return a pager. Iterating it yields every item across pages. The next page is requested only when the iteration reaches it, so `break`ing out of a loop never sends an extra QuickBooks query. While you iterate items, a page held for more than 2 seconds makes the SDK request the next page in the background, which keeps slow loops inside the cursor's idle window. `iter_pages()` requests each page when you ask for it; `list_all()` always requests the next page as soon as a page arrives. A network error while fetching a page retries the same cursor, which returns the same page.

```python
pager = client.qbd.invoices.list(customer_ids=["80000001-1700000000"], limit=100)
for invoice in pager:  # all items
    save(invoice)
page = pager.first_page()  # one page
print(page.data, page.next_cursor, page.has_more, page.remaining_count, page.cursor_expires_at)
for each_page in pager.iter_pages():  # page by page
    print(len(each_page.data))
everything = pager.list_all()  # all items in a list
```

To resume from a page you stored earlier, for example across HTTP requests, pass its `next_cursor` as `cursor`: `client.qbd.invoices.list(cursor=saved_cursor, limit=100).first_page()` returns that page, and iterating continues from it. Filters live in the cursor, so pass only the cursor and, if you like, the limit. A QuickBooks cursor expires when it sits idle, so resume soon after you store it.

The async pager works with `async for`, `await pager.first_page()`, `async for page in pager.iter_pages()` and `await pager.list_all()`.

If the iterator expires (idle too long, QuickBooks restarted, session ended), the pager raises `CursorExpiredError` with `items_yielded`, `pages_served`, `last_id`, `last_updated_at` and `reason`. It never restarts the list on its own, because records may have changed. Restart the same query and skip what you already have. Do not resume from the last record's `updatedAt`: QuickBooks returns records in its own order, not by `updatedAt`, so records you have not read yet can be older than the last one you read. An incremental sync restarts from the `updatedAfter` watermark it saved before the traversal ([pagination guide](https://www.desktopaccountingapi.com/docs/guides/pagination/#recovering-from-cursor_expired)).

```python
from desktopaccountingapi import CursorExpiredError

seen: set[str] = set()
try:
    for customer in client.qbd.customers.list():
        seen.add(customer.id)
except CursorExpiredError as error:
    print(error.items_yielded, error.pages_served, error.last_id, error.reason, error.request_id)
    # Restart the same query and skip the IDs you already have.
    for customer in client.qbd.customers.list():
        if customer.id not in seen:
            seen.add(customer.id)
```

Lists without cursor pagination (accounts, classes, terms and other small lists) return the whole list. The [pagination guide](https://www.desktopaccountingapi.com/docs/guides/pagination/) explains cursor lifetimes.

## Errors

Every error is a `DaapiError`. Problems found before sending (missing key, malformed key, missing end user) raise `DaapiError` itself. Error responses raise `APIError` or the subclass for their `type`:

| `type` | Exception |
| --- | --- |
| `INVALID_REQUEST_ERROR` | `InvalidRequestError` (and `CursorExpiredError` for `CURSOR_EXPIRED`) |
| `AUTHENTICATION_ERROR` | `AuthenticationError` |
| `PERMISSION_ERROR` | `PermissionDeniedError` |
| `BILLING_ERROR` | `BillingError` |
| `RATE_LIMIT_ERROR` | `RateLimitError` |
| `INTEGRATION_CONNECTION_ERROR` | `IntegrationConnectionError` |
| `INTEGRATION_ERROR` | `IntegrationError` |
| `OUTCOME_UNKNOWN_ERROR` | `OutcomeUnknownError` |
| `INTERNAL_ERROR` | `InternalError` |
| any other | `APIError` |

`APIConnectionError` (with `APITimeoutError`) means no response arrived after all retries. `RequestPendingError` means a request is still running in QuickBooks when the time budget ended. `WebhookVerificationError` comes from webhook verification. `ApiError`, `ApiConnectionError` and `ApiTimeoutError` are aliases.

Conductor's names work too (see [Porting from Conductor](#porting-from-conductor)). `ConductorError` is `DaapiError`. Every error from an HTTP response is also an `APIStatusError`, and for its status a `BadRequestError` (400), `NotFoundError` (404), `ConflictError` (409), `UnprocessableEntityError` (422) or `InternalServerError` (5xx): a 404 `INVALID_REQUEST_ERROR` is caught by both `except InvalidRequestError` and `except NotFoundError`. `status_code` is an alias of `status`.

`APIError` exposes the error object field by field: `status` (HTTP status), `type`, `code`, `message`, `user_facing_message`, `http_status_code`, `integration_code`, `request_id` (from the body, else the `Daapi-Request-Id` header), `cause`, `fixes` (each with `actor` and `action`), `docs_url`, `retryable`, `outcome`, `param`, `details` and the response `headers`. `ErrorCode` and `ErrorType` hold a constant for every catalog code and type:

```python
from desktopaccountingapi import ErrorCode, IntegrationConnectionError, IntegrationError

try:
    client.qbd.customers.retrieve("80000001-1700000000")
except IntegrationConnectionError as error:
    if error.code == ErrorCode.QBD_MODAL_DIALOG_OPEN:
        show_to_end_user(error.user_facing_message or error.message)
except IntegrationError as error:
    print(error.code, error.param, error.request_id, error.docs_url)
    for fix in error.fixes:
        print(fix.actor, fix.action)
```

Include the `request_id` when you contact support. See the [error handling guide](https://www.desktopaccountingapi.com/docs/guides/error-handling/).

## Retries and idempotency

Every write (create, update, delete, void, passthrough...) sends an `Idempotency-Key`: yours (`idempotency_key=...`) or a UUID generated once per call and reused on each retry of that call. The API stores the result per key for 7 days, so a retry never applies a write twice.

The SDK retries up to `max_retries` times (default 2) on:

- network errors before a response (connection failure, dropped connection, client timeout), for reads and writes;
- `429`;
- `5xx` responses with `Daapi-Should-Retry: true`.

It never retries when `Daapi-Should-Retry` is `false`, when the error's `outcome` is `unknown` or `pending`, or on a non-JSON error without that header. The delay is 0.5 s doubling to at most 8 s, with jitter; `Retry-After` overrides it. See the [idempotency guide](https://www.desktopaccountingapi.com/docs/guides/idempotency/).

## Timeouts

- `timeout` (client side, default 100 s) limits each HTTP attempt. A retry starts a new attempt with a fresh timeout, so with retries a call can take longer than `timeout`.
- `total_timeout` (client side, no default) limits the whole call: every attempt, the waits between retries, and the wait for a pending request. No SDK attempt or retry starts after it ends, and an attempt that runs past it fails with `APITimeoutError`. The async client enforces it as a hard bound. The sync client enforces it between network operations: each attempt's connect, read, write and pool timeouts are the time left, and the deadline is checked once headers arrive, between body chunks and after the body and its cleanup finish. One stalled read can therefore overrun by at most its read budget, and a server that keeps trickling header bytes can extend an attempt. The SDK never starts its own attempt or retry after the deadline. The sync client cannot bound everything: retries inside an injected transport (for example `httpx.HTTPTransport(retries=3)`), synchronous DNS lookups, and your own auth, hook, transport or cleanup code can run longer; how httpx applies its connect, read, write and pool timeouts depends on the transport; and a transport error that arrives after the deadline surfaces as `APIConnectionError` rather than `APITimeoutError`. If you need a hard bound, use the async client.
- `server_timeout` (`Daapi-Timeout-Seconds`) is how long the API waits for QuickBooks before answering a sync call (API default 90 s, health check 60 s, maximum 300 s).

If the API answers `504 QBD_REQUEST_TIMEOUT` because the request was sent to QuickBooks but has not finished, the SDK does not send it again. It long-polls `GET /v1/requests/{id}` until the request finishes or the call's deadline (`total_timeout`, else `timeout`) ends, then returns the typed result, raises the request's typed error, or raises `RequestPendingError` with `request_id` (and the last `request` snapshot). It also raises `RequestPendingError`, never the poll's own error, when a poll fails (`429`, `5xx`, `404`, network): that error says nothing about the write. `error.timeout_error` is the original 504 (with `details["diagnosis"]`) and `error.idempotency_key` the key the write was sent with; resend only with that key. Check the request later:

```python
from desktopaccountingapi import RequestPendingError

try:
    client.qbd.invoices.create(customer_id="80000001-1700000000", idempotency_key="order-8814-invoice")
except RequestPendingError as error:
    request = client.requests.retrieve(error.request_id)
    print(request.status)  # still "queued" or "running"; a webhook reports the result
```

## Async mode

Operations that run through the QuickBooks queue can be started in async mode (`Prefer: respond-async`). They return at once with a request handle:

```python
handle = client.qbd.invoices.enqueue.create(customer_id="80000001-1700000000", queue_ttl=3600)
print(handle.id)  # req_...; the request is queued
invoice = handle.wait(timeout=600)  # long-polls; returns an Invoice or raises its typed error
current = handle.status()  # current Request, no waiting
result = handle.result()  # Invoice if done, typed error if failed, RequestPendingError if running
print(invoice.id, current.status, result.id)
```

`enqueue` exposes every async-capable operation except cursor lists, with the same parameters plus `queue_ttl` (`Daapi-Queue-Ttl-Seconds`). On the async client, `await handle.wait()`. See the [request lifecycle guide](https://www.desktopaccountingapi.com/docs/guides/request-lifecycle/).

## Webhooks

Webhooks follow [Standard Webhooks](https://www.standardwebhooks.com/). `webhooks.verify(raw_body, headers, secret)` checks the `webhook-id`, `webhook-timestamp` and `webhook-signature` headers (case-insensitive, several signatures during secret rotation), rejects timestamps more than 300 seconds from the local clock in either direction (`tolerance=`; tests can pass `now=`, a function returning the current Unix time in seconds like `time.time`), compares in constant time and returns a `WebhookEvent` (`id`, `type`, `timestamp`, `project_id`, `data`). The secret is accepted with or without `whsec_`, and no API key is needed. `webhooks.verify_signature(...)` checks only the signature, and `client.webhooks.verify(...)` is the same helper on a client. Delivery is at least once: deduplicate on `event.id`.

## Raw responses

`with_raw_response` returns a typed `RawResponse[T]` with the HTTP status, headers and request ID next to the parsed result:

```python
raw = client.qbd.customers.with_raw_response.retrieve("80000001-1700000000")
print(raw.status_code, raw.request_id, raw.headers.get("daapi-warnings"))
customer = raw.parse()  # Customer
print(customer.full_name)
```

`raw.request_id` is the ID of the request that produced the result. After the SDK long-polled a request that timed out on the server (`504 QBD_REQUEST_TIMEOUT`), it is that request's ID, which `client.requests.retrieve(raw.request_id)` finds; the final poll's own ID stays in the `raw.headers["daapi-request-id"]` header.

It covers every method except cursor lists.

## Passthrough

Send qbXML as JSON, or raw qbXML, to an end user's QuickBooks:

```python
result = client.end_users.passthrough("eu_01j9x4m6v4c8k2t7q0r5s3w1zb", {"CustomerQueryRq": {"MaxReturned": 5}})
xml = client.end_users.passthrough_xml(
    "eu_01j9x4m6v4c8k2t7q0r5s3w1zb",
    '<QBXMLMsgsRq onError="stopOnError"><CustomerQueryRq><MaxReturned>5</MaxReturned></CustomerQueryRq></QBXMLMsgsRq>',
)
print(result, xml)
```

Passthrough always sends an idempotency key, because a message other than a query is a write.

## Porting from Conductor

Code written for `conductor-py` runs on this SDK with two edits: the import and the API key. The resource tree, method names, keyword arguments and response fields are the same, and Conductor's `conductor_end_user_id=` and error class names are accepted.

```python harness=none
# Before (conductor-py):
#   import conductor
#   from conductor import Conductor
#   client = Conductor(api_key=os.environ["CONDUCTOR_SECRET_KEY"])
import os

import desktopaccountingapi as conductor
from desktopaccountingapi import DesktopAccountingApi as Conductor

client = Conductor(api_key=os.environ["DAAPI_SECRET_KEY"])

# Everything below is unchanged Conductor code.
end_user_id = "eu_01j9x4m6v4c8k2t7q0r5s3w1zb"
client.qbd.health_check(conductor_end_user_id=end_user_id)
for invoice in client.qbd.invoices.list(conductor_end_user_id=end_user_id, limit=50):
    print(invoice.ref_number, invoice.subtotal)
customer = client.qbd.customers.create(conductor_end_user_id=end_user_id, name="Acme Supply")

try:
    client.qbd.invoices.retrieve("7-1700000000", conductor_end_user_id=end_user_id)
except conductor.NotFoundError:
    print("No such invoice")
except conductor.RateLimitError:
    print("A 429 status code was received; back off a bit.")
except conductor.APIStatusError as e:
    print(e.status_code, getattr(e, "code", None), getattr(e, "user_facing_message", str(e)))
    # Our richer fields are on the error too.
    print(e.type, e.integration_code, e.request_id, e.cause, e.fixes, e.docs_url, e.outcome, e.retryable)
except conductor.APIConnectionError:
    print("The server could not be reached")
print(customer.id)
```

The async client is `AsyncDesktopAccountingApi` (Conductor's `AsyncConductor`). Client options keep their Conductor names where they exist (`api_key`, `base_url`, `timeout`, `max_retries`, `default_headers`, `http_client`):

```python harness=none
import os

import httpx

from desktopaccountingapi import DesktopAccountingApi as Conductor

client = Conductor(
    api_key=os.environ["DAAPI_SECRET_KEY"],
    base_url="https://api.desktopaccountingapi.com/v1",  # a trailing /v1 is fine
    timeout=120.0,  # per attempt, as in Conductor
    max_retries=2,
    default_headers={"X-Trace-Id": "billing-sync"},
    http_client=httpx.Client(),
)
print(client.base_url)
```

What to change by hand:

- **API key and base URL.** `DAAPI_SECRET_KEY` (`sk_test_...`, `sk_live_...`) instead of `CONDUCTOR_SECRET_KEY`; `CONDUCTOR_BASE_URL` is not read.
- **End-user IDs.** Create end users here (`eu_...`); Conductor's `end_usr_...` IDs do not exist in this API.
- **Pages.** `list()` returns a pager: iterate it, or use `first_page()`, `iter_pages()` and `list_all()`. Pages are plain data (`data`, `next_cursor`, `has_more`); replace `page.has_next_page()` / `page.get_next_page()` loops with `iter_pages()`.
- **Timeouts.** `timeout` is a number of seconds per attempt; an `httpx.Timeout` object is not accepted. Use `total_timeout` to cap a whole call.
- **Errors.** `APIConnectionError` is not a subclass of `APIError` here; catch `ConductorError` (`DaapiError`) to cover both. Errors have no `response` or `request` attribute; `headers`, `body` and `status_code` are there.
- **Per-call extras.** `extra_headers`, `extra_query` and `extra_body` are not supported; use `default_headers` on the client.
- **Retries.** Writes always carry an `Idempotency-Key`, and only safe failures are retried (see [Retries and idempotency](#retries-and-idempotency)); Conductor's SDK retried 408 and 409 too.

The [migration guide](https://www.desktopaccountingapi.com/docs/get-started/migrating-from-conductor/) covers the API-level differences.

## Versioning and changelog

- This package follows [semantic versioning](https://semver.org/). Before 1.0, a minor version may contain breaking changes; they are marked Breaking in the [CHANGELOG](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/CHANGELOG.md).
- The Python, Node.js, .NET and Java SDKs and the [MCP server](https://github.com/DesktopAccountingAPI/quickbooks-desktop-mcp) are released together with the same version number, generated from the same API contract.
- Every release is listed in [CHANGELOG.md](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/CHANGELOG.md) and tagged `v<version>` on GitHub.
- The API is versioned in its path (`/v1`). Within `v1` the API only adds operations, fields, enum values and error codes, and the SDK tolerates all of them, so older SDK versions keep working.
- Each release is generated from one version of the API contract; `.daapi-sdk.json` and `desktopaccountingapi.CONTRACT_SHA256` record its digest (`sha256:09aa9517f466...` for this release), `desktopaccountingapi.API_VERSION` the API version and `desktopaccountingapi.__version__` the package version.

## Support

- [Documentation](https://www.desktopaccountingapi.com/docs/), the [API reference](https://www.desktopaccountingapi.com/docs/api/reference/) and the [error catalog](https://www.desktopaccountingapi.com/docs/errors/).
- [Status page](https://status.desktopaccountingapi.com) for API and connection incidents.
- SDK bugs and feature requests: [GitHub issues](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/issues).
- Questions about your account, keys, billing or a specific end user's connection: [contact us](https://www.desktopaccountingapi.com/contact). Include the `request_id` of a failing call, never your secret key.
- Security reports: use **Report a vulnerability** on the repository's Security tab.

## Development

The toolchain is pinned in `mise.toml`. `mise run check` installs the pinned dev tools with uv and runs ruff, `mypy --strict`, the unit tests, the cross-language conformance suite (against `conformance/mock-server.mjs`), the example type checks, the README samples (`mypy --strict` on every Python block of this file, and the quickstart against the mock server) and a build plus clean install of the wheel. Set `UV_PYTHON=3.9` (or any supported version) to run it on another interpreter.

To install from source: `pip install "git+https://github.com/DesktopAccountingAPI/quickbooks-desktop-python.git@v0.5.3"`. The code under `src/desktopaccountingapi/types` and `src/desktopaccountingapi/resources`, `api.md`, `conformance/fixtures` and this README are generated; see [CONTRIBUTING.md](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/CONTRIBUTING.md).

## License

MIT. See [LICENSE](https://github.com/DesktopAccountingAPI/quickbooks-desktop-python/blob/main/LICENSE).

QuickBooks is a registered trademark of Intuit Inc. Desktop Accounting API is an independent product and is not affiliated with, endorsed by, or approved by Intuit Inc.
