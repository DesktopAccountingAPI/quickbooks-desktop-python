"""Webhook signature verification (Standard Webhooks).

Usable without an API key::

    from desktopaccountingapi import webhooks

    event = webhooks.verify(request_body, request_headers, secret=os.environ["DAAPI_WEBHOOK_SECRET"])
    if event.type == webhooks.EventType.REQUEST_SUCCEEDED:
        ...

Each delivery carries ``webhook-id``, ``webhook-timestamp`` (Unix seconds) and ``webhook-signature``
(``v1,<base64 HMAC-SHA256 of "{id}.{timestamp}.{body}">``, several space-separated entries while a
secret is being rotated). Pass the raw request body exactly as received.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Optional, Union

from ._errors import WebhookVerificationError

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "EventType",
    "WebhookEvent",
    "Webhooks",
    "sign",
    "verify",
    "verify_signature",
]

DEFAULT_TOLERANCE_SECONDS: Final = 300
"""Maximum distance between ``webhook-timestamp`` and the local clock, in either direction."""

Payload = Union[str, bytes, bytearray]
Headers = Mapping[str, str]


class EventType:
    """Webhook event type names. ``WebhookEvent.type`` is a plain string, so new types pass through."""

    REQUEST_SUCCEEDED: Final = "request.succeeded"
    REQUEST_FAILED: Final = "request.failed"
    REQUEST_CANCELED: Final = "request.canceled"
    REQUEST_OUTCOME_UNKNOWN: Final = "request.outcome_unknown"
    REQUEST_OUTCOME_RESOLVED: Final = "request.outcome_resolved"
    CONNECTION_SETUP_COMPLETED: Final = "connection.setup_completed"
    CONNECTION_STATUS_CHANGED: Final = "connection.status_changed"
    CONNECTION_COMPANY_FILE_REMARKED: Final = "connection.company_file_remarked"
    WEBHOOK_TEST: Final = "webhook.test"


@dataclass(frozen=True)
class WebhookEvent:
    """A verified webhook event."""

    id: str
    """Event ID (``evt_...``), equal to the ``webhook-id`` header. Use it to deduplicate deliveries."""
    type: str
    """Event type, for example ``request.succeeded`` (see :class:`EventType`)."""
    timestamp: str
    """When the event happened (ISO 8601, UTC), as sent."""
    project_id: Optional[str]
    """The project (``proj_...``) the event belongs to."""
    data: dict[str, Any] = field(default_factory=dict)
    """Event data. Request events carry the request resource without ``result`` and ``timeline``."""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    """The complete parsed payload."""


def _header(headers: Headers, name: str) -> Optional[str]:
    # Exact, Title-Case, then any casing (plain dicts are case-sensitive; httpx/Starlette headers are not).
    for candidate in (name, name.title()):
        value = headers.get(candidate)
        if isinstance(value, str):
            return value
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name and isinstance(value, str):
            return value
    return None


def _secret_bytes(secret: Union[str, bytes]) -> bytes:
    if isinstance(secret, bytes):
        return secret
    text = secret.strip()
    if text.startswith("whsec_"):
        text = text[len("whsec_") :]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WebhookVerificationError("The webhook secret is not valid base64 (expected whsec_...).") from exc


def _payload_bytes(payload: Payload) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


def sign(payload: Payload, *, msg_id: str, timestamp: int, secret: Union[str, bytes]) -> str:
    """Computes the ``webhook-signature`` header value (``v1,<base64>``) for a payload.

    Useful for testing a webhook receiver without a delivered webhook.
    """
    key = _secret_bytes(secret)
    signed = f"{msg_id}.{timestamp}.".encode() + _payload_bytes(payload)
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_signature(
    payload: Payload,
    headers: Headers,
    secret: Union[str, bytes],
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[Callable[[], float]] = None,
) -> None:
    """Checks the headers, timestamp and signature of a webhook delivery.

    Raises :class:`WebhookVerificationError` on any problem. ``headers`` may be any mapping
    (names are matched case-insensitively). ``secret`` is the endpoint's signing secret, with or
    without the ``whsec_`` prefix. ``now`` returns the current Unix time in seconds, like ``time.time`` (tests can inject a clock).
    """
    msg_id = _header(headers, "webhook-id")
    timestamp_text = _header(headers, "webhook-timestamp")
    signature_header = _header(headers, "webhook-signature")
    if not msg_id:
        raise WebhookVerificationError("Missing webhook-id header.")
    if not timestamp_text:
        raise WebhookVerificationError("Missing webhook-timestamp header.")
    if not signature_header:
        raise WebhookVerificationError("Missing webhook-signature header.")
    try:
        timestamp = int(timestamp_text.strip())
    except ValueError as exc:
        raise WebhookVerificationError("The webhook-timestamp header is not a Unix timestamp.") from exc
    current = (now or time.time)()
    if timestamp < current - tolerance:
        raise WebhookVerificationError("The webhook timestamp is too old.")
    if timestamp > current + tolerance:
        raise WebhookVerificationError("The webhook timestamp is too far in the future.")
    expected = sign(payload, msg_id=msg_id, timestamp=timestamp, secret=secret).split(",", 1)[1].encode("ascii")
    for entry in signature_header.split(" "):
        version, _, value = entry.partition(",")
        if version != "v1" or not value:
            continue
        if hmac.compare_digest(value.encode("ascii", "replace"), expected):
            return
    raise WebhookVerificationError("No webhook signature matched the payload and secret.")


def verify(
    payload: Payload,
    headers: Headers,
    secret: Union[str, bytes],
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[Callable[[], float]] = None,
) -> WebhookEvent:
    """Verifies a webhook delivery (see :func:`verify_signature`) and parses its event."""
    verify_signature(payload, headers, secret, tolerance=tolerance, now=now)
    try:
        parsed = json.loads(_payload_bytes(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookVerificationError("The webhook payload is not valid JSON.") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str) or not isinstance(parsed.get("type"), str):
        raise WebhookVerificationError("The webhook payload is not a Desktop Accounting API event.")
    data = parsed.get("data")
    project_id = parsed.get("projectId")
    timestamp = parsed.get("timestamp")
    return WebhookEvent(
        id=parsed["id"],
        type=parsed["type"],
        timestamp=timestamp if isinstance(timestamp, str) else "",
        project_id=project_id if isinstance(project_id, str) else None,
        data=data if isinstance(data, dict) else {},
        raw=parsed,
    )


class Webhooks:
    """``client.webhooks``: the same helpers as the :mod:`desktopaccountingapi.webhooks` module."""

    def verify(
        self,
        payload: Payload,
        headers: Headers,
        secret: Union[str, bytes],
        *,
        tolerance: int = DEFAULT_TOLERANCE_SECONDS,
        now: Optional[Callable[[], float]] = None,
    ) -> WebhookEvent:
        """Verifies a webhook delivery and parses its event. See :func:`desktopaccountingapi.webhooks.verify`."""
        return verify(payload, headers, secret, tolerance=tolerance, now=now)

    def verify_signature(
        self,
        payload: Payload,
        headers: Headers,
        secret: Union[str, bytes],
        *,
        tolerance: int = DEFAULT_TOLERANCE_SECONDS,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        """Checks a webhook delivery's signature only. See :func:`desktopaccountingapi.webhooks.verify_signature`."""
        verify_signature(payload, headers, secret, tolerance=tolerance, now=now)
