from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from polybot.sources import wunderground
from polybot.sources.wunderground import daily_max_reading, parse_observations


def test_wunderground_daily_max_locked_high_confidence() -> None:
    payload = {
        "observations": [
            {"obsTimeLocal": "2026-07-03T12:00:00+09:00", "tempC": 24},
            {"obsTimeLocal": "2026-07-03T14:00:00+09:00", "tempC": 27},
            {"obsTimeLocal": "2026-07-03T16:00:00+09:00", "tempC": 26},
            {"obsTimeLocal": "2026-07-03T18:00:00+09:00", "tempC": 25},
        ]
    }
    next_payload = {"observations": [{"obsTimeLocal": "2026-07-04T00:05:00+09:00", "tempC": 22}]}
    reading = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(next_payload),
        target_date=date(2026, 7, 3),
        unit="C",
        raw_url="https://example.test/history",
    )
    assert reading.value == 27
    assert reading.is_locked is True
    assert reading.confidence == 1.0


def test_wunderground_gap_lowers_confidence() -> None:
    payload = {
        "observations": [
            {"obsTimeLocal": "2026-07-03T12:00:00+09:00", "tempC": 24},
            {"obsTimeLocal": "2026-07-03T16:30:00+09:00", "tempC": 28},
        ]
    }
    next_payload = {"observations": [{"obsTimeLocal": "2026-07-04T00:05:00+09:00", "tempC": 22}]}
    reading = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(next_payload),
        target_date=date(2026, 7, 3),
        unit="C",
        raw_url="https://example.test/history",
    )
    assert reading.value == 28
    assert reading.is_locked is True
    assert reading.confidence < 1.0


def test_wunderground_epoch_uses_station_local_date_for_lock() -> None:
    tz = ZoneInfo("America/New_York")

    def epoch(local_iso: str) -> int:
        return int(datetime.fromisoformat(local_iso).replace(tzinfo=tz).timestamp())

    payload = {
        "observations": [
            {"valid_time_gmt": epoch("2026-07-03T12:00:00"), "tempF": 80},
            {"valid_time_gmt": epoch("2026-07-03T14:00:00"), "tempF": 82},
            {"valid_time_gmt": epoch("2026-07-03T16:00:00"), "tempF": 81},
            {"valid_time_gmt": epoch("2026-07-03T20:30:00"), "tempF": 79},
        ]
    }
    not_next_day_local = {"observations": [{"valid_time_gmt": epoch("2026-07-03T21:00:00"), "tempF": 78}]}
    reading = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(not_next_day_local),
        target_date=date(2026, 7, 3),
        unit="F",
        raw_url="https://example.test/history",
        station_timezone="America/New_York",
    )
    assert reading.value == 82
    assert reading.is_locked is False

    next_day_local = {"observations": [{"valid_time_gmt": epoch("2026-07-04T00:05:00"), "tempF": 77}]}
    locked = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(next_day_local),
        target_date=date(2026, 7, 3),
        unit="F",
        raw_url="https://example.test/history",
        station_timezone="America/New_York",
    )
    assert locked.is_locked is True


def test_wunderground_half_degree_rounds_up_and_logs_raw_value() -> None:
    payload = {
        "observations": [
            {"obsTimeLocal": "2026-07-03T12:00:00+00:00", "tempC": 25.0},
            {"obsTimeLocal": "2026-07-03T14:00:00+00:00", "tempC": 26.5},
            {"obsTimeLocal": "2026-07-03T16:00:00+00:00", "tempC": 26.0},
        ]
    }
    next_payload = {"observations": [{"obsTimeLocal": "2026-07-04T00:05:00+00:00", "tempC": 22}]}
    reading = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(next_payload),
        target_date=date(2026, 7, 3),
        unit="C",
        raw_url="https://example.test/history",
    )
    assert reading.value == 27
    assert reading.raw_value == 26.5


def test_wunderground_utc_iso_strings_convert_to_station_timezone() -> None:
    payload = {
        "observations": [
            {"time": "2026-07-04T00:30:00Z", "tempF": 80},
            {"time": "2026-07-04T02:00:00Z", "tempF": 82},
        ]
    }
    next_payload = {"observations": [{"time": "2026-07-04T05:00:00Z", "tempF": 77}]}
    reading = daily_max_reading(
        day_obs=parse_observations(payload),
        next_day_obs=parse_observations(next_payload),
        target_date=date(2026, 7, 3),
        unit="F",
        raw_url="https://example.test/history",
        station_timezone="America/New_York",
    )
    assert reading.value == 82
    assert reading.is_locked is True


def test_wunderground_rate_limit_is_non_blocking(monkeypatch) -> None:
    events = []

    def capture(event: str, **fields):
        events.append((event, fields))

    monkeypatch.setattr(wunderground, "log_event", capture)
    monkeypatch.setattr(wunderground.time, "monotonic", lambda: 100.0)
    wunderground._LAST_FETCH_BY_STATION.clear()
    assert wunderground._claim_rate_limit_slot("KLGA") is True
    monkeypatch.setattr(wunderground.time, "monotonic", lambda: 120.0)
    assert wunderground._claim_rate_limit_slot("KLGA") is False
    assert events[-1][0] == "source_backoff"
    assert events[-1][1]["reason"] == "rate_limit"
    assert events[-1][1]["wait_seconds"] == 40.0
