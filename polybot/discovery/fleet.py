from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from polybot.core.holdings import _atomic_json_write
from polybot.core.operator import OperatorGate
from polybot.log import log_event

from .config import DiscoveryConfig, load_discovery_config
from .emit import GEO_DATA_ROOT, emit_bot_config
from .store import DiscoveryStore
from .types import MarketContext, market_dir_slug


def fleet_operator_dir() -> Path:
    # Every fleet-managed executor keeps data under GEO_DATA_ROOT/<slug>, so
    # the shared operator dir (data_dir.parent / "operator") is the same for
    # all of them: one global mode file kills or arms the entire fleet.
    return Path(GEO_DATA_ROOT) / "operator"


def set_fleet_mode_command(mode: str) -> int:
    """Master switch for every fleet-managed bot: writes the shared operator
    global mode ('off' halts all execution mid-cycle; 'alert_only' keeps bots
    watching but never trading; 'live' defers to each market's position mode)."""
    if mode not in {"off", "alert_only", "dry_run", "live"}:
        raise SystemExit(f"invalid mode {mode!r}")
    path = fleet_operator_dir() / "global_mode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(path, {"mode": mode, "updated_at": datetime.now(timezone.utc).isoformat()})
    print(json.dumps({"path": str(path), "mode": mode}, indent=2))
    return 0


class FleetManager:
    """Supervises one executor subprocess per eligible market.

    Desired set = LIVE_CONFIRMATION_ELIGIBLE markets (liquidity-ranked, capped
    at fleet.max_bots) UNION any market currently holding a position: a bot
    defending a position is never stopped by a grading change. Flat bots whose
    markets left the desired set are terminated.
    """

    def __init__(
        self,
        config: DiscoveryConfig,
        store: DiscoveryStore,
        *,
        live: bool,
        per_order_usd: float,
        ledger_path: str,
        spawner: Callable[[list[str], Path], Any] | None = None,
    ):
        self.config = config
        self.store = store
        self.live = live
        self.per_order_usd = per_order_usd
        self.ledger_path = ledger_path
        self.spawner = spawner or _default_spawner
        self.processes: dict[str, Any] = {}

    # -- planning --

    def desired_markets(self, contexts: list[MarketContext]) -> list[MarketContext]:
        eligible = sorted(
            (c for c in contexts if c.state == "LIVE_CONFIRMATION_ELIGIBLE"),
            key=lambda c: -c.liquidity,
        )[: self.config.fleet.max_bots]
        desired = {c.market_id: c for c in eligible}
        for context in contexts:
            if context.market_id not in desired and self.is_holding(context.market_id):
                desired[context.market_id] = context
        return list(desired.values())

    def is_holding(self, market_id: str) -> bool:
        base = Path(GEO_DATA_ROOT) / market_dir_slug(market_id)
        for candidate in (base / "dry_run" / "holdings.json", base / "holdings.json"):
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("held_location"):
                return True
        return False

    # -- reconciliation --

    def sync(self, contexts: list[MarketContext]) -> dict[str, Any]:
        desired = self.desired_markets(contexts)
        desired_ids = {c.market_id for c in desired}
        summary: dict[str, Any] = {"desired": sorted(desired_ids), "started": [], "stopped": [], "armed": [], "errors": {}}

        # Stop flat bots whose markets left the desired set.
        for market_id in sorted(set(self.processes) - desired_ids):
            process = self.processes.pop(market_id)
            if process.poll() is None:
                process.terminate()
            summary["stopped"].append(market_id)
            log_event("fleet_bot_stopped", market_id=market_id)

        for context in desired:
            try:
                config_path = self._ensure_config(context)
                if self._arm(config_path):
                    summary["armed"].append(context.market_id)
                if self._ensure_running(context, config_path):
                    summary["started"].append(context.market_id)
            except Exception as exc:
                summary["errors"][context.market_id] = str(exc)
                log_event("fleet_market_error", market_id=context.market_id, error=str(exc))
        summary["running"] = sorted(m for m, p in self.processes.items() if p.poll() is None)
        return summary

    def _ensure_config(self, context: MarketContext) -> Path:
        plan = self.store.load_source_plan(context.market_id)
        if plan is None:
            raise ValueError("no source plan; plan-sources stage has not covered this market yet")
        out = Path(self.config.fleet.generated_dir) / f"{market_dir_slug(context.market_id)}.yaml"
        # Re-emit only when missing, the pinned rule hash changed, or the
        # dry-run mode no longer matches the fleet's: rewriting an unchanged
        # config would needlessly churn the operator ack hash.
        expected_mode = f"dry_run: {'false' if self.live else 'true'}"
        if out.exists():
            text = out.read_text(encoding="utf-8")
            if context.rule_text_sha256 in text and expected_mode in text:
                return out
        return emit_bot_config(
            context,
            plan,
            entry_usd=self._entry_usd(context),
            out_path=out,
            ledger_path=self.ledger_path,
            dry_run=not self.live,
        )

    def _entry_usd(self, context: MarketContext) -> float:
        recommended = context.scores.get("recommended_max_order_usd")
        if recommended:
            return min(self.per_order_usd, float(recommended))
        return self.per_order_usd

    def _arm(self, config_path: Path) -> bool:
        """Write the fleet position mode (and, when configured, the live ack)
        for one managed market's operator gate."""
        loaded = self._load_executor_config(config_path)
        gate = OperatorGate(config_path, loaded)
        gate.set_position_mode(self.config.fleet.position_mode)
        if self.live and self.config.fleet.position_mode == "live" and self.config.fleet.auto_ack:
            gate.write_ack(note="fleet auto-ack: generated config, fleet-level review")
            return True
        return False

    def _ensure_running(self, context: MarketContext, config_path: Path) -> bool:
        process = self.processes.get(context.market_id)
        if process is not None and process.poll() is None:
            return False
        if process is not None:
            log_event("fleet_bot_exited", market_id=context.market_id, returncode=process.poll())
        command = self._command(context, config_path)
        log_path = self.config.logs_dir / f"fleet_{market_dir_slug(context.market_id)}.out"
        self.processes[context.market_id] = self.spawner(command, log_path)
        log_event("fleet_bot_started", market_id=context.market_id, command=" ".join(command))
        return True

    def _command(self, context: MarketContext, config_path: Path) -> list[str]:
        runner = "run-location-protection" if context.kind == "grouped" else "run-binary"
        command = [sys.executable, "-m", "polybot.geopolitics", runner, "--config", str(config_path)]
        if self.live:
            command.append("--live")
        return command

    def _load_executor_config(self, config_path: Path) -> Any:
        text = config_path.read_text(encoding="utf-8")
        if "\nevent:" in text or text.startswith("event:"):
            from polybot.location.config import load_location_config

            return load_location_config(config_path)
        from polybot.binary.config import load_binary_config

        return load_binary_config(config_path)

    def shutdown(self) -> None:
        for market_id, process in self.processes.items():
            if process.poll() is None:
                process.terminate()
                log_event("fleet_bot_stopped", market_id=market_id, reason="fleet_shutdown")
        self.processes.clear()


def run_fleet_command(
    config_path: Path,
    *,
    live: bool = False,
    once: bool = False,
    events_fetch=None,
    quotes=None,
    analyzer=None,
    notifier=None,
    spawner=None,
) -> int:
    """One supervisor for the whole geopolitical universe: run the discovery
    cycle, then reconcile the bot fleet against its output, forever.

    Master controls: `set-fleet-mode off` halts all execution mid-cycle via
    the shared operator dir; stopping this process leaves running bots
    unmanaged (they keep their own safety machinery) until it restarts.
    """
    from polybot.core.notifier import TelegramNotifier

    from .runner import _load, _run_discovery_cycle

    config, store, allocator = _load(config_path)
    if not config.fleet.enabled:
        raise SystemExit("fleet.enabled is false; enable it in the discovery config to run the fleet")
    notifier = notifier or TelegramNotifier()
    manager = FleetManager(
        config,
        store,
        live=live,
        per_order_usd=allocator.config.per_order_usd,
        ledger_path=str(allocator.state_path),
        spawner=spawner,
    )
    try:
        while True:
            try:
                _run_discovery_cycle(config_path, config, events_fetch=events_fetch, quotes=quotes, analyzer=analyzer, notifier=notifier)
            except Exception as exc:
                log_event("fleet_discovery_cycle_error", error=str(exc))
            try:
                summary = manager.sync(store.all_contexts())
                _atomic_json_write(config.data_dir / "fleet_state.json", {**summary, "live": live, "updated_at": datetime.now(timezone.utc).isoformat()})
                print(json.dumps(summary, indent=2, sort_keys=True))
                for market_id in summary["started"]:
                    notifier.notify("Fleet: bot started", market_id=market_id, live=live)
                for market_id in summary["stopped"]:
                    notifier.notify("Fleet: bot stopped", market_id=market_id)
            except Exception as exc:
                log_event("fleet_sync_error", error=str(exc))
                try:
                    notifier.notify("Fleet sync failed; continuing", error=str(exc))
                except Exception as notify_exc:
                    log_event("fleet_notify_failed", error=str(notify_exc))
            if once:
                return 0
            time.sleep(max(60.0, config.schedule.interval_minutes * 60.0))
    finally:
        if once:
            # A one-shot invocation must not leave orphan children behind in
            # tests/CI; the long-running mode keeps bots alive across cycles.
            manager.shutdown()


def _default_spawner(command: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    return subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
