from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .config import load_location_config


def evaluate_probability_state(state_path: Path, resolved_outcome: str) -> dict[str, Any]:
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    observations = raw.get("observations") if isinstance(raw, dict) else None
    if not isinstance(observations, list) or not observations:
        raise ValueError("forecast probability state contains no observations")
    resolved = resolved_outcome.strip().lower().replace(" ", "_")
    scores: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        probabilities = observation.get("after") if isinstance(observation, dict) else None
        if not isinstance(probabilities, dict) or resolved not in probabilities:
            raise ValueError(f"observation {index} does not contain resolved outcome {resolved!r}")
        parsed = {str(name): float(value) for name, value in probabilities.items()}
        total = sum(parsed.values())
        if total <= 0:
            raise ValueError(f"observation {index} has invalid zero probability total")
        normalized = {name: max(0.0, value) / total for name, value in parsed.items()}
        resolved_probability = min(1.0, max(0.0, normalized[resolved]))
        brier = sum((probability - (1.0 if name == resolved else 0.0)) ** 2 for name, probability in normalized.items())
        log_loss = -math.log(max(1e-12, resolved_probability))
        scores.append(
            {
                "index": index,
                "observed_at": observation.get("observed_at"),
                "resolved_probability": resolved_probability,
                "brier_score": brier,
                "log_loss": log_loss,
            }
        )
    return {
        "resolved_outcome": resolved,
        "observation_count": len(scores),
        "mean_brier_score": sum(item["brier_score"] for item in scores) / len(scores),
        "mean_log_loss": sum(item["log_loss"] for item in scores) / len(scores),
        "final_resolved_probability": scores[-1]["resolved_probability"],
        "scores": scores,
        "caveat": (
            "Sequential observations from one event are correlated. Aggregate results across held-out resolved events "
            "before using these metrics as a deployment gate."
        ),
    }


def evaluate_forecast_command(config_path: Path, resolved_outcome: str, state_path: Path | None = None) -> int:
    config = load_location_config(config_path)
    if config.outcome(resolved_outcome) is None:
        raise SystemExit(f"resolved outcome {resolved_outcome!r} is not configured")
    data_dir = config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir
    path = state_path or data_dir / "forecast_probability.json"
    print(json.dumps(evaluate_probability_state(path, resolved_outcome), indent=2, sort_keys=True))
    return 0


__all__ = ["evaluate_forecast_command", "evaluate_probability_state"]
