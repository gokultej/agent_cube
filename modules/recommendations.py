def generate_all_recommendations(analysis, market_data, events):
    """
    Generates procurement recommendations for each material
    by combining: trend + sentiment + event severity + vendor outstanding.
    """
    recommendations = []
    total_saving_cr = 0
    total_risk_cr   = 0

    material_prices = {
        "Copper":           market_data.get("copper_lme_inr_mt", 0),
        "Aluminium":        market_data.get("aluminium_lme_inr_mt", 0),
        "CRGO Steel":       market_data.get("crgo_steel_inr_mt", 0),
        "Amorphous":        market_data.get("amorphous_core_inr_mt", 0),
        "Transformer Oil":  market_data.get("transformer_oil_inr_kl", 0),
        "HR Steel":         market_data.get("hr_steel_inr_mt", 0),
        "Insulation Paper": market_data.get("insulation_paper_inr_mt", 0),
    }

    for cross in analysis.get("vendor_market_cross", []):
        mat          = cross["material"]
        outstanding  = cross["outstanding_cr"]
        trend        = analysis["trends"].get(mat, {})
        sentiment    = analysis["sentiments"].get(mat, {})
        event_sev    = analysis["material_event_severity"].get(mat,
                       analysis["material_event_severity"].get("All", "LOW"))

        direction    = trend.get("direction", "STABLE")
        score        = sentiment.get("score", 50)
        change_30d   = trend.get("change_30d_pct", 0)

        # Decision logic
        if event_sev == "CRITICAL" and direction == "RISING":
            action  = "DEFER BULK"
            color   = "RED"
            qty     = "Cover 2-week immediate need only"
            rationale = (f"Critical market event + price rising {change_30d:.1f}% "
                         f"over 30 days. Avoid locking large quantity at peak.")
            saving  = round(outstanding * 0.04, 2)
            risk    = round(outstanding * 0.09, 2)

        elif outstanding > 5 and direction == "RISING" and score >= 66:
            action  = "FORWARD BUY"
            color   = "GREEN"
            qty     = "Cover 60–90 day requirement now"
            rationale = (f"Price rising {change_30d:.1f}% over 30 days. "
                         f"Rs.{outstanding} Cr outstanding. Lock current rate.")
            saving  = round(outstanding * 0.05, 2)
            risk    = round(outstanding * 0.02, 2)

        elif direction == "FALLING" and score < 41:
            action  = "DEFER"
            color   = "BLUE"
            qty     = "Cover only 2-week immediate need"
            rationale = (f"Price falling {abs(change_30d):.1f}% over 30 days. "
                         f"Defer bulk purchase 2–3 weeks.")
            saving  = round(outstanding * 0.03, 2)
            risk    = round(outstanding * 0.01, 2)

        elif event_sev in ["HIGH"] and direction in ["RISING", "STABLE"]:
            action  = "NEGOTIATE & PARTIAL BUY"
            color   = "AMBER"
            qty     = "50% now at negotiated rate, 50% in 3 weeks"
            rationale = (f"High-severity event impacting supply. "
                         f"Split order reduces risk on Rs.{outstanding} Cr exposure.")
            saving  = round(outstanding * 0.02, 2)
            risk    = round(outstanding * 0.05, 2)

        else:
            action  = "PROCURE AS SCHEDULED"
            color   = "GRAY"
            qty     = "Follow standard purchase schedule"
            rationale = "Market stable. No immediate deviation from plan required."
            saving  = 0
            risk    = 0

        total_saving_cr += saving
        total_risk_cr   += risk

        recommendations.append({
            "material":          mat,
            "action":            action,
            "color":             color,
            "qty_strategy":      qty,
            "rationale":         rationale,
            "trend":             direction,
            "sentiment_score":   score,
            "sentiment_zone":    sentiment.get("zone", "NEUTRAL"),
            "event_severity":    event_sev,
            "outstanding_cr":    outstanding,
            "saving_potential_cr": saving,
            "risk_if_ignored_cr":  risk,
            "market_price":      material_prices.get(mat, 0),
        })

    return {
        "items":                recommendations,
        "total_saving_cr":      round(total_saving_cr, 2),
        "total_risk_cr":        round(total_risk_cr, 2),
        "critical_count":       sum(1 for r in recommendations if r["color"] == "RED"),
        "action_required_count":sum(1 for r in recommendations
                                    if r["action"] not in ["PROCURE AS SCHEDULED"]),
    }
