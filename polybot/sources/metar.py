from __future__ import annotations

import math
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ..config import SETTINGS
from ..log import log_event
from .base import SourceReading

AVIATIONWEATHER_METAR_URL = "https://aviationweather.gov/api/data/metar"
MAX_HISTORY_DAYS = 15
_LAST_FETCH_BY_STATION: dict[str, float] = {}
_T_GROUP_RE = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
_BODY_TEMP_RE = re.compile(r"\b(M?\d{2})/(?:M?\d{2}|//)\b")


@dataclass
class MetarDailyHighAdapter:
    station: str
    target_date: date
    unit: str
    station_timezone: str
    api_url: str = AVIATIONWEATHER_METAR_URL
    user_agent: str = SETTINGS.user_agent

    def poll(self) -> SourceReading | None:
        if not _claim_rate_limit_slot(self.station):
            return None
        payload, raw_url = fetch_metars(
            station=self.station,
            target_date=self.target_date,
            station_timezone=self.station_timezone,
            api_url=self.api_url,
            user_agent=self.user_agent,
        )
        parsed = daily_max_reading(
            observations=parse_observations(payload),
            target_date=self.target_date,
            unit=self.unit,
            raw_url=raw_url,
            station_timezone=self.station_timezone,
        )
        log_event(
            "source_poll",
            adapter="metar",
            station=self.station,
            date=self.target_date.isoformat(),
            unit=self.unit,
            value=parsed.value,
            raw_value=parsed.raw_value,
            is_locked=parsed.is_locked,
            confidence=parsed.confidence,
            raw_url=raw_url,
        )
        if parsed.is_locked:
            log_event(
                "source_locked",
                adapter="metar",
                station=self.station,
                date=self.target_date.isoformat(),
                value=parsed.value,
                raw_value=parsed.raw_value,
                unit=parsed.unit,
                confidence=parsed.confidence,
            )
        return parsed


def fetch_metars(
    *,
    station: str,
    target_date: date,
    station_timezone: str,
    api_url: str = AVIATIONWEATHER_METAR_URL,
    user_agent: str = SETTINGS.user_agent,
) -> tuple[Any, str]:
    hours = _hours_to_fetch(target_date, station_timezone)
    params = {"ids": station.upper(), "format": "json", "hours": str(hours)}
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    response = requests.get(api_url, params=params, headers=headers, timeout=20)
    if response.status_code == 204:
        return [], f"{api_url}?{urllib.parse.urlencode(params)}"
    response.raise_for_status()
    return response.json(), response.url


def parse_observations(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("features", "data", "observations", "metars"):
            value = payload.get(key)
            if isinstance(value, list):
                if key == "features":
                    rows = []
                    for item in value:
                        if isinstance(item, dict) and isinstance(item.get("properties"), dict):
                            rows.append(item["properties"])
                    return rows
                return [item for item in value if isinstance(item, dict)]
        if payload.get("rawOb") or payload.get("raw_text"):
            return [payload]
    return []


def daily_max_reading(
    *,
    observations: list[dict[str, Any]],
    target_date: date,
    unit: str,
    raw_url: str,
    station_timezone: str,
) -> SourceReading:
    tz = ZoneInfo(station_timezone)
    target_obs = [
        obs
        for obs in observations
        if _obs_date(obs, tz) == target_date and temperature_c(obs) is not None
    ]
    if not target_obs:
        return SourceReading(
            value=float("nan"),
            unit=unit.upper(),
            is_locked=False,
            confidence=0.0,
            raw_url=raw_url,
            fetched_at=datetime.now(timezone.utc),
        )
    max_c = max(temperature_c(obs) for obs in target_obs)
    assert max_c is not None
    locked = any(_obs_date(obs, tz) == target_date + timedelta(days=1) for obs in observations)
    confidence = 1.0 if locked and not _has_afternoon_gap(target_obs, target_date, tz) else 0.75
    raw_value = _convert_unit(max_c, unit)
    rounded = _round_half_up(raw_value)
    return SourceReading(
        value=float(rounded),
        unit=unit.upper(),
        is_locked=locked,
        confidence=confidence,
        raw_url=raw_url,
        fetched_at=datetime.now(timezone.utc),
        raw_value=float(raw_value),
    )


def temperature_c(obs: dict[str, Any]) -> float | None:
    raw = str(obs.get("rawOb") or obs.get("raw_text") or obs.get("raw_text_value") or "")
    parsed = temperature_c_from_t_group(raw)
    if parsed is not None:
        return parsed
    for key in ("temp", "tempC", "temperature", "temperatureC"):
        value = obs.get(key)
        try:
            if value is not None and value != "":
                return float(value)
        except (TypeError, ValueError):
            pass
    return temperature_c_from_body(raw)


def temperature_c_from_t_group(raw_metar: str) -> float | None:
    match = _T_GROUP_RE.search(raw_metar)
    if not match:
        return None
    sign, digits, _dew_sign, _dew_digits = match.groups()
    value = int(digits) / 10.0
    if sign == "1":
        value *= -1
    return value


def temperature_c_from_body(raw_metar: str) -> float | None:
    match = _BODY_TEMP_RE.search(raw_metar)
    if not match:
        return None
    token = match.group(1)
    try:
        if token.startswith("M"):
            return -float(token[1:])
        return float(token)
    except ValueError:
        return None


def _obs_datetime(obs: dict[str, Any], station_tz: ZoneInfo) -> datetime | None:
    for key in ("obsTime", "valid_time_gmt", "epoch", "timestamp"):
        value = obs.get(key)
        if value is None or value == "":
            continue
        try:
            return datetime.fromtimestamp(int(value), timezone.utc).astimezone(station_tz)
        except (TypeError, ValueError, OSError):
            pass
    for key in ("reportTime", "receiptTime", "obsTimeLocal", "time", "date"):
        value = obs.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and key != "obsTimeLocal":
                return parsed.astimezone(station_tz)
            return parsed
        except ValueError:
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=station_tz)
            except ValueError:
                continue
    return None


def _obs_date(obs: dict[str, Any], station_tz: ZoneInfo) -> date | None:
    dt = _obs_datetime(obs, station_tz)
    return dt.date() if dt is not None else None


def _obs_hour(obs: dict[str, Any], station_tz: ZoneInfo) -> int | None:
    dt = _obs_datetime(obs, station_tz)
    return dt.hour if dt is not None else None


def _has_afternoon_gap(readings: list[dict[str, Any]], target_date: date, station_tz: ZoneInfo) -> bool:
    hours = sorted(
        hour
        for obs in readings
        if _obs_date(obs, station_tz) == target_date
        for hour in [_obs_hour(obs, station_tz)]
        if hour is not None and 12 <= hour <= 18
    )
    if len(hours) < 2:
        return True
    return any((right - left) > 2 for left, right in zip(hours, hours[1:]))


def _hours_to_fetch(target_date: date, station_timezone: str) -> int:
    tz = ZoneInfo(station_timezone)
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    hours = math.ceil((now - start).total_seconds() / 3600) + 2
    return min(max(hours, 24), MAX_HISTORY_DAYS * 24)


def _convert_unit(value_c: float, unit: str) -> float:
    if unit.upper() == "C":
        return value_c
    if unit.upper() == "F":
        return value_c * 9.0 / 5.0 + 32.0
    raise ValueError(f"unsupported temperature unit: {unit!r}")


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _claim_rate_limit_slot(station: str) -> bool:
    now = time.monotonic()
    last = _LAST_FETCH_BY_STATION.get(station)
    if last is not None and now - last < 60:
        wait_seconds = 60 - (now - last)
        log_event("source_backoff", adapter="metar", station=station, reason="rate_limit", wait_seconds=wait_seconds)
        return False
    _LAST_FETCH_BY_STATION[station] = now
    return True
