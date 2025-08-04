"""Multipart form data parser for handling RdioScanner uploads."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SimpleUploadFile:
    """Simple container for uploaded file data."""

    def __init__(self, filename: str, content_type: str | None, content: bytes):
        self.filename = filename
        self.content_type = content_type
        self.content = content
        self.size = len(content)

    async def read(self) -> bytes:
        """Read file content (async compatible)."""
        return self.content

    def __repr__(self) -> str:
        return f"SimpleUploadFile(filename={self.filename}, type={self.content_type}, size={self.size})"


def parse_multipart_form(
    content: bytes, boundary: str
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse multipart form data from raw bytes.

    This is a fallback parser for cases where standard form parsing fails.
    It's specifically designed to handle quirks in SDRTrunk's multipart encoding.

    Args:
        content: Raw request body
        boundary: Multipart boundary string

    Returns:
        Tuple of (fields, files) dictionaries
    """
    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}

    if not boundary:
        logger.error("No boundary provided")
        return fields, files

    logger.debug(f"Parsing multipart form with boundary: {boundary}")
    logger.debug(f"Content length: {len(content)} bytes")
    logger.debug(f"Content preview (first 500 bytes): {content[:500]!r}")

    # Convert boundary to bytes and ensure it's properly formatted
    if isinstance(boundary, str):
        boundary_bytes = boundary.encode("utf-8")
    else:
        boundary_bytes = boundary

    if not boundary_bytes.startswith(b"--"):
        boundary_bytes = b"--" + boundary_bytes

    logger.debug(f"Using boundary bytes: {boundary_bytes!r}")

    # Split by boundary
    parts = content.split(boundary_bytes)
    logger.debug(f"Found {len(parts)} parts")
    for i, part in enumerate(parts[:5]):  # Log first 5 parts
        logger.debug(f"Part {i} preview (first 200 bytes): {part[:200]!r}")

    for idx, part in enumerate(parts):
        if idx == 0:
            continue  # Skip preamble

        if part == b"--\r\n" or part == b"--" or len(part) < 4:
            continue  # Skip epilogue

        # Remove leading CRLF
        if part.startswith(b"\r\n"):
            part = part[2:]

        # Find headers/body separator
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue

        headers = part[:header_end]  # Keep as bytes
        body = part[header_end + 4 :]

        # Remove trailing CRLF
        if body.endswith(b"\r\n"):
            body = body[:-2]
        # Also remove trailing boundary delimiters that might be included
        if body.endswith(b"\r\n--"):
            body = body[:-4]
        elif body.endswith(b"\n--"):
            body = body[:-3]
        elif body.endswith(b"--"):
            body = body[:-2]

        # Parse Content-Disposition header
        name = None
        filename = None
        content_type = None

        for header_line in headers.split(b"\r\n"):
            if header_line.lower().startswith(b"content-disposition:"):
                # Parse Content-Disposition parameters
                # Handle both orders: name="x"; filename="y" and filename="y"; name="x"
                parts = header_line.split(b";")
                for part in parts:
                    part = part.strip()
                    if part.startswith(b'name="'):
                        name = part[6:-1].decode(
                            "utf-8", errors="ignore"
                        )  # Extract value between quotes
                    elif part.startswith(b'filename="'):
                        filename = part[10:-1].decode(
                            "utf-8", errors="ignore"
                        )  # Extract value between quotes
            elif header_line.lower().startswith(b"content-type:"):
                content_type = (
                    header_line.split(b":", 1)[1]
                    .strip()
                    .decode("utf-8", errors="ignore")
                )

        if name:
            if filename:
                # It's a file upload
                files[name] = {
                    "filename": filename,
                    "content": body,
                    "content_type": content_type,
                }
                logger.debug(
                    f"Found file field: {name} = {filename} ({len(body)} bytes)"
                )
            else:
                # It's a regular field
                fields[name] = body.decode("utf-8", errors="ignore")
                logger.debug(
                    f"Found field '{name}' = '{fields[name][:50]}...'"
                    if len(fields[name]) > 50
                    else f"Found field '{name}' = '{fields[name]}'"
                )

    return fields, files


def parse_multipart_form_with_content_type(
    content_type: str, body: bytes
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse multipart form data using content-type header.

    Args:
        content_type: Content-Type header value
        body: Raw request body

    Returns:
        Tuple of (fields, files) dictionaries
    """
    # Extract boundary from content-type
    boundary = None
    if "boundary=" in content_type:
        boundary = content_type.split("boundary=")[1].split(";")[0].strip()
        # Remove quotes if present
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]

    if not boundary:
        logger.error("No boundary found in content-type header")
        return {}, {}

    return parse_multipart_form(body, boundary)
