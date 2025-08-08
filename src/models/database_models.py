"""Database models for storing radio call data."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class RadioCall(Base):
    """Main table for storing radio call records."""

    __tablename__ = "radio_calls"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    call_timestamp = Column(DateTime, nullable=False, index=True)  # From dateTime field

    # System information
    system_id = Column(String(50), nullable=False, index=True)
    system_label = Column(String(255), nullable=True)

    # Radio metadata
    frequency = Column(Integer, nullable=True, index=True)  # Hz
    talkgroup_id = Column(Integer, nullable=True, index=True)
    talkgroup_label = Column(String(255), nullable=True)
    talkgroup_group = Column(String(255), nullable=True)
    talkgroup_tag = Column(String(255), nullable=True)

    # Source information
    source_radio_id = Column(Integer, nullable=True, index=True)
    talker_alias = Column(String(255), nullable=True)

    # Audio file information
    audio_filename = Column(String(255), nullable=True)
    audio_content_type = Column(String(100), nullable=True)
    audio_size_bytes = Column(Integer, nullable=True)
    audio_file_path = Column(String(500), nullable=True)  # Full path to stored file

    # Additional metadata
    patches = Column(Text, nullable=True)  # Comma-separated patch list
    frequencies = Column(Text, nullable=True)  # Comma-separated frequency list
    sources = Column(Text, nullable=True)  # Comma-separated source list

    # Upload tracking
    upload_ip = Column(String(45), nullable=True, index=True)  # IPv4 or IPv6
    upload_timestamp = Column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False
    )
    upload_api_key_id = Column(String(100), nullable=True)  # Which API key was used

    # Create composite indexes for common queries
    __table_args__ = (
        # Primary query patterns
        Index("idx_system_talkgroup", "system_id", "talkgroup_id"),
        Index("idx_timestamp_system", "call_timestamp", "system_id"),
        Index("idx_talkgroup_timestamp", "talkgroup_id", "call_timestamp"),
        # Date range queries
        Index("idx_created_at_desc", "created_at", postgresql_using="btree"),
        # Frequency analysis
        Index("idx_frequency_system", "frequency", "system_id"),
        # Source tracking
        Index("idx_source_system", "source_radio_id", "system_id"),
        # Recent calls query optimization
        Index("idx_recent_calls", "system_id", "call_timestamp", "talkgroup_id"),
    )


class UploadLog(Base):
    """Table for logging all upload attempts (for security and debugging)."""

    __tablename__ = "upload_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True
    )

    # Request information
    client_ip = Column(String(45), nullable=False, index=True)
    user_agent = Column(String(500), nullable=True)
    api_key_used = Column(String(100), nullable=True)

    # Upload details
    system_id = Column(String(50), nullable=True)
    success = Column(Boolean, nullable=False, default=True)
    error_message = Column(Text, nullable=True)

    # File details (if upload was attempted)
    filename = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    content_type = Column(String(100), nullable=True)

    # Response details
    response_code = Column(Integer, nullable=True)
    processing_time_ms = Column(Float, nullable=True)


class SystemStats(Base):
    """Aggregated statistics by system (updated periodically)."""

    __tablename__ = "system_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # System identification
    system_id = Column(String(50), nullable=False, unique=True, index=True)
    system_label = Column(String(255), nullable=True)

    # Call statistics
    total_calls = Column(Integer, default=0, nullable=False)
    calls_today = Column(Integer, default=0, nullable=False)
    calls_this_hour = Column(Integer, default=0, nullable=False)

    # Timing
    first_seen = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)

    # Top talkgroups (JSON stored as text)
    top_talkgroups = Column(Text, nullable=True)  # JSON: {"tg_id": count, ...}

    # Upload sources
    upload_sources = Column(Text, nullable=True)  # JSON: {"ip": count, ...}

    # Update tracking
    last_updated = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
