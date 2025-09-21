#!/usr/bin/env python3
"""Cross-platform clean script for removing build artifacts and cache files."""

import shutil
import sys
from pathlib import Path


def clean_project():
    """Clean up cache files and build artifacts in a cross-platform way."""
    # Get project root (parent of scripts directory)
    project_root = Path(__file__).parent.parent

    # Patterns to clean
    patterns_to_remove = [
        # Python cache
        "**/__pycache__",
        "**/*.pyc",
        "**/*.pyo",
        # Test and coverage
        ".pytest_cache",
        ".coverage",
        "htmlcov",
        ".benchmarks",
        # Type checking and linting
        ".mypy_cache",
        ".ruff_cache",
        # Build artifacts
        "build",
        "dist",
        "*.egg-info",
    ]

    removed_items = []

    for pattern in patterns_to_remove:
        if "**/" in pattern:
            # Handle recursive patterns
            for path in project_root.rglob(pattern.replace("**/", "")):
                if path.exists():
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    removed_items.append(str(path.relative_to(project_root)))
        else:
            # Handle single patterns
            path = project_root / pattern
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                removed_items.append(pattern)

    if removed_items:
        print("Cleaned up the following items:")
        for item in removed_items:
            print(f"  - {item}")
        print(f"\nTotal: {len(removed_items)} items removed")
    else:
        print("No items to clean up - project is already clean")

    return 0


if __name__ == "__main__":
    sys.exit(clean_project())
