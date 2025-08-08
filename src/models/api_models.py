"""API request and response models for RdioScanner protocol."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RdioScannerUpload(BaseModel):
    """Model for RdioScanner call upload data.

    This model represents the data sent by SDRTrunk when uploading a radio call.
    All fields match the RdioScanner API specification.
    """

    # Required fields
    key: str = Field(..., description="API key for authentication")
    system: str = Field(..., description="System ID (numeric string)")
    dateTime: int = Field(..., description="Unix timestamp in seconds")

    # Audio file info (populated after parsing multipart form)
    audio_filename: str | None = Field(None, description="Uploaded audio filename")
    audio_content_type: str | None = Field(None, description="MIME type of audio file")
    audio_size: int | None = Field(None, description="Size of audio file in bytes")

    # Radio metadata (optional fields)
    frequency: int | None = Field(None, description="Frequency in Hz")
    talkgroup: int | None = Field(None, description="Talkgroup ID")
    source: int | None = Field(None, description="Source radio ID")

    # Labels and descriptions (optional)
    systemLabel: str | None = Field(None, description="Human-readable system name")
    talkgroupLabel: str | None = Field(
        None, description="Human-readable talkgroup name"
    )
    talkgroupGroup: str | None = Field(None, description="Talkgroup category/group")
    talkerAlias: str | None = Field(None, description="Alias of the talking radio")
    patches: str | None = Field(
        None, description="Comma-separated list of patched talkgroups"
    )

    # Additional fields that might be sent
    frequencies: str | None = Field(
        None, description="Comma-separated list of frequencies"
    )
    sources: str | None = Field(None, description="Comma-separated list of source IDs")
    talkgroupTag: str | None = Field(None, description="Additional talkgroup tag")

    # Test mode flag (not stored, just for request handling)
    test: int | None = Field(None, description="Test mode flag (1 for test)")

    # Allow extra fields that might be sent by different SDRTrunk versions
    model_config = ConfigDict(extra="allow")

    # Validators for enhanced input validation
    @field_validator("system")
    @classmethod
    def validate_system_id(cls, v: str) -> str:
        """Validate system ID is numeric and reasonable length."""
        if not v:
            raise ValueError("System ID cannot be empty")
        if not v.isdigit():
            raise ValueError("System ID must be numeric")
        if len(v) > 10:
            raise ValueError("System ID too long (max 10 digits)")
        return v

    @field_validator("dateTime")
    @classmethod
    def validate_timestamp(cls, v: int) -> int:
        """Validate timestamp is reasonable."""
        if v < 0:
            raise ValueError("Timestamp cannot be negative")
        # Check if timestamp is within reasonable range (not before 2000, not too far in future)
        current_time = int(datetime.now().timestamp())
        min_time = int(
            datetime(2000, 1, 1).timestamp()
        )  # Allow older timestamps for testing
        max_time = current_time + (86400 * 365)  # Allow up to 1 year in future

        if v < min_time:
            raise ValueError(f"Timestamp too old (before 2000): {v}")
        if v > max_time:
            raise ValueError(f"Timestamp too far in future: {v}")
        return v

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: int | None) -> int | None:
        """Validate frequency is within reasonable range."""
        if v is None:
            return v
        if v <= 0:
            raise ValueError("Frequency must be positive")
        # Reasonable frequency range: 25 MHz to 6 GHz
        if v < 25_000_000 or v > 6_000_000_000:
            raise ValueError(f"Frequency out of reasonable range: {v} Hz")
        return v

    @field_validator("talkgroup", "source")
    @classmethod
    def validate_radio_id(cls, v: int | None) -> int | None:
        """Validate radio IDs are reasonable."""
        if v is None:
            return v
        if v < 0:
            raise ValueError("Radio ID cannot be negative")
        if v > 999_999_999:  # Max reasonable ID
            raise ValueError(f"Radio ID too large: {v}")
        return v

    @field_validator(
        "systemLabel", "talkgroupLabel", "talkgroupGroup", "talkerAlias", "talkgroupTag"
    )
    @classmethod
    def validate_labels(cls, v: str | None) -> str | None:
        """Validate and sanitize label strings."""
        if v is None:
            return v
        # Remove any control characters
        v = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", v)
        # Limit length
        if len(v) > 255:
            v = v[:255]
        return v.strip()

    @field_validator("patches", "frequencies", "sources")
    @classmethod
    def validate_comma_separated(cls, v: str | None) -> str | None:
        """Validate comma-separated lists."""
        if v is None:
            return v
        # Handle empty array notation from SDRTrunk
        if v == "[]":
            return None
        # Handle JSON array format from SDRTrunk (e.g., "[52198,52199]")
        if v.startswith("[") and v.endswith("]"):
            # Remove brackets and spaces
            v = v[1:-1].replace(" ", "")
            # If empty after removing brackets, return None
            if v == "":
                return None
        else:
            # Remove spaces for regular comma-separated format
            v = v.replace(" ", "")
        # Allow empty string
        if v == "":
            return None
        if not re.match(r"^[\d,]+$", v):
            raise ValueError(f"Invalid comma-separated list format: {v}")
        return v

    @field_validator("audio_size")
    @classmethod
    def validate_audio_size(cls, v: int | None) -> int | None:
        """Validate audio file size."""
        if v is None:
            return v
        if v <= 0:
            raise ValueError("Audio size must be positive")
        # Max 100MB
        if v > 100 * 1024 * 1024:
            raise ValueError(f"Audio file too large: {v} bytes")
        return v


class CallUploadResponse(BaseModel):
    """Response model for call upload endpoint."""

    status: str = Field("ok", description="Response status")
    message: str = Field(..., description="Human-readable response message")
    callId: str | None = Field(
        None, description="Unique identifier for the uploaded call"
    )

    # Example response
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "message": "Call received and queued for processing",
                "callId": "20240101_120000_12345",
            }
        }
    )


class HealthCheckResponse(BaseModel):
    """Health check endpoint response."""

    status: str = Field("healthy", description="Service health status")
    timestamp: datetime = Field(..., description="Current server time")
    version: str = Field(..., description="API version")
    database: str = Field(..., description="Database connection status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "timestamp": "2024-01-01T12:00:00Z",
                "version": "1.0.0",
                "database": "connected",
            }
        }
    )


class StatisticsResponse(BaseModel):
    """Statistics endpoint response."""

    total_calls: int = Field(..., description="Total number of calls received")
    calls_today: int = Field(..., description="Calls received today")
    calls_last_hour: int = Field(..., description="Calls received in the last hour")

    # System breakdown
    systems: dict[str, int] = Field(
        default_factory=dict, description="Call count by system"
    )

    # Talkgroup breakdown
    talkgroups: dict[str, int] = Field(
        default_factory=dict, description="Call count by talkgroup"
    )

    # Upload source breakdown
    upload_sources: dict[str, int] = Field(
        default_factory=dict, description="Call count by upload IP"
    )

    # Storage info
    storage_used_mb: float = Field(..., description="Storage used in MB")
    audio_files_count: int = Field(..., description="Number of audio files stored")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_calls": 1234,
                "calls_today": 56,
                "calls_last_hour": 7,
                "systems": {"1": 500, "2": 734},
                "talkgroups": {"100": 123, "200": 456},
                "upload_sources": {"192.168.1.100": 1234},
                "storage_used_mb": 567.8,
                "audio_files_count": 1234,
            }
        }
    )
