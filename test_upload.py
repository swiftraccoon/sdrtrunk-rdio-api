#!/usr/bin/env python3
"""Test script for RdioCallsAPI - simulates SDRTrunk upload."""

import argparse
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
    print(f"Form data: {form_data}")

    try:
        # Send request
        response = requests.post(url, data=form_data, files=files)

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
        response = requests.get(url)
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")
    except Exception as e:
        print(f"✗ Error: {e}")


def test_metrics(base_url: str) -> None:
    """Test the metrics endpoint."""
    url = f"{base_url}/metrics"
    print(f"\nTesting metrics: {url}")

    try:
        response = requests.get(url)
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")
    except Exception as e:
        print(f"✗ Error: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test RdioCallsAPI endpoints")
    parser.add_argument(
        "--url",
        default="http://localhost:8080/api/call-upload",
        help="API endpoint URL",
    )
    parser.add_argument("--key", default="test-key", help="API key")
    parser.add_argument("--audio", help="Path to MP3 file to upload")
    parser.add_argument("--test-all", action="store_true", help="Test all endpoints")

    args = parser.parse_args()

    # Extract base URL
    base_url = args.url.replace("/api/call-upload", "")

    # Test upload
    test_upload(args.url, args.key, args.audio)

    # Test other endpoints if requested
    if args.test_all:
        test_health_check(base_url)
        test_metrics(base_url)


if __name__ == "__main__":
    main()
