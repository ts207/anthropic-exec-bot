from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from .storage import append_jsonl
from .types import Article


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._in_title:
            self.title += (" " if self.title else "") + text
        self.parts.append(text)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", " ".join(self.parts)).strip()


def fetch_article(url: str, user_agent: str = "polybot/0.1") -> Article:
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
    response.raise_for_status()
    parser = _TextExtractor()
    parser.feed(response.text)
    raw_text = parser.text()
    title = parser.title or _first_line(raw_text) or url
    fetched_at = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(f"{url}\n{title}\n{raw_text}".encode("utf-8")).hexdigest()
    return Article(
        url=url,
        domain=urlparse(url).netloc.lower().removeprefix("www."),
        title=title,
        published_at=None,
        fetched_at=fetched_at,
        raw_text=raw_text,
        hash=digest,
        source_kind="article",
    )


def fetch_feed_articles(
    feed_url: str,
    user_agent: str = "polybot/0.1",
    *,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    limit: int = 20,
) -> list[Article]:
    response = requests.get(feed_url, headers={"User-Agent": user_agent}, timeout=20)
    response.raise_for_status()
    if _looks_like_html(response.content):
        return []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return []
    articles: list[Article] = []
    for item in _feed_items(root):
        title = _clean_text(_child_text(item, "title"))
        summary = _clean_text(_child_text(item, "description") or _child_text(item, "summary") or _child_text(item, "content"))
        link = _feed_link(item) or feed_url
        published_at = _child_text(item, "pubDate") or _child_text(item, "published") or _child_text(item, "updated") or None
        source_url = _source_url(item)
        article_url = link
        domain_url = source_url or link
        raw_text = "\n".join(part for part in (title, summary) if part).strip()
        if not raw_text:
            continue
        if include_terms and not _matches_any(raw_text, include_terms):
            continue
        if exclude_terms and _matches_any(f"{domain_url}\n{raw_text}", exclude_terms):
            continue
        fetched_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(f"feed\n{article_url}\n{title}\n{published_at or ''}".encode("utf-8")).hexdigest()
        articles.append(
            Article(
                url=article_url,
                domain=urlparse(domain_url).netloc.lower().removeprefix("www."),
                title=title or _first_line(raw_text) or article_url,
                published_at=published_at,
                fetched_at=fetched_at,
                raw_text=raw_text,
                hash=digest,
                source_kind="feed",
            )
        )
        if len(articles) >= limit:
            break
    return articles


def resolve_google_news_url(url: str, user_agent: str = "polybot/0.1", timeout: float = 20.0) -> str | None:
    """Resolve a news.google.com/rss/articles/<id> JS-redirect URL to the real publisher URL.

    New-format Google News article URLs do not embed the target and do not HTTP-redirect;
    the target must be requested from the DotsSplashUi batchexecute endpoint using the
    signature/timestamp attributes embedded in the redirect page.
    """
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("news.google.com") or "/articles/" not in parsed.path:
        return None
    article_id = parsed.path.split("/articles/", 1)[1].split("/", 1)[0]
    if not article_id:
        return None
    page = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    page.raise_for_status()
    signature = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not signature or not timestamp:
        return None
    payload = [
        "garturlreq",
        [
            ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
        ],
        article_id,
        int(timestamp.group(1)),
        signature.group(1),
    ]
    response = requests.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "User-Agent": user_agent},
        data={"f.req": json.dumps([[["Fbv4je", json.dumps(payload), None, "generic"]]])},
        timeout=timeout,
    )
    response.raise_for_status()
    chunks = response.text.split("\n\n")
    body = chunks[1] if len(chunks) > 1 else response.text
    match = re.search(r'https?://(?!news\.google)[^\\"]+', body)
    return match.group(0) if match else None


def promote_feed_article(article: Article, user_agent: str = "polybot/0.1") -> Article | None:
    if article.source_kind != "feed":
        return None
    if not article.url.startswith(("http://", "https://")):
        return None
    target_url = article.url
    if urlparse(target_url).netloc.lower().endswith("news.google.com"):
        try:
            resolved = resolve_google_news_url(target_url, user_agent)
        except requests.RequestException:
            return None
        if resolved is None:
            return None
        target_url = resolved
    try:
        promoted = fetch_article(target_url, user_agent)
    except requests.RequestException:
        # Publishers like reuters.com reject direct fetches; the resolved URL and
        # domain are still useful for alerts, but the text is only feed-derived.
        return _promote_with_feed_text(article, target_url)
    if not promoted.raw_text:
        return _promote_with_feed_text(article, target_url)
    return Article(
        url=promoted.url,
        domain=promoted.domain,
        title=promoted.title or article.title,
        published_at=article.published_at,
        fetched_at=promoted.fetched_at,
        raw_text=promoted.raw_text,
        hash=hashlib.sha256(f"promoted\n{promoted.url}\n{promoted.title}\n{promoted.raw_text}".encode("utf-8")).hexdigest(),
        source_kind="article",
    )


def _promote_with_feed_text(article: Article, target_url: str) -> Article | None:
    if not article.raw_text.strip():
        return None
    return Article(
        url=target_url,
        domain=urlparse(target_url).netloc.lower().removeprefix("www."),
        title=article.title,
        published_at=article.published_at,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=article.raw_text,
        hash=hashlib.sha256(f"promoted-summary\n{target_url}\n{article.title}\n{article.raw_text}".encode("utf-8")).hexdigest(),
        source_kind="promoted_feed_summary",
    )


class ArticleStore:
    def __init__(self, articles_path: Path):
        self.articles_path = articles_path
        self._seen = self._load_seen()

    def is_seen(self, article: Article) -> bool:
        return article.hash in self._seen

    def store(self, article: Article) -> bool:
        if self.is_seen(article):
            return False
        append_jsonl(self.articles_path, article.__dict__)
        self._seen.add(article.hash)
        return True

    def _load_seen(self) -> set[str]:
        if not self.articles_path.exists():
            return set()
        seen: set[str] = set()
        for line in self.articles_path.read_text(encoding="utf-8").splitlines():
            match = re.search(r'"hash":"([^"]+)"', line)
            if match:
                seen.add(match.group(1))
        return seen


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:180]
    return ""


def _feed_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    items = root.findall(".//item")
    if items:
        return items
    return root.findall(".//{http://www.w3.org/2005/Atom}entry") + root.findall(".//entry")


def _child_text(item: ElementTree.Element, name: str) -> str:
    child = item.find(name)
    if child is None:
        child = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    if child is None:
        child = item.find(f"{{http://purl.org/rss/1.0/modules/content/}}encoded")
    if child is None:
        return ""
    return child.text or ""


def _feed_link(item: ElementTree.Element) -> str | None:
    text_link = _child_text(item, "link").strip()
    if text_link:
        return text_link
    for link in item.findall("{http://www.w3.org/2005/Atom}link") + item.findall("link"):
        href = link.attrib.get("href")
        if href:
            return href
    return None


def _source_url(item: ElementTree.Element) -> str | None:
    source = item.find("source")
    if source is None:
        source = item.find("{http://www.w3.org/2005/Atom}source")
    if source is None:
        return None
    url = source.attrib.get("url") or source.attrib.get("href")
    if url:
        return url
    text = (source.text or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _clean_text(value: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", stripped).strip()


def _matches_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _looks_like_html(content: bytes) -> bool:
    prefix = content[:200].lstrip().lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")
