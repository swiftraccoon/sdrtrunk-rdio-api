"""Request validation and streaming request-body limits."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from python_multipart.multipart import parse_options_header
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from ..security.logging import WarningSampler
from .rate_limiter import account_route_validation_failure

logger = logging.getLogger(__name__)
_rejection_warning_sampler = WarningSampler(maximum_per_window=20)


def _header_parameter_count(header: str, target: str) -> int:
    """Count a parameter without treating semicolons inside quotes as separators."""
    segments: list[str] = []
    segment: list[str] = []
    quoted = False
    escaped = False
    for character in header:
        if escaped:
            segment.append(character)
            escaped = False
        elif quoted and character == "\\":
            segment.append(character)
            escaped = True
        elif character == '"':
            segment.append(character)
            quoted = not quoted
        elif character == ";" and not quoted:
            segments.append("".join(segment))
            segment = []
        else:
            segment.append(character)
    if quoted or escaped:
        return -1
    segments.append("".join(segment))
    return sum(
        1
        for parameter in segments[1:]
        if parameter.partition("=")[0].strip().lower() == target and "=" in parameter
    )


class RequestBodyTooLarge(Exception):
    """Raised as soon as a streamed request exceeds the configured limit."""

    def __init__(self, maximum: int):
        super().__init__("request body exceeds configured maximum")
        self.maximum = maximum


class RequestBodyReadTimeout(Exception):
    """Raised when one request stream exceeds its absolute body deadline."""


class RequestValidationMiddleware:
    """Validate request metadata and cap bytes received from the ASGI server.

    ``Content-Length`` is useful for an early rejection, but it is not a
    security boundary: HTTP/1.1 chunked requests and HTTP/2 requests may omit
    it. The wrapped ``receive`` callable counts every body chunk and terminates
    the request as soon as the real byte count crosses the limit. One absolute
    per-stream deadline also covers the complete request body, because a
    connection-wide socket timeout can be kept alive by unrelated HTTP/2
    streams.

    ``max_body_size_bytes`` is an optional integration hook for deployments
    that want a fixed limit. When omitted, the existing application config is
    read for each request so test and embedded applications retain their own
    limits.
    """

    # One MiB covers the independently bounded 32 metadata parts and multipart
    # framing while keeping the transport ceiling close to the configured file
    # limit. The file part itself is separately capped by the live parser.
    SIZE_HEADROOM_BYTES = 1024 * 1024
    # The upload protocol uses multipart for audio. Starlette's URL-encoded
    # parser materializes all fields and does not enforce ``max_fields``, so a
    # much smaller transport ceiling is the pre-parsing memory boundary for
    # URL-encoded/JSON bodies.
    MAX_NON_MULTIPART_BODY_BYTES = 64 * 1024
    MAX_CONTENT_TYPE_BYTES = 8192
    MAX_MULTIPART_BOUNDARY_BYTES = 200
    ALLOWED_CONTENT_TYPES = frozenset(
        {
            "multipart/form-data",
            "application/x-www-form-urlencoded",
            "application/json",
        }
    )

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        max_body_size_bytes: int | None = None,
        size_headroom_bytes: int = SIZE_HEADROOM_BYTES,
        read_timeout_seconds: float | None = None,
    ) -> None:
        if read_timeout_seconds is not None and read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        self.app = app
        self.max_body_size_bytes = max_body_size_bytes
        self.size_headroom_bytes = size_headroom_bytes
        self.read_timeout_seconds = read_timeout_seconds

    def _max_content_length(self, request: Request | Any) -> int:
        """Return the request limit, preserving the historical testable API."""
        if self.max_body_size_bytes is not None:
            return self.max_body_size_bytes
        try:
            max_file_mb = int(request.app.state.config.file_handling.max_file_size_mb)
        except (AttributeError, TypeError, ValueError):
            max_file_mb = 100
        return max_file_mb * 1024 * 1024 + self.size_headroom_bytes

    def _max_content_length_from_scope(self, scope: Scope) -> int:
        if self.max_body_size_bytes is not None:
            return self.max_body_size_bytes
        try:
            max_file_mb = int(scope["app"].state.config.file_handling.max_file_size_mb)
        except (AttributeError, KeyError, TypeError, ValueError):
            max_file_mb = 100
        return max_file_mb * 1024 * 1024 + self.size_headroom_bytes

    def _read_timeout_from_scope(self, scope: Scope) -> float:
        """Return the absolute per-stream request-body deadline."""
        if self.read_timeout_seconds is not None:
            return self.read_timeout_seconds
        try:
            timeout = float(scope["app"].state.config.server.read_timeout_seconds)
        except (AttributeError, KeyError, TypeError, ValueError):
            timeout = 30.0
        return timeout if timeout > 0 else 30.0

    @staticmethod
    async def _respond(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        request = Request(scope, receive=receive)
        try:
            account_route_validation_failure(request, route_aware=False)
        except RateLimitExceeded as rate_limit_error:
            response = _rate_limit_exceeded_handler(request, rate_limit_error)
        else:
            response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        method = str(scope.get("method", "GET")).upper()
        maximum = self._max_content_length_from_scope(scope)

        raw_headers = [
            (name.lower(), value) for name, value in scope.get("headers", [])
        ]
        content_length_headers = [
            value for name, value in raw_headers if name == b"content-length"
        ]
        content_type_headers = [
            value for name, value in raw_headers if name == b"content-type"
        ]
        transfer_encoding_headers = [
            value for name, value in raw_headers if name == b"transfer-encoding"
        ]
        has_transfer_encoding = bool(transfer_encoding_headers)
        if (
            len(content_length_headers) > 1
            or len(content_type_headers) > 1
            or len(transfer_encoding_headers) > 1
            or (content_length_headers and has_transfer_encoding)
            or (
                has_transfer_encoding
                and (
                    str(scope.get("http_version", "")).startswith("2")
                    or transfer_encoding_headers[0].strip().lower() != b"chunked"
                )
            )
        ):
            await self._respond(scope, receive, send, 400, "Ambiguous request headers")
            return

        content_length = headers.get("content-length")
        declared_length = 0
        if content_length is not None:
            if len(content_length) > 20 or not re.fullmatch(r"[0-9]+", content_length):
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Length header"
                )
                return
            declared_length = int(content_length)
            if declared_length > maximum:
                _rejection_warning_sampler.warning(
                    logger, "Rejected request with oversized declared body"
                )
                await self._respond(
                    scope,
                    receive,
                    send,
                    413,
                    f"Request too large. Maximum size: {maximum} bytes",
                )
                return

        # POST is the application's only body-bearing method. Reject advertised
        # bodies on every other method before routing so unsupported methods
        # cannot become bandwidth sinks or create proxy/backend framing
        # differentials.
        if method != "POST" and (declared_length > 0 or has_transfer_encoding):
            await self._respond(
                scope, receive, send, 400, "Request body is not allowed for this method"
            )
            return

        if method == "POST":
            content_type = headers.get("content-type", "")
            if len(content_type.encode("latin-1")) > self.MAX_CONTENT_TYPE_BYTES or any(
                ord(character) < 32 or ord(character) == 127
                for character in content_type
            ):
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Type header"
                )
                return
            try:
                parsed_type, parsed_parameters = parse_options_header(content_type)
                base_content_type = parsed_type.decode("ascii").lower()
            except (AttributeError, UnicodeDecodeError, ValueError):
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Type header"
                )
                return
            if base_content_type not in self.ALLOWED_CONTENT_TYPES:
                _rejection_warning_sampler.warning(
                    logger, "Rejected request with unsupported content type"
                )
                await self._respond(
                    scope, receive, send, 415, "Unsupported content type"
                )
                return
            if base_content_type != "multipart/form-data":
                maximum = min(maximum, self.MAX_NON_MULTIPART_BODY_BYTES)
                if content_length is not None and int(content_length) > maximum:
                    _rejection_warning_sampler.warning(
                        logger, "Rejected oversized non-multipart request body"
                    )
                    await self._respond(
                        scope,
                        receive,
                        send,
                        413,
                        f"Request too large. Maximum size: {maximum} bytes",
                    )
                    return
            if base_content_type == "multipart/form-data":
                if _header_parameter_count(content_type, "boundary") != 1:
                    boundary_bytes = b""
                else:
                    boundary = parsed_parameters.get(b"boundary")
                    boundary_bytes = boundary if isinstance(boundary, bytes) else b""
                if (
                    not boundary_bytes
                    or len(boundary_bytes) > self.MAX_MULTIPART_BOUNDARY_BYTES
                    or any(byte < 32 or byte >= 127 for byte in boundary_bytes)
                ):
                    await self._respond(
                        scope, receive, send, 400, "Invalid multipart boundary"
                    )
                    return

        body_complete = False
        body_deadline = (
            asyncio.get_running_loop().time() + self._read_timeout_from_scope(scope)
        )

        async def receive_before_deadline() -> Message:
            """Receive until end-of-body without allowing H2 trickle resets."""
            nonlocal body_complete
            if body_complete:
                # Long-lived response streams still use receive to observe a
                # disconnect. The request-body deadline no longer applies once
                # the peer supplied the body's terminal marker.
                return await receive()
            if asyncio.get_running_loop().time() >= body_deadline:
                raise RequestBodyReadTimeout(
                    "request body exceeded its absolute receive deadline"
                )
            try:
                async with asyncio.timeout_at(body_deadline):
                    message = await receive()
            except TimeoutError:
                raise RequestBodyReadTimeout(
                    "request body exceeded its absolute receive deadline"
                ) from None
            if message["type"] == "http.disconnect" or (
                message["type"] == "http.request"
                and not message.get("more_body", False)
            ):
                body_complete = True
            return message

        downstream_receive: Receive = receive_before_deadline
        if method != "POST":
            # Content-Length is optional on HTTP/2, so headers alone cannot
            # prove a nominally bodyless request has no DATA frames. Consume
            # and replay the first ASGI message; any bytes or continuation flag
            # fail closed before the router runs. Hypercorn's read timeout is
            # connection-wide, so multiplexed traffic on another HTTP/2 stream
            # cannot be allowed to keep this receive pending indefinitely.
            try:
                first_message = await downstream_receive()
            except RequestBodyReadTimeout:
                _rejection_warning_sampler.warning(
                    logger, "Timed out waiting for a bodyless request to end"
                )
                await self._respond(
                    scope,
                    receive,
                    send,
                    408,
                    "Request body read timed out",
                )
                return
            if first_message["type"] == "http.request" and (
                first_message.get("body", b"") or first_message.get("more_body", False)
            ):
                await self._respond(
                    scope,
                    receive,
                    send,
                    400,
                    "Request body is not allowed for this method",
                )
                return
            first_message_pending = True

            async def replay_first_message() -> Message:
                nonlocal first_message_pending
                if first_message_pending:
                    first_message_pending = False
                    return first_message
                return await receive_before_deadline()

            downstream_receive = replay_first_message

        # CORSMiddleware may answer a valid preflight without routing to a
        # decorated endpoint. Charge that short-circuit to the bounded IP-only
        # early-request budget after body validation, exactly once.
        if (
            method == "OPTIONS"
            and headers.get("origin")
            and headers.get("access-control-request-method")
        ):
            preflight_request = Request(scope, receive=downstream_receive)
            try:
                account_route_validation_failure(preflight_request, route_aware=False)
            except RateLimitExceeded as rate_limit_error:
                response = _rate_limit_exceeded_handler(
                    preflight_request, rate_limit_error
                )
                await response(scope, downstream_receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await downstream_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    raise RequestBodyTooLarge(maximum)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyReadTimeout:
            _rejection_warning_sampler.warning(
                logger, "Terminated request after its body receive deadline"
            )
            await self._respond(
                scope,
                receive,
                send,
                408,
                "Request body read timed out",
            )
        except RequestBodyTooLarge:
            _rejection_warning_sampler.warning(
                logger, "Terminated request after streamed body exceeded limit"
            )
            await self._respond(
                scope,
                receive,
                send,
                413,
                f"Request too large. Maximum size: {maximum} bytes",
            )
