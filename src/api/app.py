"""FastAPI application factory and setup."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Config, setup_logging
from ..database import DatabaseManager, DatabaseOperations
from ..middleware import RateLimitMiddleware
from ..middleware.security import SecurityHeadersMiddleware
from ..middleware.validation import RequestValidationMiddleware
from ..models.api_models import HealthCheckResponse, StatisticsResponse
from ..utils.file_handler import FileHandler
from ..utils.maintenance import run_retention_cleanup, run_temp_cleanup
from .query import router as query_router
from .rdioscanner import router as rdioscanner_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan manager."""
    # Startup
    logger.info("Starting sdrtrunk-rdio-api...")

    # Initialize components
    config: Config = app.state.config

    # Database
    db_manager = DatabaseManager(
        config.database.path,
        enable_wal=config.database.enable_wal,
        echo=config.server.debug,
    )
    app.state.db_manager = db_manager
    app.state.db_ops = DatabaseOperations(db_manager)

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

    # Background maintenance: enforces retention_days (calls + audio +
    # upload logs) and cleans stale temp files. Without this the
    # documented retention config would do nothing.
    db_ops = app.state.db_ops
    retention_days = config.file_handling.storage.retention_days
    interval_hours = config.file_handling.storage.cleanup_interval_hours

    async def maintenance_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(run_temp_cleanup, file_handler)
                if retention_days > 0:
                    await asyncio.to_thread(
                        run_retention_cleanup, db_ops, file_handler, retention_days
                    )
            except Exception:
                logger.exception("Maintenance cycle failed")
            await asyncio.sleep(interval_hours * 3600)

    maintenance_task: asyncio.Task[None] | None = None
    if interval_hours > 0:
        maintenance_task = asyncio.create_task(maintenance_loop())
        app.state.maintenance_task = maintenance_task
        logger.info(
            f"Maintenance task scheduled every {interval_hours}h "
            f"(retention: {retention_days} days)"
        )

    logger.info("sdrtrunk-rdio-api started successfully")

    yield

    # Shutdown
    logger.info("Shutting down sdrtrunk-rdio-api...")

    if maintenance_task:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task

    # Cleanup
    db_manager.close()

    logger.info("sdrtrunk-rdio-api shutdown complete")


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
    if override_config:
        config = override_config
    else:
        config = Config.load_from_file(config_path)

    # Setup logging
    setup_logging(config.logging)

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

    # Configure CORS. Credentials cannot be combined with a wildcard
    # origin (browsers reject Access-Control-Allow-Origin: * with
    # credentials), so only enable them for explicit origin lists.
    if config.server.cors_origins:
        allow_credentials = "*" not in config.server.cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Add security headers middleware
    app.add_middleware(SecurityHeadersMiddleware)

    # Add request validation middleware
    app.add_middleware(RequestValidationMiddleware)

    # Configure rate limiting
    rate_limiter = RateLimitMiddleware(app, config)
    app.state.rate_limiter = rate_limiter

    # Add routers
    app.include_router(rdioscanner_router)
    app.include_router(query_router)

    # Add monitoring endpoints
    if config.monitoring.health_check.enabled:

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
                                "version": "1.0.0",
                                "database": "connected",
                            }
                        }
                    },
                }
            },
        )
        def health_check(request: Request) -> HealthCheckResponse:
            """Check API health and database connectivity.

            Runs as a sync handler (threadpool) and uses a cheap SELECT 1
            connectivity probe rather than scanning tables; health checks
            are polled frequently (e.g. by Docker every 30s).
            """
            db_manager: DatabaseManager = request.app.state.db_manager
            db_status = "connected" if db_manager.check_connection() else "error"

            return HealthCheckResponse(
                status="healthy" if db_status == "connected" else "unhealthy",
                timestamp=datetime.now(UTC),
                version=__version__,
                database=db_status,
            )

    if config.monitoring.metrics.enabled:

        @app.get(
            config.monitoring.metrics.path,
            response_model=StatisticsResponse,
            tags=["metrics"],
            summary="System Metrics",
            description="Get comprehensive system statistics and metrics",
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
        def metrics(request: Request) -> StatisticsResponse:
            """Get comprehensive system statistics.

            Runs as a sync handler (threadpool). Storage figures come from
            the database rather than walking the audio tree, which would
            block on large archives.
            """
            db_ops: DatabaseOperations = request.app.state.db_ops

            db_stats = db_ops.get_statistics()

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

    # Error handlers
    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Handle uncaught exceptions."""
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )

    return app
