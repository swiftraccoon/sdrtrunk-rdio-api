"""Query API endpoints for retrieving radio call data."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..database.operations import DatabaseOperations
from ..middleware.rate_limiter import get_limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
limiter = get_limiter()

# Query parameter definitions
date_from_query = Query(None, description="Start date for filtering (ISO 8601)")
date_to_query = Query(None, description="End date for filtering (ISO 8601)")


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
@limiter.limit("30 per minute")
async def query_calls(
    request: Request,
    system_id: str | None = Query(None, description="Filter by system ID"),
    talkgroup_id: int | None = Query(None, description="Filter by talkgroup ID"),
    source_id: int | None = Query(None, description="Filter by source radio ID"),
    frequency: int | None = Query(None, description="Filter by frequency (Hz)"),
    date_from: datetime | None = date_from_query,
    date_to: datetime | None = date_to_query,
    hours_ago: int | None = Query(
        None, description="Get calls from last N hours", ge=1, le=168
    ),
    page: int = Query(1, description="Page number", ge=1),
    per_page: int = Query(20, description="Items per page", ge=1, le=100),
    sort_by: str = Query(
        "timestamp",
        description="Sort field",
        pattern="^(timestamp|system_id|talkgroup_id|frequency)$",
    ),
    sort_order: str = Query("desc", description="Sort order", pattern="^(asc|desc)$"),
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
    db_ops: DatabaseOperations = request.app.state.db_ops

    # Build filter criteria
    filters: dict[str, Any] = {}

    if system_id:
        filters["system_id"] = system_id
    if talkgroup_id:
        filters["talkgroup_id"] = talkgroup_id
    if source_id:
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
        results = db_ops.query_calls(
            filters=filters,
            page=page,
            per_page=per_page,
            sort_by=sort_by,
            sort_order=sort_order,
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

    except Exception as e:
        logger.error(f"Error querying calls: {e}")
        raise HTTPException(status_code=500, detail="Error querying calls") from e


@router.get(
    "/api/calls/{call_id}",
    response_model=CallRecord,
    summary="Get Call by ID",
    description="Retrieve a specific radio call by its ID",
)
@limiter.limit("60 per minute")
async def get_call(request: Request, call_id: int) -> CallRecord:
    """Get a specific radio call by ID."""
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        record = db_ops.get_call_by_id(call_id)
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
        logger.error(f"Error retrieving call {call_id}: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving call") from e


@router.get(
    "/api/systems",
    response_model=list[SystemSummary],
    summary="List Systems",
    description="Get a list of all systems with summary statistics",
)
@limiter.limit("30 per minute")
async def list_systems(request: Request) -> list[SystemSummary]:
    """List all systems with summary statistics."""
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        systems = db_ops.get_systems_summary()
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

    except Exception as e:
        logger.error(f"Error listing systems: {e}")
        raise HTTPException(status_code=500, detail="Error listing systems") from e


@router.get(
    "/api/talkgroups",
    response_model=list[TalkgroupSummary],
    summary="List Talkgroups",
    description="Get a list of talkgroups with summary statistics",
)
@limiter.limit("30 per minute")
async def list_talkgroups(
    request: Request,
    system_id: str | None = Query(None, description="Filter by system ID"),
    min_calls: int = Query(1, description="Minimum number of calls", ge=1),
) -> list[TalkgroupSummary]:
    """List talkgroups with summary statistics."""
    db_ops: DatabaseOperations = request.app.state.db_ops

    try:
        talkgroups = db_ops.get_talkgroups_summary(
            system_id=system_id, min_calls=min_calls
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

    except Exception as e:
        logger.error(f"Error listing talkgroups: {e}")
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
@limiter.limit("60 per minute")
async def get_call_audio(request: Request, call_id: int) -> FileResponse:
    """Stream audio file for a specific radio call."""
    db_ops: DatabaseOperations = request.app.state.db_ops
    config = request.app.state.config

    try:
        record = db_ops.get_call_by_id(call_id)
        if not record:
            raise HTTPException(status_code=404, detail="Call not found")

        audio_path_str = record.get("audio_file_path")
        if not audio_path_str:
            raise HTTPException(status_code=404, detail="No audio file for this call")

        audio_path = Path(audio_path_str).resolve()

        # Path traversal prevention: ensure the resolved path is within the
        # configured storage directory.
        storage_dir = Path(config.file_handling.storage.directory).resolve()
        if not audio_path.is_relative_to(storage_dir):
            logger.warning(
                f"Path traversal attempt for call {call_id}: {audio_path_str}"
            )
            raise HTTPException(status_code=404, detail="Audio file not found")

        if not audio_path.is_file():
            raise HTTPException(status_code=404, detail="Audio file not found")

        filename = record.get("audio_filename") or audio_path.name

        return FileResponse(
            path=str(audio_path),
            media_type="audio/mpeg",
            filename=filename,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving audio for call {call_id}: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving audio") from e
