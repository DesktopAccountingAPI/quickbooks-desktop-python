# Harness for the README's webhook handler body: a request handler that returns an HTTP status.
from __future__ import annotations

from collections.abc import Mapping


def {{NAME}}(raw_body: bytes, headers: Mapping[str, str]) -> int:
    {{SAMPLE}}
