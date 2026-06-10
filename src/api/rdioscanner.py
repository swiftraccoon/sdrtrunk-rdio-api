"""RdioScanner API endpoint implementation."""

import asyncio
import hmac
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from ..database.operations import DatabaseOperations
from ..middleware.rate_limiter import get_active_limits, get_limiter
from ..models.api_models import CallUploadResponse, RdioScannerUpload
from ..utils.file_handler import FileHandler
from ..utils.multipart_parser import (
    SimpleUploadFile,
    parse_multipart_form_with_content_type,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])

# Get the limiter instance
limiter = get_limiter()


def get_client_info(
    request: Request, trusted_proxies: list[str] | None = None
) -> tuple[str, str]:
    """Extract client IP and user agent from request.

    X-Forwarded-For is only honored when the direct peer is a configured
    trusted proxy; otherwise it is trivially spoofable and would let
    clients bypass per-key IP restrictions.
    """
    direct_ip = request.client.host if request.client else "unknown"

    client_ip = direct_ip
    if trusted_proxies and direct_ip in trusted_proxies:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()

    user_agent = request.headers.get("user-agent", "unknown")
    return client_ip, user_agent


def validate_api_key(
    config: Any, key: str, system: str, client_ip: str
) -> tuple[bool, str | None]:
    """Validate API key with IP and system restrictions.

    Returns:
        (is_valid, api_key_id)
    """
    # If no API keys configured, allow all
    if not config.security.api_keys:
        return True, None

    # Check each configured API key with constant-time comparison
    for idx, api_key_config in enumerate(config.security.api_keys):
        # Use constant-time comparison to prevent timing attacks.
        # Compare as bytes: compare_digest rejects non-ASCII str input.
        if hmac.compare_digest(api_key_config.key.encode("utf-8"), key.encode("utf-8")):
            api_key_id = f"key_{idx}"

            # Check IP restrictions
            if api_key_config.allowed_ips:
                if client_ip not in api_key_config.allowed_ips:
                    logger.warning(f"API key {api_key_id} rejected for IP {client_ip}")
                    return False, None

            # Check system restrictions
            if api_key_config.allowed_systems:
                if system not in api_key_config.allowed_systems:
                    logger.warning(f"API key {api_key_id} rejected for system {system}")
                    return False, None

            return True, api_key_id

    return False, None


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

    # Get app state
    config = request.app.state.config
    db_ops: DatabaseOperations = request.app.state.db_ops
    file_handler: FileHandler = request.app.state.file_handler

    # Extract client info
    client_ip, user_agent = get_client_info(request, config.security.trusted_proxies)

    logger.info(f"RdioScanner upload request from {client_ip} - {user_agent}")

    # Log request details (never log body content: it contains the API key)
    logger.debug(f"Request method: {request.method}")
    logger.debug(f"Request URL: {request.url}")

    try:
        # Parse request body (buffered so the fallback parser can re-read it)
        raw_body = await request.body()
        logger.debug(f"Raw body length: {len(raw_body)} bytes")

        # Try FastAPI's built-in form parsing first
        form_data: dict[str, Any] = {}
        try:
            fastapi_form = await request.form()
            logger.debug(
                f"FastAPI form parsed successfully, got {len(fastapi_form)} fields"
            )

            # Convert to our expected format
            for key, value in fastapi_form.items():
                logger.debug(f"Processing form field '{key}': type={type(value)}")
                # Check for both FastAPI and Starlette UploadFile types
                if hasattr(value, "filename") and hasattr(value, "read"):
                    # It's an upload file
                    logger.debug(f"Detected upload file for field '{key}'")
                    # Read file content
                    content = await value.read()
                    logger.debug(f"Read {len(content)} bytes from UploadFile '{key}'")
                    form_data[key] = SimpleUploadFile(
                        filename=value.filename or "unknown",
                        content_type=(
                            value.content_type
                            if hasattr(value, "content_type")
                            else "application/octet-stream"
                        ),
                        content=content,
                    )
                    logger.debug(
                        f"Converted UploadFile '{key}' to SimpleUploadFile: filename={value.filename}, size={len(content)} bytes, type={type(form_data[key])}"
                    )
                else:
                    form_data[key] = value

        except Exception as e:
            logger.debug(f"FastAPI form parsing failed, using manual parser: {e}")
            # Fallback to manual parser
            content_type = request.headers.get("content-type", "")
            logger.debug(f"Using manual parser with content-type: {content_type}")
            fields, files = parse_multipart_form_with_content_type(
                content_type, raw_body
            )

            logger.debug(f"Manual parser extracted fields: {list(fields.keys())}")
            logger.debug(
                f"Manual parser extracted files: {[(name, {'filename': f['filename'], 'content_type': f['content_type'], 'size': len(f['content'])}) for name, f in files.items()]}"
            )

            form_data = fields  # Put fields in form_data
            # Add files to form_data as SimpleUploadFile objects
            for name, file_data in files.items():
                # Mixed dict type needed for multipart form data: strings for fields, SimpleUploadFile for files
                # Dict[str, Any] annotation allows this but mypy still flags the specific assignment
                form_data[name] = SimpleUploadFile(  # type: ignore[assignment]
                    filename=file_data["filename"],
                    content_type=file_data["content_type"],
                    content=file_data["content"],
                )

        # Extract fields
        logger.debug(f"Received form_data keys: {list(form_data.keys())}")
        # Log form data with the API key redacted
        form_data_repr = []
        for k, v in form_data.items():
            if k == "key":
                form_data_repr.append((k, "***redacted***"))
            elif isinstance(v, str):
                if len(v) > 50:
                    form_data_repr.append((k, f"{v[:50]}..."))
                else:
                    form_data_repr.append((k, v))
            elif isinstance(v, bytes):
                form_data_repr.append((k, f"<{len(v)} bytes>"))
            elif isinstance(v, SimpleUploadFile):
                form_data_repr.append(
                    (k, f"SimpleUploadFile(filename={v.filename}, size={v.size})")
                )
            else:
                form_data_repr.append((k, f"{type(v).__name__}: {str(v)[:100]}"))
        logger.debug(f"form_data content: {form_data_repr}")

        key = str(form_data.get("key", ""))
        system = str(form_data.get("system", ""))
        test = form_data.get("test")

        # Validate API key first - test requests must also authenticate,
        # otherwise SDRTrunk's "Test" button reports success with a bad key
        # and real uploads fail later with silent 401s.
        is_valid, api_key_id = validate_api_key(config, key, system, client_ip)
        if not is_valid:
            await asyncio.to_thread(
                db_ops.log_upload_attempt,
                client_ip=client_ip,
                success=False,
                system_id=system,
                user_agent=user_agent,
                error_message="Invalid API key",
                response_code=401,
            )
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Handle test requests
        if test is not None:
            logger.info(f"Test request from system {system}")
            message = "incomplete call data: no talkgroup"

            # Check if client wants JSON response
            accept_header = request.headers.get("accept", "")
            if "application/json" in accept_header:
                return JSONResponse(
                    {"status": "ok", "message": message, "callId": "test"}
                )
            else:
                return PlainTextResponse(message)

        # Extract and validate required fields
        dateTime_str = form_data.get("dateTime")
        if not system or not dateTime_str:
            error_msg = "Missing required fields: system and dateTime"
            await asyncio.to_thread(
                db_ops.log_upload_attempt,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_msg,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_msg)

        # Get audio file
        audio = form_data.get("audio")
        if not isinstance(audio, SimpleUploadFile):
            # For non-test requests, audio is required
            if config.processing.mode != "log_only":
                error_msg = "Audio file is required"
                await asyncio.to_thread(
                    db_ops.log_upload_attempt,
                    client_ip=client_ip,
                    success=False,
                    system_id=system,
                    api_key_used=api_key_id,
                    user_agent=user_agent,
                    error_message=error_msg,
                    response_code=400,
                )
                raise HTTPException(status_code=400, detail=error_msg)

        # Create upload data model. Client-supplied values that fail
        # validation are a 400, not a 500.
        try:
            upload_data = RdioScannerUpload(
                key=key,
                system=system,
                dateTime=int(dateTime_str),
                audio_filename=audio.filename if audio else None,
                audio_content_type=audio.content_type if audio else None,
                audio_size=audio.size if audio else None,
                frequency=(
                    int(form_data["frequency"]) if form_data.get("frequency") else None
                ),
                talkgroup=(
                    int(form_data["talkgroup"]) if form_data.get("talkgroup") else None
                ),
                source=int(form_data["source"]) if form_data.get("source") else None,
                systemLabel=form_data.get("systemLabel"),
                talkgroupLabel=form_data.get("talkgroupLabel"),
                talkgroupGroup=form_data.get("talkgroupGroup"),
                talkerAlias=form_data.get("talkerAlias"),
                patches=form_data.get("patches"),
                frequencies=form_data.get("frequencies"),
                sources=form_data.get("sources"),
                talkgroupTag=form_data.get("talkgroupTag"),
                test=int(test) if test is not None else None,
            )
        except ValidationError as e:
            first_error = e.errors()[0]
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            error_msg = (
                f"Invalid upload data: {field or 'request'}: "
                f"{first_error.get('msg', 'invalid value')}"
            )
            await asyncio.to_thread(
                db_ops.log_upload_attempt,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_msg,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_msg) from None
        except (ValueError, TypeError) as e:
            error_msg = f"Invalid upload data: {e}"
            await asyncio.to_thread(
                db_ops.log_upload_attempt,
                client_ip=client_ip,
                success=False,
                system_id=system,
                api_key_used=api_key_id,
                user_agent=user_agent,
                error_message=error_msg,
                response_code=400,
            )
            raise HTTPException(status_code=400, detail=error_msg) from None

        # Process based on mode
        stored_path: str | None = None
        call_id: int | None = None

        if config.processing.mode == "log_only":
            # Just log the upload without storing audio
            logger.info(
                f"Logged call: System={system}, TG={upload_data.talkgroup}, "
                f"Freq={upload_data.frequency}, Time={upload_data.dateTime}"
            )
            # Still save to database even in log_only mode
            call_id = await asyncio.to_thread(
                db_ops.save_radio_call,
                upload_data,
                audio_file_path=None,
                upload_ip=client_ip,
                api_key_id=api_key_id,
            )

        elif config.processing.mode in ["store", "process"]:
            # Validate and store audio file if provided
            if audio:
                # Validate file; oversized files are 413, other problems 400
                is_valid, error_msg_optional = file_handler.validate_file(
                    audio.filename, audio.content, audio.content_type
                )

                if not is_valid:
                    error_msg_str = error_msg_optional or "File validation failed"
                    status_code = (
                        413 if audio.size > file_handler.max_file_size_bytes else 400
                    )
                    await asyncio.to_thread(
                        db_ops.log_upload_attempt,
                        client_ip=client_ip,
                        success=False,
                        system_id=system,
                        api_key_used=api_key_id,
                        user_agent=user_agent,
                        filename=audio.filename,
                        file_size=audio.size,
                        content_type=audio.content_type,
                        error_message=error_msg_str,
                        response_code=status_code,
                    )
                    raise HTTPException(status_code=status_code, detail=error_msg_str)

                # Store file based on strategy
                if config.file_handling.storage.strategy == "filesystem":
                    # Save to temp first
                    temp_path = await asyncio.to_thread(
                        file_handler.save_temp_file, audio.filename, audio.content
                    )

                    # Move to permanent storage with verbose filename
                    stored_path_obj = await asyncio.to_thread(
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
                    )
                    stored_path = str(stored_path_obj)

                elif config.file_handling.storage.strategy == "database":
                    # For database storage, we'd store the content in a BLOB
                    # This is not recommended for large files
                    logger.warning("Database storage not implemented, using filesystem")
                    stored_path = None

                # else "discard" - don't store the file

            # Save to database
            call_id = await asyncio.to_thread(
                db_ops.save_radio_call,
                upload_data,
                audio_file_path=stored_path,
                upload_ip=client_ip,
                api_key_id=api_key_id,
            )

            logger.info(
                f"Stored call {call_id}: System={system}, TG={upload_data.talkgroup}"
            )

        # Log successful upload
        processing_time = (time.time() - start_time) * 1000
        await asyncio.to_thread(
            db_ops.log_upload_attempt,
            client_ip=client_ip,
            success=True,
            system_id=system,
            api_key_used=api_key_id,
            user_agent=user_agent,
            filename=audio.filename if audio else None,
            file_size=audio.size if audio else None,
            content_type=audio.content_type if audio else None,
            response_code=200,
            processing_time_ms=processing_time,
        )

        # Return response. callId is the database id so clients can fetch
        # the call back via /api/calls/{callId}.
        response_data = CallUploadResponse(
            status="ok",
            message="Call received and processed",
            callId=str(call_id) if call_id is not None else None,
        )

        # Check if client wants JSON
        accept_header = request.headers.get("accept", "")
        if "application/json" in accept_header:
            return JSONResponse(response_data.model_dump())
        else:
            return PlainTextResponse("Call imported successfully.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing upload: {e}", exc_info=True)

        # Log failed attempt
        try:
            processing_time = (time.time() - start_time) * 1000
            await asyncio.to_thread(
                db_ops.log_upload_attempt,
                client_ip=client_ip,
                success=False,
                user_agent=user_agent,
                error_message=str(e),
                response_code=500,
                processing_time_ms=processing_time,
            )
        except Exception as log_error:
            logger.warning(f"Failed to log upload attempt to database: {log_error}")

        raise HTTPException(status_code=500, detail="Internal server error") from None
