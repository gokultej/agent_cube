def _oldest_first(history_90d: list) -> list:
    """Normalize API series (often newest-first) to oldest → newest."""
    if len(history_90d) < 2:
        return list(history_90d)
    # Rising market stored newest-first: p[0] high, p[-1] low → reverse.
    if history_90d[0] > history_90d[-1] * 1.02:
        recent = history_90d[min(7, len(history_90d) - 1)]
        if history_90d[0] >= recent:
            return list(reversed(history_90d))
    return list(history_90d)


def analyse_price_trend(history_90d):
    """
    Analyse price direction from 90-day history.
    Returns trend label, moving averages, momentum, and procurement signal.
    """
    if not history_90d or len(history_90d) < 30:
        return {"direction": "UNKNOWN", "signal": "INSUFFICIENT DATA"}

    p = _oldest_first(history_90d)

    avg30 = sum(p[-30:]) / 30
    avg60 = sum(p[-60:]) / max(len(p[-60:]), 1)
    avg90 = sum(p[-90:]) / max(len(p[-90:]), 1)

    change_7d  = ((p[-1] - p[-7])  / p[-7])  * 100 if len(p) >= 7  else 0
    change_30d = ((p[-1] - p[-30]) / p[-30]) * 100 if len(p) >= 30 else 0

    # Build indexed series (base = 100 at day 0)
    base = p[0] if p[0] != 0 else 1
    indexed = [round((v / base) * 100, 1) for v in p]

    # Direction logic
    if avg30 > avg60 * 1.02 and avg60 > avg90 * 1.01:
        direction = "RISING"
        procurement_signal = "FORWARD BUY — Lock price now before further increase"
        color = "RED"
    elif avg30 < avg60 * 0.98 and avg60 < avg90 * 0.99:
        direction = "FALLING"
        procurement_signal = "DEFER — Wait for price bottom, buy closer to need"
        color = "GREEN"
    else:
        direction = "STABLE"
        procurement_signal = "PROCURE AS SCHEDULED"
        color = "AMBER"

    return {
        "avg_30d":           round(avg30, 2),
        "avg_60d":           round(avg60, 2),
        "avg_90d":           round(avg90, 2),
        "change_7d_pct":     round(change_7d, 2),
        "change_30d_pct":    round(change_30d, 2),
        "direction":         direction,
        "procurement_signal":procurement_signal,
        "color":             color,
        "indexed_series":    indexed[-30:],  # Last 30 points for chart
    }


def calculate_sentiment_score(trend, supply_risk_score, fx_change_pct, event_severity):
    """
    Score each commodity 0–100:
      > 65 = Bullish  → BUY / FORWARD PURCHASE
      41–65 = Neutral → MONITOR
      < 40  = Bearish → DEFER

    Components:
      - Price momentum (40 pts): based on trend direction + 30d change
      - Supply/demand balance (30 pts): from trade data / event signals
      - FX impact (15 pts): USD/INR movement effect
      - Event risk (15 pts): severity of active geopolitical events
    """
    # Momentum score (0–40)
    if trend["direction"] == "RISING":
        momentum = min(40, 20 + abs(trend["change_30d_pct"]) * 2)
    elif trend["direction"] == "FALLING":
        momentum = max(0, 20 - abs(trend["change_30d_pct"]) * 2)
    else:
        momentum = 20  # neutral baseline

    # Supply/demand score (0–30)
    # supply_risk_score passed in: 0=ample, 30=severe shortage
    supply = max(0, min(30, supply_risk_score))

    # FX impact score (0–15)
    # Positive FX change (INR weakening) = higher import cost = higher score
    fx = max(0, min(15, 7 + fx_change_pct * 3))

    # Event risk score (0–15)
    event_map = {"CRITICAL": 15, "HIGH": 10, "MEDIUM": 5, "LOW": 2, "NONE": 0}
    event = event_map.get(event_severity, 0)

    total = round(momentum + supply + fx + event, 1)

    if total >= 66:
        zone  = "BULLISH"
        badge = "BUY / FORWARD PURCHASE"
    elif total >= 41:
        zone  = "NEUTRAL"
        badge = "MONITOR"
    else:
        zone  = "BEARISH"
        badge = "DEFER BULK PROCUREMENT"

    return {
        "score":          total,
        "zone":           zone,
        "badge":          badge,
        "breakdown": {
            "momentum_score": round(momentum, 1),
            "supply_score":   round(supply, 1),
            "fx_score":       round(fx, 1),
            "event_score":    round(event, 1),
        }
    }
