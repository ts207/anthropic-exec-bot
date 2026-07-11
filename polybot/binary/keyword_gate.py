from __future__ import annotations

from polybot.core.types import Article

from .config import BinaryBotConfig


def should_escalate_binary_article(article: Article, config: BinaryBotConfig) -> bool:
    """Cheap deterministic pre-filter run before any classifier spend.

    Unlike the iran/location gates (which hardcode domain vocabulary), the
    binary bot's gate is fully config-driven: `keywords.escalate_terms` should
    name the market's qualifying-event vocabulary (and its collapse terms).
    An empty list escalates everything and relies on the classifier budget.
    """
    terms = config.keywords.escalate_terms
    if not terms:
        return True
    text = f"{article.title}\n{article.raw_text}".lower()
    return any(term.lower() in text for term in terms if term.strip())
