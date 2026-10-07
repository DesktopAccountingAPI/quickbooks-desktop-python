"""Runtime behaviour against an in-process httpx.MockTransport (no network)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

import desktopaccountingapi as daapi
from desktopaccountingapi import AsyncDesktopAccountingApi, DesktopAccountingApi
from desktopaccountingapi._base_client import backoff_delay, should_retry
from desktopaccountingapi._errors import error_from_response
from desktopaccountingapi.types import Invoice, InvoiceLineCreateInput

KEY = "sk_test_Conformance0Key0For0SDK0Tests000010nQFLR"
EU = "eu_01j9x4m6v4c8k2t7q0r5s3w1zb"
BASE = "https://api.test"

Handler = Callable[[httpx.Request], httpx.Response]


def invoice_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "7-1700000000",
        "objectType": "qbd_invoice",
        "createdAt": "2026-10-05T09:14:03-07:00",
        "updatedAt": "2026-10-05T09:14:03-07:00",
        "revisionNumber": "1",
        "subtotal": "105.50",
        "lines": [],
    }
    data.update(overrides)
    return data


def error_json(type_: str, code: str, status: int, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": type_,
        "code": code,
        "message": f"{code} happened",
        "userFacingMessage": "Something went wrong.",
        "httpStatusCode": status,
        "integrationCode": None,
        "requestId": "req_body",
        "cause": "Because.",
        "fixes": [{"actor": "developer", "action": "Fix it."}],
        "docsUrl": "https://www.desktopaccountingapi.com/docs/errors/#x",
        "retryable": False,
        "outcome": "not_applied",
        "param": None,
        "details": {},
    }
    error.update(extra)
    return {"error": error}


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def client_for(recorder: Recorder, **kwargs: Any) -> DesktopAccountingApi:
    kwargs.setdefault("end_user_id", EU)
    return DesktopAccountingApi(
        api_key=KEY, base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(recorder)), **kwargs
    )


def async_client_for(recorder: Recorder, **kwargs: Any) -> AsyncDesktopAccountingApi:
    kwargs.setdefault("end_user_id", EU)
    return AsyncDesktopAccountingApi(
        api_key=KEY, base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)), **kwargs
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("desktopaccountingapi._base_client.time.sleep", lambda s: None)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("desktopaccountingapi._base_client.asyncio.sleep", fast_sleep)
    yield


# --- configuration ---------------------------------------------------------------------------


def test_missing_key_names_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAAPI_SECRET_KEY", raising=False)
    with pytest.raises(daapi.DaapiError, match="DAAPI_SECRET_KEY"):
        DesktopAccountingApi()


def test_key_and_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAAPI_SECRET_KEY", KEY)
    monkeypatch.setenv("DAAPI_BASE_URL", "http://127.0.0.1:1/prefix/")
    with DesktopAccountingApi() as client:
        assert client.api_key == KEY
        assert client.base_url == "http://127.0.0.1:1/prefix"


def test_invalid_key_message_does_not_echo_key() -> None:
    bad = KEY[:-1] + "A"
    with pytest.raises(daapi.DaapiError) as info:
        DesktopAccountingApi(api_key=bad)
    assert bad not in str(info.value)


def test_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAAPI_BASE_URL", raising=False)
    with DesktopAccountingApi(api_key=KEY) as client:
        assert client.base_url == daapi.DEFAULT_BASE_URL


def test_rejects_wrong_http_client_type() -> None:
    with pytest.raises(daapi.DaapiError):
        DesktopAccountingApi(api_key=KEY, http_client=httpx.AsyncClient())  # type: ignore[arg-type]


# --- headers ---------------------------------------------------------------------------------


def test_headers_and_end_user_rules() -> None:
    rec = Recorder([httpx.Response(200, json={"status": "ok", "duration": 5, "quickbooks": {}})])
    client = client_for(rec, end_user_id=None)
    with pytest.raises(daapi.DaapiError, match="end user"):
        client.qbd.health_check()
    assert rec.requests == []
    scoped = client.for_end_user(EU)
    assert scoped._http is client._http
    health = scoped.qbd.health_check(server_timeout=30)
    assert health.status == "ok"
    sent = rec.requests[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["daapi-end-user-id"] == EU
    assert sent.headers["daapi-timeout-seconds"] == "30"
    assert sent.headers["user-agent"] == f"desktopaccountingapi-python/{daapi.__version__}"
    assert "idempotency-key" not in sent.headers
    assert not any(
        h.lower().startswith("daapi-") and h.lower() not in ("daapi-end-user-id", "daapi-timeout-seconds")
        for h in sent.headers
    )


def test_platform_operations_never_send_end_user() -> None:
    rec = Recorder([httpx.Response(200, json={"id": "eu_1", "objectType": "end_user"})])
    client = client_for(rec)
    client.end_users.retrieve("eu_1")
    assert "daapi-end-user-id" not in rec.requests[0].headers


def test_create_serializes_decimal_and_reuses_idempotency_key_on_retry() -> None:
    rec = Recorder(
        [
            httpx.Response(
                503,
                headers={"Daapi-Should-Retry": "true"},
                json=error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503),
            ),
            httpx.ConnectError("reset"),
            httpx.Response(201, json=invoice_json()),
        ]
    )
    client = client_for(rec)
    invoice = client.qbd.invoices.create(
        customer_id="80000001-1",
        memo="Note",
        lines=[InvoiceLineCreateInput(item_id="80000005-1", quantity=2, rate=Decimal("5.00"))],
    )
    assert isinstance(invoice, Invoice) and invoice.subtotal == Decimal("105.50")
    keys = {r.headers["idempotency-key"] for r in rec.requests}
    assert len(rec.requests) == 3 and len(keys) == 1
    assert json.loads(rec.requests[0].content) == {
        "customerId": "80000001-1",
        "memo": "Note",
        "lines": [{"itemId": "80000005-1", "quantity": 2, "rate": "5.00"}],
    }


def test_caller_idempotency_key() -> None:
    rec = Recorder([httpx.Response(200, json=invoice_json())])
    client_for(rec).qbd.invoices.update("7-1", revision_number="1", memo="x", idempotency_key="my-key")
    assert rec.requests[0].headers["idempotency-key"] == "my-key"


# --- retries ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "retried"),
    [
        (429, {}, error_json("RATE_LIMIT_ERROR", "RATE_LIMITED", 429), True),
        (503, {"Daapi-Should-Retry": "true"}, error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503), True),
        (503, {"Daapi-Should-Retry": "false"}, error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503), False),
        (503, {}, error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503), False),
        (429, {"Daapi-Should-Retry": "false"}, error_json("RATE_LIMIT_ERROR", "RATE_LIMITED", 429), False),
        (
            502,
            {"Daapi-Should-Retry": "true"},
            error_json("OUTCOME_UNKNOWN_ERROR", "QBD_WRITE_OUTCOME_UNKNOWN", 502, outcome="unknown"),
            False,
        ),
        (
            500,
            {"Daapi-Should-Retry": "true"},
            error_json("INTERNAL_ERROR", "INTERNAL_ERROR", 500, outcome="pending"),
            False,
        ),
        (400, {"Daapi-Should-Retry": "true"}, error_json("INVALID_REQUEST_ERROR", "INVALID_PARAMETER", 400), False),
    ],
)
def test_should_retry(status: int, headers: dict[str, str], body: dict[str, Any], retried: bool) -> None:
    response = httpx.Response(status, headers=headers, json=body)
    error = error_from_response(status, response.headers, response.content)
    assert should_retry(status, response.headers, error) is retried


def test_non_json_error_without_header_is_not_retried() -> None:
    rec = Recorder([httpx.Response(502, text="<html>Bad gateway</html>")])
    with pytest.raises(daapi.APIError) as info:
        client_for(rec).qbd.customers.retrieve("80000001-1")
    assert type(info.value) is daapi.InternalServerError  # APIError with Conductor's 5xx status class
    assert info.value.status == 502 and info.value.code is None
    assert len(rec.requests) == 1


def test_network_errors_exhaust_retries() -> None:
    rec = Recorder([httpx.ConnectError("refused")])
    with pytest.raises(daapi.APIConnectionError):
        client_for(rec, max_retries=1).qbd.customers.retrieve("80000001-1")
    assert len(rec.requests) == 2
    rec = Recorder([httpx.ReadTimeout("slow")])
    with pytest.raises(daapi.APITimeoutError):
        client_for(rec, max_retries=0).qbd.customers.retrieve("80000001-1")


def test_backoff_and_retry_after() -> None:
    assert backoff_delay(0, httpx.Headers({"Retry-After": "0"})) == 0.0
    assert backoff_delay(0, httpx.Headers({"Retry-After": "3"})) == 3.0
    assert backoff_delay(0, httpx.Headers({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    for attempt, cap in [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (10, 8.0)]:
        delay = backoff_delay(attempt)
        assert 0.75 * cap <= delay <= cap


# --- errors ----------------------------------------------------------------------------------


def test_error_fields_and_request_id_fallback() -> None:
    body = error_json(
        "INTEGRATION_CONNECTION_ERROR", "QBD_MODAL_DIALOG_OPEN", 503, integrationCode="0x80040414", requestId=None
    )
    error = error_from_response(503, httpx.Headers({"Daapi-Request-Id": "req_header"}), json.dumps(body).encode())
    assert isinstance(error, daapi.IntegrationConnectionError)
    assert error.code == daapi.ErrorCode.QBD_MODAL_DIALOG_OPEN
    assert error.type == daapi.ErrorType.INTEGRATION_CONNECTION_ERROR
    assert error.request_id == "req_header"
    assert error.integration_code == "0x80040414"
    assert error.fixes == [daapi.ErrorFix("developer", "Fix it.")]
    assert error.user_facing_message == "Something went wrong."
    assert "QBD_MODAL_DIALOG_OPEN" in str(error)


def test_error_classes_by_type() -> None:
    cases = {
        "INVALID_REQUEST_ERROR": daapi.InvalidRequestError,
        "AUTHENTICATION_ERROR": daapi.AuthenticationError,
        "PERMISSION_ERROR": daapi.PermissionDeniedError,
        "BILLING_ERROR": daapi.BillingError,
        "RATE_LIMIT_ERROR": daapi.RateLimitError,
        "INTEGRATION_CONNECTION_ERROR": daapi.IntegrationConnectionError,
        "INTEGRATION_ERROR": daapi.IntegrationError,
        "OUTCOME_UNKNOWN_ERROR": daapi.OutcomeUnknownError,
        "INTERNAL_ERROR": daapi.InternalError,
    }
    for type_, cls in cases.items():
        error = error_from_response(400, httpx.Headers(), json.dumps(error_json(type_, "X", 400)).encode())
        assert isinstance(error, cls) and type(error).__name__ == cls.__name__
        assert isinstance(error, daapi.BadRequestError) and isinstance(error, daapi.APIStatusError)
    future = error_from_response(418, httpx.Headers(), json.dumps(error_json("FUTURE", "X", 418)).encode())
    assert type(future) is daapi.APIStatusError
    assert not isinstance(future, tuple(cases.values()))
    expired = error_from_response(
        410, httpx.Headers(), json.dumps(error_json("INVALID_REQUEST_ERROR", "CURSOR_EXPIRED", 410)).encode()
    )
    assert isinstance(expired, daapi.CursorExpiredError) and isinstance(expired, daapi.InvalidRequestError)


def test_conductor_error_names() -> None:
    assert daapi.ConductorError is daapi.DaapiError
    not_found = error_json("INVALID_REQUEST_ERROR", "OBJECT_NOT_FOUND", 404, integrationCode="500")
    rec = Recorder([httpx.Response(404, headers={"Daapi-Should-Retry": "false"}, json=not_found)])
    client = client_for(rec)
    with pytest.raises(daapi.NotFoundError) as info:
        client.qbd.invoices.retrieve("7-1")
    error = info.value
    assert isinstance(error, daapi.InvalidRequestError) and isinstance(error, daapi.APIStatusError)
    assert type(error).__name__ == "InvalidRequestError"
    assert not isinstance(error, (daapi.BadRequestError, daapi.ConflictError, daapi.InternalServerError))
    assert error.status_code == 404 == error.status == error.http_status_code
    assert (error.code, error.type, error.integration_code, error.request_id) == (
        "OBJECT_NOT_FOUND",
        "INVALID_REQUEST_ERROR",
        "500",
        "req_body",
    )
    assert error.user_facing_message == "Something went wrong." and error.fixes and error.docs_url
    assert getattr(error, "code", None) == "OBJECT_NOT_FOUND"  # Conductor's documented getattr pattern
    statuses = {
        400: daapi.BadRequestError,
        409: daapi.ConflictError,
        422: daapi.UnprocessableEntityError,
        503: daapi.InternalServerError,
    }
    for status, cls in statuses.items():
        e = error_from_response(
            status, httpx.Headers(), json.dumps(error_json("INTEGRATION_ERROR", "X", status)).encode()
        )
        assert isinstance(e, cls) and isinstance(e, daapi.IntegrationError)
    cursor = error_from_response(
        410, httpx.Headers(), json.dumps(error_json("INVALID_REQUEST_ERROR", "CURSOR_EXPIRED", 410)).encode()
    )
    assert isinstance(cursor, daapi.CursorExpiredError) and isinstance(cursor, daapi.APIStatusError)
    again = error_from_response(404, httpx.Headers(), json.dumps(not_found).encode())
    assert type(again) is type(error), "combined classes are cached"


# --- pending requests and async mode ---------------------------------------------------------


def request_json(status: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": "req_1", "objectType": "request", "status": status, "result": None, "error": None}
    data.update(extra)
    return data


def test_pending_write_is_long_polled_not_resubmitted() -> None:
    pending = error_json(
        "INTEGRATION_CONNECTION_ERROR", "QBD_REQUEST_TIMEOUT", 504, outcome="pending", details={"requestId": "req_1"}
    )
    rec = Recorder(
        [
            httpx.Response(504, headers={"Daapi-Should-Retry": "false"}, json=pending),
            httpx.Response(200, json=request_json("sent")),
            httpx.Response(200, json=request_json("succeeded", result=invoice_json())),
        ]
    )
    invoice = client_for(rec).qbd.invoices.create(customer_id="80000001-1")
    assert invoice.id == "7-1700000000"
    assert [r.method for r in rec.requests] == ["POST", "GET", "GET"]
    assert rec.requests[1].url.path == "/v1/requests/req_1"
    wait = int(rec.requests[1].url.params["waitSeconds"])
    assert 1 <= wait <= 60
    assert "idempotency-key" not in rec.requests[1].headers


def test_enqueue_handle() -> None:
    rec = Recorder(
        [
            httpx.Response(202, json=request_json("queued")),
            httpx.Response(200, json=request_json("queued")),
            httpx.Response(
                200,
                json=request_json("failed", error=error_json("INTEGRATION_ERROR", "QBD_DUPLICATE_NAME", 409)["error"]),
            ),
        ]
    )
    handle = client_for(rec).qbd.invoices.enqueue.create(customer_id="80000001-1", queue_ttl=600)
    assert handle.id == "req_1" and handle.request is not None and handle.request.status == "queued"
    assert rec.requests[0].headers["prefer"] == "respond-async"
    assert rec.requests[0].headers["daapi-queue-ttl-seconds"] == "600"
    with pytest.raises(daapi.RequestPendingError):
        handle.result()
    with pytest.raises(daapi.IntegrationError) as info:
        handle.result()
    assert info.value.code == "QBD_DUPLICATE_NAME" and info.value.status == 409


def test_raw_response() -> None:
    rec = Recorder(
        [httpx.Response(200, headers={"Daapi-Request-Id": "req_raw", "Daapi-Warnings": "2"}, json=invoice_json())]
    )
    raw = client_for(rec).qbd.invoices.with_raw_response.retrieve("7-1")
    assert raw.status_code == 200 and raw.request_id == "req_raw" and raw.warnings == 2
    invoice: Invoice = raw.parse()
    assert invoice.id == "7-1700000000"


# --- pagination ------------------------------------------------------------------------------


def page_json(ids: list[str], cursor: str | None) -> dict[str, Any]:
    return {
        "objectType": "list",
        "url": "/v1/quickbooks-desktop/invoices",
        "data": [invoice_json(id=i, updatedAt=f"2026-10-05T09:0{n}:00-07:00") for n, i in enumerate(ids)],
        "nextCursor": cursor,
        "hasMore": cursor is not None,
        "remainingCount": 1 if cursor else None,
        "cursorExpiresAt": "2026-10-05T16:04:11.120Z" if cursor else None,
    }


def test_pagination_continue_sends_only_cursor_and_limit() -> None:
    rec = Recorder(
        [httpx.Response(200, json=page_json(["1", "2"], "c1")), httpx.Response(200, json=page_json(["3"], None))]
    )
    pager = client_for(rec).qbd.invoices.list(customer_ids=["a", "b"], limit=2)
    assert rec.requests == []  # lazy
    assert [i.id for i in pager] == ["1", "2", "3"]
    assert sorted(rec.requests[0].url.params.multi_items()) == [
        ("customerIds", "a"),
        ("customerIds", "b"),
        ("limit", "2"),
    ]
    assert rec.requests[1].url.params.multi_items() == [("cursor", "c1"), ("limit", "2")]


def test_first_page_and_iter_pages() -> None:
    rec = Recorder(
        [httpx.Response(200, json=page_json(["1", "2"], "c1")), httpx.Response(200, json=page_json(["3"], None))]
    )
    client = client_for(rec)
    page = client.qbd.invoices.list().first_page()
    assert [i.id for i in page.data] == ["1", "2"] and page.has_more and page.next_cursor == "c1"
    assert page.remaining_count == 1 and page.cursor_expires_at is not None
    rec.responses = [
        httpx.Response(200, json=page_json(["1", "2"], "c1")),
        httpx.Response(200, json=page_json(["3"], None)),
    ]
    assert [len(p) for p in client.qbd.invoices.list().iter_pages()] == [2, 1]
    assert rec.requests[-1].url.params.multi_items() == [("cursor", "c1")]


def test_loop_that_stops_early_sends_no_extra_request() -> None:
    for stop_at in ("1", "2"):
        rec = Recorder([httpx.Response(200, json=page_json(["1", "2"], "c1"))])
        for invoice in client_for(rec).qbd.invoices.list(limit=2):
            if invoice.id == stop_at:
                break
        assert len(rec.requests) == 1, stop_at
    rec = Recorder([httpx.Response(200, json=page_json(["1", "2"], "c1"))])
    for page in client_for(rec).qbd.invoices.list(limit=2).iter_pages():
        assert len(page) == 2
        break
    assert len(rec.requests) == 1


def test_fast_loop_requests_the_next_page_when_needed() -> None:
    rec = Recorder(
        [httpx.Response(200, json=page_json(["1", "2"], "c1")), httpx.Response(200, json=page_json(["3"], None))]
    )
    seen: list[str] = []
    for invoice in client_for(rec).qbd.invoices.list(limit=2):
        seen.append(invoice.id)
        if invoice.id == "2":
            assert len(rec.requests) == 1
    assert seen == ["1", "2", "3"] and len(rec.requests) == 2


def test_slow_loop_reads_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("desktopaccountingapi._pagination._READ_AHEAD_AFTER", 0.0)
    rec = Recorder(
        [httpx.Response(200, json=page_json(["1", "2"], "c1")), httpx.Response(200, json=page_json(["3"], None))]
    )
    seen = [invoice.id for invoice in client_for(rec).qbd.invoices.list(limit=2)]
    assert seen == ["1", "2", "3"]
    assert [r.url.params.get("cursor") for r in rec.requests] == [None, "c1"]

    async def scenario() -> None:
        rec.responses = [
            httpx.Response(200, json=page_json(["1", "2"], "c1")),
            httpx.Response(200, json=page_json(["3"], None)),
        ]
        rec.requests.clear()
        async with async_client_for(rec) as client:
            got: list[str] = []
            async for invoice in client.qbd.invoices.list(limit=2):
                got.append(invoice.id)
                if invoice.id == "1":
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
            assert got == ["1", "2", "3"] and len(rec.requests) == 2

    asyncio.run(scenario())


def test_cursor_expired_progress() -> None:
    expired = error_json(
        "INVALID_REQUEST_ERROR", "CURSOR_EXPIRED", 410, outcome="not_applicable", details={"reason": "idle_timeout"}
    )
    rec = Recorder([httpx.Response(200, json=page_json(["1", "2"], "c1")), httpx.Response(410, json=expired)])
    seen: list[str] = []
    with pytest.raises(daapi.CursorExpiredError) as info:
        for invoice in client_for(rec).qbd.invoices.list():
            seen.append(invoice.id)
    assert seen == ["1", "2"]
    error = info.value
    assert (error.items_yielded, error.pages_served, error.last_id) == (2, 1, "2")
    assert error.last_updated_at == "2026-10-05T09:01:00-07:00"
    assert error.reason == "idle_timeout"


# --- async client ----------------------------------------------------------------------------


def test_async_client_end_to_end() -> None:
    async def scenario() -> None:
        rec = Recorder(
            [
                httpx.Response(200, json=page_json(["1", "2"], "c1")),
                httpx.Response(200, json=page_json(["3"], None)),
            ]
        )
        async with async_client_for(rec) as client:
            ids = [i.id async for i in client.qbd.invoices.list(limit=2)]
            assert ids == ["1", "2", "3"]
            rec.responses = [
                httpx.Response(
                    503,
                    headers={"Daapi-Should-Retry": "true"},
                    json=error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503),
                ),
                httpx.Response(201, json=invoice_json()),
            ]
            rec.requests.clear()
            invoice = await client.qbd.invoices.create(customer_id="80000001-1")
            assert invoice.subtotal == Decimal("105.50")
            assert rec.requests[0].headers["idempotency-key"] == rec.requests[1].headers["idempotency-key"]
            rec.responses = [httpx.Response(200, headers={"Daapi-Request-Id": "req_raw"}, json=invoice_json())]
            raw = await client.qbd.invoices.with_raw_response.retrieve("7-1")
            assert raw.request_id == "req_raw" and raw.parse().id == "7-1700000000"
            rec.responses = [
                httpx.Response(202, json=request_json("queued")),
                httpx.Response(200, json=request_json("succeeded", result=invoice_json())),
            ]
            handle = await client.qbd.invoices.enqueue.create(customer_id="80000001-1")
            assert (await handle.wait(timeout=5)).id == "7-1700000000"
            all_items = await client.qbd.invoices.list().list_all()
            assert isinstance(all_items, list)

    asyncio.run(scenario())


# --- Conductor-compatible options -------------------------------------------------------------


def test_conductor_end_user_id_alias() -> None:
    rec = Recorder([httpx.Response(200, json=page_json(["1"], "c1")), httpx.Response(200, json=page_json(["2"], None))])
    client = client_for(rec, end_user_id=None)
    ids = [i.id for i in client.qbd.invoices.list(conductor_end_user_id="eu_ported", limit=1)]
    assert ids == ["1", "2"]
    assert [r.headers["daapi-end-user-id"] for r in rec.requests] == ["eu_ported", "eu_ported"]
    assert all(
        "conductorEndUserId" not in str(r.url) and "conductor-end-user-id" not in r.headers for r in rec.requests
    )
    rec.responses = [httpx.Response(201, json=invoice_json())]
    client.qbd.invoices.create(customer_id="80000001-1", conductor_end_user_id="eu_body")
    assert rec.requests[-1].headers["daapi-end-user-id"] == "eu_body"
    assert "conductor" not in rec.requests[-1].content.decode()
    client.qbd.invoices.retrieve("7-1", end_user_id="eu_same", conductor_end_user_id="eu_same")
    count = len(rec.requests)
    with pytest.raises(daapi.DaapiError, match="conductor_end_user_id"):
        client.qbd.invoices.retrieve("7-1", end_user_id="eu_a", conductor_end_user_id="eu_b")
    assert len(rec.requests) == count


def test_base_url_ending_in_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder([httpx.Response(200, json={"status": "ok", "duration": 5, "quickbooks": {}})])
    client = DesktopAccountingApi(
        api_key=KEY,
        base_url="https://api.test/prefix/v1/",
        end_user_id=EU,
        http_client=httpx.Client(transport=httpx.MockTransport(rec)),
    )
    assert client.base_url == "https://api.test/prefix"
    client.qbd.health_check()
    assert rec.requests[0].url.path == "/prefix/v1/quickbooks-desktop/health-check"
    monkeypatch.setenv("DAAPI_BASE_URL", "https://env.test/v1")
    with DesktopAccountingApi(api_key=KEY) as from_env:
        assert from_env.base_url == "https://env.test"


def test_default_headers() -> None:
    rec = Recorder([httpx.Response(200, json=invoice_json())])
    client = client_for(
        rec, default_headers={"X-Team": "billing", "Authorization": "Bearer nope", "Daapi-End-User-Id": "eu_header"}
    )
    client.qbd.invoices.retrieve("7-1")
    sent = rec.requests[0].headers
    assert sent["x-team"] == "billing"
    assert sent["authorization"] == f"Bearer {KEY}" and sent["daapi-end-user-id"] == EU
    client.with_options(default_headers={"X-Other": "1"}).qbd.invoices.retrieve("7-1")
    assert rec.requests[1].headers["x-other"] == "1" and "x-team" not in rec.requests[1].headers
    with pytest.raises(daapi.DaapiError):
        client_for(rec, default_headers={"X-Bad": 1})


def test_total_timeout_caps_attempts_and_retries() -> None:
    rec = Recorder([httpx.Response(200, json=invoice_json())])
    client_for(rec, total_timeout=0.5).qbd.invoices.retrieve("7-1")
    assert rec.requests[0].extensions["timeout"]["read"] <= 0.5
    client_for(rec).qbd.invoices.retrieve("7-1", total_timeout=0.25)
    assert rec.requests[1].extensions["timeout"]["read"] <= 0.25
    client_for(rec).qbd.invoices.retrieve("7-1")
    assert rec.requests[2].extensions["timeout"]["read"] == 100.0
    busy = httpx.Response(
        503,
        headers={"Daapi-Should-Retry": "true", "Retry-After": "1"},
        json=error_json("INTEGRATION_CONNECTION_ERROR", "QBD_MODAL_DIALOG_OPEN", 503),
    )
    rec = Recorder([busy])
    with pytest.raises(daapi.IntegrationConnectionError):
        client_for(rec, total_timeout=0.5).qbd.invoices.retrieve("7-1")
    assert len(rec.requests) == 1, "a retry after 1 s would end after the total timeout"
    rec = Recorder([httpx.ConnectError("refused")])
    with pytest.raises(daapi.APIConnectionError):
        client_for(rec, total_timeout=0.01, max_retries=5).qbd.invoices.retrieve("7-1")
    assert len(rec.requests) == 1


def test_daapi_log_env(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("desktopaccountingapi")
    saved_level, saved_handlers = logger.level, list(logger.handlers)
    monkeypatch.setenv("DAAPI_LOG", "info")
    try:
        client_for(Recorder([httpx.Response(200, json=invoice_json())]))
        assert logger.level == logging.INFO
        assert logger.handlers
    finally:
        logger.setLevel(saved_level)
        logger.handlers[:] = saved_handlers


def test_logger_never_logs_secrets(caplog: pytest.LogCaptureFixture) -> None:
    rec = Recorder(
        [
            httpx.Response(
                503,
                headers={"Daapi-Should-Retry": "true"},
                json=error_json("INTERNAL_ERROR", "SERVICE_UNAVAILABLE", 503),
            ),
            httpx.Response(201, json=invoice_json()),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="desktopaccountingapi"):
        client_for(rec).qbd.invoices.create(customer_id="80000001-1", memo="secret memo")
    text = caplog.text
    assert "Retrying qbd.invoices.create" in text
    assert KEY not in text and "secret memo" not in text and "Bearer" not in text
