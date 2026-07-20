from __future__ import annotations

# Small curated registry mapping geopolitical actors to official domains and
# news-discovery hooks. Used by the fixture rule analyzer (party extraction)
# and the source-plan builder (official feeds per party). Deliberately
# conservative: unknown actors simply get wire + Google News coverage.

WIRE_DOMAINS = ["reuters.com", "apnews.com", "afp.com"]

# actor key -> (aliases for detection, official domains)
ACTORS: dict[str, tuple[list[str], list[str]]] = {
    # war.gov: defense.gov now 301-redirects to war.gov ("Department of War"),
    # so Pentagon announcements arrive on a host that does NOT match
    # "defense.gov" under domain_allowed()'s exact/suffix rule -- the decisive
    # source for the Iran halt-in-offensive markets would have been demoted to
    # alert-only. Both are listed until the rename fully settles.
    "united_states": (["united states", "u.s.", "us ", "washington", "white house", "state department", "pentagon", "department of war"], ["state.gov", "whitehouse.gov", "defense.gov", "war.gov", "centcom.mil"]),
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

# Direct publisher RSS endpoints -- minutes faster than Google News indexing,
# which is the dominant latency in the confirmed-entry race. Only feeds with
# stable public URLs are listed; wires without public RSS still go through
# Google News queries.
# VERIFIED 2026-07-20 with scripts/probe_feeds.sh -- every URL here returned
# HTTP 200 AND a non-zero <item> count. The previous two entries
# (state.gov/rss-feed/press-releases, news.un.org) both returned 200 with ZERO
# items: the system believed it had fast official sources and was actually
# receiving nothing, silently leaving Google News (5-15 min indexing lag) as
# the only path. Re-probe before trusting any addition here.
DIRECT_ACTOR_FEEDS: dict[str, list[str]] = {
    "united_states": [
        # Pentagon news + press releases (defense.gov redirects here).
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20",
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=800&Site=945&max=20",
        # Formal presidential actions (EOs, proclamations, memoranda).
        "https://www.whitehouse.gov/presidential-actions/feed/",
    ],
}

GENERAL_FAST_FEEDS: list[str] = [
    # BBC Middle East: own-infrastructure RSS, publishes within minutes, and
    # measured freshest of every candidate probed (latest item minutes old vs
    # Al Jazeera's, on a separate route from both Google and Al Jazeera).
    # Alert-grade by domain policy, same as Al Jazeera -- it buys reaction
    # time, it does not authorize a trade.
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    # Al Jazeera publishes to its own RSS immediately and covers Middle East
    # diplomacy earlier than most; alert/hold-signal grade by default (domain
    # policy still decides whether it may auto-trade).
    "https://www.aljazeera.com/xml/rss/all.xml",
]


def direct_feeds(actors: list[str]) -> list[str]:
    feeds: list[str] = []
    for actor in actors:
        feeds.extend(DIRECT_ACTOR_FEEDS.get(actor, []))
    return feeds

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


# Broad region buckets for the portfolio's second correlation dimension:
# different party sets in one theater still move together on contagion.
ACTOR_REGIONS: dict[str, str] = {
    "united_states": "north_america",
    "iran": "middle_east", "israel": "middle_east", "qatar": "middle_east",
    "oman": "middle_east", "saudi_arabia": "middle_east", "uae": "middle_east",
    "egypt": "middle_east", "gaza": "middle_east", "lebanon": "middle_east",
    "syria": "middle_east", "yemen": "middle_east", "iraq": "middle_east",
    "turkey": "middle_east",
    "russia": "eastern_europe", "ukraine": "eastern_europe",
    "china": "east_asia", "taiwan": "east_asia", "north_korea": "east_asia", "south_korea": "east_asia",
    "pakistan": "south_asia", "india": "south_asia", "afghanistan": "south_asia",
    "united_kingdom": "western_europe", "france": "western_europe",
    "germany": "western_europe", "switzerland": "western_europe", "european_union": "western_europe",
    "venezuela": "south_america",
}


def region_of(actors: list[str]) -> str:
    """Majority region of the deciding actors (global institutions and the US
    are weighted last so 'us + iran' lands in middle_east, not a tie)."""
    from collections import Counter

    weighted = [ACTOR_REGIONS[a] for a in actors if a in ACTOR_REGIONS and a not in ("united_states", "united_nations", "nato")]
    if not weighted:
        weighted = [ACTOR_REGIONS[a] for a in actors if a in ACTOR_REGIONS]
    if not weighted:
        return "global"
    return Counter(weighted).most_common(1)[0][0]


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


def bing_news_rss(query: str) -> str:
    from urllib.parse import quote_plus

    # Second aggregator on separate (Microsoft) infrastructure. A degraded
    # route to Google must not blind discovery: an ISP peering fault toward
    # Google timed out every news.google.com feed for days while the rest of
    # the internet stayed reachable, leaving Al Jazeera as the only live feed.
    return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
