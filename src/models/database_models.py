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
    # New rows use a storage-root-relative POSIX reference. Legacy rows may
    # still contain an absolute path until an operator performs migration.
    audio_file_path = Column(String(500), nullable=True)

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
        # Date range and retention queries. SQLite stores the INTEGER PRIMARY
        # KEY rowid in each secondary-index entry, so this index also satisfies
        # deterministic ORDER BY created_at, id without a duplicate composite.
        Index("idx_created_at_desc", "created_at", postgresql_using="btree"),
        Index("idx_audio_file_path", "audio_file_path"),
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


class PendingFileDeletion(Base):
    """Durable queue of audio files whose owning call rows were removed."""

    __tablename__ = "pending_file_deletions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    path = Column(String(500), nullable=False, unique=True)
    queued_at = Column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    kind = Column(String(16), nullable=False, default="retention")
    attempt_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token = Column(String(64), nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    last_error = Column(String(512), nullable=True)

    __table_args__ = (
        # The scheduler polls the earliest unclaimed retry deadline every
        # maintenance cycle. Keeping claim state first makes that MIN lookup a
        # bounded covering-index operation even with a large failure backlog.
        Index(
            "idx_pending_claim_next_attempt",
            "claim_token",
            "next_attempt_at",
        ),
        # Claimed rows are leased and scheduled by their oldest claim time.
        # SQLite's implicit rowid suffix also satisfies deterministic id ties.
        Index("idx_pending_claimed_at", "claimed_at"),
    )
