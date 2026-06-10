"""Rate limiting middleware for the API."""

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ..config import Config

logger = logging.getLogger(__name__)


def get_client_identifier(request: Request) -> str:
    """Get client identifier for rate limiting.

    Uses API key if present, otherwise falls back to IP address.
    """
    # Check for API key in header
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key}"

    # Fall back to IP address
    return get_remote_address(request)


# Create the limiter instance
limiter = Limiter(key_func=get_client_identifier)


def get_limiter() -> Limiter:
    """Get the configured limiter instance."""
    return limiter


# Active limit string in slowapi multi-window format. Endpoints reference
# it through get_active_limits (a callable), so slowapi re-reads the value
# configured at app creation on every request instead of baking in a
# hardcoded string at import time.
_DEFAULT_LIMITS = "600/minute;10000/hour;100000/day"
_active_limits = _DEFAULT_LIMITS


def get_active_limits() -> str:
    """Current rate limit string derived from configuration."""
    return _active_limits


def _set_active_limits(value: str) -> None:
    global _active_limits
    _active_limits = value


class RateLimitMiddleware:
    """Applies rate limit configuration to the shared limiter."""

    def __init__(self, app: FastAPI, config: Config):
        """Initialize rate limiting middleware.

        Args:
            app: FastAPI application instance
            config: Application configuration
        """
        self.app = app
        self.config = config

        rate_limit = config.security.rate_limit
        if rate_limit.enabled:
            limiter.enabled = True
            _set_active_limits(
                f"{rate_limit.max_requests_per_minute}/minute;"
                f"{rate_limit.max_requests_per_hour}/hour;"
                f"{rate_limit.max_requests_per_day}/day"
            )

            # Add the limiter to the app state
            app.state.limiter = limiter

            # Register the rate limit exceeded handler
            app.add_exception_handler(
                RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler)
            )

            logger.info(f"Rate limiting enabled: {get_active_limits()}")
        else:
            limiter.enabled = False
            logger.info("Rate limiting disabled")
