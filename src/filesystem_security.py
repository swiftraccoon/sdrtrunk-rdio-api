"""Platform-specific checks for private filesystem objects."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import unicodedata
from pathlib import Path, PureWindowsPath

DATABASE_PROCESS_LOCK_NAME = ".rdio-database.lock"
_WINDOWS_PATH_RULES_REQUIRED = os.name == "nt"
_WINDOWS_RESERVED_CHARACTERS = frozenset(
    {chr(value) for value in range(32)} | {'"', "*", ":", "<", ">", "?", "|", "/", "\\"}
)
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)


def _win32_path_component_is_dangerous(component: str) -> bool:
    """Recognize Win32 names whose filesystem meaning is not lexical.

    This intentionally follows the conservative rules used by Python 3.13's
    ``os.path.isreserved`` and additionally rejects DOS 8.3-looking aliases.
    A short alias can be assigned only after a protected long name is created,
    so neither ``samefile`` nor a preflight ``resolve`` can reliably detect it.
    """
    if component in {"", ".", ".."}:
        return False
    if component[-1] in {".", " "}:
        return True
    if _WINDOWS_RESERVED_CHARACTERS.intersection(component):
        return True
    if any(
        component[index] == "~" and "0" <= component[index + 1] <= "9"
        for index in range(len(component) - 1)
    ):
        return True
    device_stem = component.partition(".")[0].rstrip(" ").upper()
    return device_stem in _WINDOWS_RESERVED_DEVICE_NAMES


def _win32_path_is_dangerous(path: str | Path) -> bool:
    """Apply dangerous Win32 component rules without requiring Windows.

    Keeping the lexical fallback platform-independent lets non-Windows CI test
    the complete policy while native Windows uses ``os.path.isreserved`` when
    that Python-version-specific helper is available.
    """
    raw_path = os.fspath(path)
    native_is_reserved = getattr(os.path, "isreserved", None)
    if native_is_reserved is not None:
        try:
            if native_is_reserved(raw_path):
                return True
        except (OSError, ValueError):
            return True

    windows_path = PureWindowsPath(raw_path)
    # Win32 device and extended-length namespaces bypass ordinary drive/path
    # interpretation and filename normalization. Security-sensitive paths use
    # only the regular Win32 namespace so every alias rule above stays valid.
    drive_key = windows_path.drive.casefold()
    if drive_key.startswith(("\\\\.\\", "\\\\?\\")):
        return True
    components = windows_path.parts
    if components and components[0] == windows_path.anchor:
        components = components[1:]
    return any(_win32_path_component_is_dangerous(part) for part in components)


def path_uses_dangerous_windows_namespace(path: str | Path) -> bool:
    """Return whether a path is unsafe under Win32 filename semantics.

    POSIX deliberately permits these names. The guard is therefore active only
    on Windows, while ``_win32_path_is_dangerous`` remains directly testable on
    every platform.
    """
    return _WINDOWS_PATH_RULES_REQUIRED and _win32_path_is_dangerous(path)


def path_is_anchored_windows_relative(path: str | Path) -> bool:
    """Detect Win32 drive-relative and rooted-relative path spellings."""
    windows_path = PureWindowsPath(os.fspath(path))
    return not windows_path.is_absolute() and bool(
        windows_path.drive or windows_path.root
    )


def _absolute_lexical_path(path: str | Path) -> Path:
    """Normalize dot components without resolving symlinks or filename casing."""
    return Path(os.path.abspath(Path(path).expanduser()))


def _filesystem_component_key(component: str) -> str:
    """Return a conservative cross-platform filename comparison key.

    APFS/HFS commonly compare names case-insensitively and with Unicode
    canonical equivalence while ``Path.resolve`` preserves caller spelling.
    Applying NFD+casefold everywhere deliberately overblocks aliases on
    case-sensitive filesystems; state-separation checks should favor safety.
    """
    # Win32 normal path APIs ignore trailing ASCII spaces and periods in each
    # component. Apply that rule conservatively on every platform so a missing
    # sidecar cannot be addressed through an alias that appears distinct during
    # preflight validation.
    return unicodedata.normalize("NFD", component).rstrip(" .").casefold()


def _filesystem_path_key(path: str | Path) -> tuple[str, ...]:
    absolute = _absolute_lexical_path(path)
    return tuple(_filesystem_component_key(component) for component in absolute.parts)


def paths_have_equivalent_filesystem_spelling(
    first: str | Path, second: str | Path
) -> bool:
    """Compare lexical paths under case, Unicode, and Win32 name folding."""
    return _filesystem_path_key(first) == _filesystem_path_key(second)


def _resolved_path(path: str | Path) -> Path | None:
    try:
        return _absolute_lexical_path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _same_directory_identity(first: Path, second: Path) -> bool:
    try:
        first_status = os.stat(first, follow_symlinks=True)
        second_status = os.stat(second, follow_symlinks=True)
    except OSError:
        return False
    return (
        stat.S_ISDIR(first_status.st_mode)
        and stat.S_ISDIR(second_status.st_mode)
        and (first_status.st_dev, first_status.st_ino)
        == (second_status.st_dev, second_status.st_ino)
    )


def _deepest_existing_prefix(
    path: str | Path,
) -> tuple[Path, tuple[str, ...]] | None:
    """Split a path at its deepest existing entry without resolving its alias."""
    current = _absolute_lexical_path(path)
    remainder: list[str] = []
    while True:
        try:
            os.stat(current, follow_symlinks=True)
            return current, tuple(
                _filesystem_component_key(component) for component in remainder
            )
        except (FileNotFoundError, NotADirectoryError):
            parent = current.parent
            if parent == current:
                return None
            remainder.insert(0, current.name)
            current = parent
        except OSError:
            return None


def _same_existing_identity(first: Path, second: Path) -> bool:
    try:
        first_status = os.stat(first, follow_symlinks=True)
        second_status = os.stat(second, follow_symlinks=True)
    except OSError:
        return False
    return (first_status.st_dev, first_status.st_ino) == (
        second_status.st_dev,
        second_status.st_ino,
    )


def path_is_same_or_within(candidate: str | Path, root: str | Path) -> bool:
    """Conservatively detect containment across aliases and filesystem casing."""

    def key_is_within(
        candidate_key: tuple[str, ...], root_key: tuple[str, ...]
    ) -> bool:
        return (
            len(candidate_key) >= len(root_key)
            and candidate_key[: len(root_key)] == root_key
        )

    absolute_candidate = _absolute_lexical_path(candidate)
    absolute_root = _absolute_lexical_path(root)
    if key_is_within(
        _filesystem_path_key(absolute_candidate), _filesystem_path_key(absolute_root)
    ):
        return True

    resolved_candidate = _resolved_path(absolute_candidate)
    resolved_root = _resolved_path(absolute_root)
    if (
        resolved_candidate is not None
        and resolved_root is not None
        and key_is_within(
            _filesystem_path_key(resolved_candidate),
            _filesystem_path_key(resolved_root),
        )
    ):
        return True

    candidate_split = _deepest_existing_prefix(absolute_candidate)
    root_split = _deepest_existing_prefix(absolute_root)
    if candidate_split is not None and root_split is not None:
        candidate_prefix, candidate_remainder = candidate_split
        root_prefix, root_remainder = root_split
        if _same_existing_identity(candidate_prefix, root_prefix) and key_is_within(
            candidate_remainder, root_remainder
        ):
            return True

    # Path spelling is not enough on a case-insensitive volume or through a
    # root-controlled alias. Compare every existing candidate ancestor with the
    # configured root's actual directory identity.
    for ancestor in (absolute_candidate, *absolute_candidate.parents):
        if _same_directory_identity(ancestor, absolute_root):
            return True
    return False


def paths_overlap(first: str | Path, second: str | Path) -> bool:
    """Return whether either path is the same as or nested beneath the other."""
    return path_is_same_or_within(first, second) or path_is_same_or_within(
        second, first
    )


def paths_refer_to_same_entry(candidate: str | Path, protected: str | Path) -> bool:
    """Match existing inode aliases and conservative missing-file aliases."""
    absolute_candidate = _absolute_lexical_path(candidate)
    absolute_protected = _absolute_lexical_path(protected)
    try:
        if os.path.samefile(absolute_candidate, absolute_protected):
            return True
    except OSError:
        pass

    if _filesystem_path_key(absolute_candidate) == _filesystem_path_key(
        absolute_protected
    ):
        return True
    resolved_candidate = _resolved_path(absolute_candidate)
    resolved_protected = _resolved_path(absolute_protected)
    if (
        resolved_candidate is not None
        and resolved_protected is not None
        and _filesystem_path_key(resolved_candidate)
        == _filesystem_path_key(resolved_protected)
    ):
        return True

    candidate_split = _deepest_existing_prefix(absolute_candidate)
    protected_split = _deepest_existing_prefix(absolute_protected)
    if candidate_split is not None and protected_split is not None:
        candidate_prefix, candidate_remainder = candidate_split
        protected_prefix, protected_remainder = protected_split
        if candidate_remainder == protected_remainder and _same_existing_identity(
            candidate_prefix, protected_prefix
        ):
            return True

    # Missing SQLite sidecars still need protection: compare their existing
    # parent directory by inode and their basename by filesystem semantics.
    return _filesystem_component_key(
        absolute_candidate.name
    ) == _filesystem_component_key(
        absolute_protected.name
    ) and _same_directory_identity(
        absolute_candidate.parent, absolute_protected.parent
    )


def paths_conflict(first: str | Path, second: str | Path) -> bool:
    """Detect hierarchy conflicts plus hard-link and filesystem-name aliases."""
    return paths_overlap(first, second) or paths_refer_to_same_entry(first, second)


def sqlite_state_paths(database_path: str | Path) -> tuple[Path, ...]:
    """Return every fixed-name application artifact for a SQLite database."""
    database = _absolute_lexical_path(database_path)
    return (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
        database.parent / DATABASE_PROCESS_LOCK_NAME,
    )


def rotating_file_paths(path: str | Path, backup_count: int) -> tuple[Path, ...]:
    """Return an active rotating file and all of its configured destinations."""
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")
    active = _absolute_lexical_path(path)
    return (active,) + tuple(
        Path(f"{active}.{index}") for index in range(1, backup_count + 1)
    )


def log_process_lock_path(path: str | Path) -> Path:
    """Return a stable per-log lock name shared by filesystem-name aliases."""
    active = _absolute_lexical_path(path)
    normalized_name = _filesystem_component_key(active.name).encode("utf-8")
    digest = hashlib.sha256(normalized_name).hexdigest()[:24]
    return active.parent / f".rdio-log-{digest}.lock"


def rotating_log_state_paths(path: str | Path, backup_count: int) -> tuple[Path, ...]:
    """Return active, rotated, and process-lock artifacts for a log file."""
    return (*rotating_file_paths(path, backup_count), log_process_lock_path(path))


def _serialized_acl_is_deny_only(serialized: bytes) -> bool:
    """Accept only ACL text whose action is unambiguously deny-only."""
    entries = [
        line.lower()
        for line in serialized.splitlines()
        if line and not line.startswith(b"!#acl")
    ]
    return all(b":deny:" in entry and b":allow:" not in entry for entry in entries)


def durable_fsync(descriptor: int) -> None:
    """Flush file data and directory ordering through volatile device caches.

    macOS documents ``F_FULLFSYNC`` as the stronger durability primitive;
    ordinary ``fsync`` can return after sending writes to a device whose cache
    has not reached stable media. Other supported platforms use ``fsync``.
    """
    os.fsync(descriptor)
    if sys.platform == "darwin":
        import fcntl

        # CPython does not expose this Darwin constant on every supported
        # version, but the kernel ABI value is stable. The preceding fsync
        # preserves the portable baseline and this second call forces the
        # device cache through the stronger Darwin durability boundary.
        fcntl.fcntl(descriptor, getattr(fcntl, "F_FULLFSYNC", 51))


def reject_insecure_extended_acl(
    descriptor: int,
    *,
    description: str = "Protected filesystem object",
) -> None:
    """Reject macOS ACL entries that grant access beyond Unix mode bits.

    On Darwin, ``chmod(0600/0700)`` does not clear NFSv4-style extended ACLs;
    an inherited ``allow read`` entry can therefore override apparently private
    mode bits. Query the ACL through the already-pinned descriptor to avoid a
    pathname race. Deny-only ACLs (for example the standard macOS home-directory
    ``everyone deny delete`` entry) do not grant access and remain acceptable.

    Linux POSIX access ACL permissions are reflected in the group-class mode
    mask changed by ``chmod``, while Windows ACL limitations are handled as a
    documented deployment boundary.
    """
    if sys.platform != "darwin":
        return

    libc = ctypes.CDLL(None, use_errno=True)
    acl_get_fd = libc.acl_get_fd
    acl_get_fd.argtypes = [ctypes.c_int]
    acl_get_fd.restype = ctypes.c_void_p
    acl_to_text = libc.acl_to_text
    acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    acl_to_text.restype = ctypes.c_void_p
    acl_free = libc.acl_free
    acl_free.argtypes = [ctypes.c_void_p]
    acl_free.restype = ctypes.c_int

    ctypes.set_errno(0)
    acl = acl_get_fd(descriptor)
    acl_error = ctypes.get_errno()
    if not acl:
        if acl_error == errno.ENOENT:
            return
        raise OSError(
            acl_error or errno.EIO,
            f"Could not verify the extended ACL on {description}",
        )

    text_pointer: int | None = None
    try:
        text_length = ctypes.c_ssize_t()
        ctypes.set_errno(0)
        text_pointer = acl_to_text(acl, ctypes.byref(text_length))
        text_error = ctypes.get_errno()
        if not text_pointer or not 0 <= text_length.value <= 64 * 1024:
            raise OSError(
                text_error or errno.EIO,
                f"Could not verify the extended ACL on {description}",
            )

        serialized = ctypes.string_at(text_pointer, text_length.value)
        if not _serialized_acl_is_deny_only(serialized):
            raise PermissionError(
                f"{description} has an extended ACL that grants additional access"
            )
    finally:
        if text_pointer:
            acl_free(ctypes.c_void_p(text_pointer))
        acl_free(ctypes.c_void_p(acl))
