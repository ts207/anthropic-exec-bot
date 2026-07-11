from __future__ import annotations

# Small curated registry mapping geopolitical actors to official domains and
# news-discovery hooks. Used by the fixture rule analyzer (party extraction)
# and the source-plan builder (official feeds per party). Deliberately
# conservative: unknown actors simply get wire + Google News coverage.

WIRE_DOMAINS = ["reuters.com", "apnews.com", "afp.com"]

# actor key -> (aliases for detection, official domains)
ACTORS: dict[str, tuple[list[str], list[str]]] = {
    "united_states": (["united states", "u.s.", "us ", "washington", "white house", "state department", "pentagon"], ["state.gov", "whitehouse.gov", "defense.gov"]),
    "iran": (["iran", "tehran", "iranian"], ["mfa.gov.ir"]),
    "israel": (["israel", "jerusalem", "israeli", "idf"], ["gov.il", "mfa.gov.il"]),
    "russia": (["russia", "moscow", "kremlin", "russian"], ["mid.ru", "kremlin.ru"]),
    "ukraine": (["ukraine", "kyiv", "ukrainian"], ["mfa.gov.ua", "president.gov.ua"]),
    "china": (["china", "beijing", "chinese", "prc"], ["fmprc.gov.cn"]),
    "taiwan": (["taiwan", "taipei"], ["mofa.gov.tw"]),
    "north_korea": (["north korea", "pyongyang", "dprk"], []),
    "south_korea": (["south korea", "seoul"], ["mofa.go.kr"]),
    "qatar": (["qatar", "doha", "qatari"], ["mofa.gov.qa"]),
    "oman": (["oman", "muscat", "omani"], ["fm.gov.om"]),
    "saudi_arabia": (["saudi", "riyadh"], ["mofa.gov.sa"]),
    "uae": (["united arab emirates", "abu dhabi", "emirati", "uae"], ["mofaic.gov.ae"]),
    "turkey": (["turkey", "türkiye", "ankara", "turkish"], ["mfa.gov.tr"]),
    "egypt": (["egypt", "cairo", "egyptian"], ["mfa.gov.eg"]),
    "pakistan": (["pakistan", "islamabad", "pakistani"], ["mofa.gov.pk"]),
    "india": (["india", "new delhi", "indian"], ["mea.gov.in"]),
    "switzerland": (["switzerland", "geneva", "bern", "swiss"], ["eda.admin.ch"]),
    "united_kingdom": (["united kingdom", "britain", "london", "british", "uk "], ["gov.uk"]),
    "france": (["france", "paris", "french"], ["diplomatie.gouv.fr"]),
    "germany": (["germany", "berlin", "german"], ["auswaertiges-amt.de"]),
    "european_union": (["european union", "brussels", "eu "], ["europa.eu"]),
    "united_nations": (["united nations", "security council", "un "], ["un.org"]),
    "nato": (["nato"], ["nato.int"]),
    "venezuela": (["venezuela", "caracas"], []),
    "gaza": (["gaza", "hamas"], []),
    "lebanon": (["lebanon", "beirut", "hezbollah"], []),
    "syria": (["syria", "damascus"], []),
    "yemen": (["yemen", "houthi", "sanaa"], []),
    "iraq": (["iraq", "baghdad"], []),
    "afghanistan": (["afghanistan", "kabul", "taliban"], []),
}

# Known mediator actors for diplomacy markets: appearing at all suggests a
# mediator role worth watching even when not a direct party.
MEDIATOR_ACTORS = ["qatar", "oman", "switzerland", "egypt", "turkey", "united_nations"]

# Decisive-event vocabulary by broad market family; the fixture analyzer uses
# these to seed escalate keywords when the rules match the family.
EVENT_FAMILIES: dict[str, list[str]] = {
    "talks": ["talks", "negotiations", "meeting", "summit", "round", "dialogue", "convene", "delegation"],
    "ceasefire": ["ceasefire", "truce", "cessation of hostilities", "armistice"],
    "strike": ["strike", "attack", "missile", "drone", "bomb", "airstrike"],
    "sanctions": ["sanction", "sanctions", "embargo", "export controls"],
    "election": ["election", "vote", "ballot", "runoff", "inaugurat"],
    "agreement": ["agreement", "deal", "treaty", "accord", "sign"],
    "leadership": ["resign", "impeach", "coup", "oust", "successor", "steps down"],
}


def detect_actors(text: str) -> list[str]:
    lowered = text.lower()
    found = [actor for actor, (aliases, _domains) in ACTORS.items() if any(alias in lowered for alias in aliases)]
    return sorted(found)


def detect_event_families(text: str) -> list[str]:
    lowered = text.lower()
    return sorted(family for family, terms in EVENT_FAMILIES.items() if any(term in lowered for term in terms))


def official_domains(actors: list[str]) -> list[str]:
    domains: list[str] = []
    for actor in actors:
        entry = ACTORS.get(actor)
        if entry:
            domains.extend(entry[1])
    return sorted(set(domains))


def google_news_rss(query: str) -> str:
    from urllib.parse import quote_plus

    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
