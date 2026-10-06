# Conformance suite

The four Desktop Accounting API SDKs (Node.js, Python, .NET, Java) run the same conformance suite. The fixtures in `fixtures/` are generated from the API contract together with the SDK code. Do not edit them by hand.

| File | What it checks |
| --- | --- |
| `fixtures/scenarios.json` | HTTP behaviour: pagination, retries, idempotency keys, typed errors, timeouts, async mode, raw responses. Replayed over real HTTP by `mock-server.mjs`. |
| `fixtures/webhooks.json` | Standard Webhooks signature vectors for the webhook verification helper. |
| `fixtures/api-keys.json` | Local secret-key format and checksum validation. |

`mock-server.mjs` needs Node.js 18 or later and has no dependencies. Each SDK's test command starts it, runs every scenario through the SDK's real HTTP transport and stops it.

## Mock server

```
node conformance/mock-server.mjs [--fixtures conformance/fixtures/scenarios.json] [--port 0] [--exit-on-stdin-close]
```

- The first stdout line is `MOCK_SERVER_URL=http://127.0.0.1:<port>`.
- The base URL for scenario `<name>` is `<url>/s/<name>`. The SDK appends `/v1/...` to it, so SDKs must accept a base URL that has a path.
- `POST <url>/_control/reset/<name>` clears recorded state. Call it before each scenario.
- `GET <url>/_control/verify/<name>` returns `{ "ok": bool, "errors": [string], "requests": n }`. `ok` is false if a request did not match its expectation, an unexpected request arrived, or an expected request never came.

## Scenario format

```jsonc
{
  "name": "create_retries_with_same_idempotency_key",
  "client": { "maxRetries": 2 },        // optional overrides of the top-level defaultClient
  "call": {
    "op": "qbd.invoices.create",        // operation ID
    "kind": "call",                     // call | iterate | firstPage | withResponse | enqueue
    "path": { "id": "..." },            // path parameters by wire name
    "params": { ... },                  // query parameters or JSON body, wire (camelCase) names
    "options": { "idempotencyKey": "...", "endUserId": "...", "timeoutMs": 500, "serverTimeoutSeconds": 30 },
    "wait": { "timeoutMs": 20000 }      // enqueue only: then call handle.wait()
  },
  "exchanges": [ { "expect": { ... }, "respond": { ... }, "repeat": false } ],
  "outcome": { ... }
}
```

### Client settings

Build a fresh client for each scenario:

- `apiKey`: `scenario.client.apiKey`, else the top-level `apiKey`.
- `baseUrl`: `<url>/s/<name>`.
- `endUserId`, `maxRetries`, `timeoutMs`: `scenario.client` if the key is present (`endUserId: null` means no default end user), else `defaultClient`.

If building the client throws (invalid key), handle the error as the call's error.

### Call kinds

| kind | What the runner does |
| --- | --- |
| `call` | Invokes the operation and keeps the typed result. |
| `iterate` | Auto-paginates the cursor list and collects every item. If iteration raises, keeps both the items yielded so far and the error. |
| `firstPage` | Fetches only the first page, without iterating. |
| `withResponse` | Calls through the SDK's raw-response access and keeps the result plus status and headers. |
| `enqueue` | Calls the operation in async mode (`Prefer: respond-async`), keeps the request handle, then calls `handle.wait()` with `wait.timeoutMs`. |

Operations and parameter shapes used by the fixtures:

| op | path | params |
| --- | --- | --- |
| `qbd.invoices.list` | | `customerIds` (string[]), `limit` (int) |
| `qbd.invoices.create` | | `customerId`, `transactionDate` (date), `memo`, `lines[]` of `{ itemId, quantity (number), rate (decimal string) }` |
| `qbd.invoices.update` | `id` | `revisionNumber`, `memo` (`null` means "send null to clear") |
| `qbd.invoices.void` | `id` | |
| `qbd.customers.retrieve` | `id` | |
| `qbd.healthCheck` | | |
| `endUsers.list` | | `limit` |
| `endUsers.passthrough` | `id` | free-form JSON body |
| `requests.retrieve` | `id` | |

Convert params to the SDK's native types. Decimal strings become the native decimal type and dates become the native date type. A key present with `null` is an explicit null, and an absent key is not set. The runner fails if a decimal is sent as anything other than the expected string.

### Request matchers (`expect`)

- `method` and `path` must match exactly. The path is relative to the scenario base URL.
- `query`: if present, the exact set of query keys. A value is either an array of strings (all repeated values, in order) or a matcher.
- `headers`: only the listed headers are checked, with case-insensitive names. A value is a literal string or a matcher.
- `body`: if present, deep JSON equality.

Matchers:

| Matcher | Meaning |
| --- | --- |
| `{ "$absent": true }` | Header or parameter must not be sent |
| `{ "$uuid": true }` | A UUID |
| `{ "$regex": "..." }` | Matches the regular expression |
| `{ "$capture": "k" }` | Present. The server stores the value as `k` |
| `{ "$same": "k" }` | Equals the value captured as `k` |

Matchers combine, for example `{ "$uuid": true, "$capture": "key" }`.

### Responses (`respond`)

- `status`, `headers` and `body` (JSON) or `rawBody` (text).
- `delayMs`: wait before answering. Used to trigger client timeouts.
- `drop: true`: close the connection without answering, which simulates a network failure after the request was sent.
- `repeat: true` (on the exchange): the last exchange answers any number of further requests, at least one.

### Outcome

The runner compares only the keys that are present.

- `result`: map of dotted wire paths to expected JSON values, for example `{ "id": "7-1700000000", "subtotal": "105.50" }`. Serialize the SDK's typed result back to wire JSON with the SDK's own serialization, then compare. Decimals must come out as the same decimal string.
- `items`: the `id`s of the items yielded by `iterate`, in order.
- `page`: `ids`, `nextCursor`, `hasMore`, `remainingCount` of the first page.
- `handle`: `id` and `status` of the request handle returned by `enqueue`.
- `response`: `status`, `requestId` (from `Daapi-Request-Id`) and `headers` (lowercase name to value).
- `error`: the call must raise. `class` uses the canonical name below. Error fields are exact: `type`, `code`, `status` (HTTP status), `message`, `userFacingMessage`, `requestId`, `param`, `retryable`, `outcome`, `integrationCode`, `cause`, `docsUrl`, `fixes`, and `details` (subset match). Cursor progress fields are `itemsYielded`, `pagesServed`, `lastId` and `lastUpdatedAt`.

Canonical error classes:

| Canonical | Node.js | Python | .NET | Java |
| --- | --- | --- | --- | --- |
| `DaapiError` (base, client-side problems) | `DaapiError` | `DaapiError` | `DaapiException` | `DaapiException` |
| `ApiError` (any API error response) | `ApiError` | `APIError` | `ApiException` | `ApiException` |
| `InvalidRequestError` | same | same | `InvalidRequestException` | `InvalidRequestException` |
| `AuthenticationError` | same | same | `AuthenticationException` | `AuthenticationException` |
| `PermissionError` | same | `PermissionDeniedError` | `PermissionException` | `PermissionException` |
| `BillingError` | same | same | `BillingException` | `BillingException` |
| `RateLimitError` | same | same | `RateLimitException` | `RateLimitException` |
| `IntegrationConnectionError` | same | same | `IntegrationConnectionException` | `IntegrationConnectionException` |
| `IntegrationError` | same | same | `IntegrationException` | `IntegrationException` |
| `OutcomeUnknownError` | same | same | `OutcomeUnknownException` | `OutcomeUnknownException` |
| `InternalError` | same | same | `InternalException` | `InternalException` |
| `CursorExpiredError` (subclass of `InvalidRequestError`) | same | same | `CursorExpiredException` | `CursorExpiredException` |
| `RequestPendingError` | same | same | `RequestPendingException` | `RequestPendingException` |
| `ApiConnectionError` (no response) | same | `APIConnectionError` | `ApiConnectionException` | `ApiConnectionException` |

The class must be the exact canonical class or a subclass of it. `ApiError` expects exactly the base class: an unknown error `type` must not be forced into a subclass.

After each scenario, call the verify endpoint and fail on `ok: false`.

## Webhook vectors

For each case in `webhooks.json`, call the SDK's verification helper with the body (string), the headers (as given, mixed case) and the case's `secret`. Inject the case's `now` (Unix seconds) as the verifier's clock. For `valid: true`, compare the parsed event's `id`, `type`, `timestamp`, `projectId`, `data.status` (`dataStatus`) and `data.id` (`dataId`). For `signatureOnly` cases, check only the signature, through the SDK's signature-only function. For `valid: false`, the helper must raise the SDK's webhook verification error.

## API-key vectors

Every key in `valid` must pass the SDK's local check. Every entry in `invalid` must be rejected before any request is sent.
