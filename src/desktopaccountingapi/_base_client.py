"""HTTP core shared by the generated resources: headers, retries, idempotency, long-polling.

The rules implemented here are the API conventions' SDK obligations (retries only on network
errors, 429 and retryable 5xx; idempotency keys reused across retries; never resubmitting a
pending or unknown-outcome write).
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import os
import random
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Optional, TypeVar, cast

import httpx
from typing_extensions import Self

from ._errors import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    DaapiError,
    OutcomeUnknownError,
    RequestPendingError,
    error_from_object,
    error_from_response,
)
from ._keys import is_valid_secret_key
from ._response import RawResponse
from ._response import raw_response_mode as _raw_response_mode
from ._version import USER_AGENT
from ._wire import body as wire_body
from ._wire import query_items

if TYPE_CHECKING:
    from ._handles import AsyncRequestHandle, RequestHandle
    from ._pagination import AsyncCursorPager, CursorPager
    from .types import Request

T = TypeVar("T")

DEFAULT_BASE_URL = "https://api.desktopaccountingapi.com"
DEFAULT_TIMEOUT = 100.0
DEFAULT_MAX_RETRIES = 2
MAX_POLL_WAIT_SECONDS = 60
_MAX_BACKOFF = 8.0
_INITIAL_BACKOFF = 0.5
_MAX_RETRY_AFTER = 60.0

_logger = logging.getLogger("desktopaccountingapi")

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "off": logging.CRITICAL + 10,
}

#: Headers the SDK manages; ``default_headers`` never supplies them.
_MANAGED_HEADERS = frozenset(
    name.lower()
    for name in (
        "Authorization",
        "Accept",
        "Content-Type",
        "User-Agent",
        "Daapi-End-User-Id",
        "Conductor-End-User-Id",
        "Idempotency-Key",
        "Daapi-Timeout-Seconds",
        "Prefer",
        "Daapi-Queue-Ttl-Seconds",
    )
)


def _setup_logging_from_env() -> None:
    """``DAAPI_LOG=debug|info|warn|error|off`` sets the SDK logger's level and, if the logger has no
    handler, adds one that writes to stderr."""
    level = _LOG_LEVELS.get((os.environ.get("DAAPI_LOG") or "").strip().lower())
    if level is None:
        return
    _logger.setLevel(level)
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s %(name)s] %(levelname)s %(message)s"))
        _logger.addHandler(handler)


def normalize_base_url(url: str) -> str:
    """Removes trailing slashes and one trailing ``/v1``: the SDK adds ``/v1/...`` itself."""
    url = url.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


class Op(NamedTuple):
    """Static operation metadata emitted by the generator."""

    operation_id: str
    write: bool
    """Sends an ``Idempotency-Key`` (create, update, delete, void, passthrough...)."""
    end_user: bool
    """Sends ``Daapi-End-User-Id`` and needs an end user."""
    server_timeout: bool
    """Accepts ``Daapi-Timeout-Seconds``."""
    queue_ttl: bool
    """Accepts ``Daapi-Queue-Ttl-Seconds`` in async mode."""


_REQUESTS_RETRIEVE = Op("requests.retrieve", write=False, end_user=False, server_timeout=False, queue_ttl=False)


class _Prepared(NamedTuple):
    method: str
    url: str
    params: list[tuple[str, str]]
    content: Optional[bytes]
    headers: dict[str, str]
    attempt_timeout: float
    max_retries: int
    deadline: float
    """``time.monotonic()`` deadline for waiting on a pending request: the total timeout if set, else the attempt timeout."""
    operation_id: str
    total_deadline: Optional[float] = None
    """``time.monotonic()`` after which no attempt or retry starts (``total_timeout``), if set."""


def _identity(value: Any) -> Any:
    return value


def _parse_retry_after(headers: httpx.Headers) -> Optional[float]:
    value = headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            # Returns None for some invalid dates on Python 3.10+ and raises on 3.9.
            when: Any = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - time.time()
    if math.isnan(seconds):
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER)


def backoff_delay(attempt: int, headers: Optional[httpx.Headers] = None) -> float:
    """Delay before retry number ``attempt + 1``: ``Retry-After`` if present, else 0.5 s * 2^attempt (max 8 s) with jitter."""
    if headers is not None:
        retry_after = _parse_retry_after(headers)
        if retry_after is not None:
            return retry_after
    base = min(_MAX_BACKOFF, _INITIAL_BACKOFF * (2.0**attempt))
    return base * (1.0 - 0.25 * random.random())


def should_retry(response_status: int, headers: httpx.Headers, error: APIError) -> bool:
    """Retry decision for an error response. ``Daapi-Should-Retry`` wins over status heuristics."""
    if error.outcome in ("unknown", "pending"):
        return False
    flag = (headers.get("daapi-should-retry") or "").strip().lower()
    if flag == "false":
        return False
    if response_status == 429:
        return True
    return response_status >= 500 and flag == "true"


def _retryable_transport_error(exc: httpx.TransportError) -> bool:
    # Configuration errors (bad URL scheme, proxy setup) do not improve on retry.
    return not isinstance(exc, (httpx.UnsupportedProtocol, httpx.ProxyError))


def _attempt_timeout(prep: _Prepared) -> float:
    """This attempt's timeout: the attempt timeout, cut to what is left of the total timeout. Raises
    :class:`APITimeoutError` when the total timeout has ended."""
    if prep.total_deadline is None:
        return prep.attempt_timeout
    remaining = prep.total_deadline - time.monotonic()
    if remaining <= 0:
        raise APITimeoutError(f"{prep.operation_id}: the call's total timeout ended before a response arrived.")
    return min(prep.attempt_timeout, remaining)


def _retry_fits(prep: _Prepared, delay: float) -> bool:
    """Whether a retry after ``delay`` seconds can still start before the total timeout ends."""
    return prep.total_deadline is None or time.monotonic() + delay < prep.total_deadline


def _past_deadline(prep: _Prepared, request: httpx.Request) -> httpx.ReadTimeout:
    return httpx.ReadTimeout("The call's total timeout ended while the response was arriving.", request=request)


def _buffered(response: httpx.Response, chunks: list[bytes], request: httpx.Request) -> httpx.Response:
    """A fully read copy of a streamed response (raw bytes; decoded as usual on access)."""
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=request,
        extensions=response.extensions,
    )


def _bounded_send(http: httpx.Client, prep: _Prepared, timeout: float) -> httpx.Response:
    """One sync attempt under the call's total deadline, enforced cooperatively.

    The request always goes through ``http.send`` (so auth flows, event hooks, cookies, proxies,
    mounts and ``trust_env`` behave exactly as without a deadline). Blocking I/O cannot be canceled
    from another thread, so the sync client bounds each phase instead: connect, read, write and pool
    timeouts are the time left (``timeout`` never exceeds it; how httpx applies phase timeouts
    depends on its transport), the deadline is checked once headers arrive, between body chunks and
    after the body and its cleanup finish, and the response is closed when it passes.

    Limits: one stalled read can overrun the deadline by at most its read budget; a server that
    keeps trickling header bytes can extend an attempt; retries inside an injected transport (for
    example ``HTTPTransport(retries=3)``), synchronous DNS and user auth, hook, transport or cleanup
    code are not bounded; and a transport error that arrives after the deadline surfaces as
    ``APIConnectionError`` rather than ``APITimeoutError``. The SDK never starts its own attempt or
    retry after the deadline. The async client enforces a hard bound.
    """
    deadline = prep.total_deadline
    assert deadline is not None
    request = http.build_request(
        prep.method,
        prep.url,
        params=tuple(prep.params),
        content=prep.content,
        headers=prep.headers,
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
    )
    response = http.send(request, stream=True)
    try:
        if time.monotonic() > deadline:
            raise _past_deadline(prep, request)
        if response.is_stream_consumed:
            return response  # already in memory (for example a mock transport's response)
        chunks: list[bytes] = []
        for chunk in response.iter_raw():
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise _past_deadline(prep, request)
    finally:
        response.close()
    # httpx can keep reading after the last body chunk (chunked trailers, a slow EOF) and closing
    # takes time too: check once more before returning a success (codex re-review round 6).
    if time.monotonic() > deadline:
        raise _past_deadline(prep, request)
    return _buffered(response, chunks, request)


async def _abounded_send(http: httpx.AsyncClient, prep: _Prepared, timeout: float) -> httpx.Response:
    """Async ``_bounded_send``: the whole exchange also runs under ``asyncio.wait_for`` with the time left."""
    deadline = prep.total_deadline
    assert deadline is not None
    request = http.build_request(
        prep.method, prep.url, params=tuple(prep.params), content=prep.content, headers=prep.headers, timeout=timeout
    )

    async def exchange() -> httpx.Response:
        response = await http.send(request, stream=True)
        if response.is_stream_consumed:
            return response  # already in memory (for example a mock transport's prebuilt response)
        chunks: list[bytes] = []
        try:
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise _past_deadline(prep, request)
        finally:
            await response.aclose()
        return _buffered(response, chunks, request)

    try:
        return await asyncio.wait_for(exchange(), timeout=max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError as exc:
        raise _past_deadline(prep, request) from exc


def _connection_error(exc: httpx.TransportError) -> APIConnectionError:
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(f"The request timed out before a response arrived: {exc.__class__.__name__}.")
    return APIConnectionError(f"Could not reach the Desktop Accounting API: {exc.__class__.__name__}: {exc}")


def _pending_request_id(error: APIError) -> Optional[str]:
    """The request to long-poll after ``504 QBD_REQUEST_TIMEOUT``, else None.

    The request was sent and keeps running, whatever its ``outcome``: ``pending`` for a write,
    ``not_applicable`` for a read. Same rule as every SDK: HTTP 504, code ``QBD_REQUEST_TIMEOUT``
    and a non-empty ``details.requestId``.
    """
    if error.status != 504 or error.code != "QBD_REQUEST_TIMEOUT":
        return None
    request_id = error.details.get("requestId")
    return request_id if isinstance(request_id, str) and request_id else None


def _response_text(response: httpx.Response) -> str:
    return response.text


def _xml_result(value: Any) -> str:
    """The qbXML text of an XML call: the response body, or a collected request's ``result``."""
    if isinstance(value, str):
        return value
    if value is None:
        raise DaapiError("The XML request succeeded without a qbXML result.")
    return json.dumps(value)


def _json_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return json.loads(response.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DaapiError(
            f"The API returned a {response.status_code} response that is not valid JSON "
            f"(request {response.headers.get('daapi-request-id', 'unknown')})."
        ) from exc


def _parse_request(data: Any) -> Request:
    from .types import Request

    if not isinstance(data, Mapping):
        raise DaapiError("Expected a request object from the API.")
    return Request.from_wire(data)


def settle_request(
    data: Mapping[str, Any], cast_result: Callable[[Any], T], headers: httpx.Headers
) -> tuple[bool, Any]:
    """Interprets a request resource: ``(True, result)`` when succeeded, raises when it ended in an error,
    ``(False, None)`` while it is still running."""
    status = data.get("status")
    request_id = data.get("id") if isinstance(data.get("id"), str) else None
    if status == "succeeded":
        error = data.get("error")
        if isinstance(error, Mapping):
            # QuickBooks answered, but the API could not map the answer (for example
            # QBD_RESPONSE_UNREADABLE, outcome applied): raise that catalog error, never None.
            code = error.get("httpStatusCode")
            raise error_from_object(error, status=code if isinstance(code, int) else None, headers=headers, body=data)
        if data.get("result") is None and data.get("resultExpired"):
            raise DaapiError(f"Request {request_id} succeeded, but its result is past the retention period.")
        return True, cast_result(data.get("result"))
    if status in ("failed", "canceled", "outcome_unknown"):
        error = data.get("error")
        if isinstance(error, Mapping):
            code = error.get("httpStatusCode")
            raise error_from_object(error, status=code if isinstance(code, int) else None, headers=headers, body=data)
        if status == "outcome_unknown":
            raise OutcomeUnknownError(
                f"Request {request_id} was sent to QuickBooks, but its outcome is unknown.",
                status=None,
                headers=headers,
                error={"type": "OUTCOME_UNKNOWN_ERROR", "requestId": request_id, "outcome": "unknown"},
                body=data,
            )
        raise APIError(
            f"Request {request_id} ended with status {status}.",
            status=None,
            headers=headers,
            error={"requestId": request_id},
            body=data,
        )
    return False, None


class _BaseClient:
    """Configuration shared by the sync and async clients."""

    _CONFIG_ATTRS = (
        "api_key",
        "base_url",
        "end_user_id",
        "timeout",
        "total_timeout",
        "max_retries",
        "server_timeout",
        "default_headers",
        "_logger",
        "_owns_http_client",
    )

    api_key: str
    base_url: str
    end_user_id: Optional[str]
    timeout: float
    total_timeout: Optional[float]
    max_retries: int
    server_timeout: Optional[int]
    default_headers: dict[str, str]
    _logger: logging.Logger
    _owns_http_client: bool

    def _configure(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        end_user_id: Optional[str],
        timeout: float,
        max_retries: int,
        server_timeout: Optional[int],
        logger: Optional[logging.Logger],
        total_timeout: Optional[float] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("DAAPI_SECRET_KEY")
        if not key:
            raise DaapiError(
                "Missing API key. Pass api_key=... or set the DAAPI_SECRET_KEY environment variable "
                "to a secret key from the Desktop Accounting API dashboard."
            )
        if not is_valid_secret_key(key):
            raise DaapiError(
                "The API key is not a valid Desktop Accounting API secret key. Secret keys start with "
                "sk_live_ or sk_test_ followed by 40 characters, with a checksum; check for a typo, extra "
                "whitespace or a truncated value in api_key or DAAPI_SECRET_KEY."
            )
        url = base_url if base_url is not None else os.environ.get("DAAPI_BASE_URL") or DEFAULT_BASE_URL
        if not url.startswith(("https://", "http://")):
            raise DaapiError(f"base_url must be an http(s) URL, got {url!r}.")
        if timeout is None or timeout <= 0:
            raise DaapiError("timeout must be a positive number of seconds.")
        if total_timeout is not None and total_timeout <= 0:
            raise DaapiError("total_timeout must be a positive number of seconds.")
        if max_retries < 0:
            raise DaapiError("max_retries must be zero or more.")
        self.api_key = key
        self.base_url = normalize_base_url(url)
        self.end_user_id = end_user_id
        self.timeout = float(timeout)
        self.total_timeout = float(total_timeout) if total_timeout is not None else None
        self.max_retries = max_retries
        self.server_timeout = server_timeout
        self.default_headers = _checked_headers(default_headers)
        if logger is None:
            _setup_logging_from_env()
        self._logger = logger if logger is not None else _logger

    def __repr__(self) -> str:
        return f"<{type(self).__name__} base_url={self.base_url!r} end_user_id={self.end_user_id!r}>"

    def _copy_with(self, **changes: Any) -> Self:
        clone = object.__new__(type(self))
        for name in (*self._CONFIG_ATTRS, "_http"):
            setattr(clone, name, getattr(self, name))
        clone._owns_http_client = False
        for name, value in changes.items():
            setattr(clone, name, value)
        return clone

    def for_end_user(self, end_user_id: str) -> Self:
        """A client whose QuickBooks Desktop calls default to this end user's company file.

        The new client shares this client's HTTP connection pool and settings.
        """
        if not end_user_id:
            raise DaapiError("end_user_id must be a non-empty string.")
        return self._copy_with(end_user_id=end_user_id)

    def with_options(
        self,
        *,
        end_user_id: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        server_timeout: Optional[int] = None,
        total_timeout: Optional[float] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> Self:
        """A copy of this client with some settings changed. Shares the HTTP connection pool.

        ``default_headers`` replaces the client's default headers.
        """
        changes: dict[str, Any] = {}
        if end_user_id is not None:
            changes["end_user_id"] = end_user_id
        if timeout is not None:
            if timeout <= 0:
                raise DaapiError("timeout must be a positive number of seconds.")
            changes["timeout"] = float(timeout)
        if total_timeout is not None:
            if total_timeout <= 0:
                raise DaapiError("total_timeout must be a positive number of seconds.")
            changes["total_timeout"] = float(total_timeout)
        if default_headers is not None:
            changes["default_headers"] = _checked_headers(default_headers)
        if max_retries is not None:
            if max_retries < 0:
                raise DaapiError("max_retries must be zero or more.")
            changes["max_retries"] = max_retries
        if server_timeout is not None:
            changes["server_timeout"] = server_timeout
        return self._copy_with(**changes)

    def _prepare(
        self,
        op: Op,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        params: Optional[list[tuple[str, str]]] = None,
        body: Optional[Mapping[str, Any]] = None,
        raw_body: Optional[str] = None,
        content_type: str = "application/json",
        accept: str = "application/json",
        end_user_id: Optional[str] = None,
        conductor_end_user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        total_timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        server_timeout: Optional[int] = None,
        queue_ttl: Optional[int] = None,
        respond_async: bool = False,
    ) -> _Prepared:
        started = time.monotonic()
        if conductor_end_user_id is not None:
            if end_user_id is not None and end_user_id != conductor_end_user_id:
                raise DaapiError(
                    f"{op.operation_id}: end_user_id and conductor_end_user_id are both set to different "
                    "values; pass only one."
                )
            end_user_id = conductor_end_user_id
        headers: dict[str, str] = dict(self.default_headers)
        headers["Authorization"] = f"Bearer {self.api_key}"
        headers["User-Agent"] = USER_AGENT
        headers["Accept"] = accept
        if op.end_user:
            user = end_user_id if end_user_id is not None else self.end_user_id
            if not user:
                raise DaapiError(
                    f"{op.operation_id} needs an end user. Pass end_user_id=... to the call, set "
                    "end_user_id on the client, or use client.for_end_user('eu_...')."
                )
            headers["Daapi-End-User-Id"] = user
        if op.write:
            headers["Idempotency-Key"] = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        wait_budget = server_timeout if server_timeout is not None else self.server_timeout
        if op.server_timeout and wait_budget is not None:
            headers["Daapi-Timeout-Seconds"] = str(int(wait_budget))
        if respond_async:
            headers["Prefer"] = "respond-async"
            if op.queue_ttl and queue_ttl is not None:
                headers["Daapi-Queue-Ttl-Seconds"] = str(int(queue_ttl))
        content: Optional[bytes] = None
        if raw_body is not None:
            content = raw_body.encode("utf-8")
            headers["Content-Type"] = content_type
        elif body is not None:
            content = json.dumps(wire_body(body), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempt_timeout = float(timeout) if timeout is not None else self.timeout
        if attempt_timeout <= 0:
            raise DaapiError("timeout must be a positive number of seconds.")
        total = float(total_timeout) if total_timeout is not None else self.total_timeout
        if total is not None and total <= 0:
            raise DaapiError("total_timeout must be a positive number of seconds.")
        retries = max_retries if max_retries is not None else self.max_retries
        return _Prepared(
            method=method,
            url=self.base_url + path,
            params=params if params is not None else (query_items(query) if query else []),
            content=content,
            headers=headers,
            attempt_timeout=attempt_timeout,
            max_retries=max(0, retries),
            deadline=started + (total if total is not None else attempt_timeout),
            operation_id=op.operation_id,
            total_deadline=started + total if total is not None else None,
        )

    def _prepare_poll(
        self, request_id: str, wait_seconds: Optional[int], deadline: Optional[float] = None
    ) -> _Prepared:
        params = [("waitSeconds", str(wait_seconds))] if wait_seconds is not None else []
        prep = self._prepare(
            _REQUESTS_RETRIEVE,
            "GET",
            f"/v1/requests/{request_id}",
            params=params,
            timeout=(wait_seconds or 0) + 10.0,
        )
        # A long poll, its retries and their backoff stay inside the caller's deadline (codex
        # review #15); a single retrieve has no deadline of its own.
        return prep._replace(total_deadline=deadline)

    def _log_response(self, prep: _Prepared, response: httpx.Response, attempt: int) -> None:
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "%s %s -> %s (%s, attempt %d)",
                prep.method,
                httpx.URL(prep.url).path,
                response.status_code,
                response.headers.get("daapi-request-id", "no request id"),
                attempt + 1,
            )

    def _log_retry(self, prep: _Prepared, reason: str, delay: float, attempt: int) -> None:
        self._logger.info(
            "Retrying %s (%s) in %.2f s after %s (retry %d of %d)",
            prep.operation_id,
            prep.method,
            delay,
            reason,
            attempt + 1,
            prep.max_retries,
        )


def _checked_headers(headers: Optional[Mapping[str, str]]) -> dict[str, str]:
    """Default headers without the ones the SDK manages."""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise DaapiError("default_headers must map header names to string values.")
        if name.lower() not in _MANAGED_HEADERS:
            out[name] = value
    return out


def _poll_wait_seconds(deadline: float) -> Optional[int]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return max(1, min(MAX_POLL_WAIT_SECONDS, math.ceil(remaining)))


def _pending_error(
    request_id: str,
    last: Optional[Request],
    *,
    timeout_error: Optional[APIError] = None,
    idempotency_key: Optional[str] = None,
    poll_error: Optional[DaapiError] = None,
) -> RequestPendingError:
    status = f" (status {last.status})" if last is not None else ""
    why = f"checking it failed ({poll_error})" if poll_error is not None else "the call's time budget ended"
    resend = f" or resend it only with Idempotency-Key {idempotency_key}" if idempotency_key else ""
    error = RequestPendingError(
        f"Request {request_id} is still running in QuickBooks{status}: {why}. It was not resubmitted; check it "
        f"later with client.requests.retrieve({request_id!r}, wait_seconds=60){resend}.",
        request_id=request_id,
        request=last,
        timeout_error=timeout_error,
        poll_error=poll_error,
    )
    error.idempotency_key = idempotency_key
    if poll_error is None and timeout_error is not None:
        error.__cause__ = timeout_error
    return error


E_ = TypeVar("E_", bound=DaapiError)


def _with_key(error: E_, key: Optional[str]) -> E_:
    """Attach the write's Idempotency-Key to an error raised for it (kept if already set)."""
    if key is not None and error.idempotency_key is None:
        error.idempotency_key = key
    return error


class SyncAPIClient(_BaseClient):
    """Synchronous HTTP core (``httpx.Client``)."""

    _http: httpx.Client

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        end_user_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        server_timeout: Optional[int] = None,
        http_client: Optional[httpx.Client] = None,
        logger: Optional[logging.Logger] = None,
        total_timeout: Optional[float] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._configure(
            api_key=api_key,
            base_url=base_url,
            end_user_id=end_user_id,
            timeout=timeout,
            max_retries=max_retries,
            server_timeout=server_timeout,
            logger=logger,
            total_timeout=total_timeout,
            default_headers=default_headers,
        )
        if http_client is not None and not isinstance(http_client, httpx.Client):
            raise DaapiError(
                "http_client must be an httpx.Client (use AsyncDesktopAccountingApi for httpx.AsyncClient)."
            )
        self._owns_http_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(follow_redirects=False)

    def close(self) -> None:
        """Closes the HTTP connection pool if this client created it."""
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, prep: _Prepared) -> httpx.Response:
        attempt = 0
        while True:
            try:
                if prep.total_deadline is not None:
                    response = _bounded_send(self._http, prep, _attempt_timeout(prep))
                else:
                    response = self._http.request(
                        prep.method,
                        prep.url,
                        params=tuple(prep.params),
                        content=prep.content,
                        headers=prep.headers,
                        timeout=_attempt_timeout(prep),
                    )
            except httpx.TransportError as exc:
                delay = backoff_delay(attempt)
                if attempt < prep.max_retries and _retryable_transport_error(exc) and _retry_fits(prep, delay):
                    self._log_retry(prep, exc.__class__.__name__, delay, attempt)
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise _connection_error(exc) from exc
            self._log_response(prep, response, attempt)
            if 200 <= response.status_code < 300:
                return response
            error = error_from_response(response.status_code, response.headers, response.content)
            # A request still running after the server timeout is long-polled, never resent.
            if (
                _pending_request_id(error) is None
                and attempt < prep.max_retries
                and should_retry(response.status_code, response.headers, error)
            ):
                delay = backoff_delay(attempt, response.headers)
                if _retry_fits(prep, delay):
                    self._log_retry(prep, f"HTTP {response.status_code}", delay, attempt)
                    time.sleep(delay)
                    attempt += 1
                    continue
            raise error

    def _execute(
        self,
        prep: _Prepared,
        cast_result: Callable[[Any], T],
        read: Callable[[httpx.Response], Any] = _json_body,
    ) -> tuple[T, httpx.Response, Optional[str]]:
        """Sends a sync-mode call. After ``504 QBD_REQUEST_TIMEOUT`` it long-polls the request instead.

        Returns the value, the final HTTP response and the ID of the request that produced the
        value: the response's ``Daapi-Request-Id``, or after a long poll the timed-out request's ID
        (the poll's own ID stays in the response headers).
        ``read`` turns a direct 2xx answer into the value for ``cast_result`` (JSON, or text for XML).
        Every error raised here, including one from polling (for example ``OutcomeUnknownError``),
        carries the write's Idempotency-Key."""
        key = prep.headers.get("Idempotency-Key")
        try:
            try:
                response = self._send(prep)
            except APIError as error:
                request_id = _pending_request_id(error)
                if request_id is None:
                    raise
                value, polled = self._poll(
                    request_id, cast_result, prep.deadline, timeout_error=error, idempotency_key=key
                )
                return value, polled, request_id
            return cast_result(read(response)), response, response.headers.get("daapi-request-id")
        except DaapiError as error:
            _with_key(error, key)
            raise

    def _poll(
        self,
        request_id: str,
        cast_result: Callable[[Any], T],
        deadline: float,
        *,
        timeout_error: Optional[APIError] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[T, httpx.Response]:
        """Long-polls ``GET /v1/requests/{id}`` until the request ends or ``deadline`` passes.

        A settled request returns its result or raises its own typed error. Anything else that ends
        the wait (the deadline, or a poll that failed) raises :class:`RequestPendingError`: a failed
        poll says nothing about the write, so its own retryable error is never raised."""
        last: Optional[Request] = None
        while True:
            wait = _poll_wait_seconds(deadline)
            if wait is None:
                raise _pending_error(request_id, last, timeout_error=timeout_error, idempotency_key=idempotency_key)
            try:
                response = self._send(self._prepare_poll(request_id, wait, deadline))
                data = _json_body(response)
                last = _parse_request(data)
            except DaapiError as poll_error:
                raise _pending_error(
                    request_id,
                    last,
                    timeout_error=timeout_error,
                    idempotency_key=idempotency_key,
                    poll_error=poll_error,
                ) from poll_error
            # httpx timeouts are per I/O step, so a slow body can end after the deadline: an answer
            # that arrives late is not returned, settled or not (codex re-review #15).
            if time.monotonic() > deadline:
                raise _pending_error(request_id, last, timeout_error=timeout_error, idempotency_key=idempotency_key)
            done, value = settle_request(data, cast_result, response.headers)
            if done:
                return cast(T, value), response

    def _request(self, op: Op, method: str, path: str, *, cast_to: Callable[[Any], T], **kwargs: Any) -> T:
        raw = _raw_response_mode.get()
        if raw:
            _raw_response_mode.set(False)
        prep = self._prepare(op, method, path, **kwargs)
        value, response, request_id = self._execute(prep, cast_to)
        if raw:
            return cast(T, RawResponse(response, value, prep.headers.get("Idempotency-Key"), request_id))
        return value

    def _request_text(self, op: Op, method: str, path: str, *, xml: str, **kwargs: Any) -> str:
        raw = _raw_response_mode.get()
        if raw:
            _raw_response_mode.set(False)
        prep = self._prepare(
            op, method, path, raw_body=xml, content_type="application/xml", accept="application/xml", **kwargs
        )
        # A 504 QBD_REQUEST_TIMEOUT is long-polled exactly like a JSON call; the collected result is
        # the qbXML response text.
        text, response, request_id = self._execute(prep, _xml_result, read=_response_text)
        if raw:
            return cast(str, RawResponse(response, text, prep.headers.get("Idempotency-Key"), request_id))
        return text

    def _enqueue(
        self, op: Op, method: str, path: str, *, cast_to: Callable[[Any], T], **kwargs: Any
    ) -> RequestHandle[T]:
        from ._handles import RequestHandle

        prep = self._prepare(op, method, path, respond_async=True, **kwargs)
        key = prep.headers.get("Idempotency-Key")
        try:
            response = self._send(prep)
            data = _json_body(response)
        except DaapiError as error:
            _with_key(error, key)
            raise
        if response.status_code == 202:
            return RequestHandle(self, _parse_request(data), cast_to, key)
        return RequestHandle._completed(self, response.headers.get("daapi-request-id", ""), cast_to(data), cast_to)

    def _paginate(
        self,
        op: Op,
        path: str,
        *,
        query: Mapping[str, Any],
        parse_item: Callable[[Mapping[str, Any]], T],
        **kwargs: Any,
    ) -> CursorPager[T]:
        from ._pagination import CursorPager

        return CursorPager(self, op, path, query, parse_item, kwargs)

    def _fetch_page_data(
        self, op: Op, path: str, params: list[tuple[str, str]], options: Mapping[str, Any]
    ) -> tuple[Any, httpx.Response, Optional[str]]:
        prep = self._prepare(op, "GET", path, params=params, **options)
        return self._execute(prep, _identity)

    def _retrieve_request(self, request_id: str) -> tuple[Mapping[str, Any], httpx.Response]:
        response = self._send(self._prepare_poll(request_id, None))
        data = _json_body(response)
        if not isinstance(data, Mapping):
            raise DaapiError("Expected a request object from the API.")
        return data, response


class AsyncAPIClient(_BaseClient):
    """Asynchronous HTTP core (``httpx.AsyncClient``)."""

    _http: httpx.AsyncClient

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        end_user_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        server_timeout: Optional[int] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        logger: Optional[logging.Logger] = None,
        total_timeout: Optional[float] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._configure(
            api_key=api_key,
            base_url=base_url,
            end_user_id=end_user_id,
            timeout=timeout,
            max_retries=max_retries,
            server_timeout=server_timeout,
            logger=logger,
            total_timeout=total_timeout,
            default_headers=default_headers,
        )
        if http_client is not None and not isinstance(http_client, httpx.AsyncClient):
            raise DaapiError("http_client must be an httpx.AsyncClient (use DesktopAccountingApi for httpx.Client).")
        self._owns_http_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(follow_redirects=False)

    async def close(self) -> None:
        """Closes the HTTP connection pool if this client created it."""
        if self._owns_http_client:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _send(self, prep: _Prepared) -> httpx.Response:
        attempt = 0
        while True:
            try:
                if prep.total_deadline is not None:
                    response = await _abounded_send(self._http, prep, _attempt_timeout(prep))
                else:
                    response = await self._http.request(
                        prep.method,
                        prep.url,
                        params=tuple(prep.params),
                        content=prep.content,
                        headers=prep.headers,
                        timeout=_attempt_timeout(prep),
                    )
            except httpx.TransportError as exc:
                delay = backoff_delay(attempt)
                if attempt < prep.max_retries and _retryable_transport_error(exc) and _retry_fits(prep, delay):
                    self._log_retry(prep, exc.__class__.__name__, delay, attempt)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise _connection_error(exc) from exc
            self._log_response(prep, response, attempt)
            if 200 <= response.status_code < 300:
                return response
            error = error_from_response(response.status_code, response.headers, response.content)
            # A request still running after the server timeout is long-polled, never resent.
            if (
                _pending_request_id(error) is None
                and attempt < prep.max_retries
                and should_retry(response.status_code, response.headers, error)
            ):
                delay = backoff_delay(attempt, response.headers)
                if _retry_fits(prep, delay):
                    self._log_retry(prep, f"HTTP {response.status_code}", delay, attempt)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
            raise error

    async def _execute(
        self,
        prep: _Prepared,
        cast_result: Callable[[Any], T],
        read: Callable[[httpx.Response], Any] = _json_body,
    ) -> tuple[T, httpx.Response, Optional[str]]:
        """Sends a sync-mode call. After ``504 QBD_REQUEST_TIMEOUT`` it long-polls the request instead.

        Returns the value, the final HTTP response and the ID of the request that produced the
        value: the response's ``Daapi-Request-Id``, or after a long poll the timed-out request's ID
        (the poll's own ID stays in the response headers).
        ``read`` turns a direct 2xx answer into the value for ``cast_result`` (JSON, or text for XML).
        Every error raised here, including one from polling (for example ``OutcomeUnknownError``),
        carries the write's Idempotency-Key."""
        key = prep.headers.get("Idempotency-Key")
        try:
            try:
                response = await self._send(prep)
            except APIError as error:
                request_id = _pending_request_id(error)
                if request_id is None:
                    raise
                value, polled = await self._poll(
                    request_id, cast_result, prep.deadline, timeout_error=error, idempotency_key=key
                )
                return value, polled, request_id
            return cast_result(read(response)), response, response.headers.get("daapi-request-id")
        except DaapiError as error:
            _with_key(error, key)
            raise

    async def _poll(
        self,
        request_id: str,
        cast_result: Callable[[Any], T],
        deadline: float,
        *,
        timeout_error: Optional[APIError] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[T, httpx.Response]:
        """Long-polls ``GET /v1/requests/{id}`` until the request ends or ``deadline`` passes (see the
        synchronous client: a failed poll raises :class:`RequestPendingError`)."""
        last: Optional[Request] = None
        while True:
            wait = _poll_wait_seconds(deadline)
            if wait is None:
                raise _pending_error(request_id, last, timeout_error=timeout_error, idempotency_key=idempotency_key)
            try:
                response = await self._send(self._prepare_poll(request_id, wait, deadline))
                data = _json_body(response)
                last = _parse_request(data)
            except DaapiError as poll_error:
                raise _pending_error(
                    request_id,
                    last,
                    timeout_error=timeout_error,
                    idempotency_key=idempotency_key,
                    poll_error=poll_error,
                ) from poll_error
            # httpx timeouts are per I/O step, so a slow body can end after the deadline: an answer
            # that arrives late is not returned, settled or not (codex re-review #15).
            if time.monotonic() > deadline:
                raise _pending_error(request_id, last, timeout_error=timeout_error, idempotency_key=idempotency_key)
            done, value = settle_request(data, cast_result, response.headers)
            if done:
                return cast(T, value), response

    async def _request(self, op: Op, method: str, path: str, *, cast_to: Callable[[Any], T], **kwargs: Any) -> T:
        raw = _raw_response_mode.get()
        if raw:
            _raw_response_mode.set(False)
        prep = self._prepare(op, method, path, **kwargs)
        value, response, request_id = await self._execute(prep, cast_to)
        if raw:
            return cast(T, RawResponse(response, value, prep.headers.get("Idempotency-Key"), request_id))
        return value

    async def _request_text(self, op: Op, method: str, path: str, *, xml: str, **kwargs: Any) -> str:
        raw = _raw_response_mode.get()
        if raw:
            _raw_response_mode.set(False)
        prep = self._prepare(
            op, method, path, raw_body=xml, content_type="application/xml", accept="application/xml", **kwargs
        )
        # A 504 QBD_REQUEST_TIMEOUT is long-polled exactly like a JSON call; the collected result is
        # the qbXML response text.
        text, response, request_id = await self._execute(prep, _xml_result, read=_response_text)
        if raw:
            return cast(str, RawResponse(response, text, prep.headers.get("Idempotency-Key"), request_id))
        return text

    async def _enqueue(
        self, op: Op, method: str, path: str, *, cast_to: Callable[[Any], T], **kwargs: Any
    ) -> AsyncRequestHandle[T]:
        from ._handles import AsyncRequestHandle

        prep = self._prepare(op, method, path, respond_async=True, **kwargs)
        key = prep.headers.get("Idempotency-Key")
        try:
            response = await self._send(prep)
            data = _json_body(response)
        except DaapiError as error:
            _with_key(error, key)
            raise
        if response.status_code == 202:
            return AsyncRequestHandle(self, _parse_request(data), cast_to, key)
        return AsyncRequestHandle._completed(self, response.headers.get("daapi-request-id", ""), cast_to(data), cast_to)

    def _paginate(
        self,
        op: Op,
        path: str,
        *,
        query: Mapping[str, Any],
        parse_item: Callable[[Mapping[str, Any]], T],
        **kwargs: Any,
    ) -> AsyncCursorPager[T]:
        from ._pagination import AsyncCursorPager

        return AsyncCursorPager(self, op, path, query, parse_item, kwargs)

    async def _fetch_page_data(
        self, op: Op, path: str, params: list[tuple[str, str]], options: Mapping[str, Any]
    ) -> tuple[Any, httpx.Response, Optional[str]]:
        prep = self._prepare(op, "GET", path, params=params, **options)
        return await self._execute(prep, _identity)

    async def _retrieve_request(self, request_id: str) -> tuple[Mapping[str, Any], httpx.Response]:
        response = await self._send(self._prepare_poll(request_id, None))
        data = _json_body(response)
        if not isinstance(data, Mapping):
            raise DaapiError("Expected a request object from the API.")
        return data, response
