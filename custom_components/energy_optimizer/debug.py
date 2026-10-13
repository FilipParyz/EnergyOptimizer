"""Helpers for debug logging and the diagnostics trace.

Nothing here changes behaviour -- it only formats intermediate values so
they are readable in the Home Assistant log and JSON-serialisable for the
"Download diagnostics" button.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any


def rounded(values: dict | None, digits: int = 4) -> dict[int, float] | None:
    if values is None:
        return None
    return {int(h): round(float(v), digits) for h, v in sorted(values.items())}


def fmt_hours(values: dict | None, digits: int = 3) -> str:
    if values is None:
        return "(none)"
    if not values:
        return "(empty)"
    return " ".join(f"{int(h):02d}h={float(v):.{digits}f}" for h, v in sorted(values.items()))


def json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else str(k)): json_safe(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value
