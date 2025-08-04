"""Pytest configuration and fixtures."""

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Config
from src.database.connection import DatabaseManager


@pytest.fixture
def temp_dir() -> Generator[Path]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_audio_file(temp_dir: Path) -> Path:
    """Create a temporary MP3 file for testing."""
    audio_file = temp_dir / "test.mp3"
    # Simple MP3 header followed by some data
    # This is a minimal valid MP3 file
    mp3_data = b"\xff\xfb\x90\x00" + b"\x00" * 1024  # Simplified MP3 data
    audio_file.write_bytes(mp3_data)
    return audio_file


@pytest.fixture
def test_config_dict(temp_dir: Path) -> dict:
    """Create test configuration dictionary."""
    return {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "debug": True,
            "cors_origins": ["*"],
            "enable_docs": True,
        },
        "database": {
            "path": str(temp_dir / "test.db"),
            "enable_wal": True,
            "pool_size": 5,
            "max_overflow": 10,
        },
        "security": {
            "api_keys": [],  # No API keys for testing
            "rate_limit": {
                "enabled": False,
                "max_requests_per_minute": 60,
                "max_requests_per_hour": 1000,
                "max_requests_per_day": 10000,
            },
        },
        "file_handling": {
            "accepted_formats": [".mp3"],
            "max_file_size_mb": 100,
            "min_file_size_kb": 1,
            "temp_directory": str(temp_dir / "temp"),
            "storage": {
                "strategy": "filesystem",
                "directory": str(temp_dir / "storage"),
                "organize_by_date": True,
                "retention_days": 30,
            },
        },
        "processing": {
            "mode": "log_only",  # Use log_only for most tests
            "store_fields": [
                "timestamp",
                "system",
                "frequency",
                "talkgroup",
                "source",
                "systemLabel",
                "talkgroupLabel",
            ],
        },
        "monitoring": {
            "health_check": {
                "enabled": True,
                "path": "/health",
            },
            "metrics": {
                "enabled": True,
                "path": "/metrics",
            },
            "statistics": {
                "enabled": True,
                "track_sources": True,
                "track_systems": True,
                "track_talkgroups": True,
            },
        },
        "logging": {
            "level": "DEBUG",
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "file": {
                "enabled": False,
                "path": str(temp_dir / "test.log"),
                "max_size_mb": 10,
                "backup_count": 3,
            },
            "console": {
                "enabled": True,
                "colorize": False,
            },
        },
    }


@pytest.fixture
def test_config_path(temp_dir: Path, test_config_dict: dict) -> Path:
    """Write test configuration to file."""
    config_path = temp_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(test_config_dict, f, default_flow_style=False)
    return config_path


@pytest.fixture
def test_config(test_config_dict: dict) -> Config:
    """Create test Config object."""
    return Config(**test_config_dict)


@pytest.fixture
def test_app(test_config_path: Path, test_config: Config) -> Any:
    """Create test FastAPI app."""
    # Use override_config to pass our test config
    app = create_app(config_path=str(test_config_path), override_config=test_config)
    return app


@pytest.fixture
def test_client(test_app: Any) -> Generator[TestClient]:
    """Create test client."""
    with TestClient(test_app) as client:
        yield client


@pytest.fixture
def test_app_with_storage(temp_dir: Path, test_config_dict: dict) -> Any:
    """Create test app with storage mode enabled."""
    # Modify config for storage mode
    test_config_dict["processing"]["mode"] = "store"
    config = Config(**test_config_dict)

    # Write config to file
    config_path = temp_dir / "config_storage.yaml"
    with open(config_path, "w") as f:
        yaml.dump(test_config_dict, f, default_flow_style=False)

    app = create_app(config_path=str(config_path), override_config=config)
    return app


@pytest.fixture
def test_client_with_storage(
    test_app_with_storage: Any,
) -> Generator[TestClient]:
    """Create test client with storage mode."""
    with TestClient(test_app_with_storage) as client:
        yield client


@pytest.fixture
def db_manager(test_config: Config) -> DatabaseManager:
    """Create test database manager."""
    return DatabaseManager(test_config.database)
