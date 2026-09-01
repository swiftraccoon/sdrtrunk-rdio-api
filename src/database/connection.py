"""Database connection management for SQLite."""

import errno
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from ..config import (
    DatabaseConfig,
    open_secure_regular_file,
    prepare_private_directory,
)
from ..filesystem_security import (
    DATABASE_PROCESS_LOCK_NAME,
    durable_fsync,
    path_uses_dangerous_windows_namespace,
    paths_conflict,
    reject_insecure_extended_acl,
    sqlite_state_paths,
)
from ..models.database_models import Base

logger = logging.getLogger(__name__)

# Global lock for database initialization
_db_init_lock = threading.Lock()
_DEFAULT_SCHEMA_MINIMUM_FREE_BYTES = 288 * 1024 * 1024
_DEFAULT_SCHEMA_MINIMUM_FREE_INODES = 1024
_MINIMUM_INDEX_SCRATCH_BYTES = 32 * 1024 * 1024
_INDEX_STATE_COPY_MULTIPLIER = 2
_INDEX_STATE_INODES = 8
_INDEX_SCRATCH_INODES = 8
_INDEX_SCRATCH_FREE_BYTES = 32 * 1024 * 1024
_INDEX_SCRATCH_FREE_INODES = 8
_IS_POSIX = os.name == "posix"


def _windows_sqlite_temporary_directory() -> Path:
    """Return SQLite's Win32 ``GetTempPathW`` scratch directory."""

    # Imported only on Windows: ``ctypes.WinDLL`` is not available on Unix.
    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if win_dll is None or get_last_error is None:
        raise RuntimeError("Win32 SQLite scratch APIs are unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_temp_path = kernel32.GetTempPathW
    get_temp_path.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    get_temp_path.restype = wintypes.DWORD

    buffer_length = 261
    while True:
        buffer = ctypes.create_unicode_buffer(buffer_length)
        result = get_temp_path(buffer_length, buffer)
        if result == 0:
            error_code = get_last_error()
            raise RuntimeError(
                f"Win32 could not locate SQLite scratch storage ({error_code})"
            )
        if result < buffer_length:
            break
        # A too-small call returns the required size including the terminator.
        if result > 32768:
            raise RuntimeError("Win32 returned an invalid SQLite scratch path")
        buffer_length = result

    candidate = Path(buffer.value)
    if not candidate.is_absolute():
        raise RuntimeError("Win32 returned a non-absolute SQLite scratch path")
    try:
        candidate = candidate.resolve(strict=True)
        candidate_status = os.stat(candidate, follow_symlinks=False)
    except OSError:
        raise RuntimeError("Win32 returned an unusable SQLite scratch path") from None
    if not stat.S_ISDIR(candidate_status.st_mode) or not os.access(candidate, os.W_OK):
        raise RuntimeError("Win32 returned an unusable SQLite scratch path")
    return candidate


def _sqlite_temporary_directory() -> Path:
    """Return the directory SQLite will use for file-backed temp storage.

    Python's :mod:`tempfile` ignores ``SQLITE_TMPDIR``. On Unix, however,
    SQLite gives that variable precedence over ``TMPDIR`` before consulting
    its fixed fallback list. An explicitly configured but unusable path is
    rejected instead of preflighting a different filesystem from the one the
    deployment intended SQLite to use.
    """

    if not _IS_POSIX:  # pragma: no cover - exercised by Windows CI
        return _windows_sqlite_temporary_directory()

    for variable in ("SQLITE_TMPDIR", "TMPDIR"):
        if variable not in os.environ:
            continue
        value = os.environ[variable]
        candidate = Path(value)
        if not value or not candidate.is_absolute():
            raise RuntimeError(
                f"{variable} must name an absolute SQLite scratch directory"
            )
        try:
            candidate = candidate.resolve(strict=True)
            candidate_status = os.stat(candidate, follow_symlinks=False)
        except OSError:
            raise RuntimeError(
                f"{variable} does not name a usable SQLite scratch directory"
            ) from None
        if not stat.S_ISDIR(candidate_status.st_mode) or not os.access(
            candidate, os.W_OK | os.X_OK
        ):
            raise RuntimeError(
                f"{variable} does not name a usable SQLite scratch directory"
            )
        return candidate

    # Keep this order synchronized with SQLite's Unix temporary-directory
    # search. Resolve the final current-directory fallback so a later chdir
    # cannot silently move the filesystem that was preflighted.
    for candidate in (
        # These constants mirror SQLite's VFS search order. This function only
        # probes the selected filesystem; SQLite generates its own randomized,
        # immediately unlinked scratch names.
        Path("/var/tmp"),  # nosec B108
        Path("/usr/tmp"),
        Path("/tmp"),  # nosec B108
        Path.cwd(),
    ):
        try:
            resolved_candidate = candidate.resolve(strict=True)
            candidate_status = os.stat(
                resolved_candidate,
                follow_symlinks=False,
            )
        except OSError:
            continue
        if stat.S_ISDIR(candidate_status.st_mode) and os.access(
            resolved_candidate,
            os.W_OK | os.X_OK,
        ):
            return resolved_candidate
    raise RuntimeError("Could not locate a usable SQLite scratch directory")


class DatabaseInUseError(RuntimeError):
    """Raised when another process owns the database's exclusive service lock."""


class DatabaseManager:
    """Manages SQLite database connections and sessions."""

    def __init__(
        self,
        database_path: str | DatabaseConfig,
        enable_wal: bool = True,
        echo: bool = False,
        *,
        exclusive_process_lock: bool = False,
        read_only: bool = False,
        schema_minimum_free_bytes: int = _DEFAULT_SCHEMA_MINIMUM_FREE_BYTES,
        schema_minimum_free_inodes: int = _DEFAULT_SCHEMA_MINIMUM_FREE_INODES,
    ):
        """Initialize database manager.

        Args:
            database_path: Path to SQLite database file or DatabaseConfig object
            enable_wal: Enable Write-Ahead Logging for better concurrency
            echo: Enable SQL query logging
            read_only: Open an existing database without journal/schema writes
        """
        if read_only and exclusive_process_lock:
            raise ValueError(
                "A read-only database manager cannot create a process lock"
            )
        if schema_minimum_free_bytes < 0 or schema_minimum_free_inodes < 0:
            raise ValueError("Schema migration filesystem reserves cannot be negative")
        self.read_only = read_only
        self.schema_minimum_free_bytes = schema_minimum_free_bytes
        self.schema_minimum_free_inodes = schema_minimum_free_inodes
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
        if path_uses_dangerous_windows_namespace(self.database_path):
            raise ValueError(
                "Database path cannot use a reserved or ambiguous Windows filename"
            )

        self._process_lock_descriptor = -1

        # Pin a canonical, component-validated private parent before SQLite
        # later reopens the pathname for its database, WAL, and SHM files.
        if self.read_only:
            self._validate_read_only_database_file()
        else:
            private_parent = prepare_private_directory(self.database_path.parent)
            self.database_path = private_parent / self.database_path.name
        if exclusive_process_lock:
            self._acquire_exclusive_process_lock()

        try:
            if not self.read_only:
                self._prepare_database_file()

            # Create engine with connection pooling
            self.engine = self._create_engine()
            if not self.read_only:
                self._configure_journal_mode()

            # Create thread-safe session factory using scoped_session
            # expire_on_commit=False: callers receive objects from short-lived
            # sessions and read attributes after the session closes.
            session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
            self.Session = scoped_session(session_factory)

            # Read-only monitoring commands must never create tables/indexes or
            # change a live server's persistent journal mode.
            if not self.read_only:
                self._init_database()
        except BaseException:
            engine = getattr(self, "engine", None)
            if engine is not None:
                engine.dispose()
            self._release_exclusive_process_lock()
            raise

        logger.info("Database initialized at: %s", self.database_path)

    def _validate_read_only_database_file(self) -> None:
        """Validate an existing private database without chmod/create side effects."""
        descriptor = open_secure_regular_file(self.database_path)
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError("Database path is not a regular file")
            if os.name == "posix" and stat.S_IMODE(file_status.st_mode) & 0o077:
                raise PermissionError("Database file must have mode 0600")
        finally:
            os.close(descriptor)

        # The descriptor walk above rejected attacker-controlled symlinks. Use
        # the canonical spelling for SQLite (notably macOS /var -> /private/var)
        # while preserving the literal final filename.
        self.database_path = (
            self.database_path.expanduser().absolute().parent.resolve(strict=True)
            / self.database_path.name
        )

    def _acquire_exclusive_process_lock(self) -> None:
        """Exclude another server or destructive/long-snapshot CLI process."""
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(
            self.database_path.parent / DATABASE_PROCESS_LOCK_NAME,
            flags,
            0o600,
        )
        try:
            lock_status = os.fstat(descriptor)
            if not stat.S_ISREG(lock_status.st_mode):
                raise OSError("Database process lock is not a regular file")
            if os.name == "posix" and lock_status.st_uid != os.geteuid():
                raise PermissionError(
                    "Database process lock must be owned by this user"
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            reject_insecure_extended_acl(
                descriptor, description="Private database process lock"
            )

            # msvcrt.locking requires a byte range that exists. Keeping the
            # one-byte lock file also gives every process a stable inode.
            if lock_status.st_size < 1:
                os.write(descriptor, b"\0")
                durable_fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)

            try:
                if os.name == "nt":  # pragma: no cover - exercised by Windows CI
                    import msvcrt

                    windows_api = vars(msvcrt)
                    windows_api["locking"](descriptor, windows_api["LK_NBLCK"], 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise DatabaseInUseError(
                        "Database is already in use by the server or an "
                        "exclusive maintenance/export command"
                    ) from None
                raise
        except BaseException:
            os.close(descriptor)
            raise
        self._process_lock_descriptor = descriptor

    def _release_exclusive_process_lock(self) -> None:
        """Release the process lock idempotently; closing is the final fallback."""
        descriptor = self._process_lock_descriptor
        if descriptor < 0:
            return
        self._process_lock_descriptor = -1
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":  # pragma: no cover - exercised by Windows CI
                import msvcrt

                windows_api = vars(msvcrt)
                windows_api["locking"](descriptor, windows_api["LK_UNLCK"], 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            # The descriptor close below also releases kernel-owned locks.
            pass
        finally:
            os.close(descriptor)

    def _prepare_database_file(self) -> None:
        """Open/create a regular SQLite file without following a final symlink."""
        parent_fd = -1
        descriptor = -1
        try:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            if os.name == "posix":
                parent_fd = os.open(
                    self.database_path.parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                parent_status = os.fstat(parent_fd)
                if parent_status.st_uid not in {0, os.geteuid()}:
                    raise PermissionError(
                        "Database directory must be owned by this user or root"
                    )
                if stat.S_IMODE(parent_status.st_mode) & 0o022:
                    raise PermissionError(
                        "Database directory must not be group/world writable"
                    )
                descriptor = os.open(
                    self.database_path.name,
                    flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            else:  # pragma: no cover - exercised by Windows CI
                descriptor = os.open(self.database_path, flags, 0o600)

            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError("Database path is not a regular file")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            reject_insecure_extended_acl(
                descriptor, description="Private database file"
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_fd >= 0:
                os.close(parent_fd)

    def _create_engine(self) -> Engine:
        """Create SQLAlchemy engine with SQLite optimizations."""
        # Use SQLAlchemy's structured URL constructor. Interpolating a Path
        # into a sqlite:/// string lets filename characters such as ``?`` be
        # parsed as URL syntax, causing SQLite to reopen a different path than
        # the inode validated by ``_prepare_database_file``.
        connection_url = URL.create("sqlite", database=str(self.database_path))

        connect_args: dict[str, Any] = {
            "check_same_thread": False,  # Allow multiple threads
            "timeout": 30,  # Connection timeout in seconds
            # Disable pysqlite's implicit transaction handling; the
            # "begin" listener below issues BEGIN explicitly so that
            # SQLAlchemy transactions (and rollback) actually work.
            "isolation_level": None,
        }
        engine_options: dict[str, Any] = {
            "echo": self.echo,
            "poolclass": NullPool,
        }
        if self.read_only:
            # Path.as_uri percent-encodes filename metacharacters before the
            # SQLite mode query is appended. The DB-API creator avoids a second
            # URL parser changing a literal '?' or '#' in the filename.
            database_uri = f"{self.database_path.as_uri()}?mode=ro"

            def open_read_only_connection() -> sqlite3.Connection:
                return sqlite3.connect(
                    database_uri,
                    uri=True,
                    check_same_thread=False,
                    timeout=30,
                    isolation_level=None,
                )

            engine_options["creator"] = open_read_only_connection
            connection_url = URL.create("sqlite")
        else:
            engine_options["connect_args"] = connect_args

        # Use NullPool for better thread safety with SQLite
        engine = create_engine(connection_url, **engine_options)

        # Configure SQLite for better performance
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()

            # Performance optimizations
            cursor.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                # Defense in depth beyond mode=ro. This also prevents writes to
                # attached or temporary schemas if future CLI queries change.
                cursor.execute("PRAGMA query_only=ON")
                cursor.execute("PRAGMA temp_store=FILE")
                cursor.execute("PRAGMA mmap_size=268435456")
                cursor.close()
                return
            # Overwrite deleted cell contents. Retention is a confidentiality
            # boundary, so FAST's partial freelist scrubbing is insufficient.
            cursor.execute("PRAGMA secure_delete=ON")
            # File publication/deletion is coupled to durable staging and
            # retention rows. WAL+NORMAL may lose the most recent commit on a
            # power failure after the corresponding filesystem fsync, leaving
            # a resurrected reference or an untracked file. FULL preserves the
            # ordering contract across those two resources.
            cursor.execute("PRAGMA synchronous=FULL")
            # SQLite treats these as platform-appropriate no-ops where the
            # stronger flush primitive is unavailable. On macOS they request
            # F_FULLFSYNC for commits and checkpoints.
            cursor.execute("PRAGMA fullfsync=ON")
            cursor.execute("PRAGMA checkpoint_fullfsync=ON")
            # Large first-upgrade index sorts must spill to bounded, preflighted
            # filesystem space instead of growing the process heap without a
            # useful admission boundary.
            cursor.execute("PRAGMA temp_store=FILE")
            cursor.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O

            cursor.close()

        # Emit our own BEGIN: required with isolation_level=None, otherwise
        # every statement autocommits and session.rollback() is a no-op.
        @event.listens_for(engine, "begin")
        def do_begin(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN")

        return engine

    def _configure_journal_mode(self) -> None:
        """Set and verify the requested persistent SQLite journal mode.

        Journal mode is stored in the database. Merely skipping the WAL pragma
        when WAL is disabled would leave a previously WAL-enabled database in
        WAL mode while making ``checkpoint()`` a no-op, retaining deleted data
        and an ever-growing sidecar contrary to the active configuration.
        """
        requested_mode = "WAL" if self.enable_wal else "DELETE"
        result: tuple[Any, ...] | None = None
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                result = cursor.execute(
                    f"PRAGMA journal_mode={requested_mode}"
                ).fetchone()
            finally:
                cursor.close()
        finally:
            raw.close()
        if not result or str(result[0]).lower() != requested_mode.lower():
            raise RuntimeError(
                f"SQLite refused to enable {requested_mode} journal mode"
            )

    def _init_database(self) -> None:
        """Initialize database schema (thread-safe)."""
        global _db_init_lock

        # Use a global lock to ensure only one thread initializes the database
        with _db_init_lock:
            try:
                # Create all tables if they don't exist
                # Using create_all is idempotent - it won't recreate existing tables
                Base.metadata.create_all(self.engine, checkfirst=True)
                # create_all does not add newly declared indexes to an already
                # existing table. Retention selection and reference checks rely
                # on these indexes to avoid repeated full-table scans.
                required_indexes = {
                    "idx_audio_file_path",
                    "idx_created_at_desc",
                    "idx_pending_claim_next_attempt",
                    "idx_pending_claimed_at",
                    "ix_pending_file_deletions_next_attempt_at",
                    "ix_upload_logs_timestamp",
                }
                required_index_objects = sorted(
                    (
                        index
                        for table in Base.metadata.tables.values()
                        for index in table.indexes
                        if index.name in required_indexes
                    ),
                    key=lambda index: str(index.name),
                )
                inspector = inspect(self.engine)
                existing_indexes = {
                    (table_name, str(index["name"]))
                    for table_name in Base.metadata.tables
                    for index in inspector.get_indexes(table_name)
                    if index.get("name") is not None
                }
                for required_index in required_index_objects:
                    index_table = required_index.table
                    if index_table is None:  # pragma: no cover - fixed metadata
                        raise RuntimeError("Required index is not bound to a table")
                    table_name = index_table.name
                    index_name = str(required_index.name)
                    if (table_name, index_name) in existing_indexes:
                        continue
                    self._preflight_index_creation(index_name)
                    required_index.create(self.engine, checkfirst=True)
                    if not self.checkpoint(truncate=True):
                        raise RuntimeError(
                            "SQLite WAL remained busy after required index creation"
                        )
                logger.info("Database schema created/verified")
            except Exception as e:
                logger.error("Failed to initialize database: %s", e)
                raise

    def _preflight_index_creation(self, index_name: str) -> None:
        """Conservatively reserve state, WAL-copy, and disk-sort headroom."""
        try:
            database_status = os.stat(self.database_path, follow_symlinks=False)
            if not stat.S_ISREG(database_status.st_mode):
                raise OSError("Database path is not a regular file")
            wal_bytes = 0
            wal_path = self.database_path.with_name(f"{self.database_path.name}-wal")
            try:
                wal_status = os.stat(wal_path, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(wal_status.st_mode):
                    raise OSError("SQLite WAL path is not a regular file")
                wal_bytes = wal_status.st_size
            scratch_bytes = max(
                database_status.st_size + wal_bytes,
                _MINIMUM_INDEX_SCRATCH_BYTES,
            )
            requirements: dict[int, tuple[Path, int, int, bool]] = {}

            def add_requirement(
                path: Path,
                required_bytes: int,
                required_inodes: int,
                *,
                hosts_state: bool,
            ) -> None:
                path_status = os.stat(path, follow_symlinks=False)
                if not stat.S_ISDIR(path_status.st_mode):
                    raise OSError("Schema scratch path is not a directory")
                (
                    existing_path,
                    existing_bytes,
                    existing_inodes,
                    existing_hosts_state,
                ) = requirements.get(
                    path_status.st_dev,
                    (path, 0, 0, False),
                )
                requirements[path_status.st_dev] = (
                    existing_path,
                    existing_bytes + required_bytes,
                    existing_inodes + required_inodes,
                    existing_hosts_state or hosts_state,
                )

            # At commit/checkpoint an index can temporarily occupy both WAL and
            # main-database pages. Its external sort can simultaneously require
            # another database-sized scratch file.
            add_requirement(
                self.database_path.parent,
                scratch_bytes * _INDEX_STATE_COPY_MULTIPLIER,
                _INDEX_STATE_INODES,
                hosts_state=True,
            )
            add_requirement(
                _sqlite_temporary_directory(),
                scratch_bytes,
                _INDEX_SCRATCH_INODES,
                hosts_state=False,
            )

            for (
                path,
                required_bytes,
                required_inodes,
                hosts_state,
            ) in requirements.values():
                minimum_free_bytes = (
                    self.schema_minimum_free_bytes
                    if hosts_state
                    else _INDEX_SCRATCH_FREE_BYTES
                )
                minimum_free_inodes = (
                    self.schema_minimum_free_inodes
                    if hosts_state
                    else _INDEX_SCRATCH_FREE_INODES
                )
                if hasattr(os, "statvfs"):
                    filesystem = os.statvfs(path)
                    fragment_size = filesystem.f_frsize or filesystem.f_bsize
                    available_bytes = filesystem.f_bavail * fragment_size
                    if (
                        filesystem.f_files
                        and filesystem.f_favail - required_inodes < minimum_free_inodes
                    ):
                        raise RuntimeError(
                            "Insufficient filesystem inode headroom for schema upgrade"
                        )
                else:  # pragma: no cover - exercised by Windows CI
                    available_bytes = shutil.disk_usage(path).free
                if available_bytes - required_bytes < minimum_free_bytes:
                    raise RuntimeError(
                        "Insufficient filesystem byte headroom for schema upgrade"
                    )
        except RuntimeError:
            raise
        except (AttributeError, OSError, TypeError, ValueError):
            raise RuntimeError(
                "Could not verify filesystem headroom for schema upgrade"
            ) from None

        logger.info("Verified bounded filesystem headroom for index %s", index_name)

    @contextmanager
    def get_session(self) -> Generator[Session]:
        """Get a database session with automatic cleanup.

        Thread-safe: Each thread gets its own session from the scoped_session.

        Usage:
            with db_manager.get_session() as session:
                # Use session here
                session.add(record)

        A clean context exit is the single commit boundary.  Cleanup failures
        are logged after that outcome is final and must never turn a durable
        commit into an apparent failure for callers that would compensate an
        associated filesystem mutation.
        """
        # Get thread-local session from scoped_session
        session = self.Session()
        try:
            yield session
            # Commit on clean exit; this also finalizes flushed-but-not-
            # committed changes and cleanly ends read-only transactions.
            session.commit()
        except BaseException:
            try:
                session.rollback()
            except Exception as cleanup_error:
                # Preserve the original body/commit exception. A rollback or
                # cleanup error cannot make the transaction outcome clearer.
                logger.error(
                    "Database rollback failed while preserving an earlier error (%s)",
                    type(cleanup_error).__name__,
                )
            raise
        finally:
            cleanup_failed = False
            try:
                session.close()
            except Exception as cleanup_error:
                cleanup_failed = True
                logger.error(
                    "Database session close failed after transaction finalization (%s)",
                    type(cleanup_error).__name__,
                )
            try:
                # Remove the session from the thread-local registry so a later
                # request cannot inherit transaction state from this context.
                self.Session.remove()
            except Exception as cleanup_error:
                cleanup_failed = True
                logger.error(
                    "Database session registry cleanup failed after transaction "
                    "finalization (%s)",
                    type(cleanup_error).__name__,
                )
            if cleanup_failed:
                # ``remove`` normally clears the registry after closing the
                # session. If close itself raised, force the reference out of
                # the registry so the failed object cannot be reused. This is
                # deliberately best-effort and never changes the already-final
                # transaction result observed by the caller.
                try:
                    self.Session.registry.clear()
                except Exception as cleanup_error:
                    logger.error(
                        "Database session registry could not be force-cleared (%s)",
                        type(cleanup_error).__name__,
                    )

    def close(self) -> None:
        """Close database connections."""
        try:
            # Remove scoped session registry
            self.Session.remove()
            # Dispose of the engine
            self.engine.dispose()
            logger.info("Database connections closed")
        finally:
            self._release_exclusive_process_lock()

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def check_connection(self) -> bool:
        """Cheap connectivity check (used by the health endpoint).

        Unlike get_stats, this does not scan any tables.
        """
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("Database connectivity check failed")
            return False

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
            from ..models.database_models import RadioCall, UploadLog

            tables: dict[str, int] = {
                "radio_calls": int(session.query(RadioCall).count()),
                "upload_logs": int(session.query(UploadLog).count()),
            }
            stats["tables"] = tables

        return stats

    def vacuum(self) -> None:
        """Vacuum database to reclaim space.

        Uses a raw DBAPI connection: VACUUM cannot run inside the
        transaction that the engine's begin listener would open.
        """
        try:
            raw = self.engine.raw_connection()
            try:
                raw.cursor().execute("VACUUM")
            finally:
                raw.close()
            logger.info("Database vacuumed successfully")
        except Exception as e:
            logger.error("Failed to vacuum database: %s", e)
            raise

    def checkpoint(self, *, truncate: bool = False) -> bool:
        """Checkpoint WAL pages, optionally truncating stale WAL contents.

        Returns false when SQLite reports a busy writer; callers can safely
        retry during the next maintenance cycle.
        """
        if not self.enable_wal:
            return True
        checkpoint_statement = (
            "PRAGMA wal_checkpoint(TRUNCATE)"
            if truncate
            else "PRAGMA wal_checkpoint(PASSIVE)"
        )
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                busy, _wal_pages, _checkpointed = cursor.execute(
                    checkpoint_statement
                ).fetchone()
            finally:
                cursor.close()
        finally:
            raw.close()
        if busy:
            logger.warning(
                "SQLite WAL checkpoint deferred because the database is busy"
            )
            return False
        return True

    def backup(self, backup_path: str) -> None:
        """Create a transactionally consistent, atomic SQLite backup.

        Args:
            backup_path: Path for the backup file
        """
        if path_uses_dangerous_windows_namespace(backup_path):
            raise ValueError(
                "Backup destination cannot use a reserved or ambiguous Windows filename"
            )
        backup_path_obj = Path(backup_path)
        protected_paths = sqlite_state_paths(self.database_path)
        if any(paths_conflict(backup_path_obj, path) for path in protected_paths):
            raise ValueError("Backup destination must differ from live database state")
        # Validate every component and create missing names relative to pinned
        # directory descriptors.  A pathname-only mkdir/stat sequence here
        # would let another user swap a checked leaf beneath a writable
        # ancestor before mkstemp publishes a confidential snapshot.
        private_parent = prepare_private_directory(backup_path_obj.parent)
        backup_path_obj = private_parent / backup_path_obj.name
        if backup_path_obj.is_symlink():
            raise ValueError("Backup destination must not be a symbolic link")
        if backup_path_obj.exists() and not backup_path_obj.is_file():
            raise ValueError("Backup destination must be a regular file")
        if any(paths_conflict(backup_path_obj, path) for path in protected_paths):
            raise ValueError("Backup destination must differ from live database state")

        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{backup_path_obj.name}.",
                suffix=".tmp",
                dir=backup_path_obj.parent,
            )
            temporary_path = Path(temporary_name)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            reject_insecure_extended_acl(
                descriptor, description="Private database backup"
            )
            os.close(descriptor)
            descriptor = -1

            source_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            with (
                closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source,
                closing(sqlite3.connect(temporary_path, timeout=30)) as destination,
            ):
                # SQLite's online backup API holds the locks required for a
                # coherent snapshot and includes committed WAL transactions.
                source.backup(destination)
                # Compact the snapshot, not the live service database. This
                # removes freelist remnants left by older versions that may
                # have used secure_delete=OFF or FAST.
                destination.execute("PRAGMA secure_delete=ON")
                destination.execute("VACUUM")
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")

            temporary_path.chmod(0o600)
            # Windows' CRT-backed fsync rejects read-only descriptors. The
            # snapshot is private and owned by this process, so reopening it
            # read/write preserves the same validation while remaining portable.
            with temporary_path.open("rb+") as backup_file:
                durable_fsync(backup_file.fileno())
            os.replace(temporary_path, backup_path_obj)
            temporary_path = None
            backup_path_obj.chmod(0o600)
            if os.name == "posix":
                directory_fd = os.open(
                    private_parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    durable_fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            logger.info("Database backed up to: %s", backup_path)

        except Exception as e:
            logger.error("Failed to backup database (%s)", type(e).__name__)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
