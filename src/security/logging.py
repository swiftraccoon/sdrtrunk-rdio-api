"""Bound repetitive security warnings without allocating per-client state."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any


class WarningSampler:
    """Emit a fixed number of warnings per window and summarize suppression.

    Security rejection paths are reachable before authentication, so logging
    every request gives a distributed caller a direct disk-write primitive.
    This sampler deliberately keeps only constant-size, process-local state.
    """

    def __init__(
        self,
        *,
        maximum_per_window: int = 10,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_per_window < 1:
            raise ValueError("maximum_per_window must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._maximum = maximum_per_window
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._window_started = clock()
        self._emitted = 0
        self._suppressed = 0

    def warning(
        self,
        target: logging.Logger,
        message: str,
        *args: Any,
    ) -> bool:
        """Log one sampled warning and return whether the event was emitted."""
        now = self._clock()
        suppressed_summary = 0
        with self._lock:
            if now - self._window_started >= self._window_seconds:
                suppressed_summary = self._suppressed
                self._window_started = now
                self._emitted = 0
                self._suppressed = 0

            if self._emitted < self._maximum:
                self._emitted += 1
                emit_event = True
            else:
                # Keep even the counter's integer representation bounded
                # under a sustained, distributed flood.
                self._suppressed = min(self._suppressed + 1, 2_147_483_647)
                emit_event = False

        # Keep logger callbacks outside the lock: handlers may block or invoke
        # application code, and neither should stall concurrent admission.
        if suppressed_summary:
            target.warning(
                "Suppressed %d repetitive security warnings in the prior window",
                suppressed_summary,
            )
        if emit_event:
            target.warning(message, *args)
        return emit_event
