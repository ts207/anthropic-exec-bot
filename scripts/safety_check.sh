#!/usr/bin/env bash
# One-screen answer to "can anything actually spend money right now?"
# Run this after ANY config or service change. Every line should read SAFE.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "=== valuation strategy ==="
python3 - <<'EOF'
import json, glob
for path in glob.glob('configs/valuation/*.json'):
    d = json.load(open(path))
    mode = d.get('mode')
    print(f"  config: {path}")
    print(f"    mode={mode}  (alert_only => cannot place orders)")
    print(f"    globalUsdCap={d.get('globalUsdCap')} baseOrderUsd={d.get('baseOrderUsd')}")
EOF
echo "  service ExecStart:"
systemctl --user show polybot-valuation -p ExecStart --value 2>/dev/null | grep -o 'argv\[\]=[^;]*' | head -1

echo
echo "=== geopolitics fleet ==="
echo "  service ExecStart:"
systemctl --user show polybot-fleet -p ExecStart --value 2>/dev/null | grep -o 'argv\[\]=[^;]*' | head -1
echo "  (--live absent => paper posture)"
python3 - <<'EOF'
import yaml
cfg = yaml.safe_load(open('configs/geopolitics/discovery.yaml'))
ex = (cfg.get('execution') or {})
print(f"    execution.dry_run={ex.get('dry_run', 'unset (emitted per-bot as true)')}")
print(f"    allocator caps: total={(cfg.get('allocator') or {}).get('total_usd')} per_order={(cfg.get('allocator') or {}).get('per_order_usd')}")
EOF

echo
echo "=== operator gate (master switches) ==="
for f in data/operator/*.mode data/operator/positions/*.mode; do
  [ -e "$f" ] && echo "  $f = $(cat "$f")"
done
[ -e data/operator/halt.lock ] && echo "  HALT LOCK PRESENT" || echo "  no halt lock"

echo
echo "=== evidence of any real order ever placed ==="
found=0
for f in logs/orders.jsonl logs/manual_orders.jsonl data/valuation/ladder_paper_orders.json; do
  if [ -s "$f" ]; then
    n=$(wc -l < "$f" 2>/dev/null || echo 0)
    echo "  $f has $n line(s) -- inspect if unexpected"
    found=1
  fi
done
[ "$found" = "0" ] && echo "  no order journals with content"
