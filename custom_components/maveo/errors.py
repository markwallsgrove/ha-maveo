"""Translated Home Assistant exceptions for Maveo failures."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


def translated[E: HomeAssistantError](cls: type[E], key: str, err: Exception, **placeholders: str) -> E:
    """Build `cls` with the Maveo translation `key` and the error as `{error}`."""
    return cls(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={
            "error": str(err) or type(err).__name__,
            **placeholders,
        },
    )
