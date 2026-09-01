"""Bounded text handling for security-sensitive logs."""

import unicodedata
from typing import Any


def sanitize_log_value(value: Any, maximum: int = 512) -> str:
    """Remove line/control characters and bound a value before logging it."""
    text = str(value)[:maximum]
    return "".join(
        (
            "_"
            if character in "\r\n" or unicodedata.category(character).startswith("C")
            else character
        )
        for character in text
    )
