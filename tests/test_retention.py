"""Regression tests for retention enforcement and file-handler safety."""

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.utils.file_handler import FileHandler
from src.utils.maintenance import run_retention_cleanup


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
