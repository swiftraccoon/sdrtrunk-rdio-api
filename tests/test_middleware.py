"""Tests for middleware modules."""

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from src.config import Config
from src.middleware.rate_limiter import (
    RateLimitMiddleware,
    get_active_limits,
    get_limiter,
)
from src.middleware.security import SecurityHeadersMiddleware
from src.middleware.validation import RequestValidationMiddleware


class TestSecurityHeadersMiddleware:
    """Test security headers middleware."""

    def test_security_headers_added(self):
        """Test that security headers are added to responses."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add security headers middleware
        app.add_middleware(SecurityHeadersMiddleware)

        client = TestClient(app)
        response = client.get("/test")

        # Check that security headers are present
        assert "X-Content-Type-Options" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "X-Frame-Options" in response.headers
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "Referrer-Policy" in response.headers

    def test_security_headers_with_custom_headers(self):
        """Test security headers with custom headers."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add middleware with custom headers
        custom_headers = {"X-Custom-Header": "custom-value"}
        app.add_middleware(SecurityHeadersMiddleware, custom_headers=custom_headers)

        client = TestClient(app)
        response = client.get("/test")

        # Check that custom header is present
        assert "X-Custom-Header" in response.headers
        assert response.headers["X-Custom-Header"] == "custom-value"

    def test_content_security_policy_for_html(self):
        """Test that CSP is added for HTML responses."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return Response(content="<html></html>", media_type="text/html")

        app.add_middleware(SecurityHeadersMiddleware)

        client = TestClient(app)
        response = client.get("/test")

        # Check that CSP is present for HTML
        assert "Content-Security-Policy" in response.headers
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]


class TestRequestValidationMiddleware:
    """Test request validation middleware."""

    def test_request_size_limit_uses_config(self):
        """The size limit must track max_file_size_mb from config."""
        middleware = RequestValidationMiddleware(None)

        class FakeRequest:
            class app:
                class state:
                    config = Config()

        limit = middleware._max_content_length(FakeRequest())
        expected = (
            Config().file_handling.max_file_size_mb * 1024 * 1024
            + RequestValidationMiddleware.SIZE_HEADROOM_BYTES
        )
        assert limit == expected

    def test_valid_request_passes(self):
        """Test that valid requests pass through."""
        middleware = RequestValidationMiddleware(None)
        # Test allowed content types
        assert "application/json" in middleware.ALLOWED_CONTENT_TYPES
        assert "multipart/form-data" in middleware.ALLOWED_CONTENT_TYPES


class TestRateLimitMiddleware:
    """Test rate limiting middleware."""

    def test_rate_limit_disabled(self):
        """Test rate limiter when disabled."""
        config = Config()
        config.security.rate_limit.enabled = False

        app = FastAPI()

        # Add rate limit middleware (disabled)
        RateLimitMiddleware(app, config)

        # Limiter should be disabled
        limiter = get_limiter()
        assert limiter.enabled is False

    def test_rate_limit_enabled_uses_configured_limits(self):
        """Limits must be derived from configuration, not hardcoded."""
        config = Config()
        config.security.rate_limit.enabled = True
        config.security.rate_limit.max_requests_per_minute = 123
        config.security.rate_limit.max_requests_per_hour = 4567
        config.security.rate_limit.max_requests_per_day = 89012

        app = FastAPI()
        RateLimitMiddleware(app, config)

        limiter = get_limiter()
        assert limiter.enabled is True
        assert get_active_limits() == "123/minute;4567/hour;89012/day"
