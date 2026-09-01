"""FastAPI application factory and setup."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .. import __version__
from ..config import Config, prepare_private_directory, setup_logging
from ..database import DatabaseManager, DatabaseOperations
from ..database.operations import ExpensiveQueryTimeout
from ..exceptions import ConfigurationError
from ..middleware import RateLimitMiddleware
from ..middleware.rate_limiter import (
    account_route_validation_failure,
    get_active_limits,
    get_limiter,
)
from ..middleware.security import SecurityHeadersMiddleware
from ..middleware.validation import RequestValidationMiddleware
from ..models.api_models import HealthCheckResponse, StatisticsResponse
from ..security.auth import authenticate_read_request
from ..security.text import sanitize_log_value
from ..utils.file_handler import FileHandler
from ..utils.maintenance import run_retention_cleanup, run_temp_cleanup
from ..utils.storage_quota import StorageCapacity
from .query import (
    _acquire_expensive_read,
    _ExpensiveReadLease,
)
from .query import router as query_router
from .rdioscanner import router as rdioscanner_router

logger = logging.getLogger(__name__)
limiter = get_limiter()
MAX_MAINTENANCE_CATCH_UP_CYCLES = 4
MAINTENANCE_CATCH_UP_DELAY_SECONDS = 5.0
MAINTENANCE_IDLE_POLL_SECONDS = 60.0
MAINTENANCE_RECONCILIATION_SLICE_DELAY_SECONDS = 0.05
STARTUP_MAINTENANCE_WORK_BUDGET = 100
MAX_HEALTH_SINGLEFLIGHT_WAITERS = 32
MAX_METRICS_SINGLEFLIGHT_WAITERS = 32
api_key_header = Header(
    None,
    alias="X-API-Key",
    description="API key used to authenticate and scope this metrics request",
)


def _cache_monotonic() -> float:
    """Return the cache clock through a narrow, testable indirection."""
    return time.monotonic()


@asynccontextmanager
async def _lifespan_impl(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan manager."""
    # Startup
    app.state.monitoring_shutting_down = False
    app.state.file_logging_initialized = False

    # Initialize components
    try:
        config = Config.model_validate(app.state.config.model_dump())
    except ValueError as exc:
        raise ConfigurationError(
            "Application configuration became invalid before startup"
        ) from exc
    app.state.config = config

    # Database
    db_manager = DatabaseManager(
        config.database.path,
        enable_wal=config.database.enable_wal,
        echo=config.server.debug,
        exclusive_process_lock=True,
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
    app.state.db_manager = db_manager
    app.state.db_ops = DatabaseOperations(db_manager)

    # Open the process-local rotating log only after the database process lock
    # is held. A losing second server must never append to or rotate the live
    # service's audit log before startup fails closed.
    setup_logging(config.logging)
    app.state.file_logging_initialized = True
    logger.info("Starting sdrtrunk-rdio-api...")

    # File handler
    file_handler = FileHandler(
        storage_directory=config.file_handling.storage.directory,
        temp_directory=config.file_handling.temp_directory,
        organize_by_date=config.file_handling.storage.organize_by_date,
        accepted_formats=config.file_handling.accepted_formats,
        max_file_size_mb=config.file_handling.max_file_size_mb,
        min_file_size_kb=config.file_handling.min_file_size_kb,
    )
    app.state.file_handler = file_handler

    state_directories = [db_manager.database_path.parent]
    if config.logging.file.enabled:
        # setup_logging validated and created this path after the process lock
        # was acquired. Reuse the
        # same trusted-prefix canonicalization so platform aliases such as
        # macOS /var -> /private/var are represented by a real directory for
        # no-follow device/capacity probes.
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
        minimum_free_bytes=config.file_handling.minimum_free_space_mb * 1024 * 1024,
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
        # The exact no-follow archive scan runs in the maintenance worker so
        # a very large existing archive cannot block application startup.
        scan_on_initialize=False,
    )
    file_handler.attach_storage_capacity(storage_capacity)
    app.state.storage_capacity = storage_capacity

    # Background maintenance: enforces retention_days (calls + audio +
    # upload logs) and cleans stale temp files. Without this the
    # documented retention config would do nothing.
    db_ops = app.state.db_ops
    retention_days = config.file_handling.storage.retention_days
    interval_hours = config.file_handling.storage.cleanup_interval_hours

    async def run_maintenance_worker(function: Any, *args: Any, **kwargs: Any) -> Any:
        """Keep a thread worker alive and joined if lifespan is cancelled."""
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        app.state.maintenance_worker_task = worker
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # asyncio.to_thread cannot cancel a running function. Await it to
            # completion before DatabaseManager is disposed during shutdown.
            try:
                await worker
            except Exception as exc:
                logger.error(
                    "Maintenance worker failed during shutdown (%s): %s",
                    type(exc).__name__,
                    sanitize_log_value(exc),
                )
            raise
        finally:
            if app.state.maintenance_worker_task is worker:
                app.state.maintenance_worker_task = None

    async def maintenance_loop() -> None:
        reconciliation_interval = (
            interval_hours * 3600 if interval_hours > 0 else 6 * 3600
        )
        reconciled = False
        try:
            reconciled = bool(await run_maintenance_worker(storage_capacity.reconcile))
            if not reconciled:
                if storage_capacity.snapshot.reconciliation_pending:
                    logger.info(
                        "Initial storage accounting scan is continuing in "
                        "bounded background slices"
                    )
                else:
                    logger.error(
                        "Initial storage accounting scan was incomplete; "
                        "upload admission remains closed"
                    )
        except Exception as exc:
            logger.error(
                "Initial storage accounting scan failed (%s): %s",
                type(exc).__name__,
                sanitize_log_value(exc),
            )
        # A transient startup race with an archive mutation must not leave
        # fail-closed ingestion waiting for the normal (possibly very long)
        # reconciliation interval before it can recover.
        initial_reconciliation_delay = (
            reconciliation_interval
            if reconciled
            else (
                MAINTENANCE_RECONCILIATION_SLICE_DELAY_SECONDS
                if storage_capacity.snapshot.reconciliation_pending
                else MAINTENANCE_IDLE_POLL_SECONDS
            )
        )
        next_reconciliation_at = time.monotonic() + initial_reconciliation_delay
        next_retention_at = time.monotonic() if interval_hours > 0 else None
        retention_catch_up = False
        while True:
            catch_up_needed = False
            retry_delay = MAINTENANCE_IDLE_POLL_SECONDS
            try:
                monotonic_now = time.monotonic()
                if monotonic_now >= next_reconciliation_at:
                    reconciled = bool(
                        await run_maintenance_worker(storage_capacity.reconcile)
                    )
                    next_reconciliation_at = time.monotonic() + (
                        reconciliation_interval
                        if reconciled
                        else (
                            MAINTENANCE_RECONCILIATION_SLICE_DELAY_SECONDS
                            if storage_capacity.snapshot.reconciliation_pending
                            else MAINTENANCE_IDLE_POLL_SECONDS
                        )
                    )
                retention_due = retention_catch_up or (
                    next_retention_at is not None and monotonic_now >= next_retention_at
                )
                queue_due = bool(
                    await run_maintenance_worker(db_ops.has_due_maintenance_work, 0)
                )

                if retention_due:
                    await run_maintenance_worker(run_temp_cleanup, file_handler)

                if retention_due or queue_due:
                    cycle_retention_days = retention_days if retention_due else 0
                    for _ in range(MAX_MAINTENANCE_CATCH_UP_CYCLES):
                        # Zero disables selection of newly expired rows, not
                        # durable queue retries or the mandatory checkpoint.
                        await run_maintenance_worker(
                            run_retention_cleanup,
                            db_ops,
                            file_handler,
                            cycle_retention_days,
                        )
                        catch_up_needed = bool(
                            await run_maintenance_worker(
                                db_ops.has_due_maintenance_work,
                                cycle_retention_days,
                            )
                        )
                        if not catch_up_needed:
                            break
                        # Give request handlers a scheduling opportunity
                        # between individually bounded maintenance cycles.
                        await asyncio.sleep(0)

                    if retention_due:
                        retention_catch_up = catch_up_needed
                        if not retention_catch_up and interval_hours > 0:
                            next_retention_at = time.monotonic() + interval_hours * 3600

                queue_delay = await run_maintenance_worker(
                    db_ops.seconds_until_next_pending_file_deletion
                )
                delay_candidates = [MAINTENANCE_IDLE_POLL_SECONDS]
                if queue_delay is not None:
                    delay_candidates.append(max(0.0, float(queue_delay)))
                if next_retention_at is not None:
                    delay_candidates.append(
                        max(0.0, next_retention_at - time.monotonic())
                    )
                delay_candidates.append(
                    max(0.0, next_reconciliation_at - time.monotonic())
                )
                retry_delay = min(delay_candidates)
            except Exception as exc:
                catch_up_needed = True
                logger.error(
                    "Maintenance cycle failed (%s): %s",
                    type(exc).__name__,
                    sanitize_log_value(exc),
                )
            await asyncio.sleep(
                MAINTENANCE_CATCH_UP_DELAY_SECONDS if catch_up_needed else retry_delay
            )

    maintenance_task: asyncio.Task[None] | None = None
    # Always perform one bounded crash-recovery pass. Periodic scheduling may
    # be disabled, but staged files from an interrupted prior process and WAL
    # remnants must still be reconciled on the next startup.
    try:
        await run_maintenance_worker(
            run_temp_cleanup,
            file_handler,
            1,
            STARTUP_MAINTENANCE_WORK_BUDGET,
        )
        await run_maintenance_worker(
            run_retention_cleanup,
            db_ops,
            file_handler,
            0,
            database_batch_size=STARTUP_MAINTENANCE_WORK_BUDGET,
            max_database_batches=1,
            file_batch_size=STARTUP_MAINTENANCE_WORK_BUDGET,
            max_file_batches=1,
            directory_work_budget=STARTUP_MAINTENANCE_WORK_BUDGET,
        )
    except Exception as exc:
        logger.error(
            "Startup maintenance recovery failed (%s): %s",
            type(exc).__name__,
            sanitize_log_value(exc),
        )

    # Queue recovery is mandatory even when scheduled retention is disabled:
    # a future staged-file deadline may be the only record of a crash orphan.
    maintenance_task = asyncio.create_task(maintenance_loop())
    app.state.maintenance_task = maintenance_task
    if interval_hours > 0:
        logger.info(
            f"Maintenance task scheduled every {interval_hours}h "
            f"(retention: {retention_days} days)"
        )
    else:
        logger.info("Scheduled durable upload-file recovery; retention is disabled")

    logger.info("sdrtrunk-rdio-api started successfully")

    yield


async def _cleanup_lifespan_resources(app: FastAPI) -> None:
    """Release all successfully initialized resources after any exit path."""
    logger.info("Shutting down sdrtrunk-rdio-api...")

    # Stop admission before taking the snapshot. Loader tasks are shielded from
    # disconnected request waiters because asyncio.to_thread cannot stop their
    # SQLite/filesystem work. Join them while DB/capacity resources and the
    # database process lock are still valid.
    app.state.monitoring_shutting_down = True
    monitoring_tasks = getattr(app.state, "monitoring_tasks", None)
    if monitoring_tasks:
        tasks = tuple(monitoring_tasks)
        joined = asyncio.gather(*tasks, return_exceptions=True)
        try:
            results = await asyncio.shield(joined)
        except asyncio.CancelledError:
            # A second shutdown cancellation cannot let worker threads escape
            # the database lock's lifetime. The original lifespan cancellation
            # (if any) continues propagating after this finally block.
            results = await joined
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error(
                    "Monitoring worker failed during shutdown (%s)",
                    type(result).__name__,
                )
        monitoring_tasks.clear()

    storage_capacity = getattr(app.state, "storage_capacity", None)
    if storage_capacity is not None:
        try:
            # Let a large reconciliation observe shutdown before its task is
            # cancelled and joins the cancellation-safe worker wrapper.
            storage_capacity.close()
        except Exception as exc:
            logger.error("Storage capacity cleanup failed (%s)", type(exc).__name__)

    maintenance_task = getattr(app.state, "maintenance_task", None)
    if maintenance_task is not None:
        try:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
        except Exception as exc:
            logger.error("Maintenance task cleanup failed (%s)", type(exc).__name__)
        finally:
            app.state.maintenance_task = None

    file_handler = getattr(app.state, "file_handler", None)
    if file_handler is not None:
        try:
            file_handler.close()
        except Exception as exc:
            logger.error("File handler cleanup failed (%s)", type(exc).__name__)
        finally:
            app.state.file_handler = None

    db_manager = getattr(app.state, "db_manager", None)
    # Finish and close file logging while the database process lock is still
    # held. CLI commands use console-only logging, so no second writer can race
    # RotatingFileHandler's rename sequence over this lifetime boundary.
    logger.info("sdrtrunk-rdio-api shutdown complete")
    if getattr(app.state, "file_logging_initialized", False):
        root_logger = logging.getLogger()
        for handler in tuple(root_logger.handlers):
            if isinstance(handler, logging.FileHandler):
                root_logger.removeHandler(handler)
                try:
                    handler.flush()
                    handler.close()
                except Exception as exc:
                    logger.warning(
                        "File log handler cleanup failed (%s)",
                        type(exc).__name__,
                    )
        app.state.file_logging_initialized = False

    if db_manager is not None:
        try:
            db_manager.close()
        except Exception as exc:
            logger.error("Database cleanup failed (%s)", type(exc).__name__)
        finally:
            app.state.db_manager = None
            app.state.db_ops = None

    app.state.storage_capacity = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Guarantee reverse-order cleanup on startup, runtime, and shutdown errors."""
    try:
        async with _lifespan_impl(app):
            yield
    finally:
        await _cleanup_lifespan_resources(app)


def create_app(
    config_path: str = "config.yaml", override_config: Config | None = None
) -> FastAPI:
    """Create and configure FastAPI application.

    Args:
        config_path: Path to configuration file
        override_config: Optional config object to use instead of loading from file

    Returns:
        Configured FastAPI app
    """
    # Load configuration
    if override_config is not None:
        try:
            config = Config.model_validate(override_config.model_dump())
        except ValueError as exc:
            raise ConfigurationError("Invalid override configuration") from exc
    else:
        config = Config.load_from_file(config_path, require_exists=True)

    # The RdioScanner protocol handler treats an empty key list as open access.
    # Require an explicit opt-in so missing credentials can never silently
    # expose the write endpoint.
    if (
        not config.security.api_keys
        and not config.security.allow_unauthenticated_uploads
    ):
        raise ConfigurationError(
            "No API keys are configured. Configure at least one key or set "
            "security.allow_unauthenticated_uploads=true explicitly."
        )

    # Create app with comprehensive documentation
    app = FastAPI(
        title="sdrtrunk-rdio-api",
        description="""## Professional Radio Scanner API Server

A high-performance API server for receiving, storing, and managing radio scanner audio calls from SDRTrunk.

### Features
- 📡 **RdioScanner Protocol Support** - Full compatibility with SDRTrunk's RdioScanner upload format
- 🚀 **HTTP/2 Support** - Built on Hypercorn for modern protocol support
- 🔒 **Security First** - Rate limiting, input validation, security headers
- 📊 **Real-time Metrics** - System statistics and monitoring endpoints
- 🗄️ **Organized Storage** - Date-based directory structure with metadata-rich filenames
- ⚡ **High Performance** - Async operations, connection pooling, optimized queries

### API Sections
- **Upload** - Submit radio calls with audio and metadata
- **Health** - Service health monitoring
- **Metrics** - System statistics and performance metrics
        """,
        version=__version__,
        docs_url="/docs" if config.server.enable_docs else None,
        redoc_url="/redoc" if config.server.enable_docs else None,
        openapi_tags=[
            {"name": "upload", "description": "Radio call upload endpoints"},
            {"name": "health", "description": "Health check endpoints"},
            {"name": "metrics", "description": "Statistics and metrics endpoints"},
        ],
        lifespan=lifespan,
    )

    # Store config in app state
    app.state.config = config
    app.state.monitoring_shutting_down = False
    monitoring_tasks: set[asyncio.Task[Any]] = set()
    app.state.monitoring_tasks = monitoring_tasks

    def track_monitoring_task(task: asyncio.Task[Any]) -> None:
        """Keep detached singleflight work inside the lifespan boundary."""
        monitoring_tasks.add(task)
        task.add_done_callback(monitoring_tasks.discard)

    def reject_monitoring_during_shutdown() -> None:
        if app.state.monitoring_shutting_down:
            raise HTTPException(
                status_code=503,
                detail="Service is shutting down",
                headers={"Retry-After": "1"},
            )

    # Starlette's CORS layer short-circuits preflight requests. Add it first so
    # request validation/rate accounting wraps it; malformed 4xx responses may
    # intentionally omit CORS headers. Security headers remain outermost.
    if config.server.cors_origins:
        allow_credentials = "*" not in config.server.cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(RequestValidationMiddleware)

    # Add security headers middleware
    custom_security_headers = None
    if config.server.ssl_cert and config.server.ssl_key:
        custom_security_headers = {
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains"
        }
    app.add_middleware(
        SecurityHeadersMiddleware, custom_headers=custom_security_headers
    )

    # Configure rate limiting
    rate_limiter = RateLimitMiddleware(app, config)
    app.state.rate_limiter = rate_limiter

    # Add routers
    app.include_router(rdioscanner_router)
    app.include_router(query_router)

    # Add monitoring endpoints
    if config.monitoring.health_check.enabled:
        health_cache_lock = asyncio.Lock()
        health_cache_expires_at = 0.0
        health_cache_connected = False
        health_inflight: asyncio.Task[bool] | None = None
        capacity_inflight: asyncio.Task[bool] | None = None
        health_waiters = 0

        async def load_database_health() -> bool:
            """Run one cached database probe without occupying the event loop."""
            nonlocal health_cache_connected, health_cache_expires_at, health_inflight
            current_task = asyncio.current_task()
            try:
                db_manager: DatabaseManager = app.state.db_manager
                connected = await asyncio.to_thread(db_manager.check_connection)
                async with health_cache_lock:
                    health_cache_connected = connected
                    # Start the freshness window after a potentially slow probe.
                    health_cache_expires_at = _cache_monotonic() + 2.0
                return connected
            finally:
                async with health_cache_lock:
                    if health_inflight is current_task:
                        health_inflight = None

        async def load_capacity_health() -> bool:
            """Singleflight a fresh filesystem-capacity readiness probe."""
            nonlocal capacity_inflight
            current_task = asyncio.current_task()
            try:
                capacity: StorageCapacity = app.state.storage_capacity
                return await asyncio.to_thread(capacity.ready_for_upload)
            finally:
                async with health_cache_lock:
                    if capacity_inflight is current_task:
                        capacity_inflight = None

        def observe_health_task(task: asyncio.Task[bool]) -> None:
            """Retrieve a singleflight failure if all waiting clients leave."""
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

        @app.get(
            config.monitoring.health_check.path,
            response_model=HealthCheckResponse,
            tags=["health"],
            summary="Health Check",
            description="Check the health status of the API and its dependencies",
            responses={
                200: {
                    "description": "Service is healthy",
                    "content": {
                        "application/json": {
                            "example": {
                                "status": "healthy",
                                "timestamp": "2024-12-06T12:34:56Z",
                                "version": __version__,
                                "database": "connected",
                            }
                        }
                    },
                }
            },
        )
        @limiter.limit("60/minute")
        @limiter.limit(get_active_limits)
        async def health_check(
            request: Request, response: Response
        ) -> HealthCheckResponse:
            """Check API health and database connectivity.

            Cache misses share one worker task, and the bounded waiter count
            rejects a distributed herd without parking the AnyIO worker pool.
            """
            nonlocal capacity_inflight, health_inflight, health_waiters
            database_task: asyncio.Task[bool] | None = None
            async with health_cache_lock:
                reject_monitoring_during_shutdown()
                if health_waiters >= MAX_HEALTH_SINGLEFLIGHT_WAITERS:
                    raise HTTPException(
                        status_code=503,
                        detail="Health probe capacity reached; retry later",
                        headers={"Retry-After": "1"},
                    )
                health_waiters += 1
                now = _cache_monotonic()
                if now < health_cache_expires_at:
                    connected = health_cache_connected
                else:
                    database_task = health_inflight
                    if database_task is None:
                        database_task = asyncio.create_task(load_database_health())
                        track_monitoring_task(database_task)
                        database_task.add_done_callback(observe_health_task)
                        health_inflight = database_task

            try:
                if database_task is not None:
                    # A disconnected caller must not cancel the shared probe.
                    connected = await asyncio.shield(database_task)

                # Storage readiness is intentionally not cached: quota and
                # filesystem headroom can change immediately. Concurrent
                # callers still share one probe and never queue worker threads.
                async with health_cache_lock:
                    reject_monitoring_during_shutdown()
                    capacity_task = capacity_inflight
                    if capacity_task is None:
                        capacity_task = asyncio.create_task(load_capacity_health())
                        track_monitoring_task(capacity_task)
                        capacity_task.add_done_callback(observe_health_task)
                        capacity_inflight = capacity_task
                capacity_ready = await asyncio.shield(capacity_task)

                db_status = "connected" if connected else "error"
                if db_status == "error" or not capacity_ready:
                    response.status_code = 503

                return HealthCheckResponse(
                    status=(
                        "healthy"
                        if db_status == "connected" and capacity_ready
                        else "unhealthy"
                    ),
                    timestamp=datetime.now(UTC),
                    version=__version__,
                    database=db_status,
                )
            finally:
                async with health_cache_lock:
                    health_waiters -= 1

    if config.monitoring.metrics.enabled:
        metrics_cache_lock = asyncio.Lock()
        metrics_cache: dict[frozenset[str] | None, tuple[float, dict[str, Any]]] = {}
        metrics_inflight: dict[frozenset[str] | None, asyncio.Task[dict[str, Any]]] = {}
        metrics_waiters = 0

        async def load_metrics(
            cache_key: frozenset[str] | None,
            db_ops: DatabaseOperations,
            lease: _ExpensiveReadLease,
        ) -> dict[str, Any]:
            """Populate one scoped cache entry without occupying an ASGI worker."""
            current_task = asyncio.current_task()
            try:
                db_stats = await asyncio.to_thread(
                    db_ops.get_statistics, allowed_systems=cache_key
                )
                async with metrics_cache_lock:
                    metrics_cache[cache_key] = (
                        _cache_monotonic() + 2.0,
                        db_stats,
                    )
                return db_stats
            finally:
                lease.release()
                async with metrics_cache_lock:
                    if metrics_inflight.get(cache_key) is current_task:
                        metrics_inflight.pop(cache_key, None)

        def observe_metrics_task(task: asyncio.Task[dict[str, Any]]) -> None:
            """Retrieve failures if every waiting client disconnects."""
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

        @app.get(
            config.monitoring.metrics.path,
            response_model=StatisticsResponse,
            tags=["metrics"],
            summary="System Metrics",
            description=(
                "Get system statistics. High-cardinality system and upload-source "
                "maps are capped at 1000 entries; talkgroups are capped at 20."
            ),
            responses={
                200: {
                    "description": "System metrics retrieved successfully",
                    "content": {
                        "application/json": {
                            "example": {
                                "total_calls": 1234,
                                "calls_today": 56,
                                "calls_last_hour": 12,
                                "systems": {"123": 100, "456": 50},
                                "talkgroups": {"1001": 25, "1002": 30},
                                "upload_sources": {"192.168.1.100": 150},
                                "storage_used_mb": 256.5,
                                "audio_files_count": 1234,
                            }
                        }
                    },
                }
            },
        )
        @limiter.limit("30/minute")
        @limiter.limit(get_active_limits)
        async def metrics(
            request: Request, _api_key: str | None = api_key_header
        ) -> StatisticsResponse:
            """Get comprehensive system statistics.

            Scoped cache misses use one async singleflight task. Followers await
            the same task without consuming AnyIO worker threads, while the
            process-wide expensive-read gate bounds actual SQLite work.
            """
            nonlocal metrics_waiters
            principal = authenticate_read_request(request)
            db_ops: DatabaseOperations = request.app.state.db_ops
            cache_key = principal.allowed_systems
            task: asyncio.Task[dict[str, Any]] | None = None
            async with metrics_cache_lock:
                reject_monitoring_during_shutdown()
                now = _cache_monotonic()
                cached = metrics_cache.get(cache_key)
                if cached is not None and now < cached[0]:
                    db_stats = cached[1]
                else:
                    if metrics_waiters >= MAX_METRICS_SINGLEFLIGHT_WAITERS:
                        raise HTTPException(
                            status_code=503,
                            detail="Metrics query capacity reached; retry later",
                            headers={"Retry-After": "1"},
                        )
                    task = metrics_inflight.get(cache_key)
                    if task is None:
                        lease = _acquire_expensive_read(request, principal)
                        try:
                            task = asyncio.create_task(
                                load_metrics(cache_key, db_ops, lease)
                            )
                            track_monitoring_task(task)
                        except BaseException:
                            lease.release()
                            raise
                        task.add_done_callback(observe_metrics_task)
                        metrics_inflight[cache_key] = task
                    metrics_waiters += 1

            if task is not None:
                try:
                    try:
                        # A disconnected waiter must not cancel the shared query or
                        # release its admission lease while SQLite still executes.
                        db_stats = await asyncio.shield(task)
                    except ExpensiveQueryTimeout:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "Query execution deadline exceeded; narrow the "
                                "request and retry"
                            ),
                            headers={"Retry-After": "1"},
                        ) from None
                finally:
                    async with metrics_cache_lock:
                        metrics_waiters -= 1

            return StatisticsResponse(
                total_calls=db_stats.get("total_calls", 0),
                calls_today=db_stats.get("calls_today", 0),
                calls_last_hour=db_stats.get("calls_last_hour", 0),
                systems=db_stats.get("systems", {}),
                talkgroups=db_stats.get("talkgroups", {}),
                upload_sources=db_stats.get("upload_sources", {}),
                storage_used_mb=db_stats.get("storage_used_mb", 0),
                audio_files_count=db_stats.get("audio_files_count", 0),
            )

    # FastAPI resolves typed route parameters before invoking SlowAPI's endpoint
    # wrapper. Charge that otherwise-bypassing path to the exact same route
    # limits once, then preserve FastAPI's standard bounded 422 response.
    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> Response:
        try:
            account_route_validation_failure(request)
        except RateLimitExceeded as rate_limit_error:
            return _rate_limit_exceeded_handler(request, rate_limit_error)
        return await request_validation_exception_handler(request, exc)

    # Error handlers
    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Handle uncaught exceptions."""
        # Exception messages may contain SQL values, secrets, or private paths.
        logger.error("Unhandled exception (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )

    return app
