"""Conservative, process-local admission accounting for uploaded audio.

Before multipart parsing, the accounting in this module reserves the configured
maximum upload size only for Starlette's spool, plus bounded state-write
byte/inode headroom. Custom-temp and destination stages and persistent archive
byte/file quotas are claimed separately after authentication and metadata
validation, so slow unauthenticated bodies cannot monopolize them. This is an
admission-control guard, not a filesystem allocation guarantee: another process
can consume space or inodes after ``statvfs`` returns. Deployments with more
than one application worker therefore need an external or database-backed quota
in addition to this process-local guard.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_STATE_WRITE_RESERVATION_BYTES: Final = 1024 * 1024
_STATE_WRITE_RESERVATION_INODES: Final = 4
_MAINTENANCE_STATE_RESERVATION_INODES: Final = 4
_CAPACITY_PROBE_ATTEMPTS: Final = 16
_RECONCILIATION_ENTRY_BUDGET: Final = 4096
_RECONCILIATION_TIME_BUDGET_SECONDS: Final = 0.05
_RECONCILIATION_DEPTH_LIMIT: Final = 128
_DIRECTORY_IDENTITY_TRACKING_LIMIT: Final = 65_536


class CapacityUnavailable(RuntimeError):
    """Raised when an upload cannot be admitted without exceeding capacity."""


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """A lock-consistent view of capacity accounting for tests and metrics."""

    stored_bytes: int
    stored_files: int
    persistent_reserved_bytes: int
    persistent_reserved_files: int
    filesystem_reserved_bytes: int
    filesystem_reserved_inodes: int
    active_reservations: int
    active_archive_reservations: int
    scan_certain: bool
    reconciling: bool
    reconciliation_pending: bool
    over_quota: bool


@dataclass(slots=True)
class _ArchiveScanCursor:
    """Bounded state retained between streaming reconciliation slices."""

    events: Iterator[tuple[int, int, bool]]
    generation: int
    total_bytes: int = 0
    total_files: int = 0
    certain: bool = True


class UploadCapacityReservation:
    """One worst-case upload reservation owned by :class:`StorageCapacity`."""

    def __init__(
        self,
        manager: StorageCapacity,
        device_bytes: dict[int, int],
        device_inodes: dict[int, int],
        persistent_bytes: int,
        persistent_files: int,
        spool_device: int,
    ) -> None:
        self._manager = manager
        self._device_bytes = device_bytes
        self._device_inodes = device_inodes
        self._persistent_bytes = persistent_bytes
        self._persistent_files = persistent_files
        self._spool_device = spool_device
        self._temp_device: int | None = None
        self._archive_device_bytes: dict[int, int] = {}
        self._archive_device_inodes: dict[int, int] = {}
        self._active = True
        self._spool_completed = False
        self._custom_temp_completed = False
        self._persistent_claimed = False
        self._archive_claim_active = False
        self._stored_committed = False

    @property
    def active(self) -> bool:
        """Return whether this reservation still owns capacity."""
        return self._active

    @property
    def stored_committed(self) -> bool:
        """Return whether persistent quota was converted to an actual charge."""
        return self._stored_committed

    def claim_persistent(self) -> None:
        """Claim archive quota after authentication and metadata validation."""
        self._manager._claim_persistent(self)

    def complete_spool(self) -> None:
        """Mark multipart parsing complete and stop reserving future spool growth."""
        self._manager._complete_spool(self)

    def complete_custom_temp(self) -> None:
        """Mark the application temp copy complete and stop reserving its growth."""
        self._manager._complete_custom_temp(self)

    def commit_stored(self, size_bytes: int) -> None:
        """Convert the persistent worst-case reservation to an actual file size."""
        self._manager._commit_stored(self, size_bytes)

    def commit_stored_path(self, path: str | Path) -> None:
        """Stat a newly stored regular file without following a final symlink."""
        try:
            file_status = os.stat(path, follow_symlinks=False)
        except (OSError, TypeError):
            self._manager._commit_uncertain(self)
            raise CapacityUnavailable(
                "Stored file size could not be verified"
            ) from None
        if not stat.S_ISREG(file_status.st_mode):
            self._manager._commit_uncertain(self)
            raise CapacityUnavailable("Stored path is not a regular file")
        self.commit_stored(file_status.st_size)

    def release(self) -> None:
        """Release filesystem and any unconverted persistent reservations."""
        self._manager._release(self)

    def __enter__(self) -> UploadCapacityReservation:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class StorageCapacity:
    """Thread-safe, conservative capacity accounting for one application process.

    The initial archive scan and later reconciliations stream directory entries,
    never follow symbolic links, and count every regular file under the archive
    root (including orphaned or pending-deletion files).  Any uncertain scan
    closes upload admission until a later complete reconciliation succeeds.
    """

    def __init__(
        self,
        *,
        storage_directory: str | Path,
        temp_directory: str | Path,
        max_file_bytes: int,
        max_storage_bytes: int,
        max_storage_files: int,
        minimum_free_bytes: int,
        minimum_free_inodes: int,
        persistent_archive_enabled: bool,
        maintenance_state_bytes: int = 32 * 1024 * 1024,
        destination_inode_reservation: int = 5,
        spool_directory: str | Path | None = None,
        state_directories: Collection[str | Path] = (),
        scan_on_initialize: bool = True,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if max_storage_bytes < 1:
            raise ValueError("max_storage_bytes must be positive")
        if max_storage_files < 1:
            raise ValueError("max_storage_files must be positive")
        if minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes cannot be negative")
        if minimum_free_inodes < 0:
            raise ValueError("minimum_free_inodes cannot be negative")
        if maintenance_state_bytes < 1:
            raise ValueError("maintenance_state_bytes must be positive")
        if not 1 <= destination_inode_reservation <= 16:
            raise ValueError("destination_inode_reservation must be between 1 and 16")

        self.storage_directory = Path(storage_directory)
        self.temp_directory = Path(temp_directory)
        self.spool_directory = Path(spool_directory or tempfile.gettempdir())
        self.state_directories = tuple(Path(path) for path in state_directories)
        self.max_file_bytes = max_file_bytes
        self.max_storage_bytes = max_storage_bytes
        self.max_storage_files = max_storage_files
        self.minimum_free_bytes = minimum_free_bytes
        self.minimum_free_inodes = minimum_free_inodes
        self.maintenance_state_bytes = maintenance_state_bytes
        self.persistent_archive_enabled = persistent_archive_enabled
        self.destination_inode_reservation = destination_inode_reservation

        self._storage_root_identity = self._directory_identity(
            self.storage_directory, "Storage"
        )
        self._temp_root_identity = self._directory_identity(
            self.temp_directory, "Application temp"
        )
        self._spool_root_identity = self._directory_identity(
            self.spool_directory, "Upload spool"
        )
        self._state_root_identities = tuple(
            self._directory_identity(path, "State") for path in self.state_directories
        )
        self._state_devices = frozenset(
            device for device, _inode in self._state_root_identities
        )

        self._accounting_lock = threading.RLock()
        # Re-entrant because a failed store may invoke the same FileHandler's
        # compensating delete while still inside a mutation guard.
        self._mutation_lock = threading.RLock()
        self._stored_bytes = 0
        self._stored_files = 0
        self._persistent_reserved_bytes = 0
        self._persistent_reserved_files = 0
        self._device_reserved_bytes: dict[int, int] = {}
        self._device_reserved_inodes: dict[int, int] = {}
        self._active_reservations = 0
        self._active_archive_reservations = 0
        self._maintenance_active = False
        self._reconciling = False
        self._scan_certain = False
        self._scan_cursor: _ArchiveScanCursor | None = None
        self._mutation_generation = 0
        # A probe made before a stage is materialized becomes stale when the
        # corresponding future-byte reservation is released. Admissions compare
        # this epoch before using statvfs results so that transition cannot make
        # the same bytes momentarily disappear from both sides of the check.
        self._capacity_epoch = 0
        self._scan_stop = threading.Event()
        self._closed = False

        if self.persistent_archive_enabled and scan_on_initialize:
            # Initialization itself is bounded. Large archives retain a cursor
            # and remain fail-closed until subsequent reconciliation slices
            # complete (the application schedules those in a worker thread).
            self.reconcile()
        elif not self.persistent_archive_enabled:
            # Log-only/discard configurations do not own a persistent archive.
            self._scan_certain = True

    @property
    def snapshot(self) -> CapacitySnapshot:
        """Return current accounting without exposing mutable internals."""
        with self._accounting_lock:
            return CapacitySnapshot(
                stored_bytes=self._stored_bytes,
                stored_files=self._stored_files,
                persistent_reserved_bytes=self._persistent_reserved_bytes,
                persistent_reserved_files=self._persistent_reserved_files,
                filesystem_reserved_bytes=sum(self._device_reserved_bytes.values()),
                filesystem_reserved_inodes=sum(self._device_reserved_inodes.values()),
                active_reservations=self._active_reservations,
                active_archive_reservations=self._active_archive_reservations,
                scan_certain=self._scan_certain,
                reconciling=self._reconciling,
                reconciliation_pending=self._scan_cursor is not None,
                over_quota=(
                    self._stored_bytes > self.max_storage_bytes
                    or self._stored_files > self.max_storage_files
                ),
            )

    def ready_for_upload(self) -> bool:
        """Return whether a new worst-case upload could currently be admitted."""
        try:
            required_bytes, required_inodes, device_paths, _spool_device = (
                self._preauth_requirements()
            )
            if self.persistent_archive_enabled:
                archive_bytes, archive_inodes, archive_paths, _temp_device = (
                    self._archive_stage_requirements()
                )
                for device, required in archive_bytes.items():
                    required_bytes[device] = required_bytes.get(device, 0) + required
                for device, required in archive_inodes.items():
                    required_inodes[device] = required_inodes.get(device, 0) + required
                for device, path in archive_paths.items():
                    device_paths.setdefault(device, path)
            probe_epoch = self._capacity_epoch_for_probe()
            available_bytes, available_inodes = self._available_capacity(device_paths)
        except CapacityUnavailable:
            return False

        if not self._accounting_lock.acquire(blocking=False):
            return False
        try:
            if self._closed:
                return False
            if probe_epoch != self._capacity_epoch:
                return False
            if self.persistent_archive_enabled and (
                not self._scan_certain
                or self._reconciling
                or self._scan_cursor is not None
                or self._stored_bytes
                + self._persistent_reserved_bytes
                + self.max_file_bytes
                > self.max_storage_bytes
                or self._stored_files + self._persistent_reserved_files + 1
                > self.max_storage_files
            ):
                return False
            for device, required in required_bytes.items():
                if available_bytes[device] - self._device_reserved_bytes.get(
                    device, 0
                ) - required < self._minimum_free_bytes_locked(device):
                    return False
            for device, required in required_inodes.items():
                if available_inodes[device] - self._device_reserved_inodes.get(
                    device, 0
                ) - required < self._minimum_free_inodes_locked(device):
                    return False
            return True
        finally:
            self._accounting_lock.release()

    @contextmanager
    def mutation_guard(self) -> Iterator[None]:
        """Serialize archive mutations with reconciliation scans."""
        with self._mutation_lock:
            yield

    @contextmanager
    def maintenance_state_guard(self) -> Iterator[None]:
        """Reserve one bounded state transaction and checkpoint atomically.

        The reserve is present in process-local filesystem accounting for the
        whole operation. Uploads that probe concurrently therefore cannot spend
        the bytes/inodes that SQLite may materialize after their statvfs probe.
        """
        state_paths, _state_write_inodes = self._state_device_requirements()
        device_bytes = dict.fromkeys(state_paths, self.maintenance_state_bytes)
        device_inodes = dict.fromkeys(
            state_paths, _MAINTENANCE_STATE_RESERVATION_INODES
        )

        acquired = False
        for _attempt in range(_CAPACITY_PROBE_ATTEMPTS):
            probe_epoch = self._capacity_epoch_for_probe()
            available_bytes, available_inodes = self._available_capacity(state_paths)
            if not self._accounting_lock.acquire(blocking=False):
                raise CapacityUnavailable("Storage accounting is busy")
            try:
                if self._closed:
                    raise CapacityUnavailable("Storage accounting is closed")
                if probe_epoch != self._capacity_epoch:
                    continue
                if self._maintenance_active:
                    raise CapacityUnavailable("Storage maintenance is already active")
                for device, required in device_bytes.items():
                    if (
                        available_bytes[device]
                        - self._device_reserved_bytes.get(device, 0)
                        - required
                        < self.minimum_free_bytes
                    ):
                        raise CapacityUnavailable(
                            "Maintenance filesystem free-space reserve reached"
                        )
                for device, required in device_inodes.items():
                    if (
                        available_inodes[device]
                        - self._device_reserved_inodes.get(device, 0)
                        - required
                        < self.minimum_free_inodes
                    ):
                        raise CapacityUnavailable(
                            "Maintenance filesystem free-inode reserve reached"
                        )
                for device, required in device_bytes.items():
                    self._device_reserved_bytes[device] = (
                        self._device_reserved_bytes.get(device, 0) + required
                    )
                for device, required in device_inodes.items():
                    self._device_reserved_inodes[device] = (
                        self._device_reserved_inodes.get(device, 0) + required
                    )
                self._maintenance_active = True
                self._capacity_epoch += 1
                acquired = True
                break
            finally:
                self._accounting_lock.release()
        if not acquired:
            raise CapacityUnavailable("Storage capacity changed during verification")

        try:
            yield
        finally:
            with self._accounting_lock:
                for device, reserved in device_bytes.items():
                    remaining = self._device_reserved_bytes.get(device, 0) - reserved
                    if remaining < 0:
                        raise RuntimeError("Maintenance capacity accounting underflow")
                    if remaining:
                        self._device_reserved_bytes[device] = remaining
                    else:
                        self._device_reserved_bytes.pop(device, None)
                for device, reserved in device_inodes.items():
                    remaining = self._device_reserved_inodes.get(device, 0) - reserved
                    if remaining < 0:
                        raise RuntimeError("Maintenance inode accounting underflow")
                    if remaining:
                        self._device_reserved_inodes[device] = remaining
                    else:
                        self._device_reserved_inodes.pop(device, None)
                if not self._maintenance_active:
                    raise RuntimeError("Maintenance capacity accounting underflow")
                self._maintenance_active = False
                self._capacity_epoch += 1

    def _minimum_free_bytes_locked(self, device: int) -> int:
        """Return the upload floor, including idle maintenance headroom."""
        maintenance_floor = (
            self.maintenance_state_bytes
            if device in self._state_devices and not self._maintenance_active
            else 0
        )
        return self.minimum_free_bytes + maintenance_floor

    def _minimum_free_inodes_locked(self, device: int) -> int:
        """Return the inode floor, including idle maintenance headroom."""
        maintenance_floor = (
            _MAINTENANCE_STATE_RESERVATION_INODES
            if device in self._state_devices and not self._maintenance_active
            else 0
        )
        return self.minimum_free_inodes + maintenance_floor

    @staticmethod
    def _directory_identity(path: Path, label: str) -> tuple[int, int]:
        try:
            path_status = os.stat(path, follow_symlinks=False)
        except (OSError, TypeError):
            raise CapacityUnavailable(f"{label} filesystem is unavailable") from None
        if not stat.S_ISDIR(path_status.st_mode):
            raise CapacityUnavailable(f"{label} filesystem path is not a directory")
        return path_status.st_dev, path_status.st_ino

    @staticmethod
    def _checked_directory_status(
        path: Path,
        expected_identity: tuple[int, int],
        label: str,
    ) -> os.stat_result:
        try:
            path_status = os.stat(path, follow_symlinks=False)
        except (OSError, TypeError):
            raise CapacityUnavailable(f"{label} filesystem is unavailable") from None
        if not stat.S_ISDIR(path_status.st_mode):
            raise CapacityUnavailable(f"{label} filesystem path is not a directory")
        if (path_status.st_dev, path_status.st_ino) != expected_identity:
            raise CapacityUnavailable(f"{label} filesystem root changed")
        return path_status

    @staticmethod
    def _close_cursor(cursor: _ArchiveScanCursor) -> None:
        close = getattr(cursor.events, "close", None)
        if close is not None:
            close()

    def close(self) -> None:
        """Stop an in-progress scan cooperatively and close new admissions."""
        self._scan_stop.set()
        with self._accounting_lock:
            self._closed = True
            if self.persistent_archive_enabled:
                self._scan_certain = False
        if self._mutation_lock.acquire(blocking=False):
            try:
                with self._accounting_lock:
                    cursor = self._scan_cursor
                    self._scan_cursor = None
                if cursor is not None:
                    self._close_cursor(cursor)
            finally:
                self._mutation_lock.release()

    def reserve_upload(self) -> UploadCapacityReservation:
        """Reserve only multipart spool and bounded state-write headroom."""
        if self._scan_stop.is_set():
            raise CapacityUnavailable("Storage accounting is closed")
        device_bytes, device_inodes, device_paths, spool_device = (
            self._preauth_requirements()
        )
        for _attempt in range(_CAPACITY_PROBE_ATTEMPTS):
            probe_epoch = self._capacity_epoch_for_probe()
            available_bytes, available_inodes = self._available_capacity(device_paths)

            # Reconciliation never holds up an async admission path. Filesystem
            # probes happen above this short lock; process-local reservations are
            # still checked and recorded atomically below.
            if not self._accounting_lock.acquire(blocking=False):
                raise CapacityUnavailable("Storage accounting is busy")
            try:
                if self._closed:
                    raise CapacityUnavailable("Storage accounting is closed")
                if probe_epoch != self._capacity_epoch:
                    continue
                for device, required in device_bytes.items():
                    existing = self._device_reserved_bytes.get(device, 0)
                    available = available_bytes[device]
                    if (
                        available - existing - required
                        < self._minimum_free_bytes_locked(device)
                    ):
                        raise CapacityUnavailable(
                            "Filesystem free-space reserve reached"
                        )
                for device, required in device_inodes.items():
                    existing = self._device_reserved_inodes.get(device, 0)
                    available = available_inodes[device]
                    if (
                        available - existing - required
                        < self._minimum_free_inodes_locked(device)
                    ):
                        raise CapacityUnavailable(
                            "Filesystem free-inode reserve reached"
                        )

                for device, required in device_bytes.items():
                    self._device_reserved_bytes[device] = (
                        self._device_reserved_bytes.get(device, 0) + required
                    )
                for device, required in device_inodes.items():
                    self._device_reserved_inodes[device] = (
                        self._device_reserved_inodes.get(device, 0) + required
                    )
                self._active_reservations += 1
                self._capacity_epoch += 1
                return UploadCapacityReservation(
                    self,
                    dict(device_bytes),
                    dict(device_inodes),
                    0,
                    0,
                    spool_device,
                )
            finally:
                self._accounting_lock.release()
        raise CapacityUnavailable("Storage capacity changed during verification")

    def record_deleted(self, freed_bytes: int, freed_files: int = 1) -> None:
        """Subtract only after stored regular files were successfully unlinked."""
        if freed_bytes < 0:
            raise ValueError("freed_bytes cannot be negative")
        if freed_files < 0:
            raise ValueError("freed_files cannot be negative")
        with self._accounting_lock:
            # If prior external drift made the counter smaller than a deleted
            # file, do not wrap negative; reconciliation will restore precision.
            self._stored_bytes = max(0, self._stored_bytes - freed_bytes)
            self._stored_files = max(0, self._stored_files - freed_files)
            self._mutation_generation += 1
            self._capacity_epoch += 1

    def mark_uncertain(self) -> None:
        """Close admission after a mutation whose final state is unknown."""
        with self._accounting_lock:
            self._scan_certain = False
            self._mutation_generation += 1
            self._capacity_epoch += 1

    def record_structure_mutation(self) -> None:
        """Invalidate an incremental cursor after an archive directory change."""
        with self._accounting_lock:
            self._mutation_generation += 1
            self._capacity_epoch += 1

    def reconcile(self) -> bool:
        """Advance one bounded scan slice when archive mutation is quiescent.

        ``False`` means the scan is incomplete/uncertain or an archive upload is
        active. FileHandler mutations use :meth:`mutation_guard` and increment a
        generation, so work retained between slices is discarded after any app
        mutation rather than being double-counted.
        """
        if not self.persistent_archive_enabled:
            return True
        if self._scan_stop.is_set():
            return False
        if not self._mutation_lock.acquire(blocking=False):
            return False
        scan_started = False
        try:
            with self._accounting_lock:
                if self._active_archive_reservations or self._reconciling:
                    return False
                if (
                    self._scan_cursor is not None
                    and self._scan_cursor.generation != self._mutation_generation
                ):
                    stale_cursor = self._scan_cursor
                    self._scan_cursor = None
                    self._scan_certain = False
                else:
                    stale_cursor = None
                if self._scan_cursor is None:
                    self._scan_cursor = _ArchiveScanCursor(
                        events=self._archive_scan_events(),
                        generation=self._mutation_generation,
                    )
                    self._scan_certain = False
                cursor = self._scan_cursor
                self._reconciling = True
                scan_started = True
            if stale_cursor is not None:
                self._close_cursor(stale_cursor)
            try:
                complete = self._advance_scan_cursor(cursor)
            except BaseException:
                # An interrupted or unexpected scanner failure cannot leave a
                # previously healthy snapshot authorizing new archive writes.
                with self._accounting_lock:
                    self._scan_certain = False
                    if self._scan_cursor is cursor:
                        self._scan_cursor = None
                self._close_cursor(cursor)
                raise
            with self._accounting_lock:
                if cursor.generation != self._mutation_generation:
                    # A direct caller changed accounting without taking the
                    # archive guard. Treat this slice as stale and restart.
                    complete = False
                    self._scan_certain = False
                    if self._scan_cursor is cursor:
                        self._scan_cursor = None
                    stale_after_scan = True
                elif complete:
                    self._stored_bytes = cursor.total_bytes
                    self._stored_files = cursor.total_files
                    self._scan_certain = cursor.certain
                    self._capacity_epoch += 1
                    if self._scan_cursor is cursor:
                        self._scan_cursor = None
                    stale_after_scan = False
                else:
                    self._scan_certain = False
                    stale_after_scan = False
            if complete or stale_after_scan:
                self._close_cursor(cursor)
            return complete and cursor.certain and not stale_after_scan
        finally:
            if scan_started:
                with self._accounting_lock:
                    self._reconciling = False
            self._mutation_lock.release()

    def _preauth_requirements(
        self,
    ) -> tuple[dict[int, int], dict[int, int], dict[int, Path], int]:
        """Group pre-auth spool and state-write margins by filesystem device."""
        device_bytes: dict[int, int] = {}
        device_inodes: dict[int, int] = {}
        device_paths: dict[int, Path] = {}
        path_status = self._checked_directory_status(
            self.spool_directory,
            self._spool_root_identity,
            "Upload spool",
        )
        spool_device = path_status.st_dev
        device_bytes[spool_device] = self.max_file_bytes
        device_inodes[spool_device] = 1
        device_paths[spool_device] = self.spool_directory
        if sum(device_bytes.values()) != self.max_file_bytes:
            raise RuntimeError("Incomplete upload filesystem reservation")

        # Every form-valid request writes SQLite state and normally an audit
        # record, including log-only/discard requests. Reserve a bounded WAL /
        # journal growth margin once per distinct state filesystem. This is
        # intentionally independent of audio size.
        state_device_paths, state_device_inodes = self._state_device_requirements()
        for device, path in state_device_paths.items():
            device_bytes[device] = (
                device_bytes.get(device, 0) + _STATE_WRITE_RESERVATION_BYTES
            )
            device_paths.setdefault(device, path)
        for device, required in state_device_inodes.items():
            device_inodes[device] = device_inodes.get(device, 0) + required
        return device_bytes, device_inodes, device_paths, spool_device

    def _state_device_requirements(self) -> tuple[dict[int, Path], dict[int, int]]:
        """Resolve state devices and worst-case state-file inode creation."""
        device_paths: dict[int, Path] = {}
        device_inodes: dict[int, int] = {}
        seen_paths: set[Path] = set()
        for path, expected_identity in zip(
            self.state_directories, self._state_root_identities, strict=True
        ):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            path_status = self._checked_directory_status(
                path, expected_identity, "State"
            )
            device_paths.setdefault(path_status.st_dev, path)
            device_inodes[path_status.st_dev] = (
                device_inodes.get(path_status.st_dev, 0)
                + _STATE_WRITE_RESERVATION_INODES
            )
        return device_paths, device_inodes

    def _state_device_paths(self) -> dict[int, Path]:
        """Resolve one probe path for each SQLite/log state filesystem."""
        return self._state_device_requirements()[0]

    def _archive_stage_requirements(
        self,
    ) -> tuple[dict[int, int], dict[int, int], dict[int, Path], int]:
        """Group authenticated custom-temp and destination stages by device."""
        device_bytes: dict[int, int] = {}
        device_inodes: dict[int, int] = {}
        device_paths: dict[int, Path] = {}
        temp_device: int | None = None
        stage_roots = (
            (
                self.temp_directory,
                self._temp_root_identity,
                "Application temp",
                1,
            ),
            (
                self.storage_directory,
                self._storage_root_identity,
                "Storage",
                self.destination_inode_reservation,
            ),
        )
        for index, (path, expected_identity, label, inode_count) in enumerate(
            stage_roots
        ):
            path_status = self._checked_directory_status(path, expected_identity, label)
            device_bytes[path_status.st_dev] = (
                device_bytes.get(path_status.st_dev, 0) + self.max_file_bytes
            )
            device_paths.setdefault(path_status.st_dev, path)
            device_inodes[path_status.st_dev] = (
                device_inodes.get(path_status.st_dev, 0) + inode_count
            )
            if index == 0:
                temp_device = path_status.st_dev
        if sum(device_bytes.values()) != self.max_file_bytes * 2:
            raise RuntimeError("Incomplete archive-stage reservation")
        if temp_device is None:
            raise RuntimeError("Application temp filesystem was not resolved")
        return device_bytes, device_inodes, device_paths, temp_device

    @staticmethod
    def _available_capacity(
        device_paths: dict[int, Path],
    ) -> tuple[dict[int, int], dict[int, int]]:
        available_bytes: dict[int, int] = {}
        available_inodes: dict[int, int] = {}
        for device, path in device_paths.items():
            if not hasattr(os, "statvfs"):  # pragma: no cover - Windows
                try:
                    available_bytes[device] = shutil.disk_usage(path).free
                except OSError:
                    raise CapacityUnavailable(
                        "Filesystem capacity could not be verified"
                    ) from None
                # NTFS does not expose a fixed inode pool; the persistent file
                # count limit remains the metadata-growth bound on Windows.
                available_inodes[device] = 2**63 - 1
                continue
            try:
                filesystem = os.statvfs(path)
                fragment_size = filesystem.f_frsize or filesystem.f_bsize
                available_bytes[device] = filesystem.f_bavail * fragment_size
                inode_count = filesystem.f_favail
                if (
                    inode_count == 0
                    and getattr(filesystem, "f_files", -1) == 0
                    and getattr(filesystem, "f_ffree", -1) == 0
                ):
                    # Some POSIX filesystems allocate metadata dynamically and
                    # report an all-zero inode tuple to mean "not applicable."
                    # The configured persistent file-count cap still bounds them.
                    inode_count = 2**63 - 1
                if inode_count < 0:
                    raise OSError("negative available inode count")
                available_inodes[device] = inode_count
            except (AttributeError, OSError):
                raise CapacityUnavailable(
                    "Filesystem capacity could not be verified"
                ) from None
        return available_bytes, available_inodes

    def _capacity_epoch_for_probe(self) -> int:
        """Snapshot the accounting epoch without blocking an async caller."""
        if not self._accounting_lock.acquire(blocking=False):
            raise CapacityUnavailable("Storage accounting is busy")
        try:
            if self._closed:
                raise CapacityUnavailable("Storage accounting is closed")
            return self._capacity_epoch
        finally:
            self._accounting_lock.release()

    def _commit_stored(
        self, reservation: UploadCapacityReservation, size_bytes: int
    ) -> None:
        if size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        with self._accounting_lock:
            self._validate_reservation(reservation)
            if reservation._stored_committed:
                raise RuntimeError("Stored capacity was already committed")
            if not reservation._persistent_claimed:
                raise RuntimeError("Persistent capacity was not claimed")
            if not self.persistent_archive_enabled:
                if size_bytes:
                    raise RuntimeError("Persistent storage is not enabled")
                reservation._stored_committed = True
                return

            self._persistent_reserved_bytes -= reservation._persistent_bytes
            self._persistent_reserved_files -= reservation._persistent_files
            reservation._persistent_bytes = 0
            reservation._persistent_files = 0
            reservation._stored_committed = True
            self._stored_bytes += size_bytes
            self._stored_files += 1
            self._mutation_generation += 1
            self._capacity_epoch += 1
            self._release_archive_stages_locked(reservation)
            self._finish_archive_claim_locked(reservation)
            if size_bytes > self.max_file_bytes:
                # Account the real file but fail future admission closed: an
                # invariant outside this class was violated.
                self._scan_certain = False
                raise CapacityUnavailable("Stored file exceeded its reservation")

    def _complete_spool(self, reservation: UploadCapacityReservation) -> None:
        with self._accounting_lock:
            self._validate_reservation(reservation)
            if reservation._spool_completed:
                raise RuntimeError("Multipart spool was already completed")
            remaining = (
                self._device_reserved_bytes.get(reservation._spool_device, 0)
                - self.max_file_bytes
            )
            reservation_remaining = (
                reservation._device_bytes.get(reservation._spool_device, 0)
                - self.max_file_bytes
            )
            if remaining < 0 or reservation_remaining < 0:
                raise RuntimeError("Multipart spool accounting underflow")
            if remaining:
                self._device_reserved_bytes[reservation._spool_device] = remaining
            else:
                self._device_reserved_bytes.pop(reservation._spool_device, None)
            if reservation_remaining:
                reservation._device_bytes[reservation._spool_device] = (
                    reservation_remaining
                )
            else:
                reservation._device_bytes.pop(reservation._spool_device, None)
            self._subtract_device_inodes_locked(
                reservation, reservation._spool_device, 1
            )
            reservation._spool_completed = True
            self._capacity_epoch += 1

    def _complete_custom_temp(self, reservation: UploadCapacityReservation) -> None:
        with self._accounting_lock:
            self._validate_reservation(reservation)
            if not reservation._persistent_claimed or reservation._temp_device is None:
                raise RuntimeError("Archive capacity was not claimed")
            if reservation._custom_temp_completed:
                raise RuntimeError("Application temp stage was already completed")
            self._subtract_device_bytes_locked(
                reservation, reservation._temp_device, self.max_file_bytes
            )
            archive_remaining = (
                reservation._archive_device_bytes[reservation._temp_device]
                - self.max_file_bytes
            )
            if archive_remaining:
                reservation._archive_device_bytes[reservation._temp_device] = (
                    archive_remaining
                )
            else:
                reservation._archive_device_bytes.pop(reservation._temp_device)
            self._subtract_device_inodes_locked(
                reservation, reservation._temp_device, 1
            )
            inode_remaining = (
                reservation._archive_device_inodes[reservation._temp_device] - 1
            )
            if inode_remaining:
                reservation._archive_device_inodes[reservation._temp_device] = (
                    inode_remaining
                )
            else:
                reservation._archive_device_inodes.pop(reservation._temp_device)
            reservation._custom_temp_completed = True
            self._capacity_epoch += 1

    def _claim_persistent(self, reservation: UploadCapacityReservation) -> None:
        archive_device_bytes: dict[int, int] = {}
        archive_device_inodes: dict[int, int] = {}
        device_paths, _state_inodes = self._state_device_requirements()
        temp_device: int | None = None
        if self.persistent_archive_enabled:
            (
                archive_device_bytes,
                archive_device_inodes,
                archive_device_paths,
                temp_device,
            ) = self._archive_stage_requirements()
            for device, path in archive_device_paths.items():
                device_paths.setdefault(device, path)
        # Authentication and multipart parsing can take substantial time.
        # Re-probe SQLite/log filesystems immediately before the form-valid
        # request can write state, even when no persistent audio is stored.
        for _attempt in range(_CAPACITY_PROBE_ATTEMPTS):
            probe_epoch = self._capacity_epoch_for_probe()
            available_bytes, available_inodes = self._available_capacity(device_paths)
            if not self._accounting_lock.acquire(blocking=False):
                raise CapacityUnavailable("Storage accounting is busy")
            try:
                self._validate_reservation(reservation)
                if self._closed:
                    raise CapacityUnavailable("Storage accounting is closed")
                if probe_epoch != self._capacity_epoch:
                    continue
                if reservation._persistent_claimed:
                    raise RuntimeError("Persistent capacity was already claimed")
                if not reservation._spool_completed:
                    raise RuntimeError("Multipart spool is not complete")
                if self._reconciling or not self._scan_certain:
                    raise CapacityUnavailable("Storage accounting is unavailable")
                for device, available in available_bytes.items():
                    required = archive_device_bytes.get(device, 0)
                    existing = self._device_reserved_bytes.get(device, 0)
                    if (
                        available - existing - required
                        < self._minimum_free_bytes_locked(device)
                    ):
                        raise CapacityUnavailable(
                            "Filesystem free-space reserve reached"
                        )
                for device, available in available_inodes.items():
                    required = archive_device_inodes.get(device, 0)
                    existing = self._device_reserved_inodes.get(device, 0)
                    if (
                        available - existing - required
                        < self._minimum_free_inodes_locked(device)
                    ):
                        raise CapacityUnavailable(
                            "Filesystem free-inode reserve reached"
                        )
                if not self.persistent_archive_enabled:
                    reservation._persistent_claimed = True
                    return
                if (
                    self._stored_bytes
                    + self._persistent_reserved_bytes
                    + self.max_file_bytes
                    > self.max_storage_bytes
                    or self._stored_files + self._persistent_reserved_files + 1
                    > self.max_storage_files
                ):
                    raise CapacityUnavailable("Persistent storage quota is exhausted")
                for device, required in archive_device_bytes.items():
                    self._device_reserved_bytes[device] = (
                        self._device_reserved_bytes.get(device, 0) + required
                    )
                    reservation._device_bytes[device] = (
                        reservation._device_bytes.get(device, 0) + required
                    )
                for device, required in archive_device_inodes.items():
                    self._device_reserved_inodes[device] = (
                        self._device_reserved_inodes.get(device, 0) + required
                    )
                    reservation._device_inodes[device] = (
                        reservation._device_inodes.get(device, 0) + required
                    )
                reservation._persistent_claimed = True
                reservation._archive_claim_active = True
                reservation._temp_device = temp_device
                reservation._archive_device_bytes = dict(archive_device_bytes)
                reservation._archive_device_inodes = dict(archive_device_inodes)
                reservation._persistent_bytes = self.max_file_bytes
                reservation._persistent_files = 1
                self._persistent_reserved_bytes += self.max_file_bytes
                self._persistent_reserved_files += 1
                self._active_archive_reservations += 1
                self._capacity_epoch += 1
                return
            finally:
                self._accounting_lock.release()
        raise CapacityUnavailable("Storage capacity changed during verification")

    def _commit_uncertain(self, reservation: UploadCapacityReservation) -> None:
        """Conservatively charge a maximum-sized file and close admission."""
        with self._accounting_lock:
            self._validate_reservation(reservation)
            if reservation._stored_committed:
                return
            if not reservation._persistent_claimed:
                raise RuntimeError("Persistent capacity was not claimed")
            self._persistent_reserved_bytes -= reservation._persistent_bytes
            self._persistent_reserved_files -= reservation._persistent_files
            self._stored_bytes += reservation._persistent_bytes
            self._stored_files += reservation._persistent_files
            self._mutation_generation += 1
            self._capacity_epoch += 1
            reservation._persistent_bytes = 0
            reservation._persistent_files = 0
            reservation._stored_committed = True
            self._release_archive_stages_locked(reservation)
            self._finish_archive_claim_locked(reservation)
            self._scan_certain = False

    def _release_archive_stages_locked(
        self, reservation: UploadCapacityReservation
    ) -> None:
        for device, reserved in tuple(reservation._archive_device_bytes.items()):
            self._subtract_device_bytes_locked(reservation, device, reserved)
        reservation._archive_device_bytes.clear()
        for device, reserved in tuple(reservation._archive_device_inodes.items()):
            self._subtract_device_inodes_locked(reservation, device, reserved)
        reservation._archive_device_inodes.clear()

    def _subtract_device_bytes_locked(
        self,
        reservation: UploadCapacityReservation,
        device: int,
        reserved: int,
    ) -> None:
        remaining = self._device_reserved_bytes.get(device, 0) - reserved
        reservation_remaining = reservation._device_bytes.get(device, 0) - reserved
        if remaining < 0 or reservation_remaining < 0:
            raise RuntimeError("Filesystem capacity accounting underflow")
        if remaining:
            self._device_reserved_bytes[device] = remaining
        else:
            self._device_reserved_bytes.pop(device, None)
        if reservation_remaining:
            reservation._device_bytes[device] = reservation_remaining
        else:
            reservation._device_bytes.pop(device, None)

    def _finish_archive_claim_locked(
        self, reservation: UploadCapacityReservation
    ) -> None:
        if not reservation._archive_claim_active:
            return
        self._active_archive_reservations -= 1
        if self._active_archive_reservations < 0:
            raise RuntimeError("Archive capacity accounting underflow")
        reservation._archive_claim_active = False

    def _subtract_device_inodes_locked(
        self,
        reservation: UploadCapacityReservation,
        device: int,
        reserved: int,
    ) -> None:
        remaining = self._device_reserved_inodes.get(device, 0) - reserved
        reservation_remaining = reservation._device_inodes.get(device, 0) - reserved
        if remaining < 0 or reservation_remaining < 0:
            raise RuntimeError("Filesystem inode accounting underflow")
        if remaining:
            self._device_reserved_inodes[device] = remaining
        else:
            self._device_reserved_inodes.pop(device, None)
        if reservation_remaining:
            reservation._device_inodes[device] = reservation_remaining
        else:
            reservation._device_inodes.pop(device, None)

    def _release(self, reservation: UploadCapacityReservation) -> None:
        with self._accounting_lock:
            if not reservation._active:
                return
            self._validate_reservation(reservation)
            for device, reserved in reservation._device_bytes.items():
                remaining = self._device_reserved_bytes.get(device, 0) - reserved
                if remaining < 0:
                    raise RuntimeError("Filesystem capacity accounting underflow")
                if remaining:
                    self._device_reserved_bytes[device] = remaining
                else:
                    self._device_reserved_bytes.pop(device, None)
            for device, reserved in reservation._device_inodes.items():
                remaining = self._device_reserved_inodes.get(device, 0) - reserved
                if remaining < 0:
                    raise RuntimeError("Filesystem inode accounting underflow")
                if remaining:
                    self._device_reserved_inodes[device] = remaining
                else:
                    self._device_reserved_inodes.pop(device, None)
            self._persistent_reserved_bytes -= reservation._persistent_bytes
            self._persistent_reserved_files -= reservation._persistent_files
            if self._persistent_reserved_bytes < 0:
                raise RuntimeError("Persistent capacity accounting underflow")
            if self._persistent_reserved_files < 0:
                raise RuntimeError("Persistent file accounting underflow")
            self._active_reservations -= 1
            if self._active_reservations < 0:
                raise RuntimeError("Upload capacity accounting underflow")
            self._finish_archive_claim_locked(reservation)
            reservation._persistent_bytes = 0
            reservation._persistent_files = 0
            reservation._active = False
            self._capacity_epoch += 1

    def _validate_reservation(self, reservation: UploadCapacityReservation) -> None:
        if reservation._manager is not self:
            raise RuntimeError("Capacity reservation belongs to another manager")
        if not reservation._active:
            raise RuntimeError("Capacity reservation is no longer active")

    def _advance_scan_cursor(self, cursor: _ArchiveScanCursor) -> bool:
        """Consume a bounded amount of scanner work; return whether it finished."""
        deadline = time.monotonic() + _RECONCILIATION_TIME_BUDGET_SECONDS
        examined = 0
        while examined < _RECONCILIATION_ENTRY_BUDGET and time.monotonic() < deadline:
            if self._scan_stop.is_set():
                cursor.certain = False
                return True
            try:
                size_bytes, file_count, event_certain = next(cursor.events)
            except StopIteration:
                return True
            cursor.total_bytes += size_bytes
            cursor.total_files += file_count
            cursor.certain = cursor.certain and event_certain
            examined += 1
        return False

    def _archive_scan_events(self) -> Iterator[tuple[int, int, bool]]:
        """Yield bounded-memory accounting work events for the archive tree."""
        if os.name == "posix":
            yield from self._archive_scan_events_by_descriptor()
        else:  # pragma: no cover - exercised by Windows CI
            yield from self._archive_scan_events_by_path()

    def _archive_scan_events_by_descriptor(
        self,
    ) -> Iterator[tuple[int, int, bool]]:
        """Iteratively walk POSIX directories through no-follow descriptors."""
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        active_directories: set[tuple[int, int]] = set()
        tracked_directories: set[tuple[int, int]] = set()
        frames: list[tuple[int, Any, tuple[int, int]]] = []
        root_descriptor = -1
        pending_descriptor = -1

        try:
            root_before = os.stat(self.storage_directory, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_before.st_mode)
                or (root_before.st_dev, root_before.st_ino)
                != self._storage_root_identity
            ):
                yield 0, 0, False
                return
            root_descriptor = os.open(self.storage_directory, directory_flags)
            opened_root = os.fstat(root_descriptor)
            if (opened_root.st_dev, opened_root.st_ino) != self._storage_root_identity:
                yield 0, 0, False
                return
            root_entries = os.scandir(root_descriptor)
            frames.append((root_descriptor, root_entries, self._storage_root_identity))
            root_descriptor = -1
            active_directories.add(self._storage_root_identity)
            tracked_directories.add(self._storage_root_identity)

            while frames:
                if self._scan_stop.is_set():
                    yield 0, 0, False
                    return
                directory_descriptor, entries, directory_identity = frames[-1]
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    frames.pop()
                    active_directories.remove(directory_identity)
                    os.close(directory_descriptor)
                    # Bound unwinding work as well as forward traversal work.
                    yield 0, 0, True
                    continue
                except OSError:
                    yield 0, 0, False
                    continue

                try:
                    entry_status = entry.stat(follow_symlinks=False)
                except OSError:
                    yield 0, 0, False
                    continue
                if entry_status.st_dev != opened_root.st_dev:
                    yield 0, 0, False
                    continue
                if stat.S_ISREG(entry_status.st_mode):
                    yield entry_status.st_size, 1, True
                    continue
                if not stat.S_ISDIR(entry_status.st_mode):
                    yield 0, 0, False
                    continue
                if len(frames) >= _RECONCILIATION_DEPTH_LIMIT:
                    yield 0, 0, False
                    continue

                try:
                    pending_descriptor = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                    child_status = os.fstat(pending_descriptor)
                    child_identity = (child_status.st_dev, child_status.st_ino)
                    if (
                        not stat.S_ISDIR(child_status.st_mode)
                        or child_status.st_dev != opened_root.st_dev
                        or child_identity != (entry_status.st_dev, entry_status.st_ino)
                        or child_identity in active_directories
                        or child_identity in tracked_directories
                    ):
                        yield 0, 0, False
                        continue
                    # Yield before opening the child iterator. If an app mutation
                    # happens between slices, its generation invalidates this
                    # cursor and generator.close() releases the pending fd.
                    yield 0, 0, True
                    child_entries = os.scandir(pending_descriptor)
                    frames.append((pending_descriptor, child_entries, child_identity))
                    pending_descriptor = -1
                    active_directories.add(child_identity)
                    if len(tracked_directories) < _DIRECTORY_IDENTITY_TRACKING_LIMIT:
                        tracked_directories.add(child_identity)
                except OSError:
                    yield 0, 0, False
                finally:
                    if pending_descriptor >= 0:
                        os.close(pending_descriptor)
                        pending_descriptor = -1

            root_after = os.stat(self.storage_directory, follow_symlinks=False)
            if (root_after.st_dev, root_after.st_ino) != self._storage_root_identity:
                yield 0, 0, False
        except OSError:
            yield 0, 0, False
        finally:
            if pending_descriptor >= 0:
                os.close(pending_descriptor)
            for descriptor, entries, _identity in reversed(frames):
                entries.close()
                os.close(descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def _archive_scan_events_by_path(self) -> Iterator[tuple[int, int, bool]]:
        """Iterative best-effort no-follow fallback for non-POSIX platforms."""
        active_directories: set[tuple[int, int]] = set()
        tracked_directories: set[tuple[int, int]] = set()
        frames: list[tuple[Path, Any, tuple[int, int]]] = []

        try:
            root_before = os.stat(self.storage_directory, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_before.st_mode)
                or (root_before.st_dev, root_before.st_ino)
                != self._storage_root_identity
            ):
                yield 0, 0, False
                return
            root_entries = os.scandir(self.storage_directory)
            frames.append(
                (self.storage_directory, root_entries, self._storage_root_identity)
            )
            active_directories.add(self._storage_root_identity)
            tracked_directories.add(self._storage_root_identity)

            while frames:
                if self._scan_stop.is_set():
                    yield 0, 0, False
                    return
                _directory, entries, directory_identity = frames[-1]
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    frames.pop()
                    active_directories.remove(directory_identity)
                    yield 0, 0, True
                    continue
                except OSError:
                    yield 0, 0, False
                    continue

                try:
                    # DirEntry.stat() leaves st_dev/st_ino as zero on Windows.
                    # Fetch complete identity data before comparing devices or
                    # detecting directory replacement and cycles.
                    entry_status = os.stat(entry.path, follow_symlinks=False)
                except OSError:
                    yield 0, 0, False
                    continue
                if entry_status.st_dev != root_before.st_dev:
                    yield 0, 0, False
                    continue
                if stat.S_ISREG(entry_status.st_mode):
                    yield entry_status.st_size, 1, True
                    continue
                if not stat.S_ISDIR(entry_status.st_mode):
                    yield 0, 0, False
                    continue
                if len(frames) >= _RECONCILIATION_DEPTH_LIMIT:
                    yield 0, 0, False
                    continue

                child = Path(entry.path)
                try:
                    child_status = child.stat(follow_symlinks=False)
                    child_identity = (child_status.st_dev, child_status.st_ino)
                    if (
                        not stat.S_ISDIR(child_status.st_mode)
                        or child_status.st_dev != root_before.st_dev
                        or child_identity != (entry_status.st_dev, entry_status.st_ino)
                        or child_identity in active_directories
                        or child_identity in tracked_directories
                    ):
                        yield 0, 0, False
                        continue
                    yield 0, 0, True
                    child_entries = os.scandir(child)
                    child_after_open = child.stat(follow_symlinks=False)
                    if (
                        child_after_open.st_dev,
                        child_after_open.st_ino,
                    ) != child_identity:
                        child_entries.close()
                        yield 0, 0, False
                        continue
                    frames.append((child, child_entries, child_identity))
                    active_directories.add(child_identity)
                    if len(tracked_directories) < _DIRECTORY_IDENTITY_TRACKING_LIMIT:
                        tracked_directories.add(child_identity)
                except OSError:
                    yield 0, 0, False

            root_after = os.stat(self.storage_directory, follow_symlinks=False)
            if (root_after.st_dev, root_after.st_ino) != self._storage_root_identity:
                yield 0, 0, False
        except OSError:
            yield 0, 0, False
        finally:
            for _directory, entries, _identity in reversed(frames):
                entries.close()
