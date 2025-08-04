.PHONY: help install test lint format sort typecheck check all clean

help:
	@echo "Available commands:"
	@echo "  install    Install dependencies with uv"
	@echo "  test       Run tests with pytest"
	@echo "  lint       Run ruff linter"
	@echo "  format     Format code with black"
	@echo "  sort       Sort imports with isort"
	@echo "  typecheck  Run mypy type checker"
	@echo "  check      Run all checks (lint, format check, sort check, typecheck)"
	@echo "  all        Run format, sort, lint, typecheck, and test"
	@echo "  clean      Clean up cache files"

install:
	uv sync

test:
	uv run pytest -v

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

all: format lint typecheck test

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +