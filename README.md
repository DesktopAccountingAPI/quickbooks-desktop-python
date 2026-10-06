# Desktop Accounting API Python SDK

The official Python client for [Desktop Accounting API](https://www.desktopaccountingapi.com/), the REST API for QuickBooks Desktop. It covers all 275 operations of API version 1.0.0 with typed request and response models, a synchronous and an asyncio client, automatic retries with idempotency keys, cursor pagination, async-mode request handles and webhook verification.

- Documentation: https://www.desktopaccountingapi.com/docs/
- Every method: [api.md](api.md)
- Runnable programs: [examples repository](https://github.com/DesktopAccountingAPI/examples/tree/main/python)

## Requirements

Python 3.9 to 3.14. Dependencies: [httpx](https://www.python-httpx.org/) and `typing_extensions`.

## Install

```sh
pip install desktopaccountingapi-quickbooks-desktop
```

The distribution is `desktopaccountingapi-quickbooks-desktop`; the import package is `desktopaccountingapi`.

### Install from source

```sh
pip install "git+https://github.com/DesktopAccountingAPI/quickbooks-desktop-python.git@v0.1.0"
```

## Quickstart

```python
from desktopaccountingapi import DesktopAccountingApi

client = DesktopAccountingApi()  # reads DAAPI_SECRET_KEY
acme = client.for_end_user("eu_01j9...")

health = acme.qbd.health_check()
print(health.quickbooks.company_name)

for invoice in acme.qbd.invoices.list(limit=50):
    print(invoice.ref_number, invoice.transaction_date, invoice.subtotal)
```

With asyncio:

```python
import asyncio
from desktopaccountingapi import AsyncDesktopAccountingApi


async def main() -> None:
    async with AsyncDesktopAccountingApi(end_user_id="eu_01j9...") as client:
        async for customer in client.qbd.customers.list():
            print(customer.full_name, customer.balance)


asyncio.run(main())
```

Methods mirror the API's operation IDs in snake_case: `qbd.invoices.create` is `client.qbd.invoices.create(...)`, `endUsers.passthrough` is `client.end_users.passthrough(...)`, `qbd.reports.generalSummary` is `client.qbd.reports.general_summary(...)`. Path parameters are positional; everything else is a keyword argument.

## Configuration

| Argument | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `api_key` | `DAAPI_SECRET_KEY` | required | Secret key, `sk_live_...` or `sk_test_...`. Checked locally (format and checksum) before any request; a missing or malformed key raises `DaapiError`. |
| `base_url` | `DAAPI_BASE_URL` | `https://api.desktopaccountingapi.com` | API host. Staging: `https://api-staging.desktopaccountingapi.com`. May include a path prefix. |
| `end_user_id` | | `None` | Default end user for QuickBooks Desktop operations. |
| `timeout` | | `100` | Client-side timeout in seconds for each HTTP attempt, and the time budget for waiting on requests that are still running in QuickBooks. |
| `max_retries` | | `2` | Retries for network errors, `429` and retryable `5xx` responses. |
| `server_timeout` | | API default (90) | Seconds the API waits for QuickBooks before answering (`Daapi-Timeout-Seconds`, 1-300). |
| `http_client` | | new client | Your own `httpx.Client` (`httpx.AsyncClient` for the async client) for proxies, custom transports or connection limits. You close it yourself. |
| `logger` | | `logging.getLogger("desktopaccountingapi")` | Receives request and retry diagnostics at `DEBUG`/`INFO`. It never logs keys, headers or bodies. |

Every method also accepts `timeout` and `max_retries`, QuickBooks Desktop operations accept `end_user_id` and `server_timeout`, and writes accept `idempotency_key`. `client.with_options(...)` returns a copy with other defaults. Close the client with `client.close()` (`await client.close()`) or use it as a context manager.

## End users

QuickBooks Desktop operations act on one end user's company file and send `Daapi-End-User-Id`. Set the end user on the client, scope a client with `for_end_user`, or pass it per call:

```python
acme = client.for_end_user("eu_01j9...")  # shares the connection pool
acme.qbd.invoices.retrieve("7-1700000000")
client.qbd.invoices.retrieve("7-1700000000", end_user_id="eu_01j9...")
```

A QuickBooks Desktop call without an end user raises `DaapiError` before anything is sent. Platform operations (`client.end_users`, `client.auth_sessions`, `client.requests`) never send the header; passthrough takes the end user from its path.

## Models, money and dates

Models live in `desktopaccountingapi.types`. Responses are frozen dataclasses; `to_dict()` turns one back into the API's JSON, and `from_wire()` parses it. Request models are keyword-only classes.

- Money and prices are `decimal.Decimal` and are sent as decimal strings with their scale (`Decimal("5.00")` is sent as `"5.00"`); binary floating point is never used for them. Quantities and percentages are `float`.
- Dates are `datetime.date`. Timestamps are timezone-aware `datetime.datetime` values that keep the offset QuickBooks reported (the PC's local offset).
- Date-range filters such as `updated_after` take a `date`, a `datetime` or an ISO string.
- Enums are `Literal` types. Open enums (most response enums) also accept any `str`, so values added to the API later arrive unchanged.
- Responses tolerate fields added later and fields that are missing.
- Optional request fields default to `NOT_GIVEN` and are not sent. `None` sends JSON `null`, which clears a field where the API allows it:

```python
client.qbd.invoices.update(invoice.id, revision_number=invoice.revision_number, memo=None)
```

## Pagination

Cursor lists return a pager. Iterating it yields every item across pages; the next page is requested as soon as a page arrives, so the QuickBooks iterator stays inside its idle window. A network error while fetching a page retries the same cursor, which returns the same page.

```python
pager = client.qbd.invoices.list(customer_ids=["80000001-1700000000"], limit=100)
for invoice in pager:  # all items
    ...
page = pager.first_page()  # one page
print(page.data, page.next_cursor, page.has_more, page.remaining_count, page.cursor_expires_at)
for page in pager.iter_pages():  # page by page
    ...
everything = pager.list_all()  # all items in a list
```

The async pager works with `async for`, `await pager.first_page()`, `async for page in pager.iter_pages()` and `await pager.list_all()`.

If the iterator expires (idle too long, QuickBooks restarted, session ended), the pager raises `CursorExpiredError` with `items_yielded`, `pages_served`, `last_id`, `last_updated_at` and `reason`. It never restarts the list on its own, because records may have changed. Resume with a watermark and skip what you already have:

```python
from desktopaccountingapi import CursorExpiredError

seen: set[str] = set()
try:
    for customer in client.qbd.customers.list():
        seen.add(customer.id)
except CursorExpiredError as error:
    for customer in client.qbd.customers.list(updated_after=error.last_updated_at):
        if customer.id not in seen:
            seen.add(customer.id)
```

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

`APIError` exposes the error object field by field: `status` (HTTP status), `type`, `code`, `message`, `user_facing_message`, `http_status_code`, `integration_code`, `request_id` (from the body, else the `Daapi-Request-Id` header), `cause`, `fixes` (each with `actor` and `action`), `docs_url`, `retryable`, `outcome`, `param`, `details` and the response `headers`. `ErrorCode` and `ErrorType` hold a constant for every catalog code and type:

```python
from desktopaccountingapi import ErrorCode, IntegrationConnectionError, IntegrationError

try:
    client.qbd.customers.retrieve("80000001-1700000000")
except IntegrationConnectionError as error:
    if error.code == ErrorCode.QBD_MODAL_DIALOG_OPEN:
        show_to_end_user(error.user_facing_message)
except IntegrationError as error:
    print(error.code, error.param, error.request_id, error.docs_url)
    for fix in error.fixes:
        print(fix.actor, fix.action)
```

## Retries and idempotency

Every write (create, update, delete, void, passthrough...) sends an `Idempotency-Key`: yours (`idempotency_key=...`) or a UUID generated once per call and reused on each retry of that call. The API stores the result per key for 7 days, so a retry never applies a write twice.

The SDK retries up to `max_retries` times (default 2) on:

- network errors before a response (connection failure, dropped connection, client timeout), for reads and writes;
- `429`;
- `5xx` responses with `Daapi-Should-Retry: true`.

It never retries when `Daapi-Should-Retry` is `false`, when the error's `outcome` is `unknown` or `pending`, or on a non-JSON error without that header. The delay is 0.5 s doubling to at most 8 s, with jitter; `Retry-After` overrides it.

## Timeouts

- `timeout` (client side, default 100 s) limits each HTTP attempt and is the time budget of a call.
- `server_timeout` (`Daapi-Timeout-Seconds`) is how long the API waits for QuickBooks before answering a sync call (API default 90 s, health check 60 s, maximum 300 s).

If the API answers `504 QBD_REQUEST_TIMEOUT` because the request was sent to QuickBooks but has not finished, the SDK does not send it again. It long-polls `GET /v1/requests/{id}` until the request finishes or the call's `timeout` ends, then returns the typed result, raises the request's typed error, or raises `RequestPendingError` with `request_id` (and the last `request` snapshot).

## Async mode

Operations that run through the QuickBooks queue can be started in async mode (`Prefer: respond-async`). They return at once with a request handle:

```python
handle = client.qbd.invoices.enqueue.create(customer_id="80000001-1700000000", queue_ttl=3600)
print(handle.id, handle.request.status)  # req_..., "queued"
invoice = handle.wait(timeout=600)  # long-polls; returns an Invoice or raises its typed error
handle.status()  # current Request, no waiting
handle.result()  # Invoice if done, typed error if failed, RequestPendingError if running
```

`enqueue` exposes every async-capable operation except cursor lists, with the same parameters plus `queue_ttl` (`Daapi-Queue-Ttl-Seconds`). On the async client, `await handle.wait()`.

## Webhooks

Webhooks follow [Standard Webhooks](https://www.standardwebhooks.com/). Verify the raw request body and headers with the endpoint's signing secret (with or without `whsec_`). No API key is needed:

```python
from desktopaccountingapi import WebhookVerificationError, webhooks

try:
    event = webhooks.verify(raw_body, request.headers, os.environ["DAAPI_WEBHOOK_SECRET"])
except WebhookVerificationError:
    return 400
if event.type == webhooks.EventType.REQUEST_SUCCEEDED:
    request_id = event.data["id"]
```

`verify` checks the `webhook-id`, `webhook-timestamp` and `webhook-signature` headers (case-insensitive, several signatures during secret rotation), rejects timestamps more than 300 seconds from the local clock in either direction (`tolerance=`), compares in constant time and returns a `WebhookEvent` (`id`, `type`, `timestamp`, `project_id`, `data`). `webhooks.verify_signature(...)` checks only the signature, and `client.webhooks.verify(...)` is the same helper on a client. Deduplicate deliveries on `event.id`.

## Raw responses

`with_raw_response` returns a typed `RawResponse[T]` with the HTTP status, headers and request ID next to the parsed result:

```python
raw = client.qbd.customers.with_raw_response.retrieve("80000001-1700000000")
print(raw.status_code, raw.request_id, raw.headers.get("daapi-warnings"))
customer = raw.parse()  # Customer
```

It covers every method except cursor lists.

## Passthrough

Send qbXML as JSON, or raw qbXML, to an end user's QuickBooks:

```python
result = client.end_users.passthrough("eu_01j9...", {"CustomerQueryRq": {"MaxReturned": 5}})
xml = client.end_users.passthrough_xml("eu_01j9...", "<CustomerQueryRq><MaxReturned>5</MaxReturned></CustomerQueryRq>")
```

## Versioning

This package follows semantic versioning. Each release is generated from one version of the API contract; `.daapi-sdk.json` and `desktopaccountingapi.CONTRACT_SHA256` record its digest (`sha256:1cc3058cecb5...` for this release) and `desktopaccountingapi.API_VERSION` the API version.

## Development

The toolchain is pinned in `mise.toml`. `mise run check` installs the pinned dev tools with uv and runs ruff, `mypy --strict`, the unit tests, the cross-language conformance suite (against `conformance/mock-server.mjs`), the example type checks and a build plus clean install of the wheel. Set `UV_PYTHON=3.9` (or any supported version) to run it on another interpreter. See [CONTRIBUTING.md](CONTRIBUTING.md).

QuickBooks is a registered trademark of Intuit Inc. Desktop Accounting API is an independent product and is not affiliated with, endorsed by, or approved by Intuit Inc.
