import os, logging, json
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from modules.db_connector       import DBConnector
from modules.sales_queries      import get_date_ranges, check_data_freshness
from modules.weekly_sales_queries import (
    fetch_weekly_summary_kpis,
    fetch_weekly_daily_breakdown,
    fetch_monthly_production,
    fetch_weekly_production,
    fetch_weekly_sales_by_capacity_kva,
    fetch_mtd_orders_by_capacity_kva,
    fetch_transformer_daily_sales,
    fetch_top_customers_transformer_week,
    fetch_top_materials_transformer_week,
    fetch_region_transformer_sales_week,
    build_transformer_sales_insight_summary,
)
from modules.po_queries         import fetch_po_spend_detail, fetch_po_spend_line_items
from modules.vendor_queries     import (
    fetch_customer_ageing_summary,
    fetch_vendor_ageing_summary,
    fetch_top_vendors_overdue,
    fetch_payments_due_next_week,
)
from modules.market_data        import fetch_all_market_data
from modules.events_intel       import fetch_and_classify_events
from modules.analysis           import run_full_analysis
from modules.prompt_builder     import build_user_prompt
from modules.claude_client      import call_claude
from modules.charts             import build_all_charts
from modules.pdf_generator      import generate_pdf_report
from modules.distribution       import distribute_report

os.makedirs("logs", exist_ok=True)
os.makedirs("output", exist_ok=True)
logging.basicConfig(
    filename=f"logs/agent_{datetime.now().strftime('%Y%m%d_%H%M')}.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger()

PLANT = os.getenv("PLANT", "1000")


def run_agent(distribute: bool = True) -> str:
    start = datetime.now()
    print("\n" + "="*55)
    print("  Claude-Powered Market Intelligence Agent")
    print("="*55)

    # ── 1. Database ──────────────────────────────────────────
    print("\n[1/9] Connecting to PostgreSQL...")
    if not DBConnector.test_connection():
        raise ConnectionError("DB connection failed")

    dates     = get_date_ranges()
    freshness = check_data_freshness()
    if freshness.get("lag_days", 0) > 1:
        log.warning(f"Data lag: {freshness['lag_days']} days")

    # ── 2. Internal data ─────────────────────────────────────
    print("[2/9] Fetching sales & vendor data...")
    sales_kpis    = fetch_weekly_summary_kpis(dates, PLANT)
    daily_sales   = fetch_weekly_daily_breakdown(dates, PLANT)
    monthly_prod  = fetch_monthly_production(dates, PLANT)
    weekly_prod   = fetch_weekly_production(dates, PLANT)
    po_spend      = fetch_po_spend_detail(dates, PLANT)
    po_spend_lines= fetch_po_spend_line_items(dates, PLANT)
    customer_summary = fetch_customer_ageing_summary(dates)
    vendor_summary= fetch_vendor_ageing_summary(dates)
    top_vendors   = fetch_top_vendors_overdue()
    due_next_week = fetch_payments_due_next_week(dates)
    weekly_sales_by_capacity_kva = fetch_weekly_sales_by_capacity_kva(dates, PLANT)
    mtd_orders_by_capacity_kva = fetch_mtd_orders_by_capacity_kva(dates, PLANT)
    transformer_daily_sales = fetch_transformer_daily_sales(dates, PLANT)
    top_customers_transformer_week = fetch_top_customers_transformer_week(dates, PLANT)
    top_materials_transformer_week = fetch_top_materials_transformer_week(dates, PLANT)
    region_transformer_sales_week = fetch_region_transformer_sales_week(dates, PLANT)
    transformer_sales_insight_summary = build_transformer_sales_insight_summary(
        weekly_sales_by_capacity_kva,
        mtd_orders_by_capacity_kva,
        transformer_daily_sales,
        top_customers_transformer_week,
        top_materials_transformer_week,
        region_transformer_sales_week,
    )

    # ── 3. Market data ───────────────────────────────────────
    print("[3/9] Fetching live market prices...")
    market_data = fetch_all_market_data()
    
    # ── 4. Events ────────────────────────────────────────────
    print("[4/9] Fetching global events...")
    events = fetch_and_classify_events()
    
    # ── 5. Analysis ──────────────────────────────────────────
    print("[5/9] Running analysis engine...")
    analysis = run_full_analysis(
        sales_kpis, monthly_prod, weekly_prod,
        vendor_summary, market_data, events
    )
    # ── 6. Claude API call ───────────────────────────────────
    print("[6/9] Calling Claude API...")
    payload = {
        "dates":          dates,
        "sales_kpis":     sales_kpis,
        "daily_sales":    daily_sales,
        "monthly_prod":   monthly_prod,
        "weekly_prod":    weekly_prod,
        "po_spend":       po_spend,
        "po_spend_lines": po_spend_lines,
        "customer_summary": customer_summary,
        "vendor_summary": vendor_summary,
        "weekly_sales_by_capacity_kva": weekly_sales_by_capacity_kva,
        "mtd_orders_by_capacity_kva": mtd_orders_by_capacity_kva,
        "transformer_daily_sales": transformer_daily_sales,
        "top_customers_transformer_week": top_customers_transformer_week,
        "top_materials_transformer_week": top_materials_transformer_week,
        "region_transformer_sales_week": region_transformer_sales_week,
        "transformer_sales_insight_summary": transformer_sales_insight_summary,
        "market_data":    market_data,
        "events":         events,
        "analysis":       analysis,
    }
    user_prompt   = build_user_prompt(payload)
    claude_output = call_claude(user_prompt)

    with open(f"logs/claude_output_{dates['today']}.json", "w") as f:
        json.dump(claude_output, f, indent=2)

    # ── 7. Charts ────────────────────────────────────────────
    print("[7/9] Building charts...")
    charts = build_all_charts(payload)

    # ── 8. PDF ───────────────────────────────────────────────
    print("[8/9] Generating PDF...")
    full_payload = {
        **payload,
        "top_vendors":   top_vendors,
        "due_next_week": due_next_week,
        "claude_output": claude_output,
        "charts":        charts,
    }
    pdf_path = generate_pdf_report(full_payload)

    # ── 9. Distribute ────────────────────────────────────────
    # if distribute:
    #     print("[9/9] Distributing...")
    #     distribute_report(pdf_path, sales_kpis, claude_output)
    # else:
    #     print("[9/9] Distribution skipped (webhook/manual mode).")

    elapsed = (datetime.now() - start).seconds
    print(f"\n  Done in {elapsed}s  \u2192  {pdf_path}\n")
    log.info(f"Agent complete in {elapsed}s — {pdf_path}")
    return pdf_path


if __name__ == "__main__":
    run_agent()
