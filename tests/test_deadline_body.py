"""The call's total deadline (``total_timeout``, or the wait for a pending request) and the response.

The async client enforces a hard bound: every attempt runs under ``asyncio.wait_for``. The sync
client enforces it cooperatively, without threads: each attempt gets the time left as its connect,
read, write and pool timeouts, and the deadline is checked once headers arrive and between body
chunks. One stalled read can overrun by at most its read budget (the time left when the attempt
started); trickling header bytes can extend an attempt. Sync requests always go through the
``httpx.Client`` itself, so auth flows, event hooks, cookies and ``trust_env`` behave the same with
and without a deadline.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Generator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from desktopaccountingapi import APITimeoutError, DesktopAccountingApi, RequestPendingError
from desktopaccountingapi._base_client import AsyncAPIClient, SyncAPIClient

KEY = "sk_test_Conformance0Key0For0SDK0Tests000010nQFLR"
ALLOWANCE = 0.06
SEEN: list[dict[str, str]] = []


def _snapshot(request_id: str) -> bytes:
    return json.dumps(
        {
            "id": request_id,
            "status": "succeeded",
            "outcome": "applied",
            "result": {"id": "saved"},
            "createdAt": "2026-10-07T00:00:00Z",
        }
    ).encode()


class _Slow(BaseHTTPRequestHandler):
    """`req_body`: body in 24 chunks 25 ms apart. `req_headers`: 24 header lines 25 ms apart.
    `req_tail`: one byte, one more after 180 ms, the rest after 360 ms. `req_echo`: records the
    request headers and sets a cookie. Else: immediate."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        request_id = self.path.split("?")[0].rsplit("/", 1)[-1]
        body = _snapshot(request_id)
        try:
            if request_id == "req_trailers":
                # The whole body at once, then chunked trailers trickling to EOF for about 200 ms.
                self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Slow\r\n\r\n")
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n")
                self.wfile.flush()
                for _ in range(20):
                    self.wfile.write(b"X-Slow: trailer\r\n")
                    self.wfile.flush()
                    time.sleep(0.01)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                return
            if request_id == "req_headers":
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.flush()
                for i in range(24):
                    self.wfile.write(f"X-Slow-{i}: x\r\n".encode())
                    self.wfile.flush()
                    time.sleep(0.025)
                head = f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                self.wfile.write(head.encode() + body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if request_id == "req_echo":
                SEEN.append({k.lower(): v for k, v in self.headers.items()})
                self.send_header("Set-Cookie", "daapi_affinity=abc; Path=/")
            self.end_headers()
            if request_id == "req_body":
                step = max(1, len(body) // 24)
                for i in range(0, len(body), step):
                    self.wfile.write(body[i : i + step])
                    self.wfile.flush()
                    time.sleep(0.025)
            elif request_id == "req_tail":
                for part in (body[:1], body[1:2]):
                    self.wfile.write(part)
                    self.wfile.flush()
                    time.sleep(0.18)
                self.wfile.write(body[2:])
            else:
                self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(ThreadingHTTPServer):
    """Names its handler threads, so the thread-leak test can tell them apart from the client's on
    every Python version: before 3.10 a handler thread is plain ``Thread-N``, without the
    ``(process_request_thread)`` suffix that 3.10 added."""

    def process_request(self, request: Any, client_address: Any) -> None:
        threading.Thread(
            target=self.process_request_thread, args=(request, client_address), name=SERVER_THREAD, daemon=True
        ).start()


SERVER_THREAD = "test-server-handler"


@pytest.fixture
def url() -> Iterator[str]:
    server = _Server(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# Sync bounds: a trickling body ends within one chunk of the deadline; a read begun just before the
# deadline may run for its read budget (the time left when the attempt started).
@pytest.mark.parametrize(("request_id", "budget", "bound"), [("req_body", 0.08, 0.08), ("req_tail", 0.2, 0.4)])
def test_sync_poll_ends_by_the_deadline(url: str, request_id: str, budget: float, bound: float) -> None:
    client = SyncAPIClient(api_key=KEY, base_url=url, max_retries=0, timeout=1)
    try:
        client._poll("req_fast", lambda x: x, time.monotonic() + 5, idempotency_key=None)  # warm up
        started = time.monotonic()
        with pytest.raises(RequestPendingError) as caught:
            client._poll(request_id, lambda x: x, started + budget, idempotency_key="deadline-key")
        elapsed = time.monotonic() - started
    finally:
        client.close()
    assert elapsed < bound + ALLOWANCE, f"{budget * 1000:.0f} ms budget ended after {elapsed * 1000:.0f} ms"
    assert caught.value.idempotency_key == "deadline-key"


@pytest.mark.parametrize(("request_id", "budget"), [("req_body", 0.08), ("req_headers", 0.08), ("req_tail", 0.2)])
def test_async_poll_ends_at_the_deadline(url: str, request_id: str, budget: float) -> None:
    async def run() -> float:
        client = AsyncAPIClient(api_key=KEY, base_url=url, max_retries=0, timeout=1)
        try:
            await client._poll("req_fast", lambda x: x, time.monotonic() + 5, idempotency_key=None)  # warm up
            started = time.monotonic()
            with pytest.raises(RequestPendingError):
                await client._poll(request_id, lambda x: x, started + budget, idempotency_key="deadline-key")
            return time.monotonic() - started
        finally:
            await client.close()

    elapsed = asyncio.run(run())
    assert elapsed < budget + ALLOWANCE, f"{budget * 1000:.0f} ms budget ended after {elapsed * 1000:.0f} ms"


def test_slow_response_within_the_deadline_is_returned(url: str) -> None:
    client = SyncAPIClient(api_key=KEY, base_url=url, max_retries=0, timeout=5)
    try:
        for request_id in ("req_body", "req_headers", "req_tail"):
            value, response = client._poll(request_id, lambda x: x, time.monotonic() + 5, idempotency_key=None)
            assert value == {"id": "saved"}
            assert response.json()["status"] == "succeeded"
    finally:
        client.close()


class _HeaderAuth(httpx.Auth):
    """An auth flow (as a proxy or gateway might need) that adds a header to every request."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["X-Gateway-Auth"] = "flow"
        yield request


@pytest.mark.parametrize("total_timeout", [None, 5.0])
def test_client_features_are_the_same_with_and_without_a_deadline(
    url: str, total_timeout: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth flows, request hooks, Set-Cookie storage and trust_env=False (with a broken SSL env
    setting that must not be read) behave the same whether or not the call has a deadline."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/daapi-test.pem")
    hooked: list[str] = []

    def hook(request: httpx.Request) -> None:
        request.headers["X-Hook"] = "hooked"
        hooked.append(str(request.url))

    http = httpx.Client(auth=_HeaderAuth(), event_hooks={"request": [hook]}, trust_env=False)
    kwargs: dict[str, Any] = {} if total_timeout is None else {"total_timeout": total_timeout}
    client = DesktopAccountingApi(api_key=KEY, base_url=url, http_client=http, max_retries=0, **kwargs)
    SEEN.clear()
    try:
        first = client.requests.retrieve("req_echo")
        second = client.requests.retrieve("req_echo")
    finally:
        http.close()
    assert first.id == "req_echo" and second.id == "req_echo"
    assert [h.get("x-gateway-auth") for h in SEEN] == ["flow", "flow"]
    assert [h.get("x-hook") for h in SEEN] == ["hooked", "hooked"]
    assert len(hooked) == 2
    assert http.cookies.get("daapi_affinity") == "abc", "Set-Cookie is stored in the client's jar"
    assert SEEN[1].get("cookie") == "daapi_affinity=abc", "and sent with the next request"


def test_a_timed_out_attempt_leaves_no_threads(url: str) -> None:
    before = {t.ident for t in threading.enumerate()}
    client = SyncAPIClient(api_key=KEY, base_url=url, max_retries=0, timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(RequestPendingError):
            client._poll("req_body", lambda x: x, started + 0.08, idempotency_key="k")
    finally:
        client.close()
    # The local test server's own handler threads may still be sending; only the client's count.
    extra = [t.name for t in threading.enumerate() if t.ident not in before and t.name != SERVER_THREAD]
    assert extra == [], f"threads left behind: {extra}"


def test_delayed_eof_after_the_last_chunk_is_not_a_success(url: str) -> None:
    """codex re-review round 6: httpx keeps reading after the last body chunk (chunked trailers to
    EOF). A response that completes after the deadline raises APITimeoutError, never success."""
    http = httpx.Client(trust_env=False)
    client = DesktopAccountingApi(api_key=KEY, base_url=url, http_client=http, max_retries=0)
    try:
        assert client.requests.retrieve("req_trailers").id == "req_trailers", "without a deadline it succeeds"
        with pytest.raises(APITimeoutError):
            client.requests.retrieve("req_trailers", total_timeout=0.04)
        started = time.monotonic()
        with pytest.raises(RequestPendingError):
            SyncAPIClient(api_key=KEY, base_url=url, max_retries=0, http_client=http)._poll(
                "req_trailers", lambda x: x, started + 0.04, idempotency_key=None
            )
    finally:
        http.close()
