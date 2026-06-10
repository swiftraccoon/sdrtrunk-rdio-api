"""Regression tests: config must fail loud, and dead options must be gone."""

from pathlib import Path

import pytest

from src.config import Config
from src.exceptions import ConfigurationError


class TestFailLoudConfigLoading:
    def test_invalid_yaml_raises(self, temp_dir: Path):
        """A typo'd config must not silently start an open-access server."""
        bad = temp_dir / "broken.yaml"
        bad.write_text("security:\n  api_keys:\n   - key: [unclosed\n")
        with pytest.raises(ConfigurationError):
            Config.load_from_file(str(bad))

    def test_invalid_values_raise(self, temp_dir: Path):
        bad = temp_dir / "bad_values.yaml"
        bad.write_text("processing:\n  mode: definitely-not-a-mode\n")
        with pytest.raises(ConfigurationError):
            Config.load_from_file(str(bad))

    def test_missing_file_still_returns_defaults(self, temp_dir: Path):
        """Missing file keeps the documented default-config behavior."""
        config = Config.load_from_file(str(temp_dir / "nope.yaml"))
        assert config.server.port == 8080


class TestDeadOptionsRemoved:
    """Options that nothing implements must not exist in the schema."""

    def test_pool_options_removed(self):
        config = Config()
        assert not hasattr(config.database, "pool_size")
        assert not hasattr(config.database, "max_overflow")

    def test_store_fields_removed(self):
        config = Config()
        assert not hasattr(config.processing, "store_fields")

    def test_statistics_block_removed(self):
        config = Config()
        assert not hasattr(config.monitoring, "statistics")

    def test_old_configs_with_removed_keys_still_load(self, temp_dir: Path):
        """Backwards compatibility: stale keys are ignored, not fatal."""
        old = temp_dir / "old.yaml"
        old.write_text(
            "database:\n  path: 'x.db'\n  pool_size: 5\n  max_overflow: 10\n"
        )
        config = Config.load_from_file(str(old))
        assert config.database.path == "x.db"

    def test_cleanup_interval_exists(self):
        """New knob for the background retention task."""
        config = Config()
        assert config.file_handling.storage.cleanup_interval_hours == 6
