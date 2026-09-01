"""Static deployment-hardening regressions."""

from pathlib import Path

import yaml


def test_compose_bounds_container_log_storage() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    logging_config = compose["services"]["sdrtrunk-rdio-api"]["logging"]

    assert logging_config["driver"] == "local"
    assert logging_config["options"]["max-size"] == "10m"
    assert logging_config["options"]["max-file"] == "3"
