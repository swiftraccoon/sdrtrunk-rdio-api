"""Static deployment-hardening regressions."""

import re
import tomllib
from pathlib import Path

import yaml


def test_compose_bounds_container_log_storage() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    logging_config = compose["services"]["sdrtrunk-rdio-api"]["logging"]

    assert logging_config["driver"] == "local"
    assert logging_config["options"]["max-size"] == "10m"
    assert logging_config["options"]["max-file"] == "3"


def test_dependabot_batches_routine_updates() -> None:
    config_path = Path(__file__).resolve().parents[1] / ".github/dependabot.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    toolchain_group = config["multi-ecosystem-groups"]["monthly-toolchain"]
    assert toolchain_group["schedule"]["interval"] == "monthly"
    assert toolchain_group["open-pull-requests-limit"] == 1

    uv_update = updates["uv"]
    assert uv_update["schedule"]["interval"] == "monthly"
    assert uv_update["open-pull-requests-limit"] == 1
    assert uv_update["groups"]["uv-versions"]["patterns"] == ["*"]

    for ecosystem in ("github-actions", "pre-commit", "docker"):
        update = updates[ecosystem]
        assert update["patterns"] == ["*"]
        assert update["multi-ecosystem-group"] == "monthly-toolchain"
        assert "open-pull-requests-limit" not in update


def test_uv_version_matches_container_build_tool() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    required_version = pyproject["tool"]["uv"]["required-version"]
    assert required_version.startswith("==")

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(
        r"ghcr\.io/astral-sh/uv:([^@\s]+)@sha256:[0-9a-f]{64}", dockerfile
    )
    assert match is not None
    assert match.group(1) == required_version.removeprefix(
        "=="
    ), "Update tool.uv.required-version and the digest-pinned Docker uv stage together"
