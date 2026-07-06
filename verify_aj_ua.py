import requests
from polybot.iran.source_fetcher import _TextExtractor

url = "https://www.aljazeera.com/news/liveblog/2026/7/6/iran-war-live-tehran-set-for-khameneis-procession-israel-bombs-lebanon"

for ua in [
    "polybot-iran-verify/1.0",
    "polybot/0.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]:
    r = requests.get(url, headers={"User-Agent": ua}, timeout=20)
    parser = _TextExtractor()
    parser.feed(r.text)
    text = parser.text()
    print(f"UA={ua!r}")
    print(f"  status={r.status_code} html_len={len(r.text)} extracted_len={len(text)}")
    print(f"  extracted preview: {text[:200]!r}")
    print()
