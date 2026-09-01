"""Regression tests: rate limits must come from config, not hardcoded strings."""

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Config, RateLimitConfig
from src.middleware.rate_limiter import get_limiter


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
        data = {"key": "", "system": "1", "dateTime": "1700000000"}

        limiter = get_limiter()
        limiter._storage.reset()
        try:
            with rate_limited_client as client:
                first = client.post("/api/call-upload", data=data)
                second = client.post("/api/call-upload", data=data)
                third = client.post("/api/call-upload", data=data)
        finally:
            limiter._storage.reset()

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429

    def test_default_per_minute_limit_supports_busy_systems(self):
        """60/min loses calls on busy trunked systems; default must be higher."""
        defaults = RateLimitConfig()
        assert defaults.max_requests_per_minute >= 600
        assert defaults.max_requests_per_hour >= 10000
        assert defaults.max_requests_per_day >= 100000

    def test_invalid_query_parameters_consume_the_route_limit(
        self, rate_limited_client: TestClient
    ) -> None:
        limiter = get_limiter()
        limiter._storage.reset()
        try:
            with rate_limited_client as client:
                statuses = [
                    client.get("/api/calls?page=invalid").status_code for _ in range(3)
                ]
        finally:
            limiter._storage.reset()

        assert statuses == [422, 422, 429]

    def test_route_validation_is_accounted_exactly_once(
        self, rate_limited_client: TestClient
    ) -> None:
        limiter = get_limiter()
        limiter._storage.reset()
        try:
            with rate_limited_client as client:
                invalid = client.get("/api/calls?page=invalid")
                valid = client.get("/api/calls")
                exhausted = client.get("/api/calls")
        finally:
            limiter._storage.reset()

        assert invalid.status_code == 422
        assert valid.status_code == 200
        assert exhausted.status_code == 429

    def test_cors_preflight_short_circuit_is_rate_limited_and_body_checked(
        self, rate_limited_client: TestClient
    ) -> None:
        limiter = get_limiter()
        limiter._storage.reset()
        headers = {
            "Origin": "https://example.test",
            "Access-Control-Request-Method": "GET",
        }
        try:
            with rate_limited_client as client:
                statuses = [
                    client.options("/api/calls", headers=headers).status_code
                    for _ in range(3)
                ]
        finally:
            limiter._storage.reset()

        assert statuses == [200, 200, 429]

        try:
            with rate_limited_client as client:
                with_body = client.request(
                    "OPTIONS", "/api/calls", headers=headers, content=b"unexpected"
                )
        finally:
            limiter._storage.reset()
        assert with_body.status_code == 400

    @pytest.mark.parametrize(
        ("method", "path", "request_kwargs", "rejection_status"),
        [
            ("GET", "/api/calls", {"content": b"x"}, 400),
            (
                "POST",
                "/api/call-upload",
                {"content": b"x", "headers": {"content-type": "text/plain"}},
                415,
            ),
            (
                "POST",
                "/not-a-route",
                {"content": b"x", "headers": {"content-type": "text/plain"}},
                415,
            ),
            (
                "POST",
                "/api/call-upload",
                {
                    "content": b"x" * (64 * 1024 + 1),
                    "headers": {"content-type": "application/json"},
                },
                413,
            ),
        ],
    )
    def test_early_middleware_rejections_consume_the_route_limit(
        self,
        rate_limited_client: TestClient,
        method: str,
        path: str,
        request_kwargs: dict[str, Any],
        rejection_status: int,
    ) -> None:
        limiter = get_limiter()
        limiter._storage.reset()
        try:
            with rate_limited_client as client:
                statuses = [
                    client.request(method, path, **request_kwargs).status_code
                    for _ in range(3)
                ]
        finally:
            limiter._storage.reset()

        assert statuses == [rejection_status, rejection_status, 429]
