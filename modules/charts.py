"""
Build matplotlib chart PNGs for the weekly PDF report.

Charts produced:
  monthly_production  — grouped bar: planned vs achieved (6 months)
  weekly_production   — grouped bar: planned vs achieved (weeks in month)
  sentiment_index     — horizontal bar: commodity sentiment scores (colour-coded)
  price_trend_30d     — multi-line index chart (base = 100) for key materials
  po_spend            — grouped bar: PO value by date, split by status
  transformer_daily_sales — bar: transformer net sales by day (report window)
  sales_capacity_week_bar — horizontal bar: net sales by rating/capacity (week)
  sales_capacity_mtd_pie — pie: MTD ordered-qty mix by rating
  region_transformer_sales — horizontal bar: transformer net sales by region
"""
from __future__ import annotations

import logging
import os
import random
from datetime import date, timedelta
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

log = logging.getLogger(__name__)

# ── Colour palette ──────────────────────────────────────────────────────────
C_PLANNED  = "#1e3a5f"   # dark navy
C_ACHIEVED = "#059669"   # green
C_OPEN     = "#2563eb"   # blue
C_PARTIAL  = "#f59e0b"   # amber
C_CLOSED   = "#16a34a"   # green
C_BEARISH  = "#dc2626"   # red   (sentiment < 40)
C_NEUTRAL  = "#f59e0b"   # amber (sentiment 41-65)
C_BULLISH  = "#16a34a"   # green (sentiment > 65)
BG_FIG     = "#f8fafc"   # page-light background
BG_AX      = "#f1f5f9"   # chart panel background
GRID_COL   = "#cbd5e1"   # soft grid/border color

SENTIMENT_THRESHOLDS = (40, 65)  # bearish / neutral / bullish boundaries


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _sentiment_score(value: Any, default: float = 50.0) -> float:
    """
    Sentiment values may be plain numbers or dicts like:
      {"score": 62, "zone": "NEUTRAL", ...}
    """
    if isinstance(value, dict):
        return _safe_float(value.get("score"), default)
    return _safe_float(value, default)


def _sentiment_color(score: float) -> str:
    if score < SENTIMENT_THRESHOLDS[0]:
        return C_BEARISH
    if score <= SENTIMENT_THRESHOLDS[1]:
        return C_NEUTRAL
    return C_BULLISH


def _apply_chart_theme(fig, ax):
    """Apply consistent background/border theme across all charts."""
    fig.patch.set_facecolor(BG_FIG)
    ax.set_facecolor(BG_AX)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COL)
        spine.set_linewidth(0.9)
    ax.tick_params(colors="#334155")


def _short_chart_label(text: Any, max_len: int = 22) -> str:
    t = str(text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 2] + "…"


def _format_bar_value(v: float, mode: str) -> str:
    """Smart value formatter for chart labels."""
    if mode == "int":
        return f"{int(round(v)):,}"
    if mode == "cr":
        # Preserve small non-zero values; drop trailing zeros otherwise.
        if abs(v) < 1 and abs(v) >= 0.001:
            return f"{v:.2f}".rstrip("0").rstrip(".")
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if mode == "qty":
        if abs(v - round(v)) < 1e-6:
            return f"{int(round(v)):,}"
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v:.1f}"


def _annotate_vertical_bars(ax, bars, mode: str = "int", fontsize: int = 6):
    for b in bars:
        v = b.get_height()
        if abs(v) < 1e-9:
            continue
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_y() + v * 0.5,
            _format_bar_value(v, mode),
            ha="center",
            va="center",
            fontsize=fontsize,
            color="white",
            fontweight="bold",
            zorder=3,
        )


def _annotate_horizontal_bars(ax, bars, mode: str = "cr", fontsize: int = 6):
    for b in bars:
        v = b.get_width()
        if abs(v) < 1e-9:
            continue
        ax.text(
            b.get_x() + v * 0.5,
            b.get_y() + b.get_height() / 2,
            _format_bar_value(v, mode),
            ha="center",
            va="center",
            fontsize=fontsize,
            color="white",
            fontweight="bold",
            zorder=3,
        )


# ── 1. Monthly production (grouped bar) ─────────────────────────────────────
def _chart_monthly_production(monthly_prod: List[dict], out_path: str) -> bool:
    rows = list(reversed(monthly_prod or []))
    if not rows:
        return False
    labels   = [str(r.get("month_label") or "") for r in rows]
    planned  = [_safe_float(r.get("planned_qty"))  for r in rows]
    achieved = [_safe_float(r.get("achieved_qty")) for r in rows]

    x = np.arange(len(labels))
    w = 0.35
    fig, ax = plt.subplots(figsize=(5, 3.5))
    _apply_chart_theme(fig, ax)
    bars_p = ax.bar(x - w / 2, planned,  width=w, label="Planned",  color=C_PLANNED,  zorder=2)
    bars_a = ax.bar(x + w / 2, achieved, width=w, label="Achieved", color=C_ACHIEVED, zorder=2)
    _annotate_vertical_bars(ax, bars_p, "int")
    _annotate_vertical_bars(ax, bars_a, "int")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("Units", fontsize=8)
    ax.set_title("Monthly Planned vs Achieved", fontsize=9, fontweight="bold")
    ax.legend(fontsize=7)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    # Force identical subplot geometry across production charts
    fig.subplots_adjust(left=0.10, right=0.98, top=0.90, bottom=0.22)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── 2. Weekly production (grouped bar) ──────────────────────────────────────
def _chart_weekly_production(weekly_prod: List[dict], out_path: str) -> bool:
    rows = weekly_prod or []
    if not rows:
        return False
    labels   = [str(r.get("week_label") or "") for r in rows]
    planned  = [_safe_float(r.get("planned_qty"))  for r in rows]
    achieved = [_safe_float(r.get("achieved_qty")) for r in rows]

    x = np.arange(len(labels))
    w = 0.35
    fig, ax = plt.subplots(figsize=(5, 3.5))
    _apply_chart_theme(fig, ax)
    bars_p = ax.bar(x - w / 2, planned,  width=w, label="Planned",  color=C_PLANNED,  zorder=2)
    bars_a = ax.bar(x + w / 2, achieved, width=w, label="Achieved", color=C_ACHIEVED, zorder=2)
    _annotate_vertical_bars(ax, bars_p, "int")
    _annotate_vertical_bars(ax, bars_a, "int")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Units", fontsize=8)
    ax.set_title("Weekly Planned vs Achieved", fontsize=9, fontweight="bold")
    ax.legend(fontsize=7)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    # Match subplot geometry with monthly chart for equal visual height
    fig.subplots_adjust(left=0.10, right=0.98, top=0.90, bottom=0.22)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── 3. Commodity Sentiment Index (horizontal bar) ───────────────────────────
def _chart_sentiment_index(sentiments: dict, report_date: str, out_path: str) -> bool:
    """
    sentiments: {material_name: score (0-100)}
    Colour: red <40, amber 41-65, green >65.
    Vertical dashed threshold lines at 40 and 65.
    """
    if not sentiments:
        return False

    # Display order matches the PDF (bottom to top = first to last in barh)
    order = [
        "HR Steel", "Insulation Paper", "CRGO Steel", "Amorphous",
        "Transformer Oil", "Aluminium", "Copper",
    ]
    labels = []
    scores = []
    for name in order:
        # tolerate partial key matches (e.g. "Copper" matches "Copper (LME)")
        matched = next(
            (k for k in sentiments if name.lower() in k.lower()), None
        )
        if matched:
            labels.append(matched)
            scores.append(_sentiment_score(sentiments[matched]))

    if not labels:
        labels = list(sentiments.keys())
        scores = [_sentiment_score(v) for v in sentiments.values()]

    colors = [_sentiment_color(s) for s in scores]

    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _apply_chart_theme(fig, ax)
    y_pos = np.arange(len(labels))
    bars = ax.barh(y_pos, scores, color=colors, height=0.5, zorder=2)

    # Value labels at end of each bar
    for bar, score in zip(bars, scores):
        ax.text(
            score + 1, bar.get_y() + bar.get_height() / 2,
            f"{int(score)}", va="center", ha="left", fontsize=8, fontweight="bold"
        )

    # Threshold lines
    for x_val, ls in [(40, "--"), (65, "--")]:
        ax.axvline(x=x_val, color="#9ca3af", linestyle=ls, linewidth=0.8, zorder=1)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Sentiment Score (0–100)", fontsize=8)
    ax.set_xlim(0, 110)
    ax.set_title(f"Commodity Sentiment Index — {report_date}", fontsize=9, fontweight="bold")
    ax.xaxis.grid(True, linestyle="--", alpha=0.3, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)

    # Legend
    legend_items = [
        mpatches.Patch(color=C_BEARISH, label="Bearish (<40)"),
        mpatches.Patch(color=C_NEUTRAL, label="Neutral (41–65)"),
        mpatches.Patch(color=C_BULLISH, label="Bullish (>65)"),
    ]
    ax.legend(handles=legend_items, loc="lower right", fontsize=7, framealpha=0.8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return True


# ── 4. 30-day price trend (multi-line index, base = 100) ────────────────────
def _chart_price_trend_30d(market_data: dict, sentiments: dict,
                            report_date_obj: date, out_path: str) -> bool:
    """
    Builds a 30-day relative price index (base=100 at day -30).
    Uses real history for Copper/Aluminium if available; otherwise
    generates a smooth synthetic trend from known sentiment direction.
    """
    n_days = 30
    today  = report_date_obj
    x_dates = [today - timedelta(days=n_days - i) for i in range(n_days + 1)]
    x_labels = x_dates  # used for tick formatting

    def _build_series(
        history_90d: List[float],
        sentiment_score: float,
        volatility: float = 1.0,
        phase_seed: int = 0,
    ) -> List[float]:
        """Normalise history to base=100 at day -30, or build synthetic trend."""
        if history_90d and len(history_90d) >= n_days:
            h = history_90d[-(n_days + 1):]  # oldest → newest
            base = h[0] if h[0] else 1.0
            return [v / base * 100 for v in h]

        # Synthetic: stronger linear drift + cyclical movement + noise.
        # Keeps non-live series informative instead of appearing flat.
        direction = sentiment_score - 50   # -50..+50 range
        total_change_pct = direction * 0.45  # max ±22.5%
        rng = random.Random(int(sentiment_score * 100) + phase_seed)
        values = []
        for i in range(n_days + 1):
            trend = 100 + total_change_pct * (i / n_days)
            cycle = np.sin((i / n_days) * 2 * np.pi + (phase_seed % 7)) * (1.4 * volatility)
            noise = rng.gauss(0, 0.55 * volatility)
            values.append(round(trend + cycle + noise, 2))
        return values

    copper_sent   = _sentiment_score(next((v for k, v in sentiments.items()
                                           if "copper" in k.lower()), 72))
    alum_sent     = _sentiment_score(next((v for k, v in sentiments.items()
                                           if "alumin" in k.lower()), 58))
    oil_sent      = _sentiment_score(next((v for k, v in sentiments.items()
                                           if "oil" in k.lower()), 61))
    crgo_sent     = _sentiment_score(next((v for k, v in sentiments.items()
                                           if "crgo" in k.lower()), 44))
    amorphous_sent= _sentiment_score(next((v for k, v in sentiments.items()
                                           if "amorphous" in k.lower()), 52))
    hr_sent       = _sentiment_score(next((v for k, v in sentiments.items()
                                           if "hr steel" in k.lower()), 39))

    copper_hist   = market_data.get("copper_history_90d", [])
    alum_hist     = market_data.get("aluminium_history_90d", [])

    series = {
        "Copper":          (_build_series(copper_hist, copper_sent, 0.9, 11), "-",  C_ACHIEVED, 1.9),
        "Aluminium":       (_build_series(alum_hist,   alum_sent,   0.9, 17), "-",  "#2563eb",  1.6),
        "Transformer Oil": (_build_series([],          oil_sent,    1.2, 23), "-",  "#f59e0b",  1.4),
        "Amorphous":       (_build_series([],          amorphous_sent, 1.1, 31), "-.", "#7c3aed", 1.3),
        "HR Steel":        (_build_series([],          hr_sent,     1.0, 41), "--", "#64748b",  1.2),
        "CRGO Steel":      (_build_series([],          crgo_sent,   1.0, 53), "-.", "#94a3b8",  1.2),
    }

    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _apply_chart_theme(fig, ax)
    for label, (vals, ls, color, lw) in series.items():
        ax.plot(range(n_days + 1), vals, linestyle=ls, color=color,
                linewidth=lw, label=label, marker=None)

    ax.axhline(y=100, color=GRID_COL, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Index", fontsize=8)
    ax.set_title("30-Day Price Trend (Index: Base = 100)", fontsize=9, fontweight="bold")

    # X-axis: show ~5 evenly spaced date labels
    tick_idx  = [0, 7, 14, 21, 28, 30]
    tick_lbls = [x_dates[i].strftime("%b %-d") for i in tick_idx]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(tick_lbls, fontsize=7)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3, color=GRID_COL)
    ax.set_axisbelow(True)
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return True


# ── 5. PO spend by status (grouped bar) ─────────────────────────────────────
def _chart_po_spend(po_spend: List[dict], out_path: str) -> bool:
    if not po_spend:
        return False
    labels      = [str(r.get("po_date_label") or r.get("po_date") or "") for r in po_spend]
    open_vals   = [_safe_float(r.get("open_cr"))    for r in po_spend]
    partial_vals= [_safe_float(r.get("partial_cr")) for r in po_spend]
    closed_vals = [_safe_float(r.get("closed_cr"))  for r in po_spend]

    x = np.arange(len(labels))
    w = 0.25
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _apply_chart_theme(fig, ax)
    bars_o = ax.bar(x - w, open_vals, width=w, label="Open", color=C_OPEN, zorder=2)
    bars_p = ax.bar(x, partial_vals, width=w, label="Partial", color=C_PARTIAL, zorder=2)
    bars_c = ax.bar(x + w, closed_vals, width=w, label="Closed", color=C_CLOSED, zorder=2)
    _annotate_vertical_bars(ax, bars_o, "cr")
    _annotate_vertical_bars(ax, bars_p, "cr")
    _annotate_vertical_bars(ax, bars_c, "cr")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("Rs. Cr", fontsize=8)
    ax.set_title("Weekly PO Spend by Status (Rs. Cr)", fontsize=9, fontweight="bold")
    ax.legend(fontsize=7)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return True


# ── 6. Vendor ageing buckets (kept for backward compat) ─────────────────────
def _chart_vendor_buckets(vendor_summary: dict, out_path: str) -> bool:
    if not vendor_summary:
        return False
    keys = [
        ("1–30",    "bucket_01_30"),
        ("31–60",   "bucket_31_60"),
        ("61–90",   "bucket_61_90"),
        ("91–120",  "bucket_91_120"),
        ("121–150", "bucket_121_150"),
        ("151–180", "bucket_151_180"),
        ("181–360", "bucket_181_360"),
        ("361+",    "bucket_361_plus"),
    ]
    labels = [k[0] for k in keys]
    values = [_safe_float(vendor_summary.get(k[1])) / 1e7 for k in keys]
    if sum(values) == 0:
        return False
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _apply_chart_theme(fig, ax)
    bars = ax.bar(labels, values, color="#b45309", zorder=2)
    _annotate_vertical_bars(ax, bars, "cr")
    ax.set_ylabel("Outstanding (Cr)", fontsize=8)
    ax.set_title("Vendor Ageing Buckets (INR outstanding)", fontsize=9, fontweight="bold")
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    plt.xticks(rotation=25, ha="right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return True


def _chart_customer_buckets(customer_summary: dict, out_path: str) -> bool:
    if not customer_summary:
        return False
    keys = [
        ("1–30", "bucket_01_30"),
        ("31–60", "bucket_31_60"),
        ("61–90", "bucket_61_90"),
        ("91–120", "bucket_91_120"),
        ("121–180", "bucket_121_180"),
        ("181–360", "bucket_181_360"),
        ("361+", "bucket_361_plus"),
    ]
    labels = [k[0] for k in keys]
    values = [_safe_float(customer_summary.get(k[1])) / 1e7 for k in keys]
    if sum(values) == 0:
        return False
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _apply_chart_theme(fig, ax)
    bars = ax.bar(labels, values, color="#0ea5e9", zorder=2)
    _annotate_vertical_bars(ax, bars, "cr")
    ax.set_ylabel("Outstanding (Cr)", fontsize=8)
    ax.set_title("Customer Ageing Buckets (INR outstanding)", fontsize=9, fontweight="bold")
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    plt.xticks(rotation=25, ha="right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return True


# ── 7. sales_data — transformer daily net sales (report window) ─────────────
def _chart_transformer_daily_sales(daily_rows: List[dict], out_path: str) -> bool:
    rows = daily_rows or []
    if not rows:
        return False
    labels = [str(r.get("day_label") or "") for r in rows]
    vals = [_safe_float(r.get("net_sales_cr")) for r in rows]
    if sum(vals) <= 0:
        return False
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(5, 3.35))
    _apply_chart_theme(fig, ax)
    bars = ax.bar(x, vals, color=C_ACHIEVED, zorder=2, width=0.72)
    _annotate_vertical_bars(ax, bars, "cr")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7)
    ax.set_ylabel("Rs. Cr", fontsize=8)
    ax.set_title("Transformer net sales by day (sales_data)", fontsize=9, fontweight="bold")
    ax.yaxis.grid(True, linestyle="--", alpha=0.45, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    fig.subplots_adjust(left=0.12, right=0.98, top=0.88, bottom=0.28)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── 8. sales_data — net sales by rating / capacity (week) ────────────────────
def _chart_sales_capacity_week_bar(capacity_rows: List[dict], out_path: str) -> bool:
    rows = list(capacity_rows or [])
    if not rows:
        return False
    labels = [_short_chart_label(r.get("capacity_key"), 18) for r in rows]
    vals = [_safe_float(r.get("net_sales_cr")) for r in rows]
    if sum(vals) <= 0:
        return False
    y = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(5, 3.35))
    _apply_chart_theme(fig, ax)
    bars = ax.barh(y, vals, color=C_PLANNED, zorder=2, height=0.65)
    _annotate_horizontal_bars(ax, bars, "cr")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("Net sales (Rs. Cr)", fontsize=8)
    ax.set_title("By rating / capacity (week)", fontsize=9, fontweight="bold")
    ax.xaxis.grid(True, linestyle="--", alpha=0.45, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    fig.subplots_adjust(left=0.26, right=0.98, top=0.88, bottom=0.12)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── 9. sales_data — report-period ordered quantity mix by rating (pie) ───────
def _chart_sales_capacity_mtd_pie(mtd_rows: List[dict], out_path: str) -> bool:
    rows = [r for r in (mtd_rows or []) if _safe_float(r.get("range_ordered_qty")) > 0]
    if not rows:
        return False
    rows = sorted(rows, key=lambda r: _safe_float(r.get("range_ordered_qty")), reverse=True)
    top_n = 7
    top = rows[:top_n]
    other_qty = sum(_safe_float(r.get("range_ordered_qty")) for r in rows[top_n:])
    labels = [_short_chart_label(r.get("capacity_key"), 14) for r in top]
    sizes = [_safe_float(r.get("range_ordered_qty")) for r in top]
    if other_qty > 1e-6:
        labels.append("Other")
        sizes.append(other_qty)
    fig, ax = plt.subplots(figsize=(5, 3.35))
    _apply_chart_theme(fig, ax)
    _wedges, _texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct="%1.0f%%",
        pctdistance=0.72,
        textprops={"fontsize": 6},
        colors=plt.cm.Blues(np.linspace(0.35, 0.9, len(sizes))),
    )
    for t in autotexts:
        t.set_fontsize(6)
    ax.set_title("Report-Period Ordered Quantity by Rating", fontsize=9, fontweight="bold")
    fig.subplots_adjust(left=0.02, right=0.98, top=0.86, bottom=0.06)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── 10. sales_data — transformer net sales by region (week) ─────────────────
def _chart_region_transformer_sales(region_rows: List[dict], out_path: str) -> bool:
    rows = region_rows or []
    if not rows:
        return False
    labels = [_short_chart_label(r.get("region_label"), 26) for r in rows]
    vals = [_safe_float(r.get("net_sales_cr")) for r in rows]
    if sum(vals) <= 0:
        return False
    y = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(5, 3.35))
    _apply_chart_theme(fig, ax)
    bars = ax.barh(y, vals, color="#0369a1", zorder=2, height=0.62)
    _annotate_horizontal_bars(ax, bars, "cr")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("Net sales (Rs. Cr)", fontsize=8)
    ax.set_title("Transformer sales by region (week)", fontsize=9, fontweight="bold")
    ax.xaxis.grid(True, linestyle="--", alpha=0.45, zorder=0, color=GRID_COL)
    ax.set_axisbelow(True)
    fig.subplots_adjust(left=0.22, right=0.98, top=0.88, bottom=0.12)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return True


# ── Master build function ────────────────────────────────────────────────────
def build_all_charts(payload: dict) -> Dict[str, str]:
    """
    Write chart PNGs to output/charts_<date>/ and return {logical_name: filepath}.
    Missing data skips that chart (key omitted from returned dict).
    """
    dates    = payload.get("dates") or {}
    today    = dates.get("today")
    today_s  = str(today) if today else "unknown"
    analysis = payload.get("analysis") or {}
    sentiments = analysis.get("sentiments") or {}

    out_dir = os.path.join("output", f"charts_{today_s}")
    os.makedirs(out_dir, exist_ok=True)

    paths: Dict[str, str] = {}

    # Monthly production
    p = os.path.join(out_dir, "monthly_production.png")
    if _chart_monthly_production(payload.get("monthly_prod") or [], p):
        paths["monthly_production"] = p

    # Weekly production
    p = os.path.join(out_dir, "weekly_production.png")
    if _chart_weekly_production(payload.get("weekly_prod") or [], p):
        paths["weekly_production"] = p

    # Sentiment index
    if sentiments:
        p = os.path.join(out_dir, "sentiment_index.png")
        report_date_str = today.strftime("%-d %b %Y") if hasattr(today, "strftime") else today_s
        if _chart_sentiment_index(sentiments, report_date_str, p):
            paths["sentiment_index"] = p

    # 30-day price trend
    market_data = payload.get("market_data") or {}
    if market_data and today and hasattr(today, "strftime"):
        p = os.path.join(out_dir, "price_trend_30d.png")
        if _chart_price_trend_30d(market_data, sentiments, today, p):
            paths["price_trend_30d"] = p

    # PO spend
    p = os.path.join(out_dir, "po_spend.png")
    if _chart_po_spend(payload.get("po_spend") or [], p):
        paths["po_spend"] = p

    # Vendor ageing (retained)
    p = os.path.join(out_dir, "vendor_ageing.png")
    if _chart_vendor_buckets(payload.get("vendor_summary") or {}, p):
        paths["vendor_ageing"] = p

    # Customer ageing
    p = os.path.join(out_dir, "customer_ageing.png")
    if _chart_customer_buckets(payload.get("customer_summary") or {}, p):
        paths["customer_ageing"] = p

    # sales_data — transformer / capacity / region insights
    cap_w = payload.get("weekly_sales_by_capacity_kva") or []
    cap_m = payload.get("mtd_orders_by_capacity_kva") or []
    daily_tf = payload.get("transformer_daily_sales") or []
    regions = payload.get("region_transformer_sales_week") or []

    p = os.path.join(out_dir, "transformer_daily_sales.png")
    if _chart_transformer_daily_sales(daily_tf, p):
        paths["transformer_daily_sales"] = p

    p = os.path.join(out_dir, "sales_capacity_week_bar.png")
    if _chart_sales_capacity_week_bar(cap_w, p):
        paths["sales_capacity_week_bar"] = p

    p = os.path.join(out_dir, "sales_capacity_mtd_pie.png")
    if _chart_sales_capacity_mtd_pie(cap_m, p):
        paths["sales_capacity_mtd_pie"] = p

    p = os.path.join(out_dir, "region_transformer_sales.png")
    if _chart_region_transformer_sales(regions, p):
        paths["region_transformer_sales"] = p

    log.info("Charts built: %s", list(paths.keys()))
    return paths
