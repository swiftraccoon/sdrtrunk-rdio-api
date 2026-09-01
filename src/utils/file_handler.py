"""File handling utilities for audio file storage and management."""

import asyncio
import errno
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal

from ..config import prepare_private_directory
from ..exceptions import FileSizeError
from ..filesystem_security import (
    durable_fsync,
    path_is_anchored_windows_relative,
    path_uses_dangerous_windows_namespace,
    paths_have_equivalent_filesystem_spelling,
    paths_overlap,
    reject_insecure_extended_acl,
)
from .sanitize import sanitize_filename
from .storage_quota import StorageCapacity, UploadCapacityReservation

logger = logging.getLogger(__name__)

_COPY_CHUNK_SIZE = 1024 * 1024
_DEFAULT_NAME_MAX = 255
_STORED_NAME_TOKEN_BYTES = 33  # underscore plus a 32-hex-character token
_MAX_ACTIVE_UPLOAD_LEASES = 4096
_MP3_CONTENT_TYPES = frozenset(
    {"audio/mpeg", "audio/mp3", "audio/x-mpeg", "audio/mpeg3", "audio/x-mp3"}
)


@dataclass(frozen=True, slots=True)
class FileDeletionResult:
    """Outcome of one containment-checked file deletion attempt."""

    status: Literal["deleted", "missing", "refused", "retry"]
    freed_bytes: int = 0
    error: str | None = None
    unlink_succeeded: bool = False


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    """Truncate text without cutting a UTF-8 code point."""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _safe_component(value: Any, maximum_bytes: int) -> str:
    """Return a bounded ASCII-only filename/directory component."""
    cleaned = "".join(
        (
            character
            if character.isascii() and (character.isalnum() or character in "-_")
            else "_"
        )
        for character in str(value)
    ).strip("._")
    return _truncate_utf8(cleaned or "unknown", maximum_bytes)


def _safe_log_text(value: Any, maximum: int = 512) -> str:
    """Bound log values and neutralize all Unicode control categories."""
    cleaned = "".join(
        "_" if unicodedata.category(character).startswith("C") else character
        for character in str(value)
    )
    return cleaned[:maximum]


def _upload_lease_monotonic() -> float:
    """Return a testable clock for bounded active-upload heartbeat metadata."""
    return time.monotonic()


def _lexical_absolute(path: str | Path) -> Path:
    """Return an absolute lexical path without following any component."""
    absolute = Path(path).expanduser()
    return absolute if absolute.is_absolute() else Path.cwd() / absolute


def _relative_under_validated_root(
    path_str: str,
    canonical_root: Path,
    configured_root: Path,
) -> Path:
    """Map a canonical or validated configured path to pinned-root components.

    ``prepare_private_directory`` validates ``configured_root`` at startup and
    may canonicalize only a trusted system alias (for example macOS ``/var``).
    Accepting that exact lexical prefix preserves old database paths, while
    walking the returned components beneath ``canonical_root`` avoids ever
    following the alias—or any attacker-inserted symlink—during file access.
    """
    if path_uses_dangerous_windows_namespace(path_str):
        raise ValueError("Path uses an unsafe Windows filesystem namespace")
    candidate = Path(path_str)
    if not candidate.is_absolute() and path_is_anchored_windows_relative(path_str):
        raise ValueError("Path is drive-relative or rooted-relative on Windows")
    if candidate.is_absolute():
        relative: Path | None = None
        for accepted_root in (canonical_root, configured_root):
            try:
                relative = candidate.relative_to(accepted_root)
                break
            except ValueError:
                continue
        if relative is None:
            candidate_parts = _lexical_absolute(candidate).parts
            canonical_status = os.stat(canonical_root, follow_symlinks=True)
            for accepted_root in (canonical_root, configured_root):
                accepted_parts = _lexical_absolute(accepted_root).parts
                if len(candidate_parts) < len(accepted_parts):
                    continue
                candidate_prefix = Path(*candidate_parts[: len(accepted_parts)])
                if not paths_have_equivalent_filesystem_spelling(
                    candidate_prefix, accepted_root
                ):
                    continue
                try:
                    prefix_status = os.stat(candidate_prefix, follow_symlinks=True)
                except OSError:
                    continue
                if (
                    stat.S_ISDIR(prefix_status.st_mode)
                    and stat.S_ISDIR(canonical_status.st_mode)
                    and (prefix_status.st_dev, prefix_status.st_ino)
                    == (canonical_status.st_dev, canonical_status.st_ino)
                ):
                    relative = Path(*candidate_parts[len(accepted_parts) :])
                    break
        if relative is None:
            raise ValueError("Path is outside the validated root")
    else:
        relative = candidate
    if not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError("Path is malformed")
    return relative


def _fsync_directory(directory: Path) -> None:
    """Persist a directory-entry change where the platform supports it."""
    if os.name != "posix":  # pragma: no cover - Windows has no directory fsync
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        durable_fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    """Flush a completed, non-symlink regular file before moving it."""
    descriptor = os.open(
        path,
        # Windows' CRT-backed fsync rejects read-only descriptors.
        os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Upload temporary path is not a regular file")
        reject_insecure_extended_acl(
            descriptor, description="Private upload temporary file"
        )
        durable_fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_temp_file(
    root: Path,
    expected_root_identity: tuple[int, int],
    suffix: str,
) -> tuple[int, Path]:
    """Create a mode-0600 temp file beneath the pinned application root."""
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        root_status = root.lstat()
        if (
            stat.S_ISLNK(root_status.st_mode)
            or not stat.S_ISDIR(root_status.st_mode)
            or (root_status.st_dev, root_status.st_ino) != expected_root_identity
        ):
            raise OSError("Application temp root changed after initialization")
        descriptor, raw_path = tempfile.mkstemp(
            prefix="upload_", suffix=suffix, dir=root
        )
        try:
            if os.fstat(descriptor).st_dev != expected_root_identity[0]:
                raise OSError("Application temp file crossed a filesystem boundary")
        except BaseException:
            try:
                os.close(descriptor)
            finally:
                Path(raw_path).unlink(missing_ok=True)
            raise
        return descriptor, Path(raw_path)

    directory_descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_status = os.fstat(directory_descriptor)
        if (root_status.st_dev, root_status.st_ino) != expected_root_identity:
            raise OSError("Application temp root changed after initialization")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(16):
            name = f"upload_{secrets.token_hex(16)}{suffix}"
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
            except FileExistsError:
                continue
            try:
                file_status = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(file_status.st_mode)
                    or file_status.st_dev != root_status.st_dev
                ):
                    raise OSError("Application temp file is not on its root filesystem")
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                reject_insecure_extended_acl(
                    descriptor, description="Private upload temporary file"
                )
            except BaseException:
                try:
                    os.close(descriptor)
                finally:
                    try:
                        os.unlink(name, dir_fd=directory_descriptor)
                    except OSError:
                        pass
                raise
            return descriptor, root / name
        raise FileExistsError("Could not allocate a unique temporary filename")
    finally:
        os.close(directory_descriptor)


def _secure_directory(path: Path) -> None:
    """Validate and protect one application-owned directory without symlinks."""
    path_status = path.lstat()
    if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISDIR(path_status.st_mode):
        raise OSError(f"Application path is not a real directory: {path}")
    if os.name == "posix":
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(f"Application path is not a directory: {path}")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o700)
            else:  # pragma: no cover - defensive POSIX fallback
                path.chmod(0o700)
            reject_insecure_extended_acl(
                descriptor, description="Private application directory"
            )
        finally:
            os.close(descriptor)
    else:  # pragma: no cover - exercised by Windows CI
        path.chmod(0o700)


def _mkdir_private_tree(
    root: Path,
    relative: Path,
    expected_root_identity: tuple[int, int] | None = None,
) -> Path:
    """Create each application-owned path component with mode 0700.

    ``Path.mkdir(parents=True, mode=...)`` only applies ``mode`` reliably to
    the leaf; parents are affected by the process umask. Building the known,
    relative date/system tree one component at a time avoids that ambiguity.
    """
    if any(component in {"", ".", ".."} for component in relative.parts):
        raise ValueError("Invalid storage path component")

    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        root_status = root.stat()
        root_identity = (root_status.st_dev, root_status.st_ino)
        if (
            expected_root_identity is not None
            and root_identity != expected_root_identity
        ):
            raise OSError("Storage root changed after initialization")
        current = root
        for component in relative.parts:
            current /= component
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                current_status = current.lstat()
                if stat.S_ISLNK(current_status.st_mode) or not stat.S_ISDIR(
                    current_status.st_mode
                ):
                    raise OSError(
                        "Storage directory component is not a real directory"
                    ) from None
            _secure_directory(current)
            current_status = current.lstat()
            if current_status.st_dev != root_status.st_dev:
                raise OSError("Storage tree must remain on its root filesystem")
        return current

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, directory_flags)
    try:
        root_status = os.fstat(descriptor)
        if (
            expected_root_identity is not None
            and (
                root_status.st_dev,
                root_status.st_ino,
            )
            != expected_root_identity
        ):
            raise OSError("Storage root changed after initialization")

        for component in relative.parts:
            created = False
            try:
                child_descriptor = os.open(
                    component, directory_flags, dir_fd=descriptor
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    # Persist each ancestor name immediately; fsyncing only the
                    # eventual leaf does not make its parent chain durable.
                    durable_fsync(descriptor)
                child_descriptor = os.open(
                    component, directory_flags, dir_fd=descriptor
                )

            try:
                child_status = os.fstat(child_descriptor)
                if child_status.st_dev != root_status.st_dev:
                    raise OSError("Storage tree must remain on its root filesystem")
                child_mode = stat.S_IMODE(child_status.st_mode)
                if child_status.st_uid != os.geteuid():
                    raise PermissionError(
                        "Storage directory components must be owned by this user"
                    )
                if not created and child_mode & 0o022:
                    raise PermissionError(
                        "Storage directory components must not be group/world writable"
                    )
                if hasattr(os, "fchmod"):
                    os.fchmod(child_descriptor, 0o700)
                reject_insecure_extended_acl(
                    child_descriptor, description="Private storage directory"
                )
            except BaseException:
                os.close(child_descriptor)
                raise

            old_descriptor = descriptor
            descriptor = child_descriptor
            os.close(old_descriptor)
    finally:
        os.close(descriptor)
    return root / relative


class FileHandler:
    """Handles audio file storage and management."""

    def __init__(
        self,
        storage_directory: str,
        temp_directory: str,
        organize_by_date: bool = True,
        accepted_formats: list[str] | None = None,
        max_file_size_mb: int = 100,
        min_file_size_kb: int = 1,
    ):
        """Initialize file handler.

        Args:
            storage_directory: Directory for permanent file storage
            temp_directory: Directory for temporary files
            organize_by_date: Whether to organize files by date
            accepted_formats: List of accepted file extensions
            max_file_size_mb: Maximum file size in megabytes
            min_file_size_kb: Minimum file size in kilobytes
        """
        if any(
            path_uses_dangerous_windows_namespace(path)
            for path in (storage_directory, temp_directory)
        ):
            raise ValueError(
                "Storage paths cannot use reserved or ambiguous Windows filenames"
            )
        # Retain the exact validated lexical roots as aliases for paths stored
        # by older versions. File operations still walk only the canonical,
        # pinned root descriptors below.
        self._configured_storage_dir = _lexical_absolute(storage_directory)
        self._configured_temp_dir = _lexical_absolute(temp_directory)
        if paths_overlap(self._configured_storage_dir, self._configured_temp_dir):
            raise ValueError(
                "Storage and temporary directories must not contain one another"
            )
        self.storage_dir = prepare_private_directory(self._configured_storage_dir)
        self.temp_dir = prepare_private_directory(self._configured_temp_dir)
        # Re-check after creation/canonicalization and compare existing inode
        # identities. This closes case-insensitive and root-alias races that a
        # lexical check cannot represent.
        if paths_overlap(self.storage_dir, self.temp_dir):
            raise ValueError(
                "Storage and temporary directories must not contain one another"
            )
        storage_status = self.storage_dir.stat()
        temp_status = self.temp_dir.stat()
        self._storage_root_identity = (storage_status.st_dev, storage_status.st_ino)
        self._temp_root_identity = (temp_status.st_dev, temp_status.st_ino)
        self._temp_scan_lock = threading.Lock()
        self._temp_scan_entries: Any | None = None
        self._temp_activity_lock = threading.RLock()
        self._active_temp_leases: dict[str, float] = {}
        self._storage_activity_lock = threading.RLock()
        self._active_storage_leases: dict[str, float] = {}
        self._storage_capacity: StorageCapacity | None = None
        self.organize_by_date = organize_by_date
        self.accepted_formats = accepted_formats or [".mp3"]
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.min_file_size_bytes = min_file_size_kb * 1024

        logger.info(
            "File handler initialized - Storage: %r, Temp: %r",
            _safe_log_text(self.storage_dir),
            _safe_log_text(self.temp_dir),
        )

    def attach_storage_capacity(self, capacity: StorageCapacity) -> None:
        """Attach the single process-local tracker used for archive mutations."""
        if (
            self._storage_capacity is not None
            and self._storage_capacity is not capacity
        ):
            raise RuntimeError(
                "A different storage capacity tracker is already attached"
            )
        self._storage_capacity = capacity

    @contextmanager
    def maintenance_state_guard(self) -> Iterator[None]:
        """Protect one bounded state mutation when capacity tracking is attached."""
        capacity = self._storage_capacity
        if capacity is None:
            yield
            return
        with capacity.maintenance_state_guard():
            yield

    def storage_reference(self, path: str | Path) -> str:
        """Return the root-relative POSIX reference persisted for new audio."""
        relative = _relative_under_validated_root(
            str(path), self.storage_dir, self._configured_storage_dir
        )
        reference = relative.as_posix()
        if len(reference.encode("utf-8")) > 500:
            raise OSError("Stored file reference exceeds the database limit")
        return reference

    def _create_leased_temp(self, suffix: str) -> tuple[int, Path]:
        """Create a temp file and register it atomically against cleanup."""
        with self._temp_activity_lock:
            if len(self._active_temp_leases) >= _MAX_ACTIVE_UPLOAD_LEASES:
                raise OSError("Too many active upload temporary files")
            descriptor, temp_path = _open_private_temp_file(
                self.temp_dir,
                self._temp_root_identity,
                suffix,
            )
            self._active_temp_leases[temp_path.name] = _upload_lease_monotonic()
            return descriptor, temp_path

    def _heartbeat_temp_lease(self, temp_path: Path) -> None:
        """Extend a known live temp lease without registering arbitrary paths."""
        with self._temp_activity_lock:
            if temp_path.name in self._active_temp_leases:
                self._active_temp_leases[temp_path.name] = _upload_lease_monotonic()

    def _release_temp_lease(self, temp_path: str | Path) -> None:
        with self._temp_activity_lock:
            self._active_temp_leases.pop(Path(temp_path).name, None)

    def _register_storage_lease(self, storage_path: Path) -> str:
        """Protect a durably staged destination until its DB outcome is known."""
        reference = self.storage_reference(storage_path)
        with self._storage_activity_lock:
            if len(self._active_storage_leases) >= _MAX_ACTIVE_UPLOAD_LEASES:
                raise OSError("Too many active staged upload files")
            self._active_storage_leases[reference] = _upload_lease_monotonic()
        return reference

    def heartbeat_storage_lease(self, path: str | Path) -> None:
        """Extend a known staged destination lease without registering paths."""
        try:
            reference = self.storage_reference(path)
        except (OSError, RuntimeError, ValueError):
            return
        with self._storage_activity_lock:
            if reference in self._active_storage_leases:
                self._active_storage_leases[reference] = _upload_lease_monotonic()

    def release_storage_lease(self, path: str | Path) -> None:
        """Release an upload destination after DB save reconciliation finishes."""
        try:
            reference = self.storage_reference(path)
        except (OSError, RuntimeError, ValueError):
            return
        with self._storage_activity_lock:
            self._active_storage_leases.pop(reference, None)

    def _storage_lease_is_active_locked(self, reference: str) -> bool:
        """Check one canonical reference while ``_storage_activity_lock`` is held."""
        return reference in self._active_storage_leases

    def normalize_filename(self, filename: str) -> str:
        """Return a safe, bounded display filename while preserving its suffix."""
        sanitized = sanitize_filename(filename)
        sanitized = "".join(
            character
            for character in sanitized
            if not unicodedata.category(character).startswith("C")
        )
        if not sanitized:
            sanitized = "unnamed_file"
        suffix = Path(sanitized).suffix.lower()
        # Reserve room for the suffix and keep database/header values bounded by
        # bytes as well as characters.
        maximum = 240
        if suffix and len(suffix.encode("utf-8")) < 16:
            stem_budget = maximum - len(suffix.encode("utf-8"))
            stem = _truncate_utf8(Path(sanitized).stem, stem_budget)
            return f"{stem or 'audio'}{suffix}"
        return _truncate_utf8(sanitized, maximum)

    @staticmethod
    def _has_valid_signature(file_ext: str, header: bytes) -> bool:
        if file_ext == ".mp3":
            return header.startswith(b"ID3") or (
                len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
            )
        # Unknown formats must not silently become arbitrary-file upload sinks.
        return False

    def validate_file_metadata(
        self,
        filename: str,
        file_size: int | None,
        content_type: str | None,
    ) -> tuple[bool, str | None]:
        """Validate bounded metadata before copying any uploaded bytes."""
        file_ext = Path(filename).suffix.lower()
        accepted_formats = {item.lower() for item in self.accepted_formats}
        if file_ext not in accepted_formats:
            return False, "File format is not accepted"

        if file_size is not None:
            if file_size <= 0:
                return False, "File is empty"
            if file_size > self.max_file_size_bytes:
                return False, "File is too large"
            if file_size < self.min_file_size_bytes:
                return False, "File is too small"

        normalized_type = (content_type or "").partition(";")[0].strip().lower()
        if file_ext == ".mp3" and normalized_type not in _MP3_CONTENT_TYPES:
            return False, "Invalid content type for MP3 audio"
        if file_ext != ".mp3":
            return False, "No secure content validator is available for this format"
        return True, None

    def validate_file(
        self, filename: str, content: bytes, content_type: str | None = None
    ) -> tuple[bool, str | None]:
        """Validate an in-memory file (compatibility API for direct callers)."""
        valid, error = self.validate_file_metadata(filename, len(content), content_type)
        if not valid:
            return valid, error
        if not self._has_valid_signature(Path(filename).suffix.lower(), content[:16]):
            return False, "File content does not match MP3 audio"
        return True, None

    async def validate_upload_file(
        self, filename: str, upload: Any, content_type: str | None
    ) -> tuple[bool, str | None]:
        """Validate a Starlette UploadFile without loading it into memory."""
        file_size = getattr(upload, "size", None)
        valid, error = self.validate_file_metadata(filename, file_size, content_type)
        if not valid:
            return valid, error

        await upload.seek(0)
        header = await upload.read(16)
        await upload.seek(0)
        if not self._has_valid_signature(Path(filename).suffix.lower(), header):
            return False, "File content does not match MP3 audio"
        return True, None

    def validate_file_path(
        self, filename: str, path: Path, content_type: str | None
    ) -> tuple[bool, str | None]:
        """Validate the actual bounded temporary file before persistence."""
        try:
            file_size = path.stat().st_size
            with path.open("rb") as source:
                header = source.read(16)
        except OSError:
            return False, "Uploaded file could not be validated"
        valid, error = self.validate_file_metadata(filename, file_size, content_type)
        if not valid:
            return valid, error
        if not self._has_valid_signature(Path(filename).suffix.lower(), header):
            return False, "File content does not match MP3 audio"
        return True, None

    def save_temp_file(self, filename: str, content: bytes) -> Path:
        """Save content to a temporary file.

        Args:
            filename: Original filename
            content: File content

        Returns:
            Path to temporary file
        """
        normalized = self.normalize_filename(filename)
        suffix = Path(normalized).suffix[:15]
        descriptor, temp_path = self._create_leased_temp(suffix)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                durable_fsync(output.fileno())
            if os.name != "posix":  # pragma: no cover - Windows compatibility
                temp_path.chmod(0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.delete_temp_file(str(temp_path))
            raise
        logger.debug("Saved temporary upload %r", _safe_log_text(temp_path))
        return temp_path

    async def save_upload_file(self, filename: str, upload: Any) -> Path:
        """Compatibility wrapper; production uses joined worker orchestration."""
        raw_stream = getattr(upload, "file", None)
        if raw_stream is not None:
            return await asyncio.to_thread(
                self.save_upload_stream, filename, raw_stream
            )

        # Compatibility for custom async UploadFile-like objects. Starlette's
        # real UploadFile always takes the worker-only branch above.
        normalized = self.normalize_filename(filename)
        suffix = Path(normalized).suffix[:15]
        descriptor, temp_path = self._create_leased_temp(suffix)
        written = 0
        try:
            await upload.seek(0)
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = await upload.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_file_size_bytes:
                        raise FileSizeError("Uploaded file exceeds configured maximum")
                    output.write(chunk)
                    self._heartbeat_temp_lease(temp_path)
                output.flush()
                durable_fsync(output.fileno())
            if os.name != "posix":  # pragma: no cover - Windows compatibility
                temp_path.chmod(0o600)
            if written < self.min_file_size_bytes:
                raise FileSizeError("Uploaded file is smaller than configured minimum")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.delete_temp_file(str(temp_path))
            raise
        logger.debug("Streamed temporary upload %r", _safe_log_text(temp_path))
        return temp_path

    def save_upload_stream(self, filename: str, source: BinaryIO) -> Path:
        """Copy and fsync a parsed upload stream in a synchronous worker."""
        normalized = self.normalize_filename(filename)
        suffix = Path(normalized).suffix[:15]
        descriptor, temp_path = self._create_leased_temp(suffix)
        written = 0
        try:
            source.seek(0)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while True:
                    chunk = source.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_file_size_bytes:
                        raise FileSizeError("Uploaded file exceeds configured maximum")
                    output.write(chunk)
                    self._heartbeat_temp_lease(temp_path)
                output.flush()
                durable_fsync(output.fileno())
            if os.name != "posix":  # pragma: no cover - Windows compatibility
                temp_path.chmod(0o600)
            if written < self.min_file_size_bytes:
                raise FileSizeError("Uploaded file is smaller than configured minimum")
        except BaseException:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self.delete_temp_file(str(temp_path))
            raise
        logger.debug("Streamed temporary upload %r", _safe_log_text(temp_path))
        return temp_path

    def store_file(
        self,
        temp_path: Path,
        system_id: str,
        timestamp: datetime,
        talkgroup_id: int | None = None,
        talkgroup_label: str | None = None,
        frequency: int | None = None,
        source_id: int | None = None,
        talker_alias: str | None = None,
        system_label: str | None = None,
        *,
        on_destination_reserved: Callable[[str], None] | None = None,
        capacity_reservation: UploadCapacityReservation | None = None,
    ) -> Path:
        """Publish and account one stored file in one mutation critical section."""
        capacity = self._storage_capacity
        if capacity is None:
            if capacity_reservation is not None:
                raise RuntimeError("Storage capacity tracker is not attached")
            return self._store_file_unaccounted(
                temp_path,
                system_id,
                timestamp,
                talkgroup_id,
                talkgroup_label,
                frequency,
                source_id,
                talker_alias,
                system_label,
                on_destination_reserved=on_destination_reserved,
            )

        with capacity.mutation_guard():
            try:
                stored_path = self._store_file_unaccounted(
                    temp_path,
                    system_id,
                    timestamp,
                    talkgroup_id,
                    talkgroup_label,
                    frequency,
                    source_id,
                    talker_alias,
                    system_label,
                    on_destination_reserved=on_destination_reserved,
                )
                if capacity_reservation is None:
                    # A direct caller bypassed admission, so exact accounting
                    # requires a complete reconciliation before more uploads.
                    capacity.mark_uncertain()
                else:
                    capacity_reservation.commit_stored_path(stored_path)
                return stored_path
            except BaseException:
                # The durable destination callback may have committed before a
                # later filesystem failure. No returned path means the caller
                # cannot know whether cleanup succeeded, so close admission.
                capacity.mark_uncertain()
                raise

    def _store_file_unaccounted(
        self,
        temp_path: Path,
        system_id: str,
        timestamp: datetime,
        talkgroup_id: int | None = None,
        talkgroup_label: str | None = None,
        frequency: int | None = None,
        source_id: int | None = None,
        talker_alias: str | None = None,
        system_label: str | None = None,
        *,
        on_destination_reserved: Callable[[str], None] | None = None,
    ) -> Path:
        """Move file from temp to permanent storage with descriptive filename.

        Args:
            temp_path: Path to temporary file
            system_id: System ID
            timestamp: Call timestamp
            talkgroup_id: Talkgroup ID
            talkgroup_label: Human-readable talkgroup label
            frequency: Frequency in Hz
            source_id: Source radio ID
            talker_alias: Talker alias/name
            system_label: Human-readable system label

        Returns:
            Path to stored file
        """
        self._heartbeat_temp_lease(temp_path)
        # Build storage path. Defense-in-depth sanitization keeps direct callers
        # from turning a system identifier into a path component.
        safe_system_id = _safe_component(system_id, 32)
        if self.organize_by_date:
            # Organize by date: storage/YYYY/MM/DD/system_id/
            # Use Path to construct date path for cross-platform compatibility
            date_path = (
                Path(timestamp.strftime("%Y"))
                / timestamp.strftime("%m")
                / timestamp.strftime("%d")
            )
            storage_relative = date_path / safe_system_id
        else:
            # Flat organization: storage/system_id/
            storage_relative = Path(safe_system_id)

        storage_subdir = _mkdir_private_tree(
            self.storage_dir,
            storage_relative,
            self._storage_root_identity,
        )

        # Build verbose filename with all available metadata
        # Format: YYYYMMDD_HHMMSS_SYS[system]_TG[id]_[label]_FREQ[freq]_SRC[id]_[alias].ext
        components = []

        # Timestamp (always present)
        components.append(timestamp.strftime("%Y%m%d_%H%M%S"))

        # System info
        sys_str = f"SYS{safe_system_id}"
        if system_label:
            safe_label = _safe_component(system_label, 30)
            sys_str = f"{sys_str}_{safe_label}"
        components.append(sys_str)

        # Talkgroup info
        if talkgroup_id:
            tg_str = f"TG{talkgroup_id}"
            if talkgroup_label:
                safe_label = _safe_component(talkgroup_label, 30)
                tg_str = f"{tg_str}_{safe_label}"
            components.append(tg_str)

        # Frequency info (convert Hz to MHz for readability)
        if frequency:
            freq_mhz = frequency / 1_000_000
            components.append(f"FREQ{freq_mhz:.4f}MHz")

        # Source/Unit info
        if source_id:
            src_str = f"SRC{source_id}"
            if talker_alias:
                safe_alias = _safe_component(talker_alias, 20)
                src_str = f"{src_str}_{safe_alias}"
            components.append(src_str)

        # Join all components with underscores
        candidate_suffix = temp_path.suffix.lower()
        suffix = (
            candidate_suffix
            if candidate_suffix.startswith(".")
            and len(candidate_suffix.encode("utf-8")) <= 15
            and candidate_suffix[1:].isascii()
            and candidate_suffix[1:].isalnum()
            else ".bin"
        )
        try:
            name_max = int(os.pathconf(storage_subdir, "PC_NAME_MAX"))
        except (AttributeError, OSError, ValueError):
            name_max = _DEFAULT_NAME_MAX
        name_max = min(name_max, _DEFAULT_NAME_MAX)
        base_budget = name_max - len(suffix.encode("utf-8")) - _STORED_NAME_TOKEN_BYTES
        if base_budget < 1:
            raise OSError("Filesystem filename limit is too small")
        base_filename = _truncate_utf8("_".join(components), base_budget)

        # A random suffix avoids the O(n^2) duplicate-name scan and makes every
        # destination independently claimable. Hard-linking is atomic and does
        # not duplicate file contents. Cross-device setups use bounded copying
        # into an exclusively created destination instead.
        _fsync_regular_file(temp_path)
        os.chmod(temp_path, 0o600)
        for _ in range(16):
            token = secrets.token_hex(16)
            storage_path = storage_subdir / f"{base_filename}_{token}{suffix}"
            staged_reference: str | None = None
            if on_destination_reserved is not None:
                # The callback must durably commit before the filesystem name
                # is published. Protect that durable staging row from cleanup
                # until the caller resolves the subsequent RadioCall commit.
                staged_reference = self._register_storage_lease(storage_path)
                try:
                    on_destination_reserved(staged_reference)
                except BaseException:
                    self.release_storage_lease(staged_reference)
                    raise
            try:
                os.link(temp_path, storage_path)
            except FileExistsError:
                if staged_reference is not None:
                    self.release_storage_lease(staged_reference)
                continue
            except OSError as exc:
                if exc.errno not in {
                    errno.EXDEV,
                    errno.EPERM,
                    errno.EACCES,
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                }:
                    if staged_reference is not None:
                        self.release_storage_lease(staged_reference)
                    raise
                try:
                    descriptor = os.open(
                        storage_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    if staged_reference is not None:
                        self.release_storage_lease(staged_reference)
                    continue
                except OSError:
                    if staged_reference is not None:
                        self.release_storage_lease(staged_reference)
                    raise
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(descriptor, 0o600)
                    reject_insecure_extended_acl(
                        descriptor, description="Private stored audio file"
                    )
                    with (
                        temp_path.open("rb") as source,
                        os.fdopen(descriptor, "wb") as destination,
                    ):
                        while chunk := source.read(_COPY_CHUNK_SIZE):
                            destination.write(chunk)
                            self._heartbeat_temp_lease(temp_path)
                            if staged_reference is not None:
                                self.heartbeat_storage_lease(staged_reference)
                        destination.flush()
                        durable_fsync(destination.fileno())
                except Exception:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    self._delete_file_under_root(
                        str(storage_path),
                        self.storage_dir,
                        self._configured_storage_dir,
                        self._storage_root_identity,
                    )
                    if staged_reference is not None:
                        self.release_storage_lease(staged_reference)
                    raise

            # Persist the new destination name before dropping the only prior
            # name for the completed upload.
            try:
                _fsync_directory(storage_subdir)
            except OSError:
                if staged_reference is not None:
                    self.release_storage_lease(staged_reference)
                raise
            try:
                temp_path.unlink()
            except OSError:
                # Do not leave an untracked permanent file if move semantics
                # cannot be completed after the link/copy succeeded.
                self._delete_file_under_root(
                    str(storage_path),
                    self.storage_dir,
                    self._configured_storage_dir,
                    self._storage_root_identity,
                )
                if staged_reference is not None:
                    self.release_storage_lease(staged_reference)
                raise
            self._release_temp_lease(temp_path)
            try:
                _fsync_directory(temp_path.parent)
            except BaseException:
                # The destination name and its staging row are already
                # durable, while the source name has been unlinked. If the
                # temp-directory flush fails, the caller cannot receive the
                # destination path and therefore cannot release this lease in
                # its endpoint finalizer. Hand ownership back to the durable
                # staging queue before propagating the failure so recovery can
                # delete the otherwise orphaned destination.
                if staged_reference is not None:
                    self.release_storage_lease(staged_reference)
                raise
            logger.info("Stored file: %r", _safe_log_text(storage_path))
            return storage_path

        raise FileExistsError("Could not allocate a unique stored filename")

    def cleanup_temp_files(
        self, max_age_hours: int = 1, work_budget: int = 1000
    ) -> int:
        """Clean a bounded number of application-owned temporary files.

        Args:
            max_age_hours: Maximum age of temp files in hours
            work_budget: Maximum directory entries examined during this call

        Returns:
            Number of files cleaned up
        """
        if max_age_hours < 0:
            raise ValueError("max_age_hours must not be negative")
        if work_budget < 1:
            return 0

        now_timestamp = datetime.now().timestamp()
        cleaned = 0
        examined = 0

        # Keep one streaming cursor across cycles. Restarting scandir from the
        # beginning would let a stable ineligible prefix starve old uploads
        # forever. The lock also prevents concurrent maintenance callers from
        # consuming or closing the same directory descriptor.
        with self._temp_scan_lock:
            if self._temp_scan_entries is None:
                self._temp_scan_entries = os.scandir(self.temp_dir)
            entries = self._temp_scan_entries
            while examined < work_budget:
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    self._temp_scan_entries = None
                    break
                except OSError:
                    entries.close()
                    self._temp_scan_entries = None
                    raise
                examined += 1
                if not entry.name.startswith("upload_"):
                    continue
                try:
                    file_status = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(file_status.st_mode):
                    continue
                if now_timestamp - file_status.st_mtime <= max_age_hours * 3600:
                    continue
                with self._temp_activity_lock:
                    # Active membership never expires within this process: a
                    # single filesystem call can block arbitrarily long. The
                    # registry is hard-capped, and explicit finalization,
                    # close, or process death releases its bounded entries.
                    if entry.name in self._active_temp_leases:
                        continue
                    result = self.delete_temp_file(entry.path)
                if result.status == "deleted":
                    cleaned += 1

        if cleaned > 0:
            logger.info("Cleaned up %d old temp files", cleaned)
        return cleaned

    def close(self) -> None:
        """Release the retained bounded temp-directory scan descriptor."""
        with self._temp_scan_lock:
            if self._temp_scan_entries is not None:
                self._temp_scan_entries.close()
                self._temp_scan_entries = None
        with self._temp_activity_lock:
            self._active_temp_leases.clear()
        with self._storage_activity_lock:
            self._active_storage_leases.clear()

    def _delete_file_under_root(
        self,
        path_str: str,
        root: Path,
        configured_root: Path,
        expected_root_identity: tuple[int, int],
    ) -> FileDeletionResult:
        """Delete a regular file beneath an unchanged root without symlink walks."""
        try:
            relative = _relative_under_validated_root(path_str, root, configured_root)
        except (OSError, RuntimeError, ValueError):
            return FileDeletionResult("refused")

        if os.name != "posix":  # pragma: no cover - exercised by Windows CI
            try:
                root_status = root.stat()
                if (root_status.st_dev, root_status.st_ino) != expected_root_identity:
                    return FileDeletionResult("retry", error="storage root changed")
                current = root
                for component in relative.parts[:-1]:
                    current /= component
                    current_status = current.lstat()
                    if stat.S_ISLNK(current_status.st_mode) or not stat.S_ISDIR(
                        current_status.st_mode
                    ):
                        return FileDeletionResult("refused")
                    if current_status.st_dev != root_status.st_dev:
                        return FileDeletionResult("refused")
                target = current / relative.parts[-1]
                target_status = target.lstat()
                if stat.S_ISLNK(target_status.st_mode) or not stat.S_ISREG(
                    target_status.st_mode
                ):
                    return FileDeletionResult("refused")
                if target_status.st_dev != root_status.st_dev:
                    return FileDeletionResult("refused")
                target.unlink()
                return FileDeletionResult("deleted", freed_bytes=target_status.st_size)
            except FileNotFoundError:
                return FileDeletionResult("missing")
            except OSError as exc:
                return FileDeletionResult("retry", error=_safe_log_text(exc))

        descriptors: list[int] = []
        try:
            try:
                root_descriptor = os.open(
                    root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                return FileDeletionResult("retry", error=_safe_log_text(exc))
            descriptors.append(root_descriptor)
            root_status = os.fstat(root_descriptor)
            if (root_status.st_dev, root_status.st_ino) != expected_root_identity:
                return FileDeletionResult("retry", error="storage root changed")

            directory_descriptor = root_descriptor
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for component in relative.parts[:-1]:
                try:
                    next_descriptor = os.open(
                        component, directory_flags, dir_fd=directory_descriptor
                    )
                except FileNotFoundError:
                    durable_fsync(directory_descriptor)
                    return FileDeletionResult("missing")
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        return FileDeletionResult("refused")
                    return FileDeletionResult("retry", error=_safe_log_text(exc))
                descriptors.append(next_descriptor)
                if os.fstat(next_descriptor).st_dev != root_status.st_dev:
                    return FileDeletionResult("refused")
                directory_descriptor = next_descriptor

            filename = relative.parts[-1]
            try:
                file_status = os.stat(
                    filename,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                durable_fsync(directory_descriptor)
                return FileDeletionResult("missing")
            if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISREG(
                file_status.st_mode
            ):
                return FileDeletionResult("refused")
            if file_status.st_dev != root_status.st_dev:
                return FileDeletionResult("refused")

            os.unlink(filename, dir_fd=directory_descriptor)
            try:
                durable_fsync(directory_descriptor)
            except OSError as exc:
                # The namespace mutation already happened in this process, so
                # logical capacity must be released now. Keep the queue entry
                # retryable until a later missing-path pass fsyncs the parent.
                return FileDeletionResult(
                    "retry",
                    freed_bytes=file_status.st_size,
                    error=_safe_log_text(exc),
                    unlink_succeeded=True,
                )
            return FileDeletionResult("deleted", freed_bytes=file_status.st_size)
        except OSError as exc:
            return FileDeletionResult("retry", error=_safe_log_text(exc))
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def delete_file(self, path_str: str) -> FileDeletionResult:
        """Delete one regular stored file with fail-closed containment checks.

        Missing and structurally unsafe paths are terminal outcomes. Ordinary
        filesystem errors are retryable so a durable caller can keep its work.
        """

        def delete_if_inactive() -> FileDeletionResult:
            # Keep the lease test and namespace mutation indivisible. This is
            # especially important for startup/unit callers without a capacity
            # tracker, because a durable stage exists before publication.
            with self._storage_activity_lock:
                try:
                    reference = self.storage_reference(path_str)
                except (OSError, RuntimeError, ValueError):
                    reference = None
                if reference is not None and self._storage_lease_is_active_locked(
                    reference
                ):
                    return FileDeletionResult(
                        "retry", error="stored upload is still active"
                    )
                return self._delete_file_under_root(
                    path_str,
                    self.storage_dir,
                    self._configured_storage_dir,
                    self._storage_root_identity,
                )

        capacity = self._storage_capacity
        if capacity is None:
            result = delete_if_inactive()
        else:
            with capacity.mutation_guard():
                result = delete_if_inactive()
                if result.status == "deleted" or result.unlink_succeeded:
                    capacity.record_deleted(result.freed_bytes)
        if result.status == "refused":
            logger.warning(
                "Refusing unsafe storage deletion path: %r",
                _safe_log_text(path_str),
            )
        elif result.status == "retry":
            logger.error(
                "Failed to delete file %r: %r",
                _safe_log_text(path_str),
                result.error or "unknown filesystem error",
            )
        return result

    @contextmanager
    def open_stored_file(self, path_str: str) -> Iterator[BinaryIO]:
        """Open a pinned regular stored file without following symlinks.

        The caller owns the yielded stream only for the context duration. On
        POSIX, every component is opened relative to the already-verified root
        descriptor and the final inode is checked before any bytes are read.
        """
        try:
            relative = _relative_under_validated_root(
                path_str, self.storage_dir, self._configured_storage_dir
            )
        except ValueError as exc:
            raise PermissionError(
                "Stored file path is outside the storage root"
            ) from exc

        if os.name != "posix":  # pragma: no cover - exercised by Windows CI
            root_status = self.storage_dir.stat()
            if (
                root_status.st_dev,
                root_status.st_ino,
            ) != self._storage_root_identity:
                raise OSError("Storage root changed after initialization")
            current = self.storage_dir
            for component in relative.parts[:-1]:
                current /= component
                component_status = current.lstat()
                if stat.S_ISLNK(component_status.st_mode) or not stat.S_ISDIR(
                    component_status.st_mode
                ):
                    raise PermissionError("Stored file path contains a symlink")
                if component_status.st_dev != root_status.st_dev:
                    raise PermissionError(
                        "Stored file path crosses a filesystem boundary"
                    )
            target = current / relative.parts[-1]
            descriptor = os.open(
                target,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                file_status = os.fstat(descriptor)
                if not stat.S_ISREG(file_status.st_mode):
                    raise PermissionError("Stored audio path is not a regular file")
                if file_status.st_dev != root_status.st_dev:
                    raise PermissionError(
                        "Stored audio path crosses a filesystem boundary"
                    )
                reject_insecure_extended_acl(
                    descriptor, description="Private stored audio file"
                )
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    yield stream
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            return

        directory_descriptors: list[int] = []
        file_descriptor = -1
        try:
            root_descriptor = os.open(
                self.storage_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            directory_descriptors.append(root_descriptor)
            root_status = os.fstat(root_descriptor)
            if (root_status.st_dev, root_status.st_ino) != self._storage_root_identity:
                raise OSError("Storage root changed after initialization")

            current_descriptor = root_descriptor
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for component in relative.parts[:-1]:
                current_descriptor = os.open(
                    component, directory_flags, dir_fd=current_descriptor
                )
                directory_descriptors.append(current_descriptor)
                if os.fstat(current_descriptor).st_dev != root_status.st_dev:
                    raise PermissionError(
                        "Stored file path crosses a filesystem boundary"
                    )

            file_descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_descriptor,
            )
            file_status = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise PermissionError("Stored audio path is not a regular file")
            if file_status.st_dev != root_status.st_dev:
                raise PermissionError("Stored audio path crosses a filesystem boundary")
            reject_insecure_extended_acl(
                file_descriptor, description="Private stored audio file"
            )
            with os.fdopen(file_descriptor, "rb") as stream:
                file_descriptor = -1
                yield stream
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            for descriptor in reversed(directory_descriptors):
                os.close(descriptor)

    def delete_temp_file(self, path_str: str) -> FileDeletionResult:
        """Delete one application-owned upload temporary file safely."""
        if not Path(path_str).name.startswith("upload_"):
            return FileDeletionResult("refused")
        with self._temp_activity_lock:
            self._active_temp_leases.pop(Path(path_str).name, None)
            result = self._delete_file_under_root(
                path_str,
                self.temp_dir,
                self._configured_temp_dir,
                self._temp_root_identity,
            )
        if result.status == "retry":
            logger.error(
                "Failed to delete temporary file %r: %r",
                _safe_log_text(path_str),
                result.error or "unknown filesystem error",
            )
        return result

    def delete_files(self, paths: list[str]) -> tuple[int, int]:
        """Delete specific stored files.

        Paths outside the storage directory are refused, so corrupt or
        malicious database rows cannot delete arbitrary files.

        Args:
            paths: Absolute or relative paths to delete

        Returns:
            Tuple of (files deleted, bytes freed)
        """
        deleted = 0
        freed = 0

        for path_str in paths:
            result = self.delete_file(path_str)
            if result.status == "deleted":
                deleted += 1
                freed += result.freed_bytes

        if deleted:
            logger.info(
                "Deleted %d files, freed %.2f MB",
                deleted,
                freed / (1024 * 1024),
            )
        return deleted, freed

    def remove_empty_directories(
        self,
        deleted_paths: Collection[str] = (),
        work_budget: int = 1000,
        *,
        require_complete: bool = False,
    ) -> int:
        """Prune known empty parents under the archive mutation guard.

        With ``require_complete``, unsafe traversal, exhaustion, and namespace
        durability failures are raised. Durable deletion workers can therefore
        retain their queue row rather than forgetting incomplete directory work.
        """
        capacity = self._storage_capacity
        if capacity is None:
            return self._remove_empty_directories_unaccounted(
                deleted_paths,
                work_budget,
                require_complete=require_complete,
            )
        with capacity.mutation_guard():
            removed = self._remove_empty_directories_unaccounted(
                deleted_paths,
                work_budget,
                require_complete=require_complete,
            )
            if removed:
                capacity.record_structure_mutation()
            return removed

    def directory_prune_work(self, deleted_path: str) -> int | None:
        """Return the full parent-chain attempt budget for an owned file path.

        ``None`` means the path cannot be mapped beneath the validated storage
        root. A terminal deletion queue row is not safe to acknowledge until
        this entire bounded chain has been offered to the no-follow pruner.
        """
        try:
            relative = _relative_under_validated_root(
                deleted_path, self.storage_dir, self._configured_storage_dir
            )
        except ValueError:
            return None
        directory_parts = relative.parts[:-1]
        if any(component in {"", ".", ".."} for component in directory_parts):
            return None
        return len(directory_parts)

    def _remove_empty_directories_unaccounted(
        self,
        deleted_paths: Collection[str] = (),
        work_budget: int = 1000,
        *,
        require_complete: bool = False,
    ) -> int:
        """Prune only known parent chains of files successfully deleted.

        The strict mode continues upward when a descendant is already missing
        and fsyncs each POSIX parent after either observing or creating that
        absence. It returns normally only when no remaining ancestor can be
        pruned, or the storage root is reached.
        """
        if work_budget < 1:
            if require_complete and any(
                (self.directory_prune_work(path) or 0) > 0 for path in deleted_paths
            ):
                raise RuntimeError("Directory prune work budget was exhausted")
            return 0

        removed = 0
        attempted = 0
        for path_str in deleted_paths:
            try:
                relative = _relative_under_validated_root(
                    path_str, self.storage_dir, self._configured_storage_dir
                )
            except ValueError:
                if require_complete:
                    raise
                continue
            directory_parts = relative.parts[:-1]
            if any(component in {"", ".", ".."} for component in directory_parts):
                if require_complete:
                    raise ValueError("Unsafe directory component in stored file path")
                continue
            if not directory_parts:
                continue
            if len(directory_parts) > work_budget - attempted:
                if require_complete:
                    raise RuntimeError("Directory prune work budget was exhausted")
                break

            if os.name != "posix":  # pragma: no cover - Windows CI
                root_status = self.storage_dir.stat()
                if (
                    root_status.st_dev,
                    root_status.st_ino,
                ) != self._storage_root_identity:
                    if require_complete:
                        raise RuntimeError(
                            "Storage root identity changed during pruning"
                        )
                    continue
                directory = self.storage_dir
                existing_directories: list[Path] = []
                for component in directory_parts:
                    directory /= component
                    try:
                        component_status = directory.lstat()
                    except FileNotFoundError:
                        break
                    except OSError:
                        if require_complete:
                            raise
                        existing_directories = []
                        break
                    if (
                        stat.S_ISLNK(component_status.st_mode)
                        or not stat.S_ISDIR(component_status.st_mode)
                        or component_status.st_dev != root_status.st_dev
                    ):
                        if require_complete:
                            raise OSError("Unsafe directory component during pruning")
                        existing_directories = []
                        break
                    existing_directories.append(directory)

                for directory in reversed(existing_directories):
                    attempted += 1
                    try:
                        if directory.is_symlink():
                            if require_complete:
                                raise OSError(
                                    "Unsafe symbolic link appeared during pruning"
                                )
                            break
                        directory.rmdir()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                            if require_complete:
                                raise
                        break
                    removed += 1
                continue

            descriptors: list[int] = []
            opened_names: list[str] = []
            try:
                root_descriptor = os.open(
                    self.storage_dir,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                descriptors.append(root_descriptor)
                root_status = os.fstat(root_descriptor)
                if (
                    root_status.st_dev,
                    root_status.st_ino,
                ) != self._storage_root_identity:
                    if require_complete:
                        raise RuntimeError(
                            "Storage root identity changed during pruning"
                        )
                    continue

                current_descriptor = root_descriptor
                valid_chain = True
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                for component in directory_parts:
                    try:
                        next_descriptor = os.open(
                            component,
                            directory_flags,
                            dir_fd=current_descriptor,
                        )
                    except FileNotFoundError:
                        break
                    except OSError:
                        if require_complete:
                            raise
                        valid_chain = False
                        break
                    descriptors.append(next_descriptor)
                    if os.fstat(next_descriptor).st_dev != root_status.st_dev:
                        if require_complete:
                            raise OSError(
                                "Directory crossed a filesystem boundary during pruning"
                            )
                        valid_chain = False
                        break
                    opened_names.append(component)
                    current_descriptor = next_descriptor

                if not valid_chain:
                    continue
                for index in range(len(opened_names) - 1, -1, -1):
                    attempted += 1
                    parent_descriptor = descriptors[index]
                    try:
                        os.rmdir(opened_names[index], dir_fd=parent_descriptor)
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            if require_complete:
                                durable_fsync(parent_descriptor)
                            continue
                        if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                            break
                        if require_complete:
                            raise
                        break
                    if require_complete:
                        durable_fsync(parent_descriptor)
                    else:
                        try:
                            durable_fsync(parent_descriptor)
                        except OSError:
                            break
                    removed += 1
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)

        if removed:
            logger.info("Removed %d empty directories", removed)
        return removed

    def get_storage_stats(self) -> dict[str, Any]:
        """Get storage statistics.

        Returns:
            Dictionary with storage stats
        """
        stats: dict[str, Any] = {
            "total_files": 0,
            "total_size_bytes": 0,
            "total_size_mb": 0,
            "by_system": {},  # Changed key name
            "files_by_date": {},
        }

        # Walk through storage directory
        for file_path in self.storage_dir.rglob("*"):
            if file_path.is_file():
                stats["total_files"] += 1
                file_size = file_path.stat().st_size
                stats["total_size_bytes"] += file_size
                stats["total_size_mb"] += file_size / (1024 * 1024)

                # Extract system from path
                parts = file_path.relative_to(self.storage_dir).parts
                if parts:
                    if self.organize_by_date and len(parts) > 3:
                        # Date organized: YYYY/MM/DD/system/file
                        system = parts[3]
                        date = f"{parts[0]}-{parts[1]}-{parts[2]}"

                        if system not in stats["by_system"]:
                            stats["by_system"][system] = {"count": 0, "size_bytes": 0}
                        stats["by_system"][system]["count"] += 1
                        stats["by_system"][system]["size_bytes"] += file_size

                        stats["files_by_date"][date] = (
                            stats["files_by_date"].get(date, 0) + 1
                        )
                    elif not self.organize_by_date and len(parts) > 0:
                        # Flat organized: system/file
                        system = parts[0]
                        if system not in stats["by_system"]:
                            stats["by_system"][system] = {"count": 0, "size_bytes": 0}
                        stats["by_system"][system]["count"] += 1
                        stats["by_system"][system]["size_bytes"] += file_size

        return stats
