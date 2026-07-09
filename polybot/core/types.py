from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Article:
    url: str
    domain: str
    title: str
    published_at: str | None
    fetched_at: str
    raw_text: str
    hash: str
    source_kind: str = "article"

__all__ = ["Article"]
