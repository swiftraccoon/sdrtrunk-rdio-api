#!/usr/bin/env python3
"""Enhanced CLI for RdioCallsAPI with multiple commands and options."""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent))

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from sqlalchemy import desc, func, select

from src.api import create_app
from src.config import Config, setup_logging
from src.database.connection import DatabaseManager
from src.models.database_models import RadioCall, UploadLog


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser."""
    parser = argparse.ArgumentParser(
        description="RdioCallsAPI - RdioScanner ingestion server for SDRTrunk",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    # Global arguments
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

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Start the API server")
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
        "--api-key", help="Add a simple API key for authentication"
    )
    serve_parser.add_argument(
        "--storage-dir", help="Override audio file storage directory"
    )
    serve_parser.add_argument("--db-path", help="Override database path")

    # Init command
    init_parser = subparsers.add_parser(
        "init", help="Generate example configuration file"
    )
    init_parser.add_argument(
        "-o",
        "--output",
        default="config/config.example.yaml",
        help="Output file path (default: config/config.example.yaml)",
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing file"
    )

    # Stats command
    stats_parser = subparsers.add_parser(
        "stats", help="View upload statistics and recent calls"
    )
    stats_parser.add_argument(
        "--last", type=int, default=20, help="Number of recent calls to show"
    )
    stats_parser.add_argument("--system", help="Filter by system ID")
    stats_parser.add_argument("--talkgroup", type=int, help="Filter by talkgroup")
    stats_parser.add_argument("--hours", type=int, help="Show stats for last N hours")

    # Test DB command
    subparsers.add_parser("test-db", help="Test database connection and show info")

    # Clean command
    clean_parser = subparsers.add_parser(
        "clean", help="Clean old files and database records"
    )
    clean_parser.add_argument(
        "--days", type=int, default=30, help="Delete files older than N days"
    )
    clean_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be deleted"
    )

    # Export command
    export_parser = subparsers.add_parser("export", help="Export calls data to CSV")
    export_parser.add_argument(
        "-o", "--output", default="calls_export.csv", help="Output CSV file"
    )
    export_parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    export_parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")

    return parser


async def serve_command(args: Any, config: Config) -> None:
    """Run the server with given arguments."""
    # Override config with CLI arguments
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.debug:
        config.server.debug = True
    if args.no_docs:
        config.server.enable_docs = False
    if args.mode:
        config.processing.mode = args.mode
    if args.api_key:
        from src.config import APIKeyConfig

        config.security.api_keys = [
            APIKeyConfig(
                key=args.api_key,
                description="CLI-provided API key",
            )
        ]
    if args.storage_dir:
        config.file_handling.storage.directory = args.storage_dir
    if args.db_path:
        config.database.path = args.db_path

    # Create app
    app = create_app(config_path=args.config, override_config=config)

    # Configure Hypercorn
    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"{config.server.host}:{config.server.port}"]
    hypercorn_config.use_reloader = args.reload

    # Enable HTTP/2 (required for SDRTrunk)
    hypercorn_config.alpn_protocols = ["h2"]

    # Logging
    hypercorn_config.accesslog = "-" if config.server.debug else None
    hypercorn_config.errorlog = "-"

    print("\n>> Starting RdioCallsAPI Server")
    print(f"  - Address: http://{config.server.host}:{config.server.port}")
    print("  - HTTP/2: Enabled (required for SDRTrunk)")
    print(f"  - Processing Mode: {config.processing.mode}")
    print(f"  - Debug Mode: {config.server.debug}")
    if config.server.enable_docs:
        print(f"  - API Docs: http://{config.server.host}:{config.server.port}/docs")
    print(f"  - Database: {config.database.path}")
    print(f"  - Audio Storage: {config.file_handling.storage.directory}")
    if config.security.api_keys:
        print(f"  - API Keys: {len(config.security.api_keys)} configured")
    else:
        print("  - API Keys: None (open access)")

    print("\nPress Ctrl+C to stop the server\n")

    # Run server - ignore mypy type mismatch as hypercorn's serve function has complex ASGI typing
    await serve(app, hypercorn_config)  # type: ignore[arg-type]


def init_command(args: Any) -> int:
    """Generate example configuration file."""
    output_path = Path(args.output)

    if output_path.exists() and not args.force:
        print(f"[ERROR] File {output_path} already exists. Use --force to overwrite.")
        return 1

    example_config = """# RdioCallsAPI Configuration
# This is an example configuration file for the RdioScanner API ingestion server

# API Server Configuration
server:
  host: "0.0.0.0"
  port: 8000
  cors_origins: ["*"]
  enable_docs: true
  debug: false

# Database Configuration
database:
  # SQLite database file path
  path: "data/rdio_calls.db"
  # Enable WAL mode for better concurrent performance
  enable_wal: true
  # Database connection pool settings
  pool_size: 5
  max_overflow: 10

# API Security Configuration
security:
  # API keys for authentication (leave empty for no authentication)
  api_keys: []
  # Example with API key:
  # api_keys:
  #   - key: "your-secret-key-here"
  #     description: "Main SDRTrunk node"
  #     allowed_ips: []  # Empty means all IPs allowed
  #     allowed_systems: []  # Empty means all systems allowed

  # Rate limiting
  rate_limit:
    enabled: true
    max_requests_per_minute: 60
    max_requests_per_hour: 1000
    max_requests_per_day: 10000

# File Handling Configuration
file_handling:
  # Accepted audio formats (SDRTrunk sends MP3)
  accepted_formats: [".mp3"]

  # File size limits
  max_file_size_mb: 100
  min_file_size_kb: 1

  # Temporary file storage
  temp_directory: "data/temp"

  # Audio file storage
  storage:
    # Storage strategy: "discard", "filesystem", "database"
    strategy: "filesystem"
    # For filesystem storage
    directory: "data/audio"
    # Organize files by date
    organize_by_date: true
    # File retention days (0 = keep forever)
    retention_days: 30

# Data Processing Configuration
processing:
  # What to do with incoming calls
  # Options: "log_only", "store", "process"
  mode: "store"

  # Fields to extract and store
  store_fields:
    - timestamp
    - system
    - frequency
    - talkgroup
    - source
    - systemLabel
    - talkgroupLabel
    - talkgroupGroup
    - talkerAlias
    - audio_filename
    - audio_size
    - upload_ip
    - upload_timestamp

# Logging Configuration
logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

  # Log file configuration
  file:
    enabled: true
    path: "logs/rdio_calls_api.log"
    max_size_mb: 100
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

  # Statistics tracking
  statistics:
    enabled: true
    # Track upload sources
    track_sources: true
    # Track system statistics
    track_systems: true
    # Track talkgroup statistics
    track_talkgroups: true
"""

    output_path.write_text(example_config)
    print(f"[SUCCESS] Generated example configuration at {output_path}")
    print("\nNext steps:")
    print(f"1. Copy to config.yaml: cp {output_path} config/config.yaml")
    print("2. Edit config/config.yaml to match your setup")
    print(f"3. Start the server: {sys.argv[0]} serve")
    return 0


def stats_command(args: Any, config: Config) -> int:
    """Show upload statistics."""
    # Setup logging
    setup_logging(config.logging)

    # Create database manager
    db_manager = DatabaseManager(config.database)
    db_manager._init_database()

    with db_manager.get_session() as session:
        # Build query
        query = select(RadioCall).order_by(desc(RadioCall.call_timestamp))

        # Apply filters
        if args.system:
            query = query.filter(RadioCall.system_id == args.system)
        if args.talkgroup:
            query = query.filter(RadioCall.talkgroup_id == args.talkgroup)
        if args.hours:
            cutoff = datetime.utcnow() - timedelta(hours=args.hours)
            query = query.filter(RadioCall.call_timestamp >= cutoff)

        # Limit results
        query = query.limit(args.last)

        # Execute query
        calls = session.execute(query).scalars().all()

        if not calls:
            print("No calls found matching criteria.")
            return 0

        # Print header
        print(f"\n=== Recent Radio Calls (showing last {len(calls)}) ===")
        print("-" * 100)
        print(
            f"{'Time':^20} {'System':^10} {'TG':^8} {'Label':^20} {'Freq':^12} {'Source':^10} {'Size':^10}"
        )
        print("-" * 100)

        # Print calls
        for call in calls:
            time_str = call.call_timestamp.strftime("%Y-%m-%d %H:%M:%S")
            tg_label = (call.talkgroup_label or "")[:20]
            freq_mhz = call.frequency / 1_000_000 if call.frequency else 0
            size_kb = call.audio_size_bytes / 1024 if call.audio_size_bytes else 0

            print(
                f"{time_str:^20} {call.system_id:^10} {call.talkgroup_id:^8} "
                f"{tg_label:^20} {freq_mhz:^12.4f} {call.source_radio_id or '':^10} "
                f"{size_kb:^10.1f}"
            )

        # Show summary statistics
        print("\n=== Summary Statistics ===")
        print("-" * 50)

        # Total calls
        total_calls = session.query(func.count(RadioCall.id)).scalar()
        print(f"Total Calls: {total_calls:,}")

        # Calls by system
        systems = (
            session.query(RadioCall.system_id, func.count(RadioCall.id))
            .group_by(RadioCall.system_id)
            .all()
        )

        if systems:
            print("\nCalls by System:")
            for system_id, count in systems:
                print(f"  System {system_id}: {count:,} calls")

        # Top talkgroups
        top_tgs = (
            session.query(
                RadioCall.talkgroup_id,
                RadioCall.talkgroup_label,
                func.count(RadioCall.id).label("count"),
            )
            .group_by(RadioCall.talkgroup_id, RadioCall.talkgroup_label)
            .order_by(desc("count"))
            .limit(10)
            .all()
        )

        if top_tgs:
            print("\nTop 10 Talkgroups:")
            for tg, label, count in top_tgs:
                label_str = f"({label})" if label else ""
                print(f"  TG {tg} {label_str}: {count:,} calls")

    return 0


def test_db_command(args: Any, config: Config) -> int:
    """Test database connection."""
    # Setup logging
    setup_logging(config.logging)

    print(">> Testing database connection...")
    print(f"Database path: {config.database.path}")

    try:
        # Create database manager
        db_manager = DatabaseManager(config.database)
        db_manager._init_database()

        with db_manager.get_session() as session:
            # Test query
            call_count = session.query(func.count(RadioCall.id)).scalar()
            upload_count = session.query(func.count(UploadLog.id)).scalar()

            # Get database file size
            db_path = Path(config.database.path)
            if db_path.exists():
                size_mb = db_path.stat().st_size / (1024 * 1024)
            else:
                size_mb = 0

            print("\n[SUCCESS] Database connection successful!")
            print(f"  - Radio Calls: {call_count:,}")
            print(f"  - Upload Logs: {upload_count:,}")
            print(f"  - Database Size: {size_mb:.2f} MB")

            # Show table info
            print("\n=== Database Tables ===")
            print("  - radio_calls")
            print("  - upload_logs")
            print("  - alembic_version")

            return 0

    except Exception as e:
        print("\n[ERROR] Database connection failed!")
        print(f"Error: {e}")
        return 1


def clean_command(args: Any, config: Config) -> int:
    """Clean old files and database records."""
    # Setup logging
    setup_logging(config.logging)

    cutoff_date = datetime.utcnow() - timedelta(days=args.days)
    print(f">> Cleaning files and records older than {cutoff_date.date()}")

    if args.dry_run:
        print("[DRY RUN] No files will be deleted")

    # Count files to delete
    audio_dir = Path(config.file_handling.storage.directory)
    files_to_delete = []
    total_size = 0

    if audio_dir.exists():
        for file in audio_dir.rglob("*.mp3"):
            if datetime.fromtimestamp(file.stat().st_mtime) < cutoff_date:
                files_to_delete.append(file)
                total_size += file.stat().st_size

    print(f"\nFiles to delete: {len(files_to_delete)}")
    print(f"Total size: {total_size / (1024*1024):.2f} MB")

    if not args.dry_run and files_to_delete:
        confirm = input("\nProceed with deletion? (y/N): ")
        if confirm.lower() == "y":
            for file in files_to_delete:
                file.unlink()
            print(f"[SUCCESS] Deleted {len(files_to_delete)} files")
        else:
            print("[CANCELLED] Operation cancelled")

    # Clean database records
    db_manager = DatabaseManager(config.database)
    db_manager._init_database()

    with db_manager.get_session() as session:
        # Count records to delete
        old_calls = (
            session.query(func.count(RadioCall.id))
            .filter(RadioCall.call_timestamp < cutoff_date)
            .scalar()
        )

        print(f"\nDatabase records to delete: {old_calls:,}")

        if not args.dry_run and old_calls > 0:
            if confirm.lower() == "y":
                session.query(RadioCall).filter(
                    RadioCall.call_timestamp < cutoff_date
                ).delete()
                session.commit()
                print(f"[SUCCESS] Deleted {old_calls:,} database records")

    return 0


def export_command(args: Any, config: Config) -> int:
    """Export calls to CSV."""
    import csv

    # Setup logging
    setup_logging(config.logging)

    db_manager = DatabaseManager(config.database)
    db_manager._init_database()

    with db_manager.get_session() as session:
        query = select(RadioCall).order_by(RadioCall.call_timestamp)

        # Apply date filters
        if args.start_date:
            start = datetime.strptime(args.start_date, "%Y-%m-%d")
            query = query.filter(RadioCall.call_timestamp >= start)
        if args.end_date:
            end = datetime.strptime(args.end_date, "%Y-%m-%d")
            query = query.filter(RadioCall.call_timestamp <= end)

        calls = session.execute(query).scalars().all()

        if not calls:
            print("No calls found to export.")
            return 0

        # Write CSV
        with open(args.output, "w", newline="") as csvfile:
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

            for call in calls:
                writer.writerow(
                    {
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
                )

        print(f"[SUCCESS] Exported {len(calls)} calls to {args.output}")
        return 0


async def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Show help if no command specified
    if not args.command:
        parser.print_help()
        return 1

    # Load config
    config = Config.load_from_file(args.config)

    # Override with log level argument
    if args.log_level:
        config.logging.level = args.log_level

    # Execute command
    if args.command == "serve":
        await serve_command(args, config)
        return 0
    elif args.command == "init":
        return init_command(args)
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

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
