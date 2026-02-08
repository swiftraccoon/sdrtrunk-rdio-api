"""Tests for query API endpoints."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.operations import DatabaseOperations
from src.models.api_models import RdioScannerUpload


class TestQueryEndpoints:
    """Test query API endpoints."""

    def setup_test_data(self, db_ops: DatabaseOperations) -> None:
        """Setup test data for query tests."""
        # Add some test calls
        for i in range(20):
            upload_data = RdioScannerUpload(
                key="test",
                system=str((i % 3) + 1),
                dateTime=int((datetime.now(UTC) - timedelta(hours=i)).timestamp()),
                talkgroup=(1000 + i) if i % 2 == 0 else None,
                frequency=853237500 + (i * 1000) if i % 3 == 0 else None,
                source=5000 + i if i % 4 == 0 else None,
                systemLabel=f"System {(i % 3) + 1}",
                talkgroupLabel=f"TG {1000 + i}" if i % 2 == 0 else None,
            )
            db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path=f"/test/audio_{i}.mp3",
                api_key_id="test-key",
            )

    def test_query_calls_basic(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test basic call querying."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/calls")
        assert response.status_code == 200

        data = response.json()
        assert "calls" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data
        assert "total_pages" in data
        assert data["total"] == 20
        assert len(data["calls"]) == 20  # Default per_page

    def test_query_calls_with_pagination(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test call querying with pagination."""
        self.setup_test_data(db_ops)

        # Get first page
        response = test_client.get("/api/calls?page=1&per_page=5")
        assert response.status_code == 200

        data = response.json()
        assert len(data["calls"]) == 5
        assert data["page"] == 1
        assert data["per_page"] == 5
        assert data["total_pages"] == 4

        # Get second page
        response = test_client.get("/api/calls?page=2&per_page=5")
        assert response.status_code == 200

        data = response.json()
        assert len(data["calls"]) == 5
        assert data["page"] == 2

    def test_query_calls_with_system_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test call querying with system filter."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/calls?system_id=1")
        assert response.status_code == 200

        data = response.json()
        # System 1 should have roughly 1/3 of the calls
        assert data["total"] > 0
        for call in data["calls"]:
            assert call["system_id"] == "1"

    def test_query_calls_with_talkgroup_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test call querying with talkgroup filter."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/calls?talkgroup_id=1000")
        assert response.status_code == 200

        data = response.json()
        assert data["total"] > 0
        for call in data["calls"]:
            assert call["talkgroup_id"] == 1000

    def test_query_calls_with_date_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test call querying with date filter."""
        self.setup_test_data(db_ops)

        # Get calls from last 5 hours
        response = test_client.get("/api/calls?hours_ago=5")
        assert response.status_code == 200

        data = response.json()
        assert data["total"] > 0
        assert data["total"] < 20  # Should be less than all calls

    def test_query_calls_with_sorting(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test call querying with different sort options."""
        self.setup_test_data(db_ops)

        # Sort by system_id ascending
        response = test_client.get(
            "/api/calls?sort_by=system_id&sort_order=asc&per_page=5"
        )
        assert response.status_code == 200

        data = response.json()
        calls = data["calls"]
        # Check that system_ids are sorted
        for i in range(len(calls) - 1):
            assert calls[i]["system_id"] <= calls[i + 1]["system_id"]

    def test_query_calls_invalid_parameters(self, test_client: TestClient) -> None:
        """Test call querying with invalid parameters."""
        # Invalid page number
        response = test_client.get("/api/calls?page=0")
        assert response.status_code == 422

        # Invalid per_page
        response = test_client.get("/api/calls?per_page=1000")
        assert response.status_code == 422

        # Invalid sort field
        response = test_client.get("/api/calls?sort_by=invalid_field")
        assert response.status_code == 422

    def test_get_call_by_id(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test retrieving specific call by ID."""
        # Add a test call
        upload_data = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=1234,
            systemLabel="Test System",
            talkgroupLabel="Test TG",
        )
        call_id = db_ops.save_call(
            upload_data,
            client_ip="127.0.0.1",
            stored_path="/test/audio.mp3",
            api_key_id="test-key",
        )

        # Get the call
        response = test_client.get(f"/api/calls/{call_id}")
        assert response.status_code == 200

        data = response.json()
        assert data["id"] == call_id
        assert data["system_id"] == "1"
        assert data["talkgroup_id"] == 1234

    def test_get_call_by_id_not_found(self, test_client: TestClient) -> None:
        """Test retrieving non-existent call."""
        response = test_client.get("/api/calls/99999")
        assert response.status_code == 404

        data = response.json()
        assert "detail" in data
        assert "not found" in data["detail"].lower()

    def test_list_systems(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test listing systems with summary."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/systems")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 3  # We have 3 systems in test data

        for system in data:
            assert "system_id" in system
            assert "system_label" in system
            assert "total_calls" in system
            assert "first_seen" in system
            assert "last_seen" in system
            assert "top_talkgroups" in system

    def test_list_talkgroups(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test listing talkgroups with summary."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/talkgroups")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

        for tg in data:
            assert "talkgroup_id" in tg
            assert "system_id" in tg
            assert "total_calls" in tg
            assert "last_heard" in tg

    def test_list_talkgroups_with_system_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test listing talkgroups filtered by system."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/talkgroups?system_id=1")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)

        for tg in data:
            assert tg["system_id"] == "1"

    def test_list_talkgroups_with_min_calls_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test listing talkgroups with minimum calls filter."""
        self.setup_test_data(db_ops)

        # Add a talkgroup with multiple calls
        for i in range(5):
            upload_data = RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now(UTC).timestamp()),
                talkgroup=9999,
            )
            db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path=f"/test/audio_99_{i}.mp3",
                api_key_id="test-key",
            )

        response = test_client.get("/api/talkgroups?min_calls=3")
        assert response.status_code == 200

        data = response.json()
        # Only talkgroups with 3+ calls should be returned
        for tg in data:
            assert tg["total_calls"] >= 3

    def test_query_endpoint_error_handling(
        self, test_client: TestClient, monkeypatch
    ) -> None:
        """Test error handling in query endpoints."""
        # This would require mocking database errors
        # For now, we'll just test that the endpoints handle basic errors

        # Test with extremely large page number
        response = test_client.get("/api/calls?page=999999")
        assert response.status_code == 200
        data = response.json()
        assert len(data["calls"]) == 0  # No results for that page

    def test_query_with_date_filters(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test query with date range filters."""
        self.setup_test_data(db_ops)

        # Test with date_from - use format without timezone for compatibility
        date_from = (datetime.now(UTC) - timedelta(hours=5)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        response = test_client.get(f"/api/calls?date_from={date_from}")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] <= 20

        # Test with date_to - use format without timezone for compatibility
        date_to = (datetime.now(UTC) - timedelta(hours=10)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        response = test_client.get(f"/api/calls?date_to={date_to}")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 10

    def test_query_with_frequency_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test query with frequency filter."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/calls?frequency=853237500")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0

    def test_query_with_source_filter(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        """Test query with source radio ID filter."""
        self.setup_test_data(db_ops)

        response = test_client.get("/api/calls?source_id=5000")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        # Verify all returned calls have the requested source
        for call in data["calls"]:
            assert call["source_id"] == 5000

    def test_get_call_audio(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        test_config,
    ) -> None:
        """Test retrieving audio file for a call."""
        # Create audio file in the configured storage directory
        storage_dir = Path(test_config.file_handling.storage.directory)
        storage_dir.mkdir(parents=True, exist_ok=True)
        audio_file = storage_dir / "test_audio.mp3"
        audio_file.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 1024)

        upload_data = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=1234,
        )
        call_id = db_ops.save_call(
            upload_data,
            client_ip="127.0.0.1",
            stored_path=str(audio_file),
            api_key_id="test-key",
        )

        response = test_client.get(f"/api/calls/{call_id}/audio")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert len(response.content) == 1028  # 4 header + 1024 data

    def test_get_call_audio_not_found(self, test_client: TestClient) -> None:
        """Test retrieving audio for a non-existent call."""
        response = test_client.get("/api/calls/99999/audio")
        assert response.status_code == 404

    def test_get_call_audio_no_file(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
    ) -> None:
        """Test retrieving audio when no audio file exists for the call."""
        upload_data = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=1234,
        )
        call_id = db_ops.save_call(
            upload_data,
            client_ip="127.0.0.1",
            stored_path=None,
            api_key_id="test-key",
        )

        response = test_client.get(f"/api/calls/{call_id}/audio")
        assert response.status_code == 404

    def test_get_call_audio_path_traversal(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        temp_dir,
    ) -> None:
        """Test that path traversal attempts are blocked."""
        # Create a file outside the storage directory
        outside_file = temp_dir / "secret.txt"
        outside_file.write_text("sensitive data")

        upload_data = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=1234,
        )
        call_id = db_ops.save_call(
            upload_data,
            client_ip="127.0.0.1",
            stored_path=str(outside_file),
            api_key_id="test-key",
        )

        response = test_client.get(f"/api/calls/{call_id}/audio")
        assert response.status_code == 404

    def test_get_call_audio_missing_file_on_disk(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        test_config,
    ) -> None:
        """Test 404 when DB has a path but the file was deleted from disk."""
        storage_dir = Path(test_config.file_handling.storage.directory)
        storage_dir.mkdir(parents=True, exist_ok=True)
        ghost_path = storage_dir / "deleted_audio.mp3"

        upload_data = RdioScannerUpload(
            key="test",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=1234,
        )
        call_id = db_ops.save_call(
            upload_data,
            client_ip="127.0.0.1",
            stored_path=str(ghost_path),
            api_key_id="test-key",
        )

        response = test_client.get(f"/api/calls/{call_id}/audio")
        assert response.status_code == 404
