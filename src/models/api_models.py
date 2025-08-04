"""API request and response models for RdioScanner protocol."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
