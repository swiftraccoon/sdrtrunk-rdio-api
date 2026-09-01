"""Security regressions for SQLite files and backups."""

import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Config
from src.database.connection import DatabaseInUseError, DatabaseManager
from src.database.operations import DatabaseOperations
from src.filesystem_security import (
    _serialized_acl_is_deny_only,
    _win32_path_is_dangerous,
    durable_fsync,
    path_is_anchored_windows_relative,
    path_uses_dangerous_windows_namespace,
    paths_overlap,
    paths_refer_to_same_entry,
    reject_insecure_extended_acl,
)
from src.models.api_models import RdioScannerUpload
from src.utils.file_handler import FileHandler, _relative_under_validated_root


def _modeled_index_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    same_device: bool,
    database_available_bytes: int,
    database_available_inodes: int,
    scratch_available_bytes: int,
    scratch_available_inodes: int,
    schema_minimum_free_bytes: int,
    schema_minimum_free_inodes: int,
    database_size: int = 0,
    wal_size: int = 0,
) -> tuple[DatabaseManager, list[Path]]:
    """Build a manager whose schema preflight sees modeled filesystems."""

    import src.database.connection as connection_module

    database_path = tmp_path / "state" / "calls.db"
    database_path.parent.mkdir(mode=0o700)
    database_path.touch(mode=0o600)
    os.truncate(database_path, database_size)
    if wal_size:
        wal_path = database_path.with_name(f"{database_path.name}-wal")
        wal_path.touch(mode=0o600)
        os.truncate(wal_path, wal_size)
    scratch_path = tmp_path / "sqlite-scratch"
    scratch_path.mkdir(mode=0o700)
    python_temp_path = tmp_path / "python-temp"
    python_temp_path.mkdir(mode=0o700)
    monkeypatch.setenv("SQLITE_TMPDIR", str(scratch_path))
    monkeypatch.setenv("TMPDIR", str(python_temp_path))

    database_device = 101
    scratch_device = database_device if same_device else 202
    actual_stat = connection_module.os.stat

    def modeled_stat(path: str | Path, *args: Any, **kwargs: Any) -> os.stat_result:
        result = actual_stat(path, *args, **kwargs)
        values = list(result)
        candidate = Path(path)
        if candidate in {database_path, database_path.parent}:
            values[2] = database_device
        elif candidate == scratch_path:
            values[2] = scratch_device
        return os.stat_result(values)

    observed_statvfs_paths: list[Path] = []

    def modeled_statvfs(path: str | Path) -> SimpleNamespace:
        candidate = Path(path)
        observed_statvfs_paths.append(candidate)
        if candidate == database_path.parent or same_device:
            available_bytes = database_available_bytes
            available_inodes = database_available_inodes
        else:
            assert candidate == scratch_path
            available_bytes = scratch_available_bytes
            available_inodes = scratch_available_inodes
        return SimpleNamespace(
            f_bavail=available_bytes,
            f_frsize=1,
            f_bsize=1,
            f_files=1,
            f_favail=available_inodes,
        )

    monkeypatch.setattr(connection_module.os, "stat", modeled_stat)
    monkeypatch.setattr(connection_module.os, "statvfs", modeled_statvfs)

    manager = object.__new__(DatabaseManager)
    manager.database_path = database_path
    manager.schema_minimum_free_bytes = schema_minimum_free_bytes
    manager.schema_minimum_free_inodes = schema_minimum_free_inodes
    return manager, observed_statvfs_paths


def test_sqlite_security_and_durability_pragmas_are_enabled(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / "state" / "calls.db"))
    try:
        with manager.engine.connect() as connection:
            secure_delete = connection.exec_driver_sql(
                "PRAGMA secure_delete"
            ).scalar_one()
            foreign_keys = connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar_one()
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
            fullfsync = connection.exec_driver_sql("PRAGMA fullfsync").scalar_one()
            checkpoint_fullfsync = connection.exec_driver_sql(
                "PRAGMA checkpoint_fullfsync"
            ).scalar_one()
            temp_store = connection.exec_driver_sql("PRAGMA temp_store").scalar_one()
        assert secure_delete == 1
        assert foreign_keys == 1
        assert synchronous == 2  # SQLITE_SYNC_FULL
        assert fullfsync == 1
        assert checkpoint_fullfsync == 1
        assert temp_store == 1  # FILE, so large upgrade sorts do not exhaust heap
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin full-fsync semantics")
def test_durable_fsync_flushes_the_device_cache_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    import src.filesystem_security as filesystem_security

    fsync_calls: list[int] = []
    full_fsync_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        filesystem_security.os,
        "fsync",
        lambda descriptor: fsync_calls.append(descriptor),
    )
    monkeypatch.setattr(
        fcntl,
        "fcntl",
        lambda descriptor, command: full_fsync_calls.append((descriptor, command)),
    )

    durable_fsync(123)

    assert fsync_calls == [123]
    assert full_fsync_calls == [(123, getattr(fcntl, "F_FULLFSYNC", 51))]


def test_acl_allow_action_cannot_hide_behind_a_principal_named_deny() -> None:
    deny_only = b"!#acl 1\ngroup:uuid:everyone:12:deny:delete\n"
    disguised_allow = b"!#acl 1\ngroup:uuid:deny:12:allow:read\n"

    assert _serialized_acl_is_deny_only(deny_only)
    assert not _serialized_acl_is_deny_only(disguised_allow)


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd cleanup semantics")
def test_rejected_temp_file_policy_closes_and_unlinks_new_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.utils.file_handler as file_handler_module

    root = tmp_path / "temp"
    root.mkdir(mode=0o700)
    root_status = root.stat()
    rejected_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    actual_close = os.close

    def reject(descriptor: int, **_kwargs: object) -> None:
        rejected_descriptors.append(descriptor)
        raise PermissionError("simulated ACL rejection")

    def observed_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        actual_close(descriptor)

    monkeypatch.setattr(file_handler_module, "reject_insecure_extended_acl", reject)
    monkeypatch.setattr(file_handler_module.os, "close", observed_close)

    with pytest.raises(PermissionError, match="simulated ACL rejection"):
        file_handler_module._open_private_temp_file(
            root,
            (root_status.st_dev, root_status.st_ino),
            ".mp3",
        )

    assert rejected_descriptors
    assert rejected_descriptors[0] in closed_descriptors
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd cleanup semantics")
def test_rejected_storage_component_policy_closes_child_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.utils.file_handler as file_handler_module

    root = tmp_path / "storage"
    root.mkdir(mode=0o700)
    root_status = root.stat()
    rejected_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    actual_close = os.close

    def reject(descriptor: int, **_kwargs: object) -> None:
        rejected_descriptors.append(descriptor)
        raise PermissionError("simulated ACL rejection")

    def observed_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        actual_close(descriptor)

    monkeypatch.setattr(file_handler_module, "reject_insecure_extended_acl", reject)
    monkeypatch.setattr(file_handler_module.os, "close", observed_close)

    with pytest.raises(PermissionError, match="simulated ACL rejection"):
        file_handler_module._mkdir_private_tree(
            root,
            Path("2026"),
            (root_status.st_dev, root_status.st_ino),
        )

    assert rejected_descriptors
    assert rejected_descriptors[0] in closed_descriptors


def test_disabling_wal_transitions_persistent_database_to_delete_mode(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "calls.db"
    wal_manager = DatabaseManager(str(database_path), enable_wal=True)
    wal_manager.close()

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    delete_manager = DatabaseManager(str(database_path), enable_wal=False)
    try:
        with delete_manager.engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        assert mode == "delete"
        assert delete_manager.checkpoint(truncate=True)
    finally:
        delete_manager.close()


def test_read_only_manager_neither_changes_journal_mode_nor_repairs_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "calls.db"
    with DatabaseManager(str(database_path), enable_wal=True):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX idx_audio_file_path")

    with DatabaseManager(
        str(database_path), enable_wal=False, read_only=True
    ) as read_only_manager:
        with read_only_manager.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM radio_calls"
                ).scalar_one()
                == 0
            )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(radio_calls)")
        }
    assert "idx_audio_file_path" not in indexes


def test_read_only_manager_fails_without_creating_a_missing_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "missing.db"
    database_path.parent.mkdir(mode=0o700)

    with pytest.raises(FileNotFoundError):
        DatabaseManager(str(database_path), read_only=True)

    assert not database_path.exists()


def test_missing_required_index_fails_before_unbounded_low_space_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    database_path = tmp_path / "state" / "calls.db"
    with DatabaseManager(str(database_path)):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX idx_audio_file_path")

    if hasattr(connection_module.os, "statvfs"):
        actual_statvfs = connection_module.os.statvfs

        def exhausted_statvfs(path: str | Path):
            status = actual_statvfs(path)
            values = list(status)
            values[4] = 0  # f_bavail
            return os.statvfs_result(values)

        monkeypatch.setattr(connection_module.os, "statvfs", exhausted_statvfs)
    else:
        monkeypatch.setattr(
            connection_module.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=0),
        )

    with pytest.raises(RuntimeError, match="headroom for schema upgrade"):
        DatabaseManager(str(database_path))


@pytest.mark.skipif(os.name != "posix", reason="SQLite Unix temp search semantics")
def test_sqlite_scratch_directory_precedes_python_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    sqlite_scratch = tmp_path / "sqlite-scratch"
    sqlite_scratch.mkdir(mode=0o700)
    python_temp = tmp_path / "python-temp"
    python_temp.mkdir(mode=0o700)
    monkeypatch.setenv("SQLITE_TMPDIR", str(sqlite_scratch))
    monkeypatch.setenv("TMPDIR", str(python_temp))

    assert connection_module._sqlite_temporary_directory() == sqlite_scratch


@pytest.mark.skipif(os.name != "posix", reason="SQLite Unix temp search semantics")
def test_sqlite_scratch_directory_resolves_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    actual_scratch = tmp_path / "actual-scratch"
    actual_scratch.mkdir(mode=0o700)
    scratch_alias = tmp_path / "scratch-alias"
    scratch_alias.symlink_to(actual_scratch, target_is_directory=True)
    monkeypatch.setenv("SQLITE_TMPDIR", str(scratch_alias))

    assert connection_module._sqlite_temporary_directory() == actual_scratch


@pytest.mark.skipif(os.name != "posix", reason="SQLite Unix temp search semantics")
def test_invalid_explicit_sqlite_scratch_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    python_temp = tmp_path / "python-temp"
    python_temp.mkdir(mode=0o700)
    monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_path / "missing"))
    monkeypatch.setenv("TMPDIR", str(python_temp))

    with pytest.raises(RuntimeError, match="SQLITE_TMPDIR"):
        connection_module._sqlite_temporary_directory()


def test_non_posix_sqlite_scratch_uses_win32_resolver_not_python_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    win32_temp = tmp_path / "win32-temp"
    python_temp = tmp_path / "python-temp"
    monkeypatch.setenv("TMPDIR", str(python_temp))
    monkeypatch.setattr(connection_module, "_IS_POSIX", False)
    monkeypatch.setattr(
        connection_module,
        "_windows_sqlite_temporary_directory",
        lambda: win32_temp,
    )

    assert connection_module._sqlite_temporary_directory() == win32_temp


@pytest.mark.skipif(os.name == "posix", reason="Win32 GetTempPath semantics")
def test_win32_sqlite_scratch_resolver_returns_usable_directory() -> None:
    import src.database.connection as connection_module

    scratch = connection_module._windows_sqlite_temporary_directory()

    assert scratch.is_absolute()
    assert scratch.is_dir()
    assert os.access(scratch, os.W_OK)


@pytest.mark.skipif(os.name != "posix", reason="statvfs filesystem model")
def test_separate_bounded_sqlite_scratch_uses_its_own_safety_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    scratch_bytes = connection_module._MINIMUM_INDEX_SCRATCH_BYTES
    state_reserve = 288 * 1024 * 1024
    state_inodes = 1024
    manager, observed_paths = _modeled_index_preflight(
        tmp_path,
        monkeypatch,
        same_device=False,
        database_available_bytes=(
            scratch_bytes * connection_module._INDEX_STATE_COPY_MULTIPLIER
            + state_reserve
        ),
        database_available_inodes=(
            connection_module._INDEX_STATE_INODES + state_inodes
        ),
        scratch_available_bytes=(
            scratch_bytes + connection_module._INDEX_SCRATCH_FREE_BYTES
        ),
        scratch_available_inodes=(
            connection_module._INDEX_SCRATCH_INODES
            + connection_module._INDEX_SCRATCH_FREE_INODES
        ),
        schema_minimum_free_bytes=state_reserve,
        schema_minimum_free_inodes=state_inodes,
    )

    manager._preflight_index_creation("modeled_index")

    assert tmp_path / "state" in observed_paths
    assert tmp_path / "sqlite-scratch" in observed_paths
    assert tmp_path / "python-temp" not in observed_paths


@pytest.mark.skipif(os.name != "posix", reason="statvfs filesystem model")
def test_separate_sqlite_scratch_rejects_one_byte_below_safety_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    scratch_bytes = connection_module._MINIMUM_INDEX_SCRATCH_BYTES
    state_reserve = 288 * 1024 * 1024
    manager, _ = _modeled_index_preflight(
        tmp_path,
        monkeypatch,
        same_device=False,
        database_available_bytes=(
            scratch_bytes * connection_module._INDEX_STATE_COPY_MULTIPLIER
            + state_reserve
        ),
        database_available_inodes=1032,
        scratch_available_bytes=(
            scratch_bytes + connection_module._INDEX_SCRATCH_FREE_BYTES - 1
        ),
        scratch_available_inodes=16,
        schema_minimum_free_bytes=state_reserve,
        schema_minimum_free_inodes=1024,
    )

    with pytest.raises(RuntimeError, match="byte headroom for schema upgrade"):
        manager._preflight_index_creation("modeled_index")


@pytest.mark.skipif(os.name != "posix", reason="statvfs filesystem model")
def test_shared_state_and_sqlite_scratch_requirements_are_aggregated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    scratch_bytes = connection_module._MINIMUM_INDEX_SCRATCH_BYTES
    state_reserve = 288 * 1024 * 1024
    state_inodes = 1024
    manager, observed_paths = _modeled_index_preflight(
        tmp_path,
        monkeypatch,
        same_device=True,
        database_available_bytes=(scratch_bytes * 3 + state_reserve),
        database_available_inodes=(
            connection_module._INDEX_STATE_INODES
            + connection_module._INDEX_SCRATCH_INODES
            + state_inodes
        ),
        scratch_available_bytes=0,
        scratch_available_inodes=0,
        schema_minimum_free_bytes=state_reserve,
        schema_minimum_free_inodes=state_inodes,
    )

    manager._preflight_index_creation("modeled_index")

    assert observed_paths == [tmp_path / "state"]


@pytest.mark.skipif(os.name != "posix", reason="statvfs filesystem model")
def test_schema_scratch_estimate_includes_uncheckpointed_wal_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.database.connection as connection_module

    minimum_scratch = connection_module._MINIMUM_INDEX_SCRATCH_BYTES
    state_reserve = 288 * 1024 * 1024
    manager, _ = _modeled_index_preflight(
        tmp_path,
        monkeypatch,
        same_device=False,
        # These limits would pass if only the 24 MiB main file were inspected.
        database_available_bytes=(minimum_scratch * 2 + state_reserve),
        database_available_inodes=1032,
        scratch_available_bytes=(
            minimum_scratch + connection_module._INDEX_SCRATCH_FREE_BYTES
        ),
        scratch_available_inodes=16,
        schema_minimum_free_bytes=state_reserve,
        schema_minimum_free_inodes=1024,
        database_size=24 * 1024 * 1024,
        wal_size=24 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="byte headroom for schema upgrade"):
        manager._preflight_index_creation("modeled_index")


@pytest.mark.skipif(os.name != "posix", reason="statvfs filesystem model")
@pytest.mark.parametrize("exhausted_filesystem", ["state", "scratch"])
def test_separate_schema_filesystems_preserve_their_inode_floors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exhausted_filesystem: str,
) -> None:
    import src.database.connection as connection_module

    scratch_bytes = connection_module._MINIMUM_INDEX_SCRATCH_BYTES
    state_reserve = 288 * 1024 * 1024
    state_inodes = 1024
    database_inodes = connection_module._INDEX_STATE_INODES + state_inodes
    scratch_inodes = (
        connection_module._INDEX_SCRATCH_INODES
        + connection_module._INDEX_SCRATCH_FREE_INODES
    )
    if exhausted_filesystem == "state":
        database_inodes -= 1
    else:
        scratch_inodes -= 1
    manager, _ = _modeled_index_preflight(
        tmp_path,
        monkeypatch,
        same_device=False,
        database_available_bytes=(
            scratch_bytes * connection_module._INDEX_STATE_COPY_MULTIPLIER
            + state_reserve
        ),
        database_available_inodes=database_inodes,
        scratch_available_bytes=(
            scratch_bytes + connection_module._INDEX_SCRATCH_FREE_BYTES
        ),
        scratch_available_inodes=scratch_inodes,
        schema_minimum_free_bytes=state_reserve,
        schema_minimum_free_inodes=state_inodes,
    )

    with pytest.raises(RuntimeError, match="inode headroom for schema upgrade"):
        manager._preflight_index_creation("modeled_index")


def test_partial_application_startup_releases_database_process_lock(
    test_config: Config, tmp_path: Path
) -> None:
    invalid_storage = tmp_path / "storage-is-a-file"
    invalid_storage.write_text("not a directory", encoding="utf-8")
    test_config.file_handling.storage.directory = str(invalid_storage)
    test_config.file_handling.temp_directory = str(tmp_path / "temp")
    test_config.database.path = str(tmp_path / "state" / "calls.db")
    app = create_app(override_config=test_config)

    with pytest.raises(OSError):
        with TestClient(app):
            pass

    with DatabaseManager(
        test_config.database,
        exclusive_process_lock=True,
    ):
        pass


def test_exclusive_database_process_lock_fails_closed_and_releases(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "calls.db"
    child_script = """
import sys
from src.database.connection import DatabaseInUseError, DatabaseManager

try:
    manager = DatabaseManager(sys.argv[1], exclusive_process_lock=True)
except DatabaseInUseError:
    raise SystemExit(23)
manager.close()
"""
    first = DatabaseManager(str(database_path), exclusive_process_lock=True)
    try:
        with pytest.raises(DatabaseInUseError, match="already in use"):
            DatabaseManager(str(database_path), exclusive_process_lock=True)
        blocked = subprocess.run(
            [sys.executable, "-c", child_script, str(database_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert blocked.returncode == 23, blocked.stderr
    finally:
        first.close()

    acquired = subprocess.run(
        [sys.executable, "-c", child_script, str(database_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert acquired.returncode == 0, acquired.stderr


@pytest.mark.skipif(os.name == "nt", reason="question marks are reserved on Win32")
def test_sqlite_structured_url_preserves_literal_query_character_in_path(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "calls?archive.db"
    with DatabaseManager(str(database_path)) as manager:
        with manager.engine.connect() as connection:
            opened_path = connection.exec_driver_sql("PRAGMA database_list").one()[2]

    assert Path(opened_path).resolve() == database_path.resolve()
    assert database_path.is_file()
    assert not (database_path.parent / "calls").exists()

    with DatabaseManager(str(database_path), read_only=True) as manager:
        with manager.engine.connect() as connection:
            read_only_path = connection.exec_driver_sql("PRAGMA database_list").one()[2]
    assert Path(read_only_path).resolve() == database_path.resolve()


def test_file_handler_rejects_casefold_aliases_for_storage_and_temp(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        FileHandler(
            storage_directory=str(tmp_path / "Archive"),
            temp_directory=str(tmp_path / "archive"),
        )


def test_legacy_absolute_path_accepts_only_same_inode_root_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_root = tmp_path / "archive"
    configured_root = tmp_path / "archive"
    alias_root = tmp_path / "ARCHIVE"
    canonical_root.mkdir()
    canonical_status = canonical_root.stat()
    actual_stat = os.stat

    def aliased_stat(path: os.PathLike[str] | str, *, follow_symlinks: bool = True):
        if Path(path) == alias_root:
            return canonical_status
        return actual_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", aliased_stat)
    relative = _relative_under_validated_root(
        str(alias_root / "old.mp3"), canonical_root, configured_root
    )

    assert relative == Path("old.mp3")


@pytest.mark.skipif(os.name == "nt", reason="requires a case-sensitive filesystem")
def test_legacy_absolute_path_rejects_distinct_case_sensitive_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_root = tmp_path / "archive"
    distinct_root = tmp_path / "ARCHIVE"
    canonical_root.mkdir()
    canonical_status = canonical_root.stat()
    distinct_values = list(canonical_status)
    distinct_values[1] = canonical_status.st_ino + 1
    distinct_status = os.stat_result(distinct_values)
    actual_stat = os.stat

    def distinct_stat(path: os.PathLike[str] | str, *, follow_symlinks: bool = True):
        if Path(path) == distinct_root:
            return distinct_status
        return actual_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", distinct_stat)

    with pytest.raises(ValueError, match="outside"):
        _relative_under_validated_root(
            str(distinct_root / "old.mp3"), canonical_root, canonical_root
        )


def test_backup_rejects_casefold_alias_of_live_database(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "calls.db"
    manager = DatabaseManager(str(database_path))
    try:
        with pytest.raises(ValueError, match="must differ"):
            manager.backup(str(database_path.with_name("CALLS.DB")))
        assert manager.check_connection()
    finally:
        manager.close()


@pytest.mark.parametrize("suffix", [".", " ", ". "])
def test_filesystem_comparisons_reject_win32_trailing_name_aliases(
    tmp_path: Path, suffix: str
) -> None:
    protected_file = tmp_path / "calls.db-wal"
    protected_root = tmp_path / "archive"

    assert paths_refer_to_same_entry(Path(f"{protected_file}{suffix}"), protected_file)
    assert paths_overlap(Path(f"{protected_root}{suffix}"), protected_root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX alias semantics")
def test_missing_descendants_under_aliasing_existing_prefixes_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.filesystem_security as filesystem_security

    canonical = tmp_path / "canonical"
    alias = tmp_path / "alias"
    canonical.mkdir()
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(filesystem_security, "_resolved_path", lambda _path: None)

    aliased_file = alias / "not-created" / "deep" / "calls.db-wal"
    canonical_file = canonical / "not-created" / "deep" / "calls.db-wal"
    canonical_root = canonical / "not-created"

    assert paths_refer_to_same_entry(aliased_file, canonical_file)
    assert paths_overlap(aliased_file, canonical_file)
    assert paths_overlap(aliased_file, canonical_root)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\state\calls.db-wal::$DATA",
        r"C:\state\CALLS~1.DB",
        r"C:\state\NUL.txt",
        "C:\\state\\bad\x01name.db",
        r"C:\state\trailing.",
        "C:\\state\\trailing ",
        r"\\.\NUL",
        r"\\?\C:\state\calls.db",
        r"\\?\UNC\server\share\calls.db",
    ],
)
def test_win32_dangerous_path_policy_is_platform_independently_testable(
    path: str,
) -> None:
    assert _win32_path_is_dangerous(path)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\state\calls.db-wal",
        r"C:\state\report~final.csv",
        r"C:\state\COM10.txt",
        r"\\server\share\state.db",
    ],
)
def test_win32_dangerous_path_policy_preserves_unambiguous_names(path: str) -> None:
    assert not _win32_path_is_dangerous(path)


@pytest.mark.parametrize("path", [r"C:outside\file.mp3", r"\outside\file.mp3"])
def test_win32_anchored_relative_paths_are_rejected_from_storage_references(
    path: str,
) -> None:
    assert path_is_anchored_windows_relative(path)
    with pytest.raises(ValueError, match="relative"):
        _relative_under_validated_root(path, Path("archive"), Path("archive"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename semantics")
def test_win32_path_policy_does_not_reject_posix_names() -> None:
    assert not path_uses_dangerous_windows_namespace("report~1::$DATA. ")


@pytest.mark.skipif(os.name != "nt", reason="Win32 filename semantics")
def test_native_win32_path_policy_rejects_namespace_aliases() -> None:
    assert path_uses_dangerous_windows_namespace(r"C:\state\CALLS~1.DB")
    assert path_uses_dangerous_windows_namespace(r"C:\state\calls.db::$DATA")
    assert path_uses_dangerous_windows_namespace(r"C:\state\NUL.txt")


def test_backup_and_file_handler_apply_win32_path_policy_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.filesystem_security as filesystem_security

    manager = DatabaseManager(str(tmp_path / "state" / "calls.db"))
    monkeypatch.setattr(filesystem_security, "_WINDOWS_PATH_RULES_REQUIRED", True)
    backup = tmp_path / "backup" / "CALLS~1.DB"
    storage = tmp_path / "ARCHIV~1"
    temporary = tmp_path / "temporary"
    try:
        with pytest.raises(ValueError, match="ambiguous Windows"):
            manager.backup(str(backup))
        with pytest.raises(ValueError, match="ambiguous Windows"):
            FileHandler(str(storage), str(temporary))
    finally:
        manager.close()

    assert not backup.exists()
    assert not storage.exists()
    assert not temporary.exists()


def test_backup_rejects_sidecars_and_process_lock_destinations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "calls.db"
    manager = DatabaseManager(str(database_path), exclusive_process_lock=True)
    try:
        canonical_database = manager.database_path
        lock_path = canonical_database.parent / ".rdio-database.lock"
        lock_before = lock_path.stat()
        # msvcrt byte-range locks deny concurrent reads on Windows. The inode
        # and second-manager assertions below still verify the live lock there.
        lock_contents = None if os.name == "nt" else lock_path.read_bytes()
        protected_destinations = [
            Path(f"{canonical_database}-wal"),
            Path(f"{canonical_database}-shm"),
            Path(f"{canonical_database}-journal"),
            lock_path,
            canonical_database.with_name("CALLS.DB-WAL"),
            canonical_database.parent / ".RDIO-DATABASE.LOCK",
        ]

        for destination in protected_destinations:
            with pytest.raises(ValueError, match="live database state"):
                manager.backup(str(destination))

        lock_after = lock_path.stat()
        assert (lock_after.st_dev, lock_after.st_ino) == (
            lock_before.st_dev,
            lock_before.st_ino,
        )
        if lock_contents is not None:
            assert lock_path.read_bytes() == lock_contents
        with pytest.raises(DatabaseInUseError, match="already in use"):
            DatabaseManager(str(database_path), exclusive_process_lock=True)
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
def test_database_rejects_allow_acl_hidden_by_private_mode(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database_path = state / "calls.db"
    database_path.touch(mode=0o600)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(database_path)],
        check=True,
    )
    try:
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        with pytest.raises(PermissionError, match="extended ACL"):
            DatabaseManager(str(database_path))
    finally:
        subprocess.run(["chmod", "-N", str(database_path)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
def test_database_rejects_allow_acl_on_private_directory_ancestor(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(state)],
        check=True,
    )
    try:
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        with pytest.raises(PermissionError, match="extended ACL"):
            DatabaseManager(str(state / "calls.db"))
    finally:
        subprocess.run(["chmod", "-N", str(state)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
def test_standard_deny_only_acl_does_not_weaken_private_mode(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.touch(mode=0o600)
    subprocess.run(
        ["chmod", "+a", "everyone deny delete", str(protected)],
        check=True,
    )
    descriptor = os.open(protected, os.O_RDONLY)
    try:
        reject_insecure_extended_acl(descriptor, description="Private test file")
    finally:
        os.close(descriptor)
        subprocess.run(["chmod", "-N", str(protected)], check=True)


def test_online_backup_contains_committed_wal_data(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "calls.db"
    backup_path = tmp_path / "backup" / "calls.db"
    manager = DatabaseManager(str(database_path))
    try:
        operations = DatabaseOperations(manager)
        operations.save_radio_call(
            RdioScannerUpload(key="", system="1", dateTime=1_700_000_000)
        )

        manager.backup(str(backup_path))

        with sqlite3.connect(backup_path) as backup:
            count = backup.execute("SELECT count(*) FROM radio_calls").fetchone()[0]
        assert count == 1
    finally:
        manager.close()


def test_backup_compaction_removes_legacy_freelist_secret(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "calls.db"
    database_path.parent.mkdir(mode=0o700)
    # Keep the marker within one SQLite leaf page. A larger BLOB is split over
    # overflow pages, so its logical bytes are not guaranteed to be contiguous
    # in the database image even before compaction.
    marker = ("DELETED_SECRET_MARKER_" + "x" * 512).encode()
    with sqlite3.connect(database_path) as legacy:
        legacy.execute("PRAGMA secure_delete=OFF")
        legacy.execute("CREATE TABLE legacy_secret(value BLOB)")
        legacy.execute("INSERT INTO legacy_secret VALUES (?)", (marker,))
        legacy.commit()
        legacy.execute("DELETE FROM legacy_secret")
        legacy.commit()
    database_path.chmod(0o600)
    assert marker in database_path.read_bytes()

    manager = DatabaseManager(str(database_path))
    backup_path = tmp_path / "backup" / "calls.db"
    try:
        manager.backup(str(backup_path))
        assert marker not in backup_path.read_bytes()
        with sqlite3.connect(backup_path) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_backup_rejects_symbolic_link_destination(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / "state" / "calls.db"))
    target = tmp_path / "target.db"
    target.write_bytes(b"must remain untouched")
    link = tmp_path / "backup.db"
    link.symlink_to(target)
    try:
        with pytest.raises(ValueError, match="symbolic link"):
            manager.backup(str(link))
        assert target.read_bytes() == b"must remain untouched"
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership semantics")
def test_backup_rejects_private_leaf_beneath_writable_ancestor(
    tmp_path: Path,
) -> None:
    """A checked 0700 leaf is unsafe if another user can swap its ancestor."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    checked_leaf = shared / "checked-private-leaf"
    checked_leaf.mkdir(mode=0o700)
    capture = tmp_path / "attacker-capture"
    capture.mkdir()
    manager = DatabaseManager(str(tmp_path / "state" / "calls.db"))
    try:
        with pytest.raises(PermissionError, match="group/world writable"):
            manager.backup(str(checked_leaf / "snapshot.db"))
        assert not (capture / "snapshot.db").exists()
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_database_rejects_symbolic_link_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "target"
    target.write_bytes(b"must remain untouched")
    link = state / "calls.db"
    link.symlink_to(target)

    with pytest.raises(OSError):
        DatabaseManager(str(link))
    assert target.read_bytes() == b"must remain untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_database_rejects_world_writable_parent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o777)
    state.chmod(0o777)

    with pytest.raises(PermissionError, match="group/world writable"):
        DatabaseManager(str(state / "calls.db"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_database_and_backup_files_are_private(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "calls.db"
    backup_path = tmp_path / "backup" / "calls.db"
    manager = DatabaseManager(str(database_path))
    try:
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        assert manager.checkpoint(truncate=True)
        manager.backup(str(backup_path))
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    finally:
        manager.close()
