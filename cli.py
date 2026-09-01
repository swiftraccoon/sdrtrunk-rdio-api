#!/usr/bin/env python3
"""Enhanced CLI for sdrtrunk-rdio-api with multiple commands and options."""

import argparse
import asyncio
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any, TextIO

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent))

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from sqlalchemy import desc, func, select

from src.api import create_app
from src.config import (
    Config,
    open_secure_regular_file,
    prepare_private_directory,
    secure_directory_handle,
    setup_logging,
    write_private_text_file,
)
from src.database.connection import DatabaseInUseError, DatabaseManager
from src.database.operations import (
    DatabaseOperations,
    ExpensiveQueryTimeout,
    expensive_query_deadline,
)
from src.exceptions import ConfigurationError
from src.filesystem_security import (
    durable_fsync,
    path_is_same_or_within,
    path_uses_dangerous_windows_namespace,
    paths_refer_to_same_entry,
    reject_insecure_extended_acl,
    rotating_log_state_paths,
    sqlite_state_paths,
)
from src.models.database_models import RadioCall, UploadLog
from src.security.text import sanitize_log_value
from src.utils.file_handler import FileHandler
from src.utils.maintenance import run_retention_cleanup
from src.utils.storage_quota import CapacityUnavailable, StorageCapacity


def positive_integer(value: str) -> int:
    """Argparse type for destructive counts that must be at least one."""
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be an integer") from e
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


MAX_STATS_RECENT_CALLS = 1_000
MAX_STATS_HOURS = 10 * 366 * 24
MAX_STATS_SYSTEMS = 1_000


def _database_manager(
    config: Config,
    *,
    exclusive_process_lock: bool = False,
    read_only: bool = False,
) -> DatabaseManager:
    """Create a manager with the configured state-filesystem safety floor."""
    return DatabaseManager(
        config.database,
        exclusive_process_lock=exclusive_process_lock,
        read_only=read_only,
        schema_minimum_free_bytes=(
            (
                config.file_handling.minimum_free_space_mb
                + config.file_handling.maintenance_state_reserve_mb
            )
            * 1024
            * 1024
        ),
        schema_minimum_free_inodes=config.file_handling.minimum_free_inodes,
    )


def _setup_cli_logging(config: Config) -> None:
    """Configure console logging without joining the server's rotating log."""
    cli_logging = config.logging.model_copy(deep=True)
    cli_logging.file.enabled = False
    setup_logging(cli_logging)


def _vacuum_has_headroom(db_manager: DatabaseManager, config: Config) -> bool:
    """Best-effort check for SQLite's database-sized VACUUM work file."""
    database_path = db_manager.database_path
    try:
        database_status = os.stat(database_path, follow_symlinks=False)
        if not stat.S_ISREG(database_status.st_mode):
            return False
        if hasattr(os, "statvfs"):
            filesystem = os.statvfs(database_path.parent)
            fragment_size = filesystem.f_frsize or filesystem.f_bsize
            available_bytes = filesystem.f_bavail * fragment_size
        else:  # pragma: no cover - Windows
            available_bytes = shutil.disk_usage(database_path.parent).free
    except (AttributeError, OSError):
        return False

    protected_bytes = (
        (
            config.file_handling.minimum_free_space_mb
            + config.file_handling.maintenance_state_reserve_mb
        )
        * 1024
        * 1024
    )
    return available_bytes - database_status.st_size >= protected_bytes


def stats_recent_count(value: str) -> int:
    """Bound the number of ORM rows materialized by ``stats``."""
    parsed = positive_integer(value)
    if parsed > MAX_STATS_RECENT_CALLS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_STATS_RECENT_CALLS:,}")
    return parsed


def stats_hour_count(value: str) -> int:
    """Bound date arithmetic and accidental full-history stats queries."""
    parsed = positive_integer(value)
    if parsed > MAX_STATS_HOURS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_STATS_HOURS:,}")
    return parsed


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def csv_safe_value(value: Any) -> Any:
    """Neutralize spreadsheet formulas in attacker-controlled text cells."""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(_CSV_FORMULA_PREFIXES) or stripped.startswith(
        _CSV_FORMULA_PREFIXES
    ):
        return f"'{value}"
    return value


@contextmanager
def private_text_output(path: str | Path) -> Iterator[TextIO]:
    """Atomically create a mode-0600 text output without following symlinks."""
    if path_uses_dangerous_windows_namespace(path):
        raise ValueError("Output path cannot use an ambiguous Windows filename")
    destination = Path(path)
    if not destination.name:
        raise ValueError("Output path must name a file")
    with secure_directory_handle(destination.parent, create=True) as (
        secure_parent,
        parent_descriptor,
    ):
        descriptor = -1
        temporary_name: str | None = None
        temporary_path: Path | None = None
        try:
            if parent_descriptor is None:  # pragma: no cover - Windows CI
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".rdio-export-", suffix=".tmp", dir=secure_parent
                )
                temporary_path = Path(temporary_name)
            else:
                for _ in range(100):
                    temporary_name = f".rdio-export-{secrets.token_hex(16)}.tmp"
                    try:
                        descriptor = os.open(
                            temporary_name,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=parent_descriptor,
                        )
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError("Unable to allocate a unique export file")

            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            reject_insecure_extended_acl(
                descriptor, description="Private CLI output file"
            )
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline=""
            ) as output_stream:
                descriptor = -1
                yield output_stream
                output_stream.flush()
                durable_fsync(output_stream.fileno())

            if parent_descriptor is None:  # pragma: no cover - Windows CI
                if temporary_path is None:
                    raise OSError("Temporary export path was not created")
                temporary_path.chmod(0o600)
                os.replace(temporary_path, secure_parent / destination.name)
                temporary_path = None
            else:
                if temporary_name is None:
                    raise OSError("Temporary export name was not created")
                os.replace(
                    temporary_name,
                    destination.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                temporary_name = None
                durable_fsync(parent_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_descriptor is None:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            elif temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser."""
    parser = argparse.ArgumentParser(
        description="sdrtrunk-rdio-api - RdioScanner ingestion server for SDRTrunk",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog="""
Examples:
  # Start server with default config
  %(prog)s serve

  # Start server with custom config and port
  %(prog)s serve -c config/myconfig.yaml --port 8080

  # Start server with debug logging
  %(prog)s serve --log-level DEBUG

  # Generate example configuration
  %(prog)s init

  # View recent uploads with verbose logging
  %(prog)s stats --last 10 --log-level INFO

  # Test database connection
  %(prog)s test-db
        """,
    )

    # Global arguments, accepted before the subcommand
    parser.add_argument(
        "-c",
        "--config",
        default="config/config.yaml",
        help="Path to configuration file (default: config/config.yaml)",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (overrides config file setting)",
    )

    # The same flags must also work AFTER the subcommand (`serve -c x.yaml`),
    # which is how the help examples show them. SUPPRESS keeps the
    # subcommand-level flags from overwriting a value given before the
    # subcommand when they are omitted.
    global_args = argparse.ArgumentParser(add_help=False)
    global_args.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="Path to configuration file (default: config/config.yaml)",
    )
    global_args.add_argument(
        "-l",
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=argparse.SUPPRESS,
        help="Set logging level (overrides config file setting)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve command
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the API server",
        parents=[global_args],
        allow_abbrev=False,
    )
    serve_parser.add_argument("--host", help="Override server host")
    serve_parser.add_argument("--port", type=int, help="Override server port")
    serve_parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload for development"
    )
    serve_parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    serve_parser.add_argument(
        "--no-docs", action="store_true", help="Disable API documentation"
    )
    serve_parser.add_argument(
        "--mode",
        choices=["log_only", "store", "process"],
        help="Override processing mode",
    )
    serve_parser.add_argument(
        "--api-key-file",
        help="Read an API key from a protected file and add it to configured keys",
    )
    serve_parser.add_argument(
        "--api-key-id",
        help="Stable nonsecret identifier required with --api-key-file",
    )
    serve_parser.add_argument(
        "--storage-dir", help="Override audio file storage directory"
    )
    serve_parser.add_argument("--db-path", help="Override database path")

    # Init command
    init_parser = subparsers.add_parser(
        "init", help="Generate example configuration file", parents=[global_args]
    )
    init_parser.add_argument(
        "-o",
        "--output",
        default="config/config.yaml",
        help="Output file path (default: config/config.yaml)",
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing file"
    )

    # Stats command
    stats_parser = subparsers.add_parser(
        "stats",
        help="View upload statistics and recent calls",
        parents=[global_args],
    )
    stats_parser.add_argument(
        "--last",
        type=stats_recent_count,
        default=20,
        help=f"Number of recent calls to show (1-{MAX_STATS_RECENT_CALLS})",
    )
    stats_parser.add_argument("--system", help="Filter by system ID")
    stats_parser.add_argument("--talkgroup", type=int, help="Filter by talkgroup")
    stats_parser.add_argument(
        "--hours",
        type=stats_hour_count,
        help=f"Show stats for last N hours (1-{MAX_STATS_HOURS})",
    )

    # Test DB command
    subparsers.add_parser(
        "test-db",
        help="Test database connection and show info",
        parents=[global_args],
    )

    # Clean command
    clean_parser = subparsers.add_parser(
        "clean", help="Clean old files and database records", parents=[global_args]
    )
    clean_parser.add_argument(
        "--days",
        type=positive_integer,
        default=30,
        help="Delete files older than N days (minimum: 1)",
    )
    clean_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be deleted"
    )
    clean_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for scripts/cron)",
    )

    # Export command
    export_parser = subparsers.add_parser(
        "export", help="Export calls data to CSV", parents=[global_args]
    )
    export_parser.add_argument(
        "-o", "--output", default="calls_export.csv", help="Output CSV file"
    )
    export_parser.add_argument(
        "--force", action="store_true", help="Replace an existing non-protected CSV"
    )
    export_parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    export_parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")

    return parser


def apply_serve_overrides(args: Any, config: Config) -> None:
    """Apply serve CLI flags on top of the loaded configuration."""
    try:
        if args.host:
            config.server.host = args.host
        if args.port is not None:
            config.server.port = args.port
        if args.debug:
            config.server.debug = True
        if args.no_docs:
            config.server.enable_docs = False
        if args.mode:
            config.processing.mode = args.mode
    except ValueError as e:
        raise ConfigurationError(f"Invalid serve override: {e}") from e
    api_key_file = getattr(args, "api_key_file", None)
    api_key_id = getattr(args, "api_key_id", None)
    if api_key_id and not api_key_file:
        raise ConfigurationError("--api-key-id requires --api-key-file")
    if api_key_file:
        if not api_key_id:
            raise ConfigurationError("--api-key-file requires --api-key-id")
        from src.config import APIKeyConfig

        key_path = Path(api_key_file)
        descriptor = -1
        try:
            descriptor = open_secure_regular_file(key_path)
            key_stat = os.fstat(descriptor)
            if not stat.S_ISREG(key_stat.st_mode):
                raise OSError("path is not a regular file")
            if os.name == "posix" and stat.S_IMODE(key_stat.st_mode) & 0o077:
                raise OSError(
                    "file permissions allow group or world access; "
                    "use mode 0600 or stricter"
                )
            if key_stat.st_size > 4096:
                raise OSError("file is unexpectedly large")
            with os.fdopen(descriptor, encoding="utf-8") as key_stream:
                descriptor = -1
                api_key = key_stream.read().strip()
        except (OSError, UnicodeError) as e:
            raise ConfigurationError(
                f"Could not read API key file {key_path}: {e}"
            ) from e
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        try:
            key_config = APIKeyConfig(
                key=api_key,
                identifier=api_key_id,
                description=f"API key from {key_path.name}",
            )
        except ValueError as e:
            raise ConfigurationError(f"Invalid API key file {key_path}: {e}") from e

        if any(entry.key == key_config.key for entry in config.security.api_keys):
            raise ConfigurationError(
                f"API key from {key_path} duplicates a configured key"
            )

        # Adds to any configured keys rather than replacing them
        config.security.api_keys = [
            *config.security.api_keys,
            key_config,
        ]
    if args.storage_dir:
        config.file_handling.storage.directory = args.storage_dir
    if args.db_path:
        config.database.path = args.db_path
    try:
        validated_config = Config.model_validate(config.model_dump())
    except ValueError as e:
        raise ConfigurationError(f"Invalid serve override: {e}") from e
    config_path = getattr(args, "config", None)
    if config_path:
        validated_config.validate_config_file_path(config_path)
    if api_key_file:
        validated_config.validate_protected_input_path(
            api_key_file, description="API key file"
        )


async def serve_command(args: Any, config: Config) -> None:
    """Run the server with given arguments."""
    apply_serve_overrides(args, config)

    # Create app
    app = create_app(config_path=args.config, override_config=config)

    # Configure Hypercorn
    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"{config.server.host}:{config.server.port}"]
    hypercorn_config.use_reloader = args.reload
    hypercorn_config.read_timeout = config.server.read_timeout_seconds
    hypercorn_config.include_server_header = False
    hypercorn_config.h2_max_concurrent_streams = 32
    hypercorn_config.h2_max_header_list_size = 16 * 1024
    # Rate limits, upload admission, quota accounting, and maintenance state
    # are deliberately process-local; multi-process serving would bypass them.
    hypercorn_config.workers = 1

    if config.server.ssl_cert and config.server.ssl_key:
        cert_path = Path(config.server.ssl_cert)
        key_path = Path(config.server.ssl_key)
        for tls_path, private in ((cert_path, False), (key_path, True)):
            descriptor = -1
            try:
                descriptor = open_secure_regular_file(tls_path)
                tls_status = os.fstat(descriptor)
                if (
                    private
                    and os.name == "posix"
                    and stat.S_IMODE(tls_status.st_mode) & 0o077
                ):
                    raise ConfigurationError(
                        f"TLS private key {tls_path} permits group or world access; "
                        "run chmod 600 on the file"
                    )
            except OSError as exc:
                label = "private key" if private else "certificate"
                raise ConfigurationError(
                    f"Could not securely open TLS {label} {tls_path}: {exc}"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        hypercorn_config.certfile = str(cert_path)
        hypercorn_config.keyfile = str(key_path)
        # Hypercorn accepts paths rather than already-open descriptors. The
        # no-follow ownership/ancestor checks above prevent unprivileged path
        # substitution; a malicious process with the same UID remains able to
        # replace either path between validation and Hypercorn's reopen.

    # Enable HTTP/2 (required for SDRTrunk)
    hypercorn_config.alpn_protocols = ["h2"]

    # Logging
    hypercorn_config.accesslog = "-" if config.server.debug else None
    hypercorn_config.errorlog = "-"

    print("\n>> Starting sdrtrunk-rdio-api Server")
    config_file = Path(args.config)
    if config_file.exists():
        print(f"  - Config: {config_file}")
    scheme = "https" if config.server.ssl_cert else "http"
    print(f"  - Address: {scheme}://{config.server.host}:{config.server.port}")
    print("  - HTTP/2: Enabled (required for SDRTrunk)")
    print(f"  - Processing Mode: {config.processing.mode}")
    print(f"  - Debug Mode: {config.server.debug}")
    if config.server.enable_docs:
        print(
            f"  - API Docs: {scheme}://{config.server.host}:{config.server.port}/docs"
        )
    print(f"  - Database: {config.database.path}")
    print(f"  - Audio Storage: {config.file_handling.storage.directory}")
    if config.security.api_keys:
        print(f"  - API Keys: {len(config.security.api_keys)} configured")
    else:
        print("  - API Keys: None (open access)")

    print("\nPress Ctrl+C to stop the server\n")

    # Run server - ignore mypy type mismatch as hypercorn's serve function has complex ASGI typing
    await serve(app, hypercorn_config)  # type: ignore[arg-type]


# Configuration template written by `init`. Kept in sync with
# config/config.example.yaml - update both together.
CONFIG_TEMPLATE = """\
# sdrtrunk-rdio-api Configuration
# Every option shown here is read by the server; defaults are sensible,
# so you usually only need to set an API key.

# API Server Configuration
server:
  # Localhost-only by default. Use a firewall/reverse proxy before widening this.
  host: "127.0.0.1"
  port: 8080
  cors_origins: []
  # Interactive API docs at /docs (disable in production if unwanted)
  enable_docs: true
  debug: false
  # Optional built-in TLS. Configure both or neither.
  # ssl_cert: "/path/to/fullchain.pem"
  # ssl_key: "/path/to/private-key.pem"
  read_timeout_seconds: 30

# Database Configuration
database:
  # SQLite database file path (directory is created automatically)
  path: "data/rdio_calls.db"
  # Write-Ahead Logging improves concurrent performance
  enable_wal: true

# API Security Configuration
security:
  # API keys for authentication. Each entry requires a unique, non-secret
  # identifier. Keys must be at least 16 characters; generate a random value.
  api_keys: []
  # Example:
  # api_keys:
  #   - key: "replace-with-a-long-randomly-generated-secret"
  #     identifier: "main-scanner"
  #     description: "Main SDRTrunk node"
  #     allowed_ips: []      # Empty means all IPs allowed
  #     allowed_systems: []  # Empty means all systems allowed

  # Reverse proxy IPs whose X-Forwarded-For header should be trusted.
  # Leave empty unless the server runs behind a proxy you control.
  trusted_proxies: []

  # Unsafe compatibility modes. Leave false unless another trusted layer
  # provides equivalent access control.
  allow_unauthenticated_uploads: false
  allow_unauthenticated_reads: false

  # Rate limiting by resolved client IP. Forwarded addresses are used only
  # when the direct peer is listed in trusted_proxies.
  # Defaults are sized so busy trunked systems never lose calls.
  rate_limit:
    enabled: true
    max_requests_per_minute: 600
    max_requests_per_hour: 10000
    max_requests_per_day: 100000

# File Handling Configuration
file_handling:
  # Accepted audio formats (SDRTrunk sends MP3)
  accepted_formats: [".mp3"]

  # File size limits
  max_file_size_mb: 100
  min_file_size_kb: 1
  # Keep this much usable space on upload/SQLite/log filesystems
  minimum_free_space_mb: 256
  # Keep this many free filesystem inodes for upload/state writes
  minimum_free_inodes: 1024
  # Extra headroom protected for bounded SQLite retention/WAL transactions
  maintenance_state_reserve_mb: 32

  # Temporary file storage
  temp_directory: "data/temp"

  # Audio file storage
  storage:
    # Storage strategy: "discard" (metadata only) or "filesystem"
    strategy: "filesystem"
    # For filesystem storage
    directory: "data/audio"
    # Hard cap for all regular files in the audio archive
    max_storage_size_mb: 102400
    # Independent cap for tiny-file archive growth
    max_storage_files: 5000000
    # Organize into YYYY/MM/DD/system subdirectories (UTC dates)
    organize_by_date: true
    # Delete calls (audio + metadata) older than this many days.
    # 0 = keep forever. Enforced by the server periodically and by
    # `sdrtrunk-rdio-api clean`.
    retention_days: 30
    # How often the server runs retention/temp cleanup (0 = disable)
    cleanup_interval_hours: 6

# Data Processing Configuration
processing:
  # "store" saves audio + metadata; "log_only" saves metadata only
  mode: "store"

# Logging Configuration
logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

  # Log file configuration
  file:
    enabled: true
    path: "logs/rdio_calls_api.log"
    max_size_mb: 20
    backup_count: 5

  # Console logging
  console:
    enabled: true
    colorize: true

# Monitoring Configuration
monitoring:
  # Health check endpoint
  health_check:
    enabled: true
    path: "/health"

  # Metrics endpoint
  metrics:
    enabled: true
    path: "/metrics"
"""


def init_command(args: Any) -> int:
    """Generate a configuration file."""
    output_path = Path(args.output)

    if output_path.exists() and not args.force:
        print(f"[ERROR] File {output_path} already exists. Use --force to overwrite.")
        return 1

    write_private_text_file(output_path, CONFIG_TEMPLATE)
    print(f"[SUCCESS] Generated configuration at {output_path}")
    print("\nNext steps:")
    print(f"1. Edit {output_path} and set an API key under security.api_keys")
    print("2. Start the server: sdrtrunk-rdio-api serve")
    return 0


def stats_command(args: Any, config: Config) -> int:
    """Show upload statistics."""
    # Setup logging
    _setup_cli_logging(config)

    # Materialize every bounded result and end the SQLite read transaction
    # before terminal output. A paused pipe must not pin the live server's WAL.
    try:
        with (
            _database_manager(config, read_only=True) as db_manager,
            db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            query = select(RadioCall).order_by(desc(RadioCall.call_timestamp))
            if args.system:
                query = query.filter(RadioCall.system_id == args.system)
            if args.talkgroup:
                query = query.filter(RadioCall.talkgroup_id == args.talkgroup)
            if args.hours is not None:
                cutoff = datetime.now(UTC) - timedelta(hours=args.hours)
                query = query.filter(RadioCall.call_timestamp >= cutoff)
            calls = session.execute(query.limit(args.last)).scalars().all()

            if calls:
                total_calls = int(session.query(func.count(RadioCall.id)).scalar() or 0)
                system_call_count = func.count(RadioCall.id).label("call_count")
                systems = (
                    session.query(RadioCall.system_id, system_call_count)
                    .group_by(RadioCall.system_id)
                    .order_by(desc(system_call_count), RadioCall.system_id)
                    .limit(MAX_STATS_SYSTEMS + 1)
                    .all()
                )
                top_tgs = (
                    session.query(
                        RadioCall.talkgroup_id,
                        RadioCall.talkgroup_label,
                        func.count(RadioCall.id).label("count"),
                    )
                    .group_by(RadioCall.talkgroup_id, RadioCall.talkgroup_label)
                    .order_by(
                        desc("count"),
                        RadioCall.talkgroup_id,
                        RadioCall.talkgroup_label,
                    )
                    .limit(10)
                    .all()
                )
            else:
                total_calls = 0
                systems = []
                top_tgs = []
    except ExpensiveQueryTimeout:
        print("[ERROR] Statistics query exceeded its bounded execution deadline")
        return 1

    if not calls:
        print("No calls found matching criteria.")
        return 0

    print(f"\n=== Recent Radio Calls (showing last {len(calls)}) ===")
    print("-" * 100)
    print(
        f"{'Time':^20} {'System':^10} {'TG':^8} {'Label':^20} "
        f"{'Freq':^12} {'Source':^10} {'Size':^10}"
    )
    print("-" * 100)

    for call in calls:
        time_str = call.call_timestamp.strftime("%Y-%m-%d %H:%M:%S")
        system_id = sanitize_log_value(call.system_id, 10)
        tg_label = sanitize_log_value(call.talkgroup_label or "", 20)
        freq_mhz = call.frequency / 1_000_000 if call.frequency else 0
        size_kb = call.audio_size_bytes / 1024 if call.audio_size_bytes else 0
        print(
            f"{time_str:^20} {system_id:^10} {call.talkgroup_id!s:^8} "
            f"{tg_label:^20} {freq_mhz:^12.4f} "
            f"{str(call.source_radio_id or ''):^10} {size_kb:^10.1f}"
        )

    print("\n=== Summary Statistics ===")
    print("-" * 50)
    print(f"Total Calls: {total_calls:,}")

    if systems:
        print("\nCalls by System:")
        for system_id, count in systems[:MAX_STATS_SYSTEMS]:
            print(f"  System {sanitize_log_value(system_id, 50)}: {count:,} calls")
        if len(systems) > MAX_STATS_SYSTEMS:
            print(f"  ... output capped at the top {MAX_STATS_SYSTEMS:,} systems")

    if top_tgs:
        print("\nTop 10 Talkgroups:")
        for tg, label, count in top_tgs:
            label_str = f"({sanitize_log_value(label, 255)})" if label else ""
            print(f"  TG {tg} {label_str}: {count:,} calls")

    return 0


def test_db_command(args: Any, config: Config) -> int:
    """Test database connection."""
    # Setup logging
    _setup_cli_logging(config)

    print(">> Testing database connection...")
    print(f"Database path: {sanitize_log_value(config.database.path, 512)}")

    try:
        with (
            _database_manager(config, read_only=True) as db_manager,
            db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            call_count = int(session.query(func.count(RadioCall.id)).scalar() or 0)
            upload_count = int(session.query(func.count(UploadLog.id)).scalar() or 0)

            size_mb = db_manager.database_path.stat().st_size / (1024 * 1024)

        # End the read transaction before output: a paused terminal or pipe
        # must not retain a snapshot and prevent a live WAL from checkpointing.
        print("\n[SUCCESS] Database connection successful!")
        print(f"  - Radio Calls: {call_count:,}")
        print(f"  - Upload Logs: {upload_count:,}")
        print(f"  - Database Size: {size_mb:.2f} MB")

        print("\n=== Database Tables ===")
        print("  - radio_calls")
        print("  - upload_logs")

        return 0

    except ExpensiveQueryTimeout:
        print("\n[ERROR] Database check exceeded its bounded execution budget")
        return 1
    except Exception as e:
        print("\n[ERROR] Database connection failed!")
        print(f"Error: {sanitize_log_value(str(e), 512)}")
        return 1


def _cleanup_backlog_counts(
    db_ops: DatabaseOperations, cutoff_date: datetime
) -> tuple[int, int, int, int]:
    """Return the bounded cleanup progress tuple used before and after cycles."""
    counts = db_ops.get_cleanup_backlog_counts(cutoff_date)
    return (
        counts.expired_calls,
        counts.expired_upload_logs,
        counts.due_file_deletions,
        counts.failed_file_deletions,
    )


def _run_locked_cleanup(args: Any, config: Config, cutoff_date: datetime) -> int:
    """Run mutations while excluding the live server and other offline jobs."""
    with _database_manager(config, exclusive_process_lock=True) as db_manager:
        db_ops = DatabaseOperations(db_manager)
        file_handler = FileHandler(
            storage_directory=config.file_handling.storage.directory,
            temp_directory=config.file_handling.temp_directory,
            organize_by_date=config.file_handling.storage.organize_by_date,
            accepted_formats=config.file_handling.accepted_formats,
            max_file_size_mb=config.file_handling.max_file_size_mb,
            min_file_size_kb=config.file_handling.min_file_size_kb,
        )
        storage_capacity: StorageCapacity | None = None
        try:
            state_directories = [db_manager.database_path.parent]
            if config.logging.file.enabled:
                state_directories.append(
                    prepare_private_directory(
                        Path(config.logging.file.path).expanduser().parent
                    )
                )
            storage_capacity = StorageCapacity(
                storage_directory=file_handler.storage_dir,
                temp_directory=file_handler.temp_dir,
                max_file_bytes=file_handler.max_file_size_bytes,
                max_storage_bytes=(
                    config.file_handling.storage.max_storage_size_mb * 1024 * 1024
                ),
                max_storage_files=config.file_handling.storage.max_storage_files,
                minimum_free_bytes=(
                    config.file_handling.minimum_free_space_mb * 1024 * 1024
                ),
                minimum_free_inodes=config.file_handling.minimum_free_inodes,
                maintenance_state_bytes=(
                    config.file_handling.maintenance_state_reserve_mb * 1024 * 1024
                ),
                persistent_archive_enabled=(
                    config.file_handling.storage.strategy == "filesystem"
                    and config.processing.mode != "log_only"
                ),
                destination_inode_reservation=(
                    5 if config.file_handling.storage.organize_by_date else 2
                ),
                state_directories=state_directories,
                # Cleanup only needs state-write admission. A complete archive
                # scan would make an offline retention command unbounded.
                scan_on_initialize=False,
            )
            file_handler.attach_storage_capacity(storage_capacity)
            summary = {
                "deleted_calls": 0,
                "deleted_upload_logs": 0,
                "deleted_files": 0,
                "freed_bytes": 0,
            }
            previous_counts = _cleanup_backlog_counts(db_ops, cutoff_date)
            if previous_counts != (0, 0, 0, 0):
                for _ in range(10_000):
                    cycle = run_retention_cleanup(
                        db_ops, file_handler, args.days, vacuum=False
                    )
                    for key in summary:
                        summary[key] += cycle[key]

                    remaining_counts = _cleanup_backlog_counts(db_ops, cutoff_date)
                    if remaining_counts == (0, 0, 0, 0):
                        break
                    if remaining_counts == previous_counts:
                        print(
                            "[ERROR] Cleanup is incomplete; pending filesystem "
                            "work will be retried after its backoff interval."
                        )
                        return 1
                    previous_counts = remaining_counts
                else:
                    print(
                        "[ERROR] Cleanup stopped after reaching its safety cycle limit."
                    )
                    return 1

            if summary["deleted_calls"] or summary["deleted_upload_logs"]:
                if not db_manager.checkpoint(truncate=True):
                    print("[ERROR] Cleanup WAL is busy; retry later.")
                    return 1
                if _vacuum_has_headroom(db_manager, config):
                    db_manager.vacuum()
                    if not db_manager.checkpoint(truncate=True):
                        print(
                            "[ERROR] Post-VACUUM WAL checkpoint is busy; retry later."
                        )
                        return 1
                else:
                    # Row/file cleanup is already durable. Skipping optional
                    # compaction is safer than filling the state filesystem.
                    print(
                        "[WARNING] Skipped optional VACUUM: insufficient verified "
                        "database-sized filesystem headroom."
                    )

            freed_mb = summary["freed_bytes"] / (1024 * 1024)
            print(
                f"[SUCCESS] Deleted {summary['deleted_calls']:,} calls, "
                f"{summary['deleted_upload_logs']:,} upload logs, "
                f"{summary['deleted_files']:,} files ({freed_mb:.2f} MB freed)"
            )
            return 0
        except ExpensiveQueryTimeout:
            print(
                "[ERROR] Cleanup progress query exceeded its bounded execution "
                "budget; cleanup stopped safely."
            )
            return 1
        except CapacityUnavailable as exc:
            print(f"[ERROR] Cleanup cannot safely reserve filesystem capacity: {exc}")
            return 1
        finally:
            if storage_capacity is not None:
                storage_capacity.close()
            file_handler.close()


def clean_command(args: Any, config: Config) -> int:
    """Clean old calls: database records, upload logs, and audio files."""
    if args.days < 1:
        print("[ERROR] --days must be at least 1")
        return 1

    _setup_cli_logging(config)
    cutoff_date = datetime.now(UTC) - timedelta(days=args.days)
    print(f">> Cleaning calls, logs, and audio older than {cutoff_date.date()} (UTC)")

    # The read-only preview does not take the service lock, so an unattended
    # confirmation prompt cannot block server startup. Counts are recomputed
    # after the exclusive lock is acquired before any mutation occurs.
    try:
        with _database_manager(config, read_only=True) as preview_manager:
            preview_ops = DatabaseOperations(preview_manager)
            preview_counts = preview_ops.get_cleanup_backlog_counts(
                cutoff_date, include_audio_paths=True
            )
    except ExpensiveQueryTimeout:
        print(
            "[ERROR] Cleanup preview exceeded its bounded execution budget; "
            "nothing was deleted."
        )
        return 1

    old_calls = preview_counts.expired_calls
    old_logs = preview_counts.expired_upload_logs
    pending_file_count = preview_counts.due_file_deletions
    failed_file_count = preview_counts.failed_file_deletions
    audio_path_count = preview_counts.expired_audio_paths or 0

    print(f"\nCalls to delete: {old_calls:,}")
    print(f"Upload log entries to delete: {old_logs:,}")
    print(f"Audio paths referenced by expired calls: {audio_path_count:,}")
    print(f"Due queued file deletions to process: {pending_file_count:,}")
    print(f"Deferred failed file deletions: {failed_file_count:,}")

    if args.dry_run:
        print("\n[DRY RUN] Nothing was deleted")
        return 0
    if (old_calls, old_logs, pending_file_count, failed_file_count) == (0, 0, 0, 0):
        print("\nNothing to clean.")
        return 0
    if not getattr(args, "yes", False):
        confirm = input("\nProceed with deletion? (y/N): ")
        if confirm.lower() != "y":
            print("[CANCELLED] Operation cancelled")
            return 0

    return _run_locked_cleanup(args, config, cutoff_date)


def _absolute_cli_path(path: str | Path) -> Path:
    """Return a normalized absolute path without requiring it to exist."""
    return Path(os.path.abspath(Path(path).expanduser()))


def _same_or_within(candidate: Path, root: Path) -> bool:
    return path_is_same_or_within(candidate, root)


def _validate_export_destination(
    args: Any, config: Config
) -> tuple[Path | None, str | None]:
    """Reject destructive or ambiguous CSV output destinations."""
    try:
        if path_uses_dangerous_windows_namespace(args.output):
            return None, "Export output uses a reserved or ambiguous Windows filename"
        destination = _absolute_cli_path(args.output)
        if not destination.name:
            return None, "Export output must name a file"

        if any(
            _same_or_within(destination, _absolute_cli_path(configured_root))
            for configured_root in (
                config.file_handling.storage.directory,
                config.file_handling.temp_directory,
            )
        ):
            return None, "Export output must be outside audio storage and temp roots"

        protected_paths = set(sqlite_state_paths(config.database.path))
        config_path = getattr(args, "config", None)
        if config_path:
            protected_paths.add(_absolute_cli_path(config_path))
        if config.logging.file.enabled:
            protected_paths.update(
                rotating_log_state_paths(
                    config.logging.file.path, config.logging.file.backup_count
                )
            )
        if config.server.ssl_cert:
            protected_paths.add(_absolute_cli_path(config.server.ssl_cert))
        if config.server.ssl_key:
            protected_paths.add(_absolute_cli_path(config.server.ssl_key))

        for protected in protected_paths:
            if paths_refer_to_same_entry(destination, protected):
                return None, "Export output conflicts with protected application state"

        try:
            destination_status = destination.lstat()
        except FileNotFoundError:
            destination_status = None
        if destination_status is not None:
            if stat.S_ISLNK(destination_status.st_mode) or not stat.S_ISREG(
                destination_status.st_mode
            ):
                return None, "Existing export output must be a regular non-symlink file"
            if not getattr(args, "force", False):
                return None, "Export output already exists; use --force to replace it"
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, "Export output path could not be validated safely"

    return destination, None


def export_command(args: Any, config: Config) -> int:
    """Export calls to CSV."""
    import csv

    output_path, output_error = _validate_export_destination(args, config)
    if output_error is not None or output_path is None:
        print(f"[ERROR] {output_error or 'Invalid export output'}")
        return 1

    # Setup logging
    _setup_cli_logging(config)

    with (
        _database_manager(config, exclusive_process_lock=True) as db_manager,
        db_manager.get_session() as session,
    ):
        query = select(RadioCall).order_by(RadioCall.call_timestamp)

        # Apply date filters (UTC dates; end date is inclusive)
        if args.start_date:
            start = datetime.strptime(args.start_date, "%Y-%m-%d")
            query = query.filter(RadioCall.call_timestamp >= start)
        if args.end_date:
            end = datetime.strptime(args.end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(RadioCall.call_timestamp < end)

        calls = session.execute(
            query.execution_options(yield_per=500, stream_results=True)
        ).scalars()
        try:
            first_call = next(calls)
        except StopIteration:
            print("No calls found to export.")
            return 0

        # Write CSV
        with private_text_output(output_path) as csvfile:
            fieldnames = [
                "timestamp",
                "system_id",
                "system_label",
                "talkgroup_id",
                "talkgroup_label",
                "talkgroup_group",
                "source_radio_id",
                "frequency",
                "audio_filename",
                "audio_size_bytes",
                "upload_timestamp",
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            exported_count = 0
            for call in chain((first_call,), calls):
                row = {
                    "timestamp": call.call_timestamp.isoformat(),
                    "system_id": call.system_id,
                    "system_label": call.system_label,
                    "talkgroup_id": call.talkgroup_id,
                    "talkgroup_label": call.talkgroup_label,
                    "talkgroup_group": call.talkgroup_group,
                    "source_radio_id": call.source_radio_id,
                    "frequency": call.frequency,
                    "audio_filename": call.audio_filename,
                    "audio_size_bytes": call.audio_size_bytes,
                    "upload_timestamp": (
                        call.upload_timestamp.isoformat()
                        if call.upload_timestamp
                        else None
                    ),
                }
                writer.writerow(
                    {field: csv_safe_value(value) for field, value in row.items()}
                )
                exported_count += 1

        print(f"[SUCCESS] Exported {exported_count} calls to {args.output}")
        return 0


async def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Show help if no command specified
    if not args.command:
        parser.print_help()
        return 1

    # init creates the config file, so don't try to load it first
    if args.command == "init":
        return init_command(args)

    # Every stateful command uses the operator-selected database/log/storage
    # paths. A typo must not silently create or export an unrelated default
    # database; only the library-level loader explicitly permits defaults.
    try:
        config = Config.load_from_file(args.config, require_exists=True)
    except ConfigurationError as e:
        print(f"[ERROR] {e}")
        return 1

    # Override with log level argument
    if args.log_level:
        config.logging.level = args.log_level

    # Execute command
    try:
        if args.command == "serve":
            await serve_command(args, config)
            return 0
        elif args.command == "stats":
            return stats_command(args, config)
        elif args.command == "test-db":
            return test_db_command(args, config)
        elif args.command == "clean":
            return clean_command(args, config)
        elif args.command == "export":
            return export_command(args, config)
        else:
            parser.print_help()
            return 1
    except (ConfigurationError, DatabaseInUseError) as e:
        print(f"[ERROR] {e}")
        return 1

    return 0


def main_sync() -> None:
    """Synchronous entry point for console_scripts."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    main_sync()
