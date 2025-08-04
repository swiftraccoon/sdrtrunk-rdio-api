"""Tests for API endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient


class TestHealthEndpoints:
    """Tests for health check and metrics endpoints."""

    def test_health_check(self, test_client: TestClient) -> None:
        """Test health check endpoint."""
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "unhealthy"]
        assert "timestamp" in data
        assert data["version"] == "1.0.0"
        assert "database" in data

    def test_metrics(self, test_client: TestClient) -> None:
        """Test metrics endpoint."""
        response = test_client.get("/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "total_calls" in data
        assert "calls_today" in data
        assert "calls_last_hour" in data
        assert "systems" in data
        assert "talkgroups" in data
        assert "upload_sources" in data
        assert "storage_used_mb" in data
        assert "audio_files_count" in data


class TestRdioScannerAPI:
    """Tests for RdioScanner API endpoints."""

    def test_test_mode(self, test_client: TestClient) -> None:
        """Test the test mode response."""
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "test-key",
                "system": "123",
                "test": "1",
            },
        )
        assert response.status_code == 200
        # Test mode returns plain text
        assert response.text == "incomplete call data: no talkgroup"

    def test_upload_call_without_audio_log_only_mode(
        self, test_client: TestClient
    ) -> None:
        """Test uploading call metadata without audio file in log_only mode."""
        # The fixture should set mode to log_only
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "test-key",
                "system": "123",
                "dateTime": "1234567890",
                "talkgroup": "100",
                "source": "200",
                "frequency": "854037500",
                "systemLabel": "Test System",
                "talkgroupLabel": "Test TG",
            },
        )
        assert response.status_code == 200
        assert response.text == "Call imported successfully."

    def test_upload_call_with_audio(
        self, test_client: TestClient, temp_audio_file: Path
    ) -> None:
        """Test uploading call with audio file."""
        with open(temp_audio_file, "rb") as f:
            response = test_client.post(
                "/api/call-upload",
                data={
                    "key": "test-key",
                    "system": "123",
                    "dateTime": "1234567890",
                    "talkgroup": "100",
                    "source": "200",
                    "frequency": "854037500",
                },
                files={"audio": ("test.mp3", f, "audio/mpeg")},
            )

        assert response.status_code == 200
        assert response.text == "Call imported successfully."

    def test_upload_missing_required_fields(self, test_client: TestClient) -> None:
        """Test uploading with missing required fields."""
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "test-key",
                # Missing system and dateTime
            },
        )
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Missing required fields" in data["detail"]

    def test_upload_without_api_key(self, test_client: TestClient) -> None:
        """Test uploading without API key when keys are configured."""
        # Note: This test assumes no API keys are configured in test fixture
        response = test_client.post(
            "/api/call-upload",
            data={
                "system": "123",
                "dateTime": "1234567890",
                # Missing key field
            },
        )
        # With no API keys configured, it should still work
        assert response.status_code == 200

    def test_upload_with_json_accept_header(self, test_client: TestClient) -> None:
        """Test that JSON response is returned when Accept header is set."""
        response = test_client.post(
            "/api/call-upload",
            headers={"Accept": "application/json"},
            data={
                "key": "test-key",
                "system": "123",
                "dateTime": "1234567890",
                "talkgroup": "100",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "callId" in data
        assert "message" in data

    def test_upload_invalid_audio_format(
        self, test_client_with_storage: TestClient, temp_dir: Path
    ) -> None:
        """Test uploading with invalid audio format."""
        # Create a test file with wrong extension
        audio_file = temp_dir / "test.wav"
        audio_file.write_bytes(b"fake audio data")

        with open(audio_file, "rb") as f:
            response = test_client_with_storage.post(
                "/api/call-upload",
                data={
                    "key": "test-key",
                    "system": "123",
                    "dateTime": "1234567890",
                },
                files={"audio": ("test.wav", f, "audio/wav")},
            )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "not accepted" in data["detail"]

    def test_upload_file_too_large(
        self, test_client_with_storage: TestClient, temp_dir: Path
    ) -> None:
        """Test uploading file that's too large."""
        # Create a large test file (101 MB, over the 100MB limit)
        audio_file = temp_dir / "large.mp3"
        audio_file.write_bytes(b"x" * (101 * 1024 * 1024))

        with open(audio_file, "rb") as f:
            response = test_client_with_storage.post(
                "/api/call-upload",
                data={
                    "key": "test-key",
                    "system": "123",
                    "dateTime": "1234567890",
                },
                files={"audio": ("large.mp3", f, "audio/mpeg")},
            )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "too large" in data["detail"]

    def test_upload_file_too_small(
        self, test_client_with_storage: TestClient, temp_dir: Path
    ) -> None:
        """Test uploading file that's too small."""
        # Create a tiny test file (less than 1KB)
        audio_file = temp_dir / "tiny.mp3"
        audio_file.write_bytes(b"x")

        with open(audio_file, "rb") as f:
            response = test_client_with_storage.post(
                "/api/call-upload",
                data={
                    "key": "test-key",
                    "system": "123",
                    "dateTime": "1234567890",
                },
                files={"audio": ("tiny.mp3", f, "audio/mpeg")},
            )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "too small" in data["detail"]
