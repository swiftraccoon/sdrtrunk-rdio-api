"""Security regressions for bounded upload and filesystem handling."""

import asyncio
import inspect
import os
import stat
import threading
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from src.api.rdioscanner import (
    UploadConcurrencyGate,
    _get_upload_concurrency_gate,
    _safe_text,
    upload_call,
    validate_api_key,
)
from src.config import Config
from src.models.api_models import RdioScannerUpload
from src.utils.file_handler import FileDeletionResult, FileHandler
from src.utils.multipart_parser import MultipartParseError, parse_multipart_form
from src.utils.storage_quota import CapacityUnavailable, UploadCapacityReservation


def _valid_mp3() -> bytes:
    return b"ID3" + b"\x00" * 2048


def _upload(client: TestClient, content: bytes, content_type: str = "audio/mpeg"):
    return client.post(
        "/api/call-upload",
        data={"key": "", "system": "1", "dateTime": str(int(time.time()))},
        files={"audio": ("call.mp3", content, content_type)},
    )


def _sdrtrunk_payload() -> tuple[bytes, str]:
    boundary = b"--sdrtrunk-sdrtrunk-sdrtrunk"
    delimiter = b"--" + boundary
    body = b"".join(
        [
            delimiter,
            b'\r\nContent-Disposition: form-data; name="key"\r\n\r\n\r\n',
            delimiter,
            b'\r\nContent-Disposition: form-data; name="system"\r\n\r\n1\r\n',
            delimiter,
            b'\r\nContent-Disposition: form-data; name="dateTime"\r\n\r\n',
            str(int(time.time())).encode("ascii"),
            b"\r\n",
            delimiter,
            b'\r\nContent-Disposition: form-data; filename="call.mp3"; name="audio"\r\n\r\n',
            _valid_mp3(),
            b"\r\n",
            delimiter,
            b"--\r\n",
        ]
    )
    return body, f"multipart/form-data; boundary={boundary.decode()}"


def _raw_upload_request(app) -> Request:
    body, content_type = _sdrtrunk_payload()
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/call-upload",
            "raw_path": b"/api/call-upload",
            "query_string": b"",
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"user-agent", b"cancellation-test"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "app": app,
        },
        receive,
    )


def _partial_disconnect_request(app) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": b'--bounded\r\nContent-Disposition: form-data; name="key"\r\n',
            "more_body": True,
        }

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/call-upload",
            "raw_path": b"/api/call-upload",
            "query_string": b"",
            "headers": [(b"content-type", b"multipart/form-data; boundary=bounded")],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "app": app,
        },
        receive,
    )


def test_rejects_mp3_extension_with_invalid_signature(
    test_client_with_storage: TestClient,
) -> None:
    response = _upload(test_client_with_storage, b"not an mp3" * 200)
    assert response.status_code == 400
    assert response.json() == {"detail": "File content does not match MP3 audio"}


def test_rejects_mp3_with_unapproved_mime(
    test_client_with_storage: TestClient,
) -> None:
    response = _upload(
        test_client_with_storage, _valid_mp3(), "application/octet-stream"
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid content type for MP3 audio"}


def test_accepts_exact_sdrtrunk_multipart_without_file_mime(
    test_client_with_storage: TestClient,
) -> None:
    body, content_type = _sdrtrunk_payload()
    response = test_client_with_storage.post(
        "/api/call-upload",
        content=body,
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 200
    assert test_client_with_storage.app.state.db_ops.count_pending_file_deletions() == 0


def test_urlencoded_form_enforces_unique_field_count(
    test_client_with_storage: TestClient,
) -> None:
    body = urlencode({f"field{index}": "x" for index in range(33)})
    response = test_client_with_storage.post(
        "/api/call-upload",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Too many form fields"}


def test_cancellation_during_file_storage_compensates_completed_file(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    file_handler = app.state.file_handler
    original_store = file_handler.store_file
    entered = threading.Event()
    release = threading.Event()

    def delayed_store(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release file storage")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(file_handler, "store_file", delayed_store)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        task = asyncio.create_task(endpoint(_raw_upload_request(app)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            release.set()

    asyncio.run(exercise())
    assert not [path for path in file_handler.storage_dir.rglob("*") if path.is_file()]
    assert not [path for path in file_handler.temp_dir.iterdir() if path.is_file()]
    assert app.state.db_ops.get_statistics()["total_calls"] == 0
    assert _get_upload_concurrency_gate(app).active_total == 0
    capacity = app.state.storage_capacity.snapshot
    assert capacity.active_reservations == 0
    assert capacity.stored_bytes == 0


def test_cancellation_during_database_commit_preserves_committed_file_and_row(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    db_ops = app.state.db_ops
    original_save = db_ops.save_radio_call
    entered = threading.Event()
    release = threading.Event()

    def delayed_save(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release database commit")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(db_ops, "save_radio_call", delayed_save)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        task = asyncio.create_task(endpoint(_raw_upload_request(app)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            release.set()

    asyncio.run(exercise())
    calls = db_ops.get_recent_calls(limit=10)
    assert len(calls) == 1
    assert calls[0].audio_file_path is not None
    assert not Path(calls[0].audio_file_path).is_absolute()
    stored_path = app.state.file_handler.storage_dir / calls[0].audio_file_path
    assert stored_path.is_file()
    assert stored_path.read_bytes() == _valid_mp3()
    assert not [
        path for path in app.state.file_handler.temp_dir.iterdir() if path.is_file()
    ]
    assert _get_upload_concurrency_gate(app).active_total == 0
    capacity = app.state.storage_capacity.snapshot
    assert capacity.active_reservations == 0
    assert capacity.stored_bytes == len(_valid_mp3())


def test_ambiguous_database_failure_preserves_committed_file_and_row(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compensation must reconcile a commit that was reported as failure."""
    app = test_client_with_storage.app
    db_ops = app.state.db_ops
    original_save = db_ops.save_radio_call

    def commit_then_fail(*args, **kwargs):
        original_save(*args, **kwargs)
        raise RuntimeError("simulated post-commit failure")

    monkeypatch.setattr(db_ops, "save_radio_call", commit_then_fail)

    response = _upload(test_client_with_storage, _valid_mp3())

    assert response.status_code == 500
    calls = db_ops.get_recent_calls(limit=10)
    assert len(calls) == 1
    assert calls[0].audio_file_path is not None
    assert not Path(calls[0].audio_file_path).is_absolute()
    stored_path = app.state.file_handler.storage_dir / calls[0].audio_file_path
    assert stored_path.is_file()
    assert stored_path.read_bytes() == _valid_mp3()
    assert not [
        path for path in app.state.file_handler.temp_dir.iterdir() if path.is_file()
    ]
    assert _get_upload_concurrency_gate(app).active_total == 0


def test_cancellation_during_final_compensation_is_joined_before_release(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    file_handler = app.state.file_handler
    original_delete = file_handler.delete_file
    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()

    def fail_database_save(*_args, **_kwargs):
        raise RuntimeError("forced database failure")

    def delayed_delete(path: str):
        entered_cleanup.set()
        if not release_cleanup.wait(5):
            raise TimeoutError("test did not release compensation")
        return original_delete(path)

    monkeypatch.setattr(app.state.db_ops, "save_radio_call", fail_database_save)
    monkeypatch.setattr(file_handler, "delete_file", delayed_delete)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        task = asyncio.create_task(endpoint(_raw_upload_request(app)))
        try:
            assert await asyncio.to_thread(entered_cleanup.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert _get_upload_concurrency_gate(app).active_total == 1
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_cleanup.set()

    asyncio.run(exercise())
    assert _get_upload_concurrency_gate(app).active_total == 0
    assert not [path for path in file_handler.storage_dir.rglob("*") if path.is_file()]
    assert not [path for path in file_handler.temp_dir.iterdir() if path.is_file()]
    capacity = app.state.storage_capacity.snapshot
    assert capacity.active_reservations == 0
    assert capacity.stored_bytes == 0


def test_upload_concurrency_gate_enforces_global_and_per_ip_limits() -> None:
    gate = UploadConcurrencyGate(global_limit=2, per_ip_limit=1)
    assert gate.try_acquire("192.0.2.1")
    assert not gate.try_acquire("192.0.2.1")
    assert gate.try_acquire("192.0.2.2")
    assert not gate.try_acquire("192.0.2.3")
    assert gate.active_total == 2
    gate.release("192.0.2.1")
    gate.release("192.0.2.2")
    assert gate.active_total == 0


def test_upload_concurrency_gate_collapses_ipv6_privacy_addresses() -> None:
    gate = UploadConcurrencyGate(global_limit=8, per_ip_limit=2)
    first = "2001:db8:abcd:1234::1"
    second = "2001:db8:abcd:1234:ffff::2"
    third = "2001:db8:abcd:1234:eeee::3"

    assert gate.try_acquire(first)
    assert gate.try_acquire(second)
    assert not gate.try_acquire(third)
    assert gate.active_total == 2
    gate.release(first)
    gate.release(second)
    assert gate.active_total == 0


def test_saturated_gate_rejects_before_multipart_parsing(
    test_client_with_storage: TestClient,
) -> None:
    app = test_client_with_storage.app
    gate = UploadConcurrencyGate(global_limit=1, per_ip_limit=1)
    app.state._rdio_upload_concurrency_gate = gate
    assert gate.try_acquire("127.0.0.1")

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        with pytest.raises(HTTPException) as caught:
            await endpoint(_raw_upload_request(app))
        assert caught.value.status_code == 503
        assert caught.value.detail == "Too many concurrent uploads"

    try:
        asyncio.run(exercise())
        assert gate.active_total == 1
    finally:
        gate.release("127.0.0.1")


def test_capacity_rejection_happens_before_multipart_parsing(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    parse_called = False

    def reject_capacity() -> None:
        raise CapacityUnavailable("simulated full spool filesystem")

    async def unexpected_parse(*_args, **_kwargs):
        nonlocal parse_called
        parse_called = True
        raise AssertionError("multipart parser must not run")

    monkeypatch.setattr(app.state.storage_capacity, "reserve_upload", reject_capacity)
    monkeypatch.setattr("src.api.rdioscanner._parse_upload_form", unexpected_parse)
    response = _upload(test_client_with_storage, _valid_mp3())

    assert response.status_code == 507
    assert response.json() == {"detail": "Insufficient storage capacity"}
    assert not parse_called
    assert _get_upload_concurrency_gate(app).active_total == 0
    assert app.state.storage_capacity.snapshot.active_reservations == 0


def test_cancellation_during_capacity_reservation_releases_completed_lease(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    capacity = app.state.storage_capacity
    original_reserve = capacity.reserve_upload
    entered = threading.Event()
    release = threading.Event()

    def delayed_reserve() -> UploadCapacityReservation:
        reservation = original_reserve()
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release capacity reservation")
        return reservation

    monkeypatch.setattr(capacity, "reserve_upload", delayed_reserve)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        task = asyncio.create_task(endpoint(_raw_upload_request(app)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert capacity.snapshot.active_reservations == 1
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(exercise())
    assert capacity.snapshot.active_reservations == 0
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    assert _get_upload_concurrency_gate(app).active_total == 0


def test_cancellation_during_persistent_claim_releases_all_capacity(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    capacity = app.state.storage_capacity
    original_claim = UploadCapacityReservation.claim_persistent
    entered = threading.Event()
    release = threading.Event()

    def delayed_claim(reservation: UploadCapacityReservation) -> None:
        original_claim(reservation)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release persistent claim")

    monkeypatch.setattr(UploadCapacityReservation, "claim_persistent", delayed_claim)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        task = asyncio.create_task(endpoint(_raw_upload_request(app)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            snapshot = capacity.snapshot
            assert snapshot.active_archive_reservations == 1
            assert snapshot.persistent_reserved_bytes == capacity.max_file_bytes
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(exercise())
    snapshot = capacity.snapshot
    assert snapshot.active_reservations == 0
    assert snapshot.active_archive_reservations == 0
    assert snapshot.filesystem_reserved_bytes == 0
    assert snapshot.persistent_reserved_bytes == 0
    assert _get_upload_concurrency_gate(app).active_total == 0


def test_total_multipart_parse_deadline_releases_gate(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    app.state.rdio_upload_parse_timeout_seconds = 0.01

    async def stalled_parse(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("src.api.rdioscanner._parse_upload_form", stalled_parse)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        with pytest.raises(HTTPException) as caught:
            await endpoint(_raw_upload_request(app))
        assert caught.value.status_code == 408
        assert caught.value.detail == "Upload form parsing timed out"

    asyncio.run(exercise())
    assert _get_upload_concurrency_gate(app).active_total == 0
    assert app.state.storage_capacity.snapshot.active_reservations == 0


def test_database_failure_compensates_stored_and_temporary_files(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_save(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(
        test_client_with_storage.app.state.db_ops, "save_radio_call", fail_save
    )
    response = _upload(test_client_with_storage, _valid_mp3())

    assert response.status_code == 500
    file_handler = test_client_with_storage.app.state.file_handler
    assert not [path for path in file_handler.storage_dir.rglob("*") if path.is_file()]
    assert not [path for path in file_handler.temp_dir.iterdir() if path.is_file()]
    capacity = test_client_with_storage.app.state.storage_capacity.snapshot
    assert capacity.active_reservations == 0
    assert capacity.stored_bytes == 0


def test_pre_auth_disconnect_does_not_create_durable_audit_row(
    test_client_with_storage: TestClient,
) -> None:
    app = test_client_with_storage.app
    before = app.state.db_manager.get_stats()["tables"]["upload_logs"]

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        with pytest.raises(HTTPException) as caught:
            await endpoint(_partial_disconnect_request(app))
        assert caught.value.status_code == 500

    asyncio.run(exercise())

    after = app.state.db_manager.get_stats()["tables"]["upload_logs"]
    assert after == before
    assert _get_upload_concurrency_gate(app).active_total == 0


def test_pre_auth_oversize_does_not_create_durable_audit_row(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app
    before = app.state.db_manager.get_stats()["tables"]["upload_logs"]

    async def reject_oversized_form(*_args, **_kwargs):
        from src.exceptions import FileSizeError

        raise FileSizeError("simulated oversized pre-auth part")

    monkeypatch.setattr("src.api.rdioscanner._parse_upload_form", reject_oversized_form)

    async def exercise() -> None:
        endpoint = inspect.unwrap(upload_call)
        with pytest.raises(HTTPException) as caught:
            await endpoint(_raw_upload_request(app))
        assert caught.value.status_code == 413

    asyncio.run(exercise())

    after = app.state.db_manager.get_stats()["tables"]["upload_logs"]
    assert after == before
    assert _get_upload_concurrency_gate(app).active_total == 0


def test_persistent_quota_rejection_after_validation_releases_transient_capacity(
    test_client_with_storage: TestClient,
) -> None:
    app = test_client_with_storage.app
    capacity = app.state.storage_capacity
    capacity.max_storage_bytes = capacity.max_file_bytes - 1

    response = _upload(test_client_with_storage, _valid_mp3())

    assert response.status_code == 507
    snapshot = capacity.snapshot
    assert snapshot.active_reservations == 0
    assert snapshot.persistent_reserved_bytes == 0
    assert snapshot.filesystem_reserved_bytes == 0
    assert not [
        path for path in app.state.file_handler.storage_dir.rglob("*") if path.is_file()
    ]
    assert not [
        path for path in app.state.file_handler.temp_dir.iterdir() if path.is_file()
    ]


def test_failed_compensation_remains_charged_until_reconciliation(
    test_client_with_storage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = test_client_with_storage.app

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("simulated database failure")

    def defer_delete(_path: str) -> FileDeletionResult:
        return FileDeletionResult("retry", error="simulated unlink failure")

    monkeypatch.setattr(app.state.db_ops, "save_radio_call", fail_save)
    monkeypatch.setattr(app.state.file_handler, "delete_file", defer_delete)
    response = _upload(test_client_with_storage, _valid_mp3())

    assert response.status_code == 500
    stored_files = [
        path for path in app.state.file_handler.storage_dir.rglob("*") if path.is_file()
    ]
    assert len(stored_files) == 1
    snapshot = app.state.storage_capacity.snapshot
    assert snapshot.active_reservations == 0
    assert snapshot.stored_bytes == stored_files[0].stat().st_size
    assert snapshot.stored_bytes == len(_valid_mp3())


def test_storage_permissions_unique_names_and_filename_byte_limit(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    temporary = tmp_path / "temporary"
    storage.mkdir(mode=0o755)
    temporary.mkdir(mode=0o755)
    storage.chmod(0o755)
    temporary.chmod(0o755)

    handler = FileHandler(str(storage), str(temporary), organize_by_date=True)
    assert stat.S_IMODE(storage.stat().st_mode) == 0o700
    assert stat.S_IMODE(temporary.stat().st_mode) == 0o700

    stored_paths = []
    for _ in range(2):
        source = handler.save_temp_file("call.mp3", _valid_mp3())
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        stored_paths.append(
            handler.store_file(
                source,
                system_id="1",
                timestamp=datetime.now(tz=UTC),
                talkgroup_id=int("9" * 500),
                system_label="x" * 1000,
            )
        )

    assert stored_paths[0] != stored_paths[1]
    for stored in stored_paths:
        assert len(stored.name.encode("utf-8")) <= os.pathconf(
            stored.parent, "PC_NAME_MAX"
        )
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        for directory in stored.parent.relative_to(storage).parents:
            if str(directory) != ".":
                assert stat.S_IMODE((storage / directory).stat().st_mode) == 0o700
        assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700


def test_stream_copy_uses_bounded_reads_and_mode_0600(tmp_path: Path) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temporary"),
        min_file_size_kb=1,
    )
    upload = UploadFile(
        BytesIO(_valid_mp3()),
        size=len(_valid_mp3()),
        filename="call.mp3",
        headers=Headers({"content-type": "audio/mpeg"}),
    )
    saved = asyncio.run(handler.save_upload_file("call.mp3", upload))
    assert saved.read_bytes() == _valid_mp3()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_upload_copy_fsync_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temporary"),
        min_file_size_kb=1,
    )
    upload = UploadFile(
        BytesIO(_valid_mp3()),
        size=len(_valid_mp3()),
        filename="call.mp3",
        headers=Headers({"content-type": "audio/mpeg"}),
    )
    actual_fsync = os.fsync
    fsync_started = threading.Event()
    release_fsync = threading.Event()
    blocked_once = False

    def blocking_fsync(descriptor: int) -> None:
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            fsync_started.set()
            if not release_fsync.wait(5):
                raise TimeoutError("test did not release fsync")
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", blocking_fsync)

    async def exercise() -> Path:
        copy_task = asyncio.create_task(handler.save_upload_file("call.mp3", upload))
        try:
            assert await asyncio.to_thread(fsync_started.wait, 5)
            ticker_ran = False

            async def ticker() -> None:
                nonlocal ticker_ran
                await asyncio.sleep(0)
                ticker_ran = True

            await asyncio.wait_for(ticker(), timeout=0.2)
            assert ticker_ran
            release_fsync.set()
            return await copy_task
        finally:
            release_fsync.set()

    saved = asyncio.run(exercise())
    assert saved.read_bytes() == _valid_mp3()


def test_validation_rejections_still_receive_security_headers(
    test_client_with_storage: TestClient,
) -> None:
    response = test_client_with_storage.post(
        "/api/call-upload",
        content=b"unsupported",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_cancelled_stream_copy_removes_partial_temp_file(tmp_path: Path) -> None:
    class CancelledUpload:
        async def seek(self, offset: int) -> None:
            return None

        async def read(self, size: int) -> bytes:
            raise asyncio.CancelledError

    handler = FileHandler(str(tmp_path / "storage"), str(tmp_path / "temporary"))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handler.save_upload_file("call.mp3", CancelledUpload()))
    assert list(handler.temp_dir.iterdir()) == []


def test_oversized_field_is_not_reflected(test_client: TestClient) -> None:
    marker = "do-not-reflect-this-value"
    response = test_client.post(
        "/api/call-upload",
        data={
            "key": "",
            "system": "1",
            "dateTime": str(int(time.time())),
            "systemLabel": marker * 20,
        },
    )
    assert response.status_code == 400
    assert marker not in response.text
    assert len(response.content) < 256


def test_controls_and_bidi_overrides_are_neutralized(tmp_path: Path) -> None:
    assert _safe_text("one\r\ntwo\u202ethree", 100) == "one__two_three"
    upload = RdioScannerUpload(
        key="",
        system="1",
        dateTime=int(time.time()),
        systemLabel="one\r\ntwo\u202ethree",
    )
    assert upload.systemLabel == "onetwothree"
    handler = FileHandler(str(tmp_path / "storage"), str(tmp_path / "temp"))
    assert "\u202e" not in handler.normalize_filename("call\u202e.mp3")


def test_empty_key_list_requires_explicit_upload_opt_in() -> None:
    config = Config()
    assert validate_api_key(config, "", "1", "127.0.0.1") == (False, None)
    config.security.allow_unauthenticated_uploads = True
    assert validate_api_key(config, "", "1", "127.0.0.1") == (True, None)


def test_timestamp_allows_small_skew_but_rejects_future_poisoning() -> None:
    now = int(time.time())
    RdioScannerUpload(key="", system="1", dateTime=now + 240)
    with pytest.raises(ValueError):
        RdioScannerUpload(key="", system="1", dateTime=now + 3600)


def test_upload_model_repr_never_contains_api_key() -> None:
    sentinel = "synthetic-upload-secret"
    upload = RdioScannerUpload(
        key=sentinel,
        system="1",
        dateTime=int(datetime.now(UTC).timestamp()),
    )

    assert sentinel not in repr(upload)
    assert "key" not in upload.model_dump()


def test_legacy_parser_fails_closed_on_field_limit() -> None:
    boundary = "bounded"
    body = (
        b"--bounded\r\n"
        b'Content-Disposition: form-data; name="field"\r\n\r\n'
        b"abcdef\r\n"
        b"--bounded--\r\n"
    )
    with pytest.raises(MultipartParseError, match="too large"):
        parse_multipart_form(body, boundary, max_field_bytes=5)


def test_legacy_parser_preserves_file_content_ending_in_dashes() -> None:
    boundary = "bounded"
    body = (
        b"--bounded\r\n"
        b'Content-Disposition: form-data; name="audio"; filename="call.mp3"\r\n'
        b"Content-Type: audio/mpeg\r\n\r\n"
        b"ID3content--\r\n"
        b"--bounded--\r\n"
    )
    _, files = parse_multipart_form(body, boundary)
    assert files["audio"]["content"] == b"ID3content--"
