from __future__ import annotations

import argparse
import csv
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import SETTINGS
from .sources.metar import AVIATIONWEATHER_METAR_URL, MAX_HISTORY_DAYS, daily_max_reading, parse_observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a METAR-vs-Wunderground daily-high calibration table.")
    parser.add_argument("--station", required=True, help="ICAO station, e.g. KLGA or RKSI")
    parser.add_argument("--timezone", required=True, help="Station timezone, e.g. America/New_York")
    parser.add_argument("--unit", required=True, choices=["C", "F", "c", "f"])
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--wu-url-template", default="", help="Optional WU URL template with {station}, {date}, {iso_date}")
    parser.add_argument("--wu-values", type=Path, help="Optional CSV with columns date,value from manual WU checks")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=60.0,
        help="Delay between paged AviationWeather requests; default follows the 1 request/minute guidance.",
    )
    args = parser.parse_args(argv)

    days = min(args.days, MAX_HISTORY_DAYS)
    if args.days > MAX_HISTORY_DAYS:
        print(
            f"Requested {args.days} days, but AviationWeather exposes only {MAX_HISTORY_DAYS} previous days; using {days}.",
            file=sys.stderr,
        )
    if days <= 0:
        raise SystemExit("--days must be positive")

    timezone_name = str(args.timezone)
    ZoneInfo(timezone_name)
    unit = str(args.unit).upper()
    end = datetime.now(ZoneInfo(timezone_name)).date() - timedelta(days=1)
    dates = [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    observations, raw_url = _fetch_observations_for_dates(
        station=args.station,
        dates=dates,
        timezone_name=timezone_name,
        request_interval_seconds=args.request_interval_seconds,
    )
    wu_values = _read_wu_values(args.wu_values) if args.wu_values else {}

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "station",
            "date",
            "unit",
            "metar_display_high",
            "metar_raw_high",
            "locked",
            "confidence",
            "wu_value",
            "delta",
            "match",
            "wu_url",
            "metar_url",
        ],
    )
    writer.writeheader()
    for target_date in dates:
        reading = daily_max_reading(
            observations=observations,
            target_date=target_date,
            unit=unit,
            raw_url=raw_url,
            station_timezone=timezone_name,
        )
        wu_value = wu_values.get(target_date)
        delta = None if wu_value is None or reading.raw_value is None else reading.raw_value - wu_value
        writer.writerow(
            {
                "station": args.station.upper(),
                "date": target_date.isoformat(),
                "unit": unit,
                "metar_display_high": "" if reading.value != reading.value else f"{reading.value:g}",
                "metar_raw_high": "" if reading.raw_value is None else f"{reading.raw_value:.3f}",
                "locked": reading.is_locked,
                "confidence": f"{reading.confidence:.2f}",
                "wu_value": "" if wu_value is None else f"{wu_value:.3f}",
                "delta": "" if delta is None else f"{delta:.3f}",
                "match": "" if delta is None else abs(delta) < 0.01,
                "wu_url": _wu_url(args.wu_url_template, args.station, target_date),
                "metar_url": raw_url,
            }
        )
    return 0


def _fetch_observations_for_dates(
    *,
    station: str,
    dates: list[date],
    timezone_name: str,
    request_interval_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    if not dates:
        return [], ""
    all_rows: list[dict[str, Any]] = []
    raw_urls: list[str] = []
    chunks = _date_chunks(dates, chunk_days=7)
    for index, chunk in enumerate(chunks):
        rows, raw_url = _fetch_observation_chunk(station=station, start_date=chunk[0], end_date=chunk[-1], timezone_name=timezone_name)
        all_rows.extend(rows)
        raw_urls.append(raw_url)
        if index < len(chunks) - 1 and request_interval_seconds > 0:
            time.sleep(request_interval_seconds)
    return _dedupe_observations(all_rows), " ".join(raw_urls)


def _fetch_observation_chunk(*, station: str, start_date: date, end_date: date, timezone_name: str) -> tuple[list[dict[str, Any]], str]:
    tz = ZoneInfo(timezone_name)
    end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(hours=2)
    end_utc = end_local.astimezone(timezone.utc)
    hours = int(((end_date - start_date).days + 2) * 24 + 2)
    params = {"ids": station.upper(), "format": "json", "date": end_utc.strftime("%Y%m%d_%H%M"), "hours": str(hours)}
    headers = {"User-Agent": SETTINGS.user_agent, "Accept": "application/json"}
    response = requests.get(AVIATIONWEATHER_METAR_URL, params=params, headers=headers, timeout=30)
    if response.status_code == 204:
        return [], f"{AVIATIONWEATHER_METAR_URL}?{urllib.parse.urlencode(params)}"
    response.raise_for_status()
    return parse_observations(response.json()), response.url


def _date_chunks(dates: list[date], chunk_days: int) -> list[list[date]]:
    return [dates[index : index + chunk_days] for index in range(0, len(dates), chunk_days)]


def _dedupe_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("icaoId") or ""), str(row.get("obsTime") or ""), str(row.get("rawOb") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _read_wu_values(path: Path) -> dict[date, float]:
    values: dict[date, float] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            values[date.fromisoformat(str(row["date"]))] = float(row["value"])
    return values


def _wu_url(template: str, station: str, target_date: date) -> str:
    if not template:
        return ""
    return template.format(
        station=urllib.parse.quote(station.upper()),
        date=target_date.strftime("%Y%m%d"),
        iso_date=target_date.isoformat(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
