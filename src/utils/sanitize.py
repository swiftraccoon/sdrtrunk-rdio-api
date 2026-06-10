"""Filename sanitization for client-supplied names."""

import re


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename to remove potentially dangerous characters.

    Args:
        filename: The filename to sanitize

    Returns:
        Sanitized filename
    """
    # Remove any path components (both POSIX and Windows separators)
    filename = filename.split("/")[-1].split("\\")[-1]

    # Remove control characters and non-printable characters
    filename = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", filename)

    # Replace potentially dangerous characters
    filename = re.sub(r'[<>:"|?*]', "_", filename)

    # Limit length
    max_length = 255
    if len(filename) > max_length:
        # Preserve extension if possible
        parts = filename.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) < 10:
            base = parts[0][: max_length - len(parts[1]) - 1]
            filename = f"{base}.{parts[1]}"
        else:
            filename = filename[:max_length]

    # Ensure filename is not empty
    if not filename:
        filename = "unnamed_file"

    return filename
