"""
Assemble the weekly intelligence PDF matching the visual design:
  Page 1 — Header · KPI · Production · Ageing · PO spend · Sentiment · Price trend
  Next page — Global events (section starts after PageBreak) · then recommendations, etc.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

log = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────
NAVY      = colors.HexColor("#1a3a5c")
NAVY_MID  = colors.HexColor("#2563eb")
NAVY_LITE = colors.HexColor("#dbeafe")
GOLD      = colors.HexColor("#f59e0b")
GREEN     = colors.HexColor("#16a34a")
RED       = colors.HexColor("#dc2626")
AMBER     = colors.HexColor("#d97706")
GREY_LITE = colors.HexColor("#f8fafc")
GREY_MID  = colors.HexColor("#e2e8f0")
GREY_TEXT = colors.HexColor("#64748b")
WHITE     = colors.white
BLACK     = colors.black

# Action → colour
ACTION_COLORS = {
    "BUY NOW":      GREEN,
    "DEFER":        RED,
    "DEFER BULK":   RED,
    "NEGOTIATE":    AMBER,
    "RENEGOTIATE":  RED,
    "FORWARD BUY":  GREEN,
    "MONITOR":      GREY_TEXT,
}

ACTION_BG_COLORS = {
    "BUY NOW":     colors.HexColor("#e3ecd9"),
    "FORWARD BUY": colors.HexColor("#e8efdc"),
    "NEGOTIATE":   colors.HexColor("#efe3cf"),
    "RENEGOTIATE": colors.HexColor("#ead9d8"),
    "DEFER":       colors.HexColor("#efe1e1"),
    "DEFER BULK":  colors.HexColor("#e9d9db"),
    "MONITOR":     colors.HexColor("#efe6d5"),
}

SEVERITY_BG_COLORS = {
    "CRITICAL": colors.HexColor("#f3e1e1"),
    "HIGH":     colors.HexColor("#f1e4d1"),
    "MEDIUM":   colors.HexColor("#e5edf8"),
    "LOW":      colors.HexColor("#e7efde"),
}

SEVERITY_TEXT_COLORS = {
    "CRITICAL": "#b91c1c",
    "HIGH":     "#b45309",
    "MEDIUM":   "#1d4ed8",
    "LOW":      "#166534",
}

PAGE_W, PAGE_H = letter
MARGIN = 0.55 * inch
USE_W  = PAGE_W - 2 * MARGIN   # ~7.4"


def _draw_watermark(canvas, doc):
    """Draw a subtle diagonal watermark on every page."""
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 52)
    canvas.setFillColor(colors.HexColor("#cbd5e1"))
    canvas.translate(PAGE_W / 2, PAGE_H / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, "Agent SSai")
    canvas.restoreState()


# ── Utility helpers ──────────────────────────────────────────────────────────
def _esc(text: str) -> str:
    return (
        (str(text) if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _format_po_avg_cell(val: Any, unit: str) -> str:
    """PO-weighted average for PDF (MT or KL); em dash when not available."""
    if val is None:
        return "—"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "—"
    if unit == "KL":
        return f"Rs.{v / 1000:.0f}k/KL"
    return f"Rs.{v / 1000:.0f}k/MT"


def _weekly_report_pdf_basename(report_day: date | datetime | None) -> str:
    now_ts = datetime.now().strftime("%H%M%S")
    if report_day is None:
        d = datetime.now().date()
    elif isinstance(report_day, datetime):
        d = report_day.date()
    else:
        d = report_day
    stamp = f"{d.strftime('%b')}{d.day}_{d.year}"
    return f"Weekly_Market_Intelligence_Report_{stamp}_{now_ts}.pdf"


# ── Style factory ─────────────────────────────────────────────────────────────
def _make_styles():
    base = getSampleStyleSheet()

    def S(name, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    return {
        "title": S("RTitle",
                   fontName="Helvetica-Bold", fontSize=16.5,
                   textColor=WHITE, alignment=TA_CENTER, spaceAfter=5, leading=19),
        "subtitle": S("RSub",
                      fontName="Helvetica", fontSize=9.5,
                      textColor=colors.HexColor("#cbd5e1"),
                      alignment=TA_CENTER, spaceAfter=4, leading=12),
        "sources": S("RSources",
                     fontName="Helvetica", fontSize=7.2,
                     textColor=colors.HexColor("#94a3b8"),
                     alignment=TA_CENTER, leading=10),
        "section": S("RSection",
                     fontName="Helvetica-Bold", fontSize=8,
                     textColor=GREY_TEXT, spaceAfter=4, spaceBefore=10),
        "kpi_big": S("RKpiBig",
                     fontName="Helvetica-Bold", fontSize=19,
                     textColor=NAVY, alignment=TA_CENTER,
                     leading=22, spaceAfter=2),
        "kpi_label": S("RKpiLabel",
                       fontName="Helvetica-Bold", fontSize=8,
                       textColor=NAVY, alignment=TA_CENTER,
                       leading=10, spaceAfter=1),
        "kpi_sub": S("RKpiSub",
                     fontName="Helvetica", fontSize=7,
                     textColor=GREY_TEXT, alignment=TA_CENTER,
                     leading=9),
        "body": S("RBody",
                  fontName="Helvetica", fontSize=8,
                  textColor=colors.HexColor("#374151"), leading=11),
        "body_sm": S("RBodySm",
                     fontName="Helvetica", fontSize=7,
                     textColor=colors.HexColor("#374151"), leading=10),
        "impact": S("RImpact",
                    fontName="Helvetica-Bold", fontSize=7,
                    textColor=colors.HexColor("#0369a1")),
        "event_title": S("REvTitle",
                         fontName="Helvetica-Bold", fontSize=8,
                         textColor=colors.HexColor("#0f172a")),
        "rec_action": S("RRecAction",
                        fontName="Helvetica-Bold", fontSize=8,
                        textColor=WHITE, alignment=TA_LEFT),
        "rec_material": S("RRecMat",
                          fontName="Helvetica-Bold", fontSize=8,
                          textColor=colors.HexColor("#0f172a")),
        "rec_body": S("RRecBody",
                      fontName="Helvetica", fontSize=7,
                      textColor=colors.HexColor("#374151"), leading=10),
        "footer": S("RFooter",
                    fontName="Helvetica", fontSize=6,
                    textColor=GREY_TEXT, alignment=TA_CENTER),
        "tbl_hdr": S("RTblHdr",
                     fontName="Helvetica-Bold", fontSize=7,
                     textColor=WHITE, alignment=TA_CENTER),
        "tbl_cell": S("RTblCell",
                      fontName="Helvetica", fontSize=7,
                      textColor=colors.HexColor("#374151"), alignment=TA_CENTER),
        "tbl_cell_bold": S("RTblCellB",
                           fontName="Helvetica-Bold", fontSize=7,
                           textColor=colors.HexColor("#0f172a"), alignment=TA_CENTER),
        "tbl_cell_left": S("RTblCellL",
                           fontName="Helvetica", fontSize=6.5,
                           textColor=colors.HexColor("#374151"), alignment=TA_LEFT),
    }


# ── Section header helper ─────────────────────────────────────────────────────
def _section_header(title: str, st: dict) -> List:
    return [
        Paragraph(_esc(title).upper(), st["section"]),
        HRFlowable(width="100%", thickness=0.5, color=GREY_MID, spaceAfter=6),
    ]


def _section_header_box(title: str, st: dict) -> List:
    """Box-style section title with background fill."""
    cell = [[Paragraph(_esc(title).upper(), st["section"])]]
    t = Table(cell, colWidths=[USE_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY_LITE),
        ("BOX", (0, 0), (-1, -1), 0.6, GREY_MID),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [t, Spacer(1, 0.05 * inch)]


def _subtle_label_box(text: str, st: dict) -> Table:
    """Small premium-looking subtitle chip used above key tables."""
    cell = [[Paragraph(_esc(text), st["body_sm"])]]
    t = Table(cell, colWidths=[USE_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#bfdbfe")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ── Page 1 building blocks ────────────────────────────────────────────────────
def _header_block(label: str, generated: str, st: dict) -> Table:
    """Dark navy full-width header with title, period, data sources."""
    period_line = f"Period: {_esc(label)}  |  Generated: {generated}"
    sources_line = (
        "Integrated with: LME \u00b7 MCX India \u00b7 Trading Economics \u00b7 "
        "IEEMA \u00b7 RBI \u00b7 CEA \u00b7 Alpha Vantage"
    )
    cell = [
        Spacer(1, 0.30 * inch),
        Paragraph("Weekly Sales &amp; Market Intelligence Report", st["title"]),
        Paragraph(_esc(period_line), st["subtitle"]),
        Paragraph(_esc(sources_line), st["sources"]),
        Spacer(1, 0.18 * inch),
    ]
    t = Table([[cell]], colWidths=[USE_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("ROUNDEDCORNERS", [4]),
        ("LEFTPADDING",  (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    return t


def _kpi_card(big: str, label: str, sublabel: str, st: dict,
              highlight: bool = False) -> List:
    bg = NAVY_LITE if highlight else GREY_LITE
    return [
        Spacer(1, 0.09 * inch),
        Paragraph(_esc(big),      st["kpi_big"]),
        Paragraph(_esc(label),    st["kpi_label"]),
        Paragraph(_esc(sublabel), st["kpi_sub"]),
        Spacer(1, 0.09 * inch),
    ]


def _kpi_row(sales_kpis: dict, dates: dict, st: dict) -> Table:
    net_sales = _to_float(sales_kpis.get("total_net_sales_cr"))
    planned   = int(_to_float(sales_kpis.get("month_planned_units")))
    achieved  = int(_to_float(sales_kpis.get("month_achieved_units")))
    po_val    = _to_float(sales_kpis.get("total_po_value_cr"))
    label     = str(dates.get("report_label") or "This week")

    cells = [
        _kpi_card(f"{net_sales:.2f} Cr", "Total Net Sales",  label,            st, True),
        _kpi_card(f"{planned:,}",         "Month Planned",    "Units this month", st),
        _kpi_card(f"{achieved:,}",         "Month Achieved",   "Units so far",     st),
        _kpi_card(f"{po_val:.2f} Cr",     "Total PO Value",   "This week total",  st, True),
    ]
    col_w = USE_W / 4
    t = Table([cells], colWidths=[col_w] * 4)
    t.setStyle(TableStyle([
        ("BOX",          (0, 0), (-1, -1), 0.5, GREY_MID),
        ("INNERGRID",    (0, 0), (-1, -1), 0.5, GREY_MID),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
    ]))
    return t


def _format_kva_display_label(capacity_key: str) -> str:
    """Pretty label for capacity_kva / rating (e.g. 10 -> '10 KVA')."""
    k = (capacity_key or "").strip()
    if not k or k == "Unspecified":
        return "Unspecified"
    low = k.lower()
    if "kva" in low or "mva" in low:
        return _esc(k)
    try:
        num = float(k.replace(",", ""))
        if abs(num - round(num)) < 1e-9:
            return _esc(f"{int(round(num))} KVA")
        return _esc(f"{num:g} KVA")
    except ValueError:
        return _esc(k)


def _weekly_sales_by_capacity_table(rows: List[dict], st: dict) -> Table | None:
    if not rows:
        return None
    headers = ["Rating", "Net Sales (Cr)", "Invoices", "Ordered Quantity"]
    out = [[Paragraph(_esc(h), st["tbl_hdr"]) for h in headers]]
    for r in rows:
        key = str(r.get("capacity_key") or "")
        net = _to_float(r.get("net_sales_cr"))
        inv = int(_to_float(r.get("invoice_count")))
        qty = _to_float(r.get("ordered_qty"))
        qty_s = f"{qty:,.0f}" if qty == int(qty) else f"{qty:,.2f}"
        out.append([
            Paragraph(_format_kva_display_label(key), st["tbl_cell_left"]),
            Paragraph(f"{net:.2f}", st["tbl_cell"]),
            Paragraph(str(inv), st["tbl_cell"]),
            Paragraph(qty_s, st["tbl_cell"]),
        ])
    col_ws = [1.55 * inch, 1.15 * inch, 0.95 * inch, 1.15 * inch]
    tw = sum(col_ws)
    if tw > USE_W:
        s = USE_W / tw
        col_ws = [w * s for w in col_ws]
    t = Table(out, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN",         (1, 1), (-1, -1), "CENTER"),
    ]))
    return t


def _mtd_orders_by_capacity_table(rows: List[dict], st: dict) -> Table | None:
    if not rows:
        return None
    headers = ["Rating", "Report-Period Ordered Quantity", "Report-Period Net Sales (Cr)"]
    out = [[Paragraph(_esc(h), st["tbl_hdr"]) for h in headers]]
    for r in rows:
        key = str(r.get("capacity_key") or "")
        qty = _to_float(r.get("range_ordered_qty"))
        net = _to_float(r.get("mtd_net_sales_cr"))
        qty_s = f"{qty:,.0f}" if qty == int(qty) else f"{qty:,.2f}"
        out.append([
            Paragraph(_format_kva_display_label(key), st["tbl_cell_left"]),
            Paragraph(qty_s, st["tbl_cell"]),
            Paragraph(f"{net:.2f}", st["tbl_cell"]),
        ])
    col_ws = [1.85 * inch, 1.35 * inch, 1.25 * inch]
    tw = sum(col_ws)
    if tw > USE_W:
        s = USE_W / tw
        col_ws = [w * s for w in col_ws]
    t = Table(out, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN",         (1, 1), (-1, -1), "CENTER"),
    ]))
    return t


def _transformer_top_customers_table(rows: List[dict], st: dict) -> Table | None:
    if not rows:
        return None
    hdr = ["Customer", "Region", "Net Sales (Cr)", "Invoices", "Quantity"]
    out = [[Paragraph(_esc(h), st["tbl_hdr"]) for h in hdr]]
    for r in rows:
        nm = _esc(str(r.get("customer_name") or "")[:36])
        reg = _esc(str(r.get("region_label") or "")[:22])
        net = _to_float(r.get("net_sales_cr"))
        inv = int(_to_float(r.get("invoice_count")))
        qty = _to_float(r.get("ordered_qty"))
        qty_s = f"{qty:,.0f}" if abs(qty - round(qty)) < 1e-6 else f"{qty:,.1f}"
        out.append([
            Paragraph(nm, st["tbl_cell_left"]),
            Paragraph(reg, st["tbl_cell_left"]),
            Paragraph(f"{net:.2f}", st["tbl_cell"]),
            Paragraph(str(inv), st["tbl_cell"]),
            Paragraph(qty_s, st["tbl_cell"]),
        ])
    col_ws = [1.85 * inch, 1.1 * inch, 0.78 * inch, 0.52 * inch, 0.75 * inch]
    tw = sum(col_ws)
    if tw > USE_W:
        s = USE_W / tw
        col_ws = [w * s for w in col_ws]
    t = Table(out, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN",         (2, 1), (-1, -1), "CENTER"),
    ]))
    return t


def _transformer_top_materials_table(rows: List[dict], st: dict) -> Table | None:
    if not rows:
        return None
    hdr = ["Material", "Net Sales (Cr)", "Quantity", "Invoices"]
    out = [[Paragraph(_esc(h), st["tbl_hdr"]) for h in hdr]]
    for r in rows:
        mat = _esc(str(r.get("material_label") or "")[:52])
        net = _to_float(r.get("net_sales_cr"))
        qty = _to_float(r.get("ordered_qty"))
        inv = int(_to_float(r.get("invoice_count")))
        qty_s = f"{qty:,.0f}" if abs(qty - round(qty)) < 1e-6 else f"{qty:,.1f}"
        out.append([
            Paragraph(mat, st["tbl_cell_left"]),
            Paragraph(f"{net:.2f}", st["tbl_cell"]),
            Paragraph(qty_s, st["tbl_cell"]),
            Paragraph(str(inv), st["tbl_cell"]),
        ])
    col_ws = [3.35 * inch, 0.85 * inch, 0.85 * inch, 0.55 * inch]
    tw = sum(col_ws)
    if tw > USE_W:
        s = USE_W / tw
        col_ws = [w * s for w in col_ws]
    t = Table(out, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("ALIGN",         (1, 1), (-1, -1), "CENTER"),
    ]))
    return t


def _side_by_side_charts(path_left: str | None, path_right: str | None) -> Table:
    """Two charts displayed side-by-side."""
    col_w = USE_W / 2 - 0.05 * inch
    ch    = 2.8 * inch

    def _img(p):
        if p and os.path.isfile(p):
            return Image(p, width=col_w, height=ch)
        return Spacer(col_w, ch)

    t = Table([[_img(path_left), _img(path_right)]], colWidths=[col_w, col_w])
    t.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    return t


def _full_chart(path: str | None, height: float = 3.0) -> Image | Spacer:
    if path and os.path.isfile(path):
        return Image(path, width=USE_W, height=height * inch)
    return Spacer(USE_W, height * inch)


def _customer_ageing_table(customer_summary: dict, st: dict) -> Table | None:
    if not customer_summary:
        return None
    total_outstanding = _to_float(customer_summary.get("total_outstanding")) / 1e7
    not_due_amount = _to_float(customer_summary.get("not_due_amount")) / 1e7
    rows = [
        [Paragraph("Total Customers", st["tbl_hdr"]), Paragraph("Outstanding (Cr)", st["tbl_hdr"]),
         Paragraph("Not Due (Cr)", st["tbl_hdr"]), Paragraph(">60 Days (Cr)", st["tbl_hdr"])],
        [
            Paragraph(str(int(_to_float(customer_summary.get("total_customers")))), st["tbl_cell_bold"]),
            Paragraph(f"{total_outstanding:.2f}", st["tbl_cell"]),
            Paragraph(f"{not_due_amount:.2f}", st["tbl_cell"]),
            Paragraph(
                f"{((_to_float(customer_summary.get('bucket_61_90')) + _to_float(customer_summary.get('bucket_91_120')) + _to_float(customer_summary.get('bucket_121_180')) + _to_float(customer_summary.get('bucket_181_360')) + _to_float(customer_summary.get('bucket_361_plus'))) / 1e7):.2f}",
                st["tbl_cell"],
            ),
        ],
        [Paragraph("1-30", st["tbl_hdr"]), Paragraph("31-60", st["tbl_hdr"]),
         Paragraph("61-120", st["tbl_hdr"]), Paragraph("121+ days", st["tbl_hdr"])],
        [
            Paragraph(f"{_to_float(customer_summary.get('bucket_01_30')) / 1e7:.2f}", st["tbl_cell"]),
            Paragraph(f"{_to_float(customer_summary.get('bucket_31_60')) / 1e7:.2f}", st["tbl_cell"]),
            Paragraph(f"{(_to_float(customer_summary.get('bucket_61_90')) + _to_float(customer_summary.get('bucket_91_120'))) / 1e7:.2f}", st["tbl_cell"]),
            Paragraph(f"{(_to_float(customer_summary.get('bucket_121_180')) + _to_float(customer_summary.get('bucket_181_360')) + _to_float(customer_summary.get('bucket_361_plus'))) / 1e7:.2f}", st["tbl_cell"]),
        ],
    ]
    t = Table(rows, colWidths=[USE_W / 4] * 4)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BACKGROUND", (0, 2), (-1, 2), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("TEXTCOLOR", (0, 2), (-1, 2), WHITE),
        ("GRID", (0, 0), (-1, -1), 0.4, GREY_MID),
        ("BACKGROUND", (0, 1), (-1, 1), GREY_LITE),
        ("BACKGROUND", (0, 3), (-1, 3), WHITE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _top_vendors_table(top_vendors: List[dict], st: dict) -> Table | None:
    """Top vendors by gross outstanding (from vendor_ageing_prod aggregates)."""
    if not top_vendors:
        return None

    headers = [
        "#",
        "Vendor",
        "Supplier",
        "Gross (Cr)",
        ">60 d (Cr)",
        "Max days o/d",
    ]
    rows: List[List[Any]] = [[Paragraph(_esc(h), st["tbl_hdr"]) for h in headers]]

    for i, r in enumerate(top_vendors, start=1):
        vname = _esc(str(r.get("vendor_name") or "")[:48])
        supp = _esc(str(r.get("supplier") or "").strip() or "—")[:32]
        gross = _to_float(r.get("gross_outstanding"))
        overdue = _to_float(r.get("overdue_60plus"))
        max_days = r.get("max_days_overdue")
        try:
            max_days_i = int(float(max_days)) if max_days is not None else "—"
        except (TypeError, ValueError):
            max_days_i = "—"

        rows.append([
            Paragraph(str(i), st["tbl_cell"]),
            Paragraph(vname, st["tbl_cell_left"]),
            Paragraph(supp, st["tbl_cell_left"]),
            Paragraph(f"{gross / 1e7:.2f}" if gross else "—", st["tbl_cell"]),
            Paragraph(f"{overdue / 1e7:.2f}" if overdue else "—", st["tbl_cell"]),
            Paragraph(str(max_days_i), st["tbl_cell"]),
        ])

    col_ws = [0.32 * inch, 2.35 * inch, 1.25 * inch, 0.95 * inch, 0.95 * inch, 0.78 * inch]
    total_w = sum(col_ws)
    if total_w > USE_W:
        scale = USE_W / total_w
        col_ws = [w * scale for w in col_ws]

    t = Table(rows, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 6.8),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN",         (0, 1), (0, -1), "CENTER"),
        ("ALIGN",         (3, 1), (-1, -1), "CENTER"),
    ]))
    return t


# ── Page 2 building blocks ────────────────────────────────────────────────────
def _event_cell(event: dict | None, st: dict) -> List:
    """Single event card content for use inside a Table cell."""
    if not event:
        return [Spacer(1, 0.1 * inch)]
    title   = _esc(str(event.get("title") or event.get("headline") or ""))
    desc    = _esc(str(event.get("description") or event.get("summary") or ""))
    impacts = event.get("impacts") or event.get("material_impacts") or []
    if isinstance(impacts, str):
        impacts = [impacts]
    impact_str = " \u00b7 ".join(_esc(str(i)) for i in impacts[:2])
    severity = _esc(str(event.get("severity") or "MEDIUM")).upper()
    source = _esc(str(event.get("source") or ""))

    content = [
        Spacer(1, 0.06 * inch),
        Paragraph(
            f'<font color="{SEVERITY_TEXT_COLORS.get(severity, "#64748b")}"><b>{severity}</b></font>',
            st["body_sm"],
        ),
        Spacer(1, 0.02 * inch),
        Paragraph(title, st["event_title"]),
        Spacer(1, 0.04 * inch),
        Paragraph(desc,  st["body_sm"]),
    ]
    if impact_str:
        content += [
            Spacer(1, 0.04 * inch),
            Paragraph(f"Impact: {impact_str}", st["impact"]),
        ]
    if source:
        content += [
            Spacer(1, 0.03 * inch),
            Paragraph(f"Source: {source}", st["body_sm"]),
        ]
    content.append(Spacer(1, 0.06 * inch))
    return content


def _events_grid(events: List[dict], st: dict) -> Table | None:
    if not events:
        return None
    rows = []
    row_events: List[List[dict | None]] = []
    for i in range(0, min(len(events), 6), 2):
        ev_left = events[i]
        ev_right = events[i + 1] if i + 1 < len(events) else None
        left  = _event_cell(ev_left, st)
        right = _event_cell(ev_right, st)
        rows.append([left, right])
        row_events.append([ev_left, ev_right])

    col_w = USE_W / 2 - 0.05 * inch
    t = Table(rows, colWidths=[col_w, col_w])
    style = [
        ("BOX",          (0, 0), (-1, -1), 0.5, GREY_MID),
        ("INNERGRID",    (0, 0), (-1, -1), 0.5, GREY_MID),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]
    # Per-card colouring by event severity
    for r, pair in enumerate(row_events):
        for c, ev in enumerate(pair):
            sev = str((ev or {}).get("severity") or "MEDIUM").upper()
            bg = SEVERITY_BG_COLORS.get(sev, GREY_LITE)
            style.append(("BACKGROUND", (c, r), (c, r), bg))
    t.setStyle(TableStyle(style))
    return t


def _po_table(po_spend: List[dict], st: dict) -> Table | None:
    if not po_spend:
        return None
    headers = ["PO Date", "Closed (Cr)", "Open (Cr)", "Partial (Cr)",
               "Short-Closed (Cr)", "Subtotal (Cr)"]
    rows = [[Paragraph(h, st["tbl_hdr"]) for h in headers]]

    total_closed = total_open = total_partial = total_sc = total_sub = 0.0
    for r in po_spend:
        c  = _to_float(r.get("closed_cr"))
        o  = _to_float(r.get("open_cr"))
        p  = _to_float(r.get("partial_cr"))
        sc = _to_float(r.get("short_closed_cr"))
        s  = _to_float(r.get("subtotal_cr"))
        total_closed  += c
        total_open    += o
        total_partial += p
        total_sc      += sc
        total_sub     += s

        def _cell(v: float):
            return Paragraph(f"{v:.2f}" if v else "—", st["tbl_cell"])

        date_lbl = str(r.get("po_date_label") or r.get("po_date") or "")
        rows.append([
            Paragraph(_esc(date_lbl), st["tbl_cell"]),
            _cell(c), _cell(o), _cell(p), _cell(sc), _cell(s),
        ])

    def _bold(v: float):
        return Paragraph(f"{v:.2f}", st["tbl_cell_bold"])

    rows.append([
        Paragraph("Total", st["tbl_cell_bold"]),
        _bold(total_closed), _bold(total_open),
        _bold(total_partial), _bold(total_sc), _bold(total_sub),
    ])

    col_ws = [1.4*inch, 1.0*inch, 1.0*inch, 1.0*inch, 1.2*inch, 1.0*inch]
    # Trim to USE_W if needed
    total_w = sum(col_ws)
    if total_w > USE_W:
        scale = USE_W / total_w
        col_ws = [w * scale for w in col_ws]

    t = Table(rows, colWidths=col_ws)
    n = len(rows)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",    (0, 0), (-1, 0), WHITE),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, -1), 7),
        ("GRID",         (0, 0), (-1, -1), 0.4, GREY_MID),
        ("BACKGROUND",   (0, n - 1), (-1, n - 1), GREY_LITE),
        ("ROWBACKGROUNDS",(0, 1), (-1, n - 2), [WHITE, GREY_LITE]),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    return t


def _po_line_items_table(po_lines: List[dict], st: dict) -> Table | None:
    if not po_lines:
        return None

    headers = [
        "PO Number", "Item", "Vendor", "Material",
        "PO Date", "Status", "PO Value (Cr)", "Open (Cr)"
    ]
    rows = [[Paragraph(h, st["tbl_hdr"]) for h in headers]]

    for r in po_lines:
        po_number = _esc(str(r.get("po_number") or ""))
        po_item = _esc(str(r.get("po_item") or ""))
        vendor = _esc(str(r.get("vendor_name") or ""))[:30]
        material = _esc(str(r.get("material_description") or ""))[:38]
        po_date = _esc(str(r.get("po_creation_date") or ""))
        status = _esc(str(r.get("po_status") or ""))[:18]
        po_value = _to_float(r.get("po_value_cr"))
        open_value = _to_float(r.get("open_value_cr"))

        rows.append([
            Paragraph(po_number, st["tbl_cell"]),
            Paragraph(po_item, st["tbl_cell"]),
            Paragraph(vendor, st["tbl_cell"]),
            Paragraph(material, st["tbl_cell"]),
            Paragraph(po_date, st["tbl_cell"]),
            Paragraph(status, st["tbl_cell"]),
            Paragraph(f"{po_value:.2f}" if po_value else "—", st["tbl_cell"]),
            Paragraph(f"{open_value:.2f}" if open_value else "—", st["tbl_cell"]),
        ])

    col_ws = [0.95*inch, 0.45*inch, 1.15*inch, 1.35*inch, 0.75*inch, 0.85*inch, 0.9*inch, 0.8*inch]
    total_w = sum(col_ws)
    if total_w > USE_W:
        scale = USE_W / total_w
        col_ws = [w * scale for w in col_ws]

    t = Table(rows, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 6.8),
        ("GRID",          (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("TOPPADDING",    (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    return t


def _market_sentiment_details_box(analysis: dict, st: dict) -> Table:
    msi = analysis.get("market_sentiment_index", {}) or {}
    score = _to_float(msi.get("score"), 50.0)
    zone = _esc(msi.get("zone") or "NEUTRAL")
    zone_counts = msi.get("zone_counts", {}) or {}
    top_bullish = ", ".join(msi.get("top_bullish_materials") or ["N/A"])
    top_bearish = ", ".join(msi.get("top_bearish_materials") or ["N/A"])
    pressure = msi.get("event_pressure", {}) or {}

    rows = [
        [Paragraph("<b>Overall Index</b>", st["body_sm"]),
         Paragraph(f"{score:.1f}/100 ({zone})", st["body_sm"])],
        [Paragraph("<b>Zone Split</b>", st["body_sm"]),
         Paragraph(
             f"Bullish: {int(_to_float(zone_counts.get('BULLISH')))} | "
             f"Neutral: {int(_to_float(zone_counts.get('NEUTRAL')))} | "
             f"Bearish: {int(_to_float(zone_counts.get('BEARISH')))}",
             st["body_sm"],
         )],
        [Paragraph("<b>Top Bullish</b>", st["body_sm"]),
         Paragraph(_esc(top_bullish), st["body_sm"])],
        [Paragraph("<b>Top Bearish</b>", st["body_sm"]),
         Paragraph(_esc(top_bearish), st["body_sm"])],
        [Paragraph("<b>Event Pressure</b>", st["body_sm"]),
         Paragraph(
             f"Critical events: {int(_to_float(pressure.get('CRITICAL')))} | "
             f"High events: {int(_to_float(pressure.get('HIGH')))}",
             st["body_sm"],
         )],
    ]
    t = Table(rows, colWidths=[1.6 * inch, USE_W - (1.6 * inch)])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREY_LITE),
        ("BOX", (0, 0), (-1, -1), 0.5, GREY_MID),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, GREY_MID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _is_fallback_source(status: Any, source: Any) -> bool:
    s = str(status or "").strip().lower()
    src = str(source or "").strip().lower()
    if s == "internal_historical":
        return False
    if s in {"fallback", "manual", "computed", "db_fallback"}:
        return True
    return any(x in src for x in ("fallback", "reference", "manual", "computed", "db"))


def _market_source_status_table(market_data: dict, st: dict) -> Table | None:
    rows = []
    fallback_flags = []
    entries = [
        (
            "Copper",
            "copper_source",
            "copper_status",
            "copper_po_avg_inr_mt",
            "copper_market_benchmark_inr_mt",
            "MT",
        ),
        (
            "Aluminium",
            "aluminium_source",
            "aluminium_status",
            "aluminium_po_avg_inr_mt",
            "aluminium_market_benchmark_inr_mt",
            "MT",
        ),
        (
            "CRGO Steel",
            "crgo_source",
            "crgo_status",
            "crgo_po_avg_inr_mt",
            "crgo_market_benchmark_inr_mt",
            "MT",
        ),
        (
            "Amorphous",
            "amorphous_source",
            "amorphous_status",
            "amorphous_po_avg_inr_mt",
            "amorphous_market_benchmark_inr_mt",
            "MT",
        ),
        (
            "Transformer Oil",
            "oil_source",
            "oil_status",
            "transformer_oil_po_avg_inr_kl",
            "transformer_oil_market_benchmark_inr_kl",
            "KL",
        ),
        (
            "HR Steel",
            "hr_steel_source",
            "hr_steel_status",
            "hr_steel_po_avg_inr_mt",
            "hr_steel_market_benchmark_inr_mt",
            "MT",
        ),
    ]
    for material, src_key, st_key, avg_key, live_key, avg_unit in entries:
        source = market_data.get(src_key)
        status = market_data.get(st_key)
        if not source and not status:
            continue
        fallback = _is_fallback_source(status, source)
        fallback_flags.append(fallback)
        avg_cell = _format_po_avg_cell(market_data.get(avg_key), avg_unit)
        live_val = market_data.get(live_key)
        if live_val is None and material == "Transformer Oil":
            live_val = market_data.get("transformer_oil_inr_kl")
        status_norm = str(status or "").strip().lower()
        # Live feed, or internal PO reference when no external oil index is configured.
        if live_val is not None and status_norm in ("live", "internal_historical"):
            live_cell = _format_po_avg_cell(live_val, avg_unit)
        else:
            live_cell = "N/A"
        rows.append([
            Paragraph(_esc(material), st["tbl_cell"]),
            Paragraph(_esc(str(source or "N/A")), st["tbl_cell"]),
            Paragraph(_esc(str(status or "unknown")).upper(), st["tbl_cell"]),
            Paragraph(_esc(avg_cell), st["tbl_cell"]),
            Paragraph(_esc(live_cell), st["tbl_cell"]),
            Paragraph("YES" if fallback else "NO", st["tbl_cell_bold" if fallback else "tbl_cell"]),
        ])
    if not rows:
        return None

    header = [[
        Paragraph("Material", st["tbl_hdr"]),
        Paragraph("Source", st["tbl_hdr"]),
        Paragraph("Status", st["tbl_hdr"]),
        Paragraph("PO avg (long)", st["tbl_hdr"]),
        Paragraph("Recent / live ref.", st["tbl_hdr"]),
        Paragraph("Fallback", st["tbl_hdr"]),
    ]]
    t = Table(
        header + rows,
        colWidths=[0.85 * inch, 2.05 * inch, 0.9 * inch, 0.8 * inch, 0.95 * inch, 0.6 * inch],
    )
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("GRID", (0, 0), (-1, -1), 0.4, GREY_MID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, is_fallback in enumerate(fallback_flags, start=1):
        if is_fallback:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff7ed")))
        else:
            style.append(("BACKGROUND", (0, i), (-1, i), WHITE if i % 2 else GREY_LITE))
    t.setStyle(TableStyle(style))
    return t


def _market_price_variance_table(market_data: dict, st: dict) -> Table | None:
    """
    Compare actual internal PO average vs current market for key materials.
    Difference is (market - actual): positive = Saved, negative = Lost.
    """
    rows = []
    def _has_reliable_market(status: Any, source: Any, material: str = "") -> bool:
        status_s = str(status or "").strip().lower()
        source_s = str(source or "").strip().lower()
        if status_s == "live":
            return True
        # Transformer oil has no licensed Platts feed in most deployments; show internal PO benchmark.
        if material == "Transformer Oil" and status_s == "internal_historical":
            return True
        # Suppress rows when only internal/manual/fallback reference exists.
        blocked = ("internal_historical", "manual", "fallback", "unknown", "")
        if status_s in blocked:
            return False
        if any(tok in source_s for tok in ("internal historical", "fallback", "reference price")):
            return False
        return True

    specs = [
        ("Copper", "copper_po_avg_inr_mt", "copper_market_benchmark_inr_mt", "copper_lme_inr_mt", "copper_market_benchmark_status", "copper_market_benchmark_source", "MT"),
        ("Aluminium", "aluminium_po_avg_inr_mt", "aluminium_market_benchmark_inr_mt", "aluminium_lme_inr_mt", "aluminium_market_benchmark_status", "aluminium_market_benchmark_source", "MT"),
        ("CRGO Steel", "crgo_po_avg_inr_mt", "crgo_market_benchmark_inr_mt", "crgo_steel_inr_mt", "crgo_market_benchmark_status", "crgo_market_benchmark_source", "MT"),
        ("Amorphous", "amorphous_po_avg_inr_mt", "amorphous_market_benchmark_inr_mt", "amorphous_core_inr_mt", "amorphous_market_benchmark_status", "amorphous_market_benchmark_source", "MT"),
        ("HR Steel", "hr_steel_po_avg_inr_mt", "hr_steel_market_benchmark_inr_mt", "hr_steel_inr_mt", "hr_steel_market_benchmark_status", "hr_steel_market_benchmark_source", "MT"),
        ("Transformer Oil", "transformer_oil_po_avg_inr_kl", "transformer_oil_market_benchmark_inr_kl", "transformer_oil_inr_kl", "transformer_oil_market_benchmark_status", "transformer_oil_market_benchmark_source", "KL"),
    ]

    for material, actual_key, benchmark_key, market_fallback_key, status_key, source_key, unit in specs:
        actual = market_data.get(actual_key)
        market = market_data.get(benchmark_key)
        if market is None:
            market = market_data.get(market_fallback_key)
        market_status = market_data.get(status_key)
        market_source = market_data.get(source_key)
        if not _has_reliable_market(market_status, market_source, material):
            continue
        if actual is None and market is None:
            continue

        a = _to_float(actual, None) if actual is not None else None
        m = _to_float(market, None) if market is not None else None
        if a is None or m is None:
            diff_s = "N/A"
            outcome = "N/A"
        else:
            diff = m - a
            if abs(diff) < 1e-9:
                diff_s = "0"
                outcome = "Flat"
            elif diff > 0:
                diff_s = f"+{diff:,.0f}"
                outcome = f"Saved Rs.{abs(diff):,.0f}/{unit}"
            else:
                diff_s = f"{diff:,.0f}"
                outcome = f"Lost Rs.{abs(diff):,.0f}/{unit}"

        rows.append([
            Paragraph(_esc(material), st["tbl_cell"]),
            Paragraph(_esc(f"{a:,.0f}" if a is not None else "N/A"), st["tbl_cell"]),
            Paragraph(_esc(f"{m:,.0f}" if m is not None else "N/A"), st["tbl_cell"]),
            Paragraph(_esc(diff_s), st["tbl_cell"]),
            Paragraph(_esc(outcome), st["tbl_cell_left"]),
        ])

    if not rows:
        return None

    header = [[
        Paragraph("Material", st["tbl_hdr"]),
        Paragraph("PO Avg", st["tbl_hdr"]),
        Paragraph("Market", st["tbl_hdr"]),
        Paragraph("Variance", st["tbl_hdr"]),
        Paragraph("Impact", st["tbl_hdr"]),
    ]]
    t = Table(header + rows, colWidths=[1.15 * inch, 1.1 * inch, 1.05 * inch, 1.05 * inch, 3.05 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("GRID", (0, 0), (-1, -1), 0.35, GREY_MID),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GREY_LITE]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (1, 1), (3, -1), "CENTER"),
    ]))
    return t


# ── Page 3 building blocks ────────────────────────────────────────────────────
def _rec_card(rec: dict | None, st: dict) -> List:
    """Single recommendation card content."""
    if not rec:
        return [Spacer(1, 0.1 * inch)]

    raw_action = str(rec.get("action") or "MONITOR").upper()
    # Normalise action key for colour lookup
    action_key = next(
        (k for k in ACTION_COLORS if raw_action.startswith(k)), "MONITOR"
    )
    action_color = ACTION_COLORS.get(action_key, GREY_TEXT)
    material  = _esc(str(rec.get("material") or ""))
    rationale = _esc(str(rec.get("rationale") or ""))
    saving    = _to_float(rec.get("saving_cr"))
    risk      = _to_float(rec.get("risk_cr"))

    action_style = ParagraphStyle(
        "RRecActionDyn",
        fontName="Helvetica-Bold", fontSize=8,
        textColor=action_color, spaceAfter=3,
    )
    content = [
        Spacer(1, 0.07 * inch),
        Paragraph(raw_action, action_style),
        Paragraph(material,   st["rec_material"]),
        Spacer(1, 0.04 * inch),
        Paragraph(rationale,  st["rec_body"]),
    ]
    if saving:
        content.append(
            Paragraph(f"Saving: Rs.{saving:.2f} Cr", st["rec_body"])
        )
    if risk:
        content.append(
            Paragraph(f"Risk: Rs.{risk:.2f} Cr", st["rec_body"])
        )
    content.append(Spacer(1, 0.07 * inch))
    return content


def _recommendations_grid(recs: List[dict], st: dict) -> Table | None:
    if not recs:
        return None
    rows = []
    row_actions: List[List[str]] = []
    for i in range(0, len(recs), 3):
        row = []
        action_row: List[str] = []
        for j in range(i, i + 3):
            rec = recs[j] if j < len(recs) else None
            row.append(_rec_card(rec, st))
            action_row.append(str((rec or {}).get("action") or "MONITOR").upper())
        rows.append(row)
        row_actions.append(action_row)

    col_w = USE_W / 3
    t = Table(rows, colWidths=[col_w] * 3)
    style = [
        ("BOX",          (0, 0), (-1, -1), 0.5, GREY_MID),
        ("INNERGRID",    (0, 0), (-1, -1), 0.5, GREY_MID),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]
    # Per-card colouring by action type
    for r, actions in enumerate(row_actions):
        for c, raw_action in enumerate(actions):
            action_key = next((k for k in ACTION_BG_COLORS if raw_action.startswith(k)), "MONITOR")
            style.append(("BACKGROUND", (c, r), (c, r), ACTION_BG_COLORS.get(action_key, GREY_LITE)))
    t.setStyle(TableStyle(style))
    return t


# ── Main entry point ──────────────────────────────────────────────────────────
def generate_pdf_report(full_payload: dict) -> str:
    """Build the PDF and return its absolute path."""
    dates      = full_payload.get("dates") or {}
    today      = dates.get("today")
    label      = str(dates.get("report_label") or "Weekly report")
    sales_kpis = full_payload.get("sales_kpis") or {}
    co         = full_payload.get("claude_output") or {}
    charts     = full_payload.get("charts") or {}
    events     = full_payload.get("events") or []
    po_spend   = full_payload.get("po_spend") or []
    po_lines   = full_payload.get("po_spend_lines") or []
    market_data = full_payload.get("market_data") or {}
    customer_summary = full_payload.get("customer_summary") or {}
    top_vendors  = full_payload.get("top_vendors") or []
    analysis   = full_payload.get("analysis") or {}

    generated_str = (
        today.strftime("%-d %b %Y")
        if hasattr(today, "strftime")
        else str(today or "")
    )

    os.makedirs("output", exist_ok=True)
    pdf_path = os.path.abspath(
        os.path.join("output", _weekly_report_pdf_basename(today))
    )

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        rightMargin=MARGIN,
        leftMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

    st    = _make_styles()
    story: List[Any] = []

    # ── PAGE 1 ────────────────────────────────────────────────────────────────
    # Header
    story.append(_header_block(label, generated_str, st))
    story.append(Spacer(1, 0.15 * inch))

    # KPI cards
    story.extend(_section_header_box("Key Performance Indicators — This Week", st))
    story.append(_kpi_row(sales_kpis, dates, st))
    w_cap = full_payload.get("weekly_sales_by_capacity_kva") or []
    m_cap = full_payload.get("mtd_orders_by_capacity_kva") or []
    tbl_week_cap = _weekly_sales_by_capacity_table(w_cap, st)
    tbl_mtd_cap = _mtd_orders_by_capacity_table(m_cap, st)
    if tbl_week_cap or tbl_mtd_cap:
        # Add breathing room so the rating block starts cleanly.
        story.append(Spacer(1, 0.16 * inch))
    if tbl_week_cap:
        story.append(_subtle_label_box(
            "Sales by transformer rating — current report period",
            st,
        ))
        story.append(Spacer(1, 0.05 * inch))
        story.append(tbl_week_cap)
        story.append(Spacer(1, 0.10 * inch))
    if tbl_mtd_cap:
        story.append(Paragraph(
            "<b>Order mix by rating</b> &mdash; report date range ordered quantity",
            st["body_sm"],
        ))
        story.append(Spacer(1, 0.04 * inch))
        story.append(tbl_mtd_cap)
    if tbl_week_cap or tbl_mtd_cap:
        story.append(Spacer(1, 0.1 * inch))

    si = full_payload.get("transformer_sales_insight_summary") or {}
    tc_tf = full_payload.get("top_customers_transformer_week") or []
    tm_tf = full_payload.get("top_materials_transformer_week") or []
    ch_tf_daily = charts.get("transformer_daily_sales")
    ch_cap_bar = charts.get("sales_capacity_week_bar")
    ch_cap_pie = charts.get("sales_capacity_mtd_pie")
    ch_region = charts.get("region_transformer_sales")
    has_sales_intel = bool(
        ch_tf_daily or ch_cap_bar or ch_cap_pie or ch_region or tc_tf or tm_tf or si.get("bullets")
    )
    story.extend(_section_header("Sales Intelligence — Transformer Portfolio", st))
    if has_sales_intel:
        if ch_tf_daily or ch_cap_bar:
            story.append(_side_by_side_charts(ch_tf_daily, ch_cap_bar))
            story.append(Spacer(1, 0.1 * inch))
        if ch_cap_pie or ch_region:
            story.append(_side_by_side_charts(ch_cap_pie, ch_region))
            story.append(Spacer(1, 0.1 * inch))
        t_tc = _transformer_top_customers_table(tc_tf, st)
        t_tm = _transformer_top_materials_table(tm_tf, st)
        if t_tc:
            story.append(PageBreak())
            story.append(Paragraph(
                "<b>Top customers</b> &mdash; report period",
                st["body_sm"],
            ))
            story.append(Spacer(1, 0.03 * inch))
            story.append(t_tc)
            story.append(Spacer(1, 0.08 * inch))
        if t_tm:
            story.append(Paragraph(
                "<b>Top material lines</b> &mdash; by net sales (week)",
                st["body_sm"],
            ))
            story.append(Spacer(1, 0.03 * inch))
            story.append(t_tm)
            story.append(Spacer(1, 0.08 * inch))
        for b in si.get("bullets") or []:
            story.append(Paragraph(f"• {_esc(b)}", st["body_sm"]))
    else:
        story.append(Paragraph(
            "Data currently unavailable for transformer sales insights in the selected report period.",
            st["body_sm"],
        ))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Spacer(1, 0.18 * inch))

    # Production charts should begin on a new page.
    story.append(PageBreak())
    # Production charts (side-by-side)
    story.extend(_section_header("Production Performance", st))
    story.append(
        _side_by_side_charts(
            charts.get("monthly_production"),
            charts.get("weekly_production"),
        )
    )
    story.append(Spacer(1, 0.15 * inch))

    # Customer ageing block (before vendor ageing)
    cust_tbl = _customer_ageing_table(customer_summary, st)
    if cust_tbl:
        story.extend(_section_header("Customer Ageing Buckets", st))
        if charts.get("customer_ageing"):
            story.append(_full_chart(charts["customer_ageing"], height=2.4))
            story.append(Spacer(1, 0.08 * inch))
        story.append(cust_tbl)
        story.append(Spacer(1, 0.12 * inch))

    # Vendor ageing chart + top vendors table
    vendor_chart = charts.get("vendor_ageing")
    vendor_top_tbl = _top_vendors_table(top_vendors, st)
    if vendor_chart or vendor_top_tbl:
        story.append(PageBreak())
        story.extend(_section_header("Vendor Ageing Buckets", st))
        if vendor_chart:
            story.append(_full_chart(vendor_chart, height=2.6))
            story.append(Spacer(1, 0.1 * inch))
        if vendor_top_tbl:
            story.append(Paragraph(
                "<b>Top 10 Vendors by Gross Outstanding</b> "
                "(INR, including overdue amount beyond 60 days and maximum days overdue)",
                st["body_sm"],
            ))
            story.append(Spacer(1, 0.05 * inch))
            story.append(vendor_top_tbl)
        story.append(Spacer(1, 0.12 * inch))

    # PO spend (always start on a fresh page for readability/consistency)
    story.append(PageBreak())
    po_header = f"PO Spend Detail — {_esc(label)}"
    story.extend(_section_header(po_header, st))
    if charts.get("po_spend"):
        story.append(_full_chart(charts["po_spend"], height=2.6))
        story.append(Spacer(1, 0.1 * inch))
    po_tbl = _po_table(po_spend, st)
    if po_tbl:
        story.append(po_tbl)
        story.append(Spacer(1, 0.12 * inch))

    po_detail_tbl = _po_line_items_table(po_lines, st)
    if po_detail_tbl:
        story.extend(_section_header("PO Spend Details — Top Line Items", st))
        story.append(po_detail_tbl)
        story.append(Spacer(1, 0.12 * inch))

    if events:
        story.append(PageBreak())
        story.extend(_section_header(
            "Global Events — Impact on Transformer Material Costs", st
        ))
        grid = _events_grid(events, st)
        if grid:
            story.append(grid)
        story.append(Spacer(1, 0.15 * inch))

    # Sentiment index
    if charts.get("sentiment_index"):
        story.extend(_section_header("Market Sentiment Index — Live Intelligence", st))
        story.append(_full_chart(charts["sentiment_index"], height=2.8))
        story.append(Spacer(1, 0.08 * inch))
        story.append(_market_sentiment_details_box(analysis, st))
        story.append(Spacer(1, 0.12 * inch))

    # 30-day price trend (always start on a new page)
    if charts.get("price_trend_30d"):
        story.append(PageBreak())
        story.extend(_section_header("30-Day Price Trend — Key Transformer Materials", st))
        story.append(_full_chart(charts["price_trend_30d"], height=2.6))
        story.append(Spacer(1, 0.08 * inch))
        source_tbl = _market_source_status_table(market_data, st)
        if source_tbl:
            story.append(source_tbl)
            story.append(Spacer(1, 0.08 * inch))
        variance_tbl = _market_price_variance_table(market_data, st)
        if variance_tbl:
            story.append(_subtle_label_box(
                "Price Position vs Market (Estimated Gain/Loss per unit)",
                st,
            ))
            story.append(Spacer(1, 0.04 * inch))
            story.append(variance_tbl)

    story.append(PageBreak())

    # ── PAGE 2 ────────────────────────────────────────────────────────────────
    # Recommendations section header (cards on next page if many)
    story.extend(_section_header(
        "AI Procurement Recommendations — Market + PO Signals", st
    ))

    recs = co.get("recommendations") or []
    if recs:
        rec_grid = _recommendations_grid(recs, st)
        if rec_grid:
            story.append(rec_grid)
            story.append(Spacer(1, 0.2 * inch))
    else:
        story.append(Paragraph("No recommendations generated.", st["body"]))
        story.append(Spacer(1, 0.1 * inch))

    story.append(PageBreak())

    # ── PAGE 3 — Critical alerts + commentary + outlook ──────────────────────
    alerts = co.get("critical_alerts") or []
    if alerts:
        story.extend(_section_header("Critical Alerts", st))
        for a in alerts:
            if isinstance(a, dict):
                sev   = _esc(str(a.get("severity") or "MEDIUM"))
                title = _esc(str(a.get("title") or ""))
                action= _esc(str(a.get("action") or ""))
                sev_color = {
                    "CRITICAL": "#dc2626", "HIGH": "#d97706", "MEDIUM": "#2563eb"
                }.get(sev, "#64748b")
                story.append(Paragraph(
                    f'<font color="{sev_color}"><b>[{sev}]</b></font> '
                    f'<b>{title}</b> — {action}',
                    st["body"]
                ))
            else:
                story.append(Paragraph(f"• {_esc(str(a))}", st["body"]))
        story.append(Spacer(1, 0.12 * inch))

    # Executive summary
    exec_sum = co.get("executive_summary") or []
    if exec_sum:
        story.extend(_section_header("Executive Summary", st))
        if isinstance(exec_sum, list):
            for bullet in exec_sum:
                story.append(Paragraph(f"• {_esc(str(bullet))}", st["body"]))
        else:
            story.append(Paragraph(_esc(str(exec_sum)), st["body"]))
        story.append(Spacer(1, 0.1 * inch))

    # Commentary blocks
    for heading, key in (
        ("KPI Commentary",         "kpi_commentary"),
        ("Production Commentary",  "production_commentary"),
        ("Customer Ageing Commentary", "customer_commentary"),
        ("Vendor Commentary",      "vendor_commentary"),
        ("Market Commentary",      "market_commentary"),
        ("Outlook",                "outlook"),
    ):
        text = co.get(key)
        if text:
            story.extend(_section_header(heading, st))
            story.append(Paragraph(_esc(str(text)), st["body"]))
            story.append(Spacer(1, 0.08 * inch))

    # Footer
    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=0.3, color=GREY_MID))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Data sources: LME, MCX India, Alpha Vantage, Trading Economics, IEEMA,  CEA "
        f"— as of {generated_str}. "
        "Recommendations are Agent SSai-generated and require procurement team review before action.",
        st["footer"]
    ))

    doc.build(story)
    log.info("PDF written: %s", pdf_path)
    return pdf_path
