"""RdioScanner API endpoint implementation."""

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..database.operations import DatabaseOperations
from ..models.api_models import CallUploadResponse, RdioScannerUpload
from ..utils.file_handler import FileHandler
from ..utils.multipart_parser import (
    SimpleUploadFile,
    parse_multipart_form_with_content_type,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["RdioScanner"])


def get_client_info(request: Request) -> tuple[str, str]:
    """Extract client IP and user agent from request."""
    # Try X-Forwarded-For header first (for proxies)
    client_ip = request.headers.get("x-forwarded-for")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    else:
        # Fall back to direct connection
        client_ip = request.client.host if request.client else "unknown"

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

    # Check each configured API key
    for idx, api_key_config in enumerate(config.security.api_keys):
        if api_key_config.key == key:
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


@router.post("/api/call-upload", response_model=CallUploadResponse)
async def upload_call(request: Request) -> Response:
    """RdioScanner API endpoint for receiving calls from SDRTrunk.

    This endpoint accepts multipart form data with the following fields:
    - key: API key for authentication
    - system: System ID
    - dateTime: Unix timestamp
    - audio: MP3 audio file
    - Additional optional fields per RdioScanner spec
    """
    start_time = time.time()

    # Get app state
    config = request.app.state.config
    db_ops: DatabaseOperations = request.app.state.db_ops
    file_handler: FileHandler = request.app.state.file_handler

    # Extract client info
    client_ip, user_agent = get_client_info(request)

    logger.info(f"RdioScanner upload request from {client_ip} - {user_agent}")

    # Log raw request details
    logger.debug(f"Request method: {request.method}")
    logger.debug(f"Request URL: {request.url}")
    logger.debug(f"Request headers: {dict(request.headers)}")

    try:
        # Parse request body
        raw_body = await request.body()

        # Log raw body details
        logger.debug(f"Raw body length: {len(raw_body)} bytes")
        logger.debug(f"Raw body first 1000 chars: {raw_body[:1000]!r}")
        if len(raw_body) <= 10000:  # Only log full body if it's reasonably small
            logger.debug(f"Full raw body: {raw_body!r}")
        else:
            logger.debug(
                f"Raw body too large to log fully, showing first 10KB: {raw_body[:10240]!r}"
            )

        # Try FastAPI's built-in form parsing first
        form_data: dict[str, Any] = {}
        try:
            fastapi_form = await request.form()
            logger.debug(
                f"FastAPI form parsed successfully, got {len(fastapi_form)} fields"
            )

            # Convert to our expected format
            for key, value in fastapi_form.items():
                logger.debug(
                    f"Processing form field '{key}': type={type(value)}, value={str(value)[:100] if not hasattr(value, 'filename') else f'UploadFile({value.filename})'}"
                )
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

            logger.debug(f"Manual parser extracted fields: {fields}")
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
        # Better logging for form data
        form_data_repr = []
        for k, v in form_data.items():
            if isinstance(v, str):
                if len(v) > 50:
                    form_data_repr.append((k, f"{v[:50]}..."))
                else:
                    form_data_repr.append((k, v))
            elif isinstance(v, bytes):
                if len(v) > 50:
                    form_data_repr.append((k, f"{v[:50]!r}..."))
                else:
                    form_data_repr.append((k, repr(v)))
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

        # Validate API key
        is_valid, api_key_id = validate_api_key(config, key, system, client_ip)
        if not is_valid:
            db_ops.log_upload_attempt(
                client_ip=client_ip,
                success=False,
                system_id=system,
                user_agent=user_agent,
                error_message="Invalid API key",
                response_code=401,
            )
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Extract and validate required fields
        dateTime_str = form_data.get("dateTime")
        if not system or not dateTime_str:
            error_msg = "Missing required fields: system and dateTime"
            db_ops.log_upload_attempt(
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
                db_ops.log_upload_attempt(
                    client_ip=client_ip,
                    success=False,
                    system_id=system,
                    api_key_used=api_key_id,
                    user_agent=user_agent,
                    error_message=error_msg,
                    response_code=400,
                )
                raise HTTPException(status_code=400, detail=error_msg)

        # Create upload data model
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

        # Process based on mode
        stored_path: str | None = None

        if config.processing.mode == "log_only":
            # Just log the upload
            logger.info(
                f"Logged call: System={system}, TG={upload_data.talkgroup}, "
                f"Freq={upload_data.frequency}, Time={upload_data.dateTime}"
            )

        elif config.processing.mode in ["store", "process"]:
            # Validate and store audio file if provided
            if audio:
                # Validate file
                is_valid, error_msg_optional = file_handler.validate_file(
                    audio.filename, audio.content, audio.content_type
                )

                if not is_valid:
                    error_msg_str = error_msg_optional or "File validation failed"
                    db_ops.log_upload_attempt(
                        client_ip=client_ip,
                        success=False,
                        system_id=system,
                        api_key_used=api_key_id,
                        user_agent=user_agent,
                        filename=audio.filename,
                        file_size=audio.size,
                        content_type=audio.content_type,
                        error_message=error_msg_str,
                        response_code=400,
                    )
                    raise HTTPException(status_code=400, detail=error_msg_str)

                # Store file based on strategy
                if config.file_handling.storage.strategy == "filesystem":
                    # Save to temp first
                    temp_path = file_handler.save_temp_file(
                        audio.filename, audio.content
                    )

                    # Move to permanent storage
                    stored_path_obj = file_handler.store_file(
                        temp_path,
                        system,
                        datetime.fromtimestamp(upload_data.dateTime),
                        upload_data.talkgroup,
                    )
                    stored_path = str(stored_path_obj)

                elif config.file_handling.storage.strategy == "database":
                    # For database storage, we'd store the content in a BLOB
                    # This is not recommended for large files
                    logger.warning("Database storage not implemented, using filesystem")
                    stored_path = None

                # else "discard" - don't store the file

            # Save to database
            call_id = db_ops.save_radio_call(
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
        db_ops.log_upload_attempt(
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

        # Return response
        response_data = CallUploadResponse(
            status="ok",
            message="Call received and processed",
            callId=(
                f"{system}_{upload_data.dateTime}_{upload_data.talkgroup or 'unknown'}"
            ),
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
            db_ops.log_upload_attempt(
                client_ip=client_ip,
                success=False,
                user_agent=user_agent,
                error_message=str(e),
                response_code=500,
                processing_time_ms=processing_time,
            )
        except Exception:
            pass

        raise HTTPException(status_code=500, detail="Internal server error") from None
