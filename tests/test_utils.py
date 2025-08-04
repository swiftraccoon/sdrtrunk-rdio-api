"""Tests for utility modules."""

from pathlib import Path

import pytest

from src.utils.file_handler import FileHandler
from src.utils.multipart_parser import parse_multipart_form


class TestFileHandler:
    """Tests for FileHandler utility."""

    def test_initialization(self, temp_dir: Path) -> None:
        """Test FileHandler initialization."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=True,
            accepted_formats=[".mp3"],
        )
        assert handler.storage_dir.exists()
        assert handler.temp_dir.exists()

    def test_validate_file(self, temp_dir: Path) -> None:
        """Test file validation."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=False,
            accepted_formats=[".mp3"],
        )

        # Valid MP3 file
        valid, msg = handler.validate_file(
            "test.mp3",
            b"ID3" + b"\x00" * 1024,  # Simplified MP3 data with ID3 header
            "audio/mpeg",
        )
        assert valid is True
        assert msg is None

        # Invalid format
        valid, msg = handler.validate_file(
            "test.wav", b"RIFF" + b"\x00" * 100, "audio/wav"
        )
        assert valid is False
        assert msg is not None and "not accepted" in msg

        # File too small
        valid, msg = handler.validate_file(
            "test.mp3", b"ID3", "audio/mpeg"  # Only 3 bytes
        )
        assert valid is False
        assert msg is not None and "too small" in msg

    def test_save_temp_file(self, temp_dir: Path) -> None:
        """Test saving temporary file."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=False,
            accepted_formats=[".mp3"],
        )

        content = b"test content" * 100
        temp_path = handler.save_temp_file("test.mp3", content)

        assert temp_path.exists()
        assert temp_path.parent == handler.temp_dir
        assert temp_path.read_bytes() == content

    def test_store_file(self, temp_dir: Path) -> None:
        """Test storing file permanently."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=False,
            accepted_formats=[".mp3"],
        )

        # Create temp file
        temp_file = handler.temp_dir / "test.mp3"
        temp_file.write_bytes(b"test content")

        # Store it
        from datetime import datetime

        stored_path = handler.store_file(
            temp_file, system_id="123", timestamp=datetime.now(), talkgroup_id=100
        )

        assert stored_path.exists()
        assert stored_path.parent == handler.storage_dir / "123"
        assert not temp_file.exists()  # Should be moved

    def test_organize_by_date(self, temp_dir: Path) -> None:
        """Test date-based organization."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=True,
            accepted_formats=[".mp3"],
        )

        # Create temp file
        temp_file = handler.temp_dir / "test.mp3"
        temp_file.write_bytes(b"test content")

        # Store it
        from datetime import datetime

        timestamp = datetime(2024, 1, 15, 10, 30, 45)
        stored_path = handler.store_file(
            temp_file, system_id="123", timestamp=timestamp, talkgroup_id=100
        )

        # Check path includes date components
        assert "2024" in str(stored_path)
        assert "01" in str(stored_path)
        assert "15" in str(stored_path)
        assert "123" in str(stored_path)  # System ID

    def test_cleanup_temp_files(self, temp_dir: Path) -> None:
        """Test cleaning up old temp files."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=False,
            accepted_formats=[".mp3"],
        )

        # Create some temp files
        old_file = handler.temp_dir / "old.mp3"
        old_file.write_bytes(b"old")
        # Make it old by modifying mtime
        import time

        old_time = time.time() - (25 * 3600)  # 25 hours ago
        import os

        os.utime(old_file, (old_time, old_time))

        new_file = handler.temp_dir / "new.mp3"
        new_file.write_bytes(b"new")

        # Cleanup
        cleaned = handler.cleanup_temp_files(max_age_hours=24)

        assert cleaned == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_get_storage_stats(self, temp_dir: Path) -> None:
        """Test getting storage statistics."""
        handler = FileHandler(
            storage_directory=str(temp_dir / "storage"),
            temp_directory=str(temp_dir / "temp"),
            organize_by_date=False,
            accepted_formats=[".mp3"],
        )

        # Create some files
        (handler.storage_dir / "123").mkdir(parents=True)
        file1 = handler.storage_dir / "123" / "file1.mp3"
        file1.write_bytes(b"x" * 1024)
        file2 = handler.storage_dir / "123" / "file2.mp3"
        file2.write_bytes(b"y" * 2048)

        stats = handler.get_storage_stats()

        assert stats["total_files"] == 2
        assert stats["total_size_bytes"] == 3072
        assert stats["total_size_mb"] == pytest.approx(0.00293, rel=0.01)
        assert "123" in stats["by_system"]
        assert stats["by_system"]["123"]["count"] == 2


class TestMultipartParser:
    """Tests for multipart form parser."""

    def test_parse_simple_form(self) -> None:
        """Test parsing simple multipart form."""
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        content = (
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
            b'Content-Disposition: form-data; name="field1"\r\n'
            b"\r\n"
            b"value1\r\n"
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
            b'Content-Disposition: form-data; name="field2"\r\n'
            b"\r\n"
            b"value2\r\n"
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW--\r\n"
        )

        fields, files = parse_multipart_form(content, boundary)
        assert fields["field1"] == "value1"
        assert fields["field2"] == "value2"
        assert len(files) == 0

    def test_parse_with_file(self) -> None:
        """Test parsing multipart form with file."""
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        content = (
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
            b'Content-Disposition: form-data; name="field"\r\n'
            b"\r\n"
            b"value\r\n"
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"file content\r\n"
            b"------WebKitFormBoundary7MA4YWxkTrZu0gW--\r\n"
        )

        fields, files = parse_multipart_form(content, boundary)
        assert fields["field"] == "value"
        assert "file" in files
        assert files["file"]["filename"] == "test.txt"
        assert files["file"]["content"] == b"file content"
        assert files["file"]["content_type"] == "text/plain"

    def test_parse_sdrtrunk_format(self) -> None:
        """Test parsing SDRTrunk's specific multipart format."""
        boundary = "--sdrtrunk-sdrtrunk-sdrtrunk"
        content = (
            b"----sdrtrunk-sdrtrunk-sdrtrunk\r\n"
            b'Content-Disposition: form-data; name="key"\r\n'
            b"\r\n"
            b"test-api-key\r\n"
            b"----sdrtrunk-sdrtrunk-sdrtrunk\r\n"
            b'Content-Disposition: form-data; name="system"\r\n'
            b"\r\n"
            b"123\r\n"
            b"----sdrtrunk-sdrtrunk-sdrtrunk\r\n"
            b'Content-Disposition: form-data; name="dateTime"\r\n'
            b"\r\n"
            b"1234567890\r\n"
            b"----sdrtrunk-sdrtrunk-sdrtrunk\r\n"
            b'Content-Disposition: form-data; filename="audio.mp3"; name="audio"\r\n'
            b"\r\n"
            b"MP3_DATA_HERE\r\n"
            b"----sdrtrunk-sdrtrunk-sdrtrunk--\r\n"
        )

        fields, files = parse_multipart_form(content, boundary)
        assert fields["key"] == "test-api-key"
        assert fields["system"] == "123"
        assert fields["dateTime"] == "1234567890"
        assert "audio" in files
        assert files["audio"]["filename"] == "audio.mp3"
        assert files["audio"]["content"] == b"MP3_DATA_HERE"
