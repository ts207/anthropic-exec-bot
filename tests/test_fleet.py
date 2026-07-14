from __future__ import annotations

import json
from pathlib import Path

from test_discovery import _FakeQuotes, _analyzed_context, _binary_event, _grouped_event, _sports_event

from polybot.discovery.config import DiscoveryConfig, FleetConfig
from polybot.discovery.fleet import FleetManager, run_fleet_command, set_fleet_mode_command
from polybot.discovery.scorer import grade_market
from polybot.discovery.config import ScoringConfig
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15


class _Spawner:
    def __init__(self):
        self.spawned: list[tuple[list[str], Path]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, command: list[str], log_path: Path) -> _FakeProcess:
        self.spawned.append((command, log_path))
        process = _FakeProcess()
        self.processes.append(process)
        return process


class _Notifier:
    def __init__(self):
        self.messages: list[tuple[str, dict]] = []

    def notify(self, message, **fields):
        self.messages.append((message, fields))


def _patch_roots(monkeypatch, tmp_path: Path) -> Path:
    geo = tmp_path / "geo"
    monkeypatch.setattr("polybot.discovery.emit.GEO_DATA_ROOT", str(geo))
    monkeypatch.setattr("polybot.discovery.fleet.GEO_DATA_ROOT", str(geo))
    return geo


def _fleet_yaml(tmp_path: Path, *, position_mode: str = "alert_only", auto_ack: bool = False) -> Path:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        f"""
classifier:
  provider: rule_based
scoring:
  allow_fixture_analysis_live: true
fleet:
  enabled: true
  max_bots: 5
  position_mode: "{position_mode}"
  auto_ack: {str(auto_ack).lower()}
  generated_dir: {tmp_path / 'generated'}
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return path


def _events_fetch(events):
    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    return fetch


def test_fleet_once_spawns_a_bot_per_eligible_market(tmp_path, monkeypatch) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path)
    spawner = _Spawner()
    notifier = _Notifier()
    fetch = _events_fetch([_grouped_event(), _binary_event(), _sports_event()])

    assert run_fleet_command(config_path, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=notifier, spawner=spawner) == 0

    runners = sorted(cmd[3] for cmd, _log in spawner.spawned)
    assert runners == ["run-binary", "run-location-protection"]
    assert all("--live" not in cmd for cmd, _log in spawner.spawned)
    # Configs generated and operator gates armed with the fleet position mode.
    generated = sorted(p.name for p in (tmp_path / "generated").glob("*.yaml"))
    assert len(generated) == 2
    mode_files = list((geo / "operator" / "positions").glob("*.mode"))
    assert len(mode_files) == 2
    assert all("alert_only" in p.read_text(encoding="utf-8") for p in mode_files)
    # No auto-ack in alert_only mode.
    assert not (geo / "operator" / "live_ack").exists()
    # fleet_state.json records the cycle; once-mode shuts children down.
    state = json.loads((tmp_path / "data" / "fleet_state.json").read_text(encoding="utf-8"))
    assert len(state["desired"]) == 2
    assert all(process.terminated for process in spawner.processes)
    assert [m for m, _f in notifier.messages if "bot started" in m]


def test_fleet_live_auto_ack_arms_and_passes_live_flag(tmp_path, monkeypatch) -> None:
    # The pre-spawn gate check requires the live env (telegram + anthropic)
    # to be configured, same as the bot's own startup preflight.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    geo = _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path, position_mode="live", auto_ack=True)
    spawner = _Spawner()
    fetch = _events_fetch([_binary_event()])

    assert run_fleet_command(config_path, live=True, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=_Notifier(), spawner=spawner) == 0

    (command, _log), = spawner.spawned
    assert command[-1] == "--live"
    generated = next((tmp_path / "generated").glob("*.yaml"))
    assert "dry_run: false" in generated.read_text(encoding="utf-8")
    acks = list((geo / "operator" / "live_ack").rglob("*.json"))
    assert len(acks) == 1
    mode_files = list((geo / "operator" / "positions").glob("*.mode"))
    assert all("live" in p.read_text(encoding="utf-8") for p in mode_files)


def test_fleet_keeps_defender_for_held_market_and_stops_flat_demoted(tmp_path, monkeypatch) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=5, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    held_market = grade_market(_analyzed_context(_binary_event(slug="held")), scoring)
    flat_market = grade_market(_analyzed_context(_binary_event(slug="flat")), scoring)
    for context in (held_market, flat_market):
        store.save_context(context)
        store.save_source_plan(build_source_plan(context))
    spawner = _Spawner()
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"), spawner=spawner)

    summary = manager.sync([held_market, flat_market])
    assert sorted(summary["started"]) == sorted([held_market.market_id, flat_market.market_id])

    # The held market records a live holding; both markets then get demoted.
    from polybot.discovery.types import market_dir_slug

    holdings = Path(str(geo)) / market_dir_slug(held_market.market_id) / "dry_run" / "holdings.json"
    holdings.parent.mkdir(parents=True, exist_ok=True)
    holdings.write_text(json.dumps({"held_location": "yes", "source": "entry"}), encoding="utf-8")
    demoted_held = grade_market(held_market, ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=10**9, small_live_enabled=False))
    demoted_flat = grade_market(flat_market, ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=10**9, small_live_enabled=False))
    assert demoted_held.state == "PAPER_ELIGIBLE" and demoted_flat.state == "PAPER_ELIGIBLE"

    summary = manager.sync([demoted_held, demoted_flat])
    # The defender for the held position survives; the flat bot is stopped.
    assert summary["stopped"] == [flat_market.market_id]
    assert held_market.market_id in summary["running"]


def test_set_fleet_mode_writes_master_switch(tmp_path, monkeypatch, capsys) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    assert set_fleet_mode_command("off") == 0
    raw = json.loads((geo / "operator" / "global_mode.json").read_text(encoding="utf-8"))
    assert raw["mode"] == "off"


def test_fleet_ranks_by_edge_without_liquidity_bias(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=1, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    deep = grade_market(_analyzed_context(_binary_event(slug="deep", liquidity=50000.0)), scoring)
    thin = grade_market(_analyzed_context(_binary_event(slug="thin", liquidity=300.0)), scoring)
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"))

    # No scan data: NO liquidity bias in either direction -- selection is
    # deterministic by market_id, not by book depth.
    desired = manager.desired_markets([deep, thin])
    assert [c.market_id for c in desired] == [min(deep.market_id, thin.market_id)]

    # A scanned executable edge outranks thinness.
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "opportunities.json").write_text(
        json.dumps({"opportunities": [{"market_id": deep.market_id, "outcome": "yes", "tradable_edge": 0.12, "blockers": []}]}),
        encoding="utf-8",
    )
    desired = manager.desired_markets([deep, thin])
    assert [c.market_id for c in desired] == [deep.market_id]


def test_fleet_emits_no_side_config_when_no_edge_is_best(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=5, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(_analyzed_context(_binary_event()), ScoringConfig(allow_fixture_analysis_live=True))
    store.save_context(market)
    store.save_source_plan(build_source_plan(market))
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "opportunities.json").write_text(
        json.dumps(
            {
                "opportunities": [
                    {"market_id": market.market_id, "outcome": "yes", "side": "NO", "tradable_edge": 0.15, "blockers": []},
                    {"market_id": market.market_id, "outcome": "yes", "side": "YES", "tradable_edge": 0.03, "blockers": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"), spawner=_Spawner())
    assert manager.best_entry_side(market.market_id) == "NO"

    summary = manager.sync([market])
    assert summary["started"] == [market.market_id]
    from polybot.binary.config import load_binary_config

    generated = next((tmp_path / "generated").glob("*.yaml"))
    assert load_binary_config(generated).entry.side == "NO"
