"""Verify a webhook delivery with the standalone helper (no API key needed).

DAAPI_WEBHOOK_SECRET=whsec_... python examples/verify_webhook.py
"""

from __future__ import annotations

import json
import os
import time

from desktopaccountingapi import WebhookVerificationError, webhooks


def main() -> None:
    secret = os.environ["DAAPI_WEBHOOK_SECRET"]
    body = json.dumps({"id": "evt_example", "type": "webhook.test", "timestamp": "", "projectId": None, "data": {}})
    timestamp = int(time.time())
    headers = {
        "webhook-id": "evt_example",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": webhooks.sign(body, msg_id="evt_example", timestamp=timestamp, secret=secret),
    }
    try:
        event = webhooks.verify(body, headers, secret)
    except WebhookVerificationError as error:
        print(f"Rejected: {error}")
        return
    print(f"Verified {event.type} event {event.id}")


if __name__ == "__main__":
    main()
