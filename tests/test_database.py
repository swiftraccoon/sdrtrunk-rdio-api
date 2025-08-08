"""Tests for database operations."""

from datetime import UTC, datetime, timedelta
from typing import Any

from src.database.connection import DatabaseManager
from src.database.operations import DatabaseOperations
from src.models.api_models import RdioScannerUpload
from src.models.database_models import RadioCall


def create_test_upload(**overrides: Any) -> RdioScannerUpload:
    """Create a test RdioScannerUpload with all optional fields set to None."""
    defaults: dict[str, Any] = {
        "key": "test-key",
        "system": "123",
        "dateTime": 1234567890,
        "audio_filename": None,
        "audio_content_type": None,
        "audio_size": None,
        "frequency": None,
        "talkgroup": None,
        "source": None,
        "systemLabel": None,
        "talkgroupLabel": None,
        "talkgroupGroup": None,
        "talkerAlias": None,
        "patches": None,
        "frequencies": None,
        "sources": None,
        "talkgroupTag": None,
        "test": None,
    }
    defaults.update(overrides)
    return RdioScannerUpload(**defaults)


class TestDatabaseManager:
    """Tests for DatabaseManager."""

    def test_database_creation(self, db_manager: DatabaseManager) -> None:
        """Test that database is created properly."""
        # Check that we can get stats (which means tables exist)
        stats = db_manager.get_stats()
        assert "size_mb" in stats
        assert "tables" in stats
        assert stats["tables"]["radio_calls"] == 0

    def test_get_session(self, db_manager: DatabaseManager) -> None:
        """Test getting database session."""
        with db_manager.get_session() as session:
            # Should be able to query
            count = session.query(RadioCall).count()
            assert count == 0


class TestDatabaseOperations:
    """Tests for DatabaseOperations."""

    def test_save_radio_call(self, db_manager: DatabaseManager) -> None:
        """Test saving a radio call."""
        db_ops = DatabaseOperations(db_manager)

        upload_data = create_test_upload(
            audio_filename="test.mp3",
            audio_content_type="audio/mpeg",
            audio_size=1024,
            frequency=854037500,
            talkgroup=100,
            source=200,
            systemLabel="Test System",
            talkgroupLabel="Test TG",
        )

        call_id = db_ops.save_radio_call(
            upload_data,
            audio_file_path="/tmp/test.mp3",
            upload_ip="127.0.0.1",
            api_key_id="test-key-1",
        )

        assert call_id > 0

        # Verify it was saved
        with db_manager.get_session() as session:
            call = session.query(RadioCall).filter_by(id=call_id).first()
            assert call is not None
            assert call.system_id == "123"
            assert call.talkgroup_id == 100
            assert call.source_radio_id == 200

    def test_get_recent_calls(self, db_manager: DatabaseManager) -> None:
        """Test getting recent calls."""
        db_ops = DatabaseOperations(db_manager)

        # Save some calls
        for i in range(5):
            upload_data = create_test_upload(
                dateTime=1234567890 + i,
                talkgroup=100 + i,
            )
            db_ops.save_radio_call(upload_data)

        # Get recent calls
        calls = db_ops.get_recent_calls(limit=3)
        assert len(calls) == 3

        # Should be ordered by timestamp descending
        assert calls[0].talkgroup_id == 104  # Most recent
        assert calls[1].talkgroup_id == 103
        assert calls[2].talkgroup_id == 102

    def test_get_recent_calls_with_filters(self, db_manager: DatabaseManager) -> None:
        """Test getting recent calls with filters."""
        db_ops = DatabaseOperations(db_manager)

        # Save calls for different systems
        for system in ["123", "456"]:
            for i in range(3):
                upload_data = create_test_upload(
                    system=system,
                    dateTime=1234567890 + i,
                    talkgroup=100,
                )
                db_ops.save_radio_call(upload_data)

        # Filter by system
        calls = db_ops.get_recent_calls(system_id="123")
        assert len(calls) == 3
        assert all(c.system_id == "123" for c in calls)

        # Filter by talkgroup
        calls = db_ops.get_recent_calls(talkgroup_id=100)
        assert len(calls) == 6  # All have talkgroup 100

    def test_get_statistics(self, db_manager: DatabaseManager) -> None:
        """Test getting statistics."""
        db_ops = DatabaseOperations(db_manager)

        # Save some test data
        now = datetime.now(UTC)
        timestamps = [
            int(now.timestamp()),  # Now
            int((now - timedelta(hours=0.5)).timestamp()),  # 30 min ago
            int((now - timedelta(hours=2)).timestamp()),  # 2 hours ago
            int((now - timedelta(days=2)).timestamp()),  # 2 days ago
        ]

        for i, ts in enumerate(timestamps):
            upload_data = create_test_upload(
                system="123" if i < 2 else "456",
                dateTime=ts,
                talkgroup=100 if i % 2 == 0 else 200,
                audio_size=1024 * (i + 1),
            )
            db_ops.save_radio_call(upload_data, upload_ip="127.0.0.1")

        stats = db_ops.get_statistics()

        assert stats["total_calls"] == 4
        assert stats["calls_today"] >= 2  # At least the recent ones
        assert stats["calls_last_hour"] >= 2
        assert "123" in stats["systems"]
        assert "456" in stats["systems"]
        assert stats["storage_used_mb"] > 0

    def test_log_upload_attempt(self, db_manager: DatabaseManager) -> None:
        """Test logging upload attempts."""
        db_ops = DatabaseOperations(db_manager)

        # Log successful attempt
        db_ops.log_upload_attempt(
            client_ip="127.0.0.1",
            success=True,
            system_id="123",
            api_key_used="test-key",
            user_agent="SDRTrunk/1.0",
            filename="test.mp3",
            file_size=1024,
            content_type="audio/mpeg",
            response_code=200,
            processing_time_ms=100.5,
        )

        # Log failed attempt
        db_ops.log_upload_attempt(
            client_ip="192.168.1.100",
            success=False,
            error_message="Invalid API key",
            response_code=401,
        )

        # Verify logs exist (would need to add method to query upload logs)
        # For now, just ensure no exceptions

    def test_cleanup_old_data(self, db_manager: DatabaseManager) -> None:
        """Test cleaning up old data."""
        db_ops = DatabaseOperations(db_manager)

        # Save some old and new calls
        now = datetime.now(UTC)
        old_timestamp = int((now - timedelta(days=40)).timestamp())
        new_timestamp = int(now.timestamp())

        # Old call
        old_upload = create_test_upload(
            dateTime=old_timestamp,
        )
        old_id = db_ops.save_radio_call(old_upload)

        # New call
        new_upload = create_test_upload(
            dateTime=new_timestamp,
        )
        new_id = db_ops.save_radio_call(new_upload)

        # Clean up data older than 30 days
        db_ops.cleanup_old_data(days_to_keep=30)

        # Check that old call is gone, new call remains
        with db_manager.get_session() as session:
            old_call = session.query(RadioCall).filter_by(id=old_id).first()
            new_call = session.query(RadioCall).filter_by(id=new_id).first()

            assert old_call is None
            assert new_call is not None

    def test_query_calls_with_all_filters(self, db_manager: DatabaseManager) -> None:
        """Test query calls with all filter options."""
        db_ops = DatabaseOperations(db_manager)

        # Add test data
        upload = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now().timestamp()),
            talkgroup=100,
            source=200,
            frequency=850000000,
        )
        db_ops.save_radio_call(upload)

        # Query with all filters
        result = db_ops.query_calls(
            filters={
                "system_id": "1",
                "talkgroup_id": 100,
                "source_radio_id": 200,
                "frequency": 850000000,
                "date_from": datetime.now() - timedelta(hours=1),
                "date_to": datetime.now() + timedelta(hours=1),
            },
            sort_by="frequency",
            sort_order="asc",
        )

        assert result["total"] == 1
        assert result["calls"][0]["system_id"] == "1"

    def test_get_systems_summary(self, db_manager: DatabaseManager) -> None:
        """Test getting systems summary."""
        db_ops = DatabaseOperations(db_manager)

        # Add test data for multiple systems
        for system in ["1", "2", "3"]:
            for i in range(3):
                upload = RdioScannerUpload(
                    key="test",
                    system=system,
                    dateTime=int(datetime.now().timestamp()),
                    talkgroup=100 + i,
                    systemLabel=f"System {system}",
                )
                db_ops.save_radio_call(upload)

        # Get systems summary
        summary = db_ops.get_systems_summary()

        assert len(summary) == 3
        for sys in summary:
            assert sys["total_calls"] == 3
            assert "top_talkgroups" in sys

    def test_get_talkgroups_summary(self, db_manager: DatabaseManager) -> None:
        """Test getting talkgroups summary."""
        db_ops = DatabaseOperations(db_manager)

        # Add test data
        for _ in range(5):
            upload = RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now().timestamp()),
                talkgroup=100,
                talkgroupLabel="Test TG",
            )
            db_ops.save_radio_call(upload)

        # Get talkgroups summary
        summary = db_ops.get_talkgroups_summary(min_calls=3)

        # Should have at least one talkgroup with 5 calls
        assert any(tg["total_calls"] >= 5 for tg in summary)

    def test_database_vacuum(self, db_manager: DatabaseManager) -> None:
        """Test database vacuum operation."""
        # Should not raise
        db_manager.vacuum()

        # Database should still be functional
        db_ops = DatabaseOperations(db_manager)
        stats = db_ops.get_statistics()
        assert isinstance(stats, dict)

    def test_get_call_by_id(self, db_manager: DatabaseManager) -> None:
        """Test retrieving specific call by ID."""
        db_ops = DatabaseOperations(db_manager)

        # Add a test call
        upload = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now().timestamp()),
            talkgroup=123,
        )
        call_id = db_ops.save_radio_call(upload)

        # Retrieve it
        call = db_ops.get_call_by_id(call_id)
        assert call is not None
        assert call["id"] == call_id

        # Non-existent ID
        assert db_ops.get_call_by_id(99999) is None
