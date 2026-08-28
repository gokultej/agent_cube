import os
import json
import requests
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")
G_NEWS_API_KEY = os.getenv("G_NEWS_API_KEY")

# Keywords mapped to transformer materials
KEYWORD_MATERIAL_MAP = {
    "copper tariff":         ["Copper"],
    "LME copper":            ["Copper"],
    "copper price":          ["Copper"],
    "aluminium supply":      ["Aluminium"],
    "aluminum tariff":       ["Aluminium"],
    "CRGO steel China":      ["CRGO Steel"],
    "grain oriented steel":  ["CRGO Steel"],
    "amorphous core":        ["Amorphous"],
    "amorphous transformer": ["Amorphous"],
    "amdt transformer":      ["Amorphous"],
    "transformer oil":       ["Transformer Oil"],
    "crude oil India":       ["Transformer Oil"],
    "Red Sea shipping":      ["Transformer Oil", "Insulation Paper", "All"],
    "US China trade":        ["Copper", "CRGO Steel"],
    "India power grid":      ["All"],
    "power sector capex":    ["All"],
    "rupee dollar":          ["Copper", "Aluminium", "Transformer Oil"],
    "SAIL steel price":      ["HR Steel"],
    "JSW steel":             ["HR Steel"],
    "IEEMA":                 ["All"],
}

SEVERITY_KEYWORDS = {
    "CRITICAL": ["all-time high", "ban", "embargo", "collapse", "shortage", "crisis",
                 "record high", "supply crunch", "halt"],
    "HIGH":     ["tariff", "disruption", "surge", "escalation", "spike", "deficit",
                 "restrict", "shortage"],
    "MEDIUM":   ["increase", "rise", "concern", "pressure", "tension", "uncertainty"],
    "LOW":      ["stable", "recover", "ease", "moderate", "normal", "neutral"],
}

# Keep only commodity/policy/supply-chain events relevant to transformer materials.
RELEVANCE_POSITIVE_TERMS = (
    "copper", "aluminium", "aluminum", "crgo", "grain oriented", "steel", "amorphous",
    "transformer oil", "insulation paper", "lme", "commodity", "metal",
    "tariff", "import duty", "export", "trade", "sanction", "embargo",
    "supply chain", "shipping", "freight", "red sea", "container",
    "power grid", "capex", "ieema", "currency", "rupee", "dollar",
    "factory", "plant", "smelter", "refinery",
)

RELEVANCE_NEGATIVE_TERMS = (
    "wedding", "marriage", "bride", "groom", "honeymoon", "fashion", "celebrity",
    "movie", "music", "cricket", "football", "recipe", "travel", "lifestyle",
    "festival", "astrology",
)

def classify_severity(text):
    text_lower = text.lower()
    for level in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        if any(kw in text_lower for kw in SEVERITY_KEYWORDS[level]):
            return level
    return "LOW"

def classify_impact_type(text):
    text_lower = text.lower()
    if any(w in text_lower for w in ["price rise", "cost increase", "tariff", "high", "surge", "spike"]):
        return "COST_INCREASE"
    if any(w in text_lower for w in ["price fall", "decline", "drop", "ease", "decrease"]):
        return "COST_DECREASE"
    if any(w in text_lower for w in ["supply disruption", "shortage", "halt", "ban", "embargo"]):
        return "SUPPLY_RISK"
    if any(w in text_lower for w in ["demand", "capex", "order boom", "growth"]):
        return "DEMAND_SURGE"
    return "NEUTRAL"

def suggest_action(severity, impact_type, materials):
    if severity == "CRITICAL" and impact_type == "COST_INCREASE":
        return "Cover 2-week immediate need only. Avoid bulk purchase. Renegotiate open POs."
    if severity == "CRITICAL" and impact_type == "SUPPLY_RISK":
        return "Build 4-week safety stock immediately. Identify alternative vendors."
    if impact_type == "DEMAND_SURGE":
        return "Forward buy 60-90 day requirement now before demand absorbs vendor capacity."
    if impact_type == "COST_DECREASE":
        return "Defer non-urgent procurement. Wait for price stabilisation."
    if impact_type == "COST_INCREASE" and severity == "HIGH":
        return "Accelerate procurement of 30-day buffer stock at current prices."
    return "Monitor weekly. No immediate action required."


def _infer_materials_from_text(text):
    """Infer affected materials from keyword hits in title/summary text."""
    text_lower = text.lower()
    mats = set()
    for keyword, keyword_mats in KEYWORD_MATERIAL_MAP.items():
        if keyword.lower() in text_lower:
            mats.update(keyword_mats)
    if not mats:
        mats.add("All")
    return sorted(mats)


def _is_relevant_article(text: str, materials: list[str]) -> bool:
    """
    Filter out generic/lifestyle news and keep material-market relevant stories only.
    """
    text_lower = text.lower()

    # Hard reject obvious non-business content.
    if any(term in text_lower for term in RELEVANCE_NEGATIVE_TERMS):
        return False

    # Keep if material inference found specific material beyond generic "All".
    if any(m != "All" for m in materials):
        return True

    # Otherwise require at least one market/supply/policy signal.
    return any(term in text_lower for term in RELEVANCE_POSITIVE_TERMS)


def _map_articles_to_events(articles):
    """Normalize article payloads (NewsAPI/GNews) to internal event schema."""
    events = []
    seen_titles = set()
    for art in articles:
        title = (art.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)

        summary = (art.get("description") or "").strip()
        full_text = f"{title} {summary}"
        materials = _infer_materials_from_text(full_text)
        if not _is_relevant_article(full_text, materials):
            continue
        severity = classify_severity(full_text)
        impact = classify_impact_type(full_text)
        action = suggest_action(severity, impact, materials)
        source = art.get("source") or {}

        events.append({
            "title": title,
            "summary": summary,
            "source": source.get("name", "") if isinstance(source, dict) else str(source),
            "url": art.get("url", ""),
            "published": (art.get("publishedAt") or "")[:10],
            "keyword": None,
            "affected_materials": materials,
            "severity": severity,
            "impact_type": impact,
            "recommended_action": action,
            "badge_color": {
                "CRITICAL": "RED",
                "HIGH": "AMBER",
                "MEDIUM": "BLUE",
                "LOW": "GREEN"
            }.get(severity, "GRAY")
        })
    return events


def _store_fallback_output(provider: str, payload: dict):
    """Persist fallback provider output/debug metadata to logs/ for audit."""
    try:
        os.makedirs("logs", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join("logs", f"events_fallback_{provider}_{ts}.json")
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        log.info("Stored fallback output: %s", path)
    except Exception as e:
        log.warning("Failed to store fallback output: %s", e)


def _fetch_newsapi_articles(from_date: str):
    if not NEWSAPI_KEY:
        raise RuntimeError("NEWSAPI_KEY not set")
    quoted_terms = [f"\"{k}\"" for k in KEYWORD_MATERIAL_MAP.keys()]
    combined_query = " OR ".join(quoted_terms)
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": combined_query,
        "from": from_date,
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 50,
        "apiKey": NEWSAPI_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"NewsAPI non-ok response: {payload.get('message', payload)}")
    return payload.get("articles", [])


def _fetch_gnews_articles(from_date: str):
    if not G_NEWS_API_KEY:
        raise RuntimeError("G_NEWS_API_KEY not set")
    # GNews can reject very long boolean expressions (HTTP 400),
    # so query in smaller batches and merge results.
    url = "https://gnews.io/api/v4/search"
    query_batches = [
        "copper OR aluminum OR aluminium OR steel OR transformer",
        "power grid OR capex OR rupee dollar OR red sea shipping OR ieema",
    ]

    merged = []
    seen_urls = set()
    last_error = None
    for query in query_batches:
        try:
            params = {
                "q": query,
                "lang": "en",
                "from": f"{from_date}T00:00:00Z",
                "max": 25,
                "apikey": G_NEWS_API_KEY,
            }
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
            batch = payload.get("articles", [])
            for art in batch:
                u = art.get("url")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    merged.append(art)
        except Exception as e:
            last_error = str(e)
            log.warning("GNews batch query failed (%s): %s", query, e)

    if not merged:
        raise RuntimeError(f"GNews fetch failed: {last_error or 'no articles returned'}")
    return merged


def fetch_and_classify_events():
    """
    Fetches latest news for transformer material keywords via NewsAPI.
    Classifies each article by severity, impact type, affected materials, and action.
    """
    if not NEWSAPI_KEY and not G_NEWS_API_KEY:
        raise RuntimeError("Neither NEWSAPI_KEY nor G_NEWS_API_KEY is set — cannot fetch live events")

    from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    events = []
    newsapi_error = None
    gnews_error = None

    try:
        events = _map_articles_to_events(_fetch_newsapi_articles(from_date))
    except Exception as e:
        newsapi_error = str(e)
        log.warning("NewsAPI failed, trying GNews fallback: %s", e)
        try:
            gnews_articles = _fetch_gnews_articles(from_date)
            events = _map_articles_to_events(gnews_articles)
            _store_fallback_output("gnews", {
                "used_provider": "gnews",
                "reason": newsapi_error,
                "from_date": from_date,
                "article_count": len(gnews_articles),
                "event_count": len(events),
                "articles": gnews_articles,
                "events": events,
            })
        except Exception as ge:
            gnews_error = str(ge)

    # Sort: CRITICAL first, then HIGH, then MEDIUM, LOW
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    events.sort(key=lambda x: severity_order.get(x["severity"], 4))

    # Fail hard if API returned no usable events.
    if not events:
        _store_fallback_output("failure", {
            "used_provider": None,
            "from_date": from_date,
            "newsapi_error": newsapi_error,
            "gnews_error": gnews_error,
        })
        raise RuntimeError(
            "No events received from both providers. "
            f"NewsAPI error: {newsapi_error or 'none'}, "
            f"GNews error: {gnews_error or 'none'}"
        )

    # Deduplicate and limit to top 8
    return events[:8]


# def get_sample_events():
#     """Sample events used when NewsAPI key is not configured."""
#     return [
#         {
#             "title":              "US–China Tariff Escalation to 145%",
#             "summary":            "US tariffs on Chinese copper & steel raised to 145%. Retaliatory rare earth export curbs.",
#             "affected_materials": ["Copper", "CRGO Steel"],
#             "severity":           "CRITICAL",
#             "impact_type":        "COST_INCREASE",
#             "recommended_action": "Cover 2-week need only. Avoid bulk purchase. Renegotiate open POs.",
#             "badge_color":        "RED",
#             "source":             "Sample data"
#         },
#         {
#             "title":              "Red Sea Shipping Rerouting Adds 18 Days",
#             "summary":            "Rerouting via Cape of Good Hope affecting European material shipments.",
#             "affected_materials": ["Transformer Oil", "Insulation Paper"],
#             "severity":           "HIGH",
#             "impact_type":        "SUPPLY_RISK",
#             "recommended_action": "Build 4-week safety stock. Negotiate delivery delay clauses.",
#             "badge_color":        "AMBER",
#             "source":             "Sample data"
#         },
#         {
#             "title":              "India Budget 2026: Rs.4.2L Cr Power Grid Capex",
#             "summary":            "Transformer demand up 34% YoY per IEEMA Q1 2026 report.",
#             "affected_materials": ["All"],
#             "severity":           "HIGH",
#             "impact_type":        "DEMAND_SURGE",
#             "recommended_action": "Forward buy 60-90 day requirement. Lock vendor capacity for H2 2026.",
#             "badge_color":        "AMBER",
#             "source":             "Sample data"
#         },
#         {
#             "title":              "LME Copper Hits All-Time High $10,480/MT",
#             "summary":            "EV and grid battery demand driving structural copper deficit.",
#             "affected_materials": ["Copper"],
#             "severity":           "CRITICAL",
#             "impact_type":        "COST_INCREASE",
#             "recommended_action": "Renegotiate open POs. Consider aluminium winding for applicable ratings.",
#             "badge_color":        "RED",
#             "source":             "Sample data"
#         },
#     ]
