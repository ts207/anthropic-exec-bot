from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .book import BookCache
from .config import SETTINGS
from .exec_engine import build_clob_client
from .gamma import markets_for_event_slug
from .log import log_event
from .risk import RiskState
from .settlement import SettlementWatcher
from .sources.metar import MetarDailyHighAdapter
from .sources.wunderground import WundergroundDailyHighAdapter
from .strategies.weather import WeatherStrategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybot")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--live", action="store_true")
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("slug")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return inspect_command(args.slug)
    if args.command == "run":
        return run_command(Path(args.config), live_flag=args.live)
    return 2


def inspect_command(slug: str) -> int:
    markets = markets_for_event_slug(slug)
    print(json.dumps([market.__dict__ for market in markets], indent=2))
    for market in markets:
        book = BookCache(market.token_ids)
        for token_id in market.token_ids:
            try:
                book.rest_snapshot(token_id)
            except Exception as exc:
                print(f"book snapshot failed for {token_id}: {exc}", file=sys.stderr)
        print(json.dumps({token: book.snapshot_state(token) for token in market.token_ids}, indent=2))
    return 0


def run_command(config_path: Path, live_flag: bool) -> int:
    if not SETTINGS.dry_run and not live_flag:
        raise SystemExit("POLYBOT_DRY_RUN=0 requires --live; refusing to run live-capable config by accident")
    effective_live = _confirm_live(live_flag)
    if not effective_live:
        print("Running in dry-run mode.")
    risk = RiskState.load()
    client = build_clob_client() if effective_live else None
    settlement_watcher = SettlementWatcher(client, risk) if client is not None else None
    if settlement_watcher is not None:
        settlement_watcher.start()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    market_configs = raw.get("markets", []) if isinstance(raw, dict) else []
    stop = False

    def _stop(_sig: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    strategies: list[tuple[WeatherStrategy, float, list[BookCache]]] = []
    for cfg in market_configs:
        if cfg.get("type", "weather") != "weather":
            continue
        slug = cfg["slug"]
        station_timezone = _required_station_timezone(cfg, slug)
        markets = markets_for_event_slug(slug)
        books = {market.market_slug: BookCache([market.yes_token_id]) for market in markets}
        for book in books.values():
            book.start_ws()
        source_name = str(cfg.get("source", "metar")).lower()
        if source_name == "metar":
            adapter = MetarDailyHighAdapter(
                station=cfg["station"],
                target_date=date.fromisoformat(str(cfg["date"])),
                unit=str(cfg["unit"]).upper(),
                station_timezone=station_timezone,
            )
        elif source_name == "wunderground":
            adapter = WundergroundDailyHighAdapter(
                station=cfg["station"],
                target_date=date.fromisoformat(str(cfg["date"])),
                unit=str(cfg["unit"]).upper(),
                station_timezone=station_timezone,
            )
        else:
            raise SystemExit(f"unsupported weather source for {slug}: {source_name}")
        strategies.append(
            (
                WeatherStrategy(
                    markets=markets,
                    adapter=adapter,
                    book_factory=lambda market, books=books: books[market.market_slug],
                    risk=risk,
                    client=client,
                    settlement_watcher=settlement_watcher,
                ),
                float(cfg.get("poll_seconds", 60)),
                list(books.values()),
            )
        )

    last_run = [0.0 for _ in strategies]
    next_allowed_runs = [0.0 for _ in strategies]
    backoff_waits = [0.0 for _ in strategies]
    try:
        while not stop:
            now = time.monotonic()
            for index, (strategy, poll_seconds, _books) in enumerate(strategies):
                if now < next_allowed_runs[index]:
                    continue
                if now - last_run[index] >= poll_seconds:
                    try:
                        strategy.run_once()
                        next_allowed_runs[index] = 0.0
                        backoff_waits[index] = 0.0
                    except Exception as exc:
                        wait_seconds = _next_backoff_wait(backoff_waits[index], poll_seconds)
                        backoff_waits[index] = wait_seconds
                        next_allowed_runs[index] = now + wait_seconds
                        log_event("strategy_error", strategy="weather", error=str(exc), backoff_seconds=wait_seconds)
                    finally:
                        last_run[index] = now
            time.sleep(0.25)
    finally:
        if settlement_watcher is not None:
            settlement_watcher.stop()
        for _strategy, _poll, books in strategies:
            for book in books:
                book.stop_ws()
    return 0


def _required_station_timezone(cfg: dict[str, Any], slug: str) -> str:
    value = cfg.get("timezone")
    if not value:
        raise SystemExit(f"weather market {slug} requires a station timezone in markets.yaml")
    timezone_name = str(value)
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise SystemExit(f"invalid timezone for {slug}: {timezone_name}") from exc
    return timezone_name


def _next_backoff_wait(previous_wait: float, poll_seconds: float) -> float:
    baseline = previous_wait or poll_seconds
    return min(max(poll_seconds, baseline * 2), 15 * 60)


def _confirm_live(live_flag: bool) -> bool:
    if not live_flag:
        return False
    if SETTINGS.dry_run:
        raise SystemExit("--live requires POLYBOT_DRY_RUN=0")
    print("LIVE TRADING REQUESTED. Active guardrails:")
    print(json.dumps(SETTINGS.guardrails.__dict__, indent=2, sort_keys=True))
    response = input("Type yes to enable live trading: ")
    if response != "yes":
        raise SystemExit("live trading not confirmed")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
