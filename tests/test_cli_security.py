"""Security regressions for CLI-generated files."""

import os
import stat
from pathlib import Path

import pytest

from cli import csv_safe_value, private_text_output


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", "\t=1+1", "  =1+1"],
)
def test_csv_formula_prefixes_are_neutralized(value: str) -> None:
    assert csv_safe_value(value) == f"'{value}"


def test_csv_safe_value_preserves_non_formulas_and_numbers() -> None:
    assert csv_safe_value("Dispatch") == "Dispatch"
    assert csv_safe_value(-42) == -42


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and mode semantics")
def test_private_output_replaces_symlink_without_following_it(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    output = tmp_path / "calls.csv"
    output.symlink_to(target)

    with private_text_output(output) as stream:
        stream.write("safe export")

    assert target.read_text(encoding="utf-8") == "do not overwrite"
    assert output.read_text(encoding="utf-8") == "safe export"
    assert not output.is_symlink()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_private_output_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(capture, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        with private_text_output(redirected / "calls.csv") as stream:
            stream.write("must not escape")

    assert not (capture / "calls.csv").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_private_output_rejects_world_writable_parent_component(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    private_leaf = shared / "private"
    private_leaf.mkdir(mode=0o700)

    with pytest.raises(PermissionError, match="group/world writable"):
        with private_text_output(private_leaf / "calls.csv") as stream:
            stream.write("must not escape")


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd semantics")
def test_private_output_stays_in_pinned_parent_during_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "export"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "moved-export"
    capture = tmp_path / "capture"
    capture.mkdir()
    actual_replace = os.replace

    def swap_then_replace(source, destination, *args, **kwargs):
        parent.rename(moved_parent)
        parent.symlink_to(capture, target_is_directory=True)
        return actual_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", swap_then_replace)

    with private_text_output(parent / "calls.csv") as stream:
        stream.write("pinned export")

    assert (moved_parent / "calls.csv").read_text() == "pinned export"
    assert not (capture / "calls.csv").exists()
