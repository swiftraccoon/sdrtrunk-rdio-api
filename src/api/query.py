"""Query API endpoints for retrieving radio call data."""

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import fastapi
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from ..database.operations import DatabaseOperations, ExpensiveQueryTimeout
from ..middleware.rate_limiter import get_active_limits, get_limiter
from ..security.auth import ReadPrincipal, authenticate_read_request
from ..security.text import sanitize_log_value
from ..utils.network import get_client_ip, network_abuse_identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
limiter = get_limiter()
MAX_CONCURRENT_AUDIO_STREAMS = 32
MAX_CONCURRENT_AUDIO_STREAMS_PER_PRINCIPAL = 4
MAX_CONCURRENT_EXPENSIVE_READS = 4
MAX_CONCURRENT_EXPENSIVE_READS_PER_PRINCIPAL = 1
MAX_CALL_QUERY_PAGE = 1_000
AUDIO_STREAM_MAX_LIFETIME_SECONDS = 15 * 60.0
AUDIO_STREAM_CLOSE_GRACE_SECONDS = 0.05


class _AudioStreamGate:
    """Bound pinned audio descriptors globally and per abuse principal."""

    def __init__(self, *, global_limit: int, per_principal_limit: int) -> None:
        if global_limit < 1 or per_principal_limit < 1:
            raise ValueError("audio stream limits must be positive")
        if per_principal_limit > global_limit:
            raise ValueError("per-principal audio stream limit exceeds global limit")
        self._global_limit = global_limit
        self._per_principal_limit = per_principal_limit
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_by_principal: dict[str, int] = {}

    def try_acquire(self, principal: str) -> bool:
        """Acquire one slot without waiting, or fail closed at either cap."""
        with self._lock:
            current = self._active_by_principal.get(principal, 0)
            if (
                self._active_total >= self._global_limit
                or current >= self._per_principal_limit
            ):
                return False
            self._active_total += 1
            self._active_by_principal[principal] = current + 1
            return True

    def release(self, principal: str) -> None:
        """Release one previously acquired slot."""
        with self._lock:
            current = self._active_by_principal.get(principal, 0)
            if current < 1 or self._active_total < 1:
                raise RuntimeError("audio stream gate release without acquisition")
            if current == 1:
                self._active_by_principal.pop(principal, None)
            else:
                self._active_by_principal[principal] = current - 1
            self._active_total -= 1


class _ExpensiveReadGate(_AudioStreamGate):
    """Bound concurrent whole-archive database reads without queueing workers."""


class _ExpensiveReadLease:
    """Idempotent ownership token for one expensive-read admission slot."""

    def __init__(self, gate: _ExpensiveReadGate, principal: str) -> None:
        self._gate = gate
        self._principal = principal
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._gate.release(self._principal)

    def __enter__(self) -> "_ExpensiveReadLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


_audio_gate_initialization_lock = threading.Lock()
_expensive_gate_initialization_lock = threading.Lock()
_audio_stream_cleanup_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_AUDIO_STREAMS,
    thread_name_prefix="audio-stream-close",
)


def _observe_audio_cleanup(future: Future[None]) -> None:
    """Consume cleanup failures without leaking a Future exception."""
    try:
        future.result()
    except Exception as exc:
        logger.error("Audio stream finalizer failed (%s)", type(exc).__name__)


def _get_audio_stream_gate(request: Request) -> _AudioStreamGate:
    """Return the process-local gate belonging to this application instance."""
    gate = getattr(request.app.state, "audio_stream_gate", None)
    if gate is not None:
        return cast(_AudioStreamGate, gate)
    with _audio_gate_initialization_lock:
        gate = getattr(request.app.state, "audio_stream_gate", None)
        if gate is None:
            gate = _AudioStreamGate(
                global_limit=MAX_CONCURRENT_AUDIO_STREAMS,
                per_principal_limit=MAX_CONCURRENT_AUDIO_STREAMS_PER_PRINCIPAL,
            )
            request.app.state.audio_stream_gate = gate
    return cast(_AudioStreamGate, gate)


def _read_principal_identity(request: Request, principal: ReadPrincipal) -> str:
    """Use a credential identity, or a normalized network for anonymous reads."""
    if principal.authenticated:
        if not principal.key_id:
            raise RuntimeError("authenticated read principal has no identifier")
        return f"authenticated:{principal.key_id}"
    config = request.app.state.config
    client_ip = get_client_ip(request, config.security.trusted_proxies)
    return f"client:{network_abuse_identity(client_ip)}"


def _get_expensive_read_gate(request: Request) -> _ExpensiveReadGate:
    """Return the nonblocking expensive-query gate for this application."""
    gate = getattr(request.app.state, "expensive_read_gate", None)
    if isinstance(gate, _ExpensiveReadGate):
        return gate
    with _expensive_gate_initialization_lock:
        gate = getattr(request.app.state, "expensive_read_gate", None)
        if not isinstance(gate, _ExpensiveReadGate):
            gate = _ExpensiveReadGate(
                global_limit=MAX_CONCURRENT_EXPENSIVE_READS,
                per_principal_limit=MAX_CONCURRENT_EXPENSIVE_READS_PER_PRINCIPAL,
            )
            request.app.state.expensive_read_gate = gate
    return gate


def _acquire_expensive_read(
    request: Request, principal: ReadPrincipal
) -> _ExpensiveReadLease:
    """Acquire immediately or reject before occupying a database worker."""
    identity = _read_principal_identity(request, principal)
    gate = _get_expensive_read_gate(request)
    if not gate.try_acquire(identity):
        raise HTTPException(
            status_code=503,
            detail="Expensive query capacity reached; retry later",
            headers={"Retry-After": "1"},
        )
    return _ExpensiveReadLease(gate, identity)


def _expensive_query_timeout() -> HTTPException:
    """Map a server-side SQLite execution budget to a retryable response."""
    return HTTPException(
        status_code=503,
        detail="Query execution deadline exceeded; narrow the request and retry",
        headers={"Retry-After": "1"},
    )


class _ClosingStreamingResponse(StreamingResponse):
    """Close a pinned file on disconnect or at the absolute response deadline."""

    def __init__(
        self,
        *args: Any,
        close_callback: Callable[[], None],
        response_timeout_seconds: float = AUDIO_STREAM_MAX_LIFETIME_SECONDS,
        close_grace_seconds: float = AUDIO_STREAM_CLOSE_GRACE_SECONDS,
        **kwargs: Any,
    ):
        if response_timeout_seconds <= 0 or close_grace_seconds <= 0:
            raise ValueError("response and close timeouts must be positive")
        super().__init__(*args, **kwargs)
        self._close_callback = close_callback
        self._response_timeout_seconds = response_timeout_seconds
        self._close_grace_seconds = close_grace_seconds

    async def _close_without_blocking_event_loop(self) -> None:
        """Run potentially lock-blocked cleanup without stalling other requests."""
        cleanup = _audio_stream_cleanup_executor.submit(self._close_callback)
        cleanup.add_done_callback(_observe_audio_cleanup)
        wrapped = asyncio.wrap_future(cleanup)
        try:
            async with asyncio.timeout(self._close_grace_seconds):
                await asyncio.shield(wrapped)
        except TimeoutError:
            # A pathological filesystem read can remain blocked in a worker
            # after its ASGI waiter is cancelled. The dedicated bounded pool
            # keeps that wait out of the event loop; the idempotent callback
            # retains the gate slot until the descriptor can actually close.
            pass
        except Exception:
            # The done callback above records only the bounded exception class.
            pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def tracked_send(message: Any) -> None:
            nonlocal response_started
            await send(message)
            if message.get("type") == "http.response.start":
                response_started = True

        try:
            try:
                async with asyncio.timeout(self._response_timeout_seconds):
                    await super().__call__(scope, receive, tracked_send)
            except TimeoutError:
                # Once headers have been sent there is no valid HTTP status to
                # substitute. Returning with an incomplete Content-Length lets
                # the server reset/close the response while the finalizer below
                # releases the descriptor and admission slot. Before response
                # start, preserve the timeout as an ordinary application error.
                if not response_started:
                    raise
        finally:
            # StreamingResponse does not guarantee that its background task or
            # a sync iterator's finalizer runs after an ASGI send failure.
            await self._close_without_blocking_event_loop()


# Query parameter definitions
date_from_query = Query(None, description="Start date for filtering (ISO 8601)")
date_to_query = Query(None, description="End date for filtering (ISO 8601)")
api_key_header = Header(
    None,
    alias="X-API-Key",
    description="API key used to authenticate and scope this read request",
)


class CallRecord(BaseModel):
    """Individual call record in query response."""

    id: int
    timestamp: datetime
    system_id: str
    system_label: str | None
    talkgroup_id: int | None
    talkgroup_label: str | None
    frequency: int | None
    source_id: int | None
    talker_alias: str | None
    audio_filename: str | None
    audio_size_bytes: int | None


class CallsQueryResponse(BaseModel):
    """Response for calls query endpoint."""

    calls: list[CallRecord]
    total: int
    page: int
    per_page: int
    total_pages: int


class SystemSummary(BaseModel):
    """System summary information."""

    system_id: str
    system_label: str | None
    total_calls: int
    first_seen: datetime | None
    last_seen: datetime | None
    top_talkgroups: dict[str, int]


class TalkgroupSummary(BaseModel):
    """Talkgroup summary information."""

    talkgroup_id: int
    talkgroup_label: str | None
    system_id: str
    total_calls: int
    last_heard: datetime | None


@router.get(
    "/api/calls",
    response_model=CallsQueryResponse,
    summary="Query Radio Calls",
    description="Query and filter stored radio calls with pagination support",
    responses={
        200: {
            "description": "Calls retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "calls": [
                            {
                                "id": 123,
                                "timestamp": "2024-12-06T12:34:56Z",
                                "system_id": "1",
                                "system_label": "Test System",
                                "talkgroup_id": 1001,
                                "talkgroup_label": "Police Dispatch",
                                "frequency": 853237500,
                                "source_id": 5678,
                                "talker_alias": "Unit Alpha",
                                "audio_filename": "20241206_123456_TG1001.mp3",
                                "audio_size_bytes": 45678,
                            }
                        ],
                        "total": 150,
                        "page": 1,
                        "per_page": 20,
                        "total_pages": 8,
                    }
                }
            },
        }
    },
)
@limiter.limit("120/minute")
@limiter.limit(get_active_limits)
def query_calls(
    request: Request,
    system_id: str | None = Query(
        None,
        description="Filter by system ID",
        min_length=1,
        max_length=10,
        pattern=r"^[0-9]+$",
    ),
    talkgroup_id: int | None = Query(
        None, description="Filter by talkgroup ID", ge=0, le=999_999_999
    ),
    source_id: int | None = Query(
        None, description="Filter by source radio ID", ge=0, le=999_999_999
    ),
    frequency: int | None = Query(
        None, description="Filter by frequency (Hz)", ge=1, le=6_000_000_000
    ),
    date_from: datetime | None = date_from_query,
    date_to: datetime | None = date_to_query,
    hours_ago: int | None = Query(
        None, description="Get calls from last N hours", ge=1, le=168
    ),
    page: int = Query(
        1,
        description="Page number (offset pagination is capped at 1,000 pages)",
        ge=1,
        le=MAX_CALL_QUERY_PAGE,
    ),
    per_page: int = Query(20, description="Items per page", ge=1, le=100),
    sort_by: str = Query(
        "timestamp",
        description="Sort field",
        pattern="^(timestamp|system_id|talkgroup_id|frequency)$",
    ),
    sort_order: str = Query("desc", description="Sort order", pattern="^(asc|desc)$"),
    _api_key: str | None = api_key_header,
) -> CallsQueryResponse:
    """Query radio calls with filtering and pagination.

    Supports filtering by:
    - System ID
    - Talkgroup ID
    - Source radio ID
    - Frequency
    - Date range or hours ago

    Results are paginated and sortable.
    """
    principal = authenticate_read_request(request)
    db_ops: DatabaseOperations = request.app.state.db_ops

    # Build filter criteria
    filters: dict[str, Any] = {}

    if system_id:
        filters["system_id"] = system_id
    if talkgroup_id is not None:
        filters["talkgroup_id"] = talkgroup_id
    if source_id is not None:
        filters["source_radio_id"] = source_id
    if frequency:
        filters["frequency"] = frequency

    # Handle date filtering
    if hours_ago:
        date_from = datetime.now(UTC) - timedelta(hours=hours_ago)
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to

    # Query database
    try:
        with _acquire_expensive_read(request, principal):
            results = db_ops.query_calls(
                filters=filters,
                page=page,
                per_page=per_page,
                sort_by=sort_by,
                sort_order=sort_order,
                allowed_systems=principal.allowed_systems,
            )

        # Convert to response model
        calls = []
        for record in results["calls"]:
            calls.append(
                CallRecord(
                    id=record["id"],
                    timestamp=record["call_timestamp"],
                    system_id=record["system_id"],
                    system_label=record.get("system_label"),
                    talkgroup_id=record.get("talkgroup_id"),
                    talkgroup_label=record.get("talkgroup_label"),
                    frequency=record.get("frequency"),
                    source_id=record.get("source_radio_id"),
                    talker_alias=record.get("talker_alias"),
                    audio_filename=record.get("audio_filename"),
                    audio_size_bytes=record.get("audio_size_bytes"),
                )
            )

        return CallsQueryResponse(
            calls=calls,
            total=results["total"],
            page=page,
            per_page=per_page,
            total_pages=results["total_pages"],
        )

    except HTTPException:
        raise
    except ExpensiveQueryTimeout:
        raise _expensive_query_timeout() from None
    except Exception as e:
        logger.error("Error querying calls: %s", sanitize_log_value(e))
        raise HTTPException(status_code=500, detail="Error querying calls") from e


@router.get(
    "/api/calls/{call_id}",
    response_model=CallRecord,
    summary="Get Call by ID",
    description="Retrieve a specific radio call by its ID",
)
@limiter.limit("300/minute")
@limiter.limit(get_active_limits)
def get_call(
    request: Request,
    call_id: int = fastapi.Path(..., ge=1, le=9_223_372_036_854_775_807),
    _api_key: str | None = api_key_header,
) -> CallRecord:
    """Get a specific radio call by ID."""
    principal = authenticate_read_request(request)
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        record = db_ops.get_call_by_id(
            call_id, allowed_systems=principal.allowed_systems
        )
        if not record:
            raise HTTPException(status_code=404, detail="Call not found")

        return CallRecord(
            id=record["id"],
            timestamp=record["call_timestamp"],
            system_id=record["system_id"],
            system_label=record.get("system_label"),
            talkgroup_id=record.get("talkgroup_id"),
            talkgroup_label=record.get("talkgroup_label"),
            frequency=record.get("frequency"),
            source_id=record.get("source_radio_id"),
            talker_alias=record.get("talker_alias"),
            audio_filename=record.get("audio_filename"),
            audio_size_bytes=record.get("audio_size_bytes"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error retrieving call %s: %s", call_id, sanitize_log_value(e))
        raise HTTPException(status_code=500, detail="Error retrieving call") from e


@router.get(
    "/api/systems",
    response_model=list[SystemSummary],
    summary="List Systems",
    description="Get a list of all systems with summary statistics",
)
@limiter.limit("30/minute")
@limiter.limit(get_active_limits)
def list_systems(
    request: Request,
    limit: int = Query(500, description="Maximum systems to return", ge=1, le=1000),
    _api_key: str | None = api_key_header,
) -> list[SystemSummary]:
    """List all systems with summary statistics."""
    principal = authenticate_read_request(request)
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        with _acquire_expensive_read(request, principal):
            systems = db_ops.get_systems_summary(
                allowed_systems=principal.allowed_systems, limit=limit
            )
        return [
            SystemSummary(
                system_id=system["system_id"],
                system_label=system.get("system_label"),
                total_calls=system["total_calls"],
                first_seen=system.get("first_seen"),
                last_seen=system.get("last_seen"),
                top_talkgroups=system.get("top_talkgroups", {}),
            )
            for system in systems
        ]

    except HTTPException:
        raise
    except ExpensiveQueryTimeout:
        raise _expensive_query_timeout() from None
    except Exception as e:
        logger.error("Error listing systems: %s", sanitize_log_value(e))
        raise HTTPException(status_code=500, detail="Error listing systems") from e


@router.get(
    "/api/talkgroups",
    response_model=list[TalkgroupSummary],
    summary="List Talkgroups",
    description="Get a list of talkgroups with summary statistics",
)
@limiter.limit("30/minute")
@limiter.limit(get_active_limits)
def list_talkgroups(
    request: Request,
    system_id: str | None = Query(
        None,
        description="Filter by system ID",
        min_length=1,
        max_length=10,
        pattern=r"^[0-9]+$",
    ),
    min_calls: int = Query(
        1, description="Minimum number of calls", ge=1, le=1_000_000_000
    ),
    limit: int = Query(500, description="Maximum talkgroups to return", ge=1, le=1000),
    _api_key: str | None = api_key_header,
) -> list[TalkgroupSummary]:
    """List talkgroups with summary statistics."""
    principal = authenticate_read_request(request)
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        with _acquire_expensive_read(request, principal):
            talkgroups = db_ops.get_talkgroups_summary(
                system_id=system_id,
                min_calls=min_calls,
                allowed_systems=principal.allowed_systems,
                limit=limit,
            )
        return [
            TalkgroupSummary(
                talkgroup_id=tg["talkgroup_id"],
                talkgroup_label=tg.get("talkgroup_label"),
                system_id=tg["system_id"],
                total_calls=tg["total_calls"],
                last_heard=tg.get("last_heard"),
            )
            for tg in talkgroups
        ]

    except HTTPException:
        raise
    except ExpensiveQueryTimeout:
        raise _expensive_query_timeout() from None
    except Exception as e:
        logger.error("Error listing talkgroups: %s", sanitize_log_value(e))
        raise HTTPException(status_code=500, detail="Error listing talkgroups") from e


@router.get(
    "/api/calls/{call_id}/audio",
    summary="Get Call Audio",
    description="Stream the audio file for a specific radio call",
    responses={
        200: {
            "description": "Audio file",
            "content": {"audio/mpeg": {}},
        },
        404: {"description": "Call not found or audio file missing"},
    },
)
@limiter.limit("300/minute")
@limiter.limit(get_active_limits)
def get_call_audio(
    request: Request,
    call_id: int = fastapi.Path(..., ge=1, le=9_223_372_036_854_775_807),
    _api_key: str | None = api_key_header,
) -> StreamingResponse:
    """Stream audio file for a specific radio call."""
    principal = authenticate_read_request(request)
    stream_principal = _read_principal_identity(request, principal)
    stream_gate = _get_audio_stream_gate(request)
    if not stream_gate.try_acquire(stream_principal):
        raise HTTPException(
            status_code=503,
            detail="Audio stream capacity reached; retry later",
            headers={"Retry-After": "1"},
        )

    db_ops: DatabaseOperations = request.app.state.db_ops
    file_handler = request.app.state.file_handler
    stream: Any | None = None
    stream_lock = threading.Lock()
    finalize_lock = threading.Lock()
    finalized = False
    response_handed_off = False

    def finalize_stream() -> None:
        """Close and release exactly once across body, disconnect, and errors."""
        nonlocal finalized
        with finalize_lock:
            if finalized:
                return
            finalized = True
            try:
                with stream_lock:
                    if stream is not None and not stream.closed:
                        stream.close()
            finally:
                stream_gate.release(stream_principal)

    try:
        record = db_ops.get_call_by_id(
            call_id, allowed_systems=principal.allowed_systems
        )
        if not record:
            raise HTTPException(status_code=404, detail="Call not found")

        audio_path_str = record.get("audio_file_path")
        if not audio_path_str:
            raise HTTPException(status_code=404, detail="No audio file for this call")

        # Pin the verified inode before constructing the response.  Returning a
        # pathname here would let an attacker replace a parent directory after
        # validation but before Starlette reopened the file.
        try:
            # Preserve the lexical path: ``open_stored_file`` safely maps the
            # validated configured-root alias and rejects every later symlink
            # component during its descriptor-relative walk.
            with file_handler.open_stored_file(str(audio_path_str)) as source:
                descriptor = os.dup(source.fileno())
        except (OSError, ValueError):
            raise HTTPException(
                status_code=404, detail="Audio file not found"
            ) from None

        try:
            size = os.fstat(descriptor).st_size
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

        def body() -> Any:
            try:
                while True:
                    # Serialize close with the bounded regular-file read.  This
                    # also makes cancellation while Starlette uses its worker
                    # thread deterministic.
                    with stream_lock:
                        if stream.closed:
                            return
                        chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk
            finally:
                finalize_stream()

        raw_filename = str(record.get("audio_filename") or Path(audio_path_str).name)
        filename = raw_filename.replace("\\", "/").rsplit("/", 1)[-1][:255]
        if not filename:
            filename = "audio.mp3"

        response = _ClosingStreamingResponse(
            body(),
            media_type="audio/mpeg",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(filename, safe="")
                ),
            },
            close_callback=finalize_stream,
        )
        response_handed_off = True
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Error retrieving audio for call %s: %s",
            call_id,
            sanitize_log_value(e),
        )
        raise HTTPException(status_code=500, detail="Error retrieving audio") from e
    finally:
        if not response_handed_off:
            finalize_stream()
