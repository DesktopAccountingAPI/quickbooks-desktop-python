"""Exception hierarchy.

``DaapiError`` is the base of everything this SDK raises. ``APIError`` (and one subclass per
error ``type``) carries the API's error object field by field. See the README section "Errors".

Conductor-compatible names (``conductor-py``): ``ConductorError`` is ``DaapiError``, and every
error from an HTTP response is also an ``APIStatusError`` and, for its status, a
``BadRequestError`` (400), ``NotFoundError`` (404), ``ConflictError`` (409),
``UnprocessableEntityError`` (422) or ``InternalServerError`` (5xx). The SDK raises a subclass of
both the class for the error ``type`` and the status class, so ``except NotFoundError:`` and
``except InvalidRequestError:`` both catch a 404 ``INVALID_REQUEST_ERROR``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

import httpx

if TYPE_CHECKING:
    from .types import Request

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
    "ApiConnectionError",
    "ApiError",
    "ApiTimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "BillingError",
    "ConductorError",
    "ConflictError",
    "CursorExpiredError",
    "DaapiError",
    "ErrorFix",
    "IntegrationConnectionError",
    "IntegrationError",
    "InternalError",
    "InternalServerError",
    "InvalidRequestError",
    "NotFoundError",
    "OutcomeUnknownError",
    "PermissionDeniedError",
    "RateLimitError",
    "RequestPendingError",
    "UnprocessableEntityError",
    "WebhookVerificationError",
]


class DaapiError(Exception):
    """Base class of every error raised by this SDK.

    Raised directly for client-side problems found before any request is sent: a missing or
    malformed API key, a QuickBooks Desktop call without an end user, an invalid argument.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ErrorFix:
    """One suggested fix: who should act (``developer``, ``end_user`` or ``support``) and what to do."""

    __slots__ = ("action", "actor")

    def __init__(self, actor: str, action: str) -> None:
        self.actor = actor
        self.action = action

    def __repr__(self) -> str:
        return f"ErrorFix(actor={self.actor!r}, action={self.action!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ErrorFix) and (other.actor, other.action) == (self.actor, self.action)

    __hash__ = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, str]:
        return {"actor": self.actor, "action": self.action}


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


class APIError(DaapiError):
    """The API answered with an error status (any non-2xx response).

    Every field of the API's error object is exposed directly. Fields are ``None`` when the
    response did not carry a JSON error object (for example an HTML page from a proxy).
    """

    status: Optional[int]
    """HTTP status of the response (``None`` for errors read from a request resource without one)."""
    type: Optional[str]
    """Error type, for example ``INTEGRATION_CONNECTION_ERROR`` (see :class:`ErrorType`)."""
    code: Optional[str]
    """Stable catalog code, for example ``QBD_MODAL_DIALOG_OPEN`` (see :class:`ErrorCode`)."""
    user_facing_message: Optional[str]
    """A message that is safe to show to your end user."""
    http_status_code: Optional[int]
    """``httpStatusCode`` from the error object."""
    integration_code: Optional[str]
    """Native QuickBooks code: qbXML ``statusCode``, HRESULT or QBWC code."""
    request_id: Optional[str]
    """The ``req_...`` ID of the request (body ``requestId``, else the ``Daapi-Request-Id`` header)."""
    cause: Optional[str]
    """Why the error happens."""
    fixes: list[ErrorFix]
    """Suggested fixes, each with an ``actor`` and an ``action``."""
    docs_url: Optional[str]
    """Documentation for this error code."""
    retryable: Optional[bool]
    """Repeating the identical request (same idempotency key) can succeed without changes."""
    outcome: Optional[str]
    """Write outcome: ``applied``, ``not_applied``, ``pending``, ``unknown`` or ``not_applicable``."""
    param: Optional[str]
    """Request field path the error refers to, for example ``lines[2].amount``."""
    details: dict[str, Any]
    """Code-specific details."""
    headers: httpx.Headers
    """Response headers."""
    body: Any
    """The parsed JSON response body, or the response text when it was not JSON."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int],
        headers: Optional[httpx.Headers] = None,
        error: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        e: Mapping[str, Any] = error or {}
        self.status = status
        self.headers = headers if headers is not None else httpx.Headers()
        self.body = body
        self.type = _str_or_none(e.get("type"))
        self.code = _str_or_none(e.get("code"))
        self.user_facing_message = _str_or_none(e.get("userFacingMessage"))
        status_code = e.get("httpStatusCode")
        self.http_status_code = status_code if isinstance(status_code, int) else None
        self.integration_code = _str_or_none(e.get("integrationCode"))
        self.request_id = _str_or_none(e.get("requestId")) or self.headers.get("daapi-request-id")
        self.cause = _str_or_none(e.get("cause"))
        fixes = e.get("fixes")
        self.fixes = [
            ErrorFix(str(f.get("actor", "")), str(f.get("action", "")))
            for f in (fixes if isinstance(fixes, list) else [])
            if isinstance(f, Mapping)
        ]
        self.docs_url = _str_or_none(e.get("docsUrl"))
        retryable = e.get("retryable")
        self.retryable = retryable if isinstance(retryable, bool) else None
        self.outcome = _str_or_none(e.get("outcome"))
        self.param = _str_or_none(e.get("param"))
        details = e.get("details")
        self.details = dict(details) if isinstance(details, Mapping) else {}

    @property
    def status_code(self) -> Optional[int]:
        """Alias of :attr:`status` (Conductor's name)."""
        return self.status

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"[{self.code}]")
        if self.request_id:
            parts.append(f"(request {self.request_id})")
        return " ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, type={self.type!r}, code={self.code!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class APIStatusError(APIError):
    """Any error that came with an HTTP status (Conductor's name). Raised errors combine it with their type class."""


class BadRequestError(APIStatusError):
    """HTTP 400 (Conductor's name). Raised errors combine it with their type class."""


class NotFoundError(APIStatusError):
    """HTTP 404 (Conductor's name). Raised errors combine it with their type class."""


class ConflictError(APIStatusError):
    """HTTP 409 (Conductor's name). Raised errors combine it with their type class."""


class UnprocessableEntityError(APIStatusError):
    """HTTP 422 (Conductor's name). Raised errors combine it with their type class."""


class InternalServerError(APIStatusError):
    """HTTP 500 or more (Conductor's name). Raised errors combine it with their type class."""


class InvalidRequestError(APIError):
    """``INVALID_REQUEST_ERROR``: the request is invalid (bad parameter, unknown field, bad cursor...)."""


class AuthenticationError(APIError):
    """``AUTHENTICATION_ERROR``: the API key is missing, unknown or revoked."""


class PermissionDeniedError(APIError):
    """``PERMISSION_ERROR``: the key may not perform this operation."""


class BillingError(APIError):
    """``BILLING_ERROR``: the project's billing state blocks production requests."""


class RateLimitError(APIError):
    """``RATE_LIMIT_ERROR``: too many requests. Retried automatically."""


class IntegrationConnectionError(APIError):
    """``INTEGRATION_CONNECTION_ERROR``: a problem in the end user's QuickBooks Desktop environment."""


class IntegrationError(APIError):
    """``INTEGRATION_ERROR``: QuickBooks Desktop rejected the request."""


class OutcomeUnknownError(APIError):
    """``OUTCOME_UNKNOWN_ERROR``: a write reached QuickBooks but its result could not be confirmed.

    Never retried automatically. Check the request resource or look the record up by
    ``externalId``/``refNumber`` before trying again.
    """


class InternalError(APIError):
    """``INTERNAL_ERROR``: a problem on the Desktop Accounting API side."""


class CursorExpiredError(InvalidRequestError):
    """``410 CURSOR_EXPIRED``: the QuickBooks iterator behind a list cursor is gone.

    The SDK never restarts the list silently, because data may have changed. To resume, start
    a new list call with an ``updated_after`` watermark (for example ``last_updated_at``) and
    skip IDs you already processed.
    """

    items_yielded: int
    """Items the iterator yielded before the cursor expired."""
    pages_served: int
    """Pages the server served (``details.pagesServed``), else the pages the iterator delivered."""
    last_id: Optional[str]
    """``id`` of the last item yielded, if any."""
    last_updated_at: Optional[str]
    """``updatedAt`` of the last item yielded, exactly as received, if any."""
    reason: Optional[str]
    """``details.reason``: ``idle_timeout``, ``session_ended``, ``quickbooks_restarted`` or ``evicted``."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int],
        headers: Optional[httpx.Headers] = None,
        error: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message, status=status, headers=headers, error=error, body=body)
        self.items_yielded = 0
        served = self.details.get("pagesServed")
        self.pages_served = served if isinstance(served, int) else 0
        self.last_id = None
        self.last_updated_at = None
        self.reason = _str_or_none(self.details.get("reason"))

    def _set_progress(
        self, *, items_yielded: int, pages_delivered: int, last_id: Optional[str], last_updated_at: Optional[str]
    ) -> None:
        self.items_yielded = items_yielded
        if not isinstance(self.details.get("pagesServed"), int):
            self.pages_served = pages_delivered
        self.last_id = last_id
        self.last_updated_at = last_updated_at


class APIConnectionError(DaapiError):
    """No response was received (connection failure, dropped connection), after all retries."""


class APITimeoutError(APIConnectionError):
    """The client-side timeout expired before a response arrived, after all retries."""


class RequestPendingError(DaapiError):
    """The request is still running in QuickBooks when the call's time budget ended.

    The SDK never resubmits it. Wait for it with ``client.requests.retrieve(request_id,
    wait_seconds=...)`` or a ``request.*`` webhook.
    """

    def __init__(self, message: str, *, request_id: str, request: Optional[Request] = None) -> None:
        super().__init__(message)
        self.request_id = request_id
        """The ``req_...`` ID of the pending request."""
        self.request = request
        """The last request snapshot seen, if any."""


class WebhookVerificationError(DaapiError):
    """A webhook's signature, timestamp or headers failed verification."""


# Aliases with the casing other Desktop Accounting API SDKs use.
ApiError = APIError
ApiConnectionError = APIConnectionError
ApiTimeoutError = APITimeoutError
# Conductor's name for the base class.
ConductorError = DaapiError

_BY_TYPE: dict[str, type[APIError]] = {
    "INVALID_REQUEST_ERROR": InvalidRequestError,
    "AUTHENTICATION_ERROR": AuthenticationError,
    "PERMISSION_ERROR": PermissionDeniedError,
    "BILLING_ERROR": BillingError,
    "RATE_LIMIT_ERROR": RateLimitError,
    "INTEGRATION_CONNECTION_ERROR": IntegrationConnectionError,
    "INTEGRATION_ERROR": IntegrationError,
    "OUTCOME_UNKNOWN_ERROR": OutcomeUnknownError,
    "INTERNAL_ERROR": InternalError,
}


_BY_STATUS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
}

_COMBINED: dict[tuple[type[APIError], type[APIStatusError]], type[APIError]] = {}


def _with_status(cls: type[APIError], status: Optional[int]) -> type[APIError]:
    """The class to raise: ``cls`` combined with the status class, so both ``except`` clauses match."""
    if status is None:
        return cls
    status_cls = _BY_STATUS.get(status, InternalServerError if status >= 500 else APIStatusError)
    if issubclass(cls, status_cls):
        return cls
    if cls is APIError:
        return status_cls
    combined = _COMBINED.get((cls, status_cls))
    if combined is None:
        combined = type(
            cls.__name__,
            (cls, status_cls),
            {"__module__": cls.__module__, "__qualname__": cls.__qualname__, "__doc__": cls.__doc__},
        )
        _COMBINED[(cls, status_cls)] = combined
    return combined


def error_from_object(
    error: Mapping[str, Any], *, status: Optional[int], headers: Optional[httpx.Headers] = None, body: Any = None
) -> APIError:
    """Builds the typed exception for an API error object. Unknown types get the base ``APIError``
    (with its status class)."""
    err_type = error.get("type")
    cls: type[APIError] = _BY_TYPE.get(err_type, APIError) if isinstance(err_type, str) else APIError
    if error.get("code") == "CURSOR_EXPIRED" and cls is InvalidRequestError:
        cls = CursorExpiredError
    message = error.get("message")
    text = message if isinstance(message, str) and message else f"HTTP {status} error"
    return _with_status(cls, status)(text, status=status, headers=headers, error=error, body=body)


def error_from_response(status: int, headers: httpx.Headers, content: bytes) -> APIError:
    """Builds the typed exception for a non-2xx HTTP response."""
    parsed: Any
    try:
        parsed = json.loads(content.decode("utf-8")) if content else None
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, Mapping) and isinstance(parsed.get("error"), Mapping):
        return error_from_object(parsed["error"], status=status, headers=headers, body=parsed)
    text = content.decode("utf-8", "replace")[:500] if content else ""
    return _with_status(APIError, status)(
        f"HTTP {status} error without a JSON error body", status=status, headers=headers, body=text or parsed
    )
