.PHONY: help install test test-coverage test-integration test-performance lint format sort typecheck check security benchmark all clean pre-commit

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
	@echo "  security        Run security scans (bandit, safety)"
	@echo "  benchmark       Run performance benchmarks"
	@echo "  all             Run format, sort, lint, typecheck, and test"
	@echo "  clean           Clean up cache files"
	@echo "  pre-commit      Install pre-commit hooks"

install:
	uv sync

test:
	uv run pytest -v

test-coverage:
	uv run pytest --cov=src --cov-report=html --cov-report=term

test-integration:
	uv run pytest tests/test_integration.py -v

test-performance:
	uv run pytest tests/test_performance.py -v --benchmark-only

lint:
	uv run ruff check .

format:
	uv run black .
	uv run isort .

sort:
	uv run isort .

typecheck:
	uv run mypy src

check:
	uv run black --check .
	uv run isort --check-only .
	uv run ruff check .
	uv run mypy src

security:
	uv run bandit -r src/ -ll
	uv run safety check

benchmark:
	uv run pytest tests/test_performance.py -v --benchmark-only

pre-commit:
	pip install pre-commit
	pre-commit install

all: format lint typecheck test

clean:
	python scripts/clean.py