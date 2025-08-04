"""Configuration management for RdioCallsAPI."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class APIKeyConfig(BaseModel):
    """Configuration for an individual API key."""

    key: str = Field(..., description="API key value")
    description: str | None = Field(None, description="Description of key usage")
    allowed_ips: list[str] = Field(
        default_factory=list, description="Allowed IP addresses (empty = all)"
    )
    allowed_systems: list[str] = Field(
        default_factory=list, description="Allowed system IDs (empty = all)"
    )


class ServerConfig(BaseModel):
    """Web server configuration."""

    host: str = Field("0.0.0.0", description="Server host")
    port: int = Field(8080, description="Server port")
    cors_origins: list[str] = Field(["*"], description="CORS allowed origins")
    enable_docs: bool = Field(True, description="Enable API documentation")
    debug: bool = Field(False, description="Debug mode")


class DatabaseConfig(BaseModel):
    """Database configuration."""

    path: str = Field("data/rdio_calls.db", description="SQLite database path")
    enable_wal: bool = Field(True, description="Enable Write-Ahead Logging")
    pool_size: int = Field(5, description="Connection pool size")
    max_overflow: int = Field(10, description="Max overflow connections")


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = Field(True, description="Enable rate limiting")
    max_requests_per_minute: int = Field(60, description="Max requests per minute")
    max_requests_per_hour: int = Field(1000, description="Max requests per hour")
    max_requests_per_day: int = Field(10000, description="Max requests per day")


class SecurityConfig(BaseModel):
    """Security configuration."""

    api_keys: list[APIKeyConfig] = Field(
        default_factory=list, description="API key configurations"
    )
    # Pydantic V2 has a known mypy issue with default_factory class constructors
    # https://github.com/pydantic/pydantic/issues/6713
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)  # type: ignore[arg-type]


class FileStorageConfig(BaseModel):
    """File storage configuration."""

    strategy: str = Field(
        "filesystem", description="Storage strategy: discard, filesystem, database"
    )
    directory: str = Field(
        "data/audio", description="Storage directory for filesystem strategy"
    )
    organize_by_date: bool = Field(True, description="Organize files by date")
    retention_days: int = Field(30, description="File retention in days (0 = forever)")

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        allowed = ["discard", "filesystem", "database"]
        if v not in allowed:
            raise ValueError(f"Strategy must be one of {allowed}")
        return v


class FileHandlingConfig(BaseModel):
    """File handling configuration."""

    accepted_formats: list[str] = Field([".mp3"], description="Accepted file formats")
    max_file_size_mb: int = Field(100, description="Maximum file size in MB")
    min_file_size_kb: int = Field(1, description="Minimum file size in KB")
    temp_directory: str = Field("data/temp", description="Temporary file directory")
    # Pydantic V2 mypy limitation with class constructors in default_factory
    storage: FileStorageConfig = Field(default_factory=FileStorageConfig)  # type: ignore[arg-type]


class ProcessingConfig(BaseModel):
    """Data processing configuration."""

    mode: str = Field("store", description="Processing mode: log_only, store, process")
    store_fields: list[str] = Field(
        default_factory=lambda: [
            "timestamp",
            "system",
            "frequency",
            "talkgroup",
            "source",
            "systemLabel",
            "talkgroupLabel",
            "talkgroupGroup",
            "talkerAlias",
            "audio_filename",
            "audio_size",
            "upload_ip",
            "upload_timestamp",
        ],
        description="Fields to store in database",
    )

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = ["log_only", "store", "process"]
        if v not in allowed:
            raise ValueError(f"Mode must be one of {allowed}")
        return v


class LogFileConfig(BaseModel):
    """Log file configuration."""

    enabled: bool = Field(True, description="Enable file logging")
    path: str = Field("logs/rdio_calls_api.log", description="Log file path")
    max_size_mb: int = Field(100, description="Max log file size in MB")
    backup_count: int = Field(5, description="Number of backup files to keep")


class LogConsoleConfig(BaseModel):
    """Console logging configuration."""

    enabled: bool = Field(True, description="Enable console logging")
    colorize: bool = Field(True, description="Colorize console output")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field("INFO", description="Logging level")
    format: str = Field(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Log format string",
    )
    # Pydantic V2 mypy limitation with class constructors in default_factory
    file: LogFileConfig = Field(default_factory=LogFileConfig)  # type: ignore[arg-type]
    console: LogConsoleConfig = Field(default_factory=LogConsoleConfig)  # type: ignore[arg-type]

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        allowed = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in allowed:
            raise ValueError(f"Level must be one of {allowed}")
        return v.upper()


class HealthCheckConfig(BaseModel):
    """Health check configuration."""

    enabled: bool = Field(True, description="Enable health check endpoint")
    path: str = Field("/health", description="Health check path")


class MetricsConfig(BaseModel):
    """Metrics configuration."""

    enabled: bool = Field(True, description="Enable metrics endpoint")
    path: str = Field("/metrics", description="Metrics path")


class StatisticsConfig(BaseModel):
    """Statistics tracking configuration."""

    enabled: bool = Field(True, description="Enable statistics tracking")
    track_sources: bool = Field(True, description="Track upload sources")
    track_systems: bool = Field(True, description="Track system statistics")
    track_talkgroups: bool = Field(True, description="Track talkgroup statistics")


class MonitoringConfig(BaseModel):
    """Monitoring configuration."""

    # Pydantic V2 mypy limitation with class constructors in default_factory
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)  # type: ignore[arg-type]
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)  # type: ignore[arg-type]
    statistics: StatisticsConfig = Field(default_factory=StatisticsConfig)  # type: ignore[arg-type]


class Config(BaseModel):
    """Main configuration model."""

    # Pydantic V2 mypy limitation with class constructors in default_factory
    server: ServerConfig = Field(default_factory=ServerConfig)  # type: ignore[arg-type]
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)  # type: ignore[arg-type]
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    file_handling: FileHandlingConfig = Field(default_factory=FileHandlingConfig)  # type: ignore[arg-type]
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)  # type: ignore[arg-type]
    logging: LoggingConfig = Field(default_factory=LoggingConfig)  # type: ignore[arg-type]
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @classmethod
    def load_from_file(cls, config_path: str) -> "Config":
        """Load configuration from YAML file.

        Args:
            config_path: Path to configuration file

        Returns:
            Config instance
        """
        config_path_obj = Path(config_path)

        if not config_path_obj.exists():
            logger.warning(
                f"Config file not found at {config_path_obj}, using defaults"
            )
            return cls()

        try:
            with open(config_path_obj) as f:
                data = yaml.safe_load(f) or {}

            config = cls(**data)
            logger.info(f"Loaded configuration from {config_path_obj}")
            return config

        except Exception as e:
            logger.error(f"Failed to load config from {config_path_obj}: {e}")
            logger.info("Using default configuration")
            return cls()

    def save_to_file(self, config_path: str) -> None:
        """Save configuration to YAML file.

        Args:
            config_path: Path to save configuration
        """
        config_path_obj = Path(config_path)
        config_path_obj.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(config_path_obj, "w") as f:
                yaml.dump(self.dict(), f, default_flow_style=False, sort_keys=False)

            logger.info(f"Saved configuration to {config_path_obj}")

        except Exception as e:
            logger.error(f"Failed to save config to {config_path_obj}: {e}")
            raise


def setup_logging(config: LoggingConfig) -> None:
    """Setup logging based on configuration.

    Args:
        config: Logging configuration
    """
    import logging.handlers

    # Create logger
    root_logger = logging.getLogger()
    root_logger.setLevel(config.level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(config.format)

    # Console handler
    if config.console.enabled:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        if config.console.colorize:
            try:
                import colorlog

                color_formatter = colorlog.ColoredFormatter(
                    "%(log_color)s" + config.format,
                    log_colors={
                        "DEBUG": "cyan",
                        "INFO": "green",
                        "WARNING": "yellow",
                        "ERROR": "red",
                        "CRITICAL": "red,bg_white",
                    },
                )
                console_handler.setFormatter(color_formatter)
            except ImportError:
                pass  # Colorlog not available

        root_logger.addHandler(console_handler)

    # File handler
    if config.file.enabled:
        # Create log directory
        log_path = Path(config.file.path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotating file handler
        file_handler = logging.handlers.RotatingFileHandler(
            config.file.path,
            maxBytes=config.file.max_size_mb * 1024 * 1024,
            backupCount=config.file.backup_count,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    logger.info(f"Logging configured - Level: {config.level}")
