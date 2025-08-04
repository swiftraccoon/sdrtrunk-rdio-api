"""FastAPI application factory and setup."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import Config, setup_logging
from ..database import DatabaseManager, DatabaseOperations
from ..models.api_models import HealthCheckResponse, StatisticsResponse
from ..utils.file_handler import FileHandler
from .rdioscanner import router as rdioscanner_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan manager."""
    # Startup
    logger.info("Starting RdioCallsAPI...")

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

    logger.info("RdioCallsAPI started successfully")

    yield

    # Shutdown
    logger.info("Shutting down RdioCallsAPI...")

    # Cleanup
    db_manager.close()

    logger.info("RdioCallsAPI shutdown complete")


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

    # Create app
    app = FastAPI(
        title="RdioCallsAPI",
        description="A modular RdioScanner API ingestion server for SDRTrunk",
        version="1.0.0",
        docs_url="/docs" if config.server.enable_docs else None,
        redoc_url="/redoc" if config.server.enable_docs else None,
        lifespan=lifespan,
    )

    # Store config in app state
    app.state.config = config

    # Configure CORS
    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Add routers
    app.include_router(rdioscanner_router)

    # Add monitoring endpoints
    if config.monitoring.health_check.enabled:

        @app.get(
            config.monitoring.health_check.path, response_model=HealthCheckResponse
        )
        async def health_check(request: Request) -> HealthCheckResponse:
            """Health check endpoint."""
            try:
                # Check database connection
                _ = request.app.state.db_ops
                _ = request.app.state.db_manager.get_stats()
                db_status = "connected"
            except Exception:
                db_status = "error"

            return HealthCheckResponse(
                status="healthy" if db_status == "connected" else "unhealthy",
                timestamp=datetime.now(UTC),
                version="1.0.0",
                database=db_status,
            )

    if config.monitoring.metrics.enabled:

        @app.get(config.monitoring.metrics.path, response_model=StatisticsResponse)
        async def metrics(request: Request) -> StatisticsResponse:
            """Statistics/metrics endpoint."""
            db_ops: DatabaseOperations = request.app.state.db_ops
            file_handler: FileHandler = request.app.state.file_handler

            # Get database statistics
            db_stats = db_ops.get_statistics()

            # Get file storage statistics
            storage_stats = file_handler.get_storage_stats()

            return StatisticsResponse(
                total_calls=db_stats.get("total_calls", 0),
                calls_today=db_stats.get("calls_today", 0),
                calls_last_hour=db_stats.get("calls_last_hour", 0),
                systems=db_stats.get("systems", {}),
                talkgroups=db_stats.get("talkgroups", {}),
                upload_sources=db_stats.get("upload_sources", {}),
                storage_used_mb=storage_stats.get("total_size_mb", 0),
                audio_files_count=storage_stats.get("total_files", 0),
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
