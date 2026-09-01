"""Tests for conservative upload-capacity accounting."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.api.app as app_module
import src.utils.storage_quota as storage_quota_module
from src.api.app import create_app
from src.config import Config
from src.utils.file_handler import FileDeletionResult, FileHandler
from src.utils.storage_quota import (
    CapacityUnavailable,
    StorageCapacity,
    UploadCapacityReservation,
)


def _capacity(
    tmp_path: Path,
    *,
    max_file_bytes: int = 100,
    max_storage_bytes: int = 1_000,
    max_storage_files: int = 1_000,
    minimum_free_bytes: int = 0,
    minimum_free_inodes: int = 0,
    maintenance_state_bytes: int = 32 * 1024 * 1024,
    state_directories: tuple[Path, ...] = (),
    enabled: bool = True,
) -> StorageCapacity:
    storage = tmp_path / "storage"
    temporary = tmp_path / "temporary"
    spool = tmp_path / "spool"
    storage.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    spool.mkdir(exist_ok=True)
    return StorageCapacity(
        storage_directory=storage,
        temp_directory=temporary,
        spool_directory=spool,
        max_file_bytes=max_file_bytes,
        max_storage_bytes=max_storage_bytes,
        max_storage_files=max_storage_files,
        minimum_free_bytes=minimum_free_bytes,
        minimum_free_inodes=minimum_free_inodes,
        maintenance_state_bytes=maintenance_state_bytes,
        state_directories=state_directories,
        persistent_archive_enabled=enabled,
    )


def test_reservations_enforce_atomic_persistent_boundary(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path, max_storage_bytes=200)
    barrier = threading.Barrier(3)
    release = threading.Event()
    admitted: list[object] = []
    rejected: list[CapacityUnavailable] = []
    result_lock = threading.Lock()

    def reserve() -> None:
        barrier.wait()
        reservation = None
        try:
            reservation = capacity.reserve_upload()
            reservation.complete_spool()
            reservation.claim_persistent()
        except CapacityUnavailable as exc:
            if reservation is not None:
                reservation.release()
            with result_lock:
                rejected.append(exc)
            return
        with result_lock:
            admitted.append(reservation)
        assert release.wait(5)
        reservation.release()

    threads = [threading.Thread(target=reserve) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(100):
        with result_lock:
            if len(admitted) + len(rejected) == 3:
                break
        release.wait(0.01)
    try:
        assert len(admitted) == 2
        assert len(rejected) == 1
        assert capacity.snapshot.persistent_reserved_bytes == 200
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert capacity.snapshot.active_reservations == 0


def test_pre_auth_transient_leases_do_not_monopolize_persistent_quota(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path, max_storage_bytes=100)
    slow_unauthenticated = [capacity.reserve_upload(), capacity.reserve_upload()]
    assert capacity.snapshot.persistent_reserved_bytes == 0

    authenticated = capacity.reserve_upload()
    authenticated.complete_spool()
    authenticated.claim_persistent()
    assert capacity.snapshot.persistent_reserved_bytes == 100
    for reservation in slow_unauthenticated:
        reservation.release()
    authenticated.release()


def test_completed_spool_is_not_double_charged_when_free_space_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100)
    available = iter((300, 200))
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=next(available), f_frsize=1, f_bsize=1, f_favail=10**9
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    assert capacity.snapshot.filesystem_reserved_bytes == 100
    # Parsing has materialized a 100-byte spool, represented by the lower
    # second statvfs value. Only the two future archive stages remain reserved.
    reservation.complete_spool()
    reservation.claim_persistent()
    assert capacity.snapshot.filesystem_reserved_bytes == 200
    reservation.release()


def test_filesystem_capacity_failure_rejects_without_leaking_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, minimum_free_bytes=10)

    def unavailable(_path: object) -> os.statvfs_result:
        raise OSError("simulated statvfs failure")

    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs", unavailable, raising=False
    )
    with pytest.raises(CapacityUnavailable, match="could not be verified"):
        capacity.reserve_upload()
    assert capacity.snapshot.active_reservations == 0
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    assert capacity.snapshot.persistent_reserved_bytes == 0


def test_post_auth_stage_probe_failure_keeps_transient_lease_releasable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    reservation = capacity.reserve_upload()
    reservation.complete_spool()

    def unavailable(_path: object) -> os.statvfs_result:
        raise OSError("simulated archive statvfs failure")

    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs", unavailable, raising=False
    )
    with pytest.raises(CapacityUnavailable, match="could not be verified"):
        reservation.claim_persistent()
    snapshot = capacity.snapshot
    assert snapshot.active_reservations == 1
    assert snapshot.active_archive_reservations == 0
    assert snapshot.filesystem_reserved_bytes == 0
    assert snapshot.persistent_reserved_bytes == 0
    reservation.release()
    assert capacity.snapshot.active_reservations == 0


def test_free_space_reserve_is_enforced(tmp_path: Path, monkeypatch) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100, minimum_free_bytes=25)
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=124, f_frsize=1, f_bsize=1, f_favail=10**9
        ),
        raising=False,
    )
    with pytest.raises(CapacityUnavailable, match="free-space reserve"):
        capacity.reserve_upload()

    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=125, f_frsize=1, f_bsize=1, f_favail=10**9
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    reservation.release()


def test_free_inode_reserve_is_enforced_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, minimum_free_inodes=5)
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9, f_frsize=1, f_bsize=1, f_favail=5
        ),
        raising=False,
    )
    with pytest.raises(CapacityUnavailable, match="free-inode reserve"):
        capacity.reserve_upload()

    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9, f_frsize=1, f_bsize=1, f_favail=6
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    assert capacity.snapshot.filesystem_reserved_inodes == 1
    reservation.release()


def test_archive_inode_reserve_is_rechecked_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, minimum_free_inodes=5)
    available_inodes = iter((10**9, 10))
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9,
            f_frsize=1,
            f_bsize=1,
            f_favail=next(available_inodes),
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    # One temp file plus one destination file and four date/system directories
    # need six inodes, which would leave only four free.
    with pytest.raises(CapacityUnavailable, match="free-inode reserve"):
        reservation.claim_persistent()
    reservation.release()
    assert capacity.snapshot.filesystem_reserved_inodes == 0


def test_statvfs_all_zero_inode_tuple_means_dynamic_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, minimum_free_inodes=5)
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9,
            f_frsize=1,
            f_bsize=1,
            f_files=0,
            f_ffree=0,
            f_favail=0,
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    reservation.release()

    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9,
            f_frsize=1,
            f_bsize=1,
            f_files=100,
            f_ffree=0,
            f_favail=0,
        ),
        raising=False,
    )
    with pytest.raises(CapacityUnavailable, match="free-inode reserve"):
        capacity.reserve_upload()


def test_stage_bytes_are_aggregated_by_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100)
    real_stat = os.stat
    stage_devices = {
        capacity.spool_directory: 10,
        capacity.temp_directory: 10,
        capacity.storage_directory: 20,
    }
    stage_inodes = {
        path: os.stat(path, follow_symlinks=False).st_ino for path in stage_devices
    }
    capacity._spool_root_identity = (10, stage_inodes[capacity.spool_directory])
    capacity._temp_root_identity = (10, stage_inodes[capacity.temp_directory])
    capacity._storage_root_identity = (20, stage_inodes[capacity.storage_directory])

    def staged_stat(path: object, *args: object, **kwargs: object) -> object:
        device = stage_devices.get(Path(path))
        if device is not None:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=device,
                st_ino=stage_inodes[Path(path)],
            )
        return real_stat(path, *args, **kwargs)

    statvfs_paths: list[Path] = []

    def staged_statvfs(path: object) -> object:
        statvfs_paths.append(Path(path))
        # Device 10 must reserve 200 bytes; device 20 reserves 100.
        return SimpleNamespace(f_bavail=200, f_frsize=1, f_bsize=1, f_favail=10**9)

    monkeypatch.setattr("src.utils.storage_quota.os.stat", staged_stat)
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs", staged_statvfs, raising=False
    )
    reservation = capacity.reserve_upload()
    assert capacity.snapshot.filesystem_reserved_bytes == 100
    assert capacity.snapshot.filesystem_reserved_inodes == 1
    reservation.complete_spool()
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    assert capacity.snapshot.filesystem_reserved_inodes == 0
    reservation.claim_persistent()
    assert capacity.snapshot.filesystem_reserved_bytes == 200
    assert capacity.snapshot.filesystem_reserved_inodes == 6
    # One pre-auth spool probe, then both archive devices are rechecked while
    # atomically expanding the authenticated reservation.
    assert len(statvfs_paths) == 3
    reservation.release()


def test_same_device_requires_all_three_stage_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100)
    available = iter((299, 199))
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=next(available), f_frsize=1, f_bsize=1, f_favail=10**9
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    with pytest.raises(CapacityUnavailable, match="free-space reserve"):
        reservation.claim_persistent()
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    reservation.release()


def test_capacity_probe_epoch_rejects_stale_preauth_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100, minimum_free_bytes=25)
    first = capacity.reserve_upload()
    probe_finished = threading.Event()
    resume_probe = threading.Event()
    probes = 0

    def stale_available(
        device_paths: dict[int, Path],
    ) -> tuple[dict[int, int], dict[int, int]]:
        nonlocal probes
        probes += 1
        if probes > 1:
            return dict.fromkeys(device_paths, 124), dict.fromkeys(device_paths, 10**9)
        probe_finished.set()
        assert resume_probe.wait(5)
        return dict.fromkeys(device_paths, 225), dict.fromkeys(device_paths, 10**9)

    monkeypatch.setattr(capacity, "_available_capacity", stale_available)
    rejected: list[CapacityUnavailable] = []

    def reserve_second() -> None:
        try:
            capacity.reserve_upload()
        except CapacityUnavailable as exc:
            rejected.append(exc)

    worker = threading.Thread(target=reserve_second)
    worker.start()
    assert probe_finished.wait(5)
    # The first upload has now consumed its spool allocation. Its reservation
    # disappears, so the second caller's earlier statvfs result is stale.
    first.complete_spool()
    resume_probe.set()
    worker.join(5)
    first.release()

    assert not worker.is_alive()
    assert len(rejected) == 1
    assert "free-space reserve" in str(rejected[0])
    assert probes == 2
    assert capacity.snapshot.active_reservations == 0


def test_capacity_probe_epoch_rejects_stale_archive_stage_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100, minimum_free_bytes=25)
    first = capacity.reserve_upload()
    first.complete_spool()
    first.claim_persistent()
    second = capacity.reserve_upload()
    second.complete_spool()
    probe_finished = threading.Event()
    resume_probe = threading.Event()
    probes = 0

    def stale_available(
        device_paths: dict[int, Path],
    ) -> tuple[dict[int, int], dict[int, int]]:
        nonlocal probes
        probes += 1
        if probes > 1:
            return dict.fromkeys(device_paths, 324), dict.fromkeys(device_paths, 10**9)
        probe_finished.set()
        assert resume_probe.wait(5)
        return dict.fromkeys(device_paths, 424), dict.fromkeys(device_paths, 10**9)

    monkeypatch.setattr(capacity, "_available_capacity", stale_available)
    rejected: list[CapacityUnavailable] = []

    def claim_second() -> None:
        try:
            second.claim_persistent()
        except CapacityUnavailable as exc:
            rejected.append(exc)

    worker = threading.Thread(target=claim_second)
    worker.start()
    assert probe_finished.wait(5)
    first.complete_custom_temp()
    resume_probe.set()
    worker.join(5)
    first.release()
    second.release()

    assert not worker.is_alive()
    assert len(rejected) == 1
    assert "free-space reserve" in str(rejected[0])
    assert probes == 2
    assert capacity.snapshot.active_reservations == 0


def test_separate_state_filesystem_enforces_write_margin_and_free_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = tmp_path / "database"
    state_directory.mkdir()
    capacity = _capacity(tmp_path, max_file_bytes=100, minimum_free_bytes=25)
    capacity.state_directories = (state_directory,)
    real_state = os.stat(state_directory, follow_symlinks=False)
    capacity._state_root_identities = ((real_state.st_dev + 1, real_state.st_ino),)
    real_stat = os.stat

    def staged_stat(path: object, *args: object, **kwargs: object) -> object:
        result = real_stat(path, *args, **kwargs)
        if Path(path) == state_directory:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev + 1,
                st_ino=result.st_ino,
            )
        return result

    def staged_statvfs(path: object) -> object:
        available = 1024 * 1024 + 24 if Path(path) == state_directory else 10**9
        return SimpleNamespace(
            f_bavail=available, f_frsize=1, f_bsize=1, f_favail=10**9
        )

    monkeypatch.setattr("src.utils.storage_quota.os.stat", staged_stat)
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs", staged_statvfs, raising=False
    )
    with pytest.raises(CapacityUnavailable, match="free-space reserve"):
        capacity.reserve_upload()


def test_form_valid_claim_rechecks_state_filesystem_free_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = tmp_path / "database"
    state_directory.mkdir()
    capacity = _capacity(
        tmp_path,
        max_file_bytes=100,
        minimum_free_bytes=25,
        enabled=False,
    )
    capacity.state_directories = (state_directory,)
    capacity._state_root_identities = (
        capacity._directory_identity(state_directory, "State"),
    )
    calls = 0

    def phase_available(
        device_paths: dict[int, Path],
    ) -> tuple[dict[int, int], dict[int, int]]:
        nonlocal calls
        calls += 1
        available = 2 * 1024 * 1024 if calls == 1 else 1024 * 1024 + 24
        return dict.fromkeys(device_paths, available), dict.fromkeys(
            device_paths, 10**9
        )

    monkeypatch.setattr(capacity, "_available_capacity", phase_available)

    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    with pytest.raises(CapacityUnavailable, match="free-space reserve"):
        reservation.claim_persistent()
    reservation.release()
    assert capacity.snapshot.active_reservations == 0


@pytest.mark.parametrize("value", [0, 1_048_577])
def test_minimum_free_space_config_is_strictly_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        Config(file_handling={"minimum_free_space_mb": value})


@pytest.mark.parametrize("value", [0, 104_857_601])
def test_persistent_archive_quota_config_is_strictly_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        Config(file_handling={"storage": {"max_storage_size_mb": value}})


@pytest.mark.parametrize("value", [0, 100_000_001])
def test_persistent_archive_file_count_config_is_strictly_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        Config(file_handling={"storage": {"max_storage_files": value}})


@pytest.mark.parametrize("value", [0, 100_000_001])
def test_minimum_free_inode_config_is_strictly_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        Config(file_handling={"minimum_free_inodes": value})


@pytest.mark.parametrize("value", [31, 1025])
def test_maintenance_state_reserve_config_is_strictly_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        Config(file_handling={"maintenance_state_reserve_mb": value})


def test_uploads_preserve_idle_maintenance_state_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    capacity = _capacity(
        tmp_path,
        max_file_bytes=100,
        minimum_free_bytes=10,
        maintenance_state_bytes=32,
        state_directories=(state,),
        enabled=False,
    )
    available = 1024 * 1024 + 100 + 32 + 10
    monkeypatch.setattr(
        capacity,
        "_available_capacity",
        lambda paths: (dict.fromkeys(paths, available), dict.fromkeys(paths, 10**9)),
    )
    reservation = capacity.reserve_upload()
    reservation.release()

    available -= 1
    with pytest.raises(CapacityUnavailable, match="free-space reserve"):
        capacity.reserve_upload()


def test_maintenance_state_guard_claims_and_releases_exact_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    capacity = _capacity(
        tmp_path,
        minimum_free_bytes=10,
        minimum_free_inodes=5,
        maintenance_state_bytes=32,
        state_directories=(state,),
        enabled=False,
    )
    monkeypatch.setattr(
        capacity,
        "_available_capacity",
        lambda paths: (dict.fromkeys(paths, 42), dict.fromkeys(paths, 9)),
    )

    with capacity.maintenance_state_guard():
        snapshot = capacity.snapshot
        assert snapshot.filesystem_reserved_bytes == 32
        assert snapshot.filesystem_reserved_inodes == 4
        with pytest.raises(CapacityUnavailable, match="already active"):
            with capacity.maintenance_state_guard():
                pass

    assert capacity.snapshot.filesystem_reserved_bytes == 0
    assert capacity.snapshot.filesystem_reserved_inodes == 0

    monkeypatch.setattr(
        capacity,
        "_available_capacity",
        lambda paths: (dict.fromkeys(paths, 41), dict.fromkeys(paths, 9)),
    )
    with pytest.raises(CapacityUnavailable, match="Maintenance filesystem"):
        with capacity.maintenance_state_guard():
            pass


def test_state_margin_is_reserved_once_per_shared_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_state = tmp_path / "database"
    second_state = tmp_path / "logs"
    first_state.mkdir()
    second_state.mkdir()
    capacity = _capacity(tmp_path, max_file_bytes=100)
    capacity.state_directories = (first_state, second_state)
    capacity._state_root_identities = tuple(
        capacity._directory_identity(path, "State")
        for path in capacity.state_directories
    )
    capacity._state_devices = frozenset(
        device for device, _inode in capacity._state_root_identities
    )
    monkeypatch.setattr(
        "src.utils.storage_quota.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=10**9, f_frsize=1, f_bsize=1, f_favail=10**9
        ),
        raising=False,
    )
    reservation = capacity.reserve_upload()
    assert capacity.snapshot.filesystem_reserved_bytes == 100 + 1024 * 1024
    assert capacity.snapshot.filesystem_reserved_inodes == 9
    reservation.complete_spool()
    assert capacity.snapshot.filesystem_reserved_bytes == 1024 * 1024
    assert capacity.snapshot.filesystem_reserved_inodes == 8
    reservation.claim_persistent()
    assert capacity.snapshot.filesystem_reserved_bytes == 200 + 1024 * 1024
    assert capacity.snapshot.filesystem_reserved_inodes == 14
    reservation.release()


def test_commit_and_successful_delete_update_persistent_accounting(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path)
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    assert capacity.snapshot.persistent_reserved_files == 1
    assert capacity.snapshot.filesystem_reserved_bytes == 200
    reservation.complete_custom_temp()
    assert capacity.snapshot.filesystem_reserved_bytes == 100
    stored = capacity.storage_directory / "stored.mp3"
    stored.write_bytes(b"x" * 40)
    reservation.commit_stored_path(stored)
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    assert capacity.snapshot.active_archive_reservations == 0
    reservation.release()
    assert capacity.snapshot.stored_bytes == 40
    assert capacity.snapshot.stored_files == 1
    assert capacity.snapshot.persistent_reserved_bytes == 0
    assert capacity.snapshot.persistent_reserved_files == 0

    capacity.record_deleted(40)
    assert capacity.snapshot.stored_bytes == 0
    assert capacity.snapshot.stored_files == 0


def test_failed_or_missing_delete_stays_charged_until_reconciliation(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path)
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    stored = capacity.storage_directory / "stored.mp3"
    stored.write_bytes(b"x" * 40)
    reservation.commit_stored_path(stored)
    reservation.release()

    # A failed/missing compensation does not call record_deleted.
    assert capacity.snapshot.stored_bytes == 40
    assert capacity.snapshot.stored_files == 1
    stored.unlink()
    assert capacity.snapshot.stored_bytes == 40
    assert capacity.snapshot.stored_files == 1
    assert capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 0
    assert capacity.snapshot.stored_files == 0


def test_startup_over_quota_fails_upload_admission(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "orphan.mp3").write_bytes(b"x" * 101)
    (tmp_path / "temp").mkdir()
    (tmp_path / "spool").mkdir()
    capacity = StorageCapacity(
        storage_directory=storage,
        temp_directory=tmp_path / "temp",
        spool_directory=tmp_path / "spool",
        max_file_bytes=10,
        max_storage_bytes=100,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    assert capacity.snapshot.over_quota
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    with pytest.raises(CapacityUnavailable, match="quota is exhausted"):
        reservation.claim_persistent()
    reservation.release()


def test_persistent_file_count_reservation_is_atomic(tmp_path: Path) -> None:
    capacity = _capacity(
        tmp_path,
        max_file_bytes=100,
        max_storage_bytes=10_000,
        max_storage_files=1,
    )
    first = capacity.reserve_upload()
    first.complete_spool()
    first.claim_persistent()
    assert capacity.snapshot.persistent_reserved_files == 1

    second = capacity.reserve_upload()
    second.complete_spool()
    with pytest.raises(CapacityUnavailable, match="quota is exhausted"):
        second.claim_persistent()
    second.release()
    first.release()
    assert capacity.snapshot.persistent_reserved_files == 0


def test_startup_file_count_quota_fails_closed(tmp_path: Path) -> None:
    storage = tmp_path / "storage-count"
    temporary = tmp_path / "temporary-count"
    spool = tmp_path / "spool-count"
    storage.mkdir()
    temporary.mkdir()
    spool.mkdir()
    (storage / "first.mp3").write_bytes(b"x")
    (storage / "second.mp3").write_bytes(b"x")
    capacity = StorageCapacity(
        storage_directory=storage,
        temp_directory=temporary,
        spool_directory=spool,
        max_file_bytes=100,
        max_storage_bytes=10_000,
        max_storage_files=1,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    assert capacity.snapshot.stored_files == 2
    assert capacity.snapshot.over_quota
    assert not capacity.ready_for_upload()
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    with pytest.raises(CapacityUnavailable, match="quota is exhausted"):
        reservation.claim_persistent()
    reservation.release()


def test_uncertain_startup_scan_fails_closed(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path)
    link = capacity.storage_directory / "untrusted-link"
    try:
        link.symlink_to(tmp_path / "outside")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    assert not capacity.reconcile()
    assert not capacity.snapshot.scan_certain
    pending = capacity.reserve_upload()
    pending.complete_spool()
    with pytest.raises(CapacityUnavailable, match="accounting is unavailable"):
        pending.claim_persistent()
    pending.release()


def test_unexpected_reconciliation_failure_invalidates_prior_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    assert capacity.snapshot.scan_certain

    def fail_scan() -> Iterator[tuple[int, int, bool]]:
        raise RuntimeError("simulated scanner failure")
        yield 0, 0, True

    monkeypatch.setattr(capacity, "_archive_scan_events", fail_scan)
    with pytest.raises(RuntimeError, match="scanner failure"):
        capacity.reconcile()
    assert not capacity.snapshot.scan_certain
    pending = capacity.reserve_upload()
    pending.complete_spool()
    with pytest.raises(CapacityUnavailable, match="accounting is unavailable"):
        pending.claim_persistent()
    pending.release()


def test_full_over_quota_scan_cannot_reopen_after_partial_delete(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=10, max_storage_bytes=100)
    (capacity.storage_directory / "early.mp3").write_bytes(b"x" * 101)
    (capacity.storage_directory / "later.mp3").write_bytes(b"x" * 500)
    assert capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 601
    capacity.record_deleted(101)
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    with pytest.raises(CapacityUnavailable, match="quota is exhausted"):
        reservation.claim_persistent()
    reservation.release()


def test_external_drift_reconciles_only_without_active_upload(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path)
    external = capacity.storage_directory / "external.mp3"
    external.write_bytes(b"x" * 30)
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    assert not capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 0
    reservation.release()
    assert capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 30


def test_slow_preauth_reservations_do_not_starve_archive_reconciliation(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path)
    reservations = [capacity.reserve_upload(), capacity.reserve_upload()]
    (capacity.storage_directory / "external.mp3").write_bytes(b"x" * 30)
    assert capacity.snapshot.active_archive_reservations == 0
    assert capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 30
    for reservation in reservations:
        reservation.release()


def test_reconciliation_publishes_only_after_all_bounded_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100, max_storage_bytes=10_000)
    for index in range(7):
        (capacity.storage_directory / f"call-{index}.mp3").write_bytes(
            b"x" * (index + 1)
        )
    monkeypatch.setattr(storage_quota_module, "_RECONCILIATION_ENTRY_BUDGET", 2)
    monkeypatch.setattr(
        storage_quota_module, "_RECONCILIATION_TIME_BUDGET_SECONDS", 60.0
    )

    assert not capacity.reconcile()
    first_slice = capacity.snapshot
    assert first_slice.reconciliation_pending
    assert not first_slice.scan_certain
    # A partial sum is never published as authoritative accounting.
    assert first_slice.stored_bytes == 0
    assert not capacity.ready_for_upload()
    pending = capacity.reserve_upload()
    pending.complete_spool()
    with pytest.raises(CapacityUnavailable, match="accounting is unavailable"):
        pending.claim_persistent()
    pending.release()

    slices = 1
    while not capacity.reconcile():
        slices += 1
        assert slices < 20
        assert capacity.snapshot.reconciliation_pending
    assert slices > 1
    assert capacity.snapshot.scan_certain
    assert not capacity.snapshot.reconciliation_pending
    assert capacity.snapshot.stored_bytes == sum(range(1, 8))


def test_mutation_restarts_partial_scan_and_closes_stale_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    monkeypatch.setattr(storage_quota_module, "_RECONCILIATION_ENTRY_BUDGET", 1)
    monkeypatch.setattr(
        storage_quota_module, "_RECONCILIATION_TIME_BUDGET_SECONDS", 60.0
    )
    started: list[int] = []
    closed: list[int] = []

    def scan_events() -> Iterator[tuple[int, int, bool]]:
        scan_number = len(started)
        started.append(scan_number)
        try:
            yield 10, 1, True
            yield 20, 1, True
            yield 30, 1, True
        finally:
            closed.append(scan_number)

    monkeypatch.setattr(capacity, "_archive_scan_events", scan_events)
    assert not capacity.reconcile()
    assert capacity.snapshot.stored_bytes == 0
    capacity.record_structure_mutation()

    # The next slice closes and discards the stale partial sum before starting
    # from a new generation.
    assert not capacity.reconcile()
    assert started == [0, 1]
    assert closed == [0]
    assert capacity.snapshot.stored_bytes == 0
    while not capacity.reconcile():
        pass
    assert capacity.snapshot.stored_bytes == 60
    assert capacity.snapshot.stored_files == 3
    assert closed == [0, 1]


def test_close_releases_pending_reconciliation_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    monkeypatch.setattr(storage_quota_module, "_RECONCILIATION_ENTRY_BUDGET", 1)
    monkeypatch.setattr(
        storage_quota_module, "_RECONCILIATION_TIME_BUDGET_SECONDS", 60.0
    )
    cursor_closed = threading.Event()

    def scan_events() -> Iterator[tuple[int, int, bool]]:
        try:
            yield 1, 1, True
            yield 1, 1, True
        finally:
            cursor_closed.set()

    monkeypatch.setattr(capacity, "_archive_scan_events", scan_events)
    assert not capacity.reconcile()
    assert capacity.snapshot.reconciliation_pending
    capacity.close()
    assert cursor_closed.wait(1)
    assert not capacity.snapshot.reconciliation_pending


def test_admission_fails_fast_while_background_reconciliation_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed: list[bool] = []

    original_advance = capacity._advance_scan_cursor

    def delayed_scan(cursor: Any) -> bool:
        entered.set()
        assert release.wait(5)
        return original_advance(cursor)

    monkeypatch.setattr(capacity, "_advance_scan_cursor", delayed_scan)
    worker = threading.Thread(target=lambda: completed.append(capacity.reconcile()))
    worker.start()
    assert entered.wait(5)
    try:
        pending = capacity.reserve_upload()
        pending.complete_spool()
        with pytest.raises(CapacityUnavailable, match="accounting is unavailable"):
            pending.claim_persistent()
        pending.release()
    finally:
        release.set()
        worker.join(5)
    assert completed == [True]


def test_deferred_initial_scan_fails_closed_until_background_reconcile(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    temporary = tmp_path / "temporary"
    spool = tmp_path / "spool"
    storage.mkdir()
    temporary.mkdir()
    spool.mkdir()
    capacity = StorageCapacity(
        storage_directory=storage,
        temp_directory=temporary,
        spool_directory=spool,
        max_file_bytes=100,
        max_storage_bytes=1_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
        scan_on_initialize=False,
    )
    pending = capacity.reserve_upload()
    pending.complete_spool()
    with pytest.raises(CapacityUnavailable, match="accounting is unavailable"):
        pending.claim_persistent()
    pending.release()
    assert capacity.reconcile()
    reservation = capacity.reserve_upload()
    reservation.release()


def test_storage_root_replacement_invalidates_reconciliation_and_readiness(
    tmp_path: Path,
) -> None:
    capacity = _capacity(tmp_path)
    original_root = capacity.storage_directory.with_name("storage-original")
    capacity.storage_directory.rename(original_root)
    capacity.storage_directory.mkdir(mode=0o700)

    assert not capacity.reconcile()
    assert not capacity.snapshot.scan_certain
    assert not capacity.ready_for_upload()
    transient = capacity.reserve_upload()
    transient.complete_spool()
    with pytest.raises(CapacityUnavailable, match="Storage filesystem root changed"):
        transient.claim_persistent()
    transient.release()


def test_temp_root_replacement_rejects_claim_and_file_creation(tmp_path: Path) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temporary"),
        min_file_size_kb=0,
    )
    capacity = StorageCapacity(
        storage_directory=handler.storage_dir,
        temp_directory=handler.temp_dir,
        spool_directory=tmp_path,
        max_file_bytes=100,
        max_storage_bytes=1_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    handler.attach_storage_capacity(capacity)
    original_temp = handler.temp_dir.with_name("temporary-original")
    handler.temp_dir.rename(original_temp)
    handler.temp_dir.mkdir(mode=0o700)

    with pytest.raises(OSError, match="temp root changed"):
        handler.save_temp_file("call.mp3", b"ID3payload")
    assert list(handler.temp_dir.iterdir()) == []

    transient = capacity.reserve_upload()
    transient.complete_spool()
    with pytest.raises(
        CapacityUnavailable, match="Application temp filesystem root changed"
    ):
        transient.claim_persistent()
    transient.release()
    handler.close()


def test_readiness_requires_a_full_maximum_upload_quota_slot(tmp_path: Path) -> None:
    empty_case = tmp_path / "empty"
    empty_case.mkdir()
    empty = _capacity(empty_case, max_file_bytes=100, max_storage_bytes=100)
    assert empty.ready_for_upload()

    for used_bytes in (1, 100):
        case = tmp_path / f"used-{used_bytes}"
        storage = case / "storage"
        temporary = case / "temporary"
        spool = case / "spool"
        storage.mkdir(parents=True)
        temporary.mkdir()
        spool.mkdir()
        (storage / "existing.mp3").write_bytes(b"x" * used_bytes)
        capacity = StorageCapacity(
            storage_directory=storage,
            temp_directory=temporary,
            spool_directory=spool,
            max_file_bytes=100,
            max_storage_bytes=100,
            max_storage_files=1_000,
            minimum_free_bytes=0,
            minimum_free_inodes=0,
            persistent_archive_enabled=True,
        )
        assert capacity.snapshot.scan_certain
        assert not capacity.ready_for_upload()


def test_readiness_enforces_filesystem_free_space_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path, max_file_bytes=100, minimum_free_bytes=25)
    monkeypatch.setattr(
        capacity,
        "_available_capacity",
        lambda paths: (dict.fromkeys(paths, 324), dict.fromkeys(paths, 10**9)),
    )
    assert not capacity.ready_for_upload()
    monkeypatch.setattr(
        capacity,
        "_available_capacity",
        lambda paths: (dict.fromkeys(paths, 325), dict.fromkeys(paths, 10**9)),
    )
    assert capacity.ready_for_upload()


def test_failed_initial_reconciliation_retries_on_short_interval(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["storage"]["cleanup_interval_hours"] = 8760
    attempts: list[float] = []
    second_attempt = threading.Event()

    def reconcile_twice(_capacity: StorageCapacity) -> bool:
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return False
        second_attempt.set()
        return True

    monkeypatch.setattr(app_module, "MAINTENANCE_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(StorageCapacity, "reconcile", reconcile_twice)
    app = create_app(override_config=Config.model_validate(test_config_dict))
    with TestClient(app):
        assert second_attempt.wait(2)

    assert len(attempts) >= 2
    assert attempts[1] - attempts[0] < 1


def test_health_is_not_ready_during_slow_initial_archive_scan(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    entered = threading.Event()
    release = threading.Event()
    original_advance = StorageCapacity._advance_scan_cursor

    def delayed_scan(capacity: StorageCapacity, cursor: Any) -> bool:
        entered.set()
        assert release.wait(5)
        return original_advance(capacity, cursor)

    monkeypatch.setattr(StorageCapacity, "_advance_scan_cursor", delayed_scan)
    app = create_app(override_config=Config.model_validate(test_config_dict))
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            response = client.get("/health")
            assert response.status_code == 503
            assert response.json()["status"] == "unhealthy"
            release.set()
            for _ in range(100):
                response = client.get("/health")
                if response.status_code == 200:
                    break
                time.sleep(0.01)
            assert response.status_code == 200
    finally:
        release.set()


def test_health_is_not_ready_between_reconciliation_slices(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    storage = Path(test_config_dict["file_handling"]["storage"]["directory"])
    storage.mkdir(mode=0o700)
    for index in range(3):
        (storage / f"existing-{index}.mp3").write_bytes(b"x")
    pending_slice = threading.Event()
    original_reconcile = StorageCapacity.reconcile

    def observed_reconcile(capacity: StorageCapacity) -> bool:
        result = original_reconcile(capacity)
        snapshot = capacity.snapshot
        if snapshot.reconciliation_pending and not snapshot.reconciling:
            pending_slice.set()
        return result

    monkeypatch.setattr(storage_quota_module, "_RECONCILIATION_ENTRY_BUDGET", 1)
    monkeypatch.setattr(
        storage_quota_module, "_RECONCILIATION_TIME_BUDGET_SECONDS", 60.0
    )
    monkeypatch.setattr(
        app_module, "MAINTENANCE_RECONCILIATION_SLICE_DELAY_SECONDS", 5.0
    )
    monkeypatch.setattr(StorageCapacity, "reconcile", observed_reconcile)
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        assert pending_slice.wait(2)
        snapshot = app.state.storage_capacity.snapshot
        assert snapshot.reconciliation_pending
        assert not snapshot.reconciling
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_is_not_ready_after_uncertain_archive_scan(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    attempted = threading.Event()

    def uncertain_scan(
        _capacity: StorageCapacity,
    ) -> Iterator[tuple[int, int, bool]]:
        attempted.set()
        yield 0, 0, False

    monkeypatch.setattr(StorageCapacity, "_archive_scan_events", uncertain_scan)
    app = create_app(override_config=Config.model_validate(test_config_dict))
    with TestClient(app) as client:
        assert attempted.wait(2)
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_is_not_ready_after_storage_root_replacement(
    test_config_dict: dict[str, Any],
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            if app.state.storage_capacity.snapshot.scan_certain:
                break
            time.sleep(0.01)
        capacity = app.state.storage_capacity
        assert capacity.snapshot.scan_certain
        storage = app.state.file_handler.storage_dir
        storage.rename(storage.with_name("storage-original"))
        storage.mkdir(mode=0o700)
        assert not capacity.reconcile()
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


@pytest.mark.parametrize("existing_bytes", [1, 1024 * 1024])
def test_health_requires_a_complete_maximum_upload_quota_slot(
    test_config_dict: dict[str, Any], existing_bytes: int
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["max_file_size_mb"] = 1
    test_config_dict["file_handling"]["storage"]["max_storage_size_mb"] = 1
    storage = Path(test_config_dict["file_handling"]["storage"]["directory"])
    storage.mkdir(mode=0o700)
    (storage / "existing.mp3").write_bytes(b"x" * existing_bytes)
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            if app.state.storage_capacity.snapshot.scan_certain:
                break
            time.sleep(0.01)
        assert app.state.storage_capacity.snapshot.scan_certain
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_enforces_filesystem_free_space_floor(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["max_file_size_mb"] = 1
    test_config_dict["file_handling"]["minimum_free_space_mb"] = 1
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            if app.state.storage_capacity.snapshot.scan_certain:
                break
            time.sleep(0.01)
        capacity = app.state.storage_capacity
        assert capacity.snapshot.scan_certain
        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: (dict.fromkeys(paths, 0), dict.fromkeys(paths, 10**9)),
        )
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_preserves_maintenance_headroom_on_state_filesystems(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "log_only"
    test_config_dict["file_handling"]["max_file_size_mb"] = 1
    test_config_dict["file_handling"]["minimum_free_space_mb"] = 1
    test_config_dict["file_handling"]["maintenance_state_reserve_mb"] = 32
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        capacity = app.state.storage_capacity
        required_bytes, required_inodes, paths, _spool_device = (
            capacity._preauth_requirements()
        )
        state_devices = {device for device, _inode in capacity._state_root_identities}
        one_mb = 1024 * 1024

        def boundary_capacity(
            device_paths: dict[int, Path], *, one_byte_short: bool = False
        ) -> tuple[dict[int, int], dict[int, int]]:
            available_bytes = {
                device: required_bytes[device]
                + one_mb
                + (32 * one_mb if device in state_devices else 0)
                - (1 if one_byte_short and device in state_devices else 0)
                for device in device_paths
            }
            available_inodes = {
                device: required_inodes[device]
                + capacity.minimum_free_inodes
                + (4 if device in state_devices else 0)
                for device in device_paths
            }
            return available_bytes, available_inodes

        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: boundary_capacity(paths),
        )
        assert client.get("/health").status_code == 200

        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: boundary_capacity(paths, one_byte_short=True),
        )
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_enforces_filesystem_free_inode_floor(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["max_file_size_mb"] = 1
    test_config_dict["file_handling"]["minimum_free_inodes"] = 1
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            if app.state.storage_capacity.snapshot.scan_certain:
                break
            time.sleep(0.01)
        capacity = app.state.storage_capacity
        assert capacity.snapshot.scan_certain
        monkeypatch.setattr(
            capacity,
            "_available_capacity",
            lambda paths: (dict.fromkeys(paths, 10**12), dict.fromkeys(paths, 0)),
        )
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_requires_a_persistent_file_count_slot(
    test_config_dict: dict[str, Any],
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["max_file_size_mb"] = 1
    test_config_dict["file_handling"]["storage"]["max_storage_files"] = 1
    storage = Path(test_config_dict["file_handling"]["storage"]["directory"])
    storage.mkdir(mode=0o700)
    (storage / "existing.mp3").write_bytes(b"x")
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            if app.state.storage_capacity.snapshot.scan_certain:
                break
            time.sleep(0.01)
        snapshot = app.state.storage_capacity.snapshot
        assert snapshot.scan_certain
        assert snapshot.stored_files == 1
        assert not snapshot.over_quota
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


def test_health_is_not_ready_when_existing_archive_is_over_quota(
    test_config_dict: dict[str, Any],
) -> None:
    test_config_dict["processing"]["mode"] = "store"
    test_config_dict["file_handling"]["storage"]["max_storage_size_mb"] = 1
    storage = Path(test_config_dict["file_handling"]["storage"]["directory"])
    storage.mkdir(mode=0o700)
    (storage / "existing.mp3").write_bytes(b"x" * (1024 * 1024 + 1))
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app) as client:
        for _ in range(100):
            snapshot = app.state.storage_capacity.snapshot
            if snapshot.scan_certain:
                break
            time.sleep(0.01)
        assert snapshot.scan_certain
        assert snapshot.over_quota
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


@pytest.mark.skipif(
    not Path("/var").is_symlink() or not Path("/private/var").is_dir(),
    reason="requires the macOS root-owned /var alias",
)
def test_file_log_state_capacity_uses_canonical_validated_system_alias(
    test_config_dict: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    database_parent = Path(test_config_dict["database"]["path"]).parent.resolve(
        strict=True
    )
    try:
        alias_relative = database_parent.relative_to("/private/var")
    except ValueError:
        pytest.skip("test temporary directory is not beneath /private/var")
    alias_log_parent = Path("/var") / alias_relative / "alias-logs"
    canonical_log_parent = Path("/private/var") / alias_relative / "alias-logs"
    test_config_dict["logging"]["file"]["enabled"] = True
    test_config_dict["logging"]["file"]["path"] = str(alias_log_parent / "app.log")
    # The test exercises lifespan's state-directory normalization without
    # retaining a process-global file logging handler after its temp tree dies.
    monkeypatch.setattr(app_module, "setup_logging", lambda _config: None)
    app = create_app(override_config=Config.model_validate(test_config_dict))

    with TestClient(app):
        capacity = app.state.storage_capacity
        assert canonical_log_parent in capacity.state_directories
        assert alias_log_parent not in capacity.state_directories
        reservation = capacity.reserve_upload()
        reservation.release()


def test_nonpersistent_mode_does_not_depend_on_archive_scan(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path, enabled=False)
    unexpected = capacity.storage_directory / "unexpected-link"
    try:
        unexpected.symlink_to(tmp_path / "outside")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    assert capacity.snapshot.scan_certain
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    assert capacity.snapshot.persistent_reserved_bytes == 0
    assert capacity.snapshot.filesystem_reserved_bytes == 0
    reservation.release()
    assert capacity.reconcile()
    assert capacity.snapshot.scan_certain


def test_store_publish_and_accounting_are_one_mutation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    capacity = StorageCapacity(
        storage_directory=handler.storage_dir,
        temp_directory=handler.temp_dir,
        spool_directory=tmp_path,
        max_file_bytes=1024,
        max_storage_bytes=10_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    handler.attach_storage_capacity(capacity)
    source = handler.save_temp_file("call.mp3", b"ID3payload")
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    reservation.complete_custom_temp()
    original_commit = UploadCapacityReservation.commit_stored_path
    entered = threading.Event()
    release = threading.Event()

    def delayed_commit(
        active_reservation: UploadCapacityReservation, path: str | Path
    ) -> None:
        original_commit(active_reservation, path)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(UploadCapacityReservation, "commit_stored_path", delayed_commit)
    stored: list[Path] = []
    worker = threading.Thread(
        target=lambda: stored.append(
            handler.store_file(
                source,
                "1",
                datetime.now(UTC),
                capacity_reservation=reservation,
            )
        )
    )
    worker.start()
    assert entered.wait(5)
    try:
        # commit_stored_path has run, so only the mutation guard prevents a
        # scan from racing the still-active store wrapper.
        assert not capacity.reconcile()
    finally:
        release.set()
        worker.join(5)
        reservation.release()
        handler.close()
    assert len(stored) == 1
    assert capacity.snapshot.stored_bytes == len(b"ID3payload")


def test_unlink_and_accounting_are_one_mutation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    existing = handler.storage_dir / "existing.mp3"
    existing.write_bytes(b"x" * 40)
    capacity = StorageCapacity(
        storage_directory=handler.storage_dir,
        temp_directory=handler.temp_dir,
        spool_directory=tmp_path,
        max_file_bytes=100,
        max_storage_bytes=1_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    handler.attach_storage_capacity(capacity)
    original_record = capacity.record_deleted
    entered = threading.Event()
    release = threading.Event()

    def delayed_record(freed_bytes: int) -> None:
        entered.set()
        assert release.wait(5)
        original_record(freed_bytes)

    monkeypatch.setattr(capacity, "record_deleted", delayed_record)
    results: list[FileDeletionResult] = []
    worker = threading.Thread(
        target=lambda: results.append(handler.delete_file(str(existing)))
    )
    worker.start()
    assert entered.wait(5)
    try:
        assert not capacity.reconcile()
    finally:
        release.set()
        worker.join(5)
        handler.close()
    assert results == [FileDeletionResult("deleted", freed_bytes=40)]
    assert capacity.snapshot.stored_bytes == 0


def test_post_unlink_fsync_failure_releases_capacity_inside_mutation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    existing = handler.storage_dir / "existing.mp3"
    existing.write_bytes(b"x" * 40)
    capacity = StorageCapacity(
        storage_directory=handler.storage_dir,
        temp_directory=handler.temp_dir,
        spool_directory=tmp_path,
        max_file_bytes=100,
        max_storage_bytes=1_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    handler.attach_storage_capacity(capacity)
    assert capacity.snapshot.stored_bytes == 40

    monkeypatch.setattr(
        "src.utils.file_handler.os.fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("forced fsync failure")),
    )
    original_record = capacity.record_deleted
    entered = threading.Event()
    release = threading.Event()

    def delayed_record(freed_bytes: int) -> None:
        entered.set()
        assert release.wait(5)
        original_record(freed_bytes)

    monkeypatch.setattr(capacity, "record_deleted", delayed_record)
    results: list[FileDeletionResult] = []
    worker = threading.Thread(
        target=lambda: results.append(handler.delete_file(str(existing)))
    )
    worker.start()
    assert entered.wait(5)
    try:
        assert not existing.exists()
        assert not capacity.reconcile()
    finally:
        release.set()
        worker.join(5)
        handler.close()

    assert not worker.is_alive()
    assert results == [
        FileDeletionResult(
            "retry",
            freed_bytes=40,
            error="forced fsync failure",
            unlink_succeeded=True,
        )
    ]
    assert capacity.snapshot.stored_bytes == 0
    assert capacity.snapshot.stored_files == 0


def test_unknown_store_failure_marks_accounting_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    capacity = StorageCapacity(
        storage_directory=handler.storage_dir,
        temp_directory=handler.temp_dir,
        spool_directory=tmp_path,
        max_file_bytes=100,
        max_storage_bytes=1_000,
        max_storage_files=1_000,
        minimum_free_bytes=0,
        minimum_free_inodes=0,
        persistent_archive_enabled=True,
    )
    handler.attach_storage_capacity(capacity)
    source = handler.save_temp_file("call.mp3", b"ID3payload")
    reservation = capacity.reserve_upload()
    reservation.complete_spool()
    reservation.claim_persistent()
    reservation.complete_custom_temp()
    orphan = handler.storage_dir / "orphan.mp3"

    def fail_after_publish(*args: object, **kwargs: object) -> Path:
        callback = kwargs.get("on_destination_reserved")
        if callable(callback):
            callback(str(orphan))
        orphan.write_bytes(b"ID3payload")
        raise OSError("simulated post-publish failure")

    monkeypatch.setattr(handler, "_store_file_unaccounted", fail_after_publish)
    with pytest.raises(OSError, match="post-publish"):
        handler.store_file(
            source,
            "1",
            datetime.now(UTC),
            on_destination_reserved=lambda _path: None,
            capacity_reservation=reservation,
        )
    reservation.release()
    assert not capacity.snapshot.scan_certain
    assert capacity.reconcile()
    assert capacity.snapshot.stored_bytes == len(b"ID3payload")
    handler.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symbolic-link semantics")
def test_cross_device_storage_component_is_rejected_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    source = handler.save_temp_file("call.mp3", b"ID3payload")
    actual_fstat = os.fstat
    directory_calls = 0

    def cross_device_fstat(descriptor: int) -> object:
        nonlocal directory_calls
        result = actual_fstat(descriptor)
        if stat.S_ISDIR(result.st_mode):
            directory_calls += 1
            if directory_calls == 2:
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev + 1,
                    st_ino=result.st_ino,
                    st_uid=result.st_uid,
                )
        return result

    monkeypatch.setattr("src.utils.file_handler.os.fstat", cross_device_fstat)
    staged: list[str] = []
    with pytest.raises(OSError, match="root filesystem"):
        handler.store_file(
            source,
            "1",
            datetime(2025, 1, 2, tzinfo=UTC),
            on_destination_reserved=staged.append,
        )

    assert not staged
    assert source.is_file()
    assert not [path for path in handler.storage_dir.rglob("*") if path.is_file()]
    handler.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX device identities")
def test_cross_device_component_is_refused_for_read_delete_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    nested = handler.storage_dir / "nested"
    nested.mkdir()
    stored = nested / "call.mp3"
    stored.write_bytes(b"audio")
    nested_identity = nested.stat().st_ino
    actual_fstat = os.fstat

    def cross_device_fstat(descriptor: int) -> object:
        result = actual_fstat(descriptor)
        if result.st_ino == nested_identity:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev + 1,
                st_ino=result.st_ino,
                st_uid=result.st_uid,
            )
        return result

    monkeypatch.setattr("src.utils.file_handler.os.fstat", cross_device_fstat)
    assert handler.delete_file(str(stored)).status == "refused"
    assert stored.is_file()
    with pytest.raises(PermissionError, match="filesystem boundary"):
        with handler.open_stored_file(str(stored)):
            pass
    assert handler.remove_empty_directories([str(stored)]) == 0
    assert nested.is_dir()
    handler.close()


def test_path_archive_scanner_uses_complete_identity_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model Windows, where cached DirEntry identities are unavailable."""
    capacity = _capacity(tmp_path)
    nested = capacity.storage_directory / "nested"
    nested.mkdir()
    (nested / "call.mp3").write_bytes(b"audio")
    actual_scandir = os.scandir

    class WindowsLikeEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self.path = entry.path

        @staticmethod
        def stat(*, follow_symlinks: bool) -> object:
            raise AssertionError("path scanner must fetch complete metadata")

    class WindowsLikeScan:
        def __init__(self, path: object) -> None:
            self._entries = actual_scandir(path)

        def __iter__(self) -> WindowsLikeScan:
            return self

        def __next__(self) -> WindowsLikeEntry:
            return WindowsLikeEntry(next(self._entries))

        def close(self) -> None:
            self._entries.close()

    monkeypatch.setattr(
        "src.utils.storage_quota.os.scandir", lambda path: WindowsLikeScan(path)
    )
    events = list(capacity._archive_scan_events_by_path())

    assert sum(size for size, _files, _certain in events) == len(b"audio")
    assert sum(files for _size, files, _certain in events) == 1
    assert all(certain for _size, _files, certain in events)
    capacity.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor scan")
def test_cross_device_archive_directory_makes_reconciliation_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    root_status = capacity.storage_directory.stat()

    class CrossDeviceEntry:
        name = "mounted"
        path = str(capacity.storage_directory / name)

        @staticmethod
        def stat(*, follow_symlinks: bool) -> object:
            assert not follow_symlinks
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=root_status.st_dev + 1,
                st_ino=root_status.st_ino + 1,
                st_size=0,
            )

    class OneEntryScan:
        def __init__(self) -> None:
            self._entries = iter((CrossDeviceEntry(),))

        def __iter__(self) -> OneEntryScan:
            return self

        def __next__(self) -> CrossDeviceEntry:
            return next(self._entries)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "src.utils.storage_quota.os.scandir", lambda _descriptor: OneEntryScan()
    )
    assert not capacity.reconcile()
    assert not capacity.snapshot.scan_certain


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor scan")
def test_same_device_directory_cycle_fails_closed_without_recursing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    root_status = capacity.storage_directory.stat()
    scans = 0

    class RootAliasEntry:
        name = "."
        path = str(capacity.storage_directory)

        @staticmethod
        def stat(*, follow_symlinks: bool) -> object:
            assert not follow_symlinks
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=root_status.st_dev,
                st_ino=root_status.st_ino,
                st_size=0,
            )

    class RootAliasScan:
        def __init__(self) -> None:
            nonlocal scans
            scans += 1
            self._entries = iter((RootAliasEntry(),))

        def __iter__(self) -> RootAliasScan:
            return self

        def __next__(self) -> RootAliasEntry:
            return next(self._entries)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "src.utils.storage_quota.os.scandir", lambda _descriptor: RootAliasScan()
    )
    assert not capacity.reconcile()
    assert scans == 1
    assert not capacity.snapshot.scan_certain


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor scan")
def test_repeated_directory_identity_fails_closed_without_double_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    child = capacity.storage_directory / "child"
    child.mkdir()
    child_status = child.stat()
    actual_scandir = os.scandir
    actual_fstat = os.fstat
    root_scans = 0
    child_scans = 0

    class DuplicateChildEntry:
        name = "child"
        path = str(child)

        @staticmethod
        def stat(*, follow_symlinks: bool) -> os.stat_result:
            assert not follow_symlinks
            return child_status

    class DuplicateChildScan:
        def __init__(self) -> None:
            self._entries = iter((DuplicateChildEntry(), DuplicateChildEntry()))

        def __iter__(self) -> DuplicateChildScan:
            return self

        def __next__(self) -> DuplicateChildEntry:
            return next(self._entries)

        def close(self) -> None:
            pass

    def duplicate_scandir(descriptor: int) -> Any:
        nonlocal root_scans, child_scans
        identity = actual_fstat(descriptor).st_ino
        if identity == capacity._storage_root_identity[1]:
            root_scans += 1
            return DuplicateChildScan()
        if identity == child_status.st_ino:
            child_scans += 1
        return actual_scandir(descriptor)

    monkeypatch.setattr("src.utils.storage_quota.os.scandir", duplicate_scandir)
    assert not capacity.reconcile()
    assert not capacity.snapshot.scan_certain
    assert root_scans == 1
    assert child_scans == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor scan")
def test_close_cooperatively_stops_active_archive_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = _capacity(tmp_path)
    root_status = capacity.storage_directory.stat()
    entered = threading.Event()

    class EndlessFileEntry:
        name = "call.mp3"
        path = str(capacity.storage_directory / name)

        @staticmethod
        def stat(*, follow_symlinks: bool) -> object:
            assert not follow_symlinks
            entered.set()
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_dev=root_status.st_dev,
                st_ino=root_status.st_ino + 1,
                st_size=1,
            )

    class EndlessScan:
        def __iter__(self) -> EndlessScan:
            return self

        def __next__(self) -> EndlessFileEntry:
            return EndlessFileEntry()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "src.utils.storage_quota.os.scandir", lambda _descriptor: EndlessScan()
    )
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(capacity.reconcile()))
    worker.start()
    assert entered.wait(2)
    capacity.close()
    worker.join(2)

    assert not worker.is_alive()
    assert results == [False]
    with pytest.raises(CapacityUnavailable, match="closed"):
        capacity.reserve_upload()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symbolic-link semantics")
def test_unconfigured_alias_cannot_redirect_delete_or_pruning(
    tmp_path: Path,
) -> None:
    handler = FileHandler(
        str(tmp_path / "storage"),
        str(tmp_path / "temp"),
        min_file_size_kb=0,
    )
    directory = handler.storage_dir / "nested"
    directory.mkdir()
    stored = directory / "call.mp3"
    stored.write_bytes(b"audio")
    attacker_alias = tmp_path / "attacker-alias"
    attacker_alias.symlink_to(handler.storage_dir, target_is_directory=True)
    aliased_path = attacker_alias / "nested" / stored.name

    assert handler.delete_file(str(aliased_path)).status == "refused"
    assert stored.is_file()
    assert handler.remove_empty_directories([str(aliased_path)]) == 0
    assert directory.is_dir()
    handler.close()
