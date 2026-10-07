"""Python client for Desktop Accounting API, the REST API for QuickBooks Desktop.

    from desktopaccountingapi import DesktopAccountingApi

    client = DesktopAccountingApi()  # reads DAAPI_SECRET_KEY
    for invoice in client.for_end_user("eu_...").qbd.invoices.list(limit=50):
        print(invoice.ref_number, invoice.subtotal)

Request and response models live in :mod:`desktopaccountingapi.types`.
"""

from . import webhooks
from ._base_client import DEFAULT_BASE_URL, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from ._client import AsyncDesktopAccountingApi, DesktopAccountingApi
from ._error_codes import ErrorCode, ErrorType
from ._errors import (
    APIConnectionError,
    ApiConnectionError,
    APIError,
    ApiError,
    APIStatusError,
    APITimeoutError,
    ApiTimeoutError,
    AuthenticationError,
    BadRequestError,
    BillingError,
    ConductorError,
    ConflictError,
    CursorExpiredError,
    DaapiError,
    ErrorFix,
    IntegrationConnectionError,
    IntegrationError,
    InternalError,
    InternalServerError,
    InvalidRequestError,
    NotFoundError,
    OutcomeUnknownError,
    PermissionDeniedError,
    RateLimitError,
    RequestPendingError,
    UnprocessableEntityError,
    WebhookVerificationError,
)
from ._handles import AsyncRequestHandle, RequestHandle
from ._keys import is_valid_secret_key
from ._pagination import AsyncCursorPager, CursorPage, CursorPager
from ._response import RawResponse
from ._types import NOT_GIVEN, NotGiven
from ._version import API_VERSION, CONTRACT_SHA256, __version__
from .webhooks import EventType, WebhookEvent

__all__ = [
    "API_VERSION",
    "CONTRACT_SHA256",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "NOT_GIVEN",
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
    "ApiConnectionError",
    "ApiError",
    "ApiTimeoutError",
    "AsyncCursorPager",
    "AsyncDesktopAccountingApi",
    "AsyncRequestHandle",
    "AuthenticationError",
    "BadRequestError",
    "BillingError",
    "ConductorError",
    "ConflictError",
    "CursorExpiredError",
    "CursorPage",
    "CursorPager",
    "DaapiError",
    "DesktopAccountingApi",
    "ErrorCode",
    "ErrorFix",
    "ErrorType",
    "EventType",
    "IntegrationConnectionError",
    "IntegrationError",
    "InternalError",
    "InternalServerError",
    "InvalidRequestError",
    "NotFoundError",
    "NotGiven",
    "OutcomeUnknownError",
    "PermissionDeniedError",
    "RateLimitError",
    "RawResponse",
    "RequestHandle",
    "RequestPendingError",
    "UnprocessableEntityError",
    "WebhookEvent",
    "WebhookVerificationError",
    "__version__",
    "is_valid_secret_key",
    "webhooks",
]
