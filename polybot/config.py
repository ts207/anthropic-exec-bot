from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    if value in {"1", "true", "TRUE", "yes", "YES"}:
        return True
    if value in {"0", "false", "FALSE", "no", "NO"}:
        return False
    raise ValueError(f"invalid boolean env {name}={value!r}")


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _float_env_lower_only(name: str, default: float) -> float:
    parsed = _float_env(name, default)
    return min(parsed, default)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value
    return None


def _int_env_first(names: tuple[str, ...], default: int) -> int:
    value = _env_first(*names)
    if value is None:
        return default
    parsed = int(value)
    if parsed < 0:
        joined = " or ".join(names)
        raise ValueError(f"{joined} must be non-negative")
    return parsed


def _signature_type_env() -> int | None:
    raw = _env_first("POLYBOT_SIGNATURE_TYPE")
    if raw is None:
        if _env_first("DEPOSIT_WALLET_ADDRESS"):
            return 3
        if _env_first("FUNDER_ADDRESS", "POLYBOT_FUNDER_ADDRESS"):
            return 1
        return None
    parsed = int(raw)
    if parsed not in {0, 1, 2, 3}:
        raise ValueError("POLYBOT_SIGNATURE_TYPE must be 0, 1, 2, or 3")
    return parsed


def _int_env_lower_only(name: str, default: int) -> int:
    parsed = _int_env(name, default)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return min(parsed, default)


@dataclass(frozen=True)
class Guardrails:
    max_entry_price: float = 0.90
    max_entry_price_revisable: float = 0.85
    per_order_notional: float = 25.0
    per_market_notional: float = 50.0
    per_day_notional: float = 100.0
    kill_switch_failures: int = 2
    max_book_staleness_seconds: float = 5.0


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    clob_host: str
    gamma_host: str
    data_api_host: str
    chain_id: int
    private_key: str | None
    clob_api_key: str | None
    clob_secret: str | None
    clob_passphrase: str | None
    signature_type: int | None
    funder_address: str | None
    logs_dir: Path
    risk_state_path: Path
    user_agent: str
    guardrails: Guardrails


def load_settings() -> Settings:
    logs_dir = Path(os.getenv("POLYBOT_LOGS_DIR", "logs"))
    guardrails = Guardrails(
        max_entry_price=_float_env_lower_only("POLYBOT_MAX_ENTRY_PRICE", 0.90),
        max_entry_price_revisable=_float_env_lower_only("POLYBOT_MAX_ENTRY_PRICE_REVISABLE", 0.85),
        per_order_notional=_float_env_lower_only("POLYBOT_PER_ORDER_NOTIONAL", 25.0),
        per_market_notional=_float_env_lower_only("POLYBOT_PER_MARKET_NOTIONAL", 50.0),
        per_day_notional=_float_env_lower_only("POLYBOT_PER_DAY_NOTIONAL", 100.0),
        kill_switch_failures=_int_env_lower_only("POLYBOT_KILL_SWITCH_FAILURES", 2),
        max_book_staleness_seconds=_float_env_lower_only("POLYBOT_MAX_BOOK_STALENESS_SECONDS", 5.0),
    )
    return Settings(
        dry_run=_bool_env("POLYBOT_DRY_RUN", True),
        clob_host=_env_first("POLYBOT_CLOB_HOST", "CLOB_HOST") or "https://clob.polymarket.com",
        gamma_host=os.getenv("POLYBOT_GAMMA_HOST", "https://gamma-api.polymarket.com"),
        data_api_host=os.getenv("POLYBOT_DATA_API_HOST", "https://data-api.polymarket.com"),
        chain_id=_int_env_first(("POLYBOT_CHAIN_ID", "CHAIN_ID"), 137),
        private_key=_env_first("POLYBOT_PRIVATE_KEY", "PRIVATE_KEY"),
        clob_api_key=_env_first("POLYBOT_CLOB_API_KEY", "CLOB_API_KEY"),
        clob_secret=_env_first("POLYBOT_CLOB_SECRET", "CLOB_SECRET"),
        clob_passphrase=_env_first("POLYBOT_CLOB_PASSPHRASE", "CLOB_PASS_PHRASE"),
        signature_type=_signature_type_env(),
        funder_address=_env_first("POLYBOT_FUNDER_ADDRESS", "DEPOSIT_WALLET_ADDRESS", "FUNDER_ADDRESS"),
        logs_dir=logs_dir,
        risk_state_path=Path(os.getenv("POLYBOT_RISK_STATE_PATH", str(logs_dir / "risk_state.json"))),
        user_agent=os.getenv(
            "POLYBOT_USER_AGENT",
            "polybot/0.1 contact=operator; source-update research bot",
        ),
        guardrails=guardrails,
    )


SETTINGS = load_settings()
