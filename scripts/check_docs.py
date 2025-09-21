#!/usr/bin/env python3
"""Static documentation checks for CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def iter_markdown_files() -> Iterable[Path]:
    """Yield markdown files that should be validated."""
    yield REPO_ROOT / "README.md"
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.is_dir():
        yield from sorted(docs_dir.rglob("*.md"))


def is_local_href(href: str) -> bool:
    """Return True if the href points to a local file that we can validate."""
    href = href.strip()
    if not href or href.startswith(("http://", "https://", "mailto:")):
        return False
    if href.startswith("#"):
        return False
    return True


def resolve_target(markdown_file: Path, href: str) -> Path:
    """Resolve a local href relative to the current markdown file."""
    target, *_fragment = href.split("#", maxsplit=1)
    target_path = (markdown_file.parent / target).resolve()
    return target_path


def validate_markdown_file(markdown_file: Path) -> list[str]:
    """Validate links in a single markdown file."""
    text = markdown_file.read_text(encoding="utf-8")
    errors: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(text):
        href = match.group(2)
        if not is_local_href(href):
            continue
        target_path = resolve_target(markdown_file, href)
        if not target_path.exists():
            errors.append(
                f"{markdown_file.relative_to(REPO_ROOT)}: broken link to '{href}'"
            )
    return errors


def main() -> int:
    """Entry point for the documentation checks."""
    all_errors: list[str] = []
    for md_file in iter_markdown_files():
        all_errors.extend(validate_markdown_file(md_file))

    if all_errors:
        print("::error::Broken documentation links detected:")
        for error in all_errors:
            print(f"::error::{error}")
        return 1

    print("All documentation links resolved successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
