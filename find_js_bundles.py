import re
import requests

url = "https://www.aljazeera.com/news/liveblog/2026/7/6/iran-war-live-tehran-set-for-khameneis-procession-israel-bombs-lebanon"
r = requests.get(url, headers={"User-Agent": "polybot-iran-verify/1.0"}, timeout=20)
raw = r.text

srcs = sorted(set(re.findall(r'src="([^"]+\.js[^"]*)"', raw)))
print(f"{len(srcs)} script src values found")
for s in srcs:
    print(" ", s)
