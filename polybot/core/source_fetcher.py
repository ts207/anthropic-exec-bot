from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from polybot.core.storage import append_jsonl
from polybot.core.types import Article


_ALWAYS_CHROME_TAGS = ["script", "style", "noscript", "nav", "aside", "form", "button", "figure", "figcaption"]
# Some article templates (e.g. Al Jazeera liveblogs) put the real headline/lede
# copy inside a per-article <header> nested under <main>/<article>. Only strip
# <header>/<footer> when they sit outside the chosen main content region, so
# site-wide nav/footer chrome is removed without eating legitimate lede text.
_SITE_CHROME_TAGS = ["header", "footer"]
_MAIN_TAGS = ["article", "main"]
_MIN_MAIN_TEXT_CHARS = 400
_FULL_TEXT_FEED_MIN_WORDS = 120


class _TextExtractor:
    """Extracts title/body text from HTML using a real, lenient HTML parser.

    Real-world news markup (inline SVG icons, hydration scaffolding, etc.) is
    routinely technically malformed. A hand-rolled stdlib html.parser subclass
    used to back this class and was observed to silently swallow large spans
    of real article text (including whole liveblog updates) when it hit a
    single mismatched/unbalanced tag or quote, with no error raised. lxml's
    HTML parser (via BeautifulSoup) recovers from that kind of malformed
    markup the way browsers do, so it is used here instead.
    """

    def __init__(self) -> None:
        self.title = ""
        self._text = ""

    def feed(self, markup: str) -> None:
        soup = BeautifulSoup(markup, "lxml")
        title_tag = soup.find("title")
        if title_tag is not None:
            self.title = " ".join(title_tag.get_text().split())

        # Locate the main content region before removing anything, so we know
        # what's safe to protect from the header/footer chrome pass below.
        main = None
        for name in _MAIN_TAGS:
            main = soup.find(name)
            if main is not None:
                break

        # Always-chrome elements (nav, scripts, share buttons, figures, ...)
        # are never legitimate article body text, wherever they sit.
        for tag in soup.find_all(_ALWAYS_CHROME_TAGS):
            tag.decompose()

        # header/footer are only chrome when they're site-level (outside the
        # main content region); some templates nest the real lede inside a
        # per-article <header> under <main>, which must survive this pass.
        for tag in soup.find_all(_SITE_CHROME_TAGS):
            if main is None or not _is_descendant(tag, main):
                tag.decompose()

        main_text = _clean_extracted_text(main.get_text(separator="\n")) if main is not None else ""
        if len(main_text) >= _MIN_MAIN_TEXT_CHARS:
            self._text = main_text
            return

        body = soup.find("body") or soup
        self._text = _clean_extracted_text(body.get_text(separator="\n"))

    def text(self) -> str:
        return self._text


def _clean_extracted_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_descendant(node, ancestor) -> bool:
    for parent in node.parents:
        if parent is ancestor:
            return True
    return False


def _extract_published_at(markup: str) -> str | None:
    """Best-effort extraction of a machine-readable article timestamp.

    Directly fetched publisher pages often include freshness metadata in Open
    Graph, plain meta tags, <time datetime>, or JSON-LD. The trading bots treat
    missing timestamps conservatively, so this function should return None when
    the page is ambiguous instead of guessing from page text.

    Returns the MOST RECENT of every valid timestamp found (not the first
    match). Long-running liveblogs keep their original article:published_time
    pinned to when the page first went live while article:modified_time /
    dateModified keeps advancing as new entries are added; a first-match
    strategy would permanently understate freshness and eventually trip the
    staleness gate even as the page keeps getting genuinely new updates.
    Candidates further in the future than a small clock-skew tolerance are
    discarded as bogus rather than treated as "freshest".
    """
    soup = BeautifulSoup(markup, "lxml")
    meta_keys = [
        "article:published_time",
        "article:modified_time",
        "og:published_time",
        "og:updated_time",
        "datePublished",
        "dateModified",
        "pubdate",
        "publishdate",
        "timestamp",
        "dc.date",
        "dcterms.created",
        "sailthru.date",
    ]
    candidates: list[datetime] = []

    def _consider(normalized: str | None) -> None:
        if not normalized:
            return
        try:
            candidates.append(datetime.fromisoformat(normalized))
        except ValueError:
            pass

    for key in meta_keys:
        _consider(_normalize_datetime(_meta_content(soup, key)))

    for tag in soup.find_all("time"):
        for attr in ("datetime", "content"):
            _consider(_normalize_datetime(str(tag.get(attr) or "")))

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        for candidate in _jsonld_date_candidates(raw):
            _consider(_normalize_datetime(candidate))

    if not candidates:
        return None
    now = datetime.now(timezone.utc)
    tolerance_seconds = 300
    valid = [dt for dt in candidates if (dt - now).total_seconds() <= tolerance_seconds]
    if not valid:
        return None
    return max(valid).isoformat()


def _meta_content(soup: BeautifulSoup, key: str) -> str | None:
    key_lower = key.lower()
    for attrs in ({"property": key}, {"name": key}, {"itemprop": key}):
        tag = soup.find("meta", attrs=attrs)
        if tag is not None:
            value = tag.get("content")
            if value:
                return str(value)
    for tag in soup.find_all("meta"):
        names = [str(tag.get(attr) or "").lower() for attr in ("property", "name", "itemprop")]
        if key_lower in names:
            value = tag.get("content")
            if value:
                return str(value)
    return None


def _jsonld_date_candidates(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key in ("datePublished", "dateModified", "uploadDate"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    out.append(candidate)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(parsed)
    return out


def _normalize_datetime(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _apollo_state(markup: str) -> dict | None:
    """Decode a React/Apollo SSR state blob embedded as base64 in the page.

    Some templates (observed on Al Jazeera liveblogs) render only a truncated
    preview of the real post body in the DOM, collapsed behind a client-side
    "Read more" toggle; the full body text exists only in this hydration
    state. Returns None for pages that don't use this pattern (the common
    case), so callers must treat it as a best-effort enrichment, not a
    required step.
    """
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*"([^"]*)"', markup)
    if not match:
        return None
    try:
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
        state = json.loads(decoded)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _apollo_post_text(state: dict, url: str) -> str | None:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    for value in state.values():
        if not isinstance(value, dict) or value.get("__typename") != "Post":
            continue
        if value.get("slug") != slug:
            continue
        content_html = value.get("content")
        if not content_html:
            continue
        body_text = _clean_extracted_text(BeautifulSoup(content_html, "lxml").get_text(separator="\n"))
        if not body_text:
            continue
        parts = [part for part in (value.get("title"), value.get("subheading"), body_text) if part]
        return "\n\n".join(parts)
    return None


def _normalize_fetch_url(url: str) -> str:
    """dawn.com returns 403 for slugged article URLs (/news/<id>/<slug>) to
    non-browser clients but 200 for the bare /news/<id> form, so truncate Dawn
    article paths to the numeric id. Without this Dawn never fetches to full
    text and stays a promoted_feed_summary (never classified)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "dawn.com" or host.endswith(".dawn.com"):
        match = re.match(r"(/news/\d+)(?:/.*)?$", parsed.path)
        if match:
            return parsed._replace(path=match.group(1), query="", fragment="").geturl()
    return url


def fetch_article(url: str, user_agent: str = "polybot/0.1") -> Article:
    url = _normalize_fetch_url(url)
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
    response.raise_for_status()
    markup = response.text
    parser = _TextExtractor()
    parser.feed(markup)
    raw_text = parser.text()
    title = parser.title or _first_line(raw_text) or url
    published_at = _extract_published_at(markup)

    # Prefer the untruncated Apollo-state body over the DOM text whenever it's
    # available and actually more complete; never regress below the DOM parse.
    state = _apollo_state(markup)
    if state is not None:
        apollo_text = _apollo_post_text(state, url)
        if apollo_text and len(apollo_text) > len(raw_text):
            raw_text = apollo_text
    fetched_at = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(f"{url}\n{title}\n{raw_text}".encode("utf-8")).hexdigest()
    return Article(
        url=url,
        domain=urlparse(url).netloc.lower().removeprefix("www."),
        title=title,
        published_at=published_at,
        fetched_at=fetched_at,
        raw_text=raw_text,
        hash=digest,
        source_kind="article",
    )


def fetch_listing_article_urls(url: str, user_agent: str = "polybot/0.1", *, limit: int = 10) -> list[str]:
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
    response.raise_for_status()
    return extract_listing_article_urls(url, response.text, limit=limit)


def extract_listing_article_urls(url: str, markup: str, *, limit: int = 10) -> list[str]:
    base = urlparse(url)
    soup = BeautifulSoup(markup, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for link in soup.find_all("a"):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue
        absolute = urljoin(url, href).split("#", 1)[0]
        parsed = urlparse(absolute)
        if parsed.netloc.lower().removeprefix("www.") != base.netloc.lower().removeprefix("www."):
            continue
        if absolute.rstrip("/") == url.rstrip("/"):
            continue
        if not _looks_like_news_article_path(parsed.path):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls


# Conditional-GET state per feed URL (in-process). ETag/Last-Modified turn an
# unchanged feed into a ~50ms 304 instead of a full download+parse, which is
# what makes second-scale armed polling affordable for publishers and for us.
_FEED_CONDITIONAL: dict[str, dict[str, str]] = {}
_FEED_CONDITIONAL_LOCK = threading.Lock()


def fetch_feed_articles(
    feed_url: str,
    user_agent: str = "polybot/0.1",
    *,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    limit: int = 20,
) -> list[Article]:
    headers = {"User-Agent": user_agent}
    with _FEED_CONDITIONAL_LOCK:
        cached = dict(_FEED_CONDITIONAL.get(feed_url, {}))
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    response = requests.get(feed_url, headers=headers, timeout=20)
    if getattr(response, "status_code", 200) == 304:
        return []
    response.raise_for_status()
    response_headers = getattr(response, "headers", {}) or {}
    validators = {}
    if response_headers.get("ETag"):
        validators["etag"] = response_headers["ETag"]
    if response_headers.get("Last-Modified"):
        validators["last_modified"] = response_headers["Last-Modified"]
    with _FEED_CONDITIONAL_LOCK:
        if validators:
            _FEED_CONDITIONAL[feed_url] = validators
        else:
            _FEED_CONDITIONAL.pop(feed_url, None)
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
        # domain are still useful for alerts. First-party feeds such as Dawn's
        # can carry the article body in the feed, so those should remain
        # classifier-eligible even when the web page blocks direct fetches.
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
    source_kind = "article" if _looks_like_full_text_first_party_feed(article, target_url) else "promoted_feed_summary"
    return Article(
        url=target_url,
        domain=urlparse(target_url).netloc.lower().removeprefix("www."),
        title=article.title,
        published_at=article.published_at,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=article.raw_text,
        hash=hashlib.sha256(f"promoted-summary\n{target_url}\n{article.title}\n{article.raw_text}".encode("utf-8")).hexdigest(),
        source_kind=source_kind,
    )


def _looks_like_full_text_first_party_feed(article: Article, target_url: str) -> bool:
    original_domain = urlparse(article.url).netloc.lower().removeprefix("www.")
    target_domain = urlparse(target_url).netloc.lower().removeprefix("www.")
    if not original_domain or original_domain.endswith("news.google.com") or original_domain != target_domain:
        return False
    return len(re.findall(r"\w+", article.raw_text)) >= _FULL_TEXT_FEED_MIN_WORDS


class ArticleStore:
    def __init__(self, articles_path: Path):
        self.articles_path = articles_path
        self._seen, self._seen_content = self._load_seen()

    def is_seen(self, article: Article) -> bool:
        return article.hash in self._seen or _content_fingerprint(article) in self._seen_content

    def store(self, article: Article) -> bool:
        if self.is_seen(article):
            return False
        append_jsonl(self.articles_path, article.__dict__)
        self._seen.add(article.hash)
        self._seen_content.add(_content_fingerprint(article))
        return True

    def _load_seen(self) -> tuple[set[str], set[str]]:
        if not self.articles_path.exists():
            return set(), set()
        seen: set[str] = set()
        seen_content: set[str] = set()
        for line in self.articles_path.read_text(encoding="utf-8").splitlines():
            match = re.search(r'"hash":"([^"]+)"', line)
            if match:
                seen.add(match.group(1))
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                seen_content.add(_content_fingerprint(
                    Article(
                        url=str(raw.get("url") or ""),
                        domain=str(raw.get("domain") or ""),
                        title=str(raw.get("title") or ""),
                        published_at=raw.get("published_at") if isinstance(raw.get("published_at"), str) else None,
                        fetched_at=str(raw.get("fetched_at") or ""),
                        raw_text=str(raw.get("raw_text") or ""),
                        hash=str(raw.get("hash") or ""),
                        source_kind=str(raw.get("source_kind") or "article"),
                    )
                ))
        return seen, seen_content


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


def _content_fingerprint(article: Article) -> str:
    normalized = re.sub(r"\s+", " ", f"{article.source_kind}\n{article.title}\n{article.raw_text}".strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _looks_like_news_article_path(path: str) -> bool:
    return bool(re.search(r"/(?:news|features|opinions|opinion|program|gallery|video|liveblog)/", path))


def _looks_like_html(content: bytes) -> bool:
    prefix = content[:200].lstrip().lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")
