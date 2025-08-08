"""File handling utilities for audio file storage and management."""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FileHandler:
    """Handles audio file storage and management."""

    def __init__(
        self,
        storage_directory: str,
        temp_directory: str,
        organize_by_date: bool = True,
        accepted_formats: list[str] | None = None,
        max_file_size_mb: int = 100,
        min_file_size_kb: int = 1,
    ):
        """Initialize file handler.

        Args:
            storage_directory: Directory for permanent file storage
            temp_directory: Directory for temporary files
            organize_by_date: Whether to organize files by date
            accepted_formats: List of accepted file extensions
            max_file_size_mb: Maximum file size in megabytes
            min_file_size_kb: Minimum file size in kilobytes
        """
        self.storage_dir = Path(storage_directory)
        self.temp_dir = Path(temp_directory)
        self.organize_by_date = organize_by_date
        self.accepted_formats = accepted_formats or [".mp3"]
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.min_file_size_bytes = min_file_size_kb * 1024

        # Create directories
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"File handler initialized - Storage: {self.storage_dir}, Temp: {self.temp_dir}"
        )

    def validate_file(
        self, filename: str, content: bytes, content_type: str | None = None
    ) -> tuple[bool, str | None]:
        """Validate an uploaded file.

        Args:
            filename: Original filename
            content: File content as bytes
            content_type: MIME type

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check file extension
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.accepted_formats:
            return (
                False,
                f"File format '{file_ext}' not accepted. Accepted formats: {', '.join(self.accepted_formats)}",
            )

        # Check file size
        file_size = len(content)
        if file_size == 0:
            return False, "File is empty"

        if file_size > self.max_file_size_bytes:
            return (
                False,
                f"File too large ({file_size / (1024*1024):.1f} MB > {self.max_file_size_bytes / (1024*1024):.0f} MB)",
            )

        if file_size < self.min_file_size_bytes:
            return (
                False,
                f"File too small ({file_size} bytes < {self.min_file_size_bytes / 1024:.0f} KB)",
            )

        # Basic content validation for MP3
        if file_ext == ".mp3":
            # Check for MP3 magic bytes
            if len(content) >= 3:
                # ID3v2 tag
                if content[:3] == b"ID3":
                    logger.debug(f"File {filename} has ID3v2 tag")
                # MPEG Audio frame sync
                elif (
                    len(content) >= 2
                    and content[0] == 0xFF
                    and (content[1] & 0xE0) == 0xE0
                ):
                    logger.debug(f"File {filename} has MPEG audio frame sync")
                else:
                    # Some MP3s might not start with these markers, so just log warning
                    logger.warning(f"File {filename} doesn't have typical MP3 markers")

        return True, None

    def save_temp_file(self, filename: str, content: bytes) -> Path:
        """Save content to a temporary file.

        Args:
            filename: Original filename
            content: File content

        Returns:
            Path to temporary file
        """
        # Generate unique filename with timestamp including microseconds
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[
            :21
        ]  # Trim to 5 decimal places
        safe_filename = f"{timestamp}_{Path(filename).name}"

        temp_path = self.temp_dir / safe_filename

        # Write file
        temp_path.write_bytes(content)
        logger.debug(f"Saved temp file: {temp_path}")

        return temp_path

    def store_file(
        self,
        temp_path: Path,
        system_id: str,
        timestamp: datetime,
        talkgroup_id: int | None = None,
        talkgroup_label: str | None = None,
        frequency: int | None = None,
        source_id: int | None = None,
        talker_alias: str | None = None,
        system_label: str | None = None,
    ) -> Path:
        """Move file from temp to permanent storage with descriptive filename.

        Args:
            temp_path: Path to temporary file
            system_id: System ID
            timestamp: Call timestamp
            talkgroup_id: Talkgroup ID
            talkgroup_label: Human-readable talkgroup label
            frequency: Frequency in Hz
            source_id: Source radio ID
            talker_alias: Talker alias/name
            system_label: Human-readable system label

        Returns:
            Path to stored file
        """
        # Build storage path
        if self.organize_by_date:
            # Organize by date: storage/YYYY/MM/DD/system_id/
            date_path = timestamp.strftime("%Y/%m/%d")
            storage_subdir = self.storage_dir / date_path / system_id
        else:
            # Flat organization: storage/system_id/
            storage_subdir = self.storage_dir / system_id

        storage_subdir.mkdir(parents=True, exist_ok=True)

        # Build verbose filename with all available metadata
        # Format: YYYYMMDD_HHMMSS_SYS[system]_TG[id]_[label]_FREQ[freq]_SRC[id]_[alias].ext
        components = []

        # Timestamp (always present)
        components.append(timestamp.strftime("%Y%m%d_%H%M%S"))

        # System info
        sys_str = f"SYS{system_id}"
        if system_label:
            # Sanitize label for filename
            safe_label = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in system_label
            )[:30]
            sys_str = f"{sys_str}_{safe_label}"
        components.append(sys_str)

        # Talkgroup info
        if talkgroup_id:
            tg_str = f"TG{talkgroup_id}"
            if talkgroup_label:
                # Sanitize label for filename
                safe_label = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in talkgroup_label
                )[:30]
                tg_str = f"{tg_str}_{safe_label}"
            components.append(tg_str)

        # Frequency info (convert Hz to MHz for readability)
        if frequency:
            freq_mhz = frequency / 1_000_000
            components.append(f"FREQ{freq_mhz:.4f}MHz")

        # Source/Unit info
        if source_id:
            src_str = f"SRC{source_id}"
            if talker_alias:
                # Sanitize alias for filename
                safe_alias = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in talker_alias
                )[:20]
                src_str = f"{src_str}_{safe_alias}"
            components.append(src_str)

        # Join all components with underscores
        base_filename = "_".join(components)

        # Add file extension
        filename = f"{base_filename}{temp_path.suffix}"

        # Handle duplicates by appending counter
        storage_path = storage_subdir / filename
        counter = 1
        while storage_path.exists():
            filename = f"{base_filename}_DUP{counter}{temp_path.suffix}"
            storage_path = storage_subdir / filename
            counter += 1

        # Move file
        shutil.move(str(temp_path), str(storage_path))
        logger.info(f"Stored file: {storage_path}")

        return storage_path

    def cleanup_temp_files(self, max_age_hours: int = 1) -> int:
        """Clean up old temporary files.

        Args:
            max_age_hours: Maximum age of temp files in hours

        Returns:
            Number of files cleaned up
        """
        now = datetime.now()
        cleaned = 0

        for temp_file in self.temp_dir.iterdir():
            if temp_file.is_file():
                # Check file age
                file_age = now - datetime.fromtimestamp(temp_file.stat().st_mtime)
                if file_age.total_seconds() > max_age_hours * 3600:
                    try:
                        temp_file.unlink()
                        cleaned += 1
                    except Exception as e:
                        logger.error(f"Failed to delete temp file {temp_file}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old temp files")
        return cleaned

    def cleanup_old_files(self, retention_days: int) -> tuple[int, float]:
        """Clean up old stored files based on retention policy.

        Args:
            retention_days: Number of days to retain files
        """
        if retention_days <= 0:
            return 0, 0.0  # No cleanup if retention is 0 or negative

        now = datetime.now()
        cleaned = 0
        freed_space = 0

        # Walk through all files in storage
        for file_path in self.storage_dir.rglob("*"):
            if file_path.is_file():
                # Check file age
                file_age = now - datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_age.days > retention_days:
                    try:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        cleaned += 1
                        freed_space += file_size
                    except Exception as e:
                        logger.error(f"Failed to delete old file {file_path}: {e}")

        if cleaned > 0:
            logger.info(
                f"Cleaned up {cleaned} old files, freed {freed_space / (1024*1024):.2f} MB"
            )
        return cleaned, freed_space

    def get_storage_stats(self) -> dict[str, Any]:
        """Get storage statistics.

        Returns:
            Dictionary with storage stats
        """
        stats: dict[str, Any] = {
            "total_files": 0,
            "total_size_bytes": 0,
            "total_size_mb": 0,
            "by_system": {},  # Changed key name
            "files_by_date": {},
        }

        # Walk through storage directory
        for file_path in self.storage_dir.rglob("*"):
            if file_path.is_file():
                stats["total_files"] += 1
                file_size = file_path.stat().st_size
                stats["total_size_bytes"] += file_size
                stats["total_size_mb"] += file_size / (1024 * 1024)

                # Extract system from path
                parts = file_path.relative_to(self.storage_dir).parts
                if parts:
                    if self.organize_by_date and len(parts) > 3:
                        # Date organized: YYYY/MM/DD/system/file
                        system = parts[3]
                        date = f"{parts[0]}-{parts[1]}-{parts[2]}"

                        if system not in stats["by_system"]:
                            stats["by_system"][system] = {"count": 0, "size_bytes": 0}
                        stats["by_system"][system]["count"] += 1
                        stats["by_system"][system]["size_bytes"] += file_size

                        stats["files_by_date"][date] = (
                            stats["files_by_date"].get(date, 0) + 1
                        )
                    elif not self.organize_by_date and len(parts) > 0:
                        # Flat organized: system/file
                        system = parts[0]
                        if system not in stats["by_system"]:
                            stats["by_system"][system] = {"count": 0, "size_bytes": 0}
                        stats["by_system"][system]["count"] += 1
                        stats["by_system"][system]["size_bytes"] += file_size

        return stats
