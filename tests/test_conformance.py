"""Conformance suite (conformance/README.md).

Every scenario in conformance/fixtures/scenarios.json runs through the real HTTP stack of both
the sync and the async client against conformance/mock-server.mjs; then every webhook and API-key
vector runs through the SDK helpers.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import re
import urllib.request
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Callable, Optional

import pytest

import desktopaccountingapi as daapi
from conftest import load_fixture
from desktopaccountingapi import types as daapi_types
from desktopaccountingapi._wire import WireModel
from desktopaccountingapi.webhooks import verify, verify_signature

SCENARIOS = load_fixture("scenarios.json")
WEBHOOKS = load_fixture("webhooks.json")
API_KEYS = load_fixture("api-keys.json")

CANONICAL_ERRORS: dict[str, type[BaseException]] = {
    "DaapiError": daapi.DaapiError,
    "ApiError": daapi.APIError,
    "InvalidRequestError": daapi.InvalidRequestError,
    "AuthenticationError": daapi.AuthenticationError,
    "PermissionError": daapi.PermissionDeniedError,
    "BillingError": daapi.BillingError,
    "RateLimitError": daapi.RateLimitError,
    "IntegrationConnectionError": daapi.IntegrationConnectionError,
    "IntegrationError": daapi.IntegrationError,
    "OutcomeUnknownError": daapi.OutcomeUnknownError,
    "InternalError": daapi.InternalError,
    "CursorExpiredError": daapi.CursorExpiredError,
    "RequestPendingError": daapi.RequestPendingError,
    "ApiConnectionError": daapi.APIConnectionError,
}


TYPE_CLASSES: tuple[type[daapi.APIError], ...] = (
    daapi.InvalidRequestError,
    daapi.AuthenticationError,
    daapi.PermissionDeniedError,
    daapi.BillingError,
    daapi.RateLimitError,
    daapi.IntegrationConnectionError,
    daapi.IntegrationError,
    daapi.OutcomeUnknownError,
    daapi.InternalError,
)


def snake(name: str) -> str:
    """Same word splitting as the generator (packages/sdk-generator/src/naming.ts)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]
    out = "_".join(words)
    return out + "_" if out in ("class", "from", "import", "global", "lambda") else out


# ---------------------------------------------------------------------------------------------
# Converting fixture params (wire JSON) into native SDK arguments, driven by the annotations of
# the generated method signatures and request models.
# ---------------------------------------------------------------------------------------------


def _model_class(annotation: str) -> Optional[type[WireModel]]:
    for name in re.findall(r"[A-Z][A-Za-z0-9]*", annotation):
        cls = getattr(daapi_types, name, None)
        if isinstance(cls, type) and issubclass(cls, WireModel):
            return cls
    return None


def _convert(value: Any, annotation: str) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        inner = re.search(r"Sequence\[(.*)\]", annotation)
        return [_convert(v, inner.group(1) if inner else annotation) for v in value]
    if isinstance(value, Mapping):
        cls = _model_class(annotation)
        if cls is None:
            return dict(value)
        return cls(**_kwargs(cls.__init__, value))
    if "Decimal" in annotation:
        if not isinstance(value, str):
            raise AssertionError(f"fixture decimal must be a string, got {value!r}")
        return Decimal(value)
    if "_dt.date" in annotation and "_dt.datetime" not in annotation and isinstance(value, str):
        return _dt.date.fromisoformat(value)
    return value


def _kwargs(func: Callable[..., Any], params: Mapping[str, Any]) -> dict[str, Any]:
    sig = inspect.signature(func)
    out: dict[str, Any] = {}
    for wire, value in params.items():
        name = snake(wire)
        if name not in sig.parameters:
            raise AssertionError(f"{getattr(func, '__qualname__', func)} has no parameter {name} (wire {wire})")
        out[name] = _convert(value, str(sig.parameters[name].annotation))
    return out


def _resource(client: Any, op_id: str) -> tuple[Any, str]:
    parts = op_id.split(".")
    target = client
    for part in parts[:-1]:
        target = getattr(target, snake(part))
    return target, snake(parts[-1])


def _call_args(method: Callable[..., Any], call: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    sig = inspect.signature(method)
    args: list[Any] = [call.get("path", {})[wire] for wire in call.get("path", {})]
    params = call.get("params") or {}
    if "body" in sig.parameters:
        args.append(dict(params))
        kwargs: dict[str, Any] = {}
    else:
        kwargs = _kwargs(method, params)
    options = call.get("options") or {}
    if "idempotencyKey" in options:
        kwargs["idempotency_key"] = options["idempotencyKey"]
    if "endUserId" in options:
        kwargs["end_user_id"] = options["endUserId"]
    if "conductorEndUserId" in options:
        kwargs["conductor_end_user_id"] = options["conductorEndUserId"]
    if "timeoutMs" in options:
        kwargs["timeout"] = options["timeoutMs"] / 1000
    if "serverTimeoutSeconds" in options:
        kwargs["server_timeout"] = options["serverTimeoutSeconds"]
    return args, kwargs


def _client_kwargs(scenario: Mapping[str, Any], url: str) -> dict[str, Any]:
    default = SCENARIOS["defaultClient"]
    overrides = scenario.get("client") or {}

    def pick(key: str) -> Any:
        return overrides[key] if key in overrides else default.get(key)

    kwargs: dict[str, Any] = {
        "api_key": overrides.get("apiKey", SCENARIOS["apiKey"]),
        "base_url": f"{url}/s/{scenario['name']}{overrides.get('baseUrlSuffix', '')}",
        "end_user_id": pick("endUserId"),
        "max_retries": pick("maxRetries"),
        "timeout": pick("timeoutMs") / 1000,
    }
    if "defaultHeaders" in overrides:
        kwargs["default_headers"] = overrides["defaultHeaders"]
    if "totalTimeoutMs" in overrides:
        kwargs["total_timeout"] = overrides["totalTimeoutMs"] / 1000
    return kwargs


class Observed:
    def __init__(self) -> None:
        self.result: Any = None
        self.text: Optional[str] = None
        self.items: Optional[list[Any]] = None
        self.page: Optional[daapi.CursorPage[Any]] = None
        self.handle: Any = None
        self.response: Optional[daapi.RawResponse[Any]] = None
        self.error: Optional[BaseException] = None


def run_sync(scenario: Mapping[str, Any], url: str) -> Observed:
    seen = Observed()
    call = scenario["call"]
    kind = call["kind"]
    try:
        client = daapi.DesktopAccountingApi(**_client_kwargs(scenario, url))
    except daapi.DaapiError as error:
        seen.error = error
        return seen
    with client:
        resource, name = _resource(client, call["op"])
        try:
            if kind == "call":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.result = method(*args, **kwargs)
            elif kind == "iterate":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.items = []
                for item in method(*args, **kwargs):
                    seen.items.append(item)
                    if "take" in call and len(seen.items) >= call["take"]:
                        break
            elif kind == "firstPage":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.page = method(*args, **kwargs).first_page()
            elif kind == "xml":
                method = getattr(resource, f"{name}_xml")
                args, kwargs = _call_args(method, call)
                seen.text = method(*args, call["xml"], **kwargs)
            elif kind == "withResponse":
                method = getattr(resource.with_raw_response, name)
                args, kwargs = _call_args(getattr(resource, name), call)
                seen.response = method(*args, **kwargs)
                seen.result = seen.response.parse()
            elif kind == "enqueue":
                method = getattr(resource.enqueue, name)
                args, kwargs = _call_args(method, call)
                seen.handle = method(*args, **kwargs)
                seen.result = seen.handle.wait(timeout=call["wait"]["timeoutMs"] / 1000)
            else:
                raise AssertionError(f"unknown call kind {kind}")
        except daapi.DaapiError as error:
            seen.error = error
    return seen


async def _run_async(scenario: Mapping[str, Any], url: str) -> Observed:
    seen = Observed()
    call = scenario["call"]
    kind = call["kind"]
    try:
        client = daapi.AsyncDesktopAccountingApi(**_client_kwargs(scenario, url))
    except daapi.DaapiError as error:
        seen.error = error
        return seen
    async with client:
        resource, name = _resource(client, call["op"])
        try:
            if kind == "call":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.result = await method(*args, **kwargs)
            elif kind == "iterate":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.items = []
                async for item in method(*args, **kwargs):
                    seen.items.append(item)
                    if "take" in call and len(seen.items) >= call["take"]:
                        break
            elif kind == "firstPage":
                method = getattr(resource, name)
                args, kwargs = _call_args(method, call)
                seen.page = await method(*args, **kwargs).first_page()
            elif kind == "xml":
                method = getattr(resource, f"{name}_xml")
                args, kwargs = _call_args(method, call)
                seen.text = await method(*args, call["xml"], **kwargs)
            elif kind == "withResponse":
                method = getattr(resource.with_raw_response, name)
                args, kwargs = _call_args(getattr(resource, name), call)
                seen.response = await method(*args, **kwargs)
                seen.result = seen.response.parse()
            elif kind == "enqueue":
                method = getattr(resource.enqueue, name)
                args, kwargs = _call_args(method, call)
                seen.handle = await method(*args, **kwargs)
                seen.result = await seen.handle.wait(timeout=call["wait"]["timeoutMs"] / 1000)
            else:
                raise AssertionError(f"unknown call kind {kind}")
        except daapi.DaapiError as error:
            seen.error = error
    return seen


def run_async(scenario: Mapping[str, Any], url: str) -> Observed:
    return asyncio.run(_run_async(scenario, url))


# ---------------------------------------------------------------------------------------------
# Outcome checks
# ---------------------------------------------------------------------------------------------


def _wire(value: Any) -> Any:
    if isinstance(value, WireModel):
        return value.to_dict()
    return value


def _dotted(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise AssertionError(f"result has no {path!r}: {json.dumps(data)[:300]}")
        current = current[part]
    return current


def _error_fields(error: BaseException) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if isinstance(error, daapi.APIError):
        fields.update(
            type=error.type,
            code=error.code,
            status=error.status,
            message=error.message,
            userFacingMessage=error.user_facing_message,
            requestId=error.request_id,
            param=error.param,
            retryable=error.retryable,
            outcome=error.outcome,
            integrationCode=error.integration_code,
            cause=error.cause,
            docsUrl=error.docs_url,
            fixes=[f.to_dict() for f in error.fixes],
        )
    if isinstance(error, daapi.CursorExpiredError):
        fields.update(
            itemsYielded=error.items_yielded,
            pagesServed=error.pages_served,
            lastId=error.last_id,
            lastUpdatedAt=error.last_updated_at,
        )
    if isinstance(error, daapi.RequestPendingError):
        fields["requestId"] = error.request_id
        fields["timeoutErrorCode"] = error.timeout_error.code if error.timeout_error is not None else None
    if isinstance(error, daapi.DaapiError):
        fields["idempotencyKey"] = error.idempotency_key
    return fields


def check_outcome(scenario: Mapping[str, Any], seen: Observed) -> None:
    outcome = scenario["outcome"]
    expected_error = outcome.get("error")
    if expected_error is None and seen.error is not None:
        raise AssertionError(f"unexpected error: {seen.error!r}") from seen.error
    if "result" in outcome:
        assert seen.result is not None, "no result"
        wire = _wire(seen.result)
        for path, value in outcome["result"].items():
            assert _dotted(wire, path) == value, f"result {path}: {_dotted(wire, path)!r} != {value!r}"
    if "text" in outcome:
        assert seen.text == outcome["text"]
    if "items" in outcome:
        assert seen.items is not None
        assert [item.id for item in seen.items] == outcome["items"]
    if "page" in outcome:
        page = seen.page
        assert page is not None
        exp = outcome["page"]
        if "ids" in exp:
            assert [item.id for item in page.data] == exp["ids"]
        if "nextCursor" in exp:
            assert page.next_cursor == exp["nextCursor"]
        if "hasMore" in exp:
            assert page.has_more == exp["hasMore"]
        if "remainingCount" in exp:
            assert page.remaining_count == exp["remainingCount"]
    if "handle" in outcome:
        handle = seen.handle
        assert handle is not None
        assert handle.id == outcome["handle"]["id"]
        if "status" in outcome["handle"]:
            assert handle.request is not None and handle.request.status == outcome["handle"]["status"]
    if "response" in outcome:
        response = seen.response
        assert response is not None
        exp = outcome["response"]
        if "status" in exp:
            assert response.status_code == exp["status"]
        if "requestId" in exp:
            assert response.request_id == exp["requestId"]
        for name, value in (exp.get("headers") or {}).items():
            assert response.headers.get(name) == value, f"header {name}"
    if expected_error is not None:
        error = seen.error
        assert error is not None, "expected an error, the call succeeded"
        cls = CANONICAL_ERRORS[expected_error["class"]]
        if expected_error["class"] == "ApiError":
            # Exactly APIError, apart from the Conductor status classes it is combined with.
            assert isinstance(error, daapi.APIError) and not isinstance(error, TYPE_CLASSES), (
                f"expected exactly APIError, got {type(error).__name__}: {error!r}"
            )
        else:
            assert isinstance(error, cls), f"expected {cls.__name__}, got {type(error).__name__}: {error!r}"
        fields = _error_fields(error)
        for key, value in expected_error.items():
            if key == "class":
                continue
            if key == "details":
                assert isinstance(error, daapi.APIError)
                for dk, dv in value.items():
                    assert error.details.get(dk) == dv, f"details.{dk}: {error.details.get(dk)!r} != {dv!r}"
                continue
            assert key in fields, f"error field {key} is not exposed by {type(error).__name__}"
            if key == "idempotencyKey" and value == "$uuid":
                assert re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(fields[key])
                ), fields[key]
                continue
            assert fields[key] == value, f"error {key}: {fields[key]!r} != {value!r}"


def _control(url: str, action: str, name: str) -> Any:
    method = "POST" if action == "reset" else "GET"
    request = urllib.request.Request(f"{url}/_control/{action}/{name}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
    return json.loads(body) if body else None


SCENARIO_IDS = [s["name"] for s in SCENARIOS["scenarios"]]


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("scenario", SCENARIOS["scenarios"], ids=SCENARIO_IDS)
def test_scenario(scenario: Mapping[str, Any], mode: str, mock_server_url: str) -> None:
    if "python" not in scenario.get("only", ["python"]):
        pytest.skip("scenario for other SDKs")
    _control(mock_server_url, "reset", scenario["name"])
    seen = (run_sync if mode == "sync" else run_async)(scenario, mock_server_url)
    check_outcome(scenario, seen)
    verdict = _control(mock_server_url, "verify", scenario["name"])
    assert verdict["ok"], f"mock server: {verdict['errors']}"


@pytest.mark.parametrize("case", WEBHOOKS["cases"], ids=[c["name"] for c in WEBHOOKS["cases"]])
def test_webhook_vector(case: Mapping[str, Any]) -> None:
    secret = case.get("secret", WEBHOOKS["secret"])
    tolerance = WEBHOOKS.get("toleranceSeconds", 300)

    def clock() -> float:
        return float(case["now"])

    if case.get("signatureOnly"):
        if case["valid"]:
            verify_signature(case["body"], case["headers"], secret, tolerance=tolerance, now=clock)
        else:
            with pytest.raises(daapi.WebhookVerificationError):
                verify_signature(case["body"], case["headers"], secret, tolerance=tolerance, now=clock)
        return
    if not case["valid"]:
        with pytest.raises(daapi.WebhookVerificationError):
            verify(case["body"], case["headers"], secret, tolerance=tolerance, now=clock)
        return
    event = verify(case["body"], case["headers"], secret, tolerance=tolerance, now=clock)
    expected = case["event"]
    assert event.id == expected["id"]
    assert event.type == expected["type"]
    assert event.timestamp == expected["timestamp"]
    assert event.project_id == expected["projectId"]
    assert event.data.get("status") == expected["dataStatus"]
    assert event.data.get("id") == expected["dataId"]


@pytest.mark.parametrize("key", API_KEYS["valid"])
def test_valid_api_key(key: str) -> None:
    assert daapi.is_valid_secret_key(key)
    daapi.DesktopAccountingApi(api_key=key, base_url="http://127.0.0.1:9").close()


@pytest.mark.parametrize("case", API_KEYS["invalid"], ids=[c["reason"] for c in API_KEYS["invalid"]])
def test_invalid_api_key(case: Mapping[str, Any]) -> None:
    assert not daapi.is_valid_secret_key(case["key"])
    with pytest.raises(daapi.DaapiError):
        daapi.DesktopAccountingApi(api_key=case["key"], base_url="http://127.0.0.1:9")
    with pytest.raises(daapi.DaapiError):
        daapi.AsyncDesktopAccountingApi(api_key=case["key"], base_url="http://127.0.0.1:9")
