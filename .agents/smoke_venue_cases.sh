#!/bin/bash
set -u
cd ~/poly/anthropic-exec-bot
PY=.venv/bin/python

summarize() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
out = {"decision": d.get("decision"), "ok": d.get("ok")}
f = d.get("factors")
if f:
    out |= {k: f[k] for k in ("confirmed_location", "round_status", "evidence_strength", "qualifies_as_senior_round", "source_tier")}
if not d.get("ok"):
    out["error"] = d.get("error")
print(json.dumps(out, indent=1))
'
}

echo '=== 1. Dawn-style: technical Islamabad + senior Doha EXPECTED (expect: qatar signal, NO trade) ==='
timeout 400 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain dawn.com \
  --text 'Technical negotiations between US and Iranian teams are expected in Islamabad on July 11, officials familiar with the process said. The next high-level direct talks between the two sides are expected to take place in Doha during the third week of July, after technical teams finish details of the agreement.' \
  | summarize

echo '=== 2. Doha technical only (expect: technical_only, NO_ACTION) ==='
timeout 400 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain reuters.com \
  --text 'US and Iranian technical teams continued implementation talks in Doha over Strait of Hormuz shipping and frozen funds, officials said. The nuclear file was not discussed and no senior-level meeting has been scheduled.' \
  | summarize

echo '=== 3. KILL: July 11 upgraded to senior round in Islamabad (expect: ROTATE_YES -> pakistan) ==='
timeout 400 $PY -m polybot.main smoke-location-classifier \
  --config qatar-sept30-yes-protection.yaml --domain reuters.com \
  --text 'Both governments confirmed the July 11 Islamabad meeting has been upgraded to a formal senior-level round of US-Iran peace talks. US envoy Steve Witkoff and Iranian Foreign Minister Abbas Araqchi will attend with full negotiating mandates, convening a new round of the peace process in Pakistan.' \
  | summarize
