#!/usr/bin/env bash
# Probe candidate news feeds for reachability, item count, and freshness.
# Latency is the confirmed-entry strategy's dominant cost, so a feed is only
# worth wiring in if it is live AND publishes promptly.
set -u

FEEDS=(
  "https://www.centcom.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=95&max=10"
  "https://www.whitehouse.gov/feed/"
  "https://www.whitehouse.gov/presidential-actions/feed/"
  "https://www.state.gov/rss-feed/press-releases/feed/"
  "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10"
  "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=800&Site=945&max=10"
  "https://apnews.com/index.rss"
  "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"
  "https://www.aljazeera.com/xml/rss/all.xml"
  "https://news.un.org/feed/subscribe/en/news/all/rss.xml"
  "https://www.reuters.com/world/middle-east/rss"
  "https://moderndiplomacy.eu/feed/"
)

for u in "${FEEDS[@]}"; do
  code=$(curl -s -o /tmp/probe.xml -w '%{http_code}' -L --max-time 12 -A 'Mozilla/5.0 (compatible; polybot/1.0)' "$u" 2>/dev/null)
  items=$(grep -c '<item' /tmp/probe.xml 2>/dev/null || echo 0)
  latest=$(grep -m1 -o '<pubDate>[^<]*' /tmp/probe.xml 2>/dev/null | cut -c10- | head -1)
  printf '%-4s items=%-4s latest=%-32s %s\n' "$code" "$items" "${latest:-none}" "$u"
done
