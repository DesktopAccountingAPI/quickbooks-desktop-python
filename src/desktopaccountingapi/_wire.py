"""Conversion between Python values and the API's JSON wire format.

Money and prices travel as decimal strings and become :class:`decimal.Decimal`; dates are
``YYYY-MM-DD`` strings and become :class:`datetime.date`; timestamps keep the offset QuickBooks
reported and become timezone-aware :class:`datetime.datetime` values.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Callable, ClassVar, TypeVar, Union
from urllib.parse import quote

from ._types import NotGiven

T = TypeVar("T")

DateOrDateTime = Union[str, _dt.date, _dt.datetime]
"""A date-range filter value: ``YYYY-MM-DD``, a local date-time without offset, or one with offset."""

_FRACTION = re.compile(r"(\.\d+)")


# ---------------------------------------------------------------------------------------------
# Parsing (wire -> Python). Tolerant: a value that does not parse is returned unchanged rather
# than failing the whole response, so additive or unexpected server output never breaks a call.
# ---------------------------------------------------------------------------------------------


def getter(data: Mapping[str, Any]) -> Callable[[str], Any]:
    """``data.get`` typed as returning ``Any`` (response values are checked by the parse helpers)."""
    return data.get


def parse_decimal(value: Any) -> Any:
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        try:
            return Decimal(str(value))
        except ArithmeticError:
            return value
    return value


def parse_date(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return _dt.date.fromisoformat(value) if len(value) == 10 else value
    except ValueError:
        return value


def parse_datetime(value: Any) -> Any:
    """Parses an ISO 8601 timestamp, keeping its offset. Handles ``Z`` and any fraction length."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    match = _FRACTION.search(text)
    if match is not None:
        digits = match.group(1)[1:]
        text = text[: match.start()] + "." + (digits + "000000")[:6] + text[match.end() :]
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return value


def parse_model(parse: Callable[[Mapping[str, Any]], T], value: Any) -> Any:
    return parse(value) if isinstance(value, Mapping) else value


def parse_list(parse: Callable[[Any], Any], value: Any) -> Any:
    if isinstance(value, list):
        return [parse(v) for v in value]
    return value


def as_dict(value: Any) -> dict[str, Any]:
    """A free-form JSON object response (passthrough) as a plain dict."""
    return dict(value) if isinstance(value, Mapping) else {}


def parse_model_list(parse: Callable[[Mapping[str, Any]], T], value: Any) -> Any:
    if isinstance(value, list):
        return [parse(v) if isinstance(v, Mapping) else v for v in value]
    return value


# ---------------------------------------------------------------------------------------------
# Serialization (Python -> wire)
# ---------------------------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """Formats a decimal without exponent and with its scale: ``Decimal("5.00")`` -> ``"5.00"``."""
    if not value.is_finite():
        raise ValueError(f"cannot send non-finite decimal {value!r}")
    return format(value, "f")


def format_datetime(value: _dt.datetime) -> str:
    """ISO 8601 with the value's own offset (``Z`` for UTC); seconds always present."""
    if value.microsecond == 0:
        spec = "seconds"
    elif value.microsecond % 1000 == 0:
        spec = "milliseconds"
    else:
        spec = "microseconds"
    text = value.isoformat(timespec=spec)
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def format_date_or_datetime(value: DateOrDateTime) -> str:
    if isinstance(value, _dt.datetime):
        return format_datetime(value)
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def to_json(value: Any) -> Any:
    """Converts SDK values (models, decimals, dates) into JSON-compatible values.

    Omitted values (``NOT_GIVEN``) are dropped from mappings; ``None`` stays as JSON ``null``.
    Binary floating point is never used for decimals.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, _dt.datetime):
        return format_datetime(value)
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, WireModel):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items() if not isinstance(v, NotGiven)}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    raise TypeError(f"cannot serialize {type(value).__name__} to JSON")


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return format_date_or_datetime(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    raise TypeError(f"cannot send {type(value).__name__} as a query parameter")


def query_items(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Query string pairs. Arrays use repeated keys (``ids=a&ids=b``); omitted and null values are left out."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None or isinstance(value, NotGiven):
            continue
        if isinstance(value, (list, tuple)):
            out.extend((key, _scalar(v)) for v in value)
        else:
            out.append((key, _scalar(value)))
    return out


def body(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON request body from wire-named fields; omitted fields are left out."""
    return {k: to_json(v) for k, v in fields.items() if not isinstance(v, NotGiven)}


def path_param(name: str, value: str) -> str:
    if not isinstance(value, str) or value == "":
        from ._errors import DaapiError

        raise DaapiError(f"Path parameter `{name}` must be a non-empty string, got {value!r}.")
    return quote(value, safe="")


# ---------------------------------------------------------------------------------------------
# Model base classes
# ---------------------------------------------------------------------------------------------


class WireModel:
    """Base of every generated model. ``_WIRE`` maps Python attribute names to JSON names."""

    __slots__ = ()
    _WIRE: ClassVar[tuple[tuple[str, str], ...]] = ()

    def to_dict(self) -> dict[str, Any]:
        """The model as wire JSON (camelCase names, decimals and dates as strings)."""
        out: dict[str, Any] = {}
        for attr, wire in self._WIRE:
            value = getattr(self, attr)
            if not isinstance(value, NotGiven):
                out[wire] = to_json(value)
        return out


class InputModel(WireModel):
    """Base of generated request models. Unset fields are ``NOT_GIVEN`` and are not sent."""

    __slots__ = ()

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{attr}={getattr(self, attr)!r}" for attr, _ in self._WIRE if not isinstance(getattr(self, attr), NotGiven)
        )
        return f"{type(self).__name__}({fields})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return all(getattr(self, attr) == getattr(other, attr) for attr, _ in self._WIRE)

    __hash__ = None  # type: ignore[assignment]


class ResponseModel(WireModel):
    """Base of generated response models (frozen dataclasses).

    Unknown response fields are ignored and missing fields read as ``None``, so additive API
    changes never break parsing.
    """

    __slots__ = ()
