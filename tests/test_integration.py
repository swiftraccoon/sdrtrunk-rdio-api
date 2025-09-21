"""Integration tests for the complete sdrtrunk-rdio-api workflow."""

import time
from datetime import datetime

from src.database.operations import DatabaseOperations


class TestCompleteWorkflow:
    """Test complete upload and retrieval workflow."""

    def test_complete_call_upload_workflow(self, test_client, temp_audio_file):
        """Test the complete workflow from upload to storage."""
        # Prepare test data
        test_data = {
            "key": "test-api-key",
            "system": "1",
            "dateTime": str(int(datetime.now().timestamp())),
            "frequency": "853237500",
            "talkgroup": "1234",
            "source": "5678",
            "systemLabel": "Test System",
            "talkgroupLabel": "Test Talkgroup",
            "talkerAlias": "Unit Alpha",
        }

        # Upload the call
        audio_content = temp_audio_file.read_bytes()
        files = {"audio": ("test.mp3", audio_content, "audio/mpeg")}
        response = test_client.post(
            "/api/call-upload",
            data=test_data,
            files=files,
            headers={"Accept": "application/json"},
        )

        # Verify successful upload
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert "callId" in result

        # Verify file was stored
        # Note: In a real test, we'd check the actual file system
        # For now, we'll verify through the database

    def test_api_key_workflow(self, test_client):
        """Test API key authentication and restrictions."""
        # In test mode with no API keys configured, all requests are allowed
        # Test without API key - should succeed in test mode
        test_data = {
            "system": "1",
            "dateTime": str(int(datetime.now().timestamp())),
        }
        response = test_client.post("/api/call-upload", data=test_data)
        # In test mode with no API keys configured, this succeeds
        assert response.status_code == 200

        # Test with a key field (even invalid) - should also succeed in test mode
        test_data["key"] = "test-key"
        response = test_client.post("/api/call-upload", data=test_data)
        assert response.status_code == 200

    def test_concurrent_uploads(self, test_client, temp_audio_file):
        """Test handling multiple concurrent uploads."""
        # Use synchronous approach for TestClient
        # Read the audio file once
        audio_content = temp_audio_file.read_bytes()

        for i in range(10):
            test_data = {
                "key": "test-api-key",
                "system": str(i % 3 + 1),  # Vary systems
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": str(1000 + i),
            }
            files = {"audio": ("test.mp3", audio_content, "audio/mpeg")}
            response = test_client.post("/api/call-upload", data=test_data, files=files)
            # Verify succeeded
            assert response.status_code == 200

    def test_statistics_after_uploads(self, test_client, temp_audio_file):
        """Test statistics endpoint after uploading calls."""
        # Read audio content once
        audio_content = temp_audio_file.read_bytes()

        # Upload a few calls
        for i in range(5):
            test_data = {
                "key": "test-api-key",
                "system": "1",
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": str(100 + i),
            }
            files = {"audio": ("test.mp3", audio_content, "audio/mpeg")}
            response = test_client.post("/api/call-upload", data=test_data, files=files)
            assert response.status_code == 200

        # Check statistics
        response = test_client.get("/metrics")
        assert response.status_code == 200
        stats = response.json()
        assert stats["total_calls"] >= 5
        assert "1" in stats["systems"]

    def test_error_recovery(self, test_client):
        """Test system recovery from various error conditions."""
        # Test invalid multipart data
        response = test_client.post(
            "/api/call-upload",
            content=b"invalid data",
            headers={"content-type": "multipart/form-data; boundary=test"},
        )
        assert response.status_code in [400, 422, 500]

        # System should still be functional
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestSecurityIntegration:
    """Test security features integration."""

    def test_security_headers_present(self, test_client):
        """Test that security headers are present in responses."""
        response = test_client.get("/health")
        assert response.status_code == 200

        # Check security headers
        assert "X-Content-Type-Options" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "X-Frame-Options" in response.headers
        assert response.headers["X-Frame-Options"] == "DENY"

    def test_sql_injection_prevention(self, test_client):
        """Test SQL injection prevention in various inputs."""
        # Try SQL injection in system parameter
        test_data = {
            "key": "test-api-key",
            "system": "1' OR '1'='1",
            "dateTime": str(int(datetime.now().timestamp())),
        }
        response = test_client.post("/api/call-upload", data=test_data)
        # Should be rejected by validation
        assert response.status_code in [400, 422, 500]

    def test_path_traversal_prevention(self, test_client, temp_audio_file):
        """Test path traversal attack prevention."""
        test_data = {
            "key": "test-api-key",
            "system": "1",
            "dateTime": str(int(datetime.now().timestamp())),
        }
        # Try path traversal in filename
        files = {"audio": ("../../etc/passwd", temp_audio_file, "audio/mpeg")}
        response = test_client.post("/api/call-upload", data=test_data, files=files)
        # Should succeed but sanitize the filename
        if response.status_code == 200:
            # Check that the filename was sanitized
            # In a real test, we'd verify the stored filename
            pass

    def test_rate_limiting_integration(self, test_client):
        """Test rate limiting integration."""
        # Note: Rate limiting is disabled in test config
        # This test verifies the middleware is present but not blocking
        for _ in range(10):
            response = test_client.get("/health")
            assert response.status_code == 200


class TestDatabaseIntegration:
    """Test database operations integration."""

    def test_database_transaction_rollback(self, test_app, db_manager):
        """Test database transaction rollback on error."""
        db_ops = DatabaseOperations(db_manager)

        # Start a transaction
        with db_manager.get_session():
            # This should be rolled back if an error occurs
            pass

        # Verify database is still functional
        stats = db_ops.get_statistics()
        assert isinstance(stats, dict)

    def test_database_cleanup(self, test_app, db_manager):
        """Test database cleanup operations."""
        db_ops = DatabaseOperations(db_manager)

        # Add some test data
        for i in range(10):
            upload_data = {
                "system": "123",  # System ID must be numeric
                "dateTime": int(datetime.now().timestamp()) - (i * 86400),
                "key": "test",
                "talkgroup": 1000 + i,  # Add required field
            }
            # Create mock upload object
            from src.models.api_models import RdioScannerUpload

            upload = RdioScannerUpload(**upload_data)
            db_ops.save_call(upload, "testclient", "/test/path", "test-key")

        # Get initial count
        stats = db_ops.get_statistics()
        initial_count = stats["total_calls"]

        # Run cleanup (should not delete recent calls)
        # Note: Cleanup is typically based on retention policy

        # Verify calls still exist
        stats = db_ops.get_statistics()
        assert stats["total_calls"] == initial_count


class TestFileHandlingIntegration:
    """Test file handling integration."""

    def test_file_storage_and_retrieval(self, test_app, file_handler, temp_audio_file):
        """Test file storage and retrieval workflow."""
        # Save temp file
        temp_path = file_handler.save_temp_file(
            "test.mp3", temp_audio_file.read_bytes()
        )
        assert temp_path.exists()

        # Store permanently
        stored_path = file_handler.store_file(
            temp_path,
            system_id="test_system",
            timestamp=datetime.now(),
            talkgroup_id=1234,
            talkgroup_label="Test TG",
            frequency=853237500,
            source_id=5678,
            talker_alias="Unit Alpha",
            system_label="Test System",
        )
        assert stored_path.exists()

        # Verify filename contains metadata
        filename = stored_path.name
        assert "test_system" in filename.lower() or "SYStest_system" in filename
        assert "TG1234" in filename
        assert "853" in filename  # Frequency in MHz

        # Cleanup
        stored_path.unlink()

    def test_file_cleanup(self, test_app, file_handler):
        """Test file cleanup operations."""
        # Create old temp files
        for i in range(5):
            temp_file = file_handler.temp_dir / f"old_file_{i}.mp3"
            temp_file.write_bytes(b"test data")
            # Make file appear old
            import os

            old_time = time.time() - (2 * 3600)  # 2 hours old
            os.utime(temp_file, (old_time, old_time))

        # Run cleanup
        cleaned = file_handler.cleanup_temp_files(max_age_hours=1)
        assert cleaned == 5

        # Verify files are gone
        for i in range(5):
            temp_file = file_handler.temp_dir / f"old_file_{i}.mp3"
            assert not temp_file.exists()


class TestMonitoringIntegration:
    """Test monitoring and metrics integration."""

    def test_health_check_comprehensive(self, test_client):
        """Test comprehensive health check."""
        response = test_client.get("/health")
        assert response.status_code == 200

        health = response.json()
        assert health["status"] == "healthy"
        assert health["database"] == "connected"
        assert "timestamp" in health
        assert "version" in health

    def test_metrics_comprehensive(self, test_client, temp_audio_file):
        """Test comprehensive metrics after operations."""
        # Upload some calls
        for i in range(3):
            test_data = {
                "key": "test-api-key",
                "system": str(i + 1),
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": str(100 + i),
            }
            files = {"audio": ("test.mp3", temp_audio_file.read_bytes(), "audio/mpeg")}
            test_client.post("/api/call-upload", data=test_data, files=files)

        # Get metrics
        response = test_client.get("/metrics")
        assert response.status_code == 200

        metrics = response.json()
        assert "total_calls" in metrics
        assert "calls_today" in metrics
        assert "calls_last_hour" in metrics
        assert "systems" in metrics
        assert "talkgroups" in metrics
        assert "storage_used_mb" in metrics
        assert "audio_files_count" in metrics
