from __future__ import annotations

import pytest

from polybot.main import _next_backoff_wait, _required_station_timezone


def test_weather_timezone_required() -> None:
    with pytest.raises(SystemExit):
        _required_station_timezone({"slug": "s"}, "s")


def test_weather_timezone_validated() -> None:
    assert _required_station_timezone({"timezone": "America/New_York"}, "s") == "America/New_York"
    with pytest.raises(SystemExit):
        _required_station_timezone({"timezone": "Not/AZone"}, "s")


def test_backoff_wait_escalates_and_caps() -> None:
    assert _next_backoff_wait(previous_wait=0, poll_seconds=60) == 120
    assert _next_backoff_wait(previous_wait=120, poll_seconds=60) == 240
    assert _next_backoff_wait(previous_wait=1000, poll_seconds=60) == 900
