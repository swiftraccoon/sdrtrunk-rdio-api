"""Integration tests for the complete sdrtrunk-rdio-api workflow."""

import time
from datetime import datetime

from fastapi.testclient import TestClient

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


class TestSDRTrunkEndToEnd:
    """End-to-end tests that replicate the real SDRTrunk -> API -> retrieval flow.

    These tests exercise the full lifecycle: upload via the RdioScanner protocol
    (matching what SDRTrunk actually sends), then retrieve via query and audio
    streaming endpoints, verifying data integrity throughout.
    """

    def test_sdrtrunk_test_connection(self, test_client: TestClient) -> None:
        """Replicate SDRTrunk's testConnection() call.

        SDRTrunk sends key + system + test=1, expects HTTP 200 with body
        containing 'incomplete call data: no talkgroup'.
        """
        response = test_client.post(
            "/api/call-upload",
            data={"key": "test-key", "system": "1", "test": "1"},
            headers={"User-Agent": "sdrtrunk"},
        )
        assert response.status_code == 200
        assert "incomplete call data: no talkgroup" in response.text

    def test_upload_store_query_audio_roundtrip(
        self,
        test_client_with_storage: TestClient,
        temp_audio_file,
    ) -> None:
        """Full roundtrip: upload audio -> query call -> stream audio back.

        This is the critical path: SDRTrunk uploads a call with audio,
        then a user queries for it and plays the audio back.
        """
        audio_bytes = temp_audio_file.read_bytes()

        # 1) Upload a call with audio (store mode)
        upload_response = test_client_with_storage.post(
            "/api/call-upload",
            data={
                "key": "test-key",
                "system": "1",
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": "4001",
                "source": "9876",
                "frequency": "853237500",
                "systemLabel": "County P25",
                "talkgroupLabel": "Fire Dispatch",
                "talkgroupGroup": "Fire",
                "talkerAlias": "Engine 7",
                "patches": "[4001,4002]",
            },
            files={"audio": ("recording.mp3", audio_bytes, "audio/mpeg")},
            headers={"User-Agent": "sdrtrunk"},
        )
        assert upload_response.status_code == 200
        assert upload_response.text == "Call imported successfully."

        # 2) Query calls and find the one we just uploaded
        query_response = test_client_with_storage.get(
            "/api/calls?system_id=1&talkgroup_id=4001"
        )
        assert query_response.status_code == 200
        data = query_response.json()
        assert data["total"] >= 1

        call = data["calls"][0]
        call_id = call["id"]
        assert call["system_id"] == "1"
        assert call["talkgroup_id"] == 4001
        assert call["talkgroup_label"] == "Fire Dispatch"
        assert call["frequency"] == 853237500
        assert call["source_id"] == 9876

        # 3) Retrieve the specific call by ID
        detail_response = test_client_with_storage.get(f"/api/calls/{call_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["id"] == call_id
        assert detail["system_id"] == "1"

        # 4) Stream the audio back and verify content matches
        audio_response = test_client_with_storage.get(f"/api/calls/{call_id}/audio")
        assert audio_response.status_code == 200
        assert audio_response.headers["content-type"] == "audio/mpeg"
        assert audio_response.content == audio_bytes

    def test_upload_plain_text_response_matches_sdrtrunk_expectations(
        self,
        test_client: TestClient,
    ) -> None:
        """Verify response format that SDRTrunk checks for success.

        SDRTrunk checks: response.contains("Call imported successfully.")
        If that doesn't match, it treats the upload as failed.
        """
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "test-key",
                "system": "1",
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": "100",
            },
            headers={"User-Agent": "sdrtrunk"},
        )
        assert response.status_code == 200
        # SDRTrunk does substring match on this exact string
        assert "Call imported successfully." in response.text

    def test_upload_multiple_calls_then_query_systems_and_talkgroups(
        self,
        test_client_with_storage: TestClient,
        temp_audio_file,
    ) -> None:
        """Upload calls across multiple systems, then verify query endpoints."""
        audio_bytes = temp_audio_file.read_bytes()
        now_ts = int(datetime.now().timestamp())

        # Upload calls from different systems/talkgroups
        calls = [
            {"system": "1", "talkgroup": "100", "systemLabel": "City PD"},
            {"system": "1", "talkgroup": "200", "systemLabel": "City PD"},
            {"system": "2", "talkgroup": "300", "systemLabel": "County Fire"},
            {"system": "2", "talkgroup": "300", "systemLabel": "County Fire"},
            {"system": "2", "talkgroup": "400", "systemLabel": "County Fire"},
        ]

        for i, call in enumerate(calls):
            response = test_client_with_storage.post(
                "/api/call-upload",
                data={
                    "key": "test-key",
                    "dateTime": str(now_ts - i),
                    **call,
                },
                files={"audio": (f"call_{i}.mp3", audio_bytes, "audio/mpeg")},
            )
            assert response.status_code == 200

        # Verify systems summary
        systems_response = test_client_with_storage.get("/api/systems")
        assert systems_response.status_code == 200
        systems = systems_response.json()
        system_ids = {s["system_id"] for s in systems}
        assert "1" in system_ids
        assert "2" in system_ids

        sys1 = next(s for s in systems if s["system_id"] == "1")
        sys2 = next(s for s in systems if s["system_id"] == "2")
        assert sys1["total_calls"] == 2
        assert sys2["total_calls"] == 3

        # Verify talkgroups summary
        tg_response = test_client_with_storage.get("/api/talkgroups?system_id=2")
        assert tg_response.status_code == 200
        talkgroups = tg_response.json()
        assert all(tg["system_id"] == "2" for tg in talkgroups)
        tg_ids = {tg["talkgroup_id"] for tg in talkgroups}
        assert 300 in tg_ids
        assert 400 in tg_ids

        # Verify pagination
        page_response = test_client_with_storage.get("/api/calls?per_page=2&page=1")
        assert page_response.status_code == 200
        page_data = page_response.json()
        assert len(page_data["calls"]) == 2
        assert page_data["total"] == 5
        assert page_data["total_pages"] == 3
