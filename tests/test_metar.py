from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from polybot.sources.metar import (
    daily_max_reading,
    parse_observations,
    temperature_c_from_body,
    temperature_c_from_t_group,
)


def _epoch(local_iso: str, timezone_name: str) -> int:
    tz = ZoneInfo(timezone_name)
    return int(datetime.fromisoformat(local_iso).replace(tzinfo=tz).timestamp())


def test_t_group_temperature_uses_tenths_before_json_temp() -> None:
    obs = {
        "rawOb": "METAR KLGA 030451Z 18007KT 10SM CLR 33/22 A2986 RMK AO2 T03280217",
        "temp": 33,
    }
    reading = daily_max_reading(
        observations=[
            {"obsTime": _epoch("2026-07-03T12:00:00", "America/New_York"), "rawOb": "METAR KLGA 031600Z 10SM CLR 31/20 A2990 RMK T03100200"},
            {"obsTime": _epoch("2026-07-03T14:00:00", "America/New_York"), **obs},
            {"obsTime": _epoch("2026-07-03T16:00:00", "America/New_York"), "rawOb": "METAR KLGA 032000Z 10SM CLR 32/21 A2990 RMK T03220210"},
            {"obsTime": _epoch("2026-07-03T18:00:00", "America/New_York"), "rawOb": "METAR KLGA 032200Z 10SM CLR 30/21 A2990 RMK T03020210"},
            {"obsTime": _epoch("2026-07-04T00:05:00", "America/New_York"), "rawOb": "METAR KLGA 040405Z 10SM CLR 27/20 A2990 RMK T02700200"},
        ],
        target_date=date(2026, 7, 3),
        unit="F",
        raw_url="https://aviationweather.gov/api/data/metar",
        station_timezone="America/New_York",
    )
    assert reading.is_locked is True
    assert reading.confidence == 1.0
    assert round(reading.raw_value or 0, 2) == 91.04
    assert reading.value == 91


def test_t_group_and_body_temperature_parsers() -> None:
    assert temperature_c_from_t_group("RMK T03780211") == 37.8
    assert temperature_c_from_t_group("RMK T10050011") == -0.5
    assert temperature_c_from_body("METAR TEST 031200Z 18007KT 10SM CLR M05/M10 A2990") == -5
    assert temperature_c_from_body("METAR TEST 031200Z 18007KT 10SM CLR 33/22 A2990") == 33


def test_metar_epoch_uses_station_local_date_for_lock() -> None:
    observations = [
        {"obsTime": _epoch("2026-07-03T12:00:00", "America/New_York"), "temp": 29.0},
        {"obsTime": _epoch("2026-07-03T14:00:00", "America/New_York"), "temp": 30.0},
        {"obsTime": _epoch("2026-07-03T16:00:00", "America/New_York"), "temp": 29.5},
        {"obsTime": _epoch("2026-07-03T20:30:00", "America/New_York"), "temp": 27.0},
    ]
    not_locked = daily_max_reading(
        observations=[*observations, {"obsTime": _epoch("2026-07-03T21:00:00", "America/New_York"), "temp": 26.0}],
        target_date=date(2026, 7, 3),
        unit="C",
        raw_url="https://example.test",
        station_timezone="America/New_York",
    )
    assert not_locked.is_locked is False

    locked = daily_max_reading(
        observations=[*observations, {"obsTime": _epoch("2026-07-04T00:05:00", "America/New_York"), "temp": 26.0}],
        target_date=date(2026, 7, 3),
        unit="C",
        raw_url="https://example.test",
        station_timezone="America/New_York",
    )
    assert locked.is_locked is True
    assert locked.value == 30


def test_parse_observations_accepts_geojson_features() -> None:
    payload = {"features": [{"properties": {"rawOb": "METAR TEST", "temp": 20}}, {"bad": "row"}]}
    assert parse_observations(payload) == [{"rawOb": "METAR TEST", "temp": 20}]
