"""
Queries using the actual production database tables:
  weekly_sales_summary  — pre-aggregated weekly sales (total_sales_value already in Cr)
  factory_weekly_plan   — planned vs actual production per week/month
  po_data               — purchase order details with status & INR values
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List

from modules.db_connector import DBConnector

log = logging.getLogger(__name__)


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _with_last30_dates(dates: dict) -> dict:
    """Fallback window when current report range returns sparse/empty rows."""
    today = dates.get("today")
    if isinstance(today, date):
        start_30 = today - timedelta(days=29)
        return {**dates, "week_start": start_30, "week_end": today, "month_start": start_30}
    return dates


# ── 1. Weekly Sales KPIs ─────────────────────────────────────────────────────
def _fetch_mtd_from_sales_data(dates: dict, plant: str) -> dict:
    """Month-to-date net sales from sales_data (calendar month, not rolling window)."""
    sql = """
        SELECT COALESCE(SUM(total_net_value), 0) AS mtd_net_sales_cr
        FROM sales_data
        WHERE sales_date BETWEEN %(month_start)s AND %(today)s
          AND is_transformer = 'Yes'
    """
    try:
        rows = DBConnector.execute_query(
            sql,
            {
                "month_start": dates["month_start"],
                "today": dates["today"],
                "plant": plant,
            },
        )
        if rows:
            return {"mtd_net_sales_cr": _to_float(rows[0].get("mtd_net_sales_cr"))}
    except Exception as e:
        log.debug("sales_data MTD query failed: %s", e)
    return {"mtd_net_sales_cr": 0.0}


def _fetch_sales_window_from_sales_data(dates: dict, plant: str) -> dict:
    """Exact net sales + counts for [week_start, week_end] from sales_data."""
    sql = """
        SELECT
            COALESCE(SUM(total_net_value), 0)           AS total_net_sales_cr,
            COUNT(DISTINCT invoice_number)             AS total_invoices,
            COUNT(DISTINCT sales_order_number)         AS total_orders,
            COUNT(DISTINCT customer_code)              AS unique_customers
        FROM sales_data
        WHERE sales_date BETWEEN %(week_start)s AND %(week_end)s
          AND billing_type NOT IN ('RE', 'RK')
    """
    try:
        rows = DBConnector.execute_query(
            sql,
            {
                "week_start": dates["week_start"],
                "week_end": dates["week_end"],
                "plant": plant,
            },
        )
        return rows[0] if rows else {}
    except Exception as e:
        log.warning("sales_data window KPI query failed: %s", e)
        return {}


def fetch_weekly_summary_kpis(dates: dict, plant: str) -> dict:
    """
    Headline KPIs for the configured report window (week_start … week_end).

    Primary: aggregate sales_data for the exact date range (matches rolling N-day report).
    Fallback: overlapping row from weekly_sales_summary if sales_data returns no rows.
    Production units (month_planned / month_achieved) still come from factory_weekly_plan (month).
    """
    row = _fetch_sales_window_from_sales_data(dates, plant)
    total_cr = _to_float(row.get("total_net_sales_cr"))
    inv_count = row.get("total_invoices")

    if total_cr == 0 and (inv_count is None or int(_to_float(inv_count or 0)) == 0):
        sql = """
            SELECT
                id,
                start_date,
                end_date,
                COALESCE(total_sales_value, 0)      AS total_net_sales_cr,
                COALESCE(total_invoices, 0)          AS total_invoices
            FROM weekly_sales_summary
            WHERE start_date <= %(week_end)s
              AND end_date   >= %(week_start)s
            ORDER BY end_date DESC
            LIMIT 1
        """
        try:
            rows = DBConnector.execute_query(
                sql,
                {"week_start": dates["week_start"], "week_end": dates["week_end"]},
            )
            row = rows[0] if rows else {}
        except Exception as e:
            log.error("weekly_sales_summary query failed: %s", e)
            row = {}

    # Pull production totals for the month from factory_weekly_plan
    prod_kpis = _fetch_month_production_kpis(dates)

    # Pull total PO value for the report window from po_data
    po_total = _fetch_week_po_total(dates)

    mtd = _fetch_mtd_from_sales_data(dates, plant)

    return {
        "total_net_sales_cr":   _to_float(row.get("total_net_sales_cr")),
        "total_invoices":       row.get("total_invoices"),
        "unique_customers":     row.get("unique_customers"),
        "total_orders":         row.get("total_orders"),
        "mtd_net_sales_cr":     mtd.get("mtd_net_sales_cr", 0.0),
        "month_planned_units":  prod_kpis.get("month_planned_units", 0),
        "month_achieved_units": prod_kpis.get("month_achieved_units", 0),
        "total_po_value_cr":    po_total,
        "_summary_row": row,
    }


def _fetch_month_production_kpis(dates: dict) -> dict:
    """Sum planned_qty / actual_qty from factory_weekly_plan for the current month."""
    # month_year column format is unknown — try common formats via pattern match
    today: date = dates.get("today")  # type: ignore[assignment]
    if today is None:
        return {}

    month_patterns = [
        today.strftime("%b-%Y"),    # Apr-2026
        today.strftime("%B-%Y"),    # April-2026
        today.strftime("%m-%Y"),    # 04-2026
        today.strftime("%Y-%m"),    # 2026-04
        today.strftime("%b %Y"),    # Apr 2026
        today.strftime("%B %Y"),    # April 2026
    ]
    placeholders = ",".join(f"%(p{i})s" for i in range(len(month_patterns)))
    params = {f"p{i}": v for i, v in enumerate(month_patterns)}

    sql = f"""
        SELECT
            COALESCE(SUM(planned_qty), 0) AS month_planned_units,
            COALESCE(SUM(actual_qty),  0) AS month_achieved_units
        FROM factory_weekly_plan
        WHERE month_year IN ({placeholders})
    """
    try:
        rows = DBConnector.execute_query(sql, params)
        if rows and (rows[0]["month_planned_units"] or rows[0]["month_achieved_units"]):
            return {
                "month_planned_units":  _to_float(rows[0]["month_planned_units"]),
                "month_achieved_units": _to_float(rows[0]["month_achieved_units"]),
            }
    except Exception as e:
        log.warning("factory_weekly_plan month KPI query failed: %s", e)

    # Fallback: try without month_year filter — get latest distinct month
    try:
        sql2 = """
            SELECT
                COALESCE(SUM(planned_qty), 0) AS month_planned_units,
                COALESCE(SUM(actual_qty),  0) AS month_achieved_units
            FROM factory_weekly_plan
            WHERE month_year = (
                SELECT month_year
                FROM factory_weekly_plan
                ORDER BY created_at DESC
                LIMIT 1
            )
        """
        rows = DBConnector.execute_query(sql2)
        if rows:
            return {
                "month_planned_units":  _to_float(rows[0]["month_planned_units"]),
                "month_achieved_units": _to_float(rows[0]["month_achieved_units"]),
            }
    except Exception as e:
        log.warning("factory_weekly_plan fallback KPI query failed: %s", e)

    return {}


def _fetch_week_po_total(dates: dict) -> float:
    """Total PO value (in Cr) created during the week from po_data."""
    sql = """
        SELECT COALESCE(SUM(po_value_inr), 0) AS total_po_value_cr
        FROM po_data
        WHERE po_creation_date BETWEEN %(week_start)s AND %(week_end)s
    """
    try:
        rows = DBConnector.execute_query(sql, {
            "week_start": dates["week_start"],
            "week_end":   dates["week_end"],
        })
        return _to_float(rows[0]["total_po_value_cr"] if rows else 0)
    except Exception as e:
        log.warning("po_data week total query failed: %s", e)
        return 0.0


# ── 2. Daily sales breakdown (not in weekly_summary — use sales_data if available) ──
def fetch_weekly_daily_breakdown(dates: dict, plant: str) -> List[dict]:
    """
    Attempt daily breakdown from sales_data; returns empty list if table missing.
    The new PDF no longer renders a standalone daily chart, but the data is still
    passed to Claude for commentary.
    """
    sql = """
        SELECT
            sales_date,
            TO_CHAR(sales_date, 'DD Mon')          AS date_label,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COUNT(DISTINCT invoice_number)           AS invoice_count
        FROM sales_data
        WHERE sales_date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY sales_date
        ORDER BY sales_date
    """
    try:
        return DBConnector.execute_query(sql, {**dates, "plant": plant})
    except Exception as e:
        log.debug("sales_data daily breakdown unavailable (%s) — skipped", e)
        return []


# ── 3. Monthly production from factory_weekly_plan ───────────────────────────
def fetch_monthly_production(dates: dict, plant: str) -> List[dict]:
    """
    Monthly planned vs achieved from factory_weekly_plan.
    Groups by month_year, returns last 6 months ordered oldest → newest.
    """
    sql = """
        SELECT
            month_year                              AS month_label,
            COALESCE(SUM(planned_qty), 0)           AS planned_qty,
            COALESCE(SUM(actual_qty),  0)           AS achieved_qty,
            MIN(created_at)                         AS sort_key
        FROM factory_weekly_plan
        GROUP BY month_year
        ORDER BY sort_key DESC
        LIMIT 6
    """
    try:
        rows = DBConnector.execute_query(sql)
        if rows:
            return list(reversed(rows))   # oldest first for chart
    except Exception as e:
        log.error("factory_weekly_plan monthly query failed: %s", e)
    return []


# ── 4. Weekly production from factory_weekly_plan ────────────────────────────
def fetch_weekly_production(dates: dict, plant: str) -> List[dict]:
    """
    Weekly planned vs actual for the current month, grouped by the `week` column.
    """
    today: date = dates.get("today")  # type: ignore[assignment]
    if today is None:
        return []

    month_patterns = [
        today.strftime("%b-%Y"),
        today.strftime("%B-%Y"),
        today.strftime("%m-%Y"),
        today.strftime("%Y-%m"),
        today.strftime("%b %Y"),
        today.strftime("%B %Y"),
    ]
    placeholders = ",".join(f"%(p{i})s" for i in range(len(month_patterns)))
    params = {f"p{i}": v for i, v in enumerate(month_patterns)}

    sql = f"""
        SELECT
            week                                    AS week_label,
            COALESCE(SUM(planned_qty), 0)           AS planned_qty,
            COALESCE(SUM(actual_qty),  0)           AS achieved_qty,
            MIN(created_at)                         AS sort_key
        FROM factory_weekly_plan
        WHERE month_year IN ({placeholders})
        GROUP BY week
        ORDER BY sort_key ASC
    """
    try:
        rows = DBConnector.execute_query(sql, params)
        if rows:
            return rows
    except Exception as e:
        log.warning("factory_weekly_plan weekly query failed: %s", e)

    # Fallback: latest month in table
    try:
        sql2 = """
            SELECT
                week                                AS week_label,
                COALESCE(SUM(planned_qty), 0)       AS planned_qty,
                COALESCE(SUM(actual_qty),  0)       AS achieved_qty,
                MIN(created_at)                     AS sort_key
            FROM factory_weekly_plan
            WHERE month_year = (
                SELECT month_year
                FROM factory_weekly_plan
                ORDER BY created_at DESC
                LIMIT 1
            )
            GROUP BY week
            ORDER BY sort_key ASC
        """
        return DBConnector.execute_query(sql2)
    except Exception as e:
        log.error("factory_weekly_plan weekly fallback failed: %s", e)
        return []


# ── 5. Sales / demand by transformer rating (capacity_kva) ───────────────────
_CAP_GROUP_SQL = """
    COALESCE(
        NULLIF(TRIM(CAST(capacity_kva AS TEXT)), ''),
        'Unspecified'
    )
"""

def fetch_weekly_sales_by_capacity_kva(dates: dict, plant: str) -> List[dict]:
    """
    Report-window net sales, invoices, and ordered qty by rating (KVA),
    from sales_data for transformer billing lines.
    """
    sql = f"""
        SELECT
            {_CAP_GROUP_SQL} AS capacity_key,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COALESCE(SUM(ordered_qty), 0) AS ordered_qty,
            COUNT(DISTINCT invoice_number) AS invoice_count
        FROM sales_data
        WHERE sales_date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY {_CAP_GROUP_SQL}
        ORDER BY net_sales_cr DESC NULLS LAST
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("weekly capacity query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant})
            if rows:
                return rows
        log.info("weekly capacity query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None})
        return []
    except Exception as e:
        log.warning("sales_data weekly by capacity failed: %s", e)
        return []


def fetch_mtd_orders_by_capacity_kva(dates: dict, plant: str) -> List[dict]:
    """
    Report-window ordered quantity and net sales by rating.
    Uses the same configured date range (week_start..week_end).
    """
    sql = f"""
        SELECT
            {_CAP_GROUP_SQL} AS capacity_key,
            COALESCE(SUM(ordered_qty), 0) AS range_ordered_qty,
            COALESCE(SUM(total_net_value), 0) AS mtd_net_sales_cr
        FROM sales_data
        WHERE sales_date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY {_CAP_GROUP_SQL}
        ORDER BY range_ordered_qty DESC NULLS LAST
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("capacity range query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant})
            if rows:
                return rows
        log.info("capacity range query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None})
        return []
    except Exception as e:
        log.warning("sales_data MTD by capacity failed: %s", e)
        return []


def fetch_transformer_daily_sales(dates: dict, plant: str) -> List[dict]:
    """Per-day transformer net sales and invoice counts in the report window."""
    sql = f"""
        SELECT
            (sales_date::date) AS sales_date,
            TO_CHAR(sales_date::date, 'DD Mon') AS day_label,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COUNT(DISTINCT invoice_number) AS invoice_count
        FROM sales_data
        WHERE sales_date::date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY (sales_date::date)
        ORDER BY (sales_date::date)
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("transformer daily query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant})
            if rows:
                return rows
        log.info("transformer daily query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None})
        return []
    except Exception as e:
        log.warning("sales_data transformer daily failed: %s", e)
        return []


def fetch_top_customers_transformer_week(dates: dict, plant: str, limit: int = 10) -> List[dict]:
    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(customer_name), ''), customer_code::text, 'Unknown') AS customer_name,
            COALESCE(NULLIF(TRIM(region_desc), ''), NULLIF(TRIM(customer_region::text), ''), '') AS region_label,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COUNT(DISTINCT invoice_number) AS invoice_count,
            COALESCE(SUM(ordered_qty), 0) AS ordered_qty
        FROM sales_data
        WHERE sales_date::date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY
            COALESCE(NULLIF(TRIM(customer_name), ''), customer_code::text, 'Unknown'),
            COALESCE(NULLIF(TRIM(region_desc), ''), NULLIF(TRIM(customer_region::text), ''), '')
        ORDER BY net_sales_cr DESC NULLS LAST
        LIMIT %(limit)s
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant, "limit": limit})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("top customers query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant, "limit": limit})
            if rows:
                return rows
        log.info("top customers query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None, "limit": limit})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None, "limit": limit})
        return []
    except Exception as e:
        log.warning("sales_data top customers (transformer) failed: %s", e)
        return []


def fetch_top_materials_transformer_week(dates: dict, plant: str, limit: int = 10) -> List[dict]:
    sql = f"""
        SELECT
            material_label,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COALESCE(SUM(ordered_qty), 0) AS ordered_qty,
            COUNT(DISTINCT invoice_number) AS invoice_count
        FROM (
            SELECT
                LEFT(
                    COALESCE(NULLIF(TRIM(material_description), ''), 'Unspecified'),
                    72
                ) AS material_label,
                total_net_value,
                ordered_qty,
                invoice_number
            FROM sales_data
            WHERE sales_date::date BETWEEN %(week_start)s AND %(week_end)s
              AND is_transformer = 'Yes'
        ) t
        GROUP BY material_label
        ORDER BY net_sales_cr DESC NULLS LAST
        LIMIT %(limit)s
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant, "limit": limit})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("top materials query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant, "limit": limit})
            if rows:
                return rows
        log.info("top materials query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None, "limit": limit})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None, "limit": limit})
        return []
    except Exception as e:
        log.warning("sales_data top materials (transformer) failed: %s", e)
        return []


def fetch_region_transformer_sales_week(dates: dict, plant: str) -> List[dict]:
    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(region_desc), ''), NULLIF(TRIM(customer_region::text), ''), 'Unspecified') AS region_label,
            COALESCE(SUM(total_net_value), 0) AS net_sales_cr,
            COUNT(DISTINCT customer_code) AS customer_count,
            COUNT(DISTINCT invoice_number) AS invoice_count
        FROM sales_data
        WHERE sales_date::date BETWEEN %(week_start)s AND %(week_end)s
          AND is_transformer = 'Yes'
        GROUP BY
            COALESCE(NULLIF(TRIM(region_desc), ''), NULLIF(TRIM(customer_region::text), ''), 'Unspecified')
        ORDER BY net_sales_cr DESC NULLS LAST
    """
    try:
        rows = DBConnector.execute_query(sql, {**dates, "plant": plant})
        if rows:
            return rows
        fallback_dates = _with_last30_dates(dates)
        if fallback_dates != dates:
            log.info("region transformer query empty for report window; trying last 30 days")
            rows = DBConnector.execute_query(sql, {**fallback_dates, "plant": plant})
            if rows:
                return rows
        log.info("region transformer query still empty for plant=%s; trying all plants", plant)
        rows = DBConnector.execute_query(sql, {**dates, "plant": None})
        if rows:
            return rows
        if fallback_dates != dates:
            return DBConnector.execute_query(sql, {**fallback_dates, "plant": None})
        return []
    except Exception as e:
        log.warning("sales_data region transformer week failed: %s", e)
        return []


def build_transformer_sales_insight_summary(
    capacity_week: List[dict],
    capacity_mtd: List[dict],
    daily: List[dict],
    top_customers: List[dict],
    top_materials: List[dict],
    region_rows: List[dict],
) -> dict:
    """
    Lightweight narrative metrics for PDF / prompt (no LLM required).
    """
    bullets: List[str] = []
    week_cr = sum(_to_float(r.get("net_sales_cr")) for r in (capacity_week or []))
    mtd_qty = sum(_to_float(r.get("range_ordered_qty")) for r in (capacity_mtd or []))
    daily_cr = sum(_to_float(r.get("net_sales_cr")) for r in (daily or []))

    if capacity_week and week_cr > 0:
        top = capacity_week[0]
        share = _to_float(top.get("net_sales_cr")) / week_cr * 100.0
        ck = str(top.get("capacity_key") or "?")
        bullets.append(
            f"Rating concentration: {ck} leads this week at {share:.0f}% of transformer net sales (Rs. {week_cr:.2f} Cr total by rating)."
        )
        if len(capacity_week) >= 2:
            second = capacity_week[1]
            s2 = _to_float(second.get("net_sales_cr")) / week_cr * 100.0
            bullets.append(
                f"Runner-up: {second.get('capacity_key')} ~{s2:.0f}% of week rating-level sales."
            )

    if capacity_mtd and mtd_qty > 0:
        top_m = max(capacity_mtd, key=lambda r: _to_float(r.get("range_ordered_qty")))
        q = _to_float(top_m.get("range_ordered_qty"))
        share_q = q / mtd_qty * 100.0
        bullets.append(
            f"Range order mix: largest ordered-qty rating is {top_m.get('capacity_key')} "
            f"({share_q:.0f}% of range transformer ordered units)."
        )

    if daily:
        best = max(daily, key=lambda r: _to_float(r.get("net_sales_cr")))
        bullets.append(
            f"Busiest transformer billing day: {best.get('day_label') or best.get('sales_date')} "
            f"at Rs. {_to_float(best.get('net_sales_cr')):.2f} Cr."
        )

    if top_customers:
        c0 = top_customers[0]
        bullets.append(
            f"Top customer (week, transformers): {c0.get('customer_name')} "
            f"Rs. {_to_float(c0.get('net_sales_cr')):.2f} Cr."
        )

    if top_materials:
        m0 = top_materials[0]
        bullets.append(
            f"Top material line: {str(m0.get('material_label') or '')[:60]} "
            f"Rs. {_to_float(m0.get('net_sales_cr')):.2f} Cr."
        )

    if region_rows:
        r0 = region_rows[0]
        bullets.append(
            f"Largest region by transformer sales: {r0.get('region_label')} "
            f"Rs. {_to_float(r0.get('net_sales_cr')):.2f} Cr."
        )

    return {
        "bullets": bullets[:8],
        "week_transformer_net_by_rating_cr": round(week_cr, 4),
        "week_transformer_daily_net_cr": round(daily_cr, 4),
        "mtd_transformer_ordered_qty": round(mtd_qty, 2),
        "rating_buckets_week": len(capacity_week or []),
        "rating_buckets_mtd": len(capacity_mtd or []),
    }


