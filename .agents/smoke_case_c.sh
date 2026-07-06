#!/bin/bash
cd ~/poly/anthropic-exec-bot
timeout 180 .venv/bin/python -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain reuters.com \
  --text 'Iran announced it is withdrawing from the diplomatic process with the United States and that no further rounds of peace talks will be held, with negotiations called off indefinitely, both governments confirmed.' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["decision"], indent=1))'
