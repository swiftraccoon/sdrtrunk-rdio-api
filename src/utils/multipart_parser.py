"""Bounded legacy multipart parser.

The upload endpoint uses Starlette's streaming multipart parser. These helpers
remain for compatibility with callers that need to parse SDRTrunk fixtures,
but they deliberately enforce conservative bounds and reject malformed input.
"""

from __future__ import annotations

import logging
from email.message import Message
from typing import Any

logger = logging.getLogger(__name__)

MAX_BOUNDARY_BYTES = 200
MAX_HEADER_BYTES = 8192
MAX_FIELDS = 32
MAX_FILES = 1
MAX_FIELD_BYTES = 16 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_BODY_BYTES = MAX_FILE_BYTES + 1024 * 1024
MAX_NAME_BYTES = 64
MAX_FILENAME_BYTES = 255


class MultipartParseError(ValueError):
    """Raised when legacy multipart data is malformed or exceeds a limit."""


class SimpleUploadFile:
    """Compatibility container used only by direct legacy-parser callers."""

    def __init__(self, filename: str, content_type: str | None, content: bytes):
        self.filename = filename
        self.content_type = content_type
        self.content = content
        self.size = len(content)

    async def read(self) -> bytes:
        return self.content

    def __repr__(self) -> str:
        return (
            "SimpleUploadFile("
            f"filename={self.filename!r}, type={self.content_type!r}, size={self.size}"
            ")"
        )


def _decode_utf8(value: bytes, description: str) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MultipartParseError(f"Invalid UTF-8 in {description}") from exc


def _quoted_parameter(part: bytes, prefix: bytes) -> str | None:
    part = part.strip()
    if not part.startswith(prefix) or not part.endswith(b'"'):
        return None
    raw = part[len(prefix) : -1]
    return _decode_utf8(raw, "multipart header")


def parse_multipart_form(
    content: bytes,
    boundary: str,
    *,
    max_body_bytes: int = MAX_BODY_BYTES,
    max_fields: int = MAX_FIELDS,
    max_files: int = MAX_FILES,
    max_field_bytes: int = MAX_FIELD_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse a bounded SDRTrunk multipart payload.

    This is intentionally not used as a fallback after Starlette rejects a
    request. A parser limit or syntax error must fail closed.
    """
    if not boundary:
        raise MultipartParseError("Multipart boundary is required")
    if len(content) > max_body_bytes:
        raise MultipartParseError("Multipart body is too large")

    try:
        boundary_bytes = boundary.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise MultipartParseError("Multipart boundary must be ASCII") from exc
    if not 1 <= len(boundary_bytes) <= MAX_BOUNDARY_BYTES:
        raise MultipartParseError("Invalid multipart boundary length")
    if b"\r" in boundary_bytes or b"\n" in boundary_bytes:
        raise MultipartParseError("Invalid multipart boundary")
    # The boundary parameter never includes the two delimiter-prefix dashes.
    # SDRTrunk's boundary value itself happens to begin with two dashes, so its
    # on-wire delimiter correctly begins with four.
    boundary_bytes = b"--" + boundary_bytes

    # Bound the split result so an attacker-controlled short boundary cannot
    # expand a modest body into millions of Python objects.
    maximum_parts = max_fields + max_files + 2
    parts = content.split(boundary_bytes, maximum_parts)
    if len(parts) > maximum_parts:
        raise MultipartParseError("Too many multipart parts")

    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}

    for index, raw_part in enumerate(parts):
        if index == 0:
            continue
        if raw_part in {b"--\r\n", b"--", b"\r\n", b""}:
            continue

        part = raw_part[2:] if raw_part.startswith(b"\r\n") else raw_part
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            raise MultipartParseError("Malformed multipart part")
        if header_end > MAX_HEADER_BYTES:
            raise MultipartParseError("Multipart headers are too large")

        headers = part[:header_end]
        body = part[header_end + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]

        name: str | None = None
        filename: str | None = None
        content_type: str | None = None

        for header_line in headers.split(b"\r\n"):
            if header_line.lower().startswith(b"content-disposition:"):
                for parameter in header_line.split(b";"):
                    parsed_name = _quoted_parameter(parameter, b'name="')
                    parsed_filename = _quoted_parameter(parameter, b'filename="')
                    if parsed_name is not None:
                        name = parsed_name
                    elif parsed_filename is not None:
                        filename = parsed_filename
            elif header_line.lower().startswith(b"content-type:"):
                content_type = _decode_utf8(
                    header_line.split(b":", 1)[1].strip(), "content type"
                )

        if not name:
            raise MultipartParseError("Multipart part is missing a field name")
        if len(name.encode("utf-8")) > MAX_NAME_BYTES or any(
            ord(character) < 32 or ord(character) == 127 for character in name
        ):
            raise MultipartParseError("Invalid multipart field name")
        if name in fields or name in files:
            raise MultipartParseError("Duplicate multipart field")

        if filename is not None:
            if len(files) >= max_files:
                raise MultipartParseError("Too many uploaded files")
            if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
                raise MultipartParseError("Uploaded filename is too long")
            if len(body) > max_file_bytes:
                raise MultipartParseError("Uploaded file is too large")
            files[name] = {
                "filename": filename,
                "content": body,
                "content_type": content_type,
            }
        else:
            if len(fields) >= max_fields:
                raise MultipartParseError("Too many form fields")
            if len(body) > max_field_bytes:
                raise MultipartParseError("Multipart field is too large")
            fields[name] = _decode_utf8(body, "form field")

    return fields, files


def parse_multipart_form_with_content_type(
    content_type: str,
    body: bytes,
    **limits: int,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse bounded multipart data after robustly extracting its boundary."""
    if len(content_type) > MAX_HEADER_BYTES:
        raise MultipartParseError("Content-Type header is too large")

    message = Message()
    message["content-type"] = content_type
    if message.get_content_type().lower() != "multipart/form-data":
        raise MultipartParseError("Content-Type is not multipart/form-data")
    boundary = message.get_param("boundary", header="content-type")
    if not isinstance(boundary, str) or not boundary:
        raise MultipartParseError("Multipart boundary is required")
    return parse_multipart_form(body, boundary, **limits)
