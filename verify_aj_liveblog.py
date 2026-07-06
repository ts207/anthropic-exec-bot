from polybot.iran.source_fetcher import fetch_feed_articles, fetch_article

arts = fetch_feed_articles(
    "https://www.aljazeera.com/xml/rss/all.xml",
    "polybot-iran-verify/1.0",
    include_terms=None,
    exclude_terms=[],
    limit=15,
)
print(f"fetched {len(arts)} articles\n")
liveblog_item = None
for a in arts:
    print(a.published_at, "|", a.url)
    if "liveblog" in a.url or "iran-war-live" in a.url:
        liveblog_item = a

print("\n--- liveblog item found:", liveblog_item is not None, "---")
if liveblog_item:
    print("URL:", liveblog_item.url)
    print("Title:", liveblog_item.title)
    print("raw_text length from feed summary:", len(liveblog_item.raw_text))
    print("raw_text preview:", liveblog_item.raw_text[:500])

    print("\n--- now fetching full liveblog page via fetch_article (promotion path) ---")
    promoted = fetch_article(liveblog_item.url, "polybot-iran-verify/1.0")
    print("promoted title:", promoted.title)
    print("promoted raw_text length:", len(promoted.raw_text))
    print("promoted raw_text preview (first 1500 chars):")
    print(promoted.raw_text[:1500])
