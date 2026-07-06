import json, sys, urllib.request

url = "https://gamma-api.polymarket.com/events/624242"
req = urllib.request.Request(url, headers={"User-Agent": "polybot-inspect/1.0"})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
rows = []
for m in data.get("markets", []):
    q = m.get("groupItemTitle") or m.get("question")
    prices = json.loads(m.get("outcomePrices") or "[0,0]")
    rows.append((q, float(prices[0]), m.get("bestBid"), m.get("bestAsk"),
                 m.get("active"), m.get("acceptingOrders"), m.get("closed")))
rows.sort(key=lambda r: -r[1])
print(f"{'outcome':36}{'yes':>8}{'bid':>8}{'ask':>8}  act/acc/closed")
for q, y, b, a, act, acc, cl in rows:
    bs = f"{b:.4f}" if b is not None else "-"
    asr = f"{a:.4f}" if a is not None else "-"
    print(f"{str(q)[:35]:36}{y:>8.4f}{bs:>8}{asr:>8}  {act}/{acc}/{cl}")
print("event:", data.get("title"), "| end:", data.get("endDate"))
total = sum(r[1] for r in rows)
print(f"sum of YES prices: {total:.4f}")
