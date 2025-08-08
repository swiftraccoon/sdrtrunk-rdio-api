"""Rate limiting middleware for the API."""

import logging
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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


class RateLimitMiddleware:
    """Custom rate limiting middleware with configuration support."""

    def __init__(self, app: FastAPI, config: Config):
        """Initialize rate limiting middleware.

        Args:
            app: FastAPI application instance
            config: Application configuration
        """
        self.app = app
        self.config = config

        # Only apply rate limiting if enabled in config
        if self.config.security.rate_limit.enabled:
            # Configure the limiter
            limiter.enabled = True

            # Add the limiter to the app state
            app.state.limiter = limiter

            # Register the rate limit exceeded handler
            app.add_exception_handler(
                RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler)
            )

            logger.info(
                f"Rate limiting enabled: {self.config.security.rate_limit.max_requests_per_minute} "
                f"requests per minute"
            )
        else:
            limiter.enabled = False
            logger.info("Rate limiting disabled")

    def get_rate_limit_string(self) -> str:
        """Get the rate limit string for slowapi.

        Returns rate limit in format: "X per Y"
        """
        rpm = self.config.security.rate_limit.max_requests_per_minute
        return f"{rpm} per minute"

    def apply_rate_limit(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Apply rate limiting to a specific endpoint.

        Args:
            func: The endpoint function to rate limit

        Returns:
            The decorated function with rate limiting
        """
        if not self.config.security.rate_limit.enabled:
            return func

        # Apply the rate limit decorator
        rate_limit = self.get_rate_limit_string()
        return cast(Callable[..., Any], limiter.limit(rate_limit)(func))

    def get_api_key_from_request(self, request: Request) -> str | None:
        """Extract API key from request headers.

        Args:
            request: The incoming request

        Returns:
            API key if found, None otherwise
        """
        return request.headers.get("x-api-key")

    def get_custom_limit(
        self, api_key: str | None = None, client_ip: str | None = None
    ) -> str:
        """Get custom rate limit for a specific API key or IP.

        Args:
            api_key: API key to check
            client_ip: Client IP to check

        Returns:
            Rate limit string in format "X/minute"
        """
        # Check for API key-specific limits
        if api_key and hasattr(self.config.security.rate_limit, "per_api_key"):
            per_api_key = getattr(self.config.security.rate_limit, "per_api_key", {})
            if api_key in per_api_key:
                return str(per_api_key[api_key])

        # Check for IP-specific limits
        if client_ip and hasattr(self.config.security.rate_limit, "per_ip"):
            per_ip = getattr(self.config.security.rate_limit, "per_ip", {})
            if client_ip in per_ip:
                return str(per_ip[client_ip])

        # Return default limit
        rpm = getattr(
            self.config.security.rate_limit,
            "requests_per_minute",
            self.config.security.rate_limit.max_requests_per_minute,
        )
        return f"{rpm}/minute"


def create_rate_limit_response(retry_after: int) -> JSONResponse:
    """Create a standardized rate limit exceeded response.

    Args:
        retry_after: Number of seconds to wait before retrying

    Returns:
        JSONResponse with rate limit information
    """
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded",
            "retry_after": retry_after,
            "message": f"Too many requests. Please retry after {retry_after} seconds.",
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": "60",  # Will be dynamically set
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(retry_after),
        },
    )
