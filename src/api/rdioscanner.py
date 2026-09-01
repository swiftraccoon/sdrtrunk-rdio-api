"""RdioScanner API endpoint implementation."""

import asyncio
import hmac
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from ..database.operations import DatabaseOperations
from ..exceptions import FileSizeError
from ..middleware.rate_limiter import get_active_limits, get_limiter
from ..middleware.validation import RequestBodyReadTimeout, RequestBodyTooLarge
from ..models.api_models import CallUploadResponse, RdioScannerUpload
from ..security.auth import api_key_allows_client_ip
from ..security.keys import stable_api_key_identifier
from ..security.logging import WarningSampler
from ..utils.file_handler import FileDeletionResult, FileHandler
from ..utils.network import get_client_ip, network_abuse_identity
from ..utils.storage_quota import (
    CapacityUnavailable,
    StorageCapacity,
    UploadCapacityReservation,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])

# Get the limiter instance
limiter = get_limiter()
security_warning_sampler = WarningSampler(
    maximum_per_window=10,
    window_seconds=60.0,
)

MAX_FORM_FILES = 1
MAX_FORM_FIELDS = 32
MAX_FORM_PART_BYTES = 16 * 1024
MAX_FORM_PART_HEADER_BYTES = 8 * 1024
MAX_CONCURRENT_UPLOADS = 8
MAX_CONCURRENT_UPLOADS_PER_IP = 2
UPLOAD_PARSE_TIMEOUT_SECONDS = 120.0
DEFAULT_FIELD_BYTES = 1024
FIELD_BYTE_LIMITS = {
    "key": 512,
    "system": 10,
    "dateTime": 20,
    "frequency": 20,
    "talkgroup": 12,
    "source": 12,
    "systemLabel": 255,
    "talkgroupLabel": 255,
    "talkgroupGroup": 255,
    "talkerAlias": 255,
    "patches": 4096,
    "frequencies": 4096,
    "sources": 4096,
    "talkgroupTag": 255,
    "test": 8,
}
AUDIT_TEXT_LIMITS = {
    "client_ip": 45,
    "system_id": 50,
    "api_key_used": 100,
    "user_agent": 500,
    "filename": 255,
    "content_type": 100,
    "error_message": 512,
}


@dataclass(frozen=True, slots=True)
class _ThreadOutcome:
    """Completed thread result plus cancellation observed by its waiter."""

    value: Any = None
    error: BaseException | None = None
    cancelled: bool = False


class UploadConcurrencyGate:
    """Fail-fast, per-application upload admission counter."""

    def __init__(self, global_limit: int, per_ip_limit: int) -> None:
        if global_limit < 1 or per_ip_limit < 1:
            raise ValueError("Upload concurrency limits must be positive")
        self.global_limit = global_limit
        self.per_ip_limit = per_ip_limit
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_by_ip: dict[str, int] = {}

    def try_acquire(self, client_ip: str) -> bool:
        """Claim a slot without waiting or allocating unbounded state."""
        client_identity = network_abuse_identity(client_ip)
        with self._lock:
            active_for_ip = self._active_by_ip.get(client_identity, 0)
            if (
                self._active_total >= self.global_limit
                or active_for_ip >= self.per_ip_limit
            ):
                return False
            self._active_total += 1
            self._active_by_ip[client_identity] = active_for_ip + 1
            return True

    def release(self, client_ip: str) -> None:
        """Release a previously claimed slot synchronously."""
        client_identity = network_abuse_identity(client_ip)
        with self._lock:
            active_for_ip = self._active_by_ip.get(client_identity, 0)
            if active_for_ip < 1 or self._active_total < 1:
                raise RuntimeError("Upload concurrency slot was not acquired")
            self._active_total -= 1
            if active_for_ip == 1:
                del self._active_by_ip[client_identity]
            else:
                self._active_by_ip[client_identity] = active_for_ip - 1

    @property
    def active_total(self) -> int:
        with self._lock:
            return self._active_total


_UPLOAD_GATE_INITIALIZATION_LOCK = threading.Lock()


def _get_upload_concurrency_gate(app: Any) -> UploadConcurrencyGate:
    """Return one concurrency gate per FastAPI application instance."""
    state = app.state
    gate = getattr(state, "_rdio_upload_concurrency_gate", None)
    if isinstance(gate, UploadConcurrencyGate):
        return gate
    with _UPLOAD_GATE_INITIALIZATION_LOCK:
        gate = getattr(state, "_rdio_upload_concurrency_gate", None)
        if not isinstance(gate, UploadConcurrencyGate):
            gate = UploadConcurrencyGate(
                MAX_CONCURRENT_UPLOADS, MAX_CONCURRENT_UPLOADS_PER_IP
            )
            state._rdio_upload_concurrency_gate = gate
    return gate


def _get_upload_parse_timeout(app: Any) -> float:
    """Return a test/deployment-overridable total multipart parse deadline."""
    configured = getattr(
        app.state, "rdio_upload_parse_timeout_seconds", UPLOAD_PARSE_TIMEOUT_SECONDS
    )
    try:
        timeout = float(configured)
    except (TypeError, ValueError):
        return UPLOAD_PARSE_TIMEOUT_SECONDS
    return timeout if timeout > 0 else UPLOAD_PARSE_TIMEOUT_SECONDS


async def _complete_thread_call(
    function: Callable[..., Any], *args: Any, **kwargs: Any
) -> _ThreadOutcome:
    """Finish a mutating thread call before allowing cancellation to escape.

    Cancelling ``asyncio.to_thread`` only cancels its asyncio waiter; the
    underlying function continues. Shield it, consume each cancellation
    request while it finishes, and return that fact to the caller so state can
    be recorded or compensated before a fresh ``CancelledError`` is raised.
    """
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    waiter = asyncio.current_task()
    cancellation_seen = False

    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancellation_seen = True
            if waiter is not None:
                waiter.uncancel()
        except BaseException:
            # The operation's exception is collected below after the task has
            # reached a terminal state.
            break

    try:
        return _ThreadOutcome(value=operation.result(), cancelled=cancellation_seen)
    except BaseException as exc:
        return _ThreadOutcome(error=exc, cancelled=cancellation_seen)


async def _complete_async_call(operation_awaitable: Awaitable[Any]) -> _ThreadOutcome:
    """Join an async cleanup operation while preserving cancellation intent."""
    operation = asyncio.ensure_future(operation_awaitable)
    waiter = asyncio.current_task()
    cancellation_seen = False

    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancellation_seen = True
            if waiter is not None:
                waiter.uncancel()
        except BaseException:
            break

    try:
        return _ThreadOutcome(value=operation.result(), cancelled=cancellation_seen)
    except BaseException as exc:
        return _ThreadOutcome(error=exc, cancelled=cancellation_seen)


def _raise_thread_failure(outcome: _ThreadOutcome, operation_name: str) -> None:
    """Raise a thread failure, preserving an earlier cancellation signal."""
    if outcome.error is None:
        return
    if outcome.cancelled:
        logger.error(
            "%s failed while restoring consistency after cancellation (%s)",
            operation_name,
            _safe_text(type(outcome.error).__name__, 100),
        )
        raise asyncio.CancelledError from None
    raise outcome.error


def _safe_text(value: Any, maximum: int) -> str:
    """Bound text and neutralize log/audit control characters."""
    cleaned = "".join(
        (
            "_"
            if character in "\r\n" or unicodedata.category(character).startswith("C")
            else character
        )
        for character in str(value)
    )
    return cleaned[:maximum]


async def _log_upload_attempt_safely(db_ops: DatabaseOperations, **values: Any) -> None:
    """Write a bounded audit event without changing the request outcome."""
    bounded = dict(values)
    for field, maximum in AUDIT_TEXT_LIMITS.items():
        if bounded.get(field) is not None:
            bounded[field] = _safe_text(bounded[field], maximum)
    outcome = await _complete_thread_call(db_ops.log_upload_attempt, **bounded)
    if outcome.error is not None:
        logger.warning("Unable to persist bounded upload audit event")
    if outcome.cancelled:
        raise asyncio.CancelledError


def _bounded_form_value(name: str, value: str) -> str:
    maximum = FIELD_BYTE_LIMITS.get(name, DEFAULT_FIELD_BYTES)
    if len(value.encode("utf-8")) > maximum:
        raise HTTPException(
            status_code=400, detail=f"Form field '{name[:64]}' is too long"
        )
    return value


def _parse_integer(
    form_data: dict[str, str], name: str, *, required: bool = False
) -> int | None:
    raw = form_data.get(name)
    if raw is None or raw == "":
        if required:
            raise HTTPException(
                status_code=400, detail=f"Missing required field: {name}"
            )
        return None
    if not re.fullmatch(r"[+-]?[0-9]+", raw):
        raise HTTPException(status_code=400, detail=f"Invalid upload data: {name}")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid upload data: {name}"
        ) from None


async def _parse_upload_form(
    request: Request, max_file_bytes: int
) -> tuple[FormData, dict[str, str], UploadFile | None]:
    """Parse a bounded form once; parser failures always fail closed."""
    try:
        content_type = request.headers.get("content-type", "")
        if content_type.partition(";")[0].strip().lower() == "multipart/form-data":
            parsed_form = await _BoundedMultiPartParser(
                request.headers,
                request.stream(),
                max_files=MAX_FORM_FILES,
                max_fields=MAX_FORM_FIELDS,
                max_part_size=MAX_FORM_PART_BYTES,
                max_file_size=max_file_bytes,
                max_header_size=MAX_FORM_PART_HEADER_BYTES,
            ).parse()
        else:
            parsed_form = await request.form(
                max_files=MAX_FORM_FILES,
                # The transport middleware already caps this entire body at
                # 64 KiB. Let the explicit unique-name loop below enforce the
                # protocol field cardinality consistently across Starlette
                # versions (older FormParser releases ignored max_fields).
                max_fields=float("inf"),
                max_part_size=MAX_FORM_PART_BYTES,
            )
    except (RequestBodyReadTimeout, RequestBodyTooLarge):
        raise
    except (StarletteHTTPException, MultiPartException):
        raise HTTPException(
            status_code=400, detail="Invalid multipart form data"
        ) from None

    fields: dict[str, str] = {}
    audio: UploadFile | None = None
    seen_names: set[str] = set()
    try:
        for raw_name, value in parsed_form.multi_items():
            name = _safe_text(raw_name, 64)
            if (
                name != raw_name
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name)
                or len(name.encode("ascii")) > 64
            ):
                raise HTTPException(status_code=400, detail="Invalid form field name")
            if name in seen_names:
                raise HTTPException(status_code=400, detail="Duplicate form field")
            if len(seen_names) >= MAX_FORM_FIELDS:
                raise HTTPException(status_code=400, detail="Too many form fields")
            seen_names.add(name)
            if isinstance(value, UploadFile):
                if name != "audio" or audio is not None:
                    raise HTTPException(
                        status_code=400, detail="Unexpected uploaded file"
                    )
                filename = value.filename or ""
                if not filename or len(filename.encode("utf-8")) > 255:
                    raise HTTPException(
                        status_code=400, detail="Invalid uploaded filename"
                    )
                content_type = value.content_type or ""
                if len(content_type.encode("utf-8")) > 100:
                    raise HTTPException(
                        status_code=400, detail="Invalid upload content type"
                    )
                audio = value
            else:
                if not isinstance(value, str):
                    raise HTTPException(status_code=400, detail="Invalid form field")
                fields[name] = _bounded_form_value(name, value)
    except Exception:
        await parsed_form.close()
        raise
    return parsed_form, fields, audio


class _BoundedMultiPartParser(MultiPartParser):
    """Starlette parser with explicit file-part and part-header limits."""

    def __init__(
        self,
        *args: Any,
        max_file_size: int,
        max_header_size: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_file_size = max_file_size
        self.max_header_size = max_header_size
        self._current_part_size = 0
        self._current_header_size = 0

    def on_part_begin(self) -> None:
        self._current_part_size = 0
        self._current_header_size = 0
        super().on_part_begin()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self._current_part_size += end - start
        maximum = (
            self.max_file_size
            if self._current_part.file is not None
            else self.max_part_size
        )
        if self._current_part_size > maximum:
            if self._current_part.file is not None:
                raise FileSizeError("Uploaded file exceeds configured maximum")
            raise MultiPartException("Multipart part exceeded its configured limit")
        super().on_part_data(data, start, end)

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._record_header_bytes(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._record_header_bytes(end - start)
        super().on_header_value(data, start, end)

    def _record_header_bytes(self, count: int) -> None:
        self._current_header_size += count
        if self._current_header_size > self.max_header_size:
            raise MultiPartException("Multipart part headers are too large")

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            # Starlette closes these on multipart/OSError failures. Also close
            # them when our ASGI byte limiter aborts the request or the task is
            # cancelled, so rolled-to-disk spools never leak.
            for temporary_file in self._files_to_close_on_error:
                temporary_file.close()
            raise


def get_client_info(
    request: Request, trusted_proxies: list[str] | None = None
) -> tuple[str, str]:
    """Extract a proxy-chain-safe client IP and bounded user agent."""
    client_ip = get_client_ip(request, trusted_proxies or ())
    user_agent = _safe_text(request.headers.get("user-agent", "unknown"), 500)
    return client_ip, user_agent


def validate_api_key(
    config: Any, key: str, system: str, client_ip: str
) -> tuple[bool, str | None]:
    """Validate API key with IP and system restrictions.

    Returns:
        (is_valid, api_key_id)
    """
    # Empty credentials are open only behind the explicit unsafe opt-in. The
    # app also enforces this at startup; keeping the decision here prevents a
    # direct/embedded caller from bypassing that guard.
    if not config.security.api_keys:
        return bool(config.security.allow_unauthenticated_uploads), None

    # Compare every configured key so a key's position does not create an
    # observable early-return timing signal.
    matched_key = None
    candidate_bytes = key.encode("utf-8")
    for api_key_config in config.security.api_keys:
        if hmac.compare_digest(api_key_config.key.encode("utf-8"), candidate_bytes):
            matched_key = api_key_config
    if matched_key is None:
        return False, None

    api_key_id = stable_api_key_identifier(matched_key)
    if not api_key_allows_client_ip(matched_key, client_ip):
        # The caller emits one generic sampled warning for every credential
        # failure. Avoid policy-specific synchronous logging here: an extra
        # log operation would turn a denied-but-valid key into an oracle.
        return False, None

    if matched_key.allowed_systems and system not in matched_key.allowed_systems:
        return False, None

    return True, api_key_id


@router.post(
    "/api/call-upload",
    response_model=CallUploadResponse,
    summary="Upload Radio Call",
    description="""Upload a radio call recording with metadata from SDRTrunk.

    This endpoint accepts multipart/form-data with audio file and metadata fields.
    Compatible with the RdioScanner protocol used by SDRTrunk.
    """,
    responses={
        200: {
            "description": "Call uploaded successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "callId": 12345,
                        "message": "Call uploaded successfully",
                    }
                },
                "text/plain": {"example": "ok"},
            },
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Missing required fields: system and dateTime"
                    }
                }
            },
        },
        401: {
            "description": "Invalid API key",
            "content": {"application/json": {"example": {"detail": "Invalid API key"}}},
        },
        413: {
            "description": "File too large",
            "content": {
                "application/json": {
                    "example": {"detail": "File size exceeds maximum allowed"}
                }
            },
        },
        429: {
            "description": "Rate limit exceeded",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Rate limit exceeded. Please try again later."
                    }
                }
            },
        },
        507: {
            "description": "Insufficient storage capacity",
            "content": {
                "application/json": {
                    "example": {"detail": "Insufficient storage capacity"}
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
)
@limiter.limit(get_active_limits)
async def upload_call(request: Request) -> Response:
    """Handle RdioScanner call upload from SDRTrunk.

    Accepts multipart/form-data with the following fields:

    **Required fields:**
    - key: API key for authentication
    - system: System ID (numeric string)
    - dateTime: Unix timestamp in seconds
    - audio: Audio file (MP3 by default; see file_handling.accepted_formats)

    **Optional metadata fields:**
    - frequency: Frequency in Hz
    - talkgroup: Talkgroup ID
    - source: Source radio ID
    - systemLabel: Human-readable system name
    - talkgroupLabel: Human-readable talkgroup name
    - talkgroupGroup: Talkgroup category/group
    - talkerAlias: Alias of the talking radio
    - patches: Comma-separated list of patched talkgroups
    - frequencies: Comma-separated list of frequencies
    - sources: Comma-separated list of source IDs
    - talkgroupTag: Additional talkgroup tag
    - test: Test mode flag (1 for test)
    """
    start_time = time.time()
    config = request.app.state.config
    db_ops: DatabaseOperations = request.app.state.db_ops
    file_handler: FileHandler = request.app.state.file_handler
    storage_capacity: StorageCapacity = request.app.state.storage_capacity
    client_ip, user_agent = get_client_info(request, config.security.trusted_proxies)
    upload_gate = _get_upload_concurrency_gate(request.app)
    if not upload_gate.try_acquire(client_ip):
        security_warning_sampler.warning(
            logger,
            "Rejected upload because the concurrency gate is saturated for %s",
            _safe_text(client_ip, 45),
        )
        raise HTTPException(
            status_code=503,
            detail="Too many concurrent uploads",
            headers={"Retry-After": "1"},
        )

    parsed_form: FormData | None = None
    temp_path: Path | None = None
    stored_path: str | None = None
    stored_lease_path: str | None = None
    call_committed = False
    authenticated = False
    system = ""
    api_key_id: str | None = None
    capacity_reservation: UploadCapacityReservation | None = None

    try:
        capacity_outcome = await _complete_thread_call(storage_capacity.reserve_upload)
        if capacity_outcome.error is not None:
            if capacity_outcome.cancelled:
                raise asyncio.CancelledError
            if isinstance(capacity_outcome.error, CapacityUnavailable):
                raise HTTPException(
                    status_code=507, detail="Insufficient storage capacity"
                ) from None
            raise capacity_outcome.error
        if not isinstance(capacity_outcome.value, UploadCapacityReservation):
            raise RuntimeError("Storage capacity reservation returned invalid state")
        capacity_reservation = capacity_outcome.value
        if capacity_outcome.cancelled:
            raise asyncio.CancelledError

        try:
            async with asyncio.timeout(_get_upload_parse_timeout(request.app)):
                parsed_form, form_data, audio = await _parse_upload_form(
                    request, file_handler.max_file_size_bytes
                )
        except TimeoutError:
            raise HTTPException(
                status_code=408, detail="Upload form parsing timed out"
            ) from None
        capacity_reservation.complete_spool()
        logger.debug("Parsed bounded upload fields: %s", sorted(form_data))

        key = form_data.get("key", "")
        system = form_data.get("system", "")
        test_value = form_data.get("test")

        # Validate path/log-sensitive data before authentication or logging. An
        # invalid key must not let an attacker persist an oversized/raw system.
        if system and not re.fullmatch(r"[0-9]{1,10}", system):
            raise HTTPException(status_code=400, detail="Invalid upload data: system")

        is_valid, api_key_id = validate_api_key(config, key, system, client_ip)
        if not is_valid:
            # Do not create an attacker-amplifiable database row for every
            # unauthenticated guess. The bounded rotating log and rate-limit
            # counters retain operational visibility without durable DB growth.
            security_warning_sampler.warning(
                logger,
                "Rejected upload with invalid credentials from %s",
                _safe_text(client_ip, 45),
            )
            raise HTTPException(status_code=401, detail="Invalid API key")
        authenticated = True

        logger.info(
            "Authenticated RdioScanner upload request from %s - %s",
            _safe_text(client_ip, 45),
            user_agent,
        )

        if test_value is not None:
            if _parse_integer(form_data, "test", required=True) != 1 or not system:
                raise HTTPException(status_code=400, detail="Invalid test request")
            logger.info("Test request from system %s", system)
            message = "incomplete call data: no talkgroup"
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    {"status": "ok", "message": message, "callId": "test"}
                )
            return PlainTextResponse(message)

        if not system or not form_data.get("dateTime"):
            error_message = "Missing required fields: system and dateTime"
            await _log_upload_attempt_safely(
                db_ops,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_message,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_message)

        if audio is None and config.processing.mode != "log_only":
            error_message = "Audio file is required"
            await _log_upload_attempt_safely(
                db_ops,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_message,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_message)

        safe_audio_filename = (
            file_handler.normalize_filename(audio.filename or "") if audio else None
        )
        normalized_content_type = (
            (audio.content_type or "").partition(";")[0].strip().lower()
            if audio
            else None
        )
        # SDRTrunk's RdioScannerBuilder omits a Content-Type header on the
        # audio part. Preserve protocol compatibility by assigning the sole
        # canonical type for an .mp3 filename; any explicitly supplied MIME is
        # still validated and rejected when it is not on the MP3 allowlist.
        if (
            audio is not None
            and not normalized_content_type
            and Path(audio.filename or "").suffix.lower() == ".mp3"
        ):
            normalized_content_type = "audio/mpeg"

        try:
            upload_timestamp = _parse_integer(form_data, "dateTime", required=True)
            if upload_timestamp is None:  # Narrow the required helper for typing.
                raise HTTPException(
                    status_code=400, detail="Missing required field: dateTime"
                )
            upload_data = RdioScannerUpload(
                key=key,
                system=system,
                dateTime=upload_timestamp,
                audio_filename=safe_audio_filename,
                audio_content_type=normalized_content_type,
                audio_size=audio.size if audio else None,
                frequency=_parse_integer(form_data, "frequency"),
                talkgroup=_parse_integer(form_data, "talkgroup"),
                source=_parse_integer(form_data, "source"),
                systemLabel=form_data.get("systemLabel"),
                talkgroupLabel=form_data.get("talkgroupLabel"),
                talkgroupGroup=form_data.get("talkgroupGroup"),
                talkerAlias=form_data.get("talkerAlias"),
                patches=form_data.get("patches"),
                frequencies=form_data.get("frequencies"),
                sources=form_data.get("sources"),
                talkgroupTag=form_data.get("talkgroupTag"),
                test=None,
            )
        except ValidationError as exc:
            errors = exc.errors()
            location = errors[0].get("loc", ()) if errors else ()
            field = _safe_text(".".join(str(part) for part in location), 64)
            error_message = f"Invalid upload data: {field or 'request'}"
            await _log_upload_attempt_safely(
                db_ops,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_message,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_message) from None

        if audio is not None:
            file_valid, file_error = await file_handler.validate_upload_file(
                audio.filename or "", audio, normalized_content_type
            )
            if not file_valid:
                error_message = file_error or "File validation failed"
                status_code = (
                    413
                    if audio.size is not None
                    and audio.size > file_handler.max_file_size_bytes
                    else 400
                )
                await _log_upload_attempt_safely(
                    db_ops,
                    client_ip=client_ip,
                    success=False,
                    system_id=system,
                    api_key_used=api_key_id,
                    user_agent=user_agent,
                    filename=safe_audio_filename,
                    file_size=audio.size,
                    content_type=normalized_content_type,
                    error_message=error_message,
                    response_code=status_code,
                )
                raise HTTPException(status_code=status_code, detail=error_message)

        capacity_outcome = await _complete_thread_call(
            capacity_reservation.claim_persistent
        )
        if capacity_outcome.error is not None:
            if capacity_outcome.cancelled:
                raise asyncio.CancelledError
            if isinstance(capacity_outcome.error, CapacityUnavailable):
                raise HTTPException(
                    status_code=507, detail="Insufficient storage capacity"
                ) from None
            raise capacity_outcome.error
        if capacity_outcome.cancelled:
            raise asyncio.CancelledError

        call_id: int | None = None
        if config.processing.mode == "log_only":
            database_outcome = await _complete_thread_call(
                db_ops.save_radio_call,
                upload_data,
                audio_file_path=None,
                upload_ip=client_ip,
                api_key_id=api_key_id,
            )
            _raise_thread_failure(database_outcome, "Database save")
            call_id = int(database_outcome.value)
            call_committed = True
            if database_outcome.cancelled:
                raise asyncio.CancelledError
            logger.info(
                "Logged call: System=%s, TG=%s, Freq=%s, Time=%s",
                system,
                upload_data.talkgroup,
                upload_data.frequency,
                upload_data.dateTime,
            )
        else:
            if (
                audio is not None
                and config.file_handling.storage.strategy == "filesystem"
            ):
                copy_outcome = await _complete_thread_call(
                    file_handler.save_upload_stream,
                    audio.filename or "",
                    audio.file,
                )
                _raise_thread_failure(copy_outcome, "Upload spool copy")
                if not isinstance(copy_outcome.value, Path):
                    raise RuntimeError("Upload spool copy returned an invalid path")
                temp_path = copy_outcome.value
                if copy_outcome.cancelled:
                    raise asyncio.CancelledError
                capacity_reservation.complete_custom_temp()

                validation_outcome = await _complete_thread_call(
                    file_handler.validate_file_path,
                    audio.filename or "",
                    temp_path,
                    normalized_content_type,
                )
                _raise_thread_failure(validation_outcome, "Temporary file validation")
                if not isinstance(validation_outcome.value, tuple):
                    raise RuntimeError("File validation returned an invalid result")
                file_valid, file_error = validation_outcome.value
                if validation_outcome.cancelled:
                    raise asyncio.CancelledError
                if not file_valid:
                    raise HTTPException(
                        status_code=400,
                        detail=file_error or "File validation failed",
                    )
                storage_outcome = await _complete_thread_call(
                    file_handler.store_file,
                    temp_path,
                    system,
                    datetime.fromtimestamp(upload_data.dateTime, tz=UTC),
                    upload_data.talkgroup,
                    upload_data.talkgroupLabel,
                    upload_data.frequency,
                    upload_data.source,
                    upload_data.talkerAlias,
                    upload_data.systemLabel,
                    on_destination_reserved=db_ops.stage_file_for_storage,
                    capacity_reservation=capacity_reservation,
                )
                _raise_thread_failure(storage_outcome, "File storage")
                if not isinstance(storage_outcome.value, Path):
                    raise RuntimeError("File storage returned an invalid path")
                stored_path_obj = storage_outcome.value
                temp_path = None
                stored_path = file_handler.storage_reference(stored_path_obj)
                stored_lease_path = stored_path
                if storage_outcome.cancelled:
                    raise asyncio.CancelledError

            if stored_path is not None:
                file_handler.heartbeat_storage_lease(stored_path)
            database_outcome = await _complete_thread_call(
                db_ops.save_radio_call,
                upload_data,
                audio_file_path=stored_path,
                upload_ip=client_ip,
                api_key_id=api_key_id,
                require_staged_file=stored_path is not None,
            )
            _raise_thread_failure(database_outcome, "Database save")
            call_id = int(database_outcome.value)
            call_committed = True
            if database_outcome.cancelled:
                raise asyncio.CancelledError
            logger.info(
                "Stored call %s: System=%s, TG=%s",
                call_id,
                system,
                upload_data.talkgroup,
            )

        await _log_upload_attempt_safely(
            db_ops,
            client_ip=client_ip,
            success=True,
            system_id=system,
            api_key_used=api_key_id,
            user_agent=user_agent,
            filename=safe_audio_filename,
            file_size=audio.size if audio else None,
            content_type=normalized_content_type,
            response_code=200,
            processing_time_ms=(time.time() - start_time) * 1000,
        )

        response_data = CallUploadResponse(
            status="ok",
            message="Call received and processed",
            callId=str(call_id) if call_id is not None else None,
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(response_data.model_dump())
        return PlainTextResponse("Call imported successfully.")

    except RequestBodyTooLarge:
        raise
    except FileSizeError:
        if authenticated:
            await _log_upload_attempt_safely(
                db_ops,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message="Uploaded file exceeds configured limits",
                response_code=413,
                processing_time_ms=(time.time() - start_time) * 1000,
            )
        else:
            # Oversized multipart data can be rejected before its form key is
            # available. Keep that unauthenticated path out of the durable
            # audit table and a bounded rotating log prevents disk amplification.
            security_warning_sampler.warning(
                logger,
                "Rejected oversized upload before credential validation from %s",
                _safe_text(client_ip, 45),
            )
        raise HTTPException(
            status_code=413, detail="Uploaded file exceeds configured limits"
        ) from None
    except HTTPException:
        raise
    except CapacityUnavailable:
        await _log_upload_attempt_safely(
            db_ops,
            client_ip=client_ip,
            success=False,
            system_id=system,
            api_key_used=api_key_id,
            user_agent=user_agent,
            error_message="Insufficient storage capacity",
            response_code=507,
            processing_time_ms=(time.time() - start_time) * 1000,
        )
        raise HTTPException(
            status_code=507, detail="Insufficient storage capacity"
        ) from None
    except Exception as exc:
        if authenticated:
            # Exception strings from database/filesystem libraries can embed query
            # parameters or paths. Log only the bounded class name here.
            logger.error(
                "Upload processing failed with an internal error (%s)",
                _safe_text(type(exc).__name__, 100),
            )
            await _log_upload_attempt_safely(
                db_ops,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message="Internal processing error",
                response_code=500,
                processing_time_ms=(time.time() - start_time) * 1000,
            )
        else:
            # Disconnects and unexpected parser failures happen before the
            # multipart key is trustworthy. Sampling avoids both rotating-log
            # and database amplification by anonymous clients.
            security_warning_sampler.warning(
                logger,
                "Upload parsing failed before credential validation from %s (%s)",
                _safe_text(client_ip, 45),
                _safe_text(type(exc).__name__, 100),
            )
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        finalization_cancelled = False
        try:
            if stored_path and not call_committed:
                # A database commit can succeed even when a later observer or
                # driver cleanup reports failure. Reconcile the durable state
                # before compensating so an ambiguous outcome can never leave
                # a committed RadioCall pointing at deleted audio. If this
                # bounded check itself fails, fail closed and leave the staged
                # deletion ledger to retry safely during maintenance.
                reference_outcome = await _complete_thread_call(
                    db_ops.get_referenced_audio_paths, [stored_path]
                )
                finalization_cancelled |= reference_outcome.cancelled
                safe_to_delete = False
                if reference_outcome.error is not None:
                    logger.error(
                        "Unable to reconcile an ambiguous database save; "
                        "stored upload compensation was deferred (%s)",
                        _safe_text(type(reference_outcome.error).__name__, 100),
                    )
                elif not isinstance(reference_outcome.value, set):
                    logger.error(
                        "Database save reconciliation returned invalid state; "
                        "stored upload compensation was deferred"
                    )
                elif stored_path in reference_outcome.value:
                    logger.error(
                        "Database save reported failure after committing; "
                        "preserving referenced stored audio"
                    )
                else:
                    safe_to_delete = True

                if safe_to_delete:
                    # The upload operation itself is no longer live. Drop its
                    # staging lease immediately before compensating; ordinary
                    # maintenance remains serialized by the capacity guard.
                    file_handler.release_storage_lease(stored_path)
                    cleanup_outcome = await _complete_thread_call(
                        file_handler.delete_file, stored_path
                    )
                    finalization_cancelled |= cleanup_outcome.cancelled
                    if cleanup_outcome.error is not None:
                        logger.error(
                            "Failed to compensate stored upload (%s)",
                            _safe_text(type(cleanup_outcome.error).__name__, 100),
                        )
                    elif isinstance(cleanup_outcome.value, FileDeletionResult):
                        if cleanup_outcome.value.status in {"deleted", "missing"}:
                            stored_path = None
                        elif cleanup_outcome.value.status in {"retry", "refused"}:
                            # The durable staging row remains available to retry or
                            # surface an unsafe/migrated path to the operator.
                            logger.error(
                                "Stored upload compensation was deferred (%s)",
                                cleanup_outcome.value.status,
                            )
                    else:
                        logger.error(
                            "Stored upload compensation returned invalid state"
                        )
            if temp_path is not None:
                cleanup_outcome = await _complete_thread_call(
                    file_handler.delete_temp_file, str(temp_path)
                )
                finalization_cancelled |= cleanup_outcome.cancelled
                if cleanup_outcome.error is not None:
                    logger.error(
                        "Failed to clean temporary upload (%s)",
                        _safe_text(type(cleanup_outcome.error).__name__, 100),
                    )
                elif not isinstance(
                    cleanup_outcome.value, FileDeletionResult
                ) or cleanup_outcome.value.status not in {"deleted", "missing"}:
                    logger.error("Temporary upload cleanup was not completed safely")
            if parsed_form is not None:
                close_outcome = await _complete_async_call(parsed_form.close())
                finalization_cancelled |= close_outcome.cancelled
                if close_outcome.error is not None:
                    logger.error(
                        "Failed to close multipart spool (%s)",
                        _safe_text(type(close_outcome.error).__name__, 100),
                    )
        finally:
            # Synchronous release cannot itself be interrupted by task
            # cancellation and runs even if a cleanup operation fails.
            try:
                try:
                    if stored_lease_path is not None:
                        file_handler.release_storage_lease(stored_lease_path)
                finally:
                    if capacity_reservation is not None:
                        capacity_reservation.release()
            finally:
                upload_gate.release(client_ip)
        if finalization_cancelled:
            raise asyncio.CancelledError
