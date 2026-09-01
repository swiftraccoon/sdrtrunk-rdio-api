"""Stable, nonsecret identifiers for configured API keys."""

from ..config import APIKeyConfig


def stable_api_key_identifier(api_key: APIKeyConfig) -> str:
    """Return the required identifier without deriving data from the secret."""
    return api_key.identifier
