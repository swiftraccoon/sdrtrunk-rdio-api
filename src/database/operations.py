"""Database operations for radio call data."""

import logging
import sqlite3
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models.api_models import RdioScannerUpload
from ..models.database_models import (
    PendingFileDeletion,
    RadioCall,
    UploadLog,
)
from ..security.text import sanitize_log_value
from .connection import DatabaseManager

logger = logging.getLogger(__name__)

METRICS_CARDINALITY_LIMIT = 1000
METRICS_TALKGROUP_LIMIT = 20
DEFAULT_RETENTION_BATCH_SIZE = 500
# A 500-row retention transaction has been observed to grow SQLite's WAL by
# roughly 7.5 MiB. Keeping the configurable upper bound at 1,000 lets the
# capacity manager protect a conservative 32 MiB transaction/checkpoint margin.
MAX_RETENTION_BATCH_SIZE = 1_000
STAGED_FILE_GRACE_SECONDS = 60 * 60
MAX_QUERY_OFFSET_ROWS = 100_000
EXPENSIVE_QUERY_DEADLINE_SECONDS = 15.0
EXPENSIVE_QUERY_PROGRESS_STEPS = 10_000
# Bound work independently of wall-clock speed. This is intentionally a
# process constant rather than request input: a fast database must not turn the
# 15-second deadline into an effectively unbounded amount of CPU or temporary
# sort I/O. SQLite invokes the progress callback approximately every N virtual
# machine opcodes, so the ceiling is conservative rather than cycle-exact.
EXPENSIVE_QUERY_MAX_VM_STEPS = 50_000_000


class ExpensiveQueryTimeout(RuntimeError):
    """Raised when SQLite exceeds the CPU/VM budget for an expensive read."""


@dataclass(frozen=True)
class CleanupBacklogCounts:
    """One consistent, cumulatively bounded cleanup backlog snapshot."""

    expired_calls: int
    expired_upload_logs: int
    due_file_deletions: int
    failed_file_deletions: int
    expired_audio_paths: int | None = None


@contextmanager
def expensive_query_deadline(session: Session) -> Iterator[None]:
    """Interrupt long-running SQLite VM work on this session's private connection.

    ``DatabaseManager`` uses ``NullPool``, so an expensive read owns its DB-API
    connection for the duration of this context. The progress callback therefore
    cannot interrupt another request. It bounds SQLite computation; OS-level
    filesystem stalls remain an operating-system/reverse-proxy concern.
    """
    driver_connection = session.connection().connection.driver_connection
    if not isinstance(driver_connection, sqlite3.Connection):
        yield
        return

    progress_interval = max(1, EXPENSIVE_QUERY_PROGRESS_STEPS)
    max_vm_steps = max(1, EXPENSIVE_QUERY_MAX_VM_STEPS)
    deadline = time.monotonic() + EXPENSIVE_QUERY_DEADLINE_SECONDS
    deadline_reached = False
    vm_budget_reached = False
    vm_steps = 0

    def should_interrupt() -> int:
        nonlocal deadline_reached, vm_budget_reached, vm_steps
        vm_steps += progress_interval
        if vm_steps >= max_vm_steps:
            vm_budget_reached = True
            return 1
        if time.monotonic() >= deadline:
            deadline_reached = True
            return 1
        return 0

    driver_connection.set_progress_handler(should_interrupt, progress_interval)
    try:
        yield
    except Exception:
        if deadline_reached or vm_budget_reached:
            raise ExpensiveQueryTimeout(
                "Expensive database query exceeded its execution budget"
            ) from None
        raise
    finally:
        driver_connection.set_progress_handler(None, 0)


def _validate_retention_batch_size(batch_size: int) -> None:
    """Keep every retention query and mutation within a fixed memory bound."""
    if not 1 <= batch_size <= MAX_RETENTION_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_RETENTION_BATCH_SIZE}")


class DatabaseOperations:
    """High-level database operations for radio call data."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize database operations.

        Args:
            db_manager: DatabaseManager instance
        """
        self.db_manager = db_manager

    def save_call(
        self,
        upload_data: RdioScannerUpload,
        client_ip: str | None = None,
        stored_path: str | None = None,
        api_key_id: str | None = None,
    ) -> int:
        """Save a radio call to the database (alias for save_radio_call).

        Args:
            upload_data: RdioScanner upload data
            client_ip: IP address of uploader
            stored_path: Path where audio file is stored
            api_key_id: ID of API key used

        Returns:
            Database ID of the created record
        """
        return self.save_radio_call(
            upload_data=upload_data,
            audio_file_path=stored_path,
            upload_ip=client_ip,
            api_key_id=api_key_id,
        )

    def save_radio_call(
        self,
        upload_data: RdioScannerUpload,
        audio_file_path: str | None = None,
        upload_ip: str | None = None,
        api_key_id: str | None = None,
        require_staged_file: bool = False,
    ) -> int:
        """Save a radio call to the database.

        Args:
            upload_data: RdioScanner upload data
            audio_file_path: Root-relative stored-audio reference (legacy absolute
                paths remain readable beneath the configured storage root)
            upload_ip: IP address of uploader
            api_key_id: ID of API key used
            require_staged_file: Require an unclaimed pre-publish reservation

        Returns:
            Database ID of the created record
        """
        with self.db_manager.get_session() as session:
            if audio_file_path:
                consumed_stage = (
                    session.query(PendingFileDeletion)
                    .filter(PendingFileDeletion.path == audio_file_path)
                    .filter(PendingFileDeletion.kind == "staged")
                    .filter(PendingFileDeletion.claim_token.is_(None))
                    .delete(synchronize_session=False)
                )
                if require_staged_file and consumed_stage != 1:
                    raise RuntimeError(
                        "Staged audio reservation is unavailable for commit"
                    )

            # Create RadioCall record
            call = RadioCall(
                call_timestamp=datetime.fromtimestamp(upload_data.dateTime, tz=UTC),
                system_id=upload_data.system,
                system_label=upload_data.systemLabel,
                frequency=upload_data.frequency,
                talkgroup_id=upload_data.talkgroup,
                talkgroup_label=upload_data.talkgroupLabel,
                talkgroup_group=upload_data.talkgroupGroup,
                talkgroup_tag=upload_data.talkgroupTag,
                source_radio_id=upload_data.source,
                talker_alias=upload_data.talkerAlias,
                audio_filename=upload_data.audio_filename,
                audio_content_type=upload_data.audio_content_type,
                audio_size_bytes=upload_data.audio_size,
                audio_file_path=audio_file_path,
                patches=upload_data.patches,
                frequencies=upload_data.frequencies,
                sources=upload_data.sources,
                upload_ip=upload_ip,
                upload_api_key_id=api_key_id,
            )

            session.add(call)
            session.flush()

            # Capture generated and normalized values while the flushed object
            # is attached. The context manager performs the sole commit before
            # control leaves this block.
            call_id = int(call.id)
            system_id = call.system_id
            talkgroup_id = call.talkgroup_id
            call_timestamp = call.call_timestamp

        # Observability runs after the transaction boundary and must never
        # turn a durable commit into an apparent save failure. In particular,
        # a custom logging handler is allowed to fail without prompting the
        # upload endpoint to compensate by deleting committed audio.
        try:
            logger.info(
                "Saved radio call: ID=%s, System=%s, TG=%s, Time=%s",
                call_id,
                system_id,
                talkgroup_id,
                call_timestamp,
            )
        except Exception:
            pass
        return call_id

    def log_upload_attempt(
        self,
        client_ip: str,
        success: bool,
        system_id: str | None = None,
        api_key_used: str | None = None,
        user_agent: str | None = None,
        filename: str | None = None,
        file_size: int | None = None,
        content_type: str | None = None,
        error_message: str | None = None,
        response_code: int | None = None,
        processing_time_ms: float | None = None,
    ) -> None:
        """Log an upload attempt for security and debugging.

        Args:
            client_ip: IP address of client
            success: Whether upload was successful
            system_id: System ID from request
            api_key_used: API key ID used
            user_agent: User agent string
            filename: Uploaded filename
            file_size: File size in bytes
            content_type: MIME type
            error_message: Error message if failed
            response_code: HTTP response code
            processing_time_ms: Processing time in milliseconds
        """
        with self.db_manager.get_session() as session:
            log_entry = UploadLog(
                client_ip=client_ip,
                user_agent=user_agent,
                api_key_used=api_key_used,
                system_id=system_id,
                success=success,
                error_message=error_message,
                filename=filename,
                file_size=file_size,
                content_type=content_type,
                response_code=response_code,
                processing_time_ms=processing_time_ms,
            )

            session.add(log_entry)

    def get_recent_calls(
        self,
        limit: int = 100,
        system_id: str | None = None,
        talkgroup_id: int | None = None,
    ) -> list[RadioCall]:
        """Get recent radio calls.

        Args:
            limit: Maximum number of calls to return
            system_id: Filter by system ID
            talkgroup_id: Filter by talkgroup ID

        Returns:
            List of RadioCall objects
        """
        with self.db_manager.get_session() as session:
            query = session.query(RadioCall)

            # Apply filters
            if system_id:
                query = query.filter(RadioCall.system_id == system_id)
            if talkgroup_id:
                query = query.filter(RadioCall.talkgroup_id == talkgroup_id)

            # Order by timestamp descending and apply limit
            calls = query.order_by(desc(RadioCall.call_timestamp)).limit(limit).all()

            return calls

    def get_statistics(
        self, allowed_systems: Collection[str] | None = None
    ) -> dict[str, Any]:
        """Get overall statistics.

        High-cardinality system and upload-source maps contain the top 1000
        entries by call count; talkgroups contain the top 20. Deterministic
        tie-breaks keep repeated responses stable.

        Args:
            allowed_systems: Optional system IDs visible to the caller

        Returns:
            Dictionary with statistics
        """
        stats: dict[str, Any] = {}

        with (
            self.db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):

            def scoped(query: Any) -> Any:
                if allowed_systems is not None:
                    query = query.filter(RadioCall.system_id.in_(allowed_systems))
                return query

            now = datetime.now(UTC)

            # Total calls
            stats["total_calls"] = scoped(session.query(RadioCall)).count()

            # Calls today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            stats["calls_today"] = (
                scoped(session.query(RadioCall))
                .filter(RadioCall.call_timestamp >= today_start)
                .filter(RadioCall.call_timestamp <= now)
                .count()
            )

            # Calls last hour
            hour_ago = now - timedelta(hours=1)
            stats["calls_last_hour"] = (
                scoped(session.query(RadioCall))
                .filter(RadioCall.call_timestamp >= hour_ago)
                .filter(RadioCall.call_timestamp <= now)
                .count()
            )

            # Bound high-cardinality maps so one authenticated request cannot
            # force an arbitrarily large response. Ties are deterministic.
            system_counts = (
                scoped(
                    session.query(
                        RadioCall.system_id, func.count(RadioCall.id).label("count")
                    )
                )
                .group_by(RadioCall.system_id)
                .order_by(desc("count"), RadioCall.system_id)
                .limit(METRICS_CARDINALITY_LIMIT)
                .all()
            )

            systems: dict[str, int] = {
                str(sys_id): count for sys_id, count in system_counts
            }
            stats["systems"] = systems

            # Talkgroup breakdown (top 20)
            tg_counts = (
                scoped(
                    session.query(
                        RadioCall.talkgroup_id,
                        RadioCall.talkgroup_label,
                        func.count(RadioCall.id).label("count"),
                    )
                )
                .filter(RadioCall.talkgroup_id.isnot(None))
                .group_by(RadioCall.talkgroup_id, RadioCall.talkgroup_label)
                .order_by(
                    desc("count"),
                    RadioCall.talkgroup_id,
                    RadioCall.talkgroup_label,
                )
                .limit(METRICS_TALKGROUP_LIMIT)
                .all()
            )

            talkgroups: dict[str, int] = {
                f"{tg_id} ({tg_label or 'Unknown'})": count
                for tg_id, tg_label, count in tg_counts
            }
            stats["talkgroups"] = talkgroups

            # Upload sources
            source_counts = (
                scoped(
                    session.query(
                        RadioCall.upload_ip, func.count(RadioCall.id).label("count")
                    )
                )
                .filter(RadioCall.upload_ip.isnot(None))
                .group_by(RadioCall.upload_ip)
                .order_by(desc("count"), RadioCall.upload_ip)
                .limit(METRICS_CARDINALITY_LIMIT)
                .all()
            )

            upload_sources: dict[str, int] = {
                str(ip): count for ip, count in source_counts
            }
            stats["upload_sources"] = upload_sources

            # Storage info
            stats["audio_files_count"] = (
                scoped(session.query(RadioCall))
                .filter(RadioCall.audio_file_path.isnot(None))
                .count()
            )

            # Calculate storage used (sum of file sizes)
            total_size = (
                scoped(session.query(func.sum(RadioCall.audio_size_bytes)))
                .filter(RadioCall.audio_size_bytes.isnot(None))
                .filter(RadioCall.audio_file_path.isnot(None))
                .scalar()
                or 0
            )

            storage_used_mb: float = float(total_size) / (1024 * 1024)
            stats["storage_used_mb"] = storage_used_mb

        return stats

    def count_calls_older_than(self, cutoff: datetime) -> int:
        """Count radio calls older than the cutoff."""
        with self.db_manager.get_session() as session:
            return int(
                session.query(RadioCall).filter(RadioCall.created_at < cutoff).count()
            )

    def count_upload_logs_older_than(self, cutoff: datetime) -> int:
        """Count upload log entries older than the cutoff."""
        with self.db_manager.get_session() as session:
            return int(
                session.query(UploadLog).filter(UploadLog.timestamp < cutoff).count()
            )

    def count_audio_paths_older_than(self, cutoff: datetime) -> int:
        """Count distinct stored paths belonging to expired calls."""
        with self.db_manager.get_session() as session:
            return int(
                session.query(func.count(func.distinct(RadioCall.audio_file_path)))
                .filter(RadioCall.created_at < cutoff)
                .filter(RadioCall.audio_file_path.isnot(None))
                .scalar()
                or 0
            )

    def get_cleanup_backlog_counts(
        self,
        cutoff: datetime,
        *,
        include_audio_paths: bool = False,
        stale_claim_seconds: int = 10 * 60,
    ) -> CleanupBacklogCounts:
        """Return cleanup counts from one session and one cumulative deadline.

        The CLI uses this for both its unlocked preview and every progress check
        under the exclusive service lock. A single deadline spans all queries,
        so splitting the snapshot into several individually bounded operations
        cannot multiply the configured SQLite work budget.
        """
        if not 60 <= stale_claim_seconds <= 24 * 60 * 60:
            raise ValueError("stale_claim_seconds must be between 60 and 86400")
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=stale_claim_seconds)

        with (
            self.db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            expired_calls = int(
                session.query(func.count(RadioCall.id))
                .filter(RadioCall.created_at < cutoff)
                .scalar()
                or 0
            )
            expired_upload_logs = int(
                session.query(func.count(UploadLog.id))
                .filter(UploadLog.timestamp < cutoff)
                .scalar()
                or 0
            )
            due_file_deletions = int(
                session.query(func.count(PendingFileDeletion.id))
                .filter(
                    (PendingFileDeletion.next_attempt_at.is_(None))
                    | (PendingFileDeletion.next_attempt_at <= now)
                )
                .filter(
                    (PendingFileDeletion.claim_token.is_(None))
                    | (PendingFileDeletion.claimed_at.is_(None))
                    | (PendingFileDeletion.claimed_at < stale_before)
                )
                .scalar()
                or 0
            )
            failed_file_deletions = int(
                session.query(func.count(PendingFileDeletion.id))
                .filter(PendingFileDeletion.attempt_count > 0)
                .scalar()
                or 0
            )
            expired_audio_paths = None
            if include_audio_paths:
                expired_audio_paths = int(
                    session.query(func.count(func.distinct(RadioCall.audio_file_path)))
                    .filter(RadioCall.created_at < cutoff)
                    .filter(RadioCall.audio_file_path.isnot(None))
                    .scalar()
                    or 0
                )

        return CleanupBacklogCounts(
            expired_calls=expired_calls,
            expired_upload_logs=expired_upload_logs,
            due_file_deletions=due_file_deletions,
            failed_file_deletions=failed_file_deletions,
            expired_audio_paths=expired_audio_paths,
        )

    def get_audio_paths_older_than(
        self, cutoff: datetime, limit: int = DEFAULT_RETENTION_BATCH_SIZE
    ) -> list[str]:
        """Return one bounded path batch for compatibility callers."""
        _validate_retention_batch_size(limit)
        with self.db_manager.get_session() as session:
            rows = (
                session.query(RadioCall.audio_file_path)
                .filter(RadioCall.created_at < cutoff)
                .filter(RadioCall.audio_file_path.isnot(None))
                .order_by(RadioCall.created_at, RadioCall.id)
                .limit(limit)
                .all()
            )
            return [str(row[0]) for row in rows]

    def queue_and_delete_expired_calls(
        self,
        cutoff: datetime,
        batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    ) -> int:
        """Atomically queue paths and delete one expired-call batch.

        A path still referenced by a call outside this batch is not queued.
        Any failure rolls back both the path insertion and row deletion.
        """
        _validate_retention_batch_size(batch_size)

        with self.db_manager.get_session() as session:
            rows = (
                session.query(RadioCall.id, RadioCall.audio_file_path)
                .filter(RadioCall.created_at < cutoff)
                .order_by(RadioCall.created_at, RadioCall.id)
                .limit(batch_size)
                .all()
            )
            if not rows:
                return 0

            call_ids = [int(row.id) for row in rows]
            candidate_paths = {
                str(row.audio_file_path) for row in rows if row.audio_file_path
            }
            deleted = (
                session.query(RadioCall)
                .filter(RadioCall.id.in_(call_ids))
                .delete(synchronize_session=False)
            )

            if candidate_paths:
                still_referenced = {
                    str(row[0])
                    for row in (
                        session.query(RadioCall.audio_file_path)
                        .filter(RadioCall.audio_file_path.in_(candidate_paths))
                        .distinct()
                        .all()
                    )
                }
                paths_to_queue = sorted(candidate_paths - still_referenced)
                if paths_to_queue:
                    queued_at = datetime.now(UTC)
                    statement = sqlite_insert(PendingFileDeletion).values(
                        [
                            {
                                "path": path,
                                "queued_at": queued_at,
                                "kind": "retention",
                                "attempt_count": 0,
                            }
                            for path in paths_to_queue
                        ]
                    )
                    session.execute(
                        statement.on_conflict_do_nothing(index_elements=["path"])
                    )

            return int(deleted)

    def delete_calls_older_than(
        self,
        cutoff: datetime,
        batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    ) -> int:
        """Queue files and delete one bounded expired-call batch."""
        return self.queue_and_delete_expired_calls(cutoff, batch_size=batch_size)

    def delete_upload_logs_older_than(
        self,
        cutoff: datetime,
        batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    ) -> int:
        """Delete one bounded batch of expired upload logs."""
        _validate_retention_batch_size(batch_size)
        with self.db_manager.get_session() as session:
            log_ids = [
                int(row[0])
                for row in (
                    session.query(UploadLog.id)
                    .filter(UploadLog.timestamp < cutoff)
                    .order_by(UploadLog.timestamp, UploadLog.id)
                    .limit(batch_size)
                    .all()
                )
            ]
            if not log_ids:
                return 0
            deleted = (
                session.query(UploadLog)
                .filter(UploadLog.id.in_(log_ids))
                .delete(synchronize_session=False)
            )
            return int(deleted)

    def claim_pending_file_deletions(
        self,
        claim_token: str,
        limit: int = DEFAULT_RETENTION_BATCH_SIZE,
        stale_claim_seconds: int = 10 * 60,
    ) -> tuple[list[tuple[int, str]], int]:
        """Atomically claim due, unreferenced deletion work.

        A stored-audio commit consumes only an unclaimed staged row in its own
        transaction. SQLite write serialization therefore makes claim-vs-save
        a single winner decision before any filesystem unlink occurs.
        """
        _validate_retention_batch_size(limit)
        if not claim_token or len(claim_token) > 64:
            raise ValueError("claim_token must contain between 1 and 64 characters")
        if not 60 <= stale_claim_seconds <= 24 * 60 * 60:
            raise ValueError("stale_claim_seconds must be between 60 and 86400")
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=stale_claim_seconds)
        with self.db_manager.get_session() as session:
            rows = (
                session.query(PendingFileDeletion.id, PendingFileDeletion.path)
                .filter(
                    (PendingFileDeletion.next_attempt_at.is_(None))
                    | (PendingFileDeletion.next_attempt_at <= now)
                )
                .filter(
                    (PendingFileDeletion.claim_token.is_(None))
                    | (PendingFileDeletion.claimed_at.is_(None))
                    | (PendingFileDeletion.claimed_at < stale_before)
                )
                .order_by(
                    PendingFileDeletion.next_attempt_at,
                    PendingFileDeletion.id,
                )
                .limit(limit)
                .all()
            )
            if not rows:
                return [], 0

            candidate_ids = [int(row.id) for row in rows]
            candidate_paths = [str(row.path) for row in rows]
            referenced_paths = {
                str(row[0])
                for row in (
                    session.query(RadioCall.audio_file_path)
                    .filter(RadioCall.audio_file_path.in_(candidate_paths))
                    .distinct()
                    .all()
                )
            }
            referenced_ids = [
                int(row.id) for row in rows if str(row.path) in referenced_paths
            ]
            if referenced_ids:
                session.query(PendingFileDeletion).filter(
                    PendingFileDeletion.id.in_(referenced_ids)
                ).delete(synchronize_session=False)

            claim_ids = [
                queue_id
                for queue_id, path in zip(candidate_ids, candidate_paths, strict=True)
                if path not in referenced_paths
            ]
            if not claim_ids:
                return [], len(rows)
            (
                session.query(PendingFileDeletion)
                .filter(PendingFileDeletion.id.in_(claim_ids))
                .filter(
                    (PendingFileDeletion.claim_token.is_(None))
                    | (PendingFileDeletion.claimed_at.is_(None))
                    | (PendingFileDeletion.claimed_at < stale_before)
                )
                .update(
                    {
                        PendingFileDeletion.claim_token: claim_token,
                        PendingFileDeletion.claimed_at: now,
                    },
                    synchronize_session=False,
                )
            )
            claimed_rows = (
                session.query(PendingFileDeletion.id, PendingFileDeletion.path)
                .filter(PendingFileDeletion.id.in_(claim_ids))
                .filter(PendingFileDeletion.claim_token == claim_token)
                .all()
            )
            return (
                [(int(row.id), str(row.path)) for row in claimed_rows],
                len(rows),
            )

    def count_pending_file_deletions(self) -> int:
        """Count durable file deletions awaiting acknowledgement."""
        with self.db_manager.get_session() as session:
            return int(session.query(PendingFileDeletion).count())

    def count_due_pending_file_deletions(
        self, stale_claim_seconds: int = 10 * 60
    ) -> int:
        """Count work eligible now, excluding active staging/backoff rows."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=stale_claim_seconds)
        with self.db_manager.get_session() as session:
            return int(
                session.query(PendingFileDeletion)
                .filter(
                    (PendingFileDeletion.next_attempt_at.is_(None))
                    | (PendingFileDeletion.next_attempt_at <= now)
                )
                .filter(
                    (PendingFileDeletion.claim_token.is_(None))
                    | (PendingFileDeletion.claimed_at.is_(None))
                    | (PendingFileDeletion.claimed_at < stale_before)
                )
                .count()
            )

    def seconds_until_next_pending_file_deletion(
        self, stale_claim_seconds: int = 10 * 60
    ) -> float | None:
        """Return the next durable-queue deadline, or ``None`` if empty.

        Unclaimed staging reservations and transient failures are scheduled by
        ``next_attempt_at``. A worker claim becomes eligible again once its
        bounded lease is stale. This lets the server recover upload crash
        residue independently of the optional retention interval.
        """
        if not 60 <= stale_claim_seconds <= 24 * 60 * 60:
            raise ValueError("stale_claim_seconds must be between 60 and 86400")

        now = datetime.now(UTC)

        def as_utc(value: datetime) -> datetime:
            return (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )

        with self.db_manager.get_session() as session:
            unclaimed_without_deadline = (
                session.query(PendingFileDeletion.id)
                .filter(PendingFileDeletion.claim_token.is_(None))
                .filter(PendingFileDeletion.next_attempt_at.is_(None))
                .limit(1)
                .first()
            )
            if unclaimed_without_deadline is not None:
                return 0.0

            next_unclaimed = (
                session.query(func.min(PendingFileDeletion.next_attempt_at))
                .filter(PendingFileDeletion.claim_token.is_(None))
                .scalar()
            )
            claimed_without_timestamp = (
                session.query(PendingFileDeletion.id)
                .filter(PendingFileDeletion.claim_token.isnot(None))
                .filter(PendingFileDeletion.claimed_at.is_(None))
                .limit(1)
                .first()
            )
            if claimed_without_timestamp is not None:
                return 0.0

            oldest_claim = (
                session.query(
                    PendingFileDeletion.claimed_at,
                    PendingFileDeletion.next_attempt_at,
                )
                .filter(PendingFileDeletion.claim_token.isnot(None))
                .filter(PendingFileDeletion.claimed_at.isnot(None))
                .order_by(PendingFileDeletion.claimed_at, PendingFileDeletion.id)
                .limit(1)
                .one_or_none()
            )

        deadlines: list[datetime] = []
        if isinstance(next_unclaimed, datetime):
            deadlines.append(as_utc(next_unclaimed))
        if oldest_claim is not None and isinstance(oldest_claim.claimed_at, datetime):
            claim_deadline = as_utc(oldest_claim.claimed_at) + timedelta(
                seconds=stale_claim_seconds
            )
            if isinstance(oldest_claim.next_attempt_at, datetime):
                claim_deadline = max(
                    claim_deadline, as_utc(oldest_claim.next_attempt_at)
                )
            deadlines.append(claim_deadline)

        if not deadlines:
            return None
        return max(0.0, (min(deadlines) - now).total_seconds())

    def count_failed_pending_file_deletions(self) -> int:
        """Count retained transient failures regardless of their backoff."""
        with self.db_manager.get_session() as session:
            return int(
                session.query(PendingFileDeletion)
                .filter(PendingFileDeletion.attempt_count > 0)
                .count()
            )

    def has_due_pending_file_deletion(self, stale_claim_seconds: int = 10 * 60) -> bool:
        """Check for immediately eligible queue work without counting it."""
        if not 60 <= stale_claim_seconds <= 24 * 60 * 60:
            raise ValueError("stale_claim_seconds must be between 60 and 86400")
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=stale_claim_seconds)
        with self.db_manager.get_session() as session:
            return (
                session.query(PendingFileDeletion.id)
                .filter(
                    (PendingFileDeletion.next_attempt_at.is_(None))
                    | (PendingFileDeletion.next_attempt_at <= now)
                )
                .filter(
                    (PendingFileDeletion.claim_token.is_(None))
                    | (PendingFileDeletion.claimed_at.is_(None))
                    | (PendingFileDeletion.claimed_at < stale_before)
                )
                .limit(1)
                .first()
                is not None
            )

    def has_due_maintenance_work(self, retention_days: int) -> bool:
        """Return whether another bounded catch-up cycle can make progress."""
        if retention_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            with self.db_manager.get_session() as session:
                if (
                    session.query(RadioCall.id)
                    .filter(RadioCall.created_at < cutoff)
                    .limit(1)
                    .first()
                    is not None
                ):
                    return True
                if (
                    session.query(UploadLog.id)
                    .filter(UploadLog.timestamp < cutoff)
                    .limit(1)
                    .first()
                    is not None
                ):
                    return True
        return self.has_due_pending_file_deletion()

    def stage_file_for_storage(
        self,
        path: str,
        grace_seconds: int = STAGED_FILE_GRACE_SECONDS,
    ) -> None:
        """Durably reserve a planned path before it is published.

        If the process dies after publication but before the RadioCall commit,
        the due reservation becomes ordinary deletion work. Missing files are
        harmlessly acknowledged after the same grace period.
        """
        if not 60 <= grace_seconds <= 24 * 60 * 60:
            raise ValueError("grace_seconds must be between 60 and 86400")
        if not path or len(path.encode("utf-8")) > 500:
            raise ValueError("staged path must contain between 1 and 500 bytes")
        queued_at = datetime.now(UTC)
        statement = sqlite_insert(PendingFileDeletion).values(
            path=path,
            queued_at=queued_at,
            kind="staged",
            attempt_count=0,
            next_attempt_at=queued_at + timedelta(seconds=grace_seconds),
        )
        with self.db_manager.get_session() as session:
            session.execute(statement.on_conflict_do_nothing(index_elements=["path"]))

    def get_referenced_audio_paths(self, paths: Collection[str]) -> set[str]:
        """Return queue paths that are currently referenced by a call row."""
        if not paths:
            return set()
        if len(paths) > MAX_RETENTION_BATCH_SIZE:
            raise ValueError(
                f"At most {MAX_RETENTION_BATCH_SIZE} paths may be checked at once"
            )
        with self.db_manager.get_session() as session:
            rows = (
                session.query(RadioCall.audio_file_path)
                .filter(RadioCall.audio_file_path.in_(paths))
                .distinct()
                .all()
            )
            return {str(row[0]) for row in rows}

    def acknowledge_pending_file_deletions(
        self, queue_ids: Sequence[int], claim_token: str
    ) -> int:
        """Remove queue records whose file and directory work fully completed."""
        if not queue_ids:
            return 0
        _validate_retention_batch_size(len(queue_ids))
        if not claim_token or len(claim_token) > 64:
            raise ValueError("claim_token must contain between 1 and 64 characters")
        with self.db_manager.get_session() as session:
            deleted = (
                session.query(PendingFileDeletion)
                .filter(PendingFileDeletion.id.in_(queue_ids))
                .filter(PendingFileDeletion.claim_token == claim_token)
                .delete(synchronize_session=False)
            )
            return int(deleted)

    def release_pending_file_deletion_claims(
        self, queue_ids: Sequence[int], claim_token: str
    ) -> int:
        """Return unprocessed claimed rows to the immediately-due queue."""
        if not queue_ids:
            return 0
        _validate_retention_batch_size(len(queue_ids))
        if not claim_token or len(claim_token) > 64:
            raise ValueError("claim_token must contain between 1 and 64 characters")
        with self.db_manager.get_session() as session:
            updated = (
                session.query(PendingFileDeletion)
                .filter(PendingFileDeletion.id.in_(queue_ids))
                .filter(PendingFileDeletion.claim_token == claim_token)
                .update(
                    {
                        PendingFileDeletion.claim_token: None,
                        PendingFileDeletion.claimed_at: None,
                    },
                    synchronize_session=False,
                )
            )
            return int(updated)

    def record_pending_file_deletion_failures(
        self, failures: Sequence[tuple[int, str]], claim_token: str
    ) -> None:
        """Record transient errors while retaining work for retry."""
        if not failures:
            return
        attempted_at = datetime.now(UTC)
        with self.db_manager.get_session() as session:
            for queue_id, error in failures:
                entry = (
                    session.query(PendingFileDeletion)
                    .filter(PendingFileDeletion.id == queue_id)
                    .filter(PendingFileDeletion.claim_token == claim_token)
                    .one_or_none()
                )
                if entry is None:
                    continue
                attempt_count = int(entry.attempt_count or 0) + 1
                delay_seconds = min(
                    30 * (2 ** min(max(attempt_count - 1, 0), 10)),
                    6 * 60 * 60,
                )
                entry.attempt_count = attempt_count  # type: ignore[assignment]
                entry.last_attempt_at = attempted_at  # type: ignore[assignment]
                entry.next_attempt_at = attempted_at + timedelta(  # type: ignore[assignment]
                    seconds=delay_seconds
                )
                entry.claim_token = None  # type: ignore[assignment]
                entry.claimed_at = None  # type: ignore[assignment]
                entry.last_error = sanitize_log_value(  # type: ignore[assignment]
                    error, maximum=512
                )

    def cleanup_old_data(self, days_to_keep: int) -> None:
        """Clean up old data from the database.

        Args:
            days_to_keep: Number of days of data to keep
        """
        if days_to_keep < 1:
            raise ValueError("days_to_keep must be at least 1")

        cutoff_date = datetime.now(UTC) - timedelta(days=days_to_keep)

        deleted_calls = 0
        deleted_logs = 0
        while True:
            batch = self.delete_calls_older_than(cutoff_date)
            deleted_calls += batch
            if batch < DEFAULT_RETENTION_BATCH_SIZE:
                break
        while True:
            batch = self.delete_upload_logs_older_than(cutoff_date)
            deleted_logs += batch
            if batch < DEFAULT_RETENTION_BATCH_SIZE:
                break

        logger.info(f"Cleaned up old data: {deleted_calls} calls, {deleted_logs} logs")

        # Vacuum database to reclaim space, then remove stale WAL contents.
        self.db_manager.vacuum()
        self.db_manager.checkpoint(truncate=True)

    def query_calls(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "call_timestamp",
        sort_order: str = "desc",
        allowed_systems: Collection[str] | None = None,
    ) -> dict[str, Any]:
        """Query radio calls with filtering and pagination.

        Args:
            filters: Filter criteria
            page: Page number (1-based)
            per_page: Items per page
            sort_by: Field to sort by
            sort_order: Sort order (asc/desc)
            allowed_systems: Optional system IDs visible to the caller

        Returns:
            Dictionary with calls, total count, and pagination info
        """
        with (
            self.db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            query = session.query(RadioCall)

            if allowed_systems is not None:
                query = query.filter(RadioCall.system_id.in_(allowed_systems))

            # Apply filters
            if filters:
                if "system_id" in filters:
                    query = query.filter(RadioCall.system_id == filters["system_id"])
                if "talkgroup_id" in filters:
                    query = query.filter(
                        RadioCall.talkgroup_id == filters["talkgroup_id"]
                    )
                if "source_radio_id" in filters:
                    query = query.filter(
                        RadioCall.source_radio_id == filters["source_radio_id"]
                    )
                if "frequency" in filters:
                    query = query.filter(RadioCall.frequency == filters["frequency"])
                if "date_from" in filters:
                    query = query.filter(
                        RadioCall.call_timestamp >= filters["date_from"]
                    )
                if "date_to" in filters:
                    query = query.filter(RadioCall.call_timestamp <= filters["date_to"])

            # Get total count
            total = query.count()

            # Apply sorting
            sort_column = getattr(RadioCall, sort_by, RadioCall.call_timestamp)
            if sort_order == "desc":
                query = query.order_by(desc(sort_column))
            else:
                query = query.order_by(sort_column)

            # Apply pagination
            offset = (page - 1) * per_page
            if offset > MAX_QUERY_OFFSET_ROWS:
                raise ValueError(
                    f"Query offset may not exceed {MAX_QUERY_OFFSET_ROWS:,} rows"
                )
            query = query.offset(offset).limit(per_page)

            # Execute query
            calls = query.all()

            # Convert to dict
            result_calls = []
            for call in calls:
                result_calls.append(
                    {
                        "id": call.id,
                        "call_timestamp": call.call_timestamp,
                        "system_id": call.system_id,
                        "system_label": call.system_label,
                        "talkgroup_id": call.talkgroup_id,
                        "talkgroup_label": call.talkgroup_label,
                        "frequency": call.frequency,
                        "source_radio_id": call.source_radio_id,
                        "talker_alias": call.talker_alias,
                        "audio_filename": call.audio_filename,
                        "audio_size_bytes": call.audio_size_bytes,
                        "audio_file_path": call.audio_file_path,
                    }
                )

            total_pages = (total + per_page - 1) // per_page

            return {
                "calls": result_calls,
                "total": total,
                "total_pages": total_pages,
            }

    def get_call_by_id(
        self, call_id: int, allowed_systems: Collection[str] | None = None
    ) -> dict[str, Any] | None:
        """Get a specific call by ID.

        Args:
            call_id: Database ID of the call
            allowed_systems: Optional system IDs visible to the caller

        Returns:
            Call data or None if not found
        """
        with self.db_manager.get_session() as session:
            query = session.query(RadioCall).filter(RadioCall.id == call_id)
            if allowed_systems is not None:
                query = query.filter(RadioCall.system_id.in_(allowed_systems))
            call = query.first()

            if not call:
                return None

            return {
                "id": call.id,
                "call_timestamp": call.call_timestamp,
                "system_id": call.system_id,
                "system_label": call.system_label,
                "talkgroup_id": call.talkgroup_id,
                "talkgroup_label": call.talkgroup_label,
                "frequency": call.frequency,
                "source_radio_id": call.source_radio_id,
                "talker_alias": call.talker_alias,
                "audio_filename": call.audio_filename,
                "audio_size_bytes": call.audio_size_bytes,
                "audio_file_path": call.audio_file_path,
                "patches": call.patches,
                "frequencies": call.frequencies,
                "sources": call.sources,
                "created_at": call.created_at,
                "upload_ip": call.upload_ip,
            }

    def get_systems_summary(
        self,
        allowed_systems: Collection[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get summary statistics for all systems.

        Returns:
            List of system summaries
        """
        with (
            self.db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            systems_query = session.query(
                RadioCall.system_id,
                RadioCall.system_label,
                func.count(RadioCall.id).label("total_calls"),
                func.min(RadioCall.call_timestamp).label("first_seen"),
                func.max(RadioCall.call_timestamp).label("last_seen"),
            )
            if allowed_systems is not None:
                systems_query = systems_query.filter(
                    RadioCall.system_id.in_(allowed_systems)
                )
            systems = (
                systems_query.group_by(RadioCall.system_id, RadioCall.system_label)
                .order_by(desc("total_calls"))
                .limit(limit)
                .all()
            )
            returned_systems = [system.system_id for system in systems]

            # Top talkgroups for all systems in one window-function query
            # instead of one query per system (N+1).
            rank = (
                func.row_number()
                .over(
                    partition_by=RadioCall.system_id,
                    order_by=desc(func.count(RadioCall.id)),
                )
                .label("rank")
            )
            tg_counts_query = session.query(
                RadioCall.system_id.label("system_id"),
                RadioCall.talkgroup_id.label("talkgroup_id"),
                func.count(RadioCall.id).label("count"),
                rank,
            )
            if allowed_systems is not None:
                tg_counts_query = tg_counts_query.filter(
                    RadioCall.system_id.in_(allowed_systems)
                )
            tg_counts_query = tg_counts_query.filter(
                RadioCall.system_id.in_(returned_systems)
            )
            tg_counts = (
                tg_counts_query.filter(RadioCall.talkgroup_id.isnot(None))
                .group_by(RadioCall.system_id, RadioCall.talkgroup_id)
                .subquery()
            )
            top_rows = (
                session.query(tg_counts)
                .filter(tg_counts.c.rank <= 10)
                .order_by(tg_counts.c.system_id, tg_counts.c.rank)
                .all()
            )

            top_by_system: dict[str, dict[str, int]] = {}
            for row in top_rows:
                top_by_system.setdefault(row.system_id, {})[
                    str(row.talkgroup_id)
                ] = row.count

            return [
                {
                    "system_id": system.system_id,
                    "system_label": system.system_label,
                    "total_calls": system.total_calls,
                    "first_seen": system.first_seen,
                    "last_seen": system.last_seen,
                    "top_talkgroups": top_by_system.get(system.system_id, {}),
                }
                for system in systems
            ]

    def get_talkgroups_summary(
        self,
        system_id: str | None = None,
        min_calls: int = 1,
        allowed_systems: Collection[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get summary statistics for talkgroups.

        Args:
            system_id: Optional system ID to filter by
            min_calls: Minimum number of calls to include
            allowed_systems: Optional system IDs visible to the caller

        Returns:
            List of talkgroup summaries
        """
        with (
            self.db_manager.get_session() as session,
            expensive_query_deadline(session),
        ):
            query = session.query(
                RadioCall.talkgroup_id,
                RadioCall.talkgroup_label,
                RadioCall.system_id,
                func.count(RadioCall.id).label("total_calls"),
                func.max(RadioCall.call_timestamp).label("last_heard"),
            ).filter(RadioCall.talkgroup_id.isnot(None))

            if allowed_systems is not None:
                query = query.filter(RadioCall.system_id.in_(allowed_systems))

            if system_id:
                query = query.filter(RadioCall.system_id == system_id)

            query = (
                query.group_by(
                    RadioCall.talkgroup_id,
                    RadioCall.talkgroup_label,
                    RadioCall.system_id,
                )
                .having(func.count(RadioCall.id) >= min_calls)
                .order_by(desc("total_calls"))
                .limit(limit)
            )

            talkgroups = query.all()

            return [
                {
                    "talkgroup_id": tg.talkgroup_id,
                    "talkgroup_label": tg.talkgroup_label,
                    "system_id": tg.system_id,
                    "total_calls": tg.total_calls,
                    "last_heard": tg.last_heard,
                }
                for tg in talkgroups
            ]
