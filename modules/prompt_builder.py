from datetime import datetime

from modules.analysis import _to_float


def _source_flag(status: str, source: str) -> str:
    s = (status or "").strip().lower()
    src = (source or "").strip().lower()
    if s == "internal_historical":
        return ""
    if s in {"fallback", "manual", "computed", "db_fallback"}:
        return " [FALLBACK]"
    if any(x in src for x in ("fallback", "reference", "manual", "computed", "db")):
        return " [FALLBACK]"
    return ""


def build_user_prompt(payload):
    """
    Converts all fetched data into a structured text prompt for Claude.
    Claude sees this as the 'user' message.
    """
    d  = payload["dates"]
    s  = payload["sales_kpis"]
    vs = payload["vendor_summary"]
    cs = payload.get("customer_summary") or {}
    m  = payload["market_data"]
    e  = payload["events"]

    prompt = f"""
WEEKLY REPORT DATA — {d['report_label']}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}

═══════════════════════════════
SECTION 1: SALES PERFORMANCE (from PostgreSQL → sales_data)
═══════════════════════════════
Period: {d['week_start']} to {d['week_end']}

Total Net Sales:        Rs.{_to_float(s.get('total_net_sales_cr')):.2f} Cr
Total Invoices:         {s.get('total_invoices', 0)}
Unique Customers:       {s.get('unique_customers', 0)}
Total Orders:           {s.get('total_orders', 0)}

Month-to-Date:
  MTD Net Sales:        Rs.{_to_float(s.get('mtd_net_sales_cr')):.2f} Cr
  This Month Planned:   {payload['monthly_prod'][-1].get('planned_qty', 0) if payload['monthly_prod'] else 'N/A'} units
  This Month Achieved:  {payload['monthly_prod'][-1].get('achieved_qty', 0) if payload['monthly_prod'] else 'N/A'} units
  Achievement %:        {payload['analysis'].get('achievement_pct', 0):.1f}%
  Status:               {payload['analysis'].get('achievement_status', 'UNKNOWN')}

Daily Sales Breakdown:
"""
    for row in payload.get("daily_sales", []):
        prompt += (
            f"  {row['date_label']}: Rs.{_to_float(row.get('net_sales_cr')):.2f} Cr  "
            f"({row.get('invoice_count', 0)} invoices)\n"
        )

    si = payload.get("transformer_sales_insight_summary") or {}
    prompt += (
        "\nSALES_DATA / TRANSFORMER MIX (sales_data, is_transformer=Y; capacity_kva or rating):\n"
        f"  Week transformer net (daily sum): Rs.{_to_float(si.get('week_transformer_daily_net_cr')):.2f} Cr\n"
        f"  Week net by rating buckets (sum): Rs.{_to_float(si.get('week_transformer_net_by_rating_cr')):.2f} Cr\n"
        f"  Range transformer ordered qty (sum): {_to_float(si.get('mtd_transformer_ordered_qty')):,.0f}\n"
        f"  Distinct rating buckets (week / range): {si.get('rating_buckets_week', 0)} / {si.get('rating_buckets_mtd', 0)}\n"
    )
    for line in si.get("bullets") or []:
        prompt += f"  • {line}\n"

    cust_60plus = (
        _to_float(cs.get("bucket_61_90"))
        + _to_float(cs.get("bucket_91_120"))
        + _to_float(cs.get("bucket_121_180"))
        + _to_float(cs.get("bucket_181_360"))
        + _to_float(cs.get("bucket_361_plus"))
    ) / 1e7

    prompt += f"""
═══════════════════════════════
SECTION 2: CUSTOMER AGEING / RECEIVABLES (from PostgreSQL → customer_ageing_prod)
═══════════════════════════════
Total Customers:        {int(_to_float(cs.get("total_customers"))) if cs else 0}
Total Outstanding:      Rs.{_to_float(cs.get("total_outstanding"))/1e7:.2f} Cr
Not Yet Due:            Rs.{_to_float(cs.get("not_due_amount"))/1e7:.2f} Cr

Ageing Buckets (INR Cr):
  1–30 days:            Rs.{_to_float(cs.get("bucket_01_30"))/1e7:.2f} Cr
  31–60 days:           Rs.{_to_float(cs.get("bucket_31_60"))/1e7:.2f} Cr
  61–90 days:           Rs.{_to_float(cs.get("bucket_61_90"))/1e7:.2f} Cr
  91–120 days:          Rs.{_to_float(cs.get("bucket_91_120"))/1e7:.2f} Cr
  121–180 days:         Rs.{_to_float(cs.get("bucket_121_180"))/1e7:.2f} Cr
  181–360 days:         Rs.{_to_float(cs.get("bucket_181_360"))/1e7:.2f} Cr
  361+ days:            Rs.{_to_float(cs.get("bucket_361_plus"))/1e7:.2f} Cr
Outstanding (>60 days): Rs.{cust_60plus:.2f} Cr

"""

    prompt += f"""
═══════════════════════════════
SECTION 3: VENDOR PAYMENT AGEING (from PostgreSQL → vendor_ageing_prod)
═══════════════════════════════
Total Outstanding:      Rs.{_to_float(vs.get('total_outstanding'))/1e7:.2f} Cr
Not Yet Due:            Rs.{_to_float(vs.get('not_due_amount'))/1e7:.2f} Cr

Ageing Buckets (INR Cr):
  1–30 days:            Rs.{_to_float(vs.get('bucket_01_30'))/1e7:.2f} Cr
  31–60 days:           Rs.{_to_float(vs.get('bucket_31_60'))/1e7:.2f} Cr
  61–90 days:           Rs.{_to_float(vs.get('bucket_61_90'))/1e7:.2f} Cr
  91–120 days:          Rs.{_to_float(vs.get('bucket_91_120'))/1e7:.2f} Cr
  121–180 days:         Rs.{(_to_float(vs.get('bucket_121_150')) + _to_float(vs.get('bucket_151_180')))/1e7:.2f} Cr
  181–360 days:         Rs.{_to_float(vs.get('bucket_181_360'))/1e7:.2f} Cr
  361+ days:            Rs.{_to_float(vs.get('bucket_361_plus'))/1e7:.2f} Cr
Critical (>60 days):    Rs.{_to_float(payload['analysis'].get('overdue_60plus_cr')):.2f} Cr

"""

    prompt += f"""
═══════════════════════════════
SECTION 4: LIVE COMMODITY MARKET PRICES
═══════════════════════════════
USD/INR Rate:           {_to_float(m.get('usd_inr', 84.5)):.2f}  (source: {m.get('usd_inr_source','')})

Copper:
  LME Price:            ${_to_float(m.get('copper_lme_usd_mt')):,.0f} /MT  ({m.get('copper_source','')}){_source_flag(m.get('copper_status',''), m.get('copper_source',''))}
  INR Equivalent:       Rs.{_to_float(m.get('copper_lme_inr_mt'))/1000:.0f}k /MT
  30-day Trend:         {payload['analysis']['trends'].get('Copper', {}).get('direction', 'UNKNOWN')}
  30d Change:           {_to_float(payload['analysis']['trends'].get('Copper', {}).get('change_30d_pct')):+.1f}%

Aluminium:
  LME Price:            ${_to_float(m.get('aluminium_lme_usd_mt')):,.0f} /MT
  INR Equivalent:       Rs.{_to_float(m.get('aluminium_lme_inr_mt'))/1000:.0f}k /MT
  30-day Trend:         {payload['analysis']['trends'].get('Aluminium', {}).get('direction', 'UNKNOWN')}{_source_flag(m.get('aluminium_status',''), m.get('aluminium_source',''))}

CRGO Steel:             Rs.{_to_float(m.get('crgo_steel_inr_mt'))/1000:.0f}k /MT  ({m.get('crgo_source','')}){_source_flag(m.get('crgo_status',''), m.get('crgo_source',''))}
Amorphous Core:         Rs.{_to_float(m.get('amorphous_core_inr_mt'))/1000:.0f}k /MT  ({m.get('amorphous_source','')}){_source_flag(m.get('amorphous_status',''), m.get('amorphous_source',''))}
Transformer Oil:        Rs.{_to_float(m.get('transformer_oil_inr_kl'))/1000:.0f}k /KL  ({m.get('oil_source','')}){_source_flag(m.get('oil_status',''), m.get('oil_source',''))}
HR Steel (Tank):        Rs.{_to_float(m.get('hr_steel_inr_mt'))/1000:.0f}k /MT  ({m.get('hr_steel_source','')}){_source_flag(m.get('hr_steel_status',''), m.get('hr_steel_source',''))}

PO-weighted averages (same window as internal material query where applicable):
  Copper PO avg (INR/MT):      {m.get('copper_po_avg_inr_mt') or 'N/A'}
  Aluminium PO avg (INR/MT):  {m.get('aluminium_po_avg_inr_mt') or 'N/A'}
  CRGO PO avg (INR/MT):        {m.get('crgo_po_avg_inr_mt') or 'N/A'}
  Amorphous PO avg (INR/MT):   {m.get('amorphous_po_avg_inr_mt') or 'N/A'}
  HR Steel PO avg (INR/MT):    {m.get('hr_steel_po_avg_inr_mt') or 'N/A'}
  Transformer oil PO avg (INR/KL, 30d PO lines): {m.get('transformer_oil_po_avg_inr_kl') or 'N/A'}

Sentiment Scores (0=Bearish, 100=Bullish):
"""
    for mat, sent in payload['analysis'].get('sentiments', {}).items():
        prompt += (
            f"  {mat:<22}: {_to_float(sent.get('score')):>3.0f}/100  "
            f"[{sent.get('zone', 'NEUTRAL')}]\n"
        )

    msi = payload["analysis"].get("market_sentiment_index", {})
    zone_counts = msi.get("zone_counts", {}) or {}
    event_pressure = msi.get("event_pressure", {}) or {}
    prompt += (
        "\nMarket Sentiment Index — Live Intelligence:\n"
        f"  Overall Score:       {_to_float(msi.get('score')):.1f}/100\n"
        f"  Overall Zone:        {msi.get('zone', 'NEUTRAL')}\n"
        f"  Zone Split:          Bullish={int(_to_float(zone_counts.get('BULLISH')))}, "
        f"Neutral={int(_to_float(zone_counts.get('NEUTRAL')))}, "
        f"Bearish={int(_to_float(zone_counts.get('BEARISH')))}\n"
        f"  Top Bullish:         {', '.join(msi.get('top_bullish_materials') or ['N/A'])}\n"
        f"  Top Bearish:         {', '.join(msi.get('top_bearish_materials') or ['N/A'])}\n"
        f"  Event Pressure:      CRITICAL={int(_to_float(event_pressure.get('CRITICAL')))}, "
        f"HIGH={int(_to_float(event_pressure.get('HIGH')))}\n"
    )

    prompt += "\n═══════════════════════════════\nSECTION 5: GLOBAL EVENTS\n═══════════════════════════════\n"
    for i, ev in enumerate(e[:6], 1):
        prompt += f"""
Event {i}: {ev['title']}
  Severity:   {ev['severity']}
  Materials:  {', '.join(ev.get('affected_materials', []))}
  Summary:    {ev.get('summary', '')[:200]}
  Impact:     {ev.get('impact_type', '')}
"""

    prompt += f"""
═══════════════════════════════
SECTION 6: VENDOR-MATERIAL CROSS ANALYSIS
═══════════════════════════════
(Outstanding PO value per material category from vendor_ageing_prod)
"""
    for cross in payload['analysis'].get('vendor_market_cross', []):
        prompt += (
            f"  {cross['material']:<22}: Rs.{_to_float(cross.get('outstanding_cr')):.2f} Cr outstanding\n"
        )

    prompt += "\nNow generate the JSON report as instructed in your system prompt."
    return prompt
