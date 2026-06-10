"""Regression tests: rate limits must come from config, not hardcoded strings."""

import uuid
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Config, RateLimitConfig


@pytest.fixture
def rate_limited_client(temp_dir: Any, test_config_dict: dict) -> TestClient:
    """Client whose config allows only 2 requests per minute."""
    test_config_dict["security"]["rate_limit"] = {
        "enabled": True,
        "max_requests_per_minute": 2,
        "max_requests_per_hour": 1000,
        "max_requests_per_day": 10000,
    }
    config = Config(**test_config_dict)
    config_path = temp_dir / "rl_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(test_config_dict, f, default_flow_style=False)
    app = create_app(config_path=str(config_path), override_config=config)
    return TestClient(app)


class TestRateLimitConfigWiring:
    def test_configured_per_minute_limit_is_enforced(
        self, rate_limited_client: TestClient
    ):
        """max_requests_per_minute: 2 must reject the third request."""
        # Unique x-api-key gives this test its own fresh rate bucket
        headers = {"x-api-key": f"bucket-{uuid.uuid4().hex}"}
        data = {"key": "", "system": "1", "dateTime": "1700000000"}

        with rate_limited_client as client:
            first = client.post("/api/call-upload", data=data, headers=headers)
            second = client.post("/api/call-upload", data=data, headers=headers)
            third = client.post("/api/call-upload", data=data, headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429

    def test_default_per_minute_limit_supports_busy_systems(self):
        """60/min loses calls on busy trunked systems; default must be higher."""
        defaults = RateLimitConfig()
        assert defaults.max_requests_per_minute >= 600
        assert defaults.max_requests_per_hour >= 10000
        assert defaults.max_requests_per_day >= 100000
