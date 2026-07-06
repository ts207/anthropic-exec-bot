"""One-shot status message to Telegram after the 2026-07-06 config refresh."""
import json
import sys
import urllib.request

sys.path.insert(0, "/home/tstuv/poly/anthropic-exec-bot")
from polybot.iran.notifier import TelegramNotifier  # noqa: E402

WATCH = {"Qatar", "Pakistan", "Switzerland", "Oman", "No Meeting by September 30"}


def market_lines() -> list[str]:
    req = urllib.request.Request(
        "https://gamma-api.polymarket.com/events/624242",
        headers={"User-Agent": "polybot-status/1.0"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    rows = []
    for m in data.get("markets", []):
        name = m.get("groupItemTitle") or ""
        if name in WATCH:
            price = float(json.loads(m.get("outcomePrices") or "[0]")[0])
            rows.append((price, f"  {name}: {price:.3f}"))
    return [line for _, line in sorted(rows, reverse=True)]


lines = [
    "Qatar bot restarted with refreshed config (dry-run, alert_only).",
    "",
    "Position: 3,255 Qatar-YES @ 0.2682 avg.",
    "Market now:",
    *market_lines(),
    "",
    "Config changes live:",
    "- Dawn + Reuters (Google News) feeds added",
    "- analyst_context: Dawn sequencing thesis + indirect-mediation nuance",
    "- ALL sources flattened to execution-grade (incl. feeds/X/IRNA)",
    "- 24h freshness gate + feed-summary gating enforced in code",
    "- no_meeting exit-path bug fixed; time-decay price floors enforced",
    "",
    "Still NOT live: dry_run=true, mode=alert_only, config hash needs re-ack.",
]

notifier = TelegramNotifier()
if not notifier.token or not notifier.chat_id:
    print("TELEGRAM NOT CONFIGURED — message not sent")
    sys.exit(1)
notifier.notify("\n".join(lines))
print("telegram status message sent")
