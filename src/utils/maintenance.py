"""Retention and cleanup maintenance shared by the CLI and the server.

Implements the documented ``retention_days`` behavior: calls (metadata),
their audio files, and upload logs older than the cutoff are removed
together, keeping the database and the filesystem consistent.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..database.operations import DatabaseOperations
from .file_handler import FileHandler

logger = logging.getLogger(__name__)


def run_retention_cleanup(
    db_ops: DatabaseOperations,
    file_handler: FileHandler,
    retention_days: int,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Delete calls, upload logs, and audio files older than the cutoff.

    Args:
        db_ops: Database operations
        file_handler: File handler for the audio storage directory
        retention_days: Age threshold in days; <= 0 disables cleanup
        vacuum: Vacuum the database afterwards (slow; CLI use only)

    Returns:
        Summary dict with deleted_calls, deleted_upload_logs,
        deleted_files, and freed_bytes.
    """
    summary: dict[str, Any] = {
        "deleted_calls": 0,
        "deleted_upload_logs": 0,
        "deleted_files": 0,
        "freed_bytes": 0,
    }

    if retention_days <= 0:
        return summary

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    # Delete files referenced by expiring rows first, so a failure here
    # leaves rows pointing at existing files rather than the reverse.
    audio_paths = db_ops.get_audio_paths_older_than(cutoff)
    deleted_files, freed_bytes = file_handler.delete_files(audio_paths)

    summary["deleted_calls"] = db_ops.delete_calls_older_than(cutoff)
    summary["deleted_upload_logs"] = db_ops.delete_upload_logs_older_than(cutoff)

    # Sweep orphaned files the database does not know about (by mtime)
    orphan_files, orphan_bytes = file_handler.cleanup_old_files(retention_days)
    summary["deleted_files"] = deleted_files + orphan_files
    summary["freed_bytes"] = freed_bytes + orphan_bytes

    file_handler.remove_empty_directories()

    if vacuum and (summary["deleted_calls"] or summary["deleted_upload_logs"]):
        db_ops.db_manager.vacuum()

    if any(summary.values()):
        logger.info(f"Retention cleanup ({retention_days}d): {summary}")
    return summary


def run_temp_cleanup(file_handler: FileHandler, max_age_hours: int = 1) -> int:
    """Remove stale temp files left behind by failed uploads."""
    return file_handler.cleanup_temp_files(max_age_hours=max_age_hours)
