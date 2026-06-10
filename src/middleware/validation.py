"""Request validation middleware."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)


class RequestValidationMiddleware(BaseHTTPMiddleware):
    """Middleware for validating incoming requests.

    Validates request size (against the configured max file size) and
    content type. Injection concerns are handled where they belong:
    the ORM parameterizes all queries and filenames are sanitized by
    the file handler.
    """

    # Headroom on top of max_file_size_mb for multipart framing/metadata
    SIZE_HEADROOM_BYTES = 10 * 1024 * 1024
    ALLOWED_CONTENT_TYPES = [
        "multipart/form-data",
        "application/x-www-form-urlencoded",
        "application/json",
    ]

    def _max_content_length(self, request: Request) -> int:
        """Request size limit derived from configuration."""
        try:
            max_file_mb = int(request.app.state.config.file_handling.max_file_size_mb)
        except AttributeError:
            max_file_mb = 100
        return max_file_mb * 1024 * 1024 + self.SIZE_HEADROOM_BYTES

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Validate incoming requests before processing.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            Response from the next handler
        """
        # Skip validation for health checks and metrics
        if request.url.path in [
            "/health",
            "/metrics",
            "/docs",
            "/redoc",
            "/openapi.json",
        ]:
            return await call_next(request)

        # Validate content length
        content_length = request.headers.get("content-length")
        if content_length:
            max_length = self._max_content_length(request)
            try:
                length = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if length > max_length:
                client_host = request.client.host if request.client else "unknown"
                logger.warning(f"Request too large: {length} bytes from {client_host}")
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"Request too large. Maximum size: {max_length} bytes"
                    },
                )

        # Validate content type for POST/PUT requests
        if request.method in ["POST", "PUT"]:
            content_type = request.headers.get("content-type", "").lower()

            # Extract base content type (ignore parameters like boundary)
            base_content_type = content_type.split(";")[0].strip()

            # Check if it's an allowed content type
            if not any(
                allowed in base_content_type for allowed in self.ALLOWED_CONTENT_TYPES
            ):
                client_host = request.client.host if request.client else "unknown"
                logger.warning(
                    f"Invalid content type: {content_type} from {client_host}"
                )
                return JSONResponse(
                    status_code=415,
                    content={
                        "detail": f"Unsupported content type: {base_content_type}"
                    },
                )

        # Continue to next middleware/handler
        response = await call_next(request)
        return response
