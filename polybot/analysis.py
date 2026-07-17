from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Post-hoc analysis over the streams the system already writes:
#   logs/*_articles.jsonl        every article (published_at, fetched_at)
#   logs/polybot.jsonl           every structured event (classifier attempts/
#                                results with timestamps)
#   data/**/execution_journal/   every trade attempt (created/updated, fills,
#                                proceeds)
# latency-report answers "are we actually winning the race, and where does
# the time go"; trades-report is the per-trade P&L table you study after the
# soak. Both are read-only joins -- no new instrumentation.


def _parse_ts(text: Any) -> datetime | None:
    cleaned = str(text or "").strip().replace("Z", "+00:00")
    if not cleaned:
        return None
    try:
        stamp = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _percentiles(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pick(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 2)

    return {"n": len(ordered), "p50_s": pick(0.50), "p90_s": pick(0.90), "max_s": round(ordered[-1], 2)}


# ---- latency report ----


def latency_report_command(*, logs_dir: Path = Path("logs"), data_root: Path = Path("data")) -> int:
    """Join article/classifier/journal timestamps into per-stage latency
    percentiles: publish->fetch (source freshness), fetch->classify (queue),
    classify duration (the model), decision->order-complete (execution)."""
    articles: dict[str, dict[str, Any]] = {}
    for path in sorted(logs_dir.glob("*_articles.jsonl")):
        for record in _read_jsonl(path):
            article_hash = str(record.get("hash") or "")
            if article_hash:
                articles[article_hash] = record

    first_attempt: dict[str, datetime] = {}
    last_result: dict[str, datetime] = {}
    for record in _read_jsonl(logs_dir / "polybot.jsonl"):
        event = str(record.get("event") or "")
        article_hash = str(record.get("article_hash") or "")
        stamp = _parse_ts(record.get("ts_utc"))
        if not article_hash or stamp is None:
            continue
        if event.endswith("_classifier_attempt"):
            if article_hash not in first_attempt or stamp < first_attempt[article_hash]:
                first_attempt[article_hash] = stamp
        elif event.endswith("_classifier_result"):
            if article_hash not in last_result or stamp > last_result[article_hash]:
                last_result[article_hash] = stamp

    publish_to_fetch: list[float] = []
    fetch_to_classify: list[float] = []
    classify_duration: list[float] = []
    for article_hash, article in articles.items():
        fetched = _parse_ts(article.get("fetched_at"))
        published = _parse_ts(article.get("published_at"))
        if fetched is not None and published is not None and fetched >= published:
            publish_to_fetch.append((fetched - published).total_seconds())
        attempt = first_attempt.get(article_hash)
        if fetched is not None and attempt is not None and attempt >= fetched:
            fetch_to_classify.append((attempt - fetched).total_seconds())
        result = last_result.get(article_hash)
        if attempt is not None and result is not None and result >= attempt:
            classify_duration.append((result - attempt).total_seconds())

    decision_to_order: list[float] = []
    end_to_end: list[float] = []
    for record in _journal_records(data_root):
        if record.get("phase") not in {"completed", "unfilled"}:
            continue
        created = _parse_ts(record.get("created_at"))
        updated = _parse_ts(record.get("updated_at"))
        if created is not None and updated is not None and updated >= created:
            decision_to_order.append((updated - created).total_seconds())
        article = record.get("payload", {}).get("article")
        published = _parse_ts(article.get("published_at")) if isinstance(article, dict) else None
        if published is not None and updated is not None and updated >= published:
            end_to_end.append((updated - published).total_seconds())

    report = {
        "articles_seen": len(articles),
        "classified": len(first_attempt),
        "stages": {
            "publish_to_fetch": _percentiles(publish_to_fetch),
            "fetch_to_first_classify": _percentiles(fetch_to_classify),
            "classify_duration": _percentiles(classify_duration),
            "decision_to_order_complete": _percentiles(decision_to_order),
            "publish_to_order_complete": _percentiles(end_to_end),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


# ---- trades report ----

_ENTRY_ACTIONS = {"ENTER_YES", "ENTER_NO"}


def trades_report_command(*, data_root: Path = Path("data"), ledger_path: Path | None = None) -> int:
    """Per-trade table from the execution journals: every entry and exit with
    sizes, prices, proceeds, hold time, and per-market realized P&L where the
    journal carries both legs."""
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for record in _journal_records(data_root):
        by_bot.setdefault(record["bot"], []).append(record)

    trades: list[dict[str, Any]] = []
    for bot, records in sorted(by_bot.items()):
        records.sort(key=lambda item: str(item.get("created_at") or ""))
        open_entry: dict[str, Any] | None = None
        for record in records:
            payload = record.get("payload", {})
            row = {
                "bot": bot,
                "action": record.get("action"),
                "phase": record.get("phase"),
                "result": payload.get("result"),
                "at": record.get("updated_at"),
                "filled_shares": payload.get("filled_shares"),
                "entry_usd": payload.get("estimated_fill_usd"),
                "total_sold": payload.get("total_sold"),
                "proceeds_usd": payload.get("confirmed_proceeds"),
                "reason": (payload.get("decision") or {}).get("reason") if isinstance(payload.get("decision"), dict) else None,
                "pnl_usd": None,
                "hold_seconds": None,
            }
            if record.get("phase") != "completed":
                trades.append(row)
                continue
            if record.get("action") in _ENTRY_ACTIONS:
                open_entry = record
            elif open_entry is not None:
                entry_usd = open_entry.get("payload", {}).get("estimated_fill_usd")
                proceeds = payload.get("confirmed_proceeds")
                if isinstance(entry_usd, (int, float)) and isinstance(proceeds, (int, float)):
                    row["pnl_usd"] = round(float(proceeds) - float(entry_usd), 2)
                entered = _parse_ts(open_entry.get("updated_at"))
                exited = _parse_ts(record.get("updated_at"))
                if entered is not None and exited is not None and exited >= entered:
                    row["hold_seconds"] = round((exited - entered).total_seconds(), 1)
                if str(payload.get("result") or "") not in {"TRIMMED"}:
                    open_entry = None  # full exit closes the pairing; trims keep it open
            trades.append(row)

    realized = [t["pnl_usd"] for t in trades if isinstance(t["pnl_usd"], (int, float))]
    report: dict[str, Any] = {
        "bots": len(by_bot),
        "journal_records": sum(len(v) for v in by_bot.values()),
        "closed_trades_with_pnl": len(realized),
        "realized_pnl_from_journals": round(sum(realized), 2),
        "trades": trades,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if ledger_path is not None and ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(ledger, dict):
                report["ledger"] = {
                    "realized_net": ledger.get("realized_net", 0.0),
                    "realized_by_day": ledger.get("realized_by_day", {}),
                    "open_positions": ledger.get("open_positions", []),
                }
        except (OSError, json.JSONDecodeError):
            report["ledger"] = {"error": "unreadable"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _journal_records(data_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not data_root.exists():
        return records
    for journal_dir in sorted(data_root.rglob("execution_journal")):
        if not journal_dir.is_dir():
            continue
        bot = str(journal_dir.parent.relative_to(data_root))
        for path in sorted(journal_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            raw["bot"] = bot
            if not isinstance(raw.get("payload"), dict):
                raw["payload"] = {}
            records.append(raw)
    return records


__all__ = ["latency_report_command", "trades_report_command"]
