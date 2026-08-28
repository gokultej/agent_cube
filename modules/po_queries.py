"""
PO-focused queries from po_data.
"""
from __future__ import annotations

import logging
from typing import List

from modules.db_connector import DBConnector

log = logging.getLogger(__name__)


def fetch_po_spend_detail(dates: dict, plant: str) -> List[dict]:
    """
    Daily PO spend for the reporting week from po_data, split by po_status.

    Status mapping logic:
      closed       -> po_status ILIKE '%close%' AND NOT '%short%'
      short_closed -> po_status ILIKE '%short%'
      partial      -> open_value > 0 AND grn_amount > 0
      open         -> remaining open records with no GRN amount
    """
    sql = """
        SELECT
            po_creation_date,
            TO_CHAR(po_creation_date, 'DD Mon YYYY')   AS po_date_label,
            COALESCE(SUM(
                CASE WHEN LOWER(po_status) LIKE '%%short%%'
                     THEN po_value_inr ELSE 0 END
            ), 0)                                       AS short_closed_cr,
            COALESCE(SUM(
                CASE WHEN LOWER(po_status) LIKE '%%close%%'
                      AND LOWER(po_status) NOT LIKE '%%short%%'
                     THEN po_value_inr ELSE 0 END
            ), 0)                                       AS closed_cr,
            COALESCE(SUM(
                CASE WHEN LOWER(po_status) NOT LIKE '%%close%%'
                      AND COALESCE(grn_amount, 0) > 0
                      AND COALESCE(open_value, 0) > 0
                     THEN po_value_inr ELSE 0 END
            ), 0)                                       AS partial_cr,
            COALESCE(SUM(
                CASE WHEN LOWER(po_status) NOT LIKE '%%close%%'
                      AND COALESCE(grn_amount, 0) = 0
                     THEN po_value_inr ELSE 0 END
            ), 0)                                       AS open_cr,
            COALESCE(SUM(po_value_inr), 0)             AS subtotal_cr
        FROM po_data
        WHERE po_creation_date BETWEEN %(week_start)s AND %(week_end)s
        GROUP BY po_creation_date
        ORDER BY po_creation_date
    """
    try:
        rows = DBConnector.execute_query(sql, {
            "week_start": dates["week_start"],
            "week_end": dates["week_end"],
        })
        return rows or []
    except Exception as e:
        log.warning("po_data spend detail query failed: %s — PO chart skipped", e)
        return []


def fetch_po_spend_line_items(dates: dict, plant: str, limit: int = 15) -> List[dict]:
    """
    Detailed PO line items for the reporting week from po_data.
    Uses plant_code when available; keeps rows with NULL/blank plant_code as fallback.
    """
    sql = """
        SELECT
            po_number,
            po_item,
            COALESCE(vendor_name, vendor, '')                    AS vendor_name,
            COALESCE(material_description, item_description, '') AS material_description,
            po_creation_date,
            delivery_date,
            po_status,
            COALESCE(ordered_qty, 0)                             AS ordered_qty,
            COALESCE(po_value_inr, 0)                           AS po_value_cr,
            COALESCE(open_value, 0)                             AS open_value_cr,
            COALESCE(grn_amount, 0)                             AS grn_amount_cr
        FROM po_data
        WHERE po_creation_date BETWEEN %(week_start)s AND %(week_end)s
          AND (
                plant_code = %(plant)s
                OR plant_code IS NULL
                OR plant_code = ''
              )
        ORDER BY COALESCE(po_value_inr, 0) DESC, po_creation_date DESC
        LIMIT %(limit)s
    """
    try:
        rows = DBConnector.execute_query(sql, {
            "week_start": dates["week_start"],
            "week_end": dates["week_end"],
            "plant": str(plant),
            "limit": int(limit),
        })
        return rows or []
    except Exception as e:
        log.warning("po_data line-item query failed: %s — PO detail table skipped", e)
        return []
