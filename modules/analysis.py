from modules.db_connector import DBConnector
from modules.trend_engine import analyse_price_trend, calculate_sentiment_score
import logging

log = logging.getLogger(__name__)


def _to_float(value, default=0.0):
    """Coerce DB numeric types (e.g. Decimal) for safe arithmetic with floats."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# Material to vendor_ageing keyword mapping
# Maps transformer material names to keywords in vendor_ageing_prod.vendor_name
MATERIAL_VENDOR_MAP = {
    "Copper":           ["copper", "wire", "winding", "conductor"],
    "CRGO Steel":       ["steel", "crgo", "core", "lamination"],
    "Amorphous":        ["amorphous", "amo core", "amdt", "amorphous core"],
    "Transformer Oil":  ["oil", "transformer oil", "insulating"],
    "HR Steel":         ["steel", "fabricat", "tank", "radiator"],
    "Aluminium":        ["aluminium", "aluminum", "strip"],
    "Insulation Paper": ["paper", "pressboard", "insulation"],
}

def fetch_material_vendor_outstanding(material_keywords):
    """
    Cross-reference vendor_ageing_prod with material keywords
    to find outstanding PO value for a specific material category.
    """
    keyword_conditions = " OR ".join(
        [f"LOWER(vendor_name) LIKE '%%{kw}%%'" for kw in material_keywords]
    )
    sql = f"""
        SELECT
            vendor_name,
            COALESCE(SUM(gross_outstanding), 0)     AS outstanding_inr,
            COALESCE(SUM(document_amount), 0)        AS total_docs_inr,
            MAX(no_of_days_outstanding)              AS max_days_outstanding,
            COUNT(*)                                  AS doc_count
        FROM vendor_ageing_prod
        WHERE ({keyword_conditions})
          AND gross_outstanding > 0
          AND currency = 'INR'
        GROUP BY vendor_name
        ORDER BY outstanding_inr DESC
        LIMIT 5
    """
    try:
        return DBConnector.execute_query(sql)
    except Exception as e:
        log.warning(f"Vendor outstanding query failed: {e}")
        return []


def compute_market_variance(material_name, market_price_inr,
                             reference_po_price_inr, outstanding_qty):
    """
    Compare current market price vs the price embedded in open POs.
    Calculates variance and INR saving/risk.
    """
    ref = _to_float(reference_po_price_inr)
    mkt = _to_float(market_price_inr)
    qty = _to_float(outstanding_qty)
    if not ref or not mkt:
        return None

    variance_inr = ref - mkt
    variance_pct = (variance_inr / mkt) * 100
    saving_cr    = round((variance_inr * qty) / 1e7, 3)

    return {
        "material":            material_name,
        "market_price_inr":    round(mkt, 0),
        "po_price_inr":        round(ref, 0),
        "variance_inr":        round(variance_inr, 0),
        "variance_pct":        round(variance_pct, 2),
        "outstanding_qty":     qty,
        "saving_potential_cr": saving_cr,
        "signal": (
            "OVERPRICED — Renegotiate"  if variance_pct >  5 else
            "FAVOURABLE — Confirm PO"   if variance_pct < -3 else
            "AT MARKET — Monitor"
        ),
        "signal_color": (
            "RED"   if variance_pct >  5 else
            "GREEN" if variance_pct < -3 else
            "AMBER"
        )
    }


def run_full_analysis(sales_kpis, monthly_prod, weekly_prod,
                       vendor_summary, market_data, events):
    """
    Master analysis function combining all data sources into
    a unified analysis payload for the recommendation engine and PDF.
    """
    log.info("Running full analysis...")

    # ── 1. FX rate ────────────────────────────────────────────────────────────
    usd_inr = market_data.get("usd_inr", 84.5)

    # ── 2. Trend analysis per material ───────────────────────────────────────
    trends = {
        "Copper": analyse_price_trend(
            market_data.get("copper_history_90d", [])),
        "Aluminium": analyse_price_trend(
            market_data.get("aluminium_history_90d", [])),
    }

    # ── 3. Event severity per material ───────────────────────────────────────
    material_event_severity = {}
    for event in events:
        for mat in event.get("affected_materials", []):
            existing = material_event_severity.get(mat, "LOW")
            order    = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
            if order.get(event["severity"], 0) > order.get(existing, 0):
                material_event_severity[mat] = event["severity"]

    # ── 4. Sentiment scores ───────────────────────────────────────────────────
    fx_change = 0.4  # placeholder — compute from weekly FX history if available
    sentiments = {}

    material_configs = [
        ("Copper",           30, "copper_history_90d"),
        ("Aluminium",        10, "aluminium_history_90d"),
        ("CRGO Steel",       20, None),
        ("Amorphous",        18, None),
        ("Transformer Oil",  15, None),
        ("HR Steel",         10, None),
        ("Insulation Paper",  5, None),
    ]

    for mat_name, supply_risk, history_key in material_configs:
        trend_data   = trends.get(mat_name, {"direction": "STABLE",
                                              "change_30d_pct": 0})
        event_sev    = material_event_severity.get(mat_name,
                       material_event_severity.get("All", "LOW"))
        sentiments[mat_name] = calculate_sentiment_score(
            trend_data, supply_risk, fx_change, event_sev
        )

    # Market Sentiment Index (overall, live intelligence snapshot)
    sentiment_scores = [
        _to_float(v.get("score"))
        for v in sentiments.values()
        if isinstance(v, dict) and v.get("score") is not None
    ]
    avg_sentiment = round(sum(sentiment_scores) / len(sentiment_scores), 1) if sentiment_scores else 50.0
    if avg_sentiment < 40:
        overall_zone = "BEARISH"
    elif avg_sentiment <= 65:
        overall_zone = "NEUTRAL"
    else:
        overall_zone = "BULLISH"
    zone_counts = {"BULLISH": 0, "NEUTRAL": 0, "BEARISH": 0}
    for s in sentiments.values():
        if isinstance(s, dict):
            z = str(s.get("zone", "NEUTRAL")).upper()
            if z in zone_counts:
                zone_counts[z] += 1
    sorted_materials = sorted(
        [
            (mat, _to_float((meta or {}).get("score")))
            for mat, meta in sentiments.items()
            if isinstance(meta, dict)
        ],
        key=lambda x: x[1],
        reverse=True,
    )
    top_bullish = [m for m, _ in sorted_materials[:2]]
    top_bearish = [m for m, _ in sorted_materials[-2:]] if sorted_materials else []
    event_pressure = {
        "CRITICAL": len([e for e in events if e.get("severity") == "CRITICAL"]),
        "HIGH": len([e for e in events if e.get("severity") == "HIGH"]),
    }

    # ── 5. Vendor outstanding cross-match ─────────────────────────────────────
    vendor_market_cross = []
    for mat_name, keywords in MATERIAL_VENDOR_MAP.items():
        vendors = fetch_material_vendor_outstanding(keywords)
        if vendors:
            total_outstanding = sum(_to_float(v.get("outstanding_inr")) for v in vendors)
            vendor_market_cross.append({
                "material":          mat_name,
                "vendors":           vendors,
                "total_outstanding": total_outstanding,
                "outstanding_cr":    round(total_outstanding / 1e7, 2),
            })

    # ── 6. Production achievement ─────────────────────────────────────────────
    current_month = monthly_prod[-1] if monthly_prod else {}
    planned       = _to_float(current_month.get("planned_qty")) or 1.0
    achieved      = _to_float(current_month.get("achieved_qty"))
    achievement   = round((achieved / planned) * 100, 1)

    # ── 7. Compile full analysis payload ─────────────────────────────────────
    return {
        "usd_inr":              usd_inr,
        "trends":               trends,
        "sentiments":           sentiments,
        "market_sentiment_index": {
            "score": avg_sentiment,
            "zone": overall_zone,
            "zone_counts": zone_counts,
            "top_bullish_materials": top_bullish,
            "top_bearish_materials": top_bearish,
            "event_pressure": event_pressure,
        },
        "material_event_severity": material_event_severity,
        "vendor_market_cross":  vendor_market_cross,
        "achievement_pct":      achievement,
        "achievement_status": (
            "CRITICAL"     if achievement < 80  else
            "BELOW_TARGET" if achievement < 95  else
            "ON_TARGET"
        ),
        "critical_events":  [e for e in events if e["severity"] == "CRITICAL"],
        "high_events":      [e for e in events if e["severity"] == "HIGH"],
        "total_outstanding_cr": round(
            _to_float(vendor_summary.get("total_outstanding")) / 1e7, 2),
        "overdue_60plus_cr": round((
            _to_float(vendor_summary.get("bucket_61_90")) +
            _to_float(vendor_summary.get("bucket_91_120")) +
            _to_float(vendor_summary.get("bucket_121_150")) +
            _to_float(vendor_summary.get("bucket_151_180")) +
            _to_float(vendor_summary.get("bucket_181_360")) +
            _to_float(vendor_summary.get("bucket_361_plus"))
        ) / 1e7, 2),
    }
