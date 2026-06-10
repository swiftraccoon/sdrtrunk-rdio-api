"""sdrtrunk-rdio-api - A modular RdioScanner API ingestion server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sdrtrunk-rdio-api")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0-dev"

__author__ = "sdrtrunk-rdio-api Team"
__description__ = (
    "A lightweight, modular API server for ingesting radio calls from "
    "SDRTrunk via the RdioScanner protocol"
)
