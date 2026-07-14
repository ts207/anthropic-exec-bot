from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.holdings import _atomic_json_write
from polybot.core.storage import append_jsonl

from .types import Opportunity

# Probabilities can't be fixed with cleverness, only with resolved outcomes.
# This module is the measurement loop: every scan logs what the system
# believed and what the market believed at the same instant; every resolution
# scores both. The report's verdict (does the model beat the market's own
# Brier score?) is what earns forecast probabilities the right to move money.


class CalibrationLog:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.estimates_path = self.data_dir / "calibration_estimates.jsonl"
        self.resolutions_path = self.data_dir / "calibration_resolutions.json"
        self.status_path = self.data_dir / "calibration_status.json"

    # -- collection --

    def record_estimates(self, opportunities: list[Opportunity]) -> int:
        """Log every priced outcome from a scan: the model's probability, its
        source, and the market mid at the same moment (the benchmark)."""
        at = datetime.now(timezone.utc).isoformat()
        recorded = 0
        for item in opportunities:
            if item.probability_source == "none" or item.executable_price is None:
                continue
            append_jsonl(
                self.estimates_path,
                {
                    "market_id": item.market_id,
                    "outcome": item.outcome,
                    "probability": item.estimated_probability,
                    "source": item.probability_source,
                    "market_mid": item.detail.get("market_mid"),
                    "at": at,
                },
            )
            recorded += 1
        return recorded

    def record_resolution(self, market_id: str, outcome: str, resolved_yes: bool) -> None:
        resolutions = self._resolutions()
        resolutions[f"{market_id}::{outcome}"] = {
            "market_id": market_id,
            "outcome": outcome,
            "resolved_yes": bool(resolved_yes),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json_write(self.resolutions_path, resolutions)

    # -- scoring --

    def report(self, *, min_resolved: int = 20) -> dict[str, Any]:
        """Join latest estimates to resolutions and score them.

        Per source: Brier score vs the market mid's Brier ON THE SAME ROWS --
        a model that is 'pretty good' but worse than the mid is worth negative
        money. Also buckets estimates for a calibration curve (of all the
        times we said ~70%, how often did it happen?)."""
        resolutions = self._resolutions()
        latest = self._latest_estimates()
        rows: list[dict[str, Any]] = []
        for key, resolution in resolutions.items():
            for source, estimate in latest.get(key, {}).items():
                rows.append(
                    {
                        "source": source,
                        "probability": float(estimate["probability"]),
                        "market_mid": estimate.get("market_mid"),
                        "outcome_value": 1.0 if resolution["resolved_yes"] else 0.0,
                    }
                )

        sources: dict[str, Any] = {}
        for source in sorted({row["source"] for row in rows}):
            scoped = [row for row in rows if row["source"] == source]
            brier = _brier([(row["probability"], row["outcome_value"]) for row in scoped])
            benchmarked = [row for row in scoped if row["market_mid"] is not None]
            market_brier = _brier([(float(row["market_mid"]), row["outcome_value"]) for row in benchmarked])
            sources[source] = {
                "n": len(scoped),
                "brier": brier,
                "market_brier": market_brier,
                "beats_market": brier is not None and market_brier is not None and brier < market_brier,
            }

        buckets: dict[str, Any] = {}
        for row in rows:
            low = min(9, int(row["probability"] * 10))
            label = f"{low / 10:.1f}-{(low + 1) / 10:.1f}"
            bucket = buckets.setdefault(label, {"n": 0, "sum_estimate": 0.0, "sum_outcome": 0.0})
            bucket["n"] += 1
            bucket["sum_estimate"] += row["probability"]
            bucket["sum_outcome"] += row["outcome_value"]
        for bucket in buckets.values():
            bucket["mean_estimate"] = round(bucket.pop("sum_estimate") / bucket["n"], 4)
            bucket["realized_frequency"] = round(bucket.pop("sum_outcome") / bucket["n"], 4)

        forecast = sources.get("forecast_state", {})
        forecast_calibrated = bool(forecast.get("beats_market")) and forecast.get("n", 0) >= min_resolved
        result = {
            "resolved_outcomes": len(resolutions),
            "scored_rows": len(rows),
            "min_resolved_for_calibration": min_resolved,
            "sources": sources,
            "buckets": dict(sorted(buckets.items())),
            "forecast_calibrated": forecast_calibrated,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json_write(self.status_path, {"forecast_calibrated": forecast_calibrated, "generated_at": result["generated_at"]})
        return result

    def forecast_calibrated(self) -> bool:
        """Fail closed: no status file (report never run) means uncalibrated."""
        if not self.status_path.exists():
            return False
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(isinstance(raw, dict) and raw.get("forecast_calibrated"))

    # -- internals --

    def _resolutions(self) -> dict[str, Any]:
        if not self.resolutions_path.exists():
            return {}
        try:
            raw = json.loads(self.resolutions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _latest_estimates(self) -> dict[str, dict[str, dict[str, Any]]]:
        """(market::outcome) -> source -> latest logged estimate. The model's
        final opinion before resolution is what gets scored."""
        latest: dict[str, dict[str, dict[str, Any]]] = {}
        if not self.estimates_path.exists():
            return latest
        for line in self.estimates_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            key = f"{record.get('market_id')}::{record.get('outcome')}"
            latest.setdefault(key, {})[str(record.get("source"))] = record
        return latest


def _brier(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return round(sum((p - outcome) ** 2 for p, outcome in pairs) / len(pairs), 4)


__all__ = ["CalibrationLog"]
