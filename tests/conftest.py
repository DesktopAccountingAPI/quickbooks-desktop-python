from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "conformance" / "fixtures"
TEST_KEY = "sk_test_Conformance0Key0For0SDK0Tests000010nQFLR"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def mock_server_url() -> Iterator[str]:
    """Starts ``node conformance/mock-server.mjs`` for the test session and yields its URL."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for the conformance suite (run the tests through `mise run check`).")
    proc = subprocess.Popen(
        [node, str(ROOT / "conformance" / "mock-server.mjs"), "--exit-on-stdin-close"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        if not line.startswith("MOCK_SERVER_URL="):
            pytest.fail(f"mock server did not start: {line!r}")
        yield line.split("=", 1)[1]
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
