from __future__ import annotations

import time
import urllib.parse
import urllib.robotparser
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .base import SourceReading
from ..config import SETTINGS
from ..log import log_event

_LAST_FETCH_BY_STATION: dict[str, float] = {}
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}


class WundergroundSetupError(RuntimeError):
    pass


@dataclass
class WundergroundDailyHighAdapter:
    station: str
    target_date: date
    unit: str
    station_timezone: str
    history_url_template: str | None = SETTINGS.wu_history_url_template
    api_key: str | None = SETTINGS.wu_api_key
    user_agent: str = SETTINGS.user_agent

    def poll(self) -> SourceReading | None:
        if not self.history_url_template or not self.api_key:
            raise WundergroundSetupError(
                "WU_HISTORY_URL_TEMPLATE and WU_API_KEY are required. See README Wunderground setup."
            )
        if not _claim_rate_limit_slot(self.station):
            return None
        day_url = self._url_for(self.target_date)
        next_url = self._url_for(self.target_date + timedelta(days=1))
        _check_robots(day_url, self.user_agent)
        _check_robots(next_url, self.user_agent)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        day_resp = requests.get(day_url, headers=headers, timeout=20)
        next_resp = requests.get(next_url, headers=headers, timeout=20)
        day_resp.raise_for_status()
        next_resp.raise_for_status()
        day_obs = parse_observations(day_resp.json())
        next_obs = parse_observations(next_resp.json())
        parsed = daily_max_reading(
            day_obs=day_obs,
            next_day_obs=next_obs,
            target_date=self.target_date,
            unit=self.unit,
            raw_url=day_url,
            station_timezone=self.station_timezone,
        )
        log_event(
            "source_poll",
            adapter="wunderground",
            station=self.station,
            date=self.target_date.isoformat(),
            unit=self.unit,
            value=parsed.value,
            is_locked=parsed.is_locked,
            confidence=parsed.confidence,
            raw_value=getattr(parsed, "raw_value", None),
            raw_url=day_url,
        )
        if parsed.is_locked:
            log_event(
                "source_locked",
                adapter="wunderground",
                station=self.station,
                date=self.target_date.isoformat(),
                value=parsed.value,
                unit=parsed.unit,
                confidence=parsed.confidence,
            )
        return parsed

    def _url_for(self, requested_date: date) -> str:
        return self.history_url_template.format(
            station=urllib.parse.quote(self.station),
            date=requested_date.strftime("%Y%m%d"),
            iso_date=requested_date.isoformat(),
            api_key=urllib.parse.quote(self.api_key or ""),
            unit=urllib.parse.quote(self.unit),
        )


def parse_observations(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("observations", "history", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = payload.get("valid_time_gmt")
        if nested is not None:
            return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def daily_max_reading(
    *,
    day_obs: list[dict[str, Any]],
    next_day_obs: list[dict[str, Any]],
    target_date: date,
    unit: str,
    raw_url: str,
    station_timezone: str = "UTC",
) -> SourceReading:
    tz = ZoneInfo(station_timezone)
    readings = [
        obs
        for obs in day_obs
        if _obs_date(obs, tz) == target_date and _temperature(obs, unit) is not None
    ]
    if not readings:
        return SourceReading(
            value=float("nan"),
            unit=unit,
            is_locked=False,
            confidence=0.0,
            raw_url=raw_url,
            fetched_at=datetime.now(timezone.utc),
        )
    max_value = max(_temperature(obs, unit) for obs in readings)
    assert max_value is not None
    locked = any(_obs_date(obs, tz) == target_date + timedelta(days=1) for obs in next_day_obs)
    confidence = 1.0 if locked and not _has_afternoon_gap(readings, target_date, tz) else 0.75
    rounded = _round_half_up(max_value)
    return SourceReading(
        value=float(rounded),
        unit=unit,
        is_locked=locked,
        confidence=confidence,
        raw_url=raw_url,
        fetched_at=datetime.now(timezone.utc),
        raw_value=float(max_value),
    )


def _temperature(obs: dict[str, Any], unit: str) -> float | None:
    keys = ["temp", "temperature", "metric.temp", "imperial.temp"]
    if unit.upper() == "C":
        keys = ["tempC", "temperatureC", "metric_temp", "metric.temp", *keys]
    elif unit.upper() == "F":
        keys = ["tempF", "temperatureF", "imperial_temp", "imperial.temp", *keys]
    for key in keys:
        value = _dig(obs, key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _obs_date(obs: dict[str, Any], station_tz: ZoneInfo) -> date | None:
    ts = obs.get("valid_time_gmt") or obs.get("epoch") or obs.get("timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(int(ts), timezone.utc).astimezone(station_tz).date()
        except (TypeError, ValueError, OSError):
            pass
    for key in ("obsTimeLocal", "time", "date"):
        value = obs.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and key != "obsTimeLocal":
                return parsed.astimezone(station_tz).date()
            return parsed.date()
        except ValueError:
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
    return None


def _obs_hour(obs: dict[str, Any], station_tz: ZoneInfo) -> int | None:
    ts = obs.get("valid_time_gmt") or obs.get("epoch") or obs.get("timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(int(ts), timezone.utc).astimezone(station_tz).hour
        except (TypeError, ValueError, OSError):
            pass
    value = obs.get("obsTimeLocal") or obs.get("time")
    if isinstance(value, str):
        try:
            key = "obsTimeLocal" if obs.get("obsTimeLocal") else "time"
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and key != "obsTimeLocal":
                return parsed.astimezone(station_tz).hour
            return parsed.hour
        except ValueError:
            return None
    return None


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


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    current: Any = obj
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _claim_rate_limit_slot(station: str) -> bool:
    now = time.monotonic()
    last = _LAST_FETCH_BY_STATION.get(station)
    if last is not None and now - last < 60:
        wait_seconds = 60 - (now - last)
        log_event("source_backoff", adapter="wunderground", station=station, reason="rate_limit", wait_seconds=wait_seconds)
        return False
    _LAST_FETCH_BY_STATION[station] = time.monotonic()
    return True


def _check_robots(url: str, user_agent: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return
    base = f"{parsed.scheme}://{parsed.netloc}"
    parser = _ROBOTS.get(base)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser(f"{base}/robots.txt")
        try:
            parser.read()
        except Exception:
            # If robots cannot be fetched, do not assume permission for source scraping.
            raise RuntimeError(f"could not read robots.txt for {base}")
        _ROBOTS[base] = parser
    if not parser.can_fetch(user_agent, url):
        raise RuntimeError(f"robots.txt disallows fetching {url}")
