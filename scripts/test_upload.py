#!/usr/bin/env python3
"""Test script for sdrtrunk-rdio-api - simulates SDRTrunk upload."""

import argparse
import getpass
import os
import stat
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import requests


def test_upload(url: str, api_key: str, audio_file: str | None = None) -> None:
    """Test the RdioScanner upload endpoint.

    Args:
        url: API endpoint URL
        api_key: API key for authentication
        audio_file: Optional path to MP3 file
    """
    # Prepare form data
    form_data = {
        "key": api_key,
        "system": "1",
        "dateTime": str(int(time.time())),
        "frequency": "460000000",
        "talkgroup": "100",
        "source": "12345",
        "systemLabel": "Test System",
        "talkgroupLabel": "Test Talkgroup",
        "talkgroupGroup": "Test Group",
    }

    files: dict[str, Any] = {}

    # Add audio file if provided
    if audio_file and Path(audio_file).exists():
        files["audio"] = ("test.mp3", open(audio_file, "rb"), "audio/mpeg")
    else:
        # Create a minimal MP3 for testing (ID3 header + silence)
        mp3_data = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100
        files["audio"] = ("test.mp3", BytesIO(mp3_data), "audio/mpeg")

    print(f"Testing upload to: {url}")
    printable_form_data = {**form_data, "key": "[REDACTED]"}
    print(f"Form data: {printable_form_data}")

    try:
        # Send request
        response = requests.post(
            url,
            data=form_data,
            files=files,
            timeout=(5, 30),
            # Never forward a body-embedded credential to a redirect target.
            allow_redirects=False,
        )

        print(f"Response status: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        print(f"Response body: {response.text}")

        if response.status_code == 200:
            print("✓ Upload successful!")
        else:
            print("✗ Upload failed!")

    except Exception as e:
        print(f"✗ Error: {e}")
    finally:
        # Close file if opened
        if "audio" in files and hasattr(files["audio"][1], "close"):
            files["audio"][1].close()


def test_health_check(base_url: str) -> None:
    """Test the health check endpoint."""
    url = f"{base_url}/health"
    print(f"\nTesting health check: {url}")

    try:
        response = requests.get(url, timeout=(5, 30), allow_redirects=False)
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")
    except Exception as e:
        print(f"✗ Error: {e}")


def test_metrics(base_url: str, api_key: str) -> None:
    """Test the metrics endpoint."""
    url = f"{base_url}/metrics"
    print(f"\nTesting metrics: {url}")

    try:
        response = requests.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=(5, 30),
            # Requests does not promise to strip custom auth headers on every
            # redirect; fail closed instead of forwarding the API key.
            allow_redirects=False,
        )
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")
    except Exception as e:
        print(f"✗ Error: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test sdrtrunk-rdio-api endpoints")
    parser.add_argument(
        "--url",
        default="http://localhost:8080/api/call-upload",
        help="API endpoint URL",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        help=(
            "Read the API key from this file. If omitted, RDIO_API_KEY or a "
            "hidden interactive prompt is used."
        ),
    )
    parser.add_argument("--audio", help="Path to MP3 file to upload")
    parser.add_argument("--test-all", action="store_true", help="Test all endpoints")

    args = parser.parse_args()

    if args.key_file:
        descriptor = -1
        try:
            descriptor = os.open(
                args.key_file,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            key_status = os.fstat(descriptor)
            if not stat.S_ISREG(key_status.st_mode):
                raise OSError("API key path is not a regular file")
            if key_status.st_size > 4096:
                raise OSError("API key file is unexpectedly large")
            if os.name != "nt" and stat.S_IMODE(key_status.st_mode) & 0o077:
                raise OSError("API key file is accessible by group or others")
            with os.fdopen(descriptor, encoding="utf-8") as key_stream:
                descriptor = -1
                api_key = key_stream.read().strip()
        except (OSError, UnicodeError) as exc:
            parser.error(f"could not read API key file: {exc}")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    else:
        api_key = os.environ.get("RDIO_API_KEY", "").strip()
        if not api_key:
            api_key = getpass.getpass("API key: ").strip()
    if not 16 <= len(api_key) <= 512:
        parser.error("the API key must be 16-512 characters")

    # Extract base URL
    base_url = args.url.replace("/api/call-upload", "")

    # Test upload
    test_upload(args.url, api_key, args.audio)

    # Test other endpoints if requested
    if args.test_all:
        test_health_check(base_url)
        test_metrics(base_url, api_key)


if __name__ == "__main__":
    main()
