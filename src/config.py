"""Configuration management for sdrtrunk-rdio-api."""

import errno
import ipaddress
import logging
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from .exceptions import ConfigurationError
from .filesystem_security import (
    durable_fsync,
    log_process_lock_path,
    path_uses_dangerous_windows_namespace,
    paths_conflict,
    paths_overlap,
    reject_insecure_extended_acl,
    rotating_log_state_paths,
    sqlite_state_paths,
)

logger = logging.getLogger(__name__)
MAX_CONFIG_FILE_BYTES = 1024 * 1024
MAX_TOTAL_LOG_STORAGE_MB = 512


class _UnambiguousSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects mapping overrides at every depth."""


def _construct_unambiguous_mapping(
    loader: yaml.SafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ConstructorError(
                None,
                None,
                "YAML merge keys are not allowed",
                key_node.start_mark,
            )
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise ConstructorError(
                None,
                None,
                "YAML mapping keys must be scalar values",
                key_node.start_mark,
            ) from None
        if duplicate:
            # Never interpolate the key/value: a malformed operator file may
            # contain credentials in either position and parse errors are logs.
            raise ConstructorError(
                None,
                None,
                "Duplicate YAML mapping keys are not allowed",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UnambiguousSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unambiguous_mapping,
)


def _validate_nonblank_path(value: str) -> str:
    if not value.strip():
        raise ValueError("Path cannot be blank")
    if "\x00" in value:
        raise ValueError("Path cannot contain null bytes")
    if path_uses_dangerous_windows_namespace(value):
        raise ValueError("Path uses a reserved or ambiguous Windows filename")
    return value


def _validate_monitoring_route(value: str) -> str:
    """Keep configurable operational endpoints unambiguous and local."""
    if not re.fullmatch(r"/[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Monitoring path must be one absolute, URL-safe path segment")
    if value in {"/docs", "/redoc", "/openapi.json"}:
        raise ValueError("Monitoring path collides with a built-in API route")
    return value


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _absolute_path(path: str | Path) -> Path:
    if path_uses_dangerous_windows_namespace(path):
        raise OSError("Secure paths cannot use reserved or ambiguous Windows names")
    absolute = Path(path).expanduser()
    if not absolute.is_absolute():
        absolute = Path.cwd() / absolute
    if any(component in {".", ".."} for component in absolute.parts[1:]):
        raise OSError("Secure paths must not contain dot components")
    return absolute


def _trusted_prefix_and_remainder(absolute: Path) -> tuple[Path, tuple[str, ...]]:
    """Resolve only a root-controlled path prefix.

    This permits platform aliases such as macOS ``/var`` while never resolving
    a missing name or a name below a user/sticky-writable directory. Those
    remaining components are walked later through a pinned descriptor.
    """
    parts = absolute.parts[1:]
    lexical_cursor = Path(absolute.anchor)
    trusted_count = 0

    for component in parts:
        lexical_cursor /= component
        try:
            component_status = lexical_cursor.lstat()
        except FileNotFoundError:
            break

        if stat.S_ISLNK(component_status.st_mode):
            if component_status.st_uid != 0:
                raise OSError("Secure path must not contain symbolic links")
            # The containing prefix is root-owned and non-writable, so only
            # root can replace this platform alias before it is resolved.
            trusted_count += 1
            continue
        if not stat.S_ISDIR(component_status.st_mode):
            break
        if component_status.st_uid != 0:
            break

        component_mode = stat.S_IMODE(component_status.st_mode)
        if component_mode & 0o022:
            if component_mode & stat.S_ISVTX:
                # Pin the sticky system directory itself (typically /tmp), but
                # inspect none of its attacker-creatable children by pathname.
                trusted_count += 1
                break
            raise PermissionError(
                "System directory ancestors must not be group/world writable"
            )
        trusted_count += 1

    trusted_lexical = Path(absolute.anchor).joinpath(*parts[:trusted_count])
    canonical_prefix = trusted_lexical.resolve(strict=True)
    return canonical_prefix, tuple(parts[trusted_count:])


def _open_trusted_prefix(canonical_prefix: Path) -> int:
    """Open a canonical root-controlled prefix component by component."""
    current_descriptor = os.open(canonical_prefix.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        root_status = os.fstat(current_descriptor)
        if root_status.st_uid != 0 or stat.S_IMODE(root_status.st_mode) & 0o022:
            raise PermissionError("Filesystem root is not a trusted directory")
        reject_insecure_extended_acl(
            current_descriptor, description="Trusted system directory"
        )

        canonical_parts = canonical_prefix.parts[1:]
        for index, component in enumerate(canonical_parts):
            next_descriptor = -1
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=current_descriptor,
                )
                component_status = os.fstat(next_descriptor)
                component_mode = stat.S_IMODE(component_status.st_mode)
                if component_status.st_uid != 0:
                    raise PermissionError(
                        "Resolved system aliases must remain root-owned"
                    )
                if component_mode & 0o022 and not (
                    component_mode & stat.S_ISVTX and index == len(canonical_parts) - 1
                ):
                    raise PermissionError(
                        "System directory ancestors must not be group/world writable"
                    )
                reject_insecure_extended_acl(
                    next_descriptor, description="Trusted system directory ancestor"
                )
            except BaseException:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor
    except BaseException:
        os.close(current_descriptor)
        raise


def _raise_component_error(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise OSError(
            "Secure path must not contain a symlink (symbolic link) or "
            "non-directory component"
        ) from None
    raise exc


def _walk_secure_directories(
    start_descriptor: int,
    components: tuple[str, ...],
    *,
    create: bool,
) -> tuple[int, bool]:
    """Consume the start fd and return the final fd plus creation status."""
    current_descriptor = start_descriptor
    controlled_tree = False
    final_created = False
    try:
        for component in components:
            next_descriptor = -1
            created = False
            try:
                try:
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=current_descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                        durable_fsync(current_descriptor)
                        created = True
                    except FileExistsError:
                        # A competing creator is validated by the no-follow
                        # open and ownership checks below.
                        pass
                    try:
                        next_descriptor = os.open(
                            component,
                            _DIRECTORY_OPEN_FLAGS,
                            dir_fd=current_descriptor,
                        )
                    except OSError as exc:
                        _raise_component_error(exc)
                except OSError as exc:
                    _raise_component_error(exc)

                component_status = os.fstat(next_descriptor)
                component_mode = stat.S_IMODE(component_status.st_mode)
                component_owner = component_status.st_uid
                if component_owner == os.geteuid():
                    controlled_tree = True
                elif component_owner != 0 or controlled_tree:
                    raise PermissionError(
                        "Private directory ancestors must be owned by this user or root"
                    )
                if component_owner == os.geteuid() and component_mode & 0o022:
                    raise PermissionError(
                        "Private directory ancestors must not be group/world writable"
                    )
                if (
                    component_owner == 0
                    and component_owner != os.geteuid()
                    and component_mode & 0o022
                    and not component_mode & stat.S_ISVTX
                ):
                    raise PermissionError(
                        "System directory ancestors must not be group/world writable"
                    )
                if created and hasattr(os, "fchmod"):
                    os.fchmod(next_descriptor, 0o700)
                reject_insecure_extended_acl(
                    next_descriptor, description="Secure directory ancestor"
                )
                final_created = created
            except BaseException:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
                raise

            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor, final_created
    except BaseException:
        os.close(current_descriptor)
        raise


def open_secure_regular_file(path: str | Path) -> int:
    """Open a protected regular file through pinned, no-follow parents.

    The caller owns the returned descriptor. On POSIX, final ownership must be
    the effective user or root and user-controlled parents must not be
    group/world writable.
    """
    absolute = _absolute_path(path)

    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        # Windows refuses to open directories through ``os.open``, so inspect
        # the path first to provide the same fail-closed regular-file contract
        # and diagnostic as the descriptor walk below. Revalidate the opened
        # descriptor so a path replacement cannot bypass the type check.
        if not stat.S_ISREG(os.stat(absolute, follow_symlinks=False).st_mode):
            raise OSError("Protected path is not a regular file")
        descriptor = os.open(
            absolute,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("Protected path is not a regular file")
            reject_insecure_extended_acl(
                descriptor, description="Protected regular file"
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    if not absolute.name:
        raise OSError("Protected path must name a regular file")

    canonical_prefix, remainder = _trusted_prefix_and_remainder(absolute.parent)
    parent_descriptor, _parent_created = _walk_secure_directories(
        _open_trusted_prefix(canonical_prefix), remainder, create=False
    )
    try:
        try:
            descriptor = os.open(
                absolute.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OSError("Protected file must not be a symbolic link") from None
            raise
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError("Protected path is not a regular file")
            if file_status.st_uid not in {0, os.geteuid()}:
                raise PermissionError(
                    "Protected file must be owned by this user or root"
                )
            reject_insecure_extended_acl(
                descriptor, description="Protected regular file"
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_descriptor)


@contextmanager
def secure_directory_handle(
    directory: str | Path,
    *,
    create: bool = False,
    require_private: bool = False,
) -> Iterator[tuple[Path, int | None]]:
    """Yield a canonical directory and, on POSIX, its pinned descriptor.

    Existing user-owned directories may retain read/execute permissions when
    ``require_private`` is false, but no traversed user-controlled component
    may be group/world writable. Missing components are always mode 0700.
    """
    absolute = _absolute_path(directory)

    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        if absolute.is_symlink():
            raise OSError("Secure directory must not be a symbolic link")
        if create:
            absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not absolute.is_dir():
            raise OSError("Secure path is not a directory")
        if require_private:
            absolute.chmod(0o700)
        yield absolute.resolve(strict=True), None
        return

    canonical_prefix, remainder = _trusted_prefix_and_remainder(absolute)
    directory_descriptor, final_created = _walk_secure_directories(
        _open_trusted_prefix(canonical_prefix), remainder, create=create
    )
    try:
        directory_status = os.fstat(directory_descriptor)
        directory_mode = stat.S_IMODE(directory_status.st_mode)
        if directory_status.st_uid == os.geteuid():
            if directory_mode & 0o022:
                raise PermissionError(
                    "Existing secure directory must not be group/world writable"
                )
        elif not (
            not require_private
            and directory_status.st_uid == 0
            and (not directory_mode & 0o022 or directory_mode & stat.S_ISVTX)
        ):
            raise PermissionError(
                "Existing secure directory must be owned by this user"
            )
        if require_private:
            if os.geteuid() == 0 and not final_created and directory_mode & 0o077:
                raise PermissionError(
                    "Pre-existing root-owned private directory must already "
                    "exclude group/world access"
                )
            if hasattr(os, "fchmod") and (final_created or os.geteuid() != 0):
                os.fchmod(directory_descriptor, 0o700)
            reject_insecure_extended_acl(
                directory_descriptor, description="Private directory"
            )
        yield canonical_prefix.joinpath(*remainder), directory_descriptor
    finally:
        os.close(directory_descriptor)


def prepare_private_directory(directory: str | Path) -> Path:
    """Open/create a private directory through pinned no-follow components."""
    with secure_directory_handle(directory, create=True, require_private=True) as (
        private_directory,
        _descriptor,
    ):
        return private_directory


def write_private_text_file(path: str | Path, contents: str) -> None:
    """Atomically write a UTF-8 text file with mode ``0600``.

    The temporary file is created in the destination directory so
    ``os.replace`` stays atomic on a single filesystem. This also avoids
    following an existing destination symlink while writing secrets.
    """
    if path_uses_dangerous_windows_namespace(path):
        raise ValueError("Private output cannot use an ambiguous Windows filename")
    destination = Path(path)
    private_parent = prepare_private_directory(destination.parent)
    destination = private_parent / destination.name
    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Windows compatibility
            Path(temporary_name).chmod(0o600)
        reject_insecure_extended_acl(descriptor, description="Private temporary file")
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(contents)
            output.flush()
            durable_fsync(output.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        if os.name == "posix":
            parent_descriptor = os.open(
                private_parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                durable_fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


class StrictConfigModel(BaseModel):
    """Base class that rejects misspelled or unsupported configuration keys."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        hide_input_in_errors=True,
    )


class APIKeyConfig(StrictConfigModel):
    """Configuration for an individual API key."""

    key: str = Field(
        ...,
        min_length=16,
        max_length=512,
        repr=False,
        description="API key value",
    )
    identifier: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="Stable nonsecret identifier used in audit records",
    )
    description: str | None = Field(
        None, max_length=256, description="Description of key usage"
    )
    allowed_ips: list[str] = Field(
        default_factory=list,
        max_length=256,
        description="Allowed IP addresses (empty = all)",
    )
    allowed_systems: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="Allowed system IDs (empty = all)",
    )

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Reject blank secrets even when they satisfy the length constraint."""
        if not value.strip():
            raise ValueError("API key cannot be blank")
        if value != value.strip():
            raise ValueError("API key cannot have leading or trailing whitespace")
        if any(character in value for character in ("\r", "\n", "\x00")):
            raise ValueError("API key cannot contain line breaks or null bytes")
        if len(value.encode("utf-8")) > 512:
            raise ValueError("API key cannot exceed 512 UTF-8 bytes")
        normalized = value.casefold()
        documented_placeholders = (
            "change-me",
            "replace-with",
            "paste-the",
            "paste-a-",
            "your-generated",
            "your-api-key",
        )
        if any(normalized.startswith(token) for token in documented_placeholders):
            raise ValueError("API key must not be a documented placeholder")
        return value

    @field_validator("allowed_ips")
    @classmethod
    def validate_allowed_ips(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if (
                not value
                or value != value.strip()
                or len(value.encode("utf-8")) > 64
                or any(character in value for character in ("\r", "\n", "\x00"))
            ):
                raise ValueError("Allowed IP values must be bounded, nonblank text")
            try:
                normalized.append(str(ipaddress.ip_address(value)))
            except ValueError:
                raise ValueError(
                    "Allowed IP values must be literal IP addresses"
                ) from None
        if len(normalized) != len(set(normalized)):
            raise ValueError("Allowed IP values must be unique")
        return normalized

    @field_validator("allowed_systems")
    @classmethod
    def validate_allowed_systems(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9]{1,10}", value) for value in values):
            raise ValueError("Allowed system IDs must contain 1 to 10 digits")
        if len(values) != len(set(values)):
            raise ValueError("Allowed system IDs must be unique")
        return values


class ServerConfig(StrictConfigModel):
    """Web server configuration."""

    host: str = Field("127.0.0.1", description="Server host")
    port: int = Field(8080, ge=1, le=65535, description="Server port")
    cors_origins: list[str] = Field(
        default_factory=list, max_length=64, description="CORS allowed origins"
    )
    enable_docs: StrictBool = Field(True, description="Enable API documentation")
    debug: StrictBool = Field(False, description="Debug mode")
    ssl_cert: str | None = Field(None, description="TLS certificate path")
    ssl_key: str | None = Field(None, description="TLS private key path")
    read_timeout_seconds: int = Field(
        30,
        gt=0,
        le=600,
        description=(
            "Absolute deadline for receiving one request stream's complete body, "
            "including HTTP/2 streams"
        ),
    )

    @field_validator("ssl_cert", "ssl_key")
    @classmethod
    def validate_tls_path(cls, value: str | None) -> str | None:
        """Reject configured-but-empty TLS paths."""
        if value is not None and not value.strip():
            raise ValueError("TLS paths cannot be blank")
        if value is not None and path_uses_dangerous_windows_namespace(value):
            raise ValueError("TLS path uses a reserved or ambiguous Windows filename")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                not value
                or value != value.strip()
                or len(value.encode("utf-8")) > 2048
                or any(character in value for character in ("\r", "\n", "\x00"))
            ):
                raise ValueError("CORS origins must be bounded, nonblank text")
        if len(values) != len(set(values)):
            raise ValueError("CORS origins must be unique")
        return values

    @model_validator(mode="after")
    def validate_tls_pair(self) -> Self:
        """A partial TLS configuration must never silently fall back to HTTP."""
        if bool(self.ssl_cert) != bool(self.ssl_key):
            raise ValueError("ssl_cert and ssl_key must be configured together")
        return self


class DatabaseConfig(StrictConfigModel):
    """Database configuration."""

    path: str = Field("data/rdio_calls.db", description="SQLite database path")
    enable_wal: StrictBool = Field(True, description="Enable Write-Ahead Logging")

    _path_must_not_be_blank = field_validator("path")(_validate_nonblank_path)


class RateLimitConfig(StrictConfigModel):
    """Rate limiting configuration.

    Defaults are sized for busy trunked systems: a system uploading
    several calls per second must not lose calls to 429 responses.
    """

    enabled: StrictBool = Field(True, description="Enable rate limiting")
    max_requests_per_minute: int = Field(
        600, ge=1, le=100_000_000, description="Max requests per minute"
    )
    max_requests_per_hour: int = Field(
        10000, ge=1, le=100_000_000, description="Max requests per hour"
    )
    max_requests_per_day: int = Field(
        100000, ge=1, le=100_000_000, description="Max requests per day"
    )


class SecurityConfig(StrictConfigModel):
    """Security configuration."""

    api_keys: list[APIKeyConfig] = Field(
        default_factory=list, max_length=128, description="API key configurations"
    )
    trusted_proxies: list[str] = Field(
        default_factory=list,
        max_length=128,
        description=(
            "Client IPs (reverse proxies) whose X-Forwarded-For header is "
            "trusted. Empty list means X-Forwarded-For is never trusted."
        ),
    )
    allow_unauthenticated_uploads: StrictBool = Field(
        False,
        description="Explicitly allow uploads without an API key (unsafe)",
    )
    allow_unauthenticated_reads: StrictBool = Field(
        False,
        description="Explicitly allow query, audio, and metrics reads without a key",
    )
    # Pydantic V2 has a known mypy issue with default_factory class constructors
    # https://github.com/pydantic/pydantic/issues/6713
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)  # type: ignore[arg-type]

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, values: list[str]) -> list[str]:
        """Canonicalize literal proxy hosts/networks and reject duplicates."""
        normalized_proxies: list[str] = []
        proxy_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for proxy in values:
            if (
                not proxy
                or proxy != proxy.strip()
                or len(proxy.encode("utf-8")) > 64
                or any(character in proxy for character in ("\r", "\n", "\x00"))
            ):
                raise ValueError("Trusted proxy values must be bounded, nonblank text")
            try:
                if "/" in proxy:
                    network = ipaddress.ip_network(proxy, strict=False)
                    normalized_proxies.append(network.with_prefixlen)
                else:
                    address = ipaddress.ip_address(proxy)
                    network = ipaddress.ip_network(
                        f"{address}/{address.max_prefixlen}", strict=True
                    )
                    normalized_proxies.append(str(address))
            except ValueError:
                raise ValueError(
                    "Trusted proxies must be literal IP addresses or CIDR networks"
                ) from None
            proxy_networks.append(network)
        if len(proxy_networks) != len(set(proxy_networks)):
            raise ValueError("Trusted proxy values must be unique")
        return normalized_proxies

    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        """Duplicate secrets make authorization and audit attribution ambiguous."""
        keys = [entry.key for entry in self.api_keys]
        if len(keys) != len(set(keys)):
            raise ValueError("API keys must be unique")
        identifiers = [entry.identifier for entry in self.api_keys]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("API key identifiers must be unique")
        return self


class FileStorageConfig(StrictConfigModel):
    """File storage configuration."""

    strategy: str = Field(
        "filesystem", description="Storage strategy: discard or filesystem"
    )
    directory: str = Field(
        "data/audio", description="Storage directory for filesystem strategy"
    )
    max_storage_size_mb: int = Field(
        102_400,
        ge=1,
        le=104_857_600,
        description="Maximum total size of the persistent audio archive in MB",
    )
    max_storage_files: int = Field(
        5_000_000,
        ge=1,
        le=100_000_000,
        description="Maximum number of regular files in the persistent archive",
    )
    organize_by_date: StrictBool = Field(True, description="Organize files by date")
    retention_days: int = Field(
        30,
        ge=0,
        le=36_500,
        description="Delete calls older than this many days (0 = keep forever)",
    )
    cleanup_interval_hours: int = Field(
        6,
        ge=0,
        le=8_760,
        description=(
            "How often the server runs retention and temp-file cleanup "
            "(0 = disable background cleanup)"
        ),
    )

    _directory_must_not_be_blank = field_validator("directory")(_validate_nonblank_path)

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        # Database-backed audio has never been implemented. Rejecting the old
        # value prevents a successful response from acknowledging discarded
        # audio.
        allowed = ["discard", "filesystem"]
        if v not in allowed:
            raise ValueError(f"Strategy must be one of {allowed}")
        return v


class FileHandlingConfig(StrictConfigModel):
    """File handling configuration."""

    accepted_formats: list[str] = Field(
        default_factory=lambda: [".mp3"],
        min_length=1,
        max_length=1,
        description="Accepted file formats",
    )
    max_file_size_mb: int = Field(
        100, ge=1, le=512, description="Maximum file size in MB"
    )
    min_file_size_kb: int = Field(
        1, ge=0, le=10_485_760, description="Minimum file size in KB"
    )
    minimum_free_space_mb: int = Field(
        256,
        ge=1,
        le=1_048_576,
        description="Minimum usable bytes retained on upload/state filesystems in MB",
    )
    minimum_free_inodes: int = Field(
        1024,
        ge=1,
        le=100_000_000,
        description="Minimum free inodes retained on upload/state filesystems",
    )
    maintenance_state_reserve_mb: int = Field(
        32,
        ge=32,
        le=1024,
        description=(
            "Filesystem headroom reserved for one bounded SQLite maintenance "
            "transaction and its WAL/checkpoint in MB"
        ),
    )
    temp_directory: str = Field("data/temp", description="Temporary file directory")
    # Pydantic V2 mypy limitation with class constructors in default_factory
    storage: FileStorageConfig = Field(default_factory=FileStorageConfig)  # type: ignore[arg-type]

    _temp_directory_must_not_be_blank = field_validator("temp_directory")(
        _validate_nonblank_path
    )

    @field_validator("accepted_formats")
    @classmethod
    def validate_accepted_formats(cls, values: list[str]) -> list[str]:
        if [value.lower() for value in values] != [".mp3"]:
            raise ValueError("Only the securely validated .mp3 format is supported")
        return [".mp3"]

    @model_validator(mode="after")
    def validate_size_range(self) -> Self:
        """Ensure the minimum accepted size does not exceed the maximum."""
        if self.min_file_size_kb > self.max_file_size_mb * 1024:
            raise ValueError("min_file_size_kb cannot exceed max_file_size_mb")
        return self


class ProcessingConfig(StrictConfigModel):
    """Data processing configuration."""

    mode: str = Field("store", description="Processing mode: log_only, store, process")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = ["log_only", "store", "process"]
        if v not in allowed:
            raise ValueError(f"Mode must be one of {allowed}")
        return v


class LogFileConfig(StrictConfigModel):
    """Log file configuration."""

    enabled: StrictBool = Field(True, description="Enable file logging")
    path: str = Field("logs/rdio_calls_api.log", description="Log file path")
    max_size_mb: int = Field(
        20, ge=1, le=MAX_TOTAL_LOG_STORAGE_MB, description="Max log file size in MB"
    )
    backup_count: int = Field(
        5,
        ge=1,
        le=20,
        description="Number of backup files to keep (at least one is required)",
    )

    _path_must_not_be_blank = field_validator("path")(_validate_nonblank_path)

    @model_validator(mode="after")
    def validate_total_log_storage(self) -> Self:
        total_megabytes = self.max_size_mb * (self.backup_count + 1)
        if total_megabytes > MAX_TOTAL_LOG_STORAGE_MB:
            raise ValueError(
                "Active and rotated log files may total at most "
                f"{MAX_TOTAL_LOG_STORAGE_MB} MB"
            )
        return self


class LogConsoleConfig(StrictConfigModel):
    """Console logging configuration."""

    enabled: StrictBool = Field(True, description="Enable console logging")
    colorize: StrictBool = Field(True, description="Colorize console output")


class LoggingConfig(StrictConfigModel):
    """Logging configuration."""

    level: str = Field("INFO", description="Logging level")
    format: str = Field(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        min_length=1,
        max_length=4096,
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


class HealthCheckConfig(StrictConfigModel):
    """Health check configuration."""

    enabled: StrictBool = Field(True, description="Enable health check endpoint")
    path: str = Field("/health", description="Health check path")

    _path_must_be_safe = field_validator("path")(_validate_monitoring_route)


class MetricsConfig(StrictConfigModel):
    """Metrics configuration."""

    enabled: StrictBool = Field(True, description="Enable metrics endpoint")
    path: str = Field("/metrics", description="Metrics path")

    _path_must_be_safe = field_validator("path")(_validate_monitoring_route)


class MonitoringConfig(StrictConfigModel):
    """Monitoring configuration."""

    # Pydantic V2 mypy limitation with class constructors in default_factory
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)  # type: ignore[arg-type]
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def validate_distinct_paths(self) -> Self:
        if (
            self.health_check.enabled
            and self.metrics.enabled
            and self.health_check.path == self.metrics.path
        ):
            raise ValueError("Health and metrics endpoints must use different paths")
        return self


class Config(StrictConfigModel):
    """Main configuration model."""

    # Pydantic V2 mypy limitation with class constructors in default_factory
    server: ServerConfig = Field(default_factory=ServerConfig)  # type: ignore[arg-type]
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)  # type: ignore[arg-type]
    security: SecurityConfig = Field(default_factory=SecurityConfig)  # type: ignore[arg-type]
    file_handling: FileHandlingConfig = Field(default_factory=FileHandlingConfig)  # type: ignore[arg-type]
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)  # type: ignore[arg-type]
    logging: LoggingConfig = Field(default_factory=LoggingConfig)  # type: ignore[arg-type]
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    def protected_state_paths(self) -> tuple[str | Path, ...]:
        """Return every path a config file must not replace or live beneath."""
        protected: list[str | Path] = [
            self.file_handling.storage.directory,
            self.file_handling.temp_directory,
            *sqlite_state_paths(self.database.path),
        ]
        if self.logging.file.enabled:
            protected.extend(
                rotating_log_state_paths(
                    self.logging.file.path, self.logging.file.backup_count
                )
            )
        if self.server.ssl_cert:
            protected.append(self.server.ssl_cert)
        if self.server.ssl_key:
            protected.append(self.server.ssl_key)
        return tuple(protected)

    def validate_protected_input_path(
        self, input_path: str | Path, *, description: str
    ) -> None:
        """Keep a read-only input outside mutable, secret, and cleanup state."""
        if any(
            paths_conflict(input_path, protected_path)
            for protected_path in self.protected_state_paths()
        ):
            raise ConfigurationError(
                f"The {description} must not conflict with application state, "
                "TLS files, audio storage, or temp roots"
            )

    def validate_config_file_path(self, config_path: str | Path) -> None:
        """Reject a config file that aliases mutable, secret, or cleanup state."""
        self.validate_protected_input_path(
            config_path, description="configuration file"
        )

    @model_validator(mode="after")
    def validate_destructive_path_separation(self) -> Self:
        """Keep cleanup roots from encompassing unrelated persistent state."""
        storage = self.file_handling.storage.directory
        temporary = self.file_handling.temp_directory

        if paths_overlap(storage, temporary):
            raise ValueError(
                "Storage and temporary directories must not contain one another"
            )

        database_state: list[tuple[str | Path, str]] = [
            (path, "database state") for path in sqlite_state_paths(self.database.path)
        ]
        log_state: list[tuple[str | Path, str]] = []
        if self.logging.file.enabled:
            log_state = [
                (path, "log file state")
                for path in rotating_log_state_paths(
                    self.logging.file.path, self.logging.file.backup_count
                )
            ]
        tls_files: list[tuple[str | Path, str]] = []
        if self.server.ssl_cert:
            tls_files.append((self.server.ssl_cert, "TLS certificate"))
        if self.server.ssl_key:
            tls_files.append((self.server.ssl_key, "TLS private key"))

        protected_files: list[tuple[str | Path, str]] = [
            *database_state,
            *log_state,
            *tls_files,
        ]
        for protected_path, label in protected_files:
            if paths_conflict(protected_path, storage):
                raise ValueError(f"The {label} must not be inside audio storage")
            if paths_conflict(protected_path, temporary):
                raise ValueError(f"The {label} must not be inside temp storage")

        mutable_state: list[tuple[str | Path, str]] = [
            *database_state,
            *log_state,
        ]
        for index, (first_path, first_label) in enumerate(mutable_state):
            for second_path, second_label in mutable_state[index + 1 :]:
                if paths_conflict(first_path, second_path):
                    raise ValueError(
                        f"The {first_label} and {second_label} must use different paths"
                    )

        for mutable_path, mutable_label in mutable_state:
            for tls_path, tls_label in tls_files:
                if paths_conflict(mutable_path, tls_path):
                    raise ValueError(
                        f"The {mutable_label} and {tls_label} must use different paths"
                    )
        return self

    @classmethod
    def load_from_file(
        cls, config_path: str, *, require_exists: bool = False
    ) -> "Config":
        """Load configuration from YAML file.

        A missing file falls back to defaults only when ``require_exists`` is
        false. Server startup sets it to true so a path typo cannot silently
        change the service's security posture.

        Args:
            config_path: Path to configuration file

        Returns:
            Config instance

        Raises:
            ConfigurationError: If the file is invalid, or required and missing
        """
        config_path_obj = Path(config_path)

        descriptor = -1
        try:
            descriptor = open_secure_regular_file(config_path_obj)
            with os.fdopen(descriptor, encoding="utf-8") as f:
                descriptor = -1
                file_status = os.fstat(f.fileno())
                if os.name == "posix" and stat.S_IMODE(file_status.st_mode) & 0o077:
                    raise ConfigurationError(
                        f"Config file {config_path_obj} permits group or world "
                        "access; run chmod 600 on the file"
                    )
                if file_status.st_size > MAX_CONFIG_FILE_BYTES:
                    raise ConfigurationError(
                        f"Config file {config_path_obj} exceeds the "
                        f"{MAX_CONFIG_FILE_BYTES}-byte safety limit"
                    )
                # The custom loader subclasses SafeLoader solely to reject
                # duplicate and merge keys; it never enables unsafe constructors.
                data = yaml.load(f, Loader=_UnambiguousSafeLoader) or {}  # nosec B506
        except FileNotFoundError:
            if require_exists:
                raise ConfigurationError(
                    f"Required config file not found at {config_path_obj}"
                ) from None
            logger.warning(
                "Config file not found at %s, using defaults", config_path_obj
            )
            return cls()
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}"
                if mark is not None
                else ""
            )
            raise ConfigurationError(
                f"Could not parse YAML config {config_path_obj}{location}"
            ) from None
        except OSError as e:
            raise ConfigurationError(
                f"Could not read config file {config_path_obj}: {e}"
            ) from e
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        try:
            config = cls(**data)
        except ValidationError as e:
            raise ConfigurationError(
                f"Invalid configuration in {config_path_obj}: {e}"
            ) from e

        config.validate_config_file_path(config_path_obj)

        logger.info(f"Loaded configuration from {config_path_obj}")
        return config

    def save_to_file(self, config_path: str) -> None:
        """Save configuration to YAML file.

        Args:
            config_path: Path to save configuration
        """
        try:
            config_path_obj = Path(config_path)
            self.validate_config_file_path(config_path_obj)
            serialized = yaml.safe_dump(
                self.model_dump(), default_flow_style=False, sort_keys=False
            )
            write_private_text_file(config_path_obj, serialized)

            logger.info(f"Saved configuration to {config_path_obj}")

        except Exception as e:
            logger.error(f"Failed to save config to {config_path_obj}: {e}")
            raise


def _acquire_log_process_lock(log_path: Path) -> int:
    """Acquire a stable, private advisory lock for one rotating log family."""
    lock_path = log_process_lock_path(log_path)
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        lock_status = os.fstat(descriptor)
        if not stat.S_ISREG(lock_status.st_mode):
            raise ConfigurationError("Log process lock is not a regular file")
        if os.name == "posix" and lock_status.st_uid != os.geteuid():
            raise ConfigurationError("Log process lock must be owned by this user")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        reject_insecure_extended_acl(descriptor, description="Private log process lock")
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
                raise ConfigurationError(
                    f"Log file {log_path} is already in use by another process"
                ) from None
            raise
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _release_log_process_lock(descriptor: int) -> None:
    """Release a log-family lock; closing remains the final fallback."""
    if descriptor < 0:
        return
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
        pass
    finally:
        os.close(descriptor)


def setup_logging(config: LoggingConfig) -> None:
    """Setup logging based on configuration.

    Args:
        config: Logging configuration
    """
    import logging.handlers

    class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
        """Rotating handler that keeps the active and archived logs private."""

        _process_lock_descriptor = -1

        def _open(self):  # type: ignore[no-untyped-def]
            stream = super()._open()
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                else:  # pragma: no cover - Windows compatibility
                    Path(self.baseFilename).chmod(0o600)
                reject_insecure_extended_acl(
                    stream.fileno(), description="Private log file"
                )
            except BaseException:
                stream.close()
                raise
            return stream

        def rotate(self, source: str, dest: str) -> None:
            super().rotate(source, dest)
            Path(dest).chmod(0o600)
            rotated_descriptor = open_secure_regular_file(dest)
            os.close(rotated_descriptor)

        def close(self) -> None:
            try:
                super().close()
            finally:
                descriptor = self._process_lock_descriptor
                self._process_lock_descriptor = -1
                _release_log_process_lock(descriptor)

    # Create logger
    root_logger = logging.getLogger()
    root_logger.setLevel(config.level)

    # Detach and close prior handlers. ``clear()`` alone leaks open rotating-log
    # descriptors when an embedded app reloads configuration repeatedly.
    for prior_handler in tuple(root_logger.handlers):
        root_logger.removeHandler(prior_handler)
        try:
            prior_handler.close()
        except Exception:
            # Logging teardown is best-effort and must not expose configuration
            # data or turn a safe reload into an application-startup failure.
            pass

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
        try:
            private_log_parent = prepare_private_directory(log_path.parent)
        except OSError as exc:
            raise ConfigurationError(
                f"Could not securely prepare log directory {log_path.parent}: {exc}"
            ) from exc
        log_path = private_log_parent / log_path.name
        if log_path.parent.is_symlink() or not log_path.parent.is_dir():
            raise ConfigurationError("Log parent must be a real directory")
        if log_path.is_symlink():
            raise ConfigurationError("Log file must not be a symbolic link")
        if os.name == "posix":
            parent_status = log_path.parent.stat()
            if parent_status.st_uid not in {0, os.geteuid()}:
                raise ConfigurationError(
                    "Log directory must be owned by this user or root"
                )
            if stat.S_IMODE(parent_status.st_mode) & 0o022:
                raise ConfigurationError(
                    "Log directory must not be group/world writable"
                )

        lock_descriptor = _acquire_log_process_lock(log_path)
        file_handler: PrivateRotatingFileHandler | None = None
        try:
            descriptor = os.open(
                log_path,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ConfigurationError("Log path is not a regular file")
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                reject_insecure_extended_acl(descriptor, description="Private log file")
            finally:
                os.close(descriptor)

            # Rotating file handler
            file_handler = PrivateRotatingFileHandler(
                log_path,
                maxBytes=config.file.max_size_mb * 1024 * 1024,
                backupCount=config.file.backup_count,
            )
            file_handler._process_lock_descriptor = lock_descriptor
            lock_descriptor = -1
            # Harden any existing rollover files as well. Renamed files retain
            # the active file's mode on future rollovers.
            for backup_number in range(1, config.file.backup_count + 1):
                backup_path = Path(f"{log_path}.{backup_number}")
                if backup_path.is_file():
                    backup_path.chmod(0o600)
                    backup_descriptor = open_secure_regular_file(backup_path)
                    os.close(backup_descriptor)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except BaseException:
            if file_handler is not None:
                file_handler.close()
            raise
        finally:
            _release_log_process_lock(lock_descriptor)

    logger.info(f"Logging configured - Level: {config.level}")
