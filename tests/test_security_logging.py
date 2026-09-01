"""Tests for bounded logging on unauthenticated rejection paths."""

import logging

import pytest

from src.security.logging import WarningSampler


def test_warning_sampler_bounds_events_and_reports_suppression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [100.0]
    sampler = WarningSampler(
        maximum_per_window=2,
        window_seconds=10,
        clock=lambda: now[0],
    )
    target = logging.getLogger("warning-sampler-test")

    with caplog.at_level(logging.WARNING, logger=target.name):
        assert sampler.warning(target, "rejected request %d", 1)
        assert sampler.warning(target, "rejected request %d", 2)
        assert not sampler.warning(target, "rejected request %d", 3)
        assert not sampler.warning(target, "rejected request %d", 4)
        now[0] += 10
        assert sampler.warning(target, "rejected request %d", 5)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "rejected request 1",
        "rejected request 2",
        "Suppressed 2 repetitive security warnings in the prior window",
        "rejected request 5",
    ]


@pytest.mark.parametrize(
    ("maximum", "window"),
    [(0, 1.0), (1, 0.0), (1, -1.0)],
)
def test_warning_sampler_rejects_invalid_bounds(maximum: int, window: float) -> None:
    with pytest.raises(ValueError):
        WarningSampler(maximum_per_window=maximum, window_seconds=window)
