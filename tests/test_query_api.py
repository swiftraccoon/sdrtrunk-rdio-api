"""Tests for query API endpoints."""

import asyncio
import inspect
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from src.api.query import (
    MAX_CONCURRENT_AUDIO_STREAMS_PER_PRINCIPAL,
    _AudioStreamGate,
    _ClosingStreamingResponse,
    get_call_audio,
)
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

    def test_query_calls_applies_zero_valued_radio_id_filters(
        self, test_client: TestClient, db_ops: DatabaseOperations
    ) -> None:
        now = int(datetime.now(UTC).timestamp())
        for radio_id in (0, 1):
            db_ops.save_call(
                RdioScannerUpload(
                    key="test",
                    system="1",
                    dateTime=now,
                    talkgroup=radio_id,
                    source=radio_id,
                ),
                client_ip="127.0.0.1",
                stored_path=None,
                api_key_id="test-key",
            )

        response = test_client.get("/api/calls?talkgroup_id=0&source_id=0")

        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["calls"][0]["talkgroup_id"] == 0
        assert response.json()["calls"][0]["source_id"] == 0

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

        # Pathological offsets are rejected before they can force an expensive
        # database scan.
        response = test_client.get("/api/calls?page=999999")
        assert response.status_code == 422

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

    def test_get_call_audio_streams_the_pinned_inode_after_path_replacement(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        test_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pathname swap after verification must not change response bytes."""
        storage_dir = Path(test_config.file_handling.storage.directory)
        storage_dir.mkdir(parents=True, exist_ok=True)
        audio_file = storage_dir / "pinned_audio.mp3"
        original_bytes = b"\xff\xfb" + b"original" * 128
        audio_file.write_bytes(original_bytes)

        call_id = db_ops.save_call(
            RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now(UTC).timestamp()),
                talkgroup=1234,
            ),
            client_ip="127.0.0.1",
            stored_path=str(audio_file),
            api_key_id="test-key",
        )

        file_handler = test_client.app.state.file_handler
        original_open = file_handler.open_stored_file

        @contextmanager
        def replace_after_open(path: str) -> Iterator[BinaryIO]:
            with original_open(path) as stream:
                audio_file.rename(storage_dir / "original_inode.mp3")
                audio_file.write_bytes(b"attacker-controlled replacement")
                yield stream

        monkeypatch.setattr(file_handler, "open_stored_file", replace_after_open)

        response = test_client.get(f"/api/calls/{call_id}/audio")

        assert response.status_code == 200
        assert response.content == original_bytes

    @pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
    def test_get_call_audio_rejects_symlink_directory_component(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        test_config,
    ) -> None:
        storage_dir = Path(test_config.file_handling.storage.directory)
        real_directory = storage_dir / "real_audio"
        real_directory.mkdir(parents=True)
        audio_file = real_directory / "call.mp3"
        audio_file.write_bytes(b"\xff\xfb" + b"not reachable through a link")
        linked_directory = storage_dir / "linked_audio"
        linked_directory.symlink_to(real_directory, target_is_directory=True)

        call_id = db_ops.save_call(
            RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now(UTC).timestamp()),
                talkgroup=1234,
            ),
            client_ip="127.0.0.1",
            stored_path=str(linked_directory / audio_file.name),
            api_key_id="test-key",
        )

        response = test_client.get(f"/api/calls/{call_id}/audio")

        assert response.status_code == 404

    def test_audio_stream_closes_descriptor_on_first_send_failure(
        self, temp_dir: Path
    ) -> None:
        """A disconnected ASGI send must close the pinned file immediately."""
        audio_file = temp_dir / "disconnect.mp3"
        audio_file.write_bytes(b"\xff\xfb" + b"audio" * 32)
        stream = audio_file.open("rb")

        def chunks() -> Iterator[bytes]:
            yield stream.read()

        response = _ClosingStreamingResponse(
            chunks(),
            close_callback=stream.close,
            media_type="audio/mpeg",
        )

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body":
                raise OSError("simulated disconnected socket")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/audio",
            "raw_path": b"/audio",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "root_path": "",
        }

        with pytest.raises(ClientDisconnect):
            asyncio.run(response(scope, receive, send))  # type: ignore[arg-type]

        assert stream.closed

    def test_audio_stream_timeout_closes_resource(self) -> None:
        """A started but stalled response must release its resource at deadline."""
        closed = False

        def close_resource() -> None:
            nonlocal closed
            closed = True

        response = _ClosingStreamingResponse(
            iter([b"audio"]),
            close_callback=close_resource,
            media_type="audio/mpeg",
            response_timeout_seconds=0.01,
        )
        never_send = asyncio.Event()

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body":
                await never_send.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/audio",
            "raw_path": b"/audio",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "root_path": "",
        }

        asyncio.run(response(scope, receive, send))  # type: ignore[arg-type]

        assert closed

    def test_stalled_file_read_finalization_does_not_block_event_loop(self) -> None:
        """A stuck sync read keeps its slot but cannot freeze unrelated tasks."""
        read_lock = threading.Lock()
        read_started = threading.Event()
        release_read = threading.Event()
        closed = threading.Event()

        def blocking_chunks() -> Iterator[bytes]:
            with read_lock:
                read_started.set()
                release_read.wait(timeout=2.0)
            yield b"audio"

        def close_resource() -> None:
            with read_lock:
                closed.set()

        response = _ClosingStreamingResponse(
            blocking_chunks(),
            close_callback=close_resource,
            media_type="audio/mpeg",
            response_timeout_seconds=0.01,
            close_grace_seconds=0.01,
        )

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/audio",
            "raw_path": b"/audio",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "root_path": "",
        }

        async def exercise() -> None:
            ticker_ran = False

            async def ticker() -> None:
                nonlocal ticker_ran
                await asyncio.sleep(0.02)
                ticker_ran = True

            ticker_task = asyncio.create_task(ticker())
            await asyncio.wait_for(
                response(scope, receive, send),  # type: ignore[arg-type]
                timeout=0.2,
            )
            await ticker_task
            assert read_started.is_set()
            assert ticker_ran
            assert not closed.is_set()

            release_read.set()
            for _ in range(100):
                if closed.is_set():
                    break
                await asyncio.sleep(0.005)
            assert closed.is_set()

        asyncio.run(exercise())

    def test_audio_stream_gate_releases_after_disconnect(
        self,
        test_client: TestClient,
        db_ops: DatabaseOperations,
        test_config,
    ) -> None:
        """Held responses hit the principal cap and a disconnect frees one slot."""
        storage_dir = Path(test_config.file_handling.storage.directory)
        storage_dir.mkdir(parents=True, exist_ok=True)
        audio_file = storage_dir / "held_audio.mp3"
        audio_file.write_bytes(b"\xff\xfb" + b"audio" * 64)
        call_id = db_ops.save_call(
            RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now(UTC).timestamp()),
                talkgroup=1234,
            ),
            client_ip="127.0.0.1",
            stored_path=str(audio_file),
            api_key_id="test-key",
        )

        app = test_client.app
        app.state.audio_stream_gate = _AudioStreamGate(
            global_limit=32,
            per_principal_limit=MAX_CONCURRENT_AUDIO_STREAMS_PER_PRINCIPAL,
        )
        endpoint = inspect.unwrap(get_call_audio)

        def request() -> Request:
            return Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/calls/{call_id}/audio",
                    "headers": [],
                    "client": ("2001:db8:abcd:1::1", 12345),
                    "app": app,
                }
            )

        responses = [
            endpoint(request(), call_id, None)
            for _ in range(MAX_CONCURRENT_AUDIO_STREAMS_PER_PRINCIPAL)
        ]
        try:
            with pytest.raises(HTTPException) as capacity_error:
                endpoint(request(), call_id, None)
            assert capacity_error.value.status_code == 503
            assert capacity_error.value.headers == {"Retry-After": "1"}

            held = responses.pop()

            async def receive() -> dict[str, str]:
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.body":
                    raise OSError("simulated disconnected socket")

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/api/calls/{call_id}/audio",
                "raw_path": f"/api/calls/{call_id}/audio".encode(),
                "query_string": b"",
                "headers": [],
                "client": ("2001:db8:abcd:1::1", 12345),
                "server": ("127.0.0.1", 80),
                "root_path": "",
            }
            with pytest.raises(ClientDisconnect):
                asyncio.run(held(scope, receive, send))  # type: ignore[arg-type]

            replacement = endpoint(request(), call_id, None)
            responses.append(replacement)
        finally:
            for response in responses:
                response._close_callback()

    def test_audio_stream_gate_enforces_global_cap(self) -> None:
        gate = _AudioStreamGate(global_limit=2, per_principal_limit=2)

        assert gate.try_acquire("principal-a")
        assert gate.try_acquire("principal-b")
        assert not gate.try_acquire("principal-c")
        gate.release("principal-a")
        assert gate.try_acquire("principal-c")
        gate.release("principal-b")
        gate.release("principal-c")

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
