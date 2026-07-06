#!/bin/bash
cd ~/poly/anthropic-exec-bot
echo '=== decision summaries ==='
python3 -c '
import json
for line in open("logs/location_decisions.jsonl"):
    d = json.loads(line)
    a, dec = d["article"], d["decision"]
    print(a["domain"], "|", (a.get("title") or "")[:50], "|", dec["action"], "|", dec["reason"][:60])
'
echo '=== stray bot processes ==='
pgrep -af 'polybot.main run-location' || echo none
