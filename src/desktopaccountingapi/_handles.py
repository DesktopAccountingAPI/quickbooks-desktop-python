"""Handles for requests started in async mode (``Prefer: respond-async``)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar, cast

from ._base_client import _parse_request, _pending_error, _with_key, settle_request
from ._errors import DaapiError

if TYPE_CHECKING:
    from ._base_client import AsyncAPIClient, SyncAPIClient
    from .types import Request

T = TypeVar("T")

_UNSET: Any = object()


class RequestHandle(Generic[T]):
    """A request queued in async mode, returned by the ``enqueue`` accessors.

    ``client.qbd.invoices.enqueue.create(...)`` returns at once with the queued request.
    ``wait()`` long-polls until QuickBooks has processed it and returns the typed result.
    """

    def __init__(
        self,
        client: SyncAPIClient,
        request: Optional[Request],
        cast_result: Callable[[Any], T],
        idempotency_key: Optional[str] = None,
    ) -> None:
        self._client = client
        self._cast = cast_result
        self._value: Any = _UNSET
        self.request = request
        """The request snapshot from the ``202 Accepted`` response (updated by ``status()``)."""
        self.id: str = request.id if request is not None else ""
        """The ``req_...`` ID of the request."""
        self.idempotency_key = idempotency_key
        """The ``Idempotency-Key`` sent with the write that created this request, else ``None``."""

    @classmethod
    def _completed(
        cls, client: SyncAPIClient, request_id: str, value: T, cast_result: Callable[[Any], T]
    ) -> RequestHandle[T]:
        handle = cls(client, None, cast_result)
        handle.id = request_id
        handle._value = value
        return handle

    def status(self) -> Request:
        """Fetches the current request resource once (no waiting)."""
        data, _ = self._client._retrieve_request(self.id)
        self.request = _parse_request(data)
        return self.request

    def wait(self, timeout: Optional[float] = None) -> T:
        """Long-polls until the request ends and returns its typed result.

        Raises the typed API error if the request failed, was canceled or has an unknown outcome,
        and :class:`RequestPendingError` if it is still running after ``timeout`` seconds
        (default: the client's ``total_timeout``, else its ``timeout``) or a poll fails.
        """
        if self._value is not _UNSET:
            return cast(T, self._value)
        budget = timeout if timeout is not None else (self._client.total_timeout or self._client.timeout)
        try:
            value, _ = self._client._poll(
                self.id, self._cast, time.monotonic() + budget, idempotency_key=self.idempotency_key
            )
        except DaapiError as error:
            # The request's own typed errors (failed, canceled, outcome_unknown) carry the key too.
            _with_key(error, self.idempotency_key)
            raise
        return value

    def result(self) -> T:
        """Checks the request once: the typed result if it succeeded, the typed error if it failed,
        :class:`RequestPendingError` while it is still running."""
        if self._value is not _UNSET:
            return cast(T, self._value)
        try:
            data, response = self._client._retrieve_request(self.id)
            self.request = _parse_request(data)
            done, value = settle_request(data, self._cast, response.headers)
        except DaapiError as error:
            _with_key(error, self.idempotency_key)
            raise
        if not done:
            raise _pending_error(self.id, self.request, idempotency_key=self.idempotency_key)
        return cast(T, value)

    def __repr__(self) -> str:
        status = self.request.status if self.request is not None else "completed"
        return f"<RequestHandle id={self.id!r} status={status!r}>"


class AsyncRequestHandle(Generic[T]):
    """A request queued in async mode, returned by the async client's ``enqueue`` accessors."""

    def __init__(
        self,
        client: AsyncAPIClient,
        request: Optional[Request],
        cast_result: Callable[[Any], T],
        idempotency_key: Optional[str] = None,
    ) -> None:
        self._client = client
        self._cast = cast_result
        self._value: Any = _UNSET
        self.request = request
        """The request snapshot from the ``202 Accepted`` response (updated by ``status()``)."""
        self.id: str = request.id if request is not None else ""
        """The ``req_...`` ID of the request."""
        self.idempotency_key = idempotency_key
        """The ``Idempotency-Key`` sent with the write that created this request, else ``None``."""

    @classmethod
    def _completed(
        cls, client: AsyncAPIClient, request_id: str, value: T, cast_result: Callable[[Any], T]
    ) -> AsyncRequestHandle[T]:
        handle = cls(client, None, cast_result)
        handle.id = request_id
        handle._value = value
        return handle

    async def status(self) -> Request:
        """Fetches the current request resource once (no waiting)."""
        data, _ = await self._client._retrieve_request(self.id)
        self.request = _parse_request(data)
        return self.request

    async def wait(self, timeout: Optional[float] = None) -> T:
        """Long-polls until the request ends and returns its typed result (see ``RequestHandle.wait``)."""
        if self._value is not _UNSET:
            return cast(T, self._value)
        budget = timeout if timeout is not None else (self._client.total_timeout or self._client.timeout)
        try:
            value, _ = await self._client._poll(
                self.id, self._cast, time.monotonic() + budget, idempotency_key=self.idempotency_key
            )
        except DaapiError as error:
            # The request's own typed errors (failed, canceled, outcome_unknown) carry the key too.
            _with_key(error, self.idempotency_key)
            raise
        return value

    async def result(self) -> T:
        """Checks the request once (see ``RequestHandle.result``)."""
        if self._value is not _UNSET:
            return cast(T, self._value)
        try:
            data, response = await self._client._retrieve_request(self.id)
            self.request = _parse_request(data)
            done, value = settle_request(data, self._cast, response.headers)
        except DaapiError as error:
            _with_key(error, self.idempotency_key)
            raise
        if not done:
            raise _pending_error(self.id, self.request, idempotency_key=self.idempotency_key)
        return cast(T, value)

    def __repr__(self) -> str:
        status = self.request.status if self.request is not None else "completed"
        return f"<AsyncRequestHandle id={self.id!r} status={status!r}>"
