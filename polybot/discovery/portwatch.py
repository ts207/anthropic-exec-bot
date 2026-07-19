"""IMF PortWatch chokepoint data client.

A family of Polymarket geopolitics markets resolves on a hard, public number:
the 7-day moving average of transit calls ("Arrivals of Ships") through a
maritime chokepoint (Strait of Hormuz, Bab-el-Mandeb, Suez, ...), published
daily by IMF PortWatch. Example rule:

    Resolves YES if IMF Portwatch publishes a 7-day moving average of transit
    calls for the Strait of Hormuz equal to or above 60 for any date between
    market creation and the deadline.

That is exactly the shape the valuation/NPM strategy already exploits: a
scheduled official dataset resolving a thin market. This module fetches the
daily series and computes the trailing 7-day average, so the estimator prices
these markets against the real number instead of a base-rate guess.

Data source: the PortWatch "Daily_Chokepoints_Data" ArcGIS feature service.
`n_total` is the day's transit-call count (container + dry bulk + general
cargo + roro + tanker), matching the "Arrivals of Ships" definition in the
market rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

FEATURE_SERVICE = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0/query"
)

# PortWatch chokepoint portnames, keyed by the terms most likely to appear in
# a market question. Extend as new chokepoint markets are discovered.
CHOKEPOINT_NAMES = {
    "hormuz": "Strait of Hormuz",
    "bab-el-mandeb": "Bab el-Mandeb Strait",
    "bab el-mandeb": "Bab el-Mandeb Strait",
    "suez": "Suez Canal",
    "panama": "Panama Canal",
    "malacca": "Strait of Malacca",
    "bosphorus": "Bosphorus Strait",
    "gibraltar": "Strait of Gibraltar",
}

Fetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ChokepointReading:
    portname: str
    latest_date: str
    latest_value: int
    ma7: float                 # trailing 7-day moving average of n_total
    days_available: int
    series: list[tuple[str, int]]  # (date, n_total), most-recent first


def _http_fetch(url: str) -> dict[str, Any]:
    import requests

    response = requests.get(url, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("PortWatch returned a non-object payload")
    if "error" in payload:
        raise RuntimeError(f"PortWatch query error: {payload['error']}")
    return payload


def build_query_url(portname: str, *, limit: int = 30) -> str:
    params = {
        "where": f"portname = '{portname}'",
        "outFields": "date,n_total,portname",
        "orderByFields": "date DESC",
        "resultRecordCount": limit,
        "f": "json",
    }
    return f"{FEATURE_SERVICE}?{urlencode(params)}"


def parse_features(payload: dict[str, Any]) -> list[tuple[str, int]]:
    """Return (iso_date, n_total) rows, most-recent first. ArcGIS dates come
    back either as 'YYYY-MM-DD' strings (DateOnly) or epoch-millis ints."""
    rows: list[tuple[str, int]] = []
    for feature in payload.get("features", []):
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            continue
        raw_date = attrs.get("date")
        value = attrs.get("n_total")
        if raw_date is None or value is None:
            continue
        rows.append((_normalize_date(raw_date), int(value)))
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows


def _normalize_date(raw: Any) -> str:
    if isinstance(raw, str):
        return raw[:10]
    # epoch millis -> date
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date().isoformat()


def chokepoint_reading(portname: str, *, fetcher: Fetcher | None = None, limit: int = 30) -> ChokepointReading | None:
    fetcher = fetcher or _http_fetch
    payload = fetcher(build_query_url(portname, limit=limit))
    rows = parse_features(payload)
    if not rows:
        return None
    window = [value for _date, value in rows[:7]]
    ma7 = round(sum(window) / len(window), 2)
    return ChokepointReading(
        portname=portname,
        latest_date=rows[0][0],
        latest_value=rows[0][1],
        ma7=ma7,
        days_available=len(rows),
        series=rows,
    )


def match_chokepoint(text: str) -> str | None:
    """Map a market question/rule text to a PortWatch portname, or None."""
    lowered = text.lower()
    for term, portname in CHOKEPOINT_NAMES.items():
        if term in lowered:
            return portname
    return None


def evidence_line(reading: ChokepointReading, threshold: float | None = None) -> str:
    """One-line factual summary for injection into an estimator prompt."""
    base = (
        f"IMF PortWatch data for {reading.portname}: latest daily transit calls "
        f"{reading.latest_value} on {reading.latest_date}; trailing 7-day moving "
        f"average {reading.ma7} (over {min(7, reading.days_available)} days)."
    )
    if threshold is not None:
        gap = round(threshold - reading.ma7, 2)
        base += (
            f" Market threshold is {threshold:g}; the 7-day average is currently "
            f"{'ABOVE' if gap <= 0 else 'BELOW'} it by {abs(gap):g}."
        )
    return base
