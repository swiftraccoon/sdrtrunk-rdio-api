"""Regression tests for upload endpoint fixes.

Covers: test-mode authentication, validation error status codes,
callId semantics, API key log redaction, X-Forwarded-For trust,
and unicode API keys.
"""

import logging
from collections.abc import Generator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api import rdioscanner
from src.api.app import create_app
from src.config import Config


@pytest.fixture
def keyed_config_dict(test_config_dict: dict) -> dict:
    """Test config with an API key configured."""
    config = dict(test_config_dict)
    config["security"] = {
        "api_keys": [
            {
                "key": "correct-key-123456",
                "identifier": "test-scanner",
                "description": "test key",
                "allowed_ips": [],
                "allowed_systems": [],
            }
        ],
        "rate_limit": {"enabled": False},
    }
    return config


def _make_client(
    temp_dir: Any, config_dict: dict, *, client_host: str = "testclient"
) -> TestClient:
    config = Config(**config_dict)
    config_path = temp_dir / "keyed_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f, default_flow_style=False)
    app = create_app(config_path=str(config_path), override_config=config)
    return TestClient(app, client=(client_host, 50000))


@pytest.fixture
def keyed_client(temp_dir: Any, keyed_config_dict: dict) -> Generator[TestClient]:
    with _make_client(temp_dir, keyed_config_dict) as client:
        yield client


class TestTestModeAuthentication:
    """Test-mode requests must validate the API key (SDRTrunk Test button)."""

    def test_test_mode_rejects_invalid_api_key(self, keyed_client: TestClient):
        response = keyed_client.post(
            "/api/call-upload",
            data={"key": "wrong-key", "system": "1", "test": "1"},
        )
        assert response.status_code == 401

    def test_test_mode_accepts_valid_api_key(self, keyed_client: TestClient):
        response = keyed_client.post(
            "/api/call-upload",
            data={"key": "correct-key-123456", "system": "1", "test": "1"},
        )
        assert response.status_code == 200
        # SDRTrunk expects this exact phrase for a successful test
        assert "incomplete call data" in response.text

    @pytest.mark.parametrize(
        ("key", "system", "allowed_ips", "allowed_systems"),
        [
            ("unknown-key-1234", "1", [], []),
            ("correct-key-123456", "1", ["192.0.2.10"], []),
            ("correct-key-123456", "2", [], ["1"]),
        ],
        ids=["unknown-key", "disallowed-ip", "disallowed-system"],
    )
    def test_failed_credentials_have_uniform_warning_work(
        self,
        temp_dir: Any,
        keyed_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        system: str,
        allowed_ips: list[str],
        allowed_systems: list[str],
    ) -> None:
        """Policy-denied keys must not trigger an extra key-validity log oracle."""
        api_key = keyed_config_dict["security"]["api_keys"][0]
        api_key["allowed_ips"] = allowed_ips
        api_key["allowed_systems"] = allowed_systems
        warnings: list[tuple[str, tuple[object, ...]]] = []

        def record_warning(
            _logger: logging.Logger, message: str, *args: object
        ) -> None:
            warnings.append((message, args))

        monkeypatch.setattr(
            rdioscanner.security_warning_sampler, "warning", record_warning
        )
        with _make_client(temp_dir, keyed_config_dict) as client:
            response = client.post(
                "/api/call-upload",
                data={"key": key, "system": system, "test": "1"},
            )

        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid API key"}
        assert warnings == [
            (
                "Rejected upload with invalid credentials from %s",
                ("testclient",),
            )
        ]


class TestValidationErrorStatusCodes:
    """Malformed client input must return 400, not 500."""

    def test_non_numeric_datetime_returns_400(self, test_client: TestClient):
        response = test_client.post(
            "/api/call-upload",
            data={"key": "", "system": "1", "dateTime": "not-a-number"},
        )
        assert response.status_code == 400

    def test_far_future_timestamp_returns_400(self, test_client: TestClient):
        response = test_client.post(
            "/api/call-upload",
            data={"key": "", "system": "1", "dateTime": "9999999999"},
        )
        assert response.status_code == 400

    def test_negative_talkgroup_returns_400(self, test_client: TestClient):
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "",
                "system": "1",
                "dateTime": "1700000000",
                "talkgroup": "-5",
            },
        )
        assert response.status_code == 400

    def test_hf_frequency_is_accepted(self, test_client: TestClient):
        """Frequencies below 25 MHz (HF/shortwave) are legitimate."""
        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "",
                "system": "1",
                "dateTime": "1700000000",
                "frequency": "7255000",  # 7.255 MHz
            },
        )
        assert response.status_code == 200


class TestCallIdSemantics:
    """callId in the upload response must be the database id."""

    def test_callid_is_retrievable_via_query_api(self, test_client: TestClient):
        import time

        response = test_client.post(
            "/api/call-upload",
            data={
                "key": "",
                "system": "1",
                # Recent timestamp: a call older than retention_days would
                # be legitimately removed by the maintenance cycle.
                "dateTime": str(int(time.time()) - 60),
                "talkgroup": "100",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        call_id = response.json()["callId"]
        assert call_id is not None

        lookup = test_client.get(f"/api/calls/{int(call_id)}")
        assert lookup.status_code == 200
        assert lookup.json()["system_id"] == "1"


class TestApiKeyLogRedaction:
    """The API key must never appear in logs, even at DEBUG level."""

    def test_key_not_logged_at_debug(
        self, keyed_client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.DEBUG):
            keyed_client.post(
                "/api/call-upload",
                data={
                    "key": "correct-key-123456",
                    "system": "1",
                    "dateTime": "1700000000",
                },
            )
        assert "correct-key-123456" not in caplog.text


class TestXForwardedForTrust:
    """X-Forwarded-For must only be honored from trusted proxies."""

    def test_xff_spoofing_cannot_bypass_ip_restriction(
        self, temp_dir: Any, keyed_config_dict: dict
    ):
        keyed_config_dict["security"]["api_keys"][0]["allowed_ips"] = ["10.9.8.7"]
        with _make_client(temp_dir, keyed_config_dict) as client:
            response = client.post(
                "/api/call-upload",
                data={
                    "key": "correct-key-123456",
                    "system": "1",
                    "dateTime": "1700000000",
                },
                headers={"X-Forwarded-For": "10.9.8.7"},
            )
        # Direct client is "testclient", not 10.9.8.7; spoofed XFF must not help
        assert response.status_code == 401

    def test_xff_honored_from_trusted_proxy(
        self, temp_dir: Any, keyed_config_dict: dict
    ):
        keyed_config_dict["security"]["api_keys"][0]["allowed_ips"] = ["10.9.8.7"]
        keyed_config_dict["security"]["trusted_proxies"] = ["127.0.0.1"]
        with _make_client(
            temp_dir, keyed_config_dict, client_host="127.0.0.1"
        ) as client:
            response = client.post(
                "/api/call-upload",
                data={
                    "key": "correct-key-123456",
                    "system": "1",
                    "dateTime": "1700000000",
                },
                headers={"X-Forwarded-For": "10.9.8.7"},
            )
        assert response.status_code == 200


class TestUnicodeApiKey:
    """Non-ASCII configured keys must fail closed (401), not crash (500)."""

    def test_unicode_key_mismatch_returns_401(
        self, temp_dir: Any, keyed_config_dict: dict
    ):
        keyed_config_dict["security"]["api_keys"][0]["key"] = "schlüssel-ünïcode"
        with _make_client(temp_dir, keyed_config_dict) as client:
            response = client.post(
                "/api/call-upload",
                data={"key": "wrong", "system": "1", "dateTime": "1700000000"},
            )
        assert response.status_code == 401
