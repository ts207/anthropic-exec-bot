from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polybot.analysis import latency_report_command, trades_report_command


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _journal(path: Path, *, action: str, phase: str, created: str, updated: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "execution_id": path.stem,
                "action": action,
                "phase": phase,
                "created_at": created,
                "updated_at": updated,
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


# ---- scan history (time series of edges) ----


def test_scan_command_appends_scan_history(tmp_path, capsys) -> None:
    from test_discovery import _FakeQuotes, _binary_event
    from polybot.discovery.runner import discover_markets_command, grade_markets_command, scan_opportunities_command

    binary_id = "0xiran-ceasefire-m"
    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
scoring:
  allow_fixture_analysis_live: true
opportunity:
  probability_estimates:
    "{binary_id}":
      "yes": 0.60
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    events = [_binary_event()]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    assert discover_markets_command(config_path, events_fetch=fetch) == 0
    assert grade_markets_command(config_path) == 0
    assert scan_opportunities_command(config_path, quotes=_FakeQuotes()) == 0
    assert scan_opportunities_command(config_path, quotes=_FakeQuotes()) == 0
    capsys.readouterr()

    lines = [json.loads(l) for l in (tmp_path / "data" / "scan_history.jsonl").read_text(encoding="utf-8").splitlines()]
    # Two cycles, YES+NO rows each: history APPENDS while opportunities.json overwrites.
    opportunity_rows = [l for l in lines if l["kind"] == "opportunity"]
    assert len(opportunity_rows) == 4
    assert {row["side"] for row in opportunity_rows} == {"YES", "NO"}
    assert len({row["at"] for row in opportunity_rows}) == 2  # two distinct scan stamps
    assert all("tradable_edge" in row and "spread" in row for row in opportunity_rows)


# ---- latency report ----


def test_latency_report_joins_article_classifier_and_journal(tmp_path, capsys) -> None:
    logs = tmp_path / "logs"
    data = tmp_path / "data"
    base = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    def ts(seconds: float) -> str:
        return (base + timedelta(seconds=seconds)).isoformat()

    _write_jsonl(
        logs / "binary_articles.jsonl",
        [{"hash": "h1", "url": "https://reuters.com/a", "published_at": ts(0), "fetched_at": ts(4)}],
    )
    _write_jsonl(
        logs / "polybot.jsonl",
        [
            {"event": "binary_classifier_attempt", "article_hash": "h1", "ts_utc": ts(5)},
            {"event": "binary_classifier_result", "article_hash": "h1", "ts_utc": ts(11)},
        ],
    )
    _journal(
        data / "geopolitics" / "mkt" / "execution_journal" / "e1.json",
        action="ENTER_YES",
        phase="completed",
        created=ts(12),
        updated=ts(14),
        payload={"article": {"hash": "h1", "published_at": ts(0)}, "result": "ENTERED"},
    )

    assert latency_report_command(logs_dir=logs, data_root=data) == 0
    report = json.loads(capsys.readouterr().out)
    stages = report["stages"]
    assert stages["publish_to_fetch"] == {"n": 1, "p50_s": 4.0, "p90_s": 4.0, "max_s": 4.0}
    assert stages["fetch_to_first_classify"]["p50_s"] == 1.0
    assert stages["classify_duration"]["p50_s"] == 6.0
    assert stages["decision_to_order_complete"]["p50_s"] == 2.0
    assert stages["publish_to_order_complete"]["p50_s"] == 14.0


def test_latency_report_empty_dirs_are_fine(tmp_path, capsys) -> None:
    assert latency_report_command(logs_dir=tmp_path / "logs", data_root=tmp_path / "data") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["articles_seen"] == 0
    assert report["stages"]["publish_to_fetch"] == {"n": 0}


# ---- trades report ----


def test_trades_report_pairs_entry_and_exit_with_pnl(tmp_path, capsys) -> None:
    data = tmp_path / "data"
    bot = data / "geopolitics" / "iran-ceasefire" / "dry_run"
    _journal(
        bot / "execution_journal" / "a-entry.json",
        action="ENTER_YES",
        phase="completed",
        created="2026-07-15T12:00:00+00:00",
        updated="2026-07-15T12:00:05+00:00",
        payload={"result": "ENTERED", "filled_shares": 125.0, "estimated_fill_usd": 50.0},
    )
    _journal(
        bot / "execution_journal" / "b-exit.json",
        action="EXIT_HELD",
        phase="completed",
        created="2026-07-16T12:00:00+00:00",
        updated="2026-07-16T12:00:03+00:00",
        payload={"result": "EXITED", "total_sold": 125.0, "confirmed_proceeds": 93.75, "decision": {"reason": "yes_foreclosure_confirmed"}},
    )
    ledger = tmp_path / "allocations.json"
    ledger.write_text(json.dumps({"realized_net": 43.75, "realized_by_day": {"2026-07-16": 43.75}, "open_positions": []}), encoding="utf-8")

    assert trades_report_command(data_root=data, ledger_path=ledger) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["bots"] == 1
    assert report["closed_trades_with_pnl"] == 1
    assert report["realized_pnl_from_journals"] == pytest.approx(43.75)
    exit_row = next(t for t in report["trades"] if t["action"] == "EXIT_HELD")
    assert exit_row["pnl_usd"] == pytest.approx(43.75)
    assert exit_row["hold_seconds"] == pytest.approx(86398.0)
    assert exit_row["reason"] == "yes_foreclosure_confirmed"
    assert report["ledger"]["realized_net"] == pytest.approx(43.75)


def test_trades_report_trim_keeps_entry_open_for_final_exit(tmp_path, capsys) -> None:
    data = tmp_path / "data"
    bot = data / "m1"
    _journal(
        bot / "execution_journal" / "a.json",
        action="ENTER_YES",
        phase="completed",
        created="2026-07-15T12:00:00+00:00",
        updated="2026-07-15T12:00:05+00:00",
        payload={"result": "ENTERED", "estimated_fill_usd": 50.0},
    )
    _journal(
        bot / "execution_journal" / "b.json",
        action="TRIM_HELD",
        phase="completed",
        created="2026-07-16T12:00:00+00:00",
        updated="2026-07-16T12:00:01+00:00",
        payload={"result": "TRIMMED", "total_sold": 30.0, "confirmed_proceeds": 20.0},
    )
    _journal(
        bot / "execution_journal" / "c.json",
        action="EXIT_HELD",
        phase="completed",
        created="2026-07-17T12:00:00+00:00",
        updated="2026-07-17T12:00:01+00:00",
        payload={"result": "EXITED", "total_sold": 95.0, "confirmed_proceeds": 60.0},
    )
    assert trades_report_command(data_root=data) == 0
    report = json.loads(capsys.readouterr().out)
    # Both the trim and the final exit are paired against the same entry.
    pnls = [t["pnl_usd"] for t in report["trades"] if t["pnl_usd"] is not None]
    assert pnls == [pytest.approx(-30.0), pytest.approx(10.0)]


def test_binary_entry_journal_records_fill_cost(tmp_path) -> None:
    from test_binary_bot import _config as _binary_config, _executor as _binary_executor, _signal as _binary_signal, article

    from polybot.binary.decision import BinaryDecision
    from polybot.core.execution import DryRunTradingAdapter

    executor = _binary_executor(tmp_path, _binary_config(), DryRunTradingAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _binary_signal())
    assert executor.execute(decision, article("The round will be held next week.")) == "ENTERED"
    records = list((executor.store.data_dir / "execution_journal").glob("*.json"))
    assert records
    payload = json.loads(records[0].read_text(encoding="utf-8"))["payload"]
    # DryRun fill = 100/0.90 shares at ask 0.40 -> cost recorded for the trades report.
    assert payload["estimated_fill_usd"] == pytest.approx(payload["filled_shares"] * 0.40, abs=1e-3)
    assert payload["usd_budget"] == pytest.approx(100.0)
