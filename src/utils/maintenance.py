"""Bounded, retry-safe retention and temporary-file maintenance."""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypeVar

from ..database.operations import (
    DEFAULT_RETENTION_BATCH_SIZE,
    MAX_RETENTION_BATCH_SIZE,
    DatabaseOperations,
)
from ..security.text import sanitize_log_value
from .file_handler import FileDeletionResult, FileHandler

logger = logging.getLogger(__name__)

DEFAULT_MAX_DATABASE_BATCHES = 20
DEFAULT_MAX_FILE_BATCHES = 40
DEFAULT_DIRECTORY_WORK_BUDGET = 1000
DEFAULT_TEMP_WORK_BUDGET = 1000
_ResultT = TypeVar("_ResultT")


def _validate_work_limit(value: int, name: str, maximum: int = 10_000) -> None:
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _run_bounded_state_write(
    db_ops: DatabaseOperations,
    file_handler: FileHandler,
    operation: Callable[[], _ResultT],
) -> _ResultT:
    """Run one bounded SQLite phase while its WAL headroom is reserved."""
    with file_handler.maintenance_state_guard():
        operation_error: BaseException | None = None
        try:
            return operation()
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            try:
                # Truncating after every bounded phase prevents several valid
                # batches in one cycle from cumulatively spending the reserve.
                checkpointed = db_ops.db_manager.checkpoint(truncate=True)
                if not checkpointed:
                    if operation_error is None:
                        raise RuntimeError(
                            "SQLite WAL checkpoint is busy; maintenance must retry"
                        )
                    logger.error(
                        "SQLite WAL checkpoint remained busy after state write error"
                    )
            except Exception:
                if operation_error is None:
                    raise
                logger.exception("WAL checkpoint also failed after state write error")


def _process_pending_file_deletions(
    db_ops: DatabaseOperations,
    file_handler: FileHandler,
    batch_size: int,
    directory_work_budget: int,
) -> tuple[int, int, int, int, bool]:
    """Process one durable queue batch and retain transient failures."""
    claim_token = secrets.token_hex(16)
    pending, examined = db_ops.claim_pending_file_deletions(
        claim_token, limit=batch_size
    )
    acknowledged: list[int] = []
    failures: list[tuple[int, str]] = []
    deferred: list[int] = []
    deleted_files = 0
    freed_bytes = 0
    directory_work_used = 0
    budget_exhausted = False

    for index, (queue_id, path) in enumerate(pending):
        prune_work = file_handler.directory_prune_work(path)
        if (
            prune_work is not None
            and prune_work > directory_work_budget - directory_work_used
        ):
            # Do not unlink a file unless this cycle can also attempt its full
            # parent chain. Releasing every unprocessed claim keeps the work
            # durable and immediately eligible for the next bounded cycle.
            deferred.extend(item_id for item_id, _ in pending[index:])
            budget_exhausted = True
            break

        try:
            # The DB claim was committed before this unlink. A concurrent
            # upload may consume only an unclaimed stage row, so it cannot add
            # a live reference after this worker wins the claim.
            result = file_handler.delete_file(path)
        except Exception as exc:  # one broken entry must not starve the batch
            result = FileDeletionResult(
                "retry",
                error=f"{type(exc).__name__}: {sanitize_log_value(exc)}",
            )

        if result.status in {"retry", "refused"}:
            # Refused containment checks can indicate a legitimate storage-root
            # migration as well as corrupt input. Keep the durable record for
            # explicit operator remediation instead of silently abandoning it.
            failures.append(
                (
                    queue_id,
                    result.error
                    or (
                        "path is outside or unsafe beneath the active storage root"
                        if result.status == "refused"
                        else "transient deletion failure"
                    ),
                )
            )
            continue

        if result.status == "deleted":
            deleted_files += 1
            freed_bytes += result.freed_bytes

        if prune_work is None:
            # Defensive fail-closed behavior if a FileHandler implementation
            # ever reports a terminal result for an unsafe/outside-root path.
            failures.append(
                (queue_id, "terminal path could not be mapped beneath storage root")
            )
            continue

        try:
            if prune_work:
                file_handler.remove_empty_directories(
                    [path], work_budget=prune_work, require_complete=True
                )
        except Exception as exc:
            # The unlink is idempotent. Retain the queue row so a later cycle
            # observes the missing file and retries its directory pruning.
            failures.append(
                (
                    queue_id,
                    "directory pruning failed: "
                    f"{type(exc).__name__}: {sanitize_log_value(exc)}",
                )
            )
            directory_work_used += prune_work
            continue

        directory_work_used += prune_work
        # Deleted and already-missing entries become terminal only after the
        # complete known parent chain has received its bounded prune attempt.
        acknowledged.append(queue_id)

    # A failed acknowledgement leaves idempotent work in the queue. A deleted
    # file is observed as missing on the next cycle, so DB failure cannot make
    # a live row point at a file that retention already removed.
    db_ops.acknowledge_pending_file_deletions(acknowledged, claim_token)
    db_ops.record_pending_file_deletion_failures(failures, claim_token)
    db_ops.release_pending_file_deletion_claims(deferred, claim_token)
    return (
        deleted_files,
        freed_bytes,
        examined,
        directory_work_used,
        budget_exhausted,
    )


def run_retention_cleanup(
    db_ops: DatabaseOperations,
    file_handler: FileHandler,
    retention_days: int,
    vacuum: bool = False,
    *,
    database_batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    max_database_batches: int = DEFAULT_MAX_DATABASE_BATCHES,
    file_batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    max_file_batches: int = DEFAULT_MAX_FILE_BATCHES,
    directory_work_budget: int = DEFAULT_DIRECTORY_WORK_BUDGET,
) -> dict[str, Any]:
    """Run one bounded retention cycle with durable file-deletion retries.

    Expired call rows and their now-unreferenced paths are committed to SQLite
    in one transaction. Files are unlinked only afterwards through
    :class:`FileHandler`; transient failures remain queued for later cycles.
    Upload logs and call rows are selected and deleted in bounded batches.

    A retention value of zero disables expiry selection, but pending file work
    and the WAL checkpoint still run so configuration changes cannot strand
    previously committed deletion work.
    """
    _validate_work_limit(
        database_batch_size, "database_batch_size", MAX_RETENTION_BATCH_SIZE
    )
    _validate_work_limit(max_database_batches, "max_database_batches")
    _validate_work_limit(file_batch_size, "file_batch_size", MAX_RETENTION_BATCH_SIZE)
    _validate_work_limit(max_file_batches, "max_file_batches")
    _validate_work_limit(directory_work_budget, "directory_work_budget")

    summary: dict[str, Any] = {
        "deleted_calls": 0,
        "deleted_upload_logs": 0,
        "deleted_files": 0,
        "freed_bytes": 0,
    }
    cleanup_error: Exception | None = None
    remaining_directory_work = directory_work_budget

    if retention_days > 0:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            for _ in range(max_database_batches):
                deleted = _run_bounded_state_write(
                    db_ops,
                    file_handler,
                    lambda: db_ops.queue_and_delete_expired_calls(
                        cutoff, batch_size=database_batch_size
                    ),
                )
                summary["deleted_calls"] += deleted
                if deleted < database_batch_size:
                    break

            for _ in range(max_database_batches):
                deleted = _run_bounded_state_write(
                    db_ops,
                    file_handler,
                    lambda: db_ops.delete_upload_logs_older_than(
                        cutoff, batch_size=database_batch_size
                    ),
                )
                summary["deleted_upload_logs"] += deleted
                if deleted < database_batch_size:
                    break
        except Exception as exc:
            cleanup_error = exc

    # Existing queue work is attempted even if selecting new expired rows
    # failed. This prevents an unrelated bad row/database error from starving
    # durable unlink retries forever.
    try:
        for _ in range(max_file_batches):
            (
                deleted_files,
                freed_bytes,
                processed,
                directory_work_used,
                budget_exhausted,
            ) = _run_bounded_state_write(
                db_ops,
                file_handler,
                partial(
                    _process_pending_file_deletions,
                    db_ops,
                    file_handler,
                    file_batch_size,
                    remaining_directory_work,
                ),
            )
            summary["deleted_files"] += deleted_files
            summary["freed_bytes"] += freed_bytes
            remaining_directory_work -= directory_work_used
            if budget_exhausted or processed < file_batch_size:
                break
    except Exception as exc:
        if cleanup_error is None:
            cleanup_error = exc
        else:
            logger.exception("Pending file deletion also failed", exc_info=exc)

    try:
        if vacuum and (summary["deleted_calls"] or summary["deleted_upload_logs"]):
            db_ops.db_manager.vacuum()
    except Exception as exc:
        if cleanup_error is None:
            cleanup_error = exc
        else:
            logger.exception("Database vacuum also failed", exc_info=exc)

    if cleanup_error is not None:
        raise cleanup_error

    if any(summary.values()):
        logger.info("Retention cleanup (%dd): %s", retention_days, summary)
    return summary


def run_temp_cleanup(
    file_handler: FileHandler,
    max_age_hours: int = 1,
    work_budget: int = DEFAULT_TEMP_WORK_BUDGET,
) -> int:
    """Remove a bounded number of stale application-owned upload temp files."""
    return file_handler.cleanup_temp_files(
        max_age_hours=max_age_hours, work_budget=work_budget
    )
