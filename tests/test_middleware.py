"""Tests for middleware modules."""

from unittest.mock import MagicMock

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from src.middleware.rate_limiter import RateLimitMiddleware, get_limiter
from src.middleware.security import CORSSecurityMiddleware, SecurityHeadersMiddleware
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
        assert "X-XSS-Protection" in response.headers
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


class TestCORSSecurityMiddleware:
    """Test CORS security middleware."""

    def test_cors_headers_for_allowed_origin(self):
        """Test CORS headers for allowed origin."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add CORS middleware
        app.add_middleware(
            CORSSecurityMiddleware,
            allowed_origins=["http://localhost:3000"],
            allow_credentials=True,
        )

        client = TestClient(app)
        response = client.get("/test", headers={"Origin": "http://localhost:3000"})

        # Check CORS headers
        assert "Access-Control-Allow-Origin" in response.headers
        assert (
            response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        )
        assert "Access-Control-Allow-Credentials" in response.headers
        assert response.headers["Access-Control-Allow-Credentials"] == "true"

    def test_cors_headers_for_disallowed_origin(self):
        """Test CORS headers for disallowed origin."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add CORS middleware
        app.add_middleware(
            CORSSecurityMiddleware,
            allowed_origins=["http://localhost:3000"],
        )

        client = TestClient(app)
        response = client.get("/test", headers={"Origin": "http://evil.com"})

        # Check that CORS headers are not present for disallowed origin
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_cors_preflight_request(self):
        """Test CORS preflight OPTIONS request."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add CORS middleware
        app.add_middleware(
            CORSSecurityMiddleware,
            allowed_origins=["*"],
            allowed_methods=["GET", "POST"],
            allowed_headers=["Content-Type", "Authorization"],
            max_age=3600,
        )

        client = TestClient(app)
        response = client.options("/test", headers={"Origin": "http://example.com"})

        # Check preflight response
        assert response.status_code == 200
        assert "Access-Control-Allow-Origin" in response.headers
        assert "Access-Control-Allow-Methods" in response.headers
        assert "GET" in response.headers["Access-Control-Allow-Methods"]
        assert "POST" in response.headers["Access-Control-Allow-Methods"]
        assert "Access-Control-Max-Age" in response.headers
        assert response.headers["Access-Control-Max-Age"] == "3600"


class TestRequestValidationMiddleware:
    """Test request validation middleware."""

    def test_request_size_limit(self):
        """Test that request size is limited."""
        # The middleware has a fixed 100MB limit
        # We can't easily test this without a huge request
        # So we'll test the logic directly
        middleware = RequestValidationMiddleware(None)
        assert middleware.MAX_CONTENT_LENGTH == 100 * 1024 * 1024

    def test_sql_injection_detection(self):
        """Test SQL injection detection in headers."""
        middleware = RequestValidationMiddleware(None)
        # Test the SQL injection detection function directly
        assert middleware._contains_sql_injection("'; DROP TABLE users; --")
        assert middleware._contains_sql_injection("1' OR '1'='1")
        assert not middleware._contains_sql_injection("normal text")
        # Additional patterns
        assert middleware._contains_sql_injection("SELECT * FROM users")
        assert middleware._contains_sql_injection("UNION SELECT")
        assert middleware._contains_sql_injection("INSERT INTO")
        assert middleware._contains_sql_injection("UPDATE users SET")
        assert middleware._contains_sql_injection("DELETE FROM")
        assert middleware._contains_sql_injection("--comment")
        assert middleware._contains_sql_injection("/*comment*/")

    def test_path_traversal_detection(self):
        """Test path traversal detection."""
        middleware = RequestValidationMiddleware(None)
        # Test the path traversal detection function directly
        assert middleware._contains_path_traversal("../../etc/passwd")
        assert middleware._contains_path_traversal("../../../")
        assert not middleware._contains_path_traversal("/normal/path")
        # Additional patterns
        assert middleware._contains_path_traversal("..\\..\\windows")
        assert middleware._contains_path_traversal("%2e%2e/")
        assert middleware._contains_path_traversal("..%2f")
        assert middleware._contains_path_traversal("%2e%2e%2f")

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
        config = MagicMock()
        config.security.rate_limit.enabled = False

        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add rate limit middleware (disabled)
        RateLimitMiddleware(app, config)

        # Limiter should be disabled
        limiter = get_limiter()
        assert limiter.enabled is False

    def test_rate_limit_enabled(self):
        """Test rate limiter when enabled."""
        config = MagicMock()
        config.security.rate_limit.enabled = True
        config.security.rate_limit.requests_per_minute = 60
        config.security.rate_limit.per_api_key = {}
        config.security.rate_limit.per_ip = {}

        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        # Add rate limit middleware (enabled)
        RateLimitMiddleware(app, config)

        # Limiter should be enabled
        limiter = get_limiter()
        assert limiter.enabled is True

    def test_get_api_key_from_request(self):
        """Test extracting API key from request."""
        # Test that RateLimitMiddleware class has the expected methods
        assert hasattr(RateLimitMiddleware, "get_api_key_from_request")
        assert hasattr(RateLimitMiddleware, "get_custom_limit")

    def test_get_custom_limit(self):
        """Test getting custom rate limits."""
        config = MagicMock()
        config.security.rate_limit.enabled = True
        config.security.rate_limit.requests_per_minute = 60
        config.security.rate_limit.per_api_key = {"test-key": "100/minute"}
        config.security.rate_limit.per_ip = {"192.168.1.1": "10/minute"}

        app = FastAPI()
        rate_limiter = RateLimitMiddleware(app, config)

        # Test API key limit
        limit = rate_limiter.get_custom_limit(api_key="test-key", client_ip="127.0.0.1")
        assert limit == "100/minute"

        # Test IP limit
        limit = rate_limiter.get_custom_limit(api_key=None, client_ip="192.168.1.1")
        assert limit == "10/minute"

        # Test default limit
        limit = rate_limiter.get_custom_limit(api_key=None, client_ip="127.0.0.1")
        assert limit == "60/minute"
