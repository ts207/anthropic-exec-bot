from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..book import BookCache
from ..config import SETTINGS
from ..exec_engine import submit_capped_order
from ..gamma import MarketMeta
from ..log import log_event
from ..risk import RiskState
from ..sources.base import SourceAdapter, SourceReading


@dataclass(frozen=True)
class Bucket:
    label: str
    low: float | None
    high: float | None
    unit: str
    inclusive_low: bool = True
    inclusive_high: bool = True

    def contains(self, value: float) -> bool:
        if self.low is not None:
            if value < self.low or (value == self.low and not self.inclusive_low):
                return False
        if self.high is not None:
            if value > self.high or (value == self.high and not self.inclusive_high):
                return False
        return True


@dataclass(frozen=True)
class WeatherMarketConfig:
    slug: str
    station: str
    date: date
    unit: str
    poll_seconds: float = 60.0


def parse_bucket_label(label: str) -> Bucket:
    text = label.strip()
    unit_match = re.search(r"°\s*([CF])", text, re.IGNORECASE)
    if not unit_match:
        raise ValueError(f"bucket label missing unit: {label!r}")
    unit = unit_match.group(1).upper()
    numbers = [float(item) for item in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", text)]
    lowered = text.lower()
    if "or below" in lowered or "or lower" in lowered or "below" in lowered:
        if len(numbers) != 1:
            raise ValueError(f"invalid open-low bucket: {label!r}")
        return Bucket(label=text, low=None, high=numbers[0], unit=unit)
    if "or above" in lowered or "or higher" in lowered or "above" in lowered:
        if len(numbers) != 1:
            raise ValueError(f"invalid open-high bucket: {label!r}")
        return Bucket(label=text, low=numbers[0], high=None, unit=unit)
    if "-" in text or " to " in lowered or "between" in lowered:
        if len(numbers) != 2:
            raise ValueError(f"invalid range bucket: {label!r}")
        low, high = sorted(numbers)
        return Bucket(label=text, low=low, high=high, unit=unit)
    if len(numbers) == 1:
        return Bucket(label=text, low=numbers[0], high=numbers[0], unit=unit)
    raise ValueError(f"unparseable bucket label: {label!r}")


def parse_market_buckets(markets: list[MarketMeta]) -> dict[str, Bucket]:
    buckets: dict[str, Bucket] = {}
    for market in markets:
        label = _label_from_question(market.question)
        buckets[market.market_slug] = parse_bucket_label(label)
    units = {bucket.unit for bucket in buckets.values()}
    if len(units) != 1:
        raise ValueError(f"mixed bucket units: {units}")
    return buckets


def choose_outcome(markets: list[MarketMeta], reading: SourceReading) -> MarketMeta:
    buckets = parse_market_buckets(markets)
    for market in markets:
        bucket = buckets[market.market_slug]
        if bucket.unit != reading.unit.upper():
            raise ValueError(f"unit mismatch: bucket {bucket.unit}, reading {reading.unit}")
        if bucket.contains(reading.value):
            return market
    raise ValueError(f"no bucket contains {reading.value}{reading.unit}")


class WeatherStrategy:
    def __init__(
        self,
        *,
        markets: list[MarketMeta],
        adapter: SourceAdapter,
        book_factory: Callable[[MarketMeta], BookCache],
        risk: RiskState,
        client: object | None = None,
        settlement_watcher: object | None = None,
    ):
        self.markets = markets
        self.adapter = adapter
        self.book_factory = book_factory
        self.risk = risk
        self.client = client
        self.settlement_watcher = settlement_watcher

    def run_once(self) -> None:
        try:
            parse_market_buckets(self.markets)
        except ValueError as exc:
            log_event("strategy_skip", strategy="weather", reason="unparseable_bucket", error=str(exc))
            return
        reading = self.adapter.poll()
        if reading is None:
            log_event("strategy_skip", strategy="weather", reason="no_source_reading")
            return
        if not reading.is_locked or reading.confidence != 1.0:
            log_event(
                "strategy_skip",
                strategy="weather",
                reason="parse_low_confidence",
                is_locked=reading.is_locked,
                confidence=reading.confidence,
            )
            return
        try:
            market = choose_outcome(self.markets, reading)
        except ValueError as exc:
            log_event("strategy_skip", strategy="weather", reason="bucket_mapping_failed", error=str(exc))
            return
        if _boundary_margin_needs_skip(self.markets, market, reading):
            log_event(
                "strategy_skip",
                strategy="weather",
                reason="boundary_margin",
                value=reading.value,
                raw_value=reading.raw_value,
                question=market.question,
            )
            return
        book = self.book_factory(market)
        log_event("book_snapshot", token_id=market.yes_token_id, book=book.snapshot_state(market.yes_token_id))
        submit_capped_order(
            market=market,
            token_id=market.yes_token_id,
            cap_price=SETTINGS.guardrails.max_entry_price_revisable,
            book=book,
            risk=self.risk,
            client=self.client,
            settlement_watcher=self.settlement_watcher,
        )

    def loop(self, poll_seconds: float) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:
                log_event("strategy_error", strategy="weather", error=str(exc), backoff_seconds=poll_seconds)
            time.sleep(poll_seconds)


def _label_from_question(question: str) -> str:
    match = re.search(r"\bbe\s+(.+?)\s+on\b", question, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return question


def _boundary_margin_needs_skip(
    markets: list[MarketMeta],
    selected_market: MarketMeta,
    reading: SourceReading,
    margin: float = 0.5,
) -> bool:
    bucket = parse_market_buckets(markets)[selected_market.market_slug]
    raw_value = reading.raw_value if reading.raw_value is not None else reading.value
    for edge in _effective_bucket_edges(bucket):
        if abs(float(raw_value) - edge) < margin:
            return True
    return False


def _effective_bucket_edges(bucket: Bucket) -> list[float]:
    edges: list[float] = []
    if bucket.low is not None:
        edges.append(float(bucket.low) - 0.5)
    if bucket.high is not None:
        edges.append(float(bucket.high) + 0.5)
    return edges
