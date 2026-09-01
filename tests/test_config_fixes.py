"""Regression tests: config must fail loud, and dead options must be gone."""

import logging
import os
import stat
from pathlib import Path

import pytest
import yaml

from src.config import (
    MAX_CONFIG_FILE_BYTES,
    MAX_TOTAL_LOG_STORAGE_MB,
    APIKeyConfig,
    Config,
    LogFileConfig,
    prepare_private_directory,
)
from src.exceptions import ConfigurationError


class TestFailLoudConfigLoading:
    def test_invalid_yaml_raises(self, temp_dir: Path):
        """A typo'd config must not silently start an open-access server."""
        bad = temp_dir / "broken.yaml"
        bad.write_text("security:\n  api_keys:\n   - key: [unclosed\n")
        bad.chmod(0o600)
        with pytest.raises(ConfigurationError):
            Config.load_from_file(str(bad))

    def test_invalid_yaml_error_does_not_echo_secret_line(self, temp_dir: Path):
        sentinel = "SECRET-MUST-NOT-APPEAR"
        bad = temp_dir / "broken-secret.yaml"
        bad.write_text(f'security: ["{sentinel}"\n')
        bad.chmod(0o600)

        with pytest.raises(ConfigurationError) as caught:
            Config.load_from_file(str(bad))

        assert sentinel not in str(caught.value)

    def test_invalid_values_raise(self, temp_dir: Path):
        bad = temp_dir / "bad_values.yaml"
        bad.write_text("processing:\n  mode: definitely-not-a-mode\n")
        bad.chmod(0o600)
        with pytest.raises(ConfigurationError):
            Config.load_from_file(str(bad))

    def test_invalid_secret_is_hidden_from_validation_error(self, temp_dir: Path):
        sentinel = "secret-that-must-not-leak"
        bad = temp_dir / "invalid-secret.yaml"
        bad.write_text(f"security:\n  api_keys:\n    - key: ' {sentinel} '\n")
        bad.chmod(0o600)

        with pytest.raises(ConfigurationError) as caught:
            Config.load_from_file(str(bad))

        assert sentinel not in str(caught.value)

    def test_missing_file_still_returns_defaults(self, temp_dir: Path):
        """Library callers may still explicitly use default configuration."""
        config = Config.load_from_file(str(temp_dir / "nope.yaml"))
        assert config.server.port == 8080

    def test_missing_required_file_raises(self, temp_dir: Path):
        with pytest.raises(ConfigurationError, match="Required config file"):
            Config.load_from_file(str(temp_dir / "missing.yaml"), require_exists=True)

    def test_oversized_config_is_rejected_before_parsing(self, temp_dir: Path):
        oversized = temp_dir / "oversized.yaml"
        oversized.write_bytes(b"#" * (MAX_CONFIG_FILE_BYTES + 1))
        oversized.chmod(0o600)

        with pytest.raises(ConfigurationError, match="safety limit"):
            Config.load_from_file(str(oversized))

    def test_nested_duplicate_yaml_key_is_rejected_without_echoing_secrets(
        self, temp_dir: Path
    ) -> None:
        first_sentinel = "first-secret-must-not-appear"
        second_sentinel = "second-secret-must-not-appear"
        duplicate = temp_dir / "duplicate.yaml"
        duplicate.write_text(
            "security:\n"
            "  api_keys:\n"
            f"    - key: {first_sentinel}\n"
            f"      key: {second_sentinel}\n"
            "      identifier: scanner\n"
        )
        duplicate.chmod(0o600)

        with pytest.raises(ConfigurationError, match="Could not parse YAML") as caught:
            Config.load_from_file(str(duplicate))

        error = str(caught.value)
        assert first_sentinel not in error
        assert second_sentinel not in error

    def test_yaml_merge_keys_are_rejected(self, temp_dir: Path) -> None:
        merged = temp_dir / "merged.yaml"
        merged.write_text(
            "defaults: &defaults\n"
            "  allow_unauthenticated_reads: true\n"
            "security:\n"
            "  <<: *defaults\n"
        )
        merged.chmod(0o600)

        with pytest.raises(ConfigurationError, match="Could not parse YAML"):
            Config.load_from_file(str(merged))


def test_rotated_log_configuration_has_a_hard_total_size_cap() -> None:
    defaults = LogFileConfig()
    assert defaults.max_size_mb * (defaults.backup_count + 1) <= 512

    with pytest.raises(ValueError):
        LogFileConfig(max_size_mb=1, backup_count=0)

    with pytest.raises(ValueError, match="may total at most"):
        LogFileConfig(
            max_size_mb=MAX_TOTAL_LOG_STORAGE_MB,
            backup_count=1,
        )


def test_api_key_secret_is_excluded_from_model_representations() -> None:
    sentinel = "representation-secret-key"
    api_key = APIKeyConfig(key=sentinel, identifier="scanner")
    config = Config(security={"api_keys": [api_key.model_dump()]})

    assert sentinel not in repr(api_key)
    assert sentinel not in repr(config)
    assert api_key.model_dump()["key"] == sentinel
    assert config.model_dump()["security"]["api_keys"][0]["key"] == sentinel


@pytest.mark.parametrize(
    "placeholder",
    [
        "change-me-to-a-real-secret",
        "replace-with-a-long-random-secret",
        "PASTE-THE-GENERATED-KEY-HERE",
        "paste-a-random-secret-here",
        "your-generated-api-secret",
        "your-api-key-placeholder",
    ],
)
def test_documented_api_key_placeholders_are_rejected(placeholder: str) -> None:
    with pytest.raises(ValueError, match="placeholder"):
        APIKeyConfig(key=placeholder, identifier="scanner")

    legitimate = APIKeyConfig(
        key="arbitrary-legitimate-16-byte-secret", identifier="scanner"
    )
    assert legitimate.key == "arbitrary-legitimate-16-byte-secret"


def test_only_the_implemented_audio_format_can_be_enabled() -> None:
    configured = Config(file_handling={"accepted_formats": [".MP3"]})
    assert configured.file_handling.accepted_formats == [".mp3"]
    with pytest.raises(ValueError, match="securely validated .mp3"):
        Config(file_handling={"accepted_formats": [".wav"]})


def test_authorization_scope_lists_are_bounded_and_canonical() -> None:
    key = "bounded-authorization-key"
    with pytest.raises(ValueError, match="at most 500 items"):
        Config(
            security={
                "api_keys": [
                    {
                        "key": key,
                        "identifier": "scanner",
                        "allowed_systems": [str(index) for index in range(501)],
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="must be unique"):
        Config(
            security={
                "api_keys": [
                    {
                        "key": key,
                        "identifier": "scanner",
                        "allowed_ips": ["192.0.2.1", "192.0.2.1"],
                    }
                ]
            }
        )

    canonical = Config(
        security={
            "api_keys": [
                {
                    "key": key,
                    "identifier": "scanner",
                    "allowed_ips": ["2001:0DB8::1"],
                }
            ],
            "trusted_proxies": ["2001:0DB8:0:1::1", "192.0.2.99/24"],
        }
    )
    assert canonical.security.api_keys[0].allowed_ips == ["2001:db8::1"]
    assert canonical.security.trusted_proxies == [
        "2001:db8:0:1::1",
        "192.0.2.0/24",
    ]

    for malformed in ("proxy.internal", "192.0.2.1/24", "not-an-ip"):
        with pytest.raises(ValueError, match="literal IP"):
            APIKeyConfig(
                key=key,
                identifier="scanner",
                allowed_ips=[malformed],
            )

    with pytest.raises(ValueError, match="literal IP addresses or CIDR"):
        Config(security={"trusted_proxies": ["proxy.internal"]})

    with pytest.raises(ValueError, match="must be unique"):
        Config(security={"trusted_proxies": ["192.0.2.1", "192.0.2.1/32"]})


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

    def test_old_configs_with_removed_keys_fail_closed(self, temp_dir: Path):
        """Stale or misspelled keys cannot silently change security posture."""
        old = temp_dir / "old.yaml"
        old.write_text(
            "database:\n  path: 'x.db'\n  pool_size: 5\n  max_overflow: 10\n"
        )
        old.chmod(0o600)
        with pytest.raises(ConfigurationError, match="pool_size"):
            Config.load_from_file(str(old))

    def test_cleanup_interval_exists(self):
        """New knob for the background retention task."""
        config = Config()
        assert config.file_handling.storage.cleanup_interval_hours == 6


class TestSecurityConfigValidation:
    @pytest.mark.parametrize("value", ["yes", "true", 1])
    def test_anonymous_access_flags_require_literal_booleans(self, value: object):
        with pytest.raises(ValueError):
            Config(
                security={
                    "allow_unauthenticated_uploads": value,
                    "allow_unauthenticated_reads": False,
                }
            )

    @pytest.mark.parametrize(
        "key",
        ["", " " * 16, "too-short", " valid-key-123456 ", "a" * 513, "é" * 300],
    )
    def test_api_keys_have_safe_length_and_content(self, key: str):
        with pytest.raises(ValueError):
            Config(security={"api_keys": [{"key": key, "identifier": "scanner"}]})

    def test_api_key_identifier_is_required(self):
        with pytest.raises(ValueError, match="identifier"):
            Config(security={"api_keys": [{"key": "stable-secret-key"}]})

    def test_duplicate_key_identifiers_are_rejected(self):
        with pytest.raises(ValueError, match="identifiers must be unique"):
            Config(
                security={
                    "api_keys": [
                        {"key": "a" * 16, "identifier": "scanner"},
                        {"key": "b" * 16, "identifier": "scanner"},
                    ]
                }
            )

    def test_unknown_nested_security_key_is_rejected(self):
        with pytest.raises(ValueError, match="extra_forbidden"):
            Config(security={"allow_anonymous_reads": True})

    def test_assignment_validation_cannot_bypass_bounds(self):
        config = Config()
        with pytest.raises(ValueError):
            config.server.port = 0
        with pytest.raises(ValueError):
            config.security.allow_unauthenticated_reads = "yes"  # type: ignore[assignment]

    def test_api_key_identifier_is_stable_and_not_secret_derived(self):
        from src.config import APIKeyConfig
        from src.security.keys import stable_api_key_identifier

        explicit = APIKeyConfig(key="another-secret-key", identifier="scanner-east")
        assert stable_api_key_identifier(explicit) == "scanner-east"

    def test_unimplemented_database_audio_storage_is_rejected(self):
        with pytest.raises(ValueError, match="discard.*filesystem"):
            Config(file_handling={"storage": {"strategy": "database"}})


class TestBoundedConfiguration:
    @pytest.mark.parametrize(
        "path", ["health", "/nested/health", "/docs", "/health?verbose=true"]
    )
    def test_monitoring_paths_are_single_safe_non_reserved_segments(
        self, path: str
    ) -> None:
        with pytest.raises(ValueError, match="Monitoring path|collides"):
            Config(monitoring={"health_check": {"path": path}})

    def test_monitoring_paths_cannot_collide(self) -> None:
        with pytest.raises(ValueError, match="different paths"):
            Config(
                monitoring={
                    "health_check": {"path": "/status"},
                    "metrics": {"path": "/status"},
                }
            )

    @pytest.mark.parametrize(
        ("certificate", "private_key"),
        [("", ""), ("   ", "   "), ("cert.pem", None), (None, "key.pem")],
    )
    def test_tls_requires_nonblank_certificate_and_key_pair(
        self, certificate: str | None, private_key: str | None
    ):
        with pytest.raises(ValueError):
            Config(server={"ssl_cert": certificate, "ssl_key": private_key})

    def test_minimum_audio_size_cannot_exceed_maximum(self):
        with pytest.raises(ValueError, match="min_file_size_kb"):
            Config(
                file_handling={
                    "max_file_size_mb": 1,
                    "min_file_size_kb": 1025,
                }
            )

    def test_audio_size_ceiling_bounds_pre_auth_spooling(self):
        with pytest.raises(ValueError, match="less than or equal to 512"):
            Config(file_handling={"max_file_size_mb": 513})

    @pytest.mark.parametrize(
        "configuration",
        [
            {"server": {"debug": "false"}},
            {"database": {"enable_wal": "off"}},
            {"security": {"rate_limit": {"enabled": "no"}}},
            {"monitoring": {"metrics": {"enabled": 1}}},
        ],
    )
    def test_all_boolean_configuration_is_strict(self, configuration: dict):
        with pytest.raises(ValueError):
            Config(**configuration)

    @pytest.mark.parametrize(
        "overrides",
        [
            {
                "file_handling": {
                    "temp_directory": "data/audio/temp",
                    "storage": {"directory": "data/audio"},
                }
            },
            {
                "database": {"path": "data/audio/database.sqlite"},
                "file_handling": {"storage": {"directory": "data/audio"}},
            },
            {
                "logging": {"file": {"path": "data/temp/service.log"}},
                "file_handling": {"temp_directory": "data/temp"},
            },
            {
                "file_handling": {
                    "temp_directory": "DATA/AUDIO",
                    "storage": {"directory": "data/audio"},
                }
            },
            {
                "database": {"path": "DATA/STATE/upload_db"},
                "file_handling": {"temp_directory": "data/state"},
            },
            {
                "database": {"path": "data/state.sqlite"},
                "logging": {"file": {"path": "DATA/STATE.SQLITE"}},
            },
            {
                "server": {
                    "ssl_cert": "DATA/TEMP/upload_cert.pem",
                    "ssl_key": "tls/private-key.pem",
                },
                "file_handling": {"temp_directory": "data/temp"},
            },
            {
                "database": {"path": "state/shared.pem"},
                "server": {
                    "ssl_cert": "STATE/SHARED.PEM",
                    "ssl_key": "tls/private-key.pem",
                },
            },
            {
                "logging": {"file": {"path": "tls/service.pem"}},
                "server": {
                    "ssl_cert": "TLS/SERVICE.PEM",
                    "ssl_key": "tls/private-key.pem",
                },
            },
            {
                "database": {"path": "logs/service.log.1"},
                "logging": {"file": {"path": "logs/service.log", "backup_count": 2}},
            },
            {
                "database": {"path": "state/calls.db"},
                "logging": {"file": {"path": "STATE/CALLS.DB-WAL"}},
            },
            {
                "database": {"path": "state/calls.db"},
                "file_handling": {"storage": {"directory": "STATE/CALLS.DB-WAL"}},
            },
            {
                "database": {"path": "state/calls.db"},
                "file_handling": {"temp_directory": "STATE/.RDIO-DATABASE.LOCK"},
            },
            {"database": {"path": "state/.rdio-database.lock"}},
        ],
    )
    def test_destructive_cleanup_roots_cannot_contain_persistent_state(
        self, overrides: dict
    ) -> None:
        with pytest.raises(ValueError, match="must not|contain|different paths"):
            Config(**overrides)

    def test_tls_certificate_and_key_may_share_one_combined_pem(self) -> None:
        config = Config(
            server={"ssl_cert": "tls/combined.pem", "ssl_key": "TLS/COMBINED.PEM"}
        )

        assert config.server.ssl_cert == "tls/combined.pem"


@pytest.mark.parametrize(
    "overrides",
    [
        {"database": {"path": r"C:\state\CALLS~1.DB"}},
        {"file_handling": {"temp_directory": r"C:\state\temp::$DATA"}},
        {"file_handling": {"storage": {"directory": r"C:\state\NUL.txt"}}},
        {"logging": {"file": {"path": "C:\\logs\\service.log "}}},
        {
            "server": {
                "ssl_cert": r"C:\tls\certificate.",
                "ssl_key": r"C:\tls\key.pem",
            }
        },
    ],
)
def test_security_sensitive_config_paths_apply_win32_namespace_policy(
    overrides: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.filesystem_security as filesystem_security

    monkeypatch.setattr(filesystem_security, "_WINDOWS_PATH_RULES_REQUIRED", True)

    with pytest.raises(ValueError, match="Windows filename"):
        Config(**overrides)


def test_config_file_cannot_be_a_database_sidecar_alias(temp_dir: Path) -> None:
    database = temp_dir / "calls.db"
    config_file = Path(f"{database}-wal")
    original = yaml.safe_dump({"database": {"path": str(database)}})
    config_file.write_text(original)
    config_file.chmod(0o600)

    with pytest.raises(ConfigurationError, match="must not conflict"):
        Config.load_from_file(str(config_file))

    assert config_file.read_text() == original


def test_config_file_cannot_be_a_casefolded_rotated_log_alias(
    temp_dir: Path,
) -> None:
    log_file = temp_dir / "service.log"
    config_file = temp_dir / "SERVICE.LOG.1"
    original = yaml.safe_dump(
        {
            "logging": {
                "file": {
                    "enabled": True,
                    "path": str(log_file),
                    "backup_count": 1,
                }
            }
        },
        sort_keys=False,
    )
    config_file.write_text(original)
    config_file.chmod(0o600)

    with pytest.raises(ConfigurationError, match="must not conflict"):
        Config.load_from_file(str(config_file))

    assert config_file.read_text() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link semantics")
def test_config_file_cannot_be_a_hard_link_of_enabled_log(temp_dir: Path) -> None:
    config_file = temp_dir / "config.yaml"
    log_alias = temp_dir / "service.log"
    original = f'logging:\n  file:\n    enabled: true\n    path: "{log_alias}"\n'
    config_file.write_text(original)
    config_file.chmod(0o600)
    os.link(config_file, log_alias)

    with pytest.raises(ConfigurationError, match="must not conflict"):
        Config.load_from_file(str(config_file))

    assert config_file.read_text() == original


def test_config_save_rejects_every_mutable_or_secret_artifact(
    temp_dir: Path,
) -> None:
    database = temp_dir / "state" / "calls.db"
    log_file = temp_dir / "logs" / "service.log"
    certificate = temp_dir / "tls" / "certificate.pem"
    private_key = temp_dir / "tls" / "private-key.pem"
    config = Config(
        database={"path": str(database)},
        logging={"file": {"path": str(log_file), "backup_count": 2}},
        server={"ssl_cert": str(certificate), "ssl_key": str(private_key)},
        file_handling={
            "storage": {"directory": str(temp_dir / "audio")},
            "temp_directory": str(temp_dir / "temp"),
        },
    )
    targets = [
        database,
        Path(f"{database}-wal"),
        database.parent / ".rdio-database.lock",
        Path(f"{log_file}.1"),
        certificate,
        temp_dir / "audio" / "config.yaml",
    ]

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"must remain unchanged")
        with pytest.raises(ConfigurationError, match="must not conflict"):
            config.save_to_file(str(target))
        assert target.read_bytes() == b"must remain unchanged"


def test_create_app_deeply_revalidates_mutated_override(temp_dir: Path) -> None:
    from src.api.app import create_app

    shared_path = temp_dir / "state" / "service"
    config = Config(
        database={"path": str(temp_dir / "state" / "calls.db")},
        logging={"file": {"path": str(shared_path)}},
        security={"allow_unauthenticated_uploads": True},
        file_handling={
            "storage": {"directory": str(temp_dir / "audio")},
            "temp_directory": str(temp_dir / "temp"),
        },
    )
    # Nested assignment validates the child model but cannot run Config's
    # cross-model invariant; the application boundary must revalidate deeply.
    config.database.path = str(shared_path)

    with pytest.raises(ConfigurationError, match="Invalid override"):
        create_app(override_config=config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_save_creates_private_file_and_directories(temp_dir: Path):
    target = temp_dir / "private" / "nested" / "config.yaml"
    Config().save_to_file(str(target))

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permission bits")
def test_root_cannot_rechmod_a_preexisting_broad_system_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chmod_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("src.config.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "src.config.os.fchmod",
        lambda descriptor, mode: chmod_calls.append((descriptor, mode)),
    )

    with pytest.raises(PermissionError, match="Pre-existing root-owned"):
        prepare_private_directory("/")
    assert chmod_calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_load_rejects_group_or_world_access(temp_dir: Path):
    target = temp_dir / "exposed.yaml"
    target.write_text("server:\n  port: 8080\n")
    target.chmod(0o640)

    with pytest.raises(ConfigurationError, match="chmod 600"):
        Config.load_from_file(str(target))


def test_config_load_rejects_non_regular_path(temp_dir: Path):
    with pytest.raises(ConfigurationError, match="not a regular file"):
        Config.load_from_file(str(temp_dir))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_config_load_rejects_symbolic_link(temp_dir: Path):
    target = temp_dir / "target.yaml"
    target.write_text("server:\n  port: 8080\n")
    target.chmod(0o600)
    link = temp_dir / "config.yaml"
    link.symlink_to(target)

    with pytest.raises(ConfigurationError, match="symbolic link"):
        Config.load_from_file(str(link))


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_config_load_rejects_writable_user_controlled_parent(temp_dir: Path):
    unsafe_parent = temp_dir / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    target = unsafe_parent / "config.yaml"
    target.write_text("server:\n  port: 8080\n")
    target.chmod(0o600)

    with pytest.raises(ConfigurationError, match="group/world writable"):
        Config.load_from_file(str(target))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_config_load_rejects_intermediate_user_symlink(temp_dir: Path):
    target = temp_dir / "target"
    target.mkdir()
    config_file = target / "config.yaml"
    config_file.write_text("server:\n  port: 8080\n")
    config_file.chmod(0o600)
    redirect = temp_dir / "redirect"
    redirect.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="symbolic link"):
        Config.load_from_file(str(redirect / "config.yaml"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cleanup semantics")
def test_secure_file_acl_rejection_closes_the_open_descriptor(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.config as config_module

    protected = temp_dir / "protected.yaml"
    protected.write_text("server: {}\n")
    protected.chmod(0o600)
    rejected_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    actual_close = os.close

    def reject(descriptor: int, *, description: str) -> None:
        if description == "Protected regular file":
            rejected_descriptors.append(descriptor)
            raise PermissionError("simulated ACL rejection")

    def observed_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        actual_close(descriptor)

    monkeypatch.setattr(config_module, "reject_insecure_extended_acl", reject)
    monkeypatch.setattr(config_module.os, "close", observed_close)

    with pytest.raises(PermissionError, match="simulated ACL rejection"):
        config_module.open_secure_regular_file(protected)

    assert rejected_descriptors
    assert rejected_descriptors[0] in closed_descriptors


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd semantics")
def test_private_directory_rejects_symlink_inserted_after_prefix_check(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing component must never be resolved through a racing symlink."""
    victim = temp_dir / "unintended-victim"
    victim.mkdir(mode=0o700)
    racing_component = temp_dir / "missing-component"
    intended = racing_component / "nested"
    original_resolve = Path.resolve
    injected = False

    def insert_before_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal injected
        if not injected:
            racing_component.symlink_to(victim, target_is_directory=True)
            injected = True
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", insert_before_resolve)

    with pytest.raises(OSError, match="symlink"):
        prepare_private_directory(intended)

    assert injected
    assert not (victim / "nested").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_active_and_rotated_logs_are_private(temp_dir: Path):
    from src.config import LoggingConfig, setup_logging

    log_path = temp_dir / "private-logs" / "service.log"
    config = LoggingConfig(
        file={
            "enabled": True,
            "path": str(log_path),
            "max_size_mb": 1,
            "backup_count": 2,
        },
        console={"enabled": False},
    )
    setup_logging(config)
    root_logger = logging.getLogger()
    try:
        root_logger.info("security event")
        handler = root_logger.handlers[0]
        handler.doRollover()

        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(f"{log_path}.1").stat().st_mode) == 0o600
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers.clear()


def test_repeated_logging_setup_closes_replaced_file_handler(temp_dir: Path) -> None:
    from src.config import LoggingConfig, setup_logging

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    root_logger.handlers = []
    config = LoggingConfig(
        file={
            "enabled": True,
            "path": str(temp_dir / "logs" / "repeat.log"),
            "max_size_mb": 1,
            "backup_count": 1,
        },
        console={"enabled": False},
    )
    try:
        setup_logging(config)
        first_handler = root_logger.handlers[0]
        first_stream = first_handler.stream
        assert first_stream is not None and not first_stream.closed

        setup_logging(config)

        assert first_handler.stream is None
        assert len(root_logger.handlers) == 1
    finally:
        for handler in tuple(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.handlers = original_handlers


def test_rotating_log_process_lock_is_held_until_handler_close(
    temp_dir: Path,
) -> None:
    from src.config import (
        LoggingConfig,
        _acquire_log_process_lock,
        _release_log_process_lock,
        setup_logging,
    )

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    root_logger.handlers = []
    log_path = temp_dir / "logs" / "service.log"
    config = LoggingConfig(
        file={"enabled": True, "path": str(log_path), "backup_count": 1},
        console={"enabled": False},
    )
    try:
        setup_logging(config)
        with pytest.raises(ConfigurationError, match="already in use"):
            _acquire_log_process_lock(log_path)

        for handler in tuple(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

        descriptor = _acquire_log_process_lock(log_path)
        _release_log_process_lock(descriptor)
    finally:
        for handler in tuple(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.handlers = original_handlers


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_logging_rejects_symbolic_link_destination(temp_dir: Path):
    from src.config import LoggingConfig, setup_logging

    log_directory = temp_dir / "logs"
    log_directory.mkdir(mode=0o700)
    target = log_directory / "target.log"
    target.write_text("must remain untouched")
    link = log_directory / "service.log"
    link.symlink_to(target)
    config = LoggingConfig(
        file={"enabled": True, "path": str(link)},
        console={"enabled": False},
    )

    with pytest.raises(ConfigurationError, match="symbolic link"):
        setup_logging(config)
    assert target.read_text() == "must remain untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_logging_rejects_user_controlled_symlink_ancestor(temp_dir: Path):
    from src.config import LoggingConfig, setup_logging

    target = temp_dir / "target"
    target.mkdir()
    redirect = temp_dir / "redirect"
    redirect.symlink_to(target, target_is_directory=True)
    config = LoggingConfig(
        file={"enabled": True, "path": str(redirect / "service.log")},
        console={"enabled": False},
    )

    with pytest.raises(ConfigurationError, match="symlink"):
        setup_logging(config)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_logging_rejects_world_writable_parent(temp_dir: Path):
    from src.config import LoggingConfig, setup_logging

    log_directory = temp_dir / "logs"
    log_directory.mkdir(mode=0o777)
    log_directory.chmod(0o777)
    config = LoggingConfig(
        file={"enabled": True, "path": str(log_directory / "service.log")},
        console={"enabled": False},
    )

    with pytest.raises(ConfigurationError, match="group/world writable"):
        setup_logging(config)
