#!/bin/bash
cd ~/poly/anthropic-exec-bot
.venv/bin/python -m pytest -q 2>&1 | tail -4
echo '=== config loads + flat levels ==='
.venv/bin/python - <<'EOF'
from pathlib import Path
from polybot.location.config import load_location_config

c = load_location_config(Path("qatar-sept30-yes-protection.yaml"))
print("feed_auto_trade:", c.sources.allow_feed_auto_trade)
print("alert_only:", c.sources.alert_only_domains)
print("dawn/x/twitter/t.me/irna in auto_trade:",
      all(d in c.sources.auto_trade_domains for d in ["dawn.com", "x.com", "twitter.com", "t.me", "irna.ir"]))
print("max_trade_age_h:", c.sources.max_trade_article_age_hours)
EOF
