"""Regression tests for database-layer fixes.

Covers: real transactional rollback, UTC-based statistics windows,
and version single-sourcing in the health endpoint.
"""

from datetime import UTC, datetime, timedelta
from importlib.metadata import version as pkg_version

import pytest
from fastapi.testclient import TestClient

from src.database.connection import DatabaseManager
from src.database.operations import DatabaseOperations
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


class TestHealthEndpoint:
    def test_health_version_matches_package(self, test_client: TestClient):
        """Health must report the real package version, not a hardcoded one."""
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["version"] == pkg_version("sdrtrunk-rdio-api")
        assert response.json()["database"] == "connected"
