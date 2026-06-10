"""Regression tests for CLI fixes (clean, export, init defaults)."""

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import cli  # noqa: E402  (project-root module)
from src.config import Config
from src.database.connection import DatabaseManager
from src.database.operations import DatabaseOperations
from src.models.api_models import RdioScannerUpload
from src.models.database_models import RadioCall


def _save_old_call(
    db_ops: DatabaseOperations, days_ago: int, audio_file_path: str | None = None
) -> int:
    ts = int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())
    return db_ops.save_radio_call(
        RdioScannerUpload(key="", system="1", dateTime=ts),
        audio_file_path=audio_file_path,
        upload_ip="127.0.0.1",
    )


class TestCleanCommand:
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


class TestExportCommand:
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


class TestInitDefaults:
    def test_init_defaults_to_real_config_path(self):
        """`init` must generate config/config.yaml, the path the server reads."""
        parser = cli.create_parser()
        args = parser.parse_args(["init"])
        assert args.output == "config/config.yaml"


class TestServeApiKeyOverride:
    def test_api_key_flag_appends_to_existing_keys(self, test_config: Config):
        """--api-key is documented as 'Add'; it must not replace configured keys."""
        from src.config import APIKeyConfig

        test_config.security.api_keys = [
            APIKeyConfig(key="existing-key", description="configured")
        ]
        args = SimpleNamespace(
            host=None,
            port=None,
            debug=False,
            no_docs=False,
            mode=None,
            api_key="added-key",
            storage_dir=None,
            db_path=None,
        )
        cli.apply_serve_overrides(args, test_config)

        keys = [k.key for k in test_config.security.api_keys]
        assert "existing-key" in keys
        assert "added-key" in keys


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
