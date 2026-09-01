"""Security regression tests for authenticated and system-scoped read APIs."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.app import _cleanup_lifespan_resources, create_app
from src.api.query import _ExpensiveReadGate
from src.config import Config
from src.exceptions import ConfigurationError
from src.middleware.rate_limiter import get_limiter
from src.models.api_models import RdioScannerUpload

READ_KEY = "system-one-read-key-123456"


def _secure_config(test_config_dict: dict[str, Any]) -> Config:
    config_dict = deepcopy(test_config_dict)
    config_dict["security"] = {
        "api_keys": [
            {
                "key": READ_KEY,
                "identifier": "system-one-reader",
                "allowed_ips": [],
                "allowed_systems": ["1"],
            }
        ],
        "allow_unauthenticated_uploads": False,
        "allow_unauthenticated_reads": False,
        "trusted_proxies": [],
        "rate_limit": {"enabled": False},
    }
    return Config(**config_dict)


def _save_call(client: TestClient, system: str, audio_path: Path) -> int:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"\xff\xfb" + b"\x00" * 64)
    return client.app.state.db_ops.save_radio_call(
        RdioScannerUpload(
            key=READ_KEY,
            system=system,
            dateTime=int(datetime.now(UTC).timestamp()),
            talkgroup=100 + int(system),
            talkgroupLabel=f"Talkgroup {system}",
            audio_filename=audio_path.name,
            audio_content_type="audio/mpeg",
            audio_size=audio_path.stat().st_size,
        ),
        audio_file_path=str(audio_path),
        upload_ip=f"192.0.2.{system}",
        api_key_id="system-one-reader",
    )


def test_health_is_public_but_every_read_endpoint_requires_api_key(
    test_config_dict: dict[str, Any], temp_dir: Path
):
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:
        call_id = _save_call(client, "1", temp_dir / "one.mp3")

        assert client.get("/health").status_code == 200
        for path in (
            "/api/calls",
            f"/api/calls/{call_id}",
            "/api/systems",
            "/api/talkgroups",
            f"/api/calls/{call_id}/audio",
            "/metrics",
        ):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "X-API-Key"


def test_duplicate_api_key_headers_are_rejected(test_config_dict: dict[str, Any]):
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:
        response = client.get(
            "/api/calls",
            headers=[("X-API-Key", READ_KEY), ("X-API-Key", READ_KEY)],
        )

    assert response.status_code == 401


def test_ip_restricted_key_does_not_disclose_that_the_secret_is_valid(
    test_config_dict: dict[str, Any],
) -> None:
    config = _secure_config(test_config_dict)
    config.security.api_keys[0].allowed_ips = ["192.0.2.10"]
    app = create_app(override_config=config)
    with TestClient(app, client=("192.0.2.11", 50000)) as client:
        restricted = client.get("/api/calls", headers={"X-API-Key": READ_KEY})
        unknown = client.get(
            "/api/calls", headers={"X-API-Key": "unknown-read-key-123456"}
        )

    assert restricted.status_code == unknown.status_code == 401
    assert (
        restricted.json() == unknown.json() == {"detail": "Invalid or missing API key"}
    )
    assert restricted.headers["www-authenticate"] == "X-API-Key"


def test_disallowed_ip_attempts_do_not_drain_the_authorized_key_bucket(
    test_config_dict: dict[str, Any],
) -> None:
    config_dict = deepcopy(test_config_dict)
    config_dict["security"] = {
        "api_keys": [
            {
                "key": READ_KEY,
                "identifier": "ip-restricted-reader",
                "allowed_ips": ["192.0.2.10"],
            }
        ],
        "allow_unauthenticated_uploads": False,
        "allow_unauthenticated_reads": False,
        "trusted_proxies": [],
        "rate_limit": {
            "enabled": True,
            "max_requests_per_minute": 2,
            "max_requests_per_hour": 100,
            "max_requests_per_day": 1000,
        },
    }
    limiter = get_limiter()
    limiter._storage.reset()
    try:
        app = create_app(override_config=Config(**config_dict))
        with TestClient(app, client=("192.0.2.11", 50000)) as denied_client:
            denied = [
                denied_client.get(
                    "/api/calls", headers={"X-API-Key": READ_KEY}
                ).status_code
                for _ in range(2)
            ]
        with TestClient(app, client=("192.0.2.10", 50000)) as allowed_client:
            authorized = allowed_client.get(
                "/api/calls", headers={"X-API-Key": READ_KEY}
            )

        assert denied == [401, 401]
        assert authorized.status_code == 200
    finally:
        limiter._storage.reset()


def test_health_returns_503_when_database_probe_fails(test_config_dict: dict[str, Any]):
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:
        client.app.state.db_manager.check_connection = lambda: False
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_health_probe_is_briefly_cached(test_config_dict: dict[str, Any]):
    calls = 0
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:

        def probe() -> bool:
            nonlocal calls
            calls += 1
            return True

        client.app.state.db_manager.check_connection = probe
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200

    assert calls == 1


def test_slow_health_probe_cache_expiry_starts_after_probe(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.app as app_module

    calls = 0
    clock = [100.0]
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:
        monkeypatch.setattr(app_module, "_cache_monotonic", lambda: clock[0])

        def slow_probe() -> bool:
            nonlocal calls
            calls += 1
            clock[0] += 3.0
            return True

        client.app.state.db_manager.check_connection = slow_probe
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200

    assert calls == 1


def test_slow_metrics_cache_expiry_starts_after_query(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.app as app_module

    calls = 0
    clock = [200.0]
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}
    with TestClient(app) as client:
        monkeypatch.setattr(app_module, "_cache_monotonic", lambda: clock[0])

        def slow_statistics(*, allowed_systems: object) -> dict[str, Any]:
            nonlocal calls
            assert allowed_systems == frozenset({"1"})
            calls += 1
            clock[0] += 3.0
            return {"total_calls": 1}

        client.app.state.db_ops.get_statistics = slow_statistics
        assert client.get("/metrics", headers=headers).status_code == 200
        assert client.get("/metrics", headers=headers).status_code == 200

    assert calls == 1


def test_expensive_read_gate_rejects_same_principal_without_waiting(
    test_config_dict: dict[str, Any],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}

    with TestClient(app) as client:

        def blocked_query(**_kwargs: object) -> dict[str, Any]:
            entered.set()
            assert release.wait(timeout=5)
            return {"calls": [], "total": 0, "total_pages": 0}

        client.app.state.db_ops.query_calls = blocked_query
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.get, "/api/calls", headers=headers)
            assert entered.wait(timeout=5)
            rejected = client.get("/api/calls", headers=headers)
            assert rejected.status_code == 503
            assert rejected.headers["retry-after"] == "1"
            assert not first.done()
            release.set()
            assert first.result(timeout=5).status_code == 200

        # Admission is released when the database operation completes.
        assert client.get("/api/calls", headers=headers).status_code == 200


def test_global_expensive_read_gate_covers_every_archive_scan(
    test_config_dict: dict[str, Any],
) -> None:
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}
    gate = _ExpensiveReadGate(global_limit=1, per_principal_limit=1)
    assert gate.try_acquire("occupied")

    with TestClient(app) as client:
        client.app.state.expensive_read_gate = gate
        for path in ("/api/calls", "/api/systems", "/api/talkgroups", "/metrics"):
            response = client.get(path, headers=headers)
            assert response.status_code == 503, path
            assert response.headers["retry-after"] == "1"

    gate.release("occupied")


def test_metrics_cache_miss_is_singleflight(
    test_config_dict: dict[str, Any],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    statistics_calls = 0
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}

    with TestClient(app) as client:

        def blocked_statistics(*, allowed_systems: object) -> dict[str, Any]:
            nonlocal statistics_calls
            assert allowed_systems == frozenset({"1"})
            statistics_calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return {"total_calls": 7}

        client.app.state.db_ops.get_statistics = blocked_statistics
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.get, "/metrics", headers=headers)
            assert entered.wait(timeout=5)
            assert len(client.app.state.monitoring_tasks) == 1
            second = executor.submit(client.get, "/metrics", headers=headers)
            assert statistics_calls == 1
            release.set()
            responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["total_calls"] for response in responses] == [7, 7]
    assert statistics_calls == 1


def test_metrics_singleflight_waiters_are_fail_fast_bounded(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.app as app_module

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(app_module, "MAX_METRICS_SINGLEFLIGHT_WAITERS", 1)
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}

    with TestClient(app) as client:

        def blocked_statistics(*, allowed_systems: object) -> dict[str, Any]:
            assert allowed_systems == frozenset({"1"})
            entered.set()
            assert release.wait(timeout=5)
            return {"total_calls": 7}

        client.app.state.db_ops.get_statistics = blocked_statistics
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.get, "/metrics", headers=headers)
            assert entered.wait(timeout=5)
            rejected = client.get("/metrics", headers=headers)
            assert rejected.status_code == 503
            assert rejected.headers["retry-after"] == "1"
            assert not first.done()
            release.set()
            assert first.result(timeout=5).status_code == 200


@pytest.mark.asyncio
async def test_shutdown_joins_detached_monitoring_worker_before_database_close() -> (
    None
):
    app = FastAPI()
    worker_entered = threading.Event()
    release_worker = threading.Event()
    database_closed = False

    def blocked_probe() -> bool:
        worker_entered.set()
        assert release_worker.wait(timeout=5)
        return True

    class Database:
        def close(self) -> None:
            nonlocal database_closed
            database_closed = True

    worker = asyncio.create_task(asyncio.to_thread(blocked_probe))
    app.state.monitoring_tasks = {worker}
    app.state.monitoring_shutting_down = False
    app.state.storage_capacity = None
    app.state.maintenance_task = None
    app.state.file_handler = None
    app.state.db_manager = Database()
    app.state.db_ops = object()
    assert await asyncio.to_thread(worker_entered.wait, 1)

    cleanup = asyncio.create_task(_cleanup_lifespan_resources(app))
    await asyncio.sleep(0)
    assert not cleanup.done()
    assert not database_closed

    release_worker.set()
    await asyncio.wait_for(cleanup, timeout=2)
    assert database_closed
    assert app.state.monitoring_tasks == set()


def test_health_probe_is_singleflight_and_waiters_are_fail_fast_bounded(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.app as app_module

    entered = threading.Event()
    release = threading.Event()
    probe_calls = 0
    monkeypatch.setattr(app_module, "MAX_HEALTH_SINGLEFLIGHT_WAITERS", 1)
    app = create_app(override_config=_secure_config(test_config_dict))

    with TestClient(app) as client:

        def blocked_probe() -> bool:
            nonlocal probe_calls
            probe_calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return True

        client.app.state.db_manager.check_connection = blocked_probe
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.get, "/health")
            assert entered.wait(timeout=5)
            rejected = client.get("/health")
            assert rejected.status_code == 503
            assert rejected.headers["retry-after"] == "1"
            assert not first.done()
            assert probe_calls == 1
            release.set()
            assert first.result(timeout=5).status_code == 200

        # The completed singleflight result is cached and admission recovered.
        assert client.get("/health").status_code == 200

    assert probe_calls == 1


def test_allowed_systems_scope_calls_audio_summaries_and_metrics(
    test_config_dict: dict[str, Any], temp_dir: Path
):
    app = create_app(override_config=_secure_config(test_config_dict))
    headers = {"X-API-Key": READ_KEY}
    with TestClient(app) as client:
        storage = Path(client.app.state.config.file_handling.storage.directory)
        allowed_id = _save_call(client, "1", storage / "allowed.mp3")
        hidden_id = _save_call(client, "2", storage / "hidden.mp3")

        calls = client.get("/api/calls", headers=headers)
        assert calls.status_code == 200
        assert calls.json()["total"] == 1
        assert [call["system_id"] for call in calls.json()["calls"]] == ["1"]

        assert (
            client.get(f"/api/calls/{allowed_id}", headers=headers).status_code == 200
        )
        assert client.get(f"/api/calls/{hidden_id}", headers=headers).status_code == 404
        assert (
            client.get(f"/api/calls/{allowed_id}/audio", headers=headers).status_code
            == 200
        )
        assert (
            client.get(f"/api/calls/{hidden_id}/audio", headers=headers).status_code
            == 404
        )

        systems = client.get("/api/systems", headers=headers)
        assert systems.status_code == 200
        assert [system["system_id"] for system in systems.json()] == ["1"]

        talkgroups = client.get("/api/talkgroups", headers=headers)
        assert talkgroups.status_code == 200
        assert {talkgroup["system_id"] for talkgroup in talkgroups.json()} == {"1"}

        metrics = client.get("/metrics", headers=headers)
        assert metrics.status_code == 200
        body = metrics.json()
        assert body["total_calls"] == 1
        assert body["systems"] == {"1": 1}
        assert body["upload_sources"] == {"192.0.2.1": 1}
        assert body["audio_files_count"] == 1


def test_read_api_key_header_is_exposed_in_openapi(test_config_dict: dict[str, Any]):
    app = create_app(override_config=_secure_config(test_config_dict))
    schema = app.openapi()
    protected_operations = (
        ("/api/calls", "get"),
        ("/api/calls/{call_id}", "get"),
        ("/api/systems", "get"),
        ("/api/talkgroups", "get"),
        ("/api/calls/{call_id}/audio", "get"),
        ("/metrics", "get"),
    )
    for path, method in protected_operations:
        parameters = schema["paths"][path][method]["parameters"]
        assert any(
            parameter["in"] == "header" and parameter["name"] == "X-API-Key"
            for parameter in parameters
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/calls?page=1001",
        "/api/calls?system_id=not-numeric",
        "/api/calls?frequency=6000000001",
        "/api/talkgroups?system_id=12345678901",
        "/api/calls/9223372036854775808",
    ],
)
def test_read_query_bounds_reject_pathological_values(
    path: str, test_config_dict: dict[str, Any]
):
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:
        response = client.get(path, headers={"X-API-Key": READ_KEY})

    assert response.status_code == 422


def test_query_error_log_neutralizes_request_derived_newlines(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    from src.api import query

    logged: list[str] = []

    def capture_error(template: str, *values: object) -> None:
        logged.append(template % values)

    monkeypatch.setattr(query.logger, "error", capture_error)
    app = create_app(override_config=_secure_config(test_config_dict))
    with TestClient(app) as client:

        def fail_query(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("forged\nlog-line\x1b")

        client.app.state.db_ops.query_calls = fail_query
        response = client.get("/api/calls", headers={"X-API-Key": READ_KEY})

    assert response.status_code == 500
    assert all("forged\nlog-line" not in message for message in logged)
    assert any("forged_log-line_" in message for message in logged)


def test_create_app_requires_existing_config_without_override(temp_dir: Path):
    with pytest.raises(ConfigurationError, match="Required config file"):
        create_app(config_path=str(temp_dir / "missing.yaml"))


def test_no_keys_requires_explicit_upload_compatibility_flag(
    test_config_dict: dict[str, Any],
):
    config_dict = deepcopy(test_config_dict)
    config_dict["security"]["allow_unauthenticated_uploads"] = False
    config_dict["security"]["allow_unauthenticated_reads"] = False
    with pytest.raises(ConfigurationError, match="No API keys are configured"):
        create_app(override_config=Config(**config_dict))


def test_failed_api_key_guesses_consume_the_rate_limit(
    test_config_dict: dict[str, Any],
):
    config_dict = deepcopy(test_config_dict)
    config_dict["security"] = {
        "api_keys": [{"key": READ_KEY, "identifier": "rate-limit-reader"}],
        "allow_unauthenticated_uploads": False,
        "allow_unauthenticated_reads": False,
        "trusted_proxies": [],
        "rate_limit": {
            "enabled": True,
            "max_requests_per_minute": 2,
            "max_requests_per_hour": 100,
            "max_requests_per_day": 1000,
        },
    }
    limiter = get_limiter()
    limiter._storage.reset()
    try:
        app = create_app(override_config=Config(**config_dict))
        with TestClient(app) as client:
            statuses = [
                client.get(
                    "/api/calls", headers={"X-API-Key": f"wrong-key-{index:016d}"}
                ).status_code
                for index in range(3)
            ]
        assert statuses == [401, 401, 429]
    finally:
        limiter._storage.reset()


def test_future_call_does_not_inflate_current_time_windows(
    test_config: Config, db_ops: Any
):
    # The upload model permits a small amount of sender clock skew. Even that
    # bounded future skew must not count as activity that already happened.
    future = datetime.now(UTC) + timedelta(minutes=2)
    db_ops.save_radio_call(
        RdioScannerUpload(key="", system="1", dateTime=int(future.timestamp()))
    )

    stats = db_ops.get_statistics()
    assert stats["total_calls"] == 1
    assert stats["calls_today"] == 0
    assert stats["calls_last_hour"] == 0


def test_retention_uses_server_created_at_not_untrusted_call_timestamp(
    db_ops: Any,
):
    untrusted_old_timestamp = datetime.now(UTC) - timedelta(days=3650)
    call_id = db_ops.save_radio_call(
        RdioScannerUpload(
            key="", system="1", dateTime=int(untrusted_old_timestamp.timestamp())
        )
    )

    db_ops.cleanup_old_data(days_to_keep=30)
    assert db_ops.get_call_by_id(call_id) is not None
