#!/bin/bash
set -u
cd ~/poly/anthropic-exec-bot
PY=.venv/bin/python

summarize() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
out = {"decision": d["decision"]}
if "factors" in d and d["factors"]:
    f = d["factors"]
    out["confirmed"] = f["confirmed_location"]
    out["strength"] = f["evidence_strength"]
    out["round_status"] = f["round_status"]
print(json.dumps(out, indent=1))
'
}

echo '=== CASE A: Pakistan senior round confirmed (expect ROTATE_YES -> pakistan) ==='
timeout 180 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain reuters.com \
  --text 'Senior US and Iranian representatives began a new formal round of peace talks in Islamabad, Pakistan on Monday, both governments confirmed. The round convened senior negotiators authorized to direct diplomacy.' \
  | summarize

echo '=== CASE B: July 11 technical meeting in Pakistan (expect NO_ACTION) ==='
timeout 180 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain apnews.com \
  --text 'A technical, staff-level meeting between US and Iranian working groups is set for July 11 in Islamabad, Pakistan, officials said, to continue implementation discussions from the June Switzerland round.' \
  | summarize

echo '=== CASE C: talks called off entirely (expect EXIT_YES_ONLY) ==='
timeout 180 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain reuters.com \
  --text 'Iran announced it is withdrawing from the diplomatic process with the United States and that no further rounds of peace talks will be held, with negotiations called off indefinitely, both governments confirmed.' \
  | summarize
