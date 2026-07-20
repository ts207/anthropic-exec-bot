"""Why is every valuation ladder plan ineligible? Dumps raw plan records.

Usage: PYTHONPATH=. python3 scripts/valuation_why_blocked.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

blob = json.loads(Path("data/valuation/ladder_paper_orders.json").read_text(encoding="utf-8"))
rows = blob if isinstance(blob, list) else next(
    (v for v in blob.values() if isinstance(v, list) and v and isinstance(v[0], dict)), []
)

print(f"records: {len(rows)}")
if rows:
    print(f"generatedAt(top-level): {blob.get('generatedAt') if isinstance(blob, dict) else 'n/a'}")
    print(f"\nfields present: {sorted(rows[0].keys())}\n")

blockers = Counter()
reasons = Counter()
for r in rows:
    for b in (r.get("blockers") or []):
        blockers[str(b)] += 1
    reasons[str(r.get("reason"))[:60]] += 1

print("reasons:")
for k, n in reasons.most_common(12):
    print(f"  {n:4}  {k}")
print("\nblockers:")
for k, n in blockers.most_common(12):
    print(f"  {n:4}  {k}")

print("\nsample records:")
for r in rows[:4]:
    keep = {
        k: r.get(k)
        for k in (
            "company", "marketSlug", "entryMode", "direction", "paperEligible", "liveEligible",
            "yesAsk", "yesBid", "modelFair", "passiveBidPrice", "distancePct", "reason", "blockers",
        )
        if k in r
    }
    print("  " + json.dumps(keep)[:400])
