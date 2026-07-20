"""What has the valuation (NPM ladder) strategy actually decided, on paper?

The July 31 ladders are the fastest source of real calibration evidence in the
whole system: they resolve on a scheduled public data print, so paper results
translate to live behaviour almost 1:1. This prints the paper order plans and
why each was or was not eligible.

Usage: PYTHONPATH=. python3 scripts/valuation_paper_report.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def load(path: str):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # tolerate jsonl
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


def rows_of(blob):
    if blob is None:
        return []
    if isinstance(blob, list):
        return [r for r in blob if isinstance(r, dict)]
    for key in ("orders", "plans", "entries", "candidates", "paperOrders"):
        val = blob.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    for val in blob.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return []


def main() -> None:
    for name in ("ladder_paper_orders.json", "forecast_paper_trades.json", "last_candidates.json"):
        blob = load(f"data/valuation/{name}")
        rows = rows_of(blob)
        print(f"\n=== {name}: {len(rows)} record(s)")
        if not rows:
            if blob is not None:
                print(f"    (top-level keys: {list(blob)[:8] if isinstance(blob, dict) else type(blob).__name__})")
            continue

        modes = Counter(r.get("entryMode") or r.get("signalType") or "?" for r in rows)
        print(f"    entry modes: {dict(modes)}")
        paper = Counter(str(r.get("paperEligible")) for r in rows if "paperEligible" in r)
        if paper:
            print(f"    paperEligible: {dict(paper)}")
        live = Counter(str(r.get("liveEligible")) for r in rows if "liveEligible" in r)
        if live:
            print(f"    liveEligible: {dict(live)}")

        blockers = Counter()
        for r in rows:
            for b in (r.get("blockers") or []):
                blockers[str(b)[:46]] += 1
        if blockers:
            print("    top blockers:")
            for b, n in blockers.most_common(8):
                print(f"      {n:4}  {b}")

        eligible = [r for r in rows if r.get("paperEligible") or r.get("liveEligible")]
        print(f"    ELIGIBLE PAPER/LIVE ENTRIES: {len(eligible)}")
        for r in eligible[:10]:
            print(
                f"      {str(r.get('company'))[:12]:12} {str(r.get('marketSlug'))[:44]:44} "
                f"mode={r.get('entryMode')} ask={r.get('yesAsk')} fair={r.get('modelFair')} "
                f"bid={r.get('passiveBidPrice')} reason={str(r.get('reason'))[:40]}"
            )


if __name__ == "__main__":
    main()
