"""Regression tests for database-layer fixes.

Covers: real transactional rollback, UTC-based statistics windows,
and version single-sourcing in the health endpoint.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as pkg_version

import pytest
from fastapi.testclient import TestClient

from src.database.connection import DatabaseManager
from src.database.operations import DatabaseOperations, ExpensiveQueryTimeout
from src.models.api_models import RdioScannerUpload
from src.models.database_models import RadioCall


class TestTransactionalRollback:
    def test_rollback_discards_flushed_changes(self, db_manager: DatabaseManager):
        """An exception inside get_session must undo flushed writes."""
        with pytest.raises(RuntimeError):
            with db_manager.get_session() as session:
                session.add(
                    RadioCall(
                        call_timestamp=datetime.now(UTC),
                        system_id="999",
                    )
                )
                session.flush()  # sends INSERT inside the open transaction
                raise RuntimeError("boom")

        with db_manager.get_session() as session:
            count = (
                session.query(RadioCall).filter(RadioCall.system_id == "999").count()
            )
        assert count == 0

    def test_post_commit_cleanup_error_does_not_report_save_failure(
        self,
        db_manager: DatabaseManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cleanup failure cannot make a durable insert look uncommitted."""
        db_ops = DatabaseOperations(db_manager)
        session = db_manager.Session()
        original_commit = session.commit
        commit_calls = 0

        def counted_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            original_commit()

        def failed_close() -> None:
            raise RuntimeError("simulated post-commit close failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "commit", counted_commit)
            patch.setattr(session, "close", failed_close)
            call_id = db_ops.save_radio_call(
                RdioScannerUpload(
                    key="",
                    system="42",
                    dateTime=int(datetime.now(UTC).timestamp()),
                )
            )

        assert commit_calls == 1
        with db_manager.get_session() as verification_session:
            saved = verification_session.get(RadioCall, call_id)
            assert saved is not None
            assert saved.system_id == "42"

    def test_post_commit_logger_error_does_not_report_save_failure(
        self,
        db_manager: DatabaseManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing logging handler cannot obscure a durable insert."""
        db_ops = DatabaseOperations(db_manager)

        def failed_log(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated logging handler failure")

        monkeypatch.setattr("src.database.operations.logger.info", failed_log)
        call_id = db_ops.save_radio_call(
            RdioScannerUpload(
                key="",
                system="43",
                dateTime=int(datetime.now(UTC).timestamp()),
            )
        )

        with db_manager.get_session() as verification_session:
            saved = verification_session.get(RadioCall, call_id)
            assert saved is not None
            assert saved.system_id == "43"


class TestUtcStatisticsWindows:
    def test_calls_last_hour_uses_utc_window(self, db_ops: DatabaseOperations):
        """A 2-hour-old call must not appear in calls_last_hour.

        Timestamps are stored as naive UTC; filtering with local time
        widens or narrows the window by the UTC offset.
        """
        two_hours_ago = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
        five_minutes_ago = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())

        for ts in (two_hours_ago, five_minutes_ago):
            db_ops.save_call(
                RdioScannerUpload(key="", system="42", dateTime=ts),
                client_ip="127.0.0.1",
            )

        stats = db_ops.get_statistics()
        assert stats["calls_last_hour"] == 1
        assert stats["total_calls"] == 2


class TestExpensiveQueryDeadline:
    @pytest.mark.parametrize(
        "operation_name",
        [
            "get_statistics",
            "query_calls",
            "get_systems_summary",
            "get_talkgroups_summary",
        ],
    )
    def test_archive_scans_are_interrupted_at_the_sqlite_vm_boundary(
        self,
        operation_name: str,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.database.operations as operations_module

        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_DEADLINE_SECONDS", 0.0)
        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_PROGRESS_STEPS", 1)

        with pytest.raises(ExpensiveQueryTimeout):
            getattr(db_ops, operation_name)()

    def test_cleanup_snapshot_shares_one_session_and_cumulative_deadline(
        self,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_get_session = db_ops.db_manager.get_session
        opened_sessions = 0

        @contextmanager
        def counted_session():
            nonlocal opened_sessions
            opened_sessions += 1
            with original_get_session() as session:
                yield session

        monkeypatch.setattr(db_ops.db_manager, "get_session", counted_session)
        snapshot = db_ops.get_cleanup_backlog_counts(
            datetime.now(UTC), include_audio_paths=True
        )

        assert opened_sessions == 1
        assert snapshot.expired_audio_paths == 0

        import src.database.operations as operations_module

        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_DEADLINE_SECONDS", 0.0)
        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_PROGRESS_STEPS", 1)
        with pytest.raises(ExpensiveQueryTimeout):
            db_ops.get_cleanup_backlog_counts(
                datetime.now(UTC), include_audio_paths=True
            )

    def test_archive_scan_has_a_deterministic_sqlite_vm_step_ceiling(
        self,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.database.operations as operations_module

        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_DEADLINE_SECONDS", 60.0)
        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_PROGRESS_STEPS", 1)
        monkeypatch.setattr(operations_module, "EXPENSIVE_QUERY_MAX_VM_STEPS", 1)

        with pytest.raises(ExpensiveQueryTimeout, match="execution budget"):
            db_ops.get_statistics()


class TestHealthEndpoint:
    def test_health_version_matches_package(self, test_client: TestClient):
        """Health must report the real package version, not a hardcoded one."""
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["version"] == pkg_version("sdrtrunk-rdio-api")
        assert response.json()["database"] == "connected"
