"""Database connection management for SQLite."""

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from ..config import DatabaseConfig
from ..models.database_models import Base

logger = logging.getLogger(__name__)

# Global lock for database initialization
_db_init_lock = threading.Lock()


class DatabaseManager:
    """Manages SQLite database connections and sessions."""

    def __init__(
        self,
        database_path: str | DatabaseConfig,
        enable_wal: bool = True,
        echo: bool = False,
    ):
        """Initialize database manager.

        Args:
            database_path: Path to SQLite database file or DatabaseConfig object
            enable_wal: Enable Write-Ahead Logging for better concurrency
            echo: Enable SQL query logging
        """
        # Handle both string path and config object
        if isinstance(database_path, str):
            # It's a string path
            self.database_path = Path(database_path)
            self.enable_wal = enable_wal
            self.echo = echo
        else:
            # It's a DatabaseConfig object
            self.database_path = Path(database_path.path)
            self.enable_wal = database_path.enable_wal
            self.echo = echo

        # Ensure database directory exists
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        # Create engine with connection pooling
        self.engine = self._create_engine()

        # Create thread-safe session factory using scoped_session
        session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(session_factory)

        # Initialize database
        self._init_database()

        logger.info(f"Database initialized at: {self.database_path}")

    def _create_engine(self) -> Engine:
        """Create SQLAlchemy engine with SQLite optimizations."""
        # SQLite connection string
        connection_string = f"sqlite:///{self.database_path}"

        # Create engine with connection pooling optimized for concurrent access
        # Use NullPool for better thread safety with SQLite
        engine = create_engine(
            connection_string,
            echo=self.echo,
            poolclass=NullPool,  # Better for SQLite with threads
            connect_args={
                "check_same_thread": False,  # Allow multiple threads
                "timeout": 30,  # Connection timeout in seconds
                "isolation_level": None,  # Use autocommit mode for better concurrency
            },
            pool_pre_ping=True,  # Verify connections before using
        )

        # Configure SQLite for better performance
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()

            # Enable Write-Ahead Logging for better concurrency
            if self.enable_wal:
                cursor.execute("PRAGMA journal_mode=WAL")

            # Performance optimizations
            cursor.execute("PRAGMA synchronous=NORMAL")  # Faster writes
            cursor.execute("PRAGMA cache_size=10000")  # Larger cache
            cursor.execute("PRAGMA temp_store=MEMORY")  # Use memory for temp tables
            cursor.execute("PRAGMA mmap_size=30000000000")  # Memory-mapped I/O

            cursor.close()

        return engine

    def _init_database(self) -> None:
        """Initialize database schema (thread-safe)."""
        global _db_init_lock

        # Use a global lock to ensure only one thread initializes the database
        with _db_init_lock:
            try:
                # Create all tables if they don't exist
                # Using create_all is idempotent - it won't recreate existing tables
                Base.metadata.create_all(self.engine, checkfirst=True)
                logger.info("Database schema created/verified")
            except Exception as e:
                logger.error(f"Failed to initialize database: {e}")
                raise

    @contextmanager
    def get_session(self) -> Generator[Session]:
        """Get a database session with automatic cleanup.

        Thread-safe: Each thread gets its own session from the scoped_session.

        Usage:
            with db_manager.get_session() as session:
                # Use session here
                session.add(record)
                session.commit()
        """
        # Get thread-local session from scoped_session
        session = self.Session()
        try:
            yield session
            # Auto-commit if there are pending changes and no explicit commit was called
            if session.new or session.dirty or session.deleted:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            # Close this thread's session
            session.close()
            # Remove the session from the scoped_session registry
            # This is critical for thread safety - ensures each thread gets a fresh session
            self.Session.remove()

    def close(self) -> None:
        """Close database connections."""
        # Remove scoped session registry
        self.Session.remove()
        # Dispose of the engine
        self.engine.dispose()
        logger.info("Database connections closed")

    def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        stats: dict[str, Any] = {}

        with self.get_session() as session:
            # Get database file size
            if self.database_path.exists():
                stats["size_mb"] = self.database_path.stat().st_size / (1024 * 1024)
            else:
                stats["size_mb"] = 0

            # Get table row counts
            from ..models.database_models import (
                RadioCall,
                SystemStats,
                UploadLog,
            )

            tables: dict[str, int] = {
                "radio_calls": int(session.query(RadioCall).count()),
                "upload_logs": int(session.query(UploadLog).count()),
                "system_stats": int(session.query(SystemStats).count()),
            }
            stats["tables"] = tables

        return stats

    def vacuum(self) -> None:
        """Vacuum database to reclaim space."""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("VACUUM"))
            logger.info("Database vacuumed successfully")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")
            raise

    def backup(self, backup_path: str) -> None:
        """Create a backup of the database.

        Args:
            backup_path: Path for the backup file
        """
        import shutil

        backup_path_obj = Path(backup_path)
        backup_path_obj.parent.mkdir(parents=True, exist_ok=True)

        try:
            # For SQLite, we can just copy the file
            # But first, checkpoint the WAL file if using WAL mode
            if self.enable_wal:
                with self.engine.connect() as conn:
                    conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

            # Copy the database file
            shutil.copy2(self.database_path, backup_path_obj)
            logger.info(f"Database backed up to: {backup_path}")

        except Exception as e:
            logger.error(f"Failed to backup database: {e}")
            raise
