"""Regression tests for CLI fixes (clean, export, init defaults)."""

import csv
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli  # noqa: E402  (project-root module)
from src.config import Config
from src.database.connection import DatabaseManager
from src.database.operations import DatabaseOperations, ExpensiveQueryTimeout
from src.models.api_models import RdioScannerUpload
from src.models.database_models import RadioCall
from src.utils.storage_quota import CapacityUnavailable, StorageCapacity


def _save_old_call(
    db_ops: DatabaseOperations, days_ago: int, audio_file_path: str | None = None
) -> int:
    ts = int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())
    call_id = db_ops.save_radio_call(
        RdioScannerUpload(key="", system="1", dateTime=ts),
        audio_file_path=audio_file_path,
        upload_ip="127.0.0.1",
    )
    # Retention is based on server-controlled ingestion time, not the
    # caller-supplied call timestamp.
    with db_ops.db_manager.get_session() as session:
        call = session.query(RadioCall).filter_by(id=call_id).one()
        call.created_at = datetime.now(UTC) - timedelta(days=days_ago)
        session.commit()
    return call_id


class TestCleanCommand:
    def test_clean_preview_timeout_fails_closed_without_prompting(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def timeout(*_args: object, **_kwargs: object) -> None:
            raise ExpensiveQueryTimeout("sensitive database detail")

        monkeypatch.setattr(DatabaseOperations, "get_cleanup_backlog_counts", timeout)
        monkeypatch.setattr(
            "builtins.input",
            lambda *_args: pytest.fail("cleanup must not prompt after preview timeout"),
        )

        result = cli.clean_command(
            SimpleNamespace(days=30, dry_run=False, yes=False), test_config
        )

        output = capsys.readouterr().out
        assert result == 1
        assert "preview exceeded its bounded execution budget" in output
        assert "sensitive database detail" not in output

    def test_locked_cleanup_count_timeout_stops_before_mutation(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        call_id = _save_old_call(db_ops, days_ago=60)
        original_counts = DatabaseOperations.get_cleanup_backlog_counts
        count_calls = 0

        def timeout_after_preview(
            operations: DatabaseOperations, *args: object, **kwargs: object
        ) -> object:
            nonlocal count_calls
            count_calls += 1
            if count_calls > 1:
                raise ExpensiveQueryTimeout("sensitive database detail")
            return original_counts(operations, *args, **kwargs)

        monkeypatch.setattr(
            DatabaseOperations,
            "get_cleanup_backlog_counts",
            timeout_after_preview,
        )

        result = cli.clean_command(
            SimpleNamespace(days=30, dry_run=False, yes=True), test_config
        )

        output = capsys.readouterr().out
        assert result == 1
        assert "progress query exceeded its bounded execution budget" in output
        assert "sensitive database detail" not in output
        assert db_ops.get_call_by_id(call_id) is not None

    def test_clean_with_only_db_records_does_not_crash(
        self, test_config: Config, db_ops: DatabaseOperations
    ):
        """Old DB rows but zero old files: must not raise UnboundLocalError."""
        _save_old_call(db_ops, days_ago=60)

        args = SimpleNamespace(days=30, dry_run=False, yes=True)
        result = cli.clean_command(args, test_config)

        assert result == 0
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 0

    def test_clean_deletes_files_referenced_by_old_records(
        self, test_config: Config, db_ops: DatabaseOperations
    ):
        """Audio files of deleted rows must be removed even if recently written."""
        storage = Path(test_config.file_handling.storage.directory)
        audio_dir = storage / "2025" / "01" / "01" / "1"
        audio_dir.mkdir(parents=True)
        audio_file = audio_dir / "old_call.mp3"
        audio_file.write_bytes(b"\xff\xfb" + b"\x00" * 64)

        _save_old_call(db_ops, days_ago=60, audio_file_path=str(audio_file))

        args = SimpleNamespace(days=30, dry_run=False, yes=True)
        result = cli.clean_command(args, test_config)

        assert result == 0
        assert not audio_file.exists()
        # Empty date directories are pruned
        assert not audio_dir.exists()

    def test_clean_dry_run_deletes_nothing(
        self, test_config: Config, db_ops: DatabaseOperations
    ):
        storage = Path(test_config.file_handling.storage.directory)
        audio_dir = storage / "2025" / "01" / "01" / "1"
        audio_dir.mkdir(parents=True)
        audio_file = audio_dir / "old_call.mp3"
        audio_file.write_bytes(b"\xff\xfb" + b"\x00" * 64)

        _save_old_call(db_ops, days_ago=60, audio_file_path=str(audio_file))

        args = SimpleNamespace(days=30, dry_run=True, yes=False)
        result = cli.clean_command(args, test_config)

        assert result == 0
        assert audio_file.exists()
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 1

    def test_clean_runs_catch_up_cycles_until_more_than_ten_thousand_rows_are_gone(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=60)
        with db_ops.db_manager.get_session() as session:
            session.execute(
                RadioCall.__table__.insert(),
                [
                    {
                        "created_at": old,
                        "call_timestamp": old,
                        "system_id": "1",
                    }
                    for _ in range(10_001)
                ],
            )

        cycles = 0
        actual_cleanup = cli.run_retention_cleanup

        def observed_cleanup(*args, **kwargs):
            nonlocal cycles
            cycles += 1
            return actual_cleanup(*args, **kwargs)

        monkeypatch.setattr(cli, "run_retention_cleanup", observed_cleanup)
        result = cli.clean_command(
            SimpleNamespace(days=30, dry_run=False, yes=True), test_config
        )

        assert result == 0
        assert cycles >= 2
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 0

    def test_clean_safely_skips_vacuum_without_database_sized_headroom(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _save_old_call(db_ops, days_ago=60)
        vacuum_calls: list[None] = []
        monkeypatch.setattr(cli, "_vacuum_has_headroom", lambda *_args: False)
        monkeypatch.setattr(
            DatabaseManager,
            "vacuum",
            lambda _manager: vacuum_calls.append(None),
        )

        result = cli.clean_command(
            SimpleNamespace(days=30, dry_run=False, yes=True), test_config
        )

        assert result == 0
        assert vacuum_calls == []
        assert "Skipped optional VACUUM" in capsys.readouterr().out

    def test_clean_fails_closed_when_state_write_headroom_cannot_be_reserved(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _save_old_call(db_ops, days_ago=60)

        @contextmanager
        def reject_maintenance(_capacity: StorageCapacity):
            raise CapacityUnavailable("simulated protected reserve exhaustion")
            yield  # pragma: no cover - makes this a context manager generator

        monkeypatch.setattr(
            StorageCapacity, "maintenance_state_guard", reject_maintenance
        )

        result = cli.clean_command(
            SimpleNamespace(days=30, dry_run=False, yes=True), test_config
        )

        assert result == 1
        assert "cannot safely reserve filesystem capacity" in capsys.readouterr().out
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 1

    def test_vacuum_headroom_includes_database_size_and_protected_reserve(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_size = 8 * 1024 * 1024
        protected = (
            (
                test_config.file_handling.minimum_free_space_mb
                + test_config.file_handling.maintenance_state_reserve_mb
            )
            * 1024
            * 1024
        )
        available = [protected + database_size - 1]
        monkeypatch.setattr(
            cli.os,
            "stat",
            lambda *_args, **_kwargs: SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_size=database_size,
            ),
        )
        monkeypatch.setattr(
            cli.os,
            "statvfs",
            lambda _path: SimpleNamespace(
                f_frsize=1,
                f_bsize=1,
                f_bavail=available[0],
            ),
            raising=False,
        )

        assert not cli._vacuum_has_headroom(db_ops.db_manager, test_config)
        available[0] += 1
        assert cli._vacuum_has_headroom(db_ops.db_manager, test_config)


class TestExportCommand:
    def test_export_cannot_replace_the_live_database_even_with_force(
        self,
        test_config: Config,
        db_ops: DatabaseOperations,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _save_old_call(db_ops, days_ago=1)
        database_path = Path(test_config.database.path)
        original_header = database_path.read_bytes()[:16]

        result = cli.export_command(
            SimpleNamespace(
                output=str(database_path),
                start_date=None,
                end_date=None,
                force=True,
            ),
            test_config,
        )

        assert result == 1
        assert "protected application state" in capsys.readouterr().out
        assert database_path.read_bytes()[:16] == original_header
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 1

    @pytest.mark.parametrize("suffix", ["", "-WAL", "-SHM", "-JOURNAL"])
    def test_export_rejects_casefold_aliases_of_database_and_sidecars(
        self,
        suffix: str,
        test_config: Config,
        db_ops: DatabaseOperations,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _save_old_call(db_ops, days_ago=1)
        database_path = Path(test_config.database.path)
        output = database_path.with_name(f"{database_path.name.upper()}{suffix}")

        result = cli.export_command(
            SimpleNamespace(
                output=str(output),
                start_date=None,
                end_date=None,
                force=True,
            ),
            test_config,
        )

        assert result == 1
        assert "protected application state" in capsys.readouterr().out
        with DatabaseManager(test_config.database).get_session() as session:
            assert session.query(RadioCall).count() == 1

    def test_export_rejects_casefold_alias_beneath_storage_root(
        self,
        test_config: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        storage = Path(test_config.file_handling.storage.directory)
        casefold_alias = storage.parent / storage.name.upper() / "export.csv"

        result = cli.export_command(
            SimpleNamespace(
                output=str(casefold_alias),
                start_date=None,
                end_date=None,
                force=True,
            ),
            test_config,
        )

        assert result == 1
        assert "outside audio storage" in capsys.readouterr().out

    def test_export_rejects_ambiguous_win32_output_before_writing(
        self,
        test_config: Config,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import src.filesystem_security as filesystem_security

        monkeypatch.setattr(filesystem_security, "_WINDOWS_PATH_RULES_REQUIRED", True)
        result = cli.export_command(
            SimpleNamespace(
                output=r"C:\state\CALLS~1.WAL",
                start_date=None,
                end_date=None,
                force=True,
            ),
            test_config,
        )

        assert result == 1
        assert "ambiguous Windows filename" in capsys.readouterr().out

    def test_export_cannot_write_inside_audio_or_temp_roots(
        self,
        test_config: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for root in (
            test_config.file_handling.storage.directory,
            test_config.file_handling.temp_directory,
        ):
            output = Path(root) / "destructive-export.csv"
            result = cli.export_command(
                SimpleNamespace(
                    output=str(output),
                    start_date=None,
                    end_date=None,
                    force=True,
                ),
                test_config,
            )
            assert result == 1
            assert not output.exists()

        assert "outside audio storage and temp roots" in capsys.readouterr().out

    def test_export_requires_force_before_replacing_an_existing_file(
        self,
        test_config: Config,
        temp_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output = temp_dir / "existing.csv"
        output.write_text("preserve me", encoding="utf-8")

        result = cli.export_command(
            SimpleNamespace(output=str(output), start_date=None, end_date=None),
            test_config,
        )

        assert result == 1
        assert output.read_text(encoding="utf-8") == "preserve me"
        assert "use --force" in capsys.readouterr().out

    def test_export_end_date_is_inclusive(
        self, test_config: Config, db_ops: DatabaseOperations, temp_dir: Path
    ):
        """--end-date 2025-01-15 must include calls from Jan 15 itself."""
        mid_jan_15 = int(datetime(2025, 1, 15, 10, 0, tzinfo=UTC).timestamp())
        mid_jan_16 = int(datetime(2025, 1, 16, 10, 0, tzinfo=UTC).timestamp())
        for ts in (mid_jan_15, mid_jan_16):
            db_ops.save_radio_call(RdioScannerUpload(key="", system="1", dateTime=ts))

        output = temp_dir / "export.csv"
        args = SimpleNamespace(
            output=str(output), start_date="2025-01-15", end_date="2025-01-15"
        )
        result = cli.export_command(args, test_config)

        assert result == 0
        assert output.exists()
        with open(output) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1

    def test_export_streams_rows_without_materializing_the_result(
        self,
        test_config: Config,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        records = [
            SimpleNamespace(
                call_timestamp=now,
                system_id="1",
                system_label="System",
                talkgroup_id=index,
                talkgroup_label="Dispatch",
                talkgroup_group="Public Safety",
                source_radio_id=None,
                frequency=851_000_000,
                audio_filename=f"call-{index}.mp3",
                audio_size_bytes=128,
                upload_timestamp=now,
            )
            for index in range(2)
        ]

        class StreamingScalars:
            def __init__(self):
                self._records = iter(records)

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._records)

            def all(self):
                raise AssertionError("export must not materialize every row")

        class Result:
            def scalars(self):
                return StreamingScalars()

        class Session:
            def execute(self, query):
                assert query.get_execution_options()["yield_per"] == 500
                assert query.get_execution_options()["stream_results"] is True
                return Result()

        class Manager:
            def __init__(self, _config, **_kwargs):
                self.session = Session()

            def get_session(self):
                return SessionContext(self.session)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class SessionContext:
            def __init__(self, session):
                self.session = session

            def __enter__(self):
                return self.session

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(cli, "DatabaseManager", Manager)
        monkeypatch.setattr(cli, "setup_logging", lambda _config: None)
        output = temp_dir / "streamed.csv"

        result = cli.export_command(
            SimpleNamespace(output=str(output), start_date=None, end_date=None),
            test_config,
        )

        assert result == 0
        with output.open(newline="") as exported:
            assert len(list(csv.DictReader(exported))) == 2


def test_stats_closes_its_read_transaction_before_terminal_output(
    test_config: Config,
    db_ops: DatabaseOperations,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_old_call(db_ops, days_ago=1)
    manager_closed = False
    output_observed = False
    actual_close = DatabaseManager.close

    def observed_close(manager: DatabaseManager) -> None:
        nonlocal manager_closed
        actual_close(manager)
        manager_closed = True

    def observed_print(*values: object, **_kwargs: object) -> None:
        nonlocal output_observed
        output_observed = True
        assert manager_closed, f"terminal output preceded DB close: {values!r}"

    monkeypatch.setattr(DatabaseManager, "close", observed_close)
    monkeypatch.setattr(cli, "print", observed_print, raising=False)

    result = cli.stats_command(
        SimpleNamespace(system=None, talkgroup=None, hours=None, last=20),
        test_config,
    )

    assert result == 0
    assert output_observed


def test_test_db_is_read_only_and_closes_before_success_output(
    test_config: Config,
    db_ops: DatabaseOperations,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_old_call(db_ops, days_ago=1)
    manager_closed = False
    success_observed = False
    actual_close = DatabaseManager.close

    def observed_close(manager: DatabaseManager) -> None:
        nonlocal manager_closed
        assert manager.read_only
        actual_close(manager)
        manager_closed = True

    def observed_print(*values: object, **_kwargs: object) -> None:
        nonlocal success_observed
        if values and "[SUCCESS]" in str(values[0]):
            success_observed = True
            assert manager_closed

    monkeypatch.setattr(DatabaseManager, "close", observed_close)
    monkeypatch.setattr(cli, "print", observed_print, raising=False)

    result = cli.test_db_command(SimpleNamespace(), test_config)

    assert result == 0
    assert success_observed


def test_stats_cli_bounds_materialized_rows_and_date_arithmetic() -> None:
    parser = cli.create_parser()
    assert parser.parse_args(["stats", "--last", "1000"]).last == 1000
    assert parser.parse_args(["stats", "--hours", "87840"]).hours == 87840

    with pytest.raises(SystemExit):
        parser.parse_args(["stats", "--last", "1001"])
    with pytest.raises(SystemExit):
        parser.parse_args(["stats", "--hours", "87841"])


class TestInitDefaults:
    def test_init_defaults_to_real_config_path(self):
        """`init` must generate config/config.yaml, the path the server reads."""
        parser = cli.create_parser()
        args = parser.parse_args(["init"])
        assert args.output == "config/config.yaml"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_init_creates_private_config(self, temp_dir: Path):
        output = temp_dir / "private" / "config.yaml"
        result = cli.init_command(SimpleNamespace(output=str(output), force=False))

        assert result == 0
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


class TestServeApiKeyOverride:
    def test_api_key_file_appends_to_existing_keys(
        self, test_config: Config, temp_dir: Path
    ):
        """--api-key-file adds its secret without exposing it in argv."""
        from src.config import APIKeyConfig

        test_config.security.api_keys = [
            APIKeyConfig(
                key="existing-key-1234",
                identifier="configured-key",
                description="configured",
            )
        ]
        key_file = temp_dir / "api-key"
        key_file.write_text("added-key-123456\n")
        key_file.chmod(0o600)
        args = SimpleNamespace(
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=str(key_file),
            api_key_id="added-key",
            storage_dir=None,
            db_path=None,
        )
        cli.apply_serve_overrides(args, test_config)

        keys = [k.key for k in test_config.security.api_keys]
        assert "existing-key-1234" in keys
        assert "added-key-123456" in keys

    def test_short_api_key_file_fails_cleanly(
        self, test_config: Config, temp_dir: Path
    ):
        key_file = temp_dir / "api-key"
        key_file.write_text("short")
        key_file.chmod(0o600)
        args = SimpleNamespace(
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=str(key_file),
            api_key_id="short-key-test",
            storage_dir=None,
            db_path=None,
        )

        with pytest.raises(cli.ConfigurationError, match="Invalid API key file"):
            cli.apply_serve_overrides(args, test_config)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_group_readable_api_key_file_is_rejected(
        self, test_config: Config, temp_dir: Path
    ):
        key_file = temp_dir / "api-key"
        key_file.write_text("added-key-123456")
        key_file.chmod(0o640)
        args = SimpleNamespace(
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=str(key_file),
            api_key_id="insecure-key-test",
            storage_dir=None,
            db_path=None,
        )

        with pytest.raises(cli.ConfigurationError, match="group or world access"):
            cli.apply_serve_overrides(args, test_config)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX path permissions")
    def test_api_key_file_under_writable_parent_is_rejected(
        self, test_config: Config, temp_dir: Path
    ) -> None:
        unsafe_parent = temp_dir / "unsafe"
        unsafe_parent.mkdir()
        unsafe_parent.chmod(0o777)
        key_file = unsafe_parent / "api-key"
        key_file.write_text("added-key-123456")
        key_file.chmod(0o600)
        args = SimpleNamespace(
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=str(key_file),
            api_key_id="unsafe-parent",
            storage_dir=None,
            db_path=None,
        )

        with pytest.raises(cli.ConfigurationError, match="group/world writable"):
            cli.apply_serve_overrides(args, test_config)

    def test_plaintext_api_key_flag_is_not_accepted(self):
        with pytest.raises(SystemExit):
            cli.create_parser().parse_args(["serve", "--api-key", "secret-in-argv"])

    def test_final_overrides_recheck_config_file_against_database_sidecars(
        self, test_config: Config, temp_dir: Path
    ) -> None:
        database = temp_dir / "state" / "calls.db"
        args = SimpleNamespace(
            config=str(Path(f"{database}-wal")),
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=None,
            api_key_id=None,
            storage_dir=None,
            db_path=str(database),
        )

        with pytest.raises(cli.ConfigurationError, match="configuration file"):
            cli.apply_serve_overrides(args, test_config)

    def test_api_key_file_cannot_become_final_database_sidecar(
        self, test_config: Config, temp_dir: Path
    ) -> None:
        database = temp_dir / "state" / "calls.db"
        key_file = Path(f"{database}-wal")
        key_file.parent.mkdir(parents=True)
        key_file.write_text("safe-length-api-key-material")
        key_file.chmod(0o600)
        args = SimpleNamespace(
            config=None,
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=str(key_file),
            api_key_id="sidecar-key",
            storage_dir=None,
            db_path=str(database),
        )

        with pytest.raises(cli.ConfigurationError, match="API key file"):
            cli.apply_serve_overrides(args, test_config)
        assert key_file.read_text() == "safe-length-api-key-material"


class TestDestructiveArgumentValidation:
    @pytest.mark.parametrize("days", ["0", "-1"])
    def test_clean_rejects_nonpositive_days(self, days: str):
        with pytest.raises(SystemExit):
            cli.create_parser().parse_args(["clean", "--days", days])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command", ["serve", "clean", "stats", "test-db", "export"]
    )
    async def test_stateful_commands_require_their_config_file(
        self,
        command: str,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        missing = temp_dir / "misspelled-config.yaml"
        monkeypatch.setattr(
            cli.sys,
            "argv",
            ["sdrtrunk-rdio-api", command, "--config", str(missing)],
        )

        assert await cli.main() == 1


class TestServeTransportConfig:
    @pytest.mark.asyncio
    async def test_tls_and_read_timeout_are_wired_to_hypercorn(
        self,
        test_config: Config,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        certificate = temp_dir / "certificate.pem"
        private_key = temp_dir / "private-key.pem"
        certificate.write_text("test certificate")
        private_key.write_text("test key")
        private_key.chmod(0o600)
        from src.config import ServerConfig

        test_config.server = ServerConfig(
            **(
                test_config.server.model_dump()
                | {
                    "ssl_cert": str(certificate),
                    "ssl_key": str(private_key),
                    "read_timeout_seconds": 17,
                }
            )
        )
        captured: dict[str, object] = {}

        async def fake_serve(app: object, hypercorn_config: object) -> None:
            captured["app"] = app
            captured["config"] = hypercorn_config

        monkeypatch.setattr(cli, "serve", fake_serve)
        args = SimpleNamespace(
            config=str(temp_dir / "config.yaml"),
            host=None,
            port=None,
            reload=False,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=None,
            storage_dir=None,
            db_path=None,
        )

        await cli.serve_command(args, test_config)

        hypercorn_config = captured["config"]
        assert hypercorn_config.read_timeout == 17
        assert hypercorn_config.include_server_header is False
        assert hypercorn_config.h2_max_concurrent_streams == 32
        assert hypercorn_config.h2_max_header_list_size == 16 * 1024
        assert hypercorn_config.workers == 1
        assert hypercorn_config.certfile == str(certificate)
        assert hypercorn_config.keyfile == str(private_key)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    async def test_group_readable_tls_private_key_is_rejected(
        self,
        test_config: Config,
        temp_dir: Path,
    ):
        from src.config import ServerConfig

        certificate = temp_dir / "certificate.pem"
        private_key = temp_dir / "private-key.pem"
        certificate.write_text("test certificate")
        private_key.write_text("test key")
        private_key.chmod(0o640)
        test_config.server = ServerConfig(
            **(
                test_config.server.model_dump()
                | {"ssl_cert": str(certificate), "ssl_key": str(private_key)}
            )
        )
        args = SimpleNamespace(
            config=str(temp_dir / "config.yaml"),
            host=None,
            port=None,
            reload=False,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=None,
            storage_dir=None,
            db_path=None,
        )

        with pytest.raises(cli.ConfigurationError, match="chmod 600"):
            await cli.serve_command(args, test_config)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
    async def test_tls_private_key_symlink_is_rejected(
        self, test_config: Config, temp_dir: Path
    ) -> None:
        from src.config import ServerConfig

        certificate = temp_dir / "certificate.pem"
        key_target = temp_dir / "key-target.pem"
        private_key = temp_dir / "private-key.pem"
        certificate.write_text("test certificate")
        key_target.write_text("test key")
        key_target.chmod(0o600)
        private_key.symlink_to(key_target)
        test_config.server = ServerConfig(
            **(
                test_config.server.model_dump()
                | {"ssl_cert": str(certificate), "ssl_key": str(private_key)}
            )
        )
        args = SimpleNamespace(
            config=str(temp_dir / "config.yaml"),
            host=None,
            port=None,
            reload=False,
            debug=False,
            no_docs=False,
            mode=None,
            api_key_file=None,
            storage_dir=None,
            db_path=None,
        )

        with pytest.raises(cli.ConfigurationError, match="symbolic link"):
            await cli.serve_command(args, test_config)


class TestGlobalArgsPosition:
    """Global flags must work before AND after the subcommand.

    The CLI's own --help epilog shows `serve -c config/myconfig.yaml`,
    so that order has to parse.
    """

    def test_config_flag_after_subcommand(self):
        parser = cli.create_parser()
        args = parser.parse_args(["serve", "-c", "myconf.yaml"])
        assert args.config == "myconf.yaml"

    def test_config_flag_before_subcommand(self):
        parser = cli.create_parser()
        args = parser.parse_args(["-c", "myconf.yaml", "serve"])
        assert args.config == "myconf.yaml"

    def test_config_default_when_not_given(self):
        parser = cli.create_parser()
        args = parser.parse_args(["serve"])
        assert args.config == "config/config.yaml"

    def test_log_level_after_subcommand(self):
        parser = cli.create_parser()
        args = parser.parse_args(["stats", "--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"


def test_cli_logging_never_joins_server_rotating_file(
    test_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_file_logging: list[bool] = []
    monkeypatch.setattr(
        cli,
        "setup_logging",
        lambda logging_config: observed_file_logging.append(
            logging_config.file.enabled
        ),
    )
    test_config.logging.file.enabled = True

    cli._setup_cli_logging(test_config)

    assert observed_file_logging == [False]
    assert test_config.logging.file.enabled is True
