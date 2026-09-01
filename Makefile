.PHONY: help install test test-coverage test-integration test-performance lint format sort typecheck check security benchmark all clean pre-commit

# Local automation should exercise the reviewed dependency graph too. Run
# `uv lock --upgrade` explicitly when an update is intended.
UV_LOCKED ?= 1
export UV_LOCKED
UV_NO_BUILD_ISOLATION ?= 1
export UV_NO_BUILD_ISOLATION

help:
	@echo "Available commands:"
	@echo "  install         Install dependencies with uv"
	@echo "  test            Run tests with pytest"
	@echo "  test-coverage   Run tests with coverage report"
	@echo "  test-integration Run integration tests"
	@echo "  test-performance Run performance benchmarks"
	@echo "  lint            Run ruff linter"
	@echo "  format          Format code with black and isort"
	@echo "  sort            Sort imports with isort"
	@echo "  typecheck       Run mypy type checker"
	@echo "  check           Run all checks (lint, format check, sort check, typecheck)"
	@echo "  security        Run security scans (bandit, pip-audit)"
	@echo "  benchmark       Run performance benchmarks"
	@echo "  all             Run format, sort, lint, typecheck, and test"
	@echo "  clean           Clean up cache files"
	@echo "  pre-commit      Install pre-commit hooks"

install:
	uv sync --locked --only-group build --no-install-project --no-build
	uv sync --locked --no-build-isolation

test: install
	uv run --locked pytest -v

test-coverage: install
	uv run --locked pytest --cov=src --cov-report=html --cov-report=term

test-integration: install
	uv run --locked pytest tests/test_integration.py -v

test-performance: install
	uv run --locked pytest tests/test_performance.py -v --benchmark-only

lint: install
	uv run --locked ruff check .

format: install
	uv run --locked black .
	uv run --locked isort .

sort: install
	uv run --locked isort .

typecheck: install
	uv run --locked mypy src

check: install
	uv run --locked black --check .
	uv run --locked isort --check-only .
	uv run --locked ruff check .
	uv run --locked mypy src

security: install
	uv run --locked bandit -r src/ cli.py scripts/ -ll
	uv export --quiet --locked --no-dev --no-emit-project --format requirements-txt --output-file .audit-runtime-requirements.txt
	uv run --locked pip-audit --disable-pip --require-hashes --requirement .audit-runtime-requirements.txt
	uv export --quiet --locked --all-groups --no-emit-project --format requirements-txt --output-file .audit-development-requirements.txt
	uv run --locked pip-audit --disable-pip --require-hashes --requirement .audit-development-requirements.txt
	python -c "from pathlib import Path; [Path(name).unlink(missing_ok=True) for name in ('.audit-runtime-requirements.txt', '.audit-development-requirements.txt')]"

benchmark: install
	uv run --locked pytest tests/test_performance.py -v --benchmark-only

pre-commit: install
	uv run --locked pre-commit install

all: format lint typecheck test

clean:
	python scripts/clean.py
	python -c "from pathlib import Path; [Path(name).unlink(missing_ok=True) for name in ('.audit-runtime-requirements.txt', '.audit-development-requirements.txt')]"
