from __future__ import annotations

import base64
import json

import pytest

import desktopaccountingapi as daapi
from desktopaccountingapi import webhooks
from desktopaccountingapi._keys import redact_key

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
BODY = json.dumps(
    {
        "id": "evt_1",
        "type": "request.succeeded",
        "timestamp": "2026-10-05T16:04:01.311Z",
        "projectId": "proj_1",
        "data": {"objectType": "request", "id": "req_1", "status": "succeeded"},
    }
)
NOW = 1791216241


def headers(signature: str, timestamp: int = NOW) -> dict[str, str]:
    return {"webhook-id": "evt_1", "webhook-timestamp": str(timestamp), "webhook-signature": signature}


def test_sign_and_verify_round_trip() -> None:
    signature = webhooks.sign(BODY, msg_id="evt_1", timestamp=NOW, secret=SECRET)
    event = webhooks.verify(BODY, headers(signature), SECRET, now=lambda: NOW)
    assert event.type == webhooks.EventType.REQUEST_SUCCEEDED
    assert event.project_id == "proj_1" and event.data["id"] == "req_1"
    # The secret works without its prefix, and bytes payloads work too.
    secret_without_prefix = SECRET[len("whsec_") :]
    webhooks.verify(BODY.encode(), headers(signature), secret_without_prefix, now=lambda: NOW)


def test_rotation_and_other_versions() -> None:
    good = webhooks.sign(BODY, msg_id="evt_1", timestamp=NOW, secret=SECRET)
    webhooks.verify_signature(BODY, headers(f"v2,abc v1,AAAA {good}"), SECRET, now=lambda: NOW)
    with pytest.raises(daapi.WebhookVerificationError):
        webhooks.verify_signature(BODY, headers("v2," + good.split(",", 1)[1]), SECRET, now=lambda: NOW)


def test_tolerance_both_directions() -> None:
    signature = webhooks.sign(BODY, msg_id="evt_1", timestamp=NOW, secret=SECRET)
    for offset in (301, -301):

        def clock(offset: int = offset) -> float:
            return float(NOW + offset)

        with pytest.raises(daapi.WebhookVerificationError):
            webhooks.verify(BODY, headers(signature), SECRET, now=clock)
    webhooks.verify(BODY, headers(signature), SECRET, now=lambda: NOW + 600, tolerance=600)


def test_client_helper_and_header_case() -> None:
    signature = webhooks.sign(BODY, msg_id="evt_1", timestamp=NOW, secret=SECRET)
    mixed = {"Webhook-Id": "evt_1", "WEBHOOK-TIMESTAMP": str(NOW), "webhook-Signature": signature}
    with daapi.DesktopAccountingApi(api_key="sk_test_Conformance0Key0For0SDK0Tests000010nQFLR") as client:
        event = client.webhooks.verify(BODY, mixed, SECRET, now=lambda: NOW)
    assert event.id == "evt_1"


def test_not_an_event() -> None:
    body = "[1, 2]"
    signature = webhooks.sign(body, msg_id="evt_1", timestamp=NOW, secret=SECRET)
    with pytest.raises(daapi.WebhookVerificationError):
        webhooks.verify(body, headers(signature), SECRET, now=lambda: NOW)
    with pytest.raises(daapi.WebhookVerificationError):
        webhooks.verify(BODY, headers(signature), "whsec_not base64!", now=lambda: NOW)


def test_key_helpers() -> None:
    assert daapi.is_valid_secret_key("sk_live_aZ09aZ09aZ09aZ09aZ09aZ09aZ09aZ09xy26zcWJ")
    assert not daapi.is_valid_secret_key(None)
    assert redact_key("sk_live_aZ09aZ09aZ09aZ09aZ09aZ09aZ09aZ09xy26zcWJ") == "sk_live_...zcWJ"
