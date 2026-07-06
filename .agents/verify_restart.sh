#!/bin/bash
sleep 30
cd ~/poly/anthropic-exec-bot
echo '--- new articles by domain (last 10) ---'
tail -10 logs/location_articles.jsonl 2>/dev/null | python3 -c '
import json, sys
for l in sys.stdin:
    a = json.loads(l)
    print(a.get("domain"), "|", a.get("source_kind"), "|", (a.get("title") or "")[:58])
'
echo '--- running process ---'
pgrep -af run-location-protection | grep python || echo none
