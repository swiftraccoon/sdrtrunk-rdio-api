"""Tests for middleware modules."""

import asyncio

import pytest
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
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Permitted-Cross-Domain-Policies"] == "none"

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

    def test_streamed_body_limit_without_content_length(self):
        """Actual ASGI chunks are capped when Content-Length is absent."""
        sent = []
        messages = iter(
            [
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"456", "more_body": False},
            ]
        )

        async def inner_app(scope, receive, send):
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    return

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        middleware = RequestValidationMiddleware(inner_app, max_body_size_bytes=5)
        asyncio.run(middleware(scope, receive, send))

        response_start = next(
            item for item in sent if item["type"] == "http.response.start"
        )
        assert response_start["status"] == 413

    @pytest.mark.parametrize(
        ("http_version", "headers"),
        [
            (
                "1.1",
                [
                    (b"transfer-encoding", b"chunked"),
                    (b"transfer-encoding", b"chunked"),
                ],
            ),
            (
                "1.1",
                [
                    (b"content-length", b"1"),
                    (b"transfer-encoding", b"chunked"),
                ],
            ),
            ("1.1", [(b"transfer-encoding", b"gzip, chunked")]),
            ("2", [(b"transfer-encoding", b"chunked")]),
        ],
    )
    def test_rejects_ambiguous_transfer_framing(self, http_version, headers):
        sent = []
        inner_called = False

        async def inner_app(scope, receive, send):
            nonlocal inner_called
            inner_called = True

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": http_version,
            "method": "POST",
            "scheme": "http",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                *headers,
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }

        middleware = RequestValidationMiddleware(inner_app)
        asyncio.run(middleware(scope, receive, send))

        response_start = next(
            item for item in sent if item["type"] == "http.response.start"
        )
        assert response_start["status"] == 400
        assert not inner_called

    def test_rejects_oversized_live_content_type_header(self):
        response = self._request_with_content_type(
            "multipart/form-data; boundary=x; ignored=" + "a" * 8192
        )
        assert response.status_code == 400

    def test_non_multipart_body_has_small_preparse_limit(self):
        """URL-encoded/JSON parsers must not materialize an audio-sized body."""
        app = FastAPI()

        @app.post("/upload")
        async def upload():
            return {"ok": True}

        app.add_middleware(
            RequestValidationMiddleware,
            max_body_size_bytes=2 * 1024 * 1024,
        )
        oversized = b"x" * (
            RequestValidationMiddleware.MAX_NON_MULTIPART_BODY_BYTES + 1
        )
        with TestClient(app) as client:
            response = client.post(
                "/upload",
                content=oversized,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert response.status_code == 413

    def test_rejects_oversized_live_multipart_boundary(self):
        response = self._request_with_content_type(
            "multipart/form-data; boundary=" + "a" * 201
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid multipart boundary"}

    def test_rejects_conflicting_duplicate_multipart_boundaries(self):
        response = self._request_with_content_type(
            "multipart/form-data; boundary=first; boundary=second"
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid multipart boundary"}

    @pytest.mark.parametrize(
        "method", ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"]
    )
    def test_bodyless_methods_reject_declared_request_bodies(self, method: str):
        app = FastAPI()

        @app.api_route("/resource", methods=["GET", "HEAD", "OPTIONS"])
        async def resource():
            return {"ok": True}

        app.add_middleware(RequestValidationMiddleware, max_body_size_bytes=1024)
        with TestClient(app) as client:
            response = client.request(method, "/resource", content=b"unexpected")

        assert response.status_code == 400

    def test_bodyless_methods_reject_chunked_transfer_encoding(self):
        app_called = False

        async def downstream(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RequestValidationMiddleware(downstream, max_body_size_bytes=1024)
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/resource",
            "headers": [(b"transfer-encoding", b"chunked")],
        }
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

        assert not app_called
        assert messages[0]["status"] == 400

    def test_bodyless_methods_reject_undeclared_http2_data(self):
        """HTTP/2 DATA frames are rejected even without framing headers."""
        app_called = False

        async def downstream(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RequestValidationMiddleware(downstream, max_body_size_bytes=1024)
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"unexpected", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "http_version": "2",
            "method": "GET",
            "path": "/resource",
            "headers": [],
        }
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

        assert not app_called
        assert messages[0]["status"] == 400

    def test_bodyless_http2_stream_has_independent_receive_timeout(self):
        """Traffic on other H2 streams cannot pin a pre-routing receive."""
        app_called = False

        async def downstream(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RequestValidationMiddleware(
            downstream,
            max_body_size_bytes=1024,
            read_timeout_seconds=0.01,
        )
        messages: list[dict[str, object]] = []
        never_received = asyncio.Event()

        async def receive() -> dict[str, object]:
            await never_received.wait()
            raise AssertionError("unreachable")

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "http_version": "2",
            "method": "GET",
            "path": "/resource",
            "headers": [],
        }
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

        assert not app_called
        assert messages[0]["status"] == 408

    def test_stalled_post_stream_has_independent_receive_timeout(self):
        async def downstream(scope, receive, send):
            while True:
                await receive()

        middleware = RequestValidationMiddleware(
            downstream,
            max_body_size_bytes=1024,
            read_timeout_seconds=0.01,
        )
        messages: list[dict[str, object]] = []
        never_received = asyncio.Event()

        async def receive() -> dict[str, object]:
            await never_received.wait()
            raise AssertionError("unreachable")

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "http_version": "2",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-type", b"application/json")],
        }
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

        assert messages[0]["status"] == 408

    def test_post_trickle_cannot_reset_absolute_body_deadline(self):
        chunks_received = 0

        async def downstream(scope, receive, send):
            while True:
                await receive()

        middleware = RequestValidationMiddleware(
            downstream,
            max_body_size_bytes=1024,
            read_timeout_seconds=0.03,
        )
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            nonlocal chunks_received
            await asyncio.sleep(0.005)
            chunks_received += 1
            return {"type": "http.request", "body": b"x", "more_body": True}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "http_version": "2",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-type", b"application/json")],
        }
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

        assert chunks_received > 1
        assert messages[0]["status"] == 408

    def test_bodyless_receive_timeout_uses_server_config(self):
        middleware = RequestValidationMiddleware(None)

        class FakeApp:
            class state:
                config = Config(server={"read_timeout_seconds": 17})

        scope = {"app": FakeApp()}
        assert middleware._read_timeout_from_scope(scope) == 17  # type: ignore[arg-type]

    @staticmethod
    def _request_with_content_type(content_type: str):
        app = FastAPI()

        @app.post("/upload")
        async def upload():
            return {"ok": True}

        app.add_middleware(RequestValidationMiddleware, max_body_size_bytes=1024)
        with TestClient(app) as client:
            return client.post(
                "/upload", content=b"x", headers={"Content-Type": content_type}
            )


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
