#!/bin/bash
cd ~/poly/anthropic-exec-bot
timeout 60 .venv/bin/python -m polybot.main run-location-protection --config qatar-sept30-yes-protection.yaml
echo '=== decisions log (last 5) ==='
tail -5 logs/location_decisions.jsonl 2>/dev/null | python3 -c '
import json, sys
for line in sys.stdin:
    d = json.loads(line)
    a, dec = d["article"], d["decision"]
    print(a["domain"], "|", a["title"][:55], "|", dec["action"], dec["reason"][:55])
'
echo '=== state dir ==='
find data/location-protection-bot -type f 2>/dev/null | head
echo '=== articles stored ==='
wc -l logs/location_articles.jsonl 2>/dev/null
