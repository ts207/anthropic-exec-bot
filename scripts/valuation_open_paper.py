"""Show the valuation strategy's OPEN paper positions and their P&L.

Usage: PYTHONPATH=. python3 scripts/valuation_open_paper.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

blob = json.loads(Path("data/valuation/ladder_paper_orders.json").read_text(encoding="utf-8"))
rows = blob if isinstance(blob, list) else next(
    (v for v in blob.values() if isinstance(v, list) and v and isinstance(v[0], dict)), []
)

status = Counter(str(r.get("status")) for r in rows)
mode = Counter(str(r.get("entryMode")) for r in rows)
low = sum(1 for r in rows if "-low-" in str(r.get("marketSlug", "")))
print(f"records={len(rows)}  status={dict(status)}  modes={dict(mode)}")
print(f"records on (LOW) falls-to markets: {low} / {len(rows)}")

pnl = [r.get("hypotheticalPnl") for r in rows if isinstance(r.get("hypotheticalPnl"), (int, float))]
if pnl:
    print(f"hypothetical P&L: n={len(pnl)} total={sum(pnl):.2f}")

print("\nper position:")
for r in rows:
    slug = str(r.get("marketSlug", ""))[:46]
    flag = "LOW!" if "-low-" in slug else "    "
    print(
        f"  {flag} {str(r.get('company'))[:11]:11} {slug:46} {str(r.get('entryMode'))[:22]:22} "
        f"status={str(r.get('status')):9} bid={r.get('passiveBidPrice')} size={r.get('sizeUsd')} "
        f"fill={r.get('fillPrice')} pnl={r.get('hypotheticalPnl')}"
    )
