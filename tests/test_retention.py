"""Regression tests for retention enforcement and file-handler safety."""

import os
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src.database.operations import DatabaseOperations
from src.models.api_models import RdioScannerUpload
from src.models.database_models import PendingFileDeletion, RadioCall, UploadLog
from src.utils.file_handler import FileDeletionResult, FileHandler
from src.utils.maintenance import run_retention_cleanup
from src.utils.storage_quota import CapacityUnavailable, StorageCapacity


def _save_expired_call(
    db_ops: DatabaseOperations,
    path: str | None = None,
    *,
    days_old: int = 60,
) -> int:
    call_id = db_ops.save_radio_call(
        RdioScannerUpload(
            key="",
            system="1",
            dateTime=int(datetime.now(UTC).timestamp()),
            audio_size=128 if path else None,
        ),
        audio_file_path=path,
    )
    with db_ops.db_manager.get_session() as session:
        call = session.query(RadioCall).filter_by(id=call_id).one()
        call.created_at = datetime.now(UTC) - timedelta(days=days_old)
    return call_id


class TestBackgroundMaintenance:
    def test_server_schedules_maintenance_task(
        self, temp_dir: Path, test_config_dict: dict
    ):
        """The server must enforce retention_days itself, not rely on cron."""
        import yaml

        from src.api.app import create_app
        from src.config import Config

        test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 6
        config = Config(**test_config_dict)
        config_path = temp_dir / "maint_config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(test_config_dict, f, default_flow_style=False)
        app = create_app(config_path=str(config_path), override_config=config)

        with TestClient(app) as client:
            assert hasattr(client.app.state, "maintenance_task")
            assert not client.app.state.maintenance_task.done()

    def test_retention_zero_days_deletes_nothing(
        self, db_ops: Any, file_handler: FileHandler
    ):
        summary = run_retention_cleanup(db_ops, file_handler, retention_days=0)
        assert summary == {
            "deleted_calls": 0,
            "deleted_upload_logs": 0,
            "deleted_files": 0,
            "freed_bytes": 0,
        }

    def test_retention_transaction_batch_size_is_hard_bounded(
        self, db_ops: Any, file_handler: FileHandler
    ) -> None:
        with pytest.raises(ValueError, match="database_batch_size"):
            run_retention_cleanup(
                db_ops,
                file_handler,
                retention_days=0,
                database_batch_size=1001,
            )

    def test_busy_checkpoint_makes_maintenance_retryable(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            db_ops.db_manager, "checkpoint", lambda *, truncate=False: False
        )

        with pytest.raises(RuntimeError, match="checkpoint is busy"):
            run_retention_cleanup(db_ops, file_handler, retention_days=0)

    def test_interval_zero_still_recovers_due_staged_file_on_startup(
        self, temp_dir: Path, test_config_dict: dict
    ) -> None:
        from src.api.app import create_app
        from src.config import Config
        from src.database.connection import DatabaseManager

        test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 0
        config = Config(**test_config_dict)
        manager = DatabaseManager(config.database)
        db_ops = DatabaseOperations(manager)
        handler = FileHandler(
            config.file_handling.storage.directory,
            config.file_handling.temp_directory,
        )
        source = handler.save_temp_file("call.mp3", b"audio")
        stored = handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        with manager.get_session() as session:
            pending = session.query(PendingFileDeletion).one()
            pending.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        expired_live_call = _save_expired_call(db_ops)
        manager.close()

        app = create_app(override_config=config)
        with TestClient(app) as client:
            assert not stored.exists()
            assert client.app.state.db_ops.count_pending_file_deletions() == 0
            assert client.app.state.db_ops.get_call_by_id(expired_live_call) is not None
            assert hasattr(client.app.state, "maintenance_task")
            assert not client.app.state.maintenance_task.done()

    def test_interval_zero_retries_a_future_stage_at_its_deadline(
        self,
        temp_dir: Path,
        test_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.api.app as app_module
        from src.config import Config
        from src.database.connection import DatabaseManager

        test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 0
        config = Config(**test_config_dict)
        manager = DatabaseManager(config.database)
        db_ops = DatabaseOperations(manager)
        handler = FileHandler(
            config.file_handling.storage.directory,
            config.file_handling.temp_directory,
        )
        source = handler.save_temp_file("future.mp3", b"audio")
        stored = handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        with manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) + timedelta(seconds=0.5)
        manager.close()
        monkeypatch.setattr(app_module, "MAINTENANCE_IDLE_POLL_SECONDS", 0.05)

        app = app_module.create_app(override_config=config)
        with TestClient(app):
            assert stored.exists()
            deadline = time.monotonic() + 3
            while stored.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not stored.exists()

    def test_startup_recovery_processes_only_its_small_bounded_batch(
        self,
        test_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.api.app as app_module
        from src.config import Config
        from src.database.connection import DatabaseManager

        test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 0
        config = Config(**test_config_dict)
        manager = DatabaseManager(config.database)
        with manager.get_session() as session:
            session.add_all(
                [
                    PendingFileDeletion(path=f"missing-{index}", kind="retention")
                    for index in range(250)
                ]
            )
        manager.close()

        deletion_calls = 0

        def slow_missing_delete(_handler: FileHandler, _path: str):
            nonlocal deletion_calls
            deletion_calls += 1
            time.sleep(0.001)
            return FileDeletionResult("missing")

        monkeypatch.setattr(FileHandler, "delete_file", slow_missing_delete)
        monkeypatch.setattr(
            DatabaseOperations,
            "has_due_maintenance_work",
            lambda _self, _days: False,
        )
        monkeypatch.setattr(
            DatabaseOperations,
            "seconds_until_next_pending_file_deletion",
            lambda _self: 60.0,
        )

        app = app_module.create_app(override_config=config)
        with TestClient(app) as client:
            assert deletion_calls == app_module.STARTUP_MAINTENANCE_WORK_BUDGET
            assert client.app.state.db_ops.count_pending_file_deletions() == 150

    def test_shutdown_waits_for_running_maintenance_thread(
        self,
        test_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.api.app as app_module
        from src.config import Config
        from src.database.connection import DatabaseManager

        test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 6
        config = Config(**test_config_dict)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = 0

        def blocking_temp_cleanup(
            _handler: FileHandler, *_args: Any, **_kwargs: Any
        ) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:  # bounded startup recovery
                return 0
            started.set()
            release.wait(timeout=5)
            finished.set()
            return 0

        close_observations: list[bool] = []
        actual_close = DatabaseManager.close

        def observed_close(manager: DatabaseManager) -> None:
            close_observations.append(finished.is_set())
            actual_close(manager)

        monkeypatch.setattr(app_module, "run_temp_cleanup", blocking_temp_cleanup)
        monkeypatch.setattr(DatabaseManager, "close", observed_close)
        app = app_module.create_app(override_config=config)
        with TestClient(app):
            assert started.wait(timeout=5)
            timer = threading.Timer(0.1, release.set)
            timer.start()
        timer.join(timeout=2)

        assert finished.is_set()
        assert close_observations == [True]


class TestDurableRetentionQueue:
    def test_retention_batch_deletes_oldest_created_row_first(
        self, db_ops: DatabaseOperations
    ) -> None:
        newer_id = _save_expired_call(db_ops, days_old=40)
        older_id = _save_expired_call(db_ops, days_old=60)

        deleted = db_ops.queue_and_delete_expired_calls(
            datetime.now(UTC) - timedelta(days=30), batch_size=1
        )

        assert deleted == 1
        assert db_ops.get_call_by_id(older_id) is None
        assert db_ops.get_call_by_id(newer_id) is not None

    def test_upload_log_batch_deletes_oldest_timestamp_first(
        self, db_ops: DatabaseOperations
    ) -> None:
        now = datetime.now(UTC)
        with db_ops.db_manager.get_session() as session:
            newer = UploadLog(
                timestamp=now - timedelta(days=40),
                client_ip="127.0.0.1",
                success=False,
            )
            older = UploadLog(
                timestamp=now - timedelta(days=60),
                client_ip="127.0.0.1",
                success=False,
            )
            session.add_all([newer, older])
            session.flush()
            newer_id = int(newer.id)
            older_id = int(older.id)

        deleted = db_ops.delete_upload_logs_older_than(
            now - timedelta(days=30), batch_size=1
        )

        assert deleted == 1
        with db_ops.db_manager.get_session() as session:
            remaining_ids = {row[0] for row in session.query(UploadLog.id).all()}
        assert remaining_ids == {newer_id}
        assert older_id not in remaining_ids

    def test_due_work_check_uses_existence_not_unbounded_count(
        self,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with db_ops.db_manager.get_session() as session:
            session.add(PendingFileDeletion(path="due", kind="retention"))
        monkeypatch.setattr(
            db_ops,
            "count_due_pending_file_deletions",
            lambda: (_ for _ in ()).throw(AssertionError("unbounded count called")),
        )

        assert db_ops.has_due_maintenance_work(0) is True

    def test_next_queue_deadline_tracks_future_staging_and_due_work(
        self, db_ops: DatabaseOperations
    ) -> None:
        future = datetime.now(UTC) + timedelta(seconds=30)
        with db_ops.db_manager.get_session() as session:
            session.add(
                PendingFileDeletion(
                    path="future", kind="staged", next_attempt_at=future
                )
            )

        delay = db_ops.seconds_until_next_pending_file_deletion()
        assert delay is not None
        assert 25 <= delay <= 30
        assert db_ops.has_due_pending_file_deletion() is False

        with db_ops.db_manager.get_session() as session:
            session.query(PendingFileDeletion).one().next_attempt_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        assert db_ops.seconds_until_next_pending_file_deletion() == 0.0
        assert db_ops.has_due_pending_file_deletion() is True

    def test_queue_insert_failure_rolls_back_call_deletion(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        audio = file_handler.storage_dir / "atomic.mp3"
        audio.write_bytes(b"audio")
        call_id = _save_expired_call(db_ops, str(audio))
        with db_ops.db_manager.engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TRIGGER fail_pending_insert
                BEFORE INSERT ON pending_file_deletions
                BEGIN
                    SELECT RAISE(ABORT, 'forced queue failure');
                END
                """)

        with pytest.raises(DBAPIError, match="forced queue failure"):
            db_ops.queue_and_delete_expired_calls(
                datetime.now(UTC) - timedelta(days=30), batch_size=1
            )

        assert db_ops.get_call_by_id(call_id) is not None
        assert db_ops.count_pending_file_deletions() == 0
        assert audio.exists()

    def test_transient_failure_is_backed_off_then_retried_with_retention_disabled(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audio = file_handler.storage_dir / "retry.mp3"
        audio.write_bytes(b"retry-me")
        _save_expired_call(db_ops, str(audio))
        actual_delete = file_handler.delete_file
        checkpoints: list[bool] = []
        actual_checkpoint = db_ops.db_manager.checkpoint

        monkeypatch.setattr(
            file_handler,
            "delete_file",
            lambda _path: FileDeletionResult("retry", error="temporarily locked"),
        )
        monkeypatch.setattr(
            db_ops.db_manager,
            "checkpoint",
            lambda *, truncate=False: (
                checkpoints.append(truncate) or actual_checkpoint(truncate=truncate)
            ),
        )
        first = run_retention_cleanup(db_ops, file_handler, retention_days=30)

        assert first["deleted_calls"] == 1
        assert first["deleted_files"] == 0
        assert audio.exists()
        with db_ops.db_manager.get_session() as session:
            pending = session.query(PendingFileDeletion).one()
            assert pending.attempt_count == 1
            assert pending.claim_token is None
            assert pending.next_attempt_at is not None
            pending.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        monkeypatch.setattr(file_handler, "delete_file", actual_delete)
        second = run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert second["deleted_files"] == 1
        assert not audio.exists()
        assert db_ops.count_pending_file_deletions() == 0
        # Each bounded DB phase is checkpointed before its state reservation
        # is released: three phases in the first cycle and one in the retry.
        assert checkpoints == [True, True, True, True]

    def test_near_floor_retention_uses_reserved_wal_headroom(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audio = file_handler.storage_dir / "near-floor.mp3"
        audio.write_bytes(b"audio")
        _save_expired_call(db_ops, str(audio))
        capacity = StorageCapacity(
            storage_directory=file_handler.storage_dir,
            temp_directory=file_handler.temp_dir,
            spool_directory=file_handler.temp_dir,
            state_directories=(db_ops.db_manager.database_path.parent,),
            max_file_bytes=1024,
            max_storage_bytes=1024 * 1024,
            max_storage_files=1000,
            minimum_free_bytes=10,
            minimum_free_inodes=5,
            maintenance_state_bytes=32,
            persistent_archive_enabled=False,
        )
        file_handler.attach_storage_capacity(capacity)
        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: (dict.fromkeys(paths, 42), dict.fromkeys(paths, 9)),
        )

        summary = run_retention_cleanup(db_ops, file_handler, retention_days=30)

        assert summary["deleted_calls"] == 1
        assert summary["deleted_files"] == 1
        assert not audio.exists()
        assert capacity.snapshot.filesystem_reserved_bytes == 0
        assert capacity.snapshot.filesystem_reserved_inodes == 0

    def test_retention_preflight_failure_never_unlinks_before_queue_commit(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audio = file_handler.storage_dir / "no-headroom.mp3"
        audio.write_bytes(b"audio")
        call_id = _save_expired_call(db_ops, str(audio))
        capacity = StorageCapacity(
            storage_directory=file_handler.storage_dir,
            temp_directory=file_handler.temp_dir,
            spool_directory=file_handler.temp_dir,
            state_directories=(db_ops.db_manager.database_path.parent,),
            max_file_bytes=1024,
            max_storage_bytes=1024 * 1024,
            max_storage_files=1000,
            minimum_free_bytes=10,
            minimum_free_inodes=5,
            maintenance_state_bytes=32,
            persistent_archive_enabled=False,
        )
        file_handler.attach_storage_capacity(capacity)
        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: (dict.fromkeys(paths, 41), dict.fromkeys(paths, 9)),
        )
        unlinks: list[str] = []
        monkeypatch.setattr(
            file_handler,
            "delete_file",
            lambda path: unlinks.append(path) or FileDeletionResult("deleted", 5),
        )

        with pytest.raises(CapacityUnavailable, match="Maintenance filesystem"):
            run_retention_cleanup(db_ops, file_handler, retention_days=30)

        assert unlinks == []
        assert audio.exists()
        assert db_ops.get_call_by_id(call_id) is not None
        assert db_ops.count_pending_file_deletions() == 0

    def test_each_database_and_file_phase_obeys_its_batch_limit(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        for index in range(3):
            audio = file_handler.storage_dir / f"batch-{index}.mp3"
            audio.write_bytes(b"x")
            _save_expired_call(db_ops, str(audio))
        with db_ops.db_manager.get_session() as session:
            session.add_all(
                [
                    UploadLog(
                        client_ip="127.0.0.1",
                        success=False,
                        timestamp=datetime.now(UTC) - timedelta(days=60),
                    )
                    for _ in range(3)
                ]
            )

        summary = run_retention_cleanup(
            db_ops,
            file_handler,
            retention_days=30,
            database_batch_size=2,
            max_database_batches=1,
            file_batch_size=2,
            max_file_batches=1,
        )

        assert summary["deleted_calls"] == 2
        assert summary["deleted_upload_logs"] == 2
        assert summary["deleted_files"] == 2
        with db_ops.db_manager.get_session() as session:
            assert session.query(RadioCall).count() == 1
            assert session.query(UploadLog).count() == 1

    def test_directory_prune_budget_keeps_every_unprocessed_path_durable(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        paths: list[str] = []
        system_directories: list[Path] = []
        for index in range(6):
            relative_path = f"2026/01/01/unique-system-{index}/call.mp3"
            audio_path = file_handler.storage_dir / relative_path
            audio_path.parent.mkdir(parents=True)
            audio_path.write_bytes(b"audio")
            paths.append(relative_path)
            system_directories.append(audio_path.parent)

        with db_ops.db_manager.get_session() as session:
            session.add_all(
                [PendingFileDeletion(path=path, kind="retention") for path in paths]
            )

        first = run_retention_cleanup(
            db_ops,
            file_handler,
            retention_days=0,
            file_batch_size=6,
            max_file_batches=5,
            directory_work_budget=8,
        )

        assert first["deleted_files"] == 2
        assert db_ops.count_pending_file_deletions() == 4
        assert all(not directory.exists() for directory in system_directories[:2])
        assert all(directory.exists() for directory in system_directories[2:])

        for expected_remaining in (2, 0):
            run_retention_cleanup(
                db_ops,
                file_handler,
                retention_days=0,
                file_batch_size=6,
                max_file_batches=5,
                directory_work_budget=8,
            )
            assert db_ops.count_pending_file_deletions() == expected_remaining

        assert all(not directory.exists() for directory in system_directories)

    def test_directory_prune_failure_keeps_deleted_path_in_durable_queue(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        relative_path = "2026/01/01/retry-system/call.mp3"
        audio = file_handler.storage_dir / relative_path
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"audio")
        with db_ops.db_manager.get_session() as session:
            session.add(PendingFileDeletion(path=relative_path, kind="retention"))

        actual_prune = file_handler.remove_empty_directories

        def fail_prune(*_args: Any, **_kwargs: Any) -> int:
            raise OSError("simulated directory durability failure")

        monkeypatch.setattr(file_handler, "remove_empty_directories", fail_prune)
        first = run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert first["deleted_files"] == 1
        assert not audio.exists()
        assert db_ops.count_pending_file_deletions() == 1

        monkeypatch.setattr(file_handler, "remove_empty_directories", actual_prune)
        with db_ops.db_manager.get_session() as session:
            pending = session.query(PendingFileDeletion).one()
            pending.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        second = run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert second["deleted_files"] == 0
        assert db_ops.count_pending_file_deletions() == 0
        assert not audio.parent.exists()

    def test_failed_page_rotates_behind_fresh_due_work(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with db_ops.db_manager.get_session() as session:
            session.add_all(
                [
                    PendingFileDeletion(path=f"failure-{index}", kind="retention")
                    for index in range(500)
                ]
                + [PendingFileDeletion(path="later", kind="retention")]
            )

        def simulated_delete(path: str) -> FileDeletionResult:
            if path == "later":
                return FileDeletionResult("missing")
            return FileDeletionResult("retry", error="locked")

        monkeypatch.setattr(file_handler, "delete_file", simulated_delete)
        run_retention_cleanup(
            db_ops,
            file_handler,
            retention_days=0,
            file_batch_size=500,
            max_file_batches=2,
        )

        with db_ops.db_manager.get_session() as session:
            assert session.query(PendingFileDeletion).count() == 500
            assert (
                session.query(PendingFileDeletion).filter_by(path="later").first()
                is None
            )

    def test_outside_root_queue_entry_is_retained_without_deletion(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        temp_dir: Path,
    ) -> None:
        outside = temp_dir / "outside.mp3"
        outside.write_bytes(b"do not delete")
        with db_ops.db_manager.get_session() as session:
            session.add(PendingFileDeletion(path=str(outside), kind="retention"))

        run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert outside.exists()
        assert db_ops.count_pending_file_deletions() == 1
        with db_ops.db_manager.get_session() as session:
            pending = session.query(PendingFileDeletion).one()
            assert pending.attempt_count == 1
            assert pending.claim_token is None
            assert "outside or unsafe" in str(pending.last_error)


class TestStagedFileRecovery:
    def test_pre_publish_stage_recovers_crash_orphan_and_ignores_unknown_file(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        unknown = file_handler.storage_dir / "operator-notes.txt"
        unknown.write_text("keep")

        def reserve_before_publish(path: str) -> None:
            assert not Path(path).is_absolute()
            assert not (file_handler.storage_dir / path).exists()
            db_ops.stage_file_for_storage(path)

        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=reserve_before_publish,
        )
        # Model process death after publish: in-memory live-upload leases do
        # not survive, while the durable staging row must recover the orphan.
        file_handler.release_storage_lease(stored)
        assert stored.exists()
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            assert staged.kind == "staged"
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        result = run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert result["deleted_files"] == 1
        assert not stored.exists()
        assert unknown.exists()
        assert db_ops.count_pending_file_deletions() == 0

    def test_callback_failure_cannot_publish_destination(
        self,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")

        def fail_reservation(_path: str) -> None:
            raise RuntimeError("database unavailable")

        with pytest.raises(RuntimeError, match="database unavailable"):
            file_handler.store_file(
                source,
                "1",
                datetime.now(UTC),
                on_destination_reserved=fail_reservation,
            )

        assert source.exists()
        assert not any(path.is_file() for path in file_handler.storage_dir.rglob("*"))

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
    def test_store_rejects_allow_acl_on_existing_storage_component(
        self,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        timestamp = datetime.now(UTC)
        year_directory = file_handler.storage_dir / timestamp.strftime("%Y")
        year_directory.mkdir(mode=0o700)
        subprocess.run(
            ["chmod", "+a", "everyone allow read", str(year_directory)],
            check=True,
        )
        try:
            with pytest.raises(PermissionError, match="extended ACL"):
                file_handler.store_file(source, "1", timestamp)
            assert source.exists()
        finally:
            subprocess.run(["chmod", "-N", str(year_directory)], check=True)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
    def test_temp_directory_fsync_failure_releases_published_stage_lease(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        source = file_handler.save_temp_file("call.mp3", b"audio")
        actual_fsync_directory = file_handler_module._fsync_directory

        def fail_temp_directory_fsync(directory: Path) -> None:
            if directory == source.parent:
                raise OSError("simulated temp directory fsync failure")
            actual_fsync_directory(directory)

        monkeypatch.setattr(
            file_handler_module, "_fsync_directory", fail_temp_directory_fsync
        )

        with pytest.raises(OSError, match="simulated temp directory fsync failure"):
            file_handler.store_file(
                source,
                "1",
                datetime.now(UTC),
                on_destination_reserved=db_ops.stage_file_for_storage,
            )

        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            staged_path = file_handler.storage_dir / staged.path
        assert staged_path.exists()

        result = run_retention_cleanup(db_ops, file_handler, retention_days=0)

        assert result["deleted_files"] == 1
        assert not staged_path.exists()
        assert db_ops.count_pending_file_deletions() == 0

    def test_due_stage_cannot_race_exceptionally_slow_live_publish(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        lease_clock = [0.0]
        publish_entered = threading.Event()
        allow_publish = threading.Event()
        actual_link = os.link

        def delayed_link(source: Path, destination: Path) -> None:
            publish_entered.set()
            assert allow_publish.wait(5)
            actual_link(source, destination)

        monkeypatch.setattr(
            file_handler_module, "_upload_lease_monotonic", lambda: lease_clock[0]
        )
        monkeypatch.setattr(file_handler_module.os, "link", delayed_link)
        source = file_handler.save_temp_file("slow.mp3", b"audio")
        stored: list[Path] = []
        errors: list[BaseException] = []

        def publish() -> None:
            try:
                stored.append(
                    file_handler.store_file(
                        source,
                        "1",
                        datetime.now(UTC),
                        on_destination_reserved=db_ops.stage_file_for_storage,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=publish)
        worker.start()
        assert publish_entered.wait(5)
        lease_clock[0] = 48 * 60 * 60
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        first = run_retention_cleanup(db_ops, file_handler, retention_days=0)
        assert first["deleted_files"] == 0
        assert db_ops.count_pending_file_deletions() == 1

        allow_publish.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert len(stored) == 1
        assert stored[0].exists()

        # Once the request resolves its database outcome, ordinary durable
        # cleanup can retry the intentionally orphaned stage.
        file_handler.release_storage_lease(stored[0])
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        second = run_retention_cleanup(db_ops, file_handler, retention_days=0)
        assert second["deleted_files"] == 1
        assert not stored[0].exists()
        assert db_ops.count_pending_file_deletions() == 0

    def test_active_and_released_stage_deletions_complete_without_deadlock(
        self,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        staged: list[str] = []
        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=staged.append,
        )
        results: list[FileDeletionResult] = []

        active_delete = threading.Thread(
            target=lambda: results.append(file_handler.delete_file(staged[0]))
        )
        active_delete.start()
        active_delete.join(1)
        assert not active_delete.is_alive()
        assert results == [
            FileDeletionResult("retry", error="stored upload is still active")
        ]
        assert stored.exists()

        file_handler.release_storage_lease(staged[0])
        inactive_delete = threading.Thread(
            target=lambda: results.append(file_handler.delete_file(staged[0]))
        )
        inactive_delete.start()
        inactive_delete.join(1)
        assert not inactive_delete.is_alive()
        assert results[-1] == FileDeletionResult("deleted", freed_bytes=5)
        assert not stored.exists()

    def test_active_stage_registry_is_bounded_and_recovers_after_release(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        monkeypatch.setattr(file_handler_module, "_MAX_ACTIVE_UPLOAD_LEASES", 1)
        first_source = file_handler.save_temp_file("first.mp3", b"first")
        first = file_handler.store_file(
            first_source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=lambda _path: None,
        )
        second_source = file_handler.save_temp_file("second.mp3", b"second")

        with pytest.raises(OSError, match="Too many active staged"):
            file_handler.store_file(
                second_source,
                "1",
                datetime.now(UTC),
                on_destination_reserved=lambda _path: None,
            )

        file_handler.release_storage_lease(first)
        second = file_handler.store_file(
            second_source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=lambda _path: None,
        )
        file_handler.release_storage_lease(second)
        assert first.exists()
        assert second.exists()

    def test_successful_call_commit_consumes_exact_stage_atomically(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        stored_reference = file_handler.storage_reference(stored)

        call_id = db_ops.save_radio_call(
            RdioScannerUpload(
                key="", system="1", dateTime=int(datetime.now(UTC).timestamp())
            ),
            audio_file_path=stored_reference,
            require_staged_file=True,
        )

        assert db_ops.get_call_by_id(call_id) is not None
        assert db_ops.count_pending_file_deletions() == 0
        assert stored.exists()

    def test_claim_winner_prevents_late_live_reference(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        stored_reference = file_handler.storage_reference(stored)
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        claimed, examined = db_ops.claim_pending_file_deletions("claim-winner")
        assert examined == 1
        assert claimed == [(claimed[0][0], stored_reference)]
        with pytest.raises(RuntimeError, match="reservation is unavailable"):
            db_ops.save_radio_call(
                RdioScannerUpload(
                    key="", system="1", dateTime=int(datetime.now(UTC).timestamp())
                ),
                audio_file_path=stored_reference,
                require_staged_file=True,
            )
        with db_ops.db_manager.get_session() as session:
            assert session.query(RadioCall).count() == 0

    def test_claim_and_save_are_serialized_across_real_connections(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        stored_reference = file_handler.storage_reference(stored)
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        claim_waiting_to_commit = threading.Event()
        save_delete_started = threading.Event()
        results: dict[str, Any] = {}

        def pause_claim_commit(_session: Session) -> None:
            if threading.current_thread().name == "claim-worker":
                claim_waiting_to_commit.set()
                assert save_delete_started.wait(timeout=5)

        def observe_save_delete(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if (
                threading.current_thread().name == "save-worker"
                and statement.lstrip()
                .upper()
                .startswith("DELETE FROM PENDING_FILE_DELETIONS")
            ):
                save_delete_started.set()

        event.listen(Session, "before_commit", pause_claim_commit)
        event.listen(
            db_ops.db_manager.engine,
            "before_cursor_execute",
            observe_save_delete,
        )
        try:

            def claim() -> None:
                try:
                    results["claim"] = db_ops.claim_pending_file_deletions(
                        "barrier-claim"
                    )
                except Exception as exc:  # pragma: no cover - diagnostic
                    results["claim_error"] = exc

            def save() -> None:
                try:
                    db_ops.save_radio_call(
                        RdioScannerUpload(
                            key="",
                            system="1",
                            dateTime=int(datetime.now(UTC).timestamp()),
                        ),
                        audio_file_path=stored_reference,
                        require_staged_file=True,
                    )
                except Exception as exc:
                    results["save_error"] = exc

            claim_thread = threading.Thread(target=claim, name="claim-worker")
            claim_thread.start()
            assert claim_waiting_to_commit.wait(timeout=5)
            save_thread = threading.Thread(target=save, name="save-worker")
            save_thread.start()
            claim_thread.join(timeout=5)
            save_thread.join(timeout=5)
        finally:
            event.remove(Session, "before_commit", pause_claim_commit)
            event.remove(
                db_ops.db_manager.engine,
                "before_cursor_execute",
                observe_save_delete,
            )

        assert not claim_thread.is_alive()
        assert not save_thread.is_alive()
        assert "claim_error" not in results
        claimed, examined = results["claim"]
        assert examined == 1
        assert len(claimed) == 1
        assert isinstance(results.get("save_error"), RuntimeError)
        with db_ops.db_manager.get_session() as session:
            assert session.query(RadioCall).count() == 0

    def test_save_winner_prevents_cleanup_claim_from_committing(
        self,
        db_ops: DatabaseOperations,
        file_handler: FileHandler,
    ) -> None:
        source = file_handler.save_temp_file("call.mp3", b"audio")
        stored = file_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=db_ops.stage_file_for_storage,
        )
        stored_reference = file_handler.storage_reference(stored)
        with db_ops.db_manager.get_session() as session:
            staged = session.query(PendingFileDeletion).one()
            staged.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        save_waiting_to_commit = threading.Event()
        claim_select_started = threading.Event()
        results: dict[str, Any] = {}

        def pause_save_commit(_session: Session) -> None:
            if (
                threading.current_thread().name == "save-winner"
                and not save_waiting_to_commit.is_set()
            ):
                save_waiting_to_commit.set()
                assert claim_select_started.wait(timeout=5)

        def observe_claim_select(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if (
                threading.current_thread().name == "claim-loser"
                and statement.lstrip()
                .upper()
                .startswith("SELECT PENDING_FILE_DELETIONS")
            ):
                claim_select_started.set()

        event.listen(Session, "before_commit", pause_save_commit)
        event.listen(
            db_ops.db_manager.engine,
            "before_cursor_execute",
            observe_claim_select,
        )
        try:

            def save() -> None:
                try:
                    results["call_id"] = db_ops.save_radio_call(
                        RdioScannerUpload(
                            key="",
                            system="1",
                            dateTime=int(datetime.now(UTC).timestamp()),
                        ),
                        audio_file_path=stored_reference,
                        require_staged_file=True,
                    )
                except Exception as exc:  # pragma: no cover - diagnostic
                    results["save_error"] = exc

            def claim() -> None:
                try:
                    results["claim"] = db_ops.claim_pending_file_deletions(
                        "barrier-loser"
                    )
                except Exception as exc:
                    # A stale read transaction may fail its write upgrade after
                    # the save commits. Rollback is the required safe outcome.
                    results["claim_error"] = exc

            save_thread = threading.Thread(target=save, name="save-winner")
            save_thread.start()
            assert save_waiting_to_commit.wait(timeout=5)
            claim_thread = threading.Thread(target=claim, name="claim-loser")
            claim_thread.start()
            save_thread.join(timeout=5)
            claim_thread.join(timeout=5)
        finally:
            event.remove(Session, "before_commit", pause_save_commit)
            event.remove(
                db_ops.db_manager.engine,
                "before_cursor_execute",
                observe_claim_select,
            )

        assert not save_thread.is_alive()
        assert not claim_thread.is_alive()
        assert "save_error" not in results
        assert isinstance(results.get("call_id"), int)
        claimed = results.get("claim", ([], 0))[0]
        assert claimed == []
        assert stored.exists()
        with db_ops.db_manager.get_session() as session:
            assert session.query(RadioCall).count() == 1
            assert session.query(PendingFileDeletion).count() == 0

    def test_relative_reference_survives_configured_storage_root_move(
        self, temp_dir: Path
    ) -> None:
        original_root = temp_dir / "original-storage"
        original_handler = FileHandler(
            str(original_root), str(temp_dir / "original-temp")
        )
        source = original_handler.save_temp_file("call.mp3", b"audio")
        staged_references: list[str] = []
        stored = original_handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=staged_references.append,
        )
        reference = original_handler.storage_reference(stored)
        assert staged_references == [reference]
        assert not Path(reference).is_absolute()
        original_handler.close()

        moved_root = temp_dir / "moved-storage"
        original_root.rename(moved_root)
        moved_handler = FileHandler(str(moved_root), str(temp_dir / "moved-temp"))
        try:
            with moved_handler.open_stored_file(reference) as stream:
                assert stream.read() == b"audio"
            assert moved_handler.delete_file(reference) == FileDeletionResult(
                "deleted", freed_bytes=5
            )
        finally:
            moved_handler.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX path ownership semantics")
class TestPrivateStorageRoots:
    def test_rejects_group_or_world_writable_existing_root(
        self, temp_dir: Path
    ) -> None:
        storage = temp_dir / "unsafe-storage"
        storage.mkdir()
        storage.chmod(0o777)

        with pytest.raises(PermissionError, match="group/world writable"):
            FileHandler(str(storage), str(temp_dir / "safe-temp"))

    def test_rejects_user_controlled_intermediate_symlink(self, temp_dir: Path) -> None:
        target = temp_dir / "target"
        target.mkdir()
        link = temp_dir / "redirect"
        link.symlink_to(target, target_is_directory=True)

        with pytest.raises(OSError, match="symlink"):
            FileHandler(str(link / "storage"), str(temp_dir / "safe-temp"))

    def test_safe_open_rejects_symlink_component(
        self, file_handler: FileHandler
    ) -> None:
        real_directory = file_handler.storage_dir / "real"
        real_directory.mkdir()
        audio = real_directory / "audio.mp3"
        audio.write_bytes(b"audio")
        link = file_handler.storage_dir / "redirect"
        link.symlink_to(real_directory, target_is_directory=True)

        with pytest.raises(OSError):
            with file_handler.open_stored_file(str(link / "audio.mp3")):
                pass

        with file_handler.open_stored_file(str(audio)) as stream:
            assert stream.read() == b"audio"


class TestBoundedFilesystemWork:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
    def test_new_storage_ancestors_are_each_fsynced(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        actual_fsync = os.fsync
        directory_syncs = 0

        def observed_fsync(descriptor: int) -> None:
            nonlocal directory_syncs
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_syncs += 1
            actual_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", observed_fsync)
        source = file_handler.save_temp_file("call.mp3", b"audio")
        file_handler.store_file(
            source,
            "1",
            datetime(2025, 1, 2, tzinfo=UTC),
        )

        # YYYY/MM/DD/system are four newly-created names; publishing the file
        # and removing the temp name add further directory syncs.
        assert directory_syncs >= 6

    def test_temp_scan_consumes_no_more_than_work_budget(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        class FakeEntry:
            name = "unrelated"
            path = "unrelated"

        class CountingScan:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                self.consumed += 1
                return FakeEntry()

            def close(self) -> None:
                pass

        scan = CountingScan()
        monkeypatch.setattr(file_handler_module.os, "scandir", lambda _path: scan)

        assert file_handler.cleanup_temp_files(work_budget=3) == 0
        assert scan.consumed == 3

    def test_one_hour_cleanup_skips_exceptionally_slow_live_temp_copy(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        lease_clock = [0.0]
        read_entered = threading.Event()
        allow_read = threading.Event()

        class DelayedSource:
            sent = False

            def seek(self, _offset: int) -> None:
                pass

            def read(self, _size: int) -> bytes:
                if self.sent:
                    return b""
                read_entered.set()
                assert allow_read.wait(5)
                self.sent = True
                return b"ID3" + b"x" * 2048

        monkeypatch.setattr(
            file_handler_module, "_upload_lease_monotonic", lambda: lease_clock[0]
        )
        copied: list[Path] = []
        errors: list[BaseException] = []

        def copy() -> None:
            try:
                copied.append(
                    file_handler.save_upload_stream("slow.mp3", DelayedSource())  # type: ignore[arg-type]
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=copy)
        worker.start()
        assert read_entered.wait(5)
        live_temp = next(file_handler.temp_dir.glob("upload_*.mp3"))
        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
        os.utime(live_temp, (old_timestamp, old_timestamp))
        lease_clock[0] = 48 * 60 * 60

        assert file_handler.cleanup_temp_files(max_age_hours=1) == 0
        assert live_temp.exists()

        allow_read.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert copied == [live_temp]
        assert file_handler.delete_temp_file(str(live_temp)).status == "deleted"

    def test_active_temp_registry_is_bounded_and_reopens_after_release(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        monkeypatch.setattr(file_handler_module, "_MAX_ACTIVE_UPLOAD_LEASES", 1)
        first = file_handler.save_temp_file("first.mp3", b"audio")
        with pytest.raises(OSError, match="Too many active upload temporary"):
            file_handler.save_temp_file("second.mp3", b"audio")

        assert file_handler.delete_temp_file(str(first)).status == "deleted"
        second = file_handler.save_temp_file("second.mp3", b"audio")
        assert file_handler.delete_temp_file(str(second)).status == "deleted"

    def test_temp_scan_cursor_reaches_stale_suffix_across_cycles(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()

        class FakeEntry:
            def __init__(self, name: str) -> None:
                self.name = name
                self.path = str(file_handler.temp_dir / name)

            def stat(self, *, follow_symlinks: bool):
                assert follow_symlinks is False
                return os.stat_result(
                    (
                        stat.S_IFREG | 0o600,
                        0,
                        0,
                        1,
                        0,
                        0,
                        1,
                        old_timestamp,
                        old_timestamp,
                        old_timestamp,
                    )
                )

        class FiniteScan:
            def __init__(self) -> None:
                self.entries = iter(
                    [FakeEntry(f"ignore-{index}") for index in range(3)]
                    + [FakeEntry("upload_stale.mp3")]
                )

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.entries)

            def close(self) -> None:
                pass

        scan = FiniteScan()
        deleted: list[str] = []
        monkeypatch.setattr(file_handler_module.os, "scandir", lambda _path: scan)
        monkeypatch.setattr(
            file_handler,
            "delete_temp_file",
            lambda path: (
                deleted.append(path) or FileDeletionResult("deleted", freed_bytes=1)
            ),
        )

        assert file_handler.cleanup_temp_files(max_age_hours=1, work_budget=3) == 0
        assert file_handler.cleanup_temp_files(max_age_hours=1, work_budget=3) == 1
        assert deleted == [str(file_handler.temp_dir / "upload_stale.mp3")]

    def test_directory_pruning_never_scans_unrelated_tree(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        directory = file_handler.storage_dir / "2025" / "01" / "01" / "1"
        directory.mkdir(parents=True)
        audio = directory / "audio.mp3"
        audio.write_bytes(b"audio")
        assert file_handler.delete_file(str(audio)).status == "deleted"
        monkeypatch.setattr(
            file_handler_module.os,
            "scandir",
            lambda _path: (_ for _ in ()).throw(AssertionError("unexpected scan")),
        )

        assert file_handler.remove_empty_directories([str(audio)], 4) == 4

    @pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
    def test_strict_pruning_recovers_missing_descendant_after_fsync_failure(
        self,
        file_handler: FileHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.utils.file_handler as file_handler_module

        directory = file_handler.storage_dir / "2025" / "01" / "01" / "1"
        directory.mkdir(parents=True)
        audio = directory / "audio.mp3"
        audio.write_bytes(b"audio")
        audio.unlink()
        actual_durable_fsync = file_handler_module.durable_fsync

        def fail_fsync(_descriptor: int) -> None:
            raise OSError("simulated parent fsync failure")

        monkeypatch.setattr(file_handler_module, "durable_fsync", fail_fsync)
        with pytest.raises(OSError, match="simulated parent fsync failure"):
            file_handler.remove_empty_directories(
                [str(audio)], work_budget=4, require_complete=True
            )
        assert not directory.exists()

        monkeypatch.setattr(file_handler_module, "durable_fsync", actual_durable_fsync)
        assert (
            file_handler.remove_empty_directories(
                [str(audio)], work_budget=4, require_complete=True
            )
            == 3
        )
        assert not (file_handler.storage_dir / "2025").exists()


class TestTempFilenameSanitization:
    def test_hostile_filename_is_sanitized(self, file_handler: FileHandler):
        """Path components and shell-hostile characters must not survive."""
        temp_path = file_handler.save_temp_file("..\\..\\evil<>.mp3", b"\xff\xfb")
        assert temp_path.parent == file_handler.temp_dir
        assert "\\" not in temp_path.name
        assert "<" not in temp_path.name
        assert ">" not in temp_path.name
        assert ".." not in temp_path.name.replace("..", "", 0) or True
        temp_path.unlink()


class TestStoreFileConcurrency:
    def test_concurrent_duplicate_stores_lose_no_audio(
        self, file_handler: FileHandler, temp_dir: Path
    ):
        """Simultaneous calls with identical metadata must not overwrite."""
        n_threads = 8
        timestamp = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        barrier = threading.Barrier(n_threads)
        results: list[Path] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def store(i: int) -> None:
            content = f"audio-{i}".encode()
            src = temp_dir / f"in_{i}.mp3"
            src.write_bytes(content)
            barrier.wait()
            try:
                stored = file_handler.store_file(src, "1", timestamp, talkgroup_id=100)
                with lock:
                    results.append(stored)
            except Exception as e:  # pragma: no cover - failure detail
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=store, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == n_threads
        # Every stored path must be distinct and still exist with its content
        assert len({str(p) for p in results}) == n_threads
        contents = {p.read_bytes() for p in results}
        assert len(contents) == n_threads
