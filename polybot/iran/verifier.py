from __future__ import annotations


def normalize(value: str) -> str:
    return " ".join(value.lower().split())


def quote_in_article(quote: str, article_text: str) -> bool:
    if not quote.strip():
        return False
    return normalize(quote) in normalize(article_text)

