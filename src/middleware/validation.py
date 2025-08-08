"""Request validation middleware."""

import logging
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)


class RequestValidationMiddleware(BaseHTTPMiddleware):
    """Middleware for validating incoming requests."""

    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB max request size
    ALLOWED_CONTENT_TYPES = [
        "multipart/form-data",
        "application/x-www-form-urlencoded",
        "application/json",
    ]

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Validate incoming requests before processing.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            Response from the next handler

        Raises:
            HTTPException: If validation fails
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
            try:
                length = int(content_length)
                if length > self.MAX_CONTENT_LENGTH:
                    client_host = request.client.host if request.client else "unknown"
                    logger.warning(
                        f"Request too large: {length} bytes from {client_host}"
                    )
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request too large. Maximum size: {self.MAX_CONTENT_LENGTH} bytes"
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length header"}
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

        # Validate specific headers for suspicious patterns
        # Only check user-controlled headers, not standard HTTP headers
        suspicious_headers = [
            "x-api-key",
            "authorization",
            "referer",
            "x-custom-header",
        ]
        for header_name in suspicious_headers:
            if header_name in request.headers:
                header_value = request.headers[header_name]
                # Check for SQL injection patterns in headers
                if self._contains_sql_injection(header_value):
                    client_host = request.client.host if request.client else "unknown"
                    logger.warning(
                        f"Potential SQL injection in header {header_name} from {client_host}"
                    )
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Potential SQL injection detected"},
                    )

                # Check for path traversal attempts
                if self._contains_path_traversal(header_value):
                    client_host = request.client.host if request.client else "unknown"
                    logger.warning(
                        f"Path traversal attempt in header {header_name} from {client_host}"
                    )
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Potential path traversal detected"},
                    )

        # Check for path traversal in URL path
        if self._contains_path_traversal(str(request.url.path)):
            client_host = request.client.host if request.client else "unknown"
            logger.warning(
                f"Path traversal attempt in URL from {client_host}: {request.url.path}"
            )
            return JSONResponse(
                status_code=400, content={"detail": "Potential path traversal detected"}
            )

        # Continue to next middleware/handler
        response = await call_next(request)
        return response

    @staticmethod
    def _contains_sql_injection(value: str) -> bool:
        """Check if value contains potential SQL injection patterns.

        Args:
            value: String to check

        Returns:
            True if suspicious patterns found
        """
        # Common SQL injection patterns
        sql_patterns = [
            r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|CREATE)\b)",
            r"(--|#|/\*|\*/)",
            r"(\bOR\b.*=.*)",
            r"(\bAND\b.*=.*)",
            r"(\'.*\bOR\b.*\')",
        ]

        value_upper = value.upper()
        for pattern in sql_patterns:
            if re.search(pattern, value_upper):
                return True
        return False

    @staticmethod
    def _contains_path_traversal(value: str) -> bool:
        """Check if value contains path traversal attempts.

        Args:
            value: String to check

        Returns:
            True if path traversal patterns found
        """
        # Path traversal patterns
        traversal_patterns = [
            r"\.\./",
            r"\.\.\\",  # Windows path traversal
            r"%2e%2e",
            r"\.\.%2f",
            r"%2e%2e%2f",
        ]

        value_lower = value.lower()
        for pattern in traversal_patterns:
            if re.search(pattern, value_lower, re.IGNORECASE):
                return True
        return False


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename to remove potentially dangerous characters.

    Args:
        filename: The filename to sanitize

    Returns:
        Sanitized filename
    """
    # Remove any path components
    filename = filename.split("/")[-1].split("\\")[-1]

    # Remove control characters and non-printable characters
    filename = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", filename)

    # Replace potentially dangerous characters
    filename = re.sub(r'[<>:"|?*]', "_", filename)

    # Limit length
    max_length = 255
    if len(filename) > max_length:
        # Preserve extension if possible
        parts = filename.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) < 10:
            base = parts[0][: max_length - len(parts[1]) - 1]
            filename = f"{base}.{parts[1]}"
        else:
            filename = filename[:max_length]

    # Ensure filename is not empty
    if not filename:
        filename = "unnamed_file"

    return filename


def sanitize_string(value: str, max_length: int = 255) -> str:
    """Sanitize a string value for safe storage.

    Args:
        value: The string to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized string
    """
    # Remove control characters
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value)

    # Limit length
    if len(value) > max_length:
        value = value[:max_length]

    return value.strip()
