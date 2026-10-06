"""Local validation of secret API keys (format and CRC32 checksum), without a network call."""

from __future__ import annotations

import re
import zlib

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SECRET_KEY = re.compile(r"sk_(live|test)_([0-9A-Za-z]{40})")
_RANDOM_LENGTH = 34
_CHECKSUM_LENGTH = 6


def _base62(value: int, width: int) -> str:
    out = ""
    while value > 0:
        value, digit = divmod(value, 62)
        out = _BASE62[digit] + out
    return out.rjust(width, "0")


def is_valid_secret_key(key: object) -> bool:
    """True if ``key`` looks like a Desktop Accounting API secret key.

    A secret key is ``sk_live_`` or ``sk_test_`` followed by 40 base62 characters. The last 6
    characters are the zero-padded base62 CRC32 of the 34 characters before them, so a mistyped
    or truncated key is caught before any request is sent.
    """
    if not isinstance(key, str):
        return False
    match = _SECRET_KEY.fullmatch(key)
    if match is None:
        return False
    body = match.group(2)
    random_part, checksum = body[:_RANDOM_LENGTH], body[_RANDOM_LENGTH:]
    return _base62(zlib.crc32(random_part.encode("ascii")), _CHECKSUM_LENGTH) == checksum


def redact_key(key: str) -> str:
    """``sk_live_...`` plus the last 4 characters, safe for logs and error messages."""
    prefix = key[:8] if key.startswith(("sk_live_", "sk_test_")) else ""
    return f"{prefix}...{key[-4:]}" if len(key) >= 4 else "..."
