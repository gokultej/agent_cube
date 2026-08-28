import os
from datetime import datetime, timedelta
from modules.db_connector import DBConnector


def get_date_ranges():
    """
    Report window: last N calendar days ending today (inclusive).
    N is configurable via REPORT_LOOKBACK_DAYS (default 6), clamped 1–90.

    Keys week_start / week_end are reused by queries as the report date range.
    month_start / next_week_* remain calendar-based for MTD-style helpers and vendor due dates.
    """
    today = datetime.now().date()
    try:
        lookback = int(os.getenv("REPORT_LOOKBACK_DAYS", "6"))
    except ValueError:
        lookback = 6
    lookback = max(1, min(lookback, 90))

    week_end = today
    week_start = today - timedelta(days=lookback - 1)

    month_start = today.replace(day=1)
    next_mon = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
    next_sun = next_mon + timedelta(days=6)

    return {
        "today": today,
        "week_start": week_start,
        "week_end": week_end,
        "report_lookback_days": lookback,
        "month_start": month_start,
        "next_week_start": next_mon,
        "next_week_end": next_sun,
        "report_label": (
            f"Last {lookback} days ({week_start.strftime('%b %d')}–{week_end.strftime('%b %d, %Y')})"
        ),
    }

def fetch_this_week(dates):
    """Fetch the single row for last week from weekly_sales_summary."""
    rows = DBConnector.execute_query("""
        SELECT
            id,
            start_date,
            end_date,
            total_sales_value,
            ROUND(total_sales_value / 1e7, 2)   AS sales_cr,
            total_invoices,
            created_at
        FROM weekly_sales_summary
        WHERE end_date = %(week_end)s
           OR (start_date <= %(week_end)s AND end_date >= %(week_start)s)
        ORDER BY end_date DESC
        LIMIT 1
    """, dates)
    return rows[0] if rows else None

def fetch_last_8_weeks():
    """Fetch last 8 weekly rows for trend chart."""
    return DBConnector.execute_query("""
        SELECT
            start_date,
            end_date,
            ROUND(total_sales_value / 1e7, 2)   AS sales_cr,
            total_invoices,
            TO_CHAR(end_date, 'DD Mon')          AS week_label
        FROM weekly_sales_summary
        ORDER BY end_date DESC
        LIMIT 8
    """)

def fetch_wow_growth():
    """Week-on-week growth for last 8 weeks."""
    return DBConnector.execute_query("""
        SELECT
            end_date,
            TO_CHAR(end_date, 'DD Mon')           AS week_label,
            ROUND(total_sales_value / 1e7, 2)     AS sales_cr,
            total_invoices,
            ROUND(
                (total_sales_value
                    - LAG(total_sales_value) OVER (ORDER BY end_date))
                / NULLIF(
                    LAG(total_sales_value) OVER (ORDER BY end_date), 0
                ) * 100
            , 1)                                   AS wow_growth_pct
        FROM weekly_sales_summary
        ORDER BY end_date DESC
        LIMIT 8
    """)

def fetch_mtd_summary(dates):
    """Month-to-date aggregated from weekly rows."""
    rows = DBConnector.execute_query("""
        SELECT
            SUM(total_sales_value)              AS mtd_value,
            ROUND(SUM(total_sales_value)/1e7,2) AS mtd_cr,
            SUM(total_invoices)                 AS mtd_invoices,
            COUNT(*)                            AS weeks_in_month,
            MIN(start_date)                     AS month_from,
            MAX(end_date)                       AS month_to
        FROM weekly_sales_summary
        WHERE start_date >= %(month_start)s
          AND end_date   <= %(today)s
    """, dates)
    return rows[0] if rows else None

def fetch_best_worst_weeks():
    """Best and worst performing weeks in last 12 months."""
    return DBConnector.execute_query("""
        SELECT
            end_date,
            TO_CHAR(end_date, 'DD Mon YYYY')    AS week_label,
            ROUND(total_sales_value/1e7, 2)     AS sales_cr,
            total_invoices
        FROM weekly_sales_summary
        WHERE start_date >= CURRENT_DATE - INTERVAL '12 months'
        ORDER BY total_sales_value DESC
        LIMIT 5
    """)

def check_data_freshness():
    """Verify weekly_sales_summary has recent data — alert if last row is stale."""
    rows = DBConnector.execute_query("""
        SELECT
            MAX(end_date)                       AS latest_week_end,
            CURRENT_DATE - MAX(end_date)        AS lag_days,
            COUNT(*)                            AS total_rows
        FROM weekly_sales_summary
    """)
    return rows[0] if rows else {"lag_days": 0, "latest_week_end": None, "total_rows": 0}