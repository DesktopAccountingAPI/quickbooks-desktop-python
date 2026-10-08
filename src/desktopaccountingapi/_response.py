"""Raw response access: the parsed result plus status, headers and request ID."""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Awaitable
from typing import Callable, Generic, Optional, TypeVar, cast

import httpx
from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")

raw_response_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("daapi_raw_response", default=False)


class RawResponse(Generic[T]):
    """A parsed result together with the HTTP response it came from.

    Returned by the ``with_raw_response`` accessors::

        raw = client.qbd.customers.with_raw_response.retrieve("80000001-1700000000")
        raw.status_code, raw.request_id, raw.headers["daapi-warnings"]
        customer = raw.parse()
    """

    def __init__(self, http_response: httpx.Response, parsed: T, idempotency_key: Optional[str] = None) -> None:
        self.http_response = http_response
        """The underlying ``httpx.Response`` (body already read)."""
        self._parsed = parsed
        self._idempotency_key = idempotency_key

    @property
    def status_code(self) -> int:
        """HTTP status code."""
        return self.http_response.status_code

    @property
    def headers(self) -> httpx.Headers:
        """Response headers (case-insensitive)."""
        return self.http_response.headers

    @property
    def request_id(self) -> Optional[str]:
        """The ``Daapi-Request-Id`` response header."""
        value: Optional[str] = self.http_response.headers.get("daapi-request-id")
        return value

    @property
    def idempotency_key(self) -> Optional[str]:
        """The ``Idempotency-Key`` the SDK sent for a write (generated unless you passed one), else ``None``."""
        return self._idempotency_key

    @property
    def warnings(self) -> int:
        """``Daapi-Warnings``: the number of QuickBooks warnings recorded on the request."""
        try:
            return int(self.http_response.headers.get("daapi-warnings", "0"))
        except ValueError:
            return 0

    @property
    def idempotent_replayed(self) -> bool:
        """True when ``Daapi-Idempotent-Replayed: true`` (the body is a stored replay)."""
        value: str = self.http_response.headers.get("daapi-idempotent-replayed", "")
        return value.lower() == "true"

    def parse(self) -> T:
        """The typed result."""
        return self._parsed

    def __repr__(self) -> str:
        return f"<RawResponse status_code={self.status_code} request_id={self.request_id!r}>"


def to_raw_response_wrapper(func: Callable[P, T]) -> Callable[P, RawResponse[T]]:
    """Wraps a resource method so that it returns ``RawResponse[T]`` instead of ``T``."""

    @functools.wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> RawResponse[T]:
        token = raw_response_mode.set(True)
        try:
            return cast("RawResponse[T]", func(*args, **kwargs))
        finally:
            raw_response_mode.reset(token)

    return wrapped


def async_to_raw_response_wrapper(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[RawResponse[T]]]:
    """Wraps an async resource method so that it returns ``RawResponse[T]`` instead of ``T``."""

    @functools.wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> RawResponse[T]:
        token = raw_response_mode.set(True)
        try:
            return cast("RawResponse[T]", await func(*args, **kwargs))
        finally:
            raw_response_mode.reset(token)

    return wrapped
