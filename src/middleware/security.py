"""Security middleware for enhanced application security."""

import logging
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers to all responses."""

    # Default security headers
    DEFAULT_HEADERS: dict[str, str] = {
        # Prevent MIME type sniffing
        "X-Content-Type-Options": "nosniff",
        # Prevent clickjacking
        "X-Frame-Options": "DENY",
        # Control referrer information
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # Permissions policy (formerly Feature Policy)
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        # Prevent IE from executing downloads in site context
        "X-Download-Options": "noopen",
        # DNS prefetch control
        "X-DNS-Prefetch-Control": "off",
        # Strict transport security (HSTS) - uncomment for HTTPS
        # "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    }

    def __init__(self, app: Any, custom_headers: dict[str, str] | None = None):
        """Initialize security headers middleware.

        Args:
            app: FastAPI application instance
            custom_headers: Optional custom headers to add/override
        """
        super().__init__(app)
        self.security_headers = self.DEFAULT_HEADERS.copy()
        if custom_headers:
            self.security_headers.update(custom_headers)
        logger.info(
            f"Security headers middleware initialized with {len(self.security_headers)} headers"
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Add security headers to the response.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            Response with security headers added
        """
        # Process the request
        response = await call_next(request)

        # Add security headers to response
        for header_name, header_value in self.security_headers.items():
            response.headers[header_name] = header_value

        # Add Content Security Policy based on content type
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            # Strict CSP for HTML responses
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "  # Allow inline scripts for Swagger UI
                "style-src 'self' 'unsafe-inline'; "  # Allow inline styles for Swagger UI
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )

        return response
