import os
import time
import logging
from collections import defaultdict
import re
import requests
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from modules.db_connector import DBConnector

load_dotenv()
log = logging.getLogger(__name__)

# ── API Keys ──────────────────────────────────────────────────────────────────
ALPHA_VANTAGE_KEY    = os.getenv("ALPHA_VANTAGE_KEY")
# Keep backward compatibility: if FMP_API_KEY is not set, reuse old var.
FMP_API_KEY          = os.getenv("FMP_API_KEY") or os.getenv("TRADING_ECONOMICS_KEY")
MCX_API_KEY          = os.getenv("MCX_API_KEY")
NEWSAPI_KEY          = os.getenv("NEWSAPI_KEY")
PLATTS_KEY           = os.getenv("PLATTS_API_KEY")

TIMEOUT = 12  # seconds per API call
MARKET_BENCHMARK_AVG_MONTHS = max(1, int((os.getenv("MARKET_BENCHMARK_AVG_MONTHS") or "3").strip()))

INTERNAL_HISTORICAL_SOURCE = "Internal Historical Data"
INTERNAL_HISTORICAL_STATUS = "internal_historical"

# Weighted average PO price (INR/kg) from po_data for classified materials.
# INTERNAL_PO_PRICE_LOOKBACK_DAYS: default 365; set 0 to disable date filter (all rows).
PO_INTERNAL_MATERIALS_SQL = """
SELECT
    po_number,
    po_item,
    item_description,
    vendor_name,
    po_creation_date,
    ordered_qty,
    uom,
    ROUND(po_value_inr * 10000000, 2)::DECIMAL(15,2) AS po_value_rs,
    CASE
        WHEN LOWER(item_description) ~* 'aluminium.*(ingot|sheet|flat|rod|wire|alloy|foil)'
            THEN 'Aluminium'
        WHEN LOWER(item_description) ~* '(copper.*(rod|flat|plate|foil|pipe|tube|earth|bus|braided|flexible|cable)|ec grade)'
            THEN 'Copper'
        WHEN LOWER(item_description) ~* '(amorphous|amt core slit)'
            THEN 'Amorphous'
        WHEN LOWER(item_description) ~* '(hr ?sheet|hrsht|hr sht)'
            THEN 'HR Steel'
        WHEN LOWER(item_description) ~* 'crgo'
            THEN 'CRGO'
    END AS material,
    ROUND(
        (po_value_inr * 10000000) / NULLIF(ordered_qty, 0)
    , 2)::DECIMAL(15,2) AS price_per_kg
FROM po_data
WHERE sign_flag = '1'
  AND LOWER(uom) = 'kg'
    AND po_number NOT IN ('4200000928')
      AND LOWER(item_description) NOT LIKE '%silver%'


  AND (
        LOWER(item_description) ~* 'aluminium.*(ingot|sheet|flat|rod|wire|alloy|foil)'
     OR LOWER(item_description) ~* '(copper.*(rod|flat|plate|foil|pipe|tube|earth|bus|braided|flexible|cable)|ec grade)'
     OR LOWER(item_description) ~* '(amorphous|amt core slit)'
     OR LOWER(item_description) ~* '(hr ?sheet|hrsht|hr sht)'
     OR LOWER(item_description) ~* 'crgo'
  )
"""

# Drop PO lines whose implied INR/kg is outside a plausible band (outliers / bad qty).
# Defaults are wide; tighten with env vars per material if needed.
def _internal_po_inr_kg_bounds(material: str) -> tuple[float, float] | None:
    """
    Return (lo, hi) INR/kg for weighted-average filtering, or None = no band for this material.
    """
    specs: dict[str, tuple[str, str, float, float]] = {
        "Copper": (
            "INTERNAL_PO_COPPER_KG_MIN",
            "INTERNAL_PO_COPPER_KG_MAX",
            300.0,
            2000.0,
        ),
        "Aluminium": (
            "INTERNAL_PO_ALUMINIUM_KG_MIN",
            "INTERNAL_PO_ALUMINIUM_KG_MAX",
            100.0,
            1000.0,
        ),
        "Amorphous": (
            "INTERNAL_PO_AMORPHOUS_KG_MIN",
            "INTERNAL_PO_AMORPHOUS_KG_MAX",
            50.0,
            500.0,
        ),
        "CRGO": (
            "INTERNAL_PO_CRGO_KG_MIN",
            "INTERNAL_PO_CRGO_KG_MAX",
            80.0,
            900.0,
        ),
        "HR Steel": (
            "INTERNAL_PO_HR_STEEL_KG_MIN",
            "INTERNAL_PO_HR_STEEL_KG_MAX",
            15.0,
            400.0,
        ),
    }
    if material not in specs:
        return None
    min_key, max_key, d_lo, d_hi = specs[material]
    try:
        lo = float((os.getenv(min_key) or str(d_lo)).strip())
        hi = float((os.getenv(max_key) or str(d_hi)).strip())
    except ValueError:
        lo, hi = d_lo, d_hi
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def _internal_po_price_per_kg_in_band(material: str, price_per_kg: float) -> bool:
    b = _internal_po_inr_kg_bounds(material)
    if b is None:
        return True
    lo, hi = b
    return lo <= price_per_kg <= hi


# ── Fallback prices (used if API fails) ──────────────────────────────────────
FALLBACK_PRICES = {
    "copper_lme_usd_mt":     13149,
    "aluminium_lme_usd_mt":  3500,
    "crgo_steel_inr_mt":     178000,
    "amorphous_core_inr_mt": 250000,
    "transformer_oil_inr_kl":108000,
    "hr_steel_inr_mt":       56000,
    "insulation_paper_inr_mt":96000,
    "usd_inr":               92.50,
}


def _transformer_oil_inr_per_litre_bounds() -> tuple[float, float]:
    try:
        lo = float((os.getenv("INTERNAL_PO_OIL_L_MIN") or "60").strip())
        hi = float((os.getenv("INTERNAL_PO_OIL_L_MAX") or "200").strip())
    except ValueError:
        lo, hi = 60.0, 200.0
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _transformer_oil_po_rows(days: int) -> list[dict]:
    """PO lines for bulk transformer oil (L / barrel-NOS), excluding lab/sampling SKUs."""
    sql = """
        SELECT
            item_description,
            vendor_name,
            po_creation_date,
            ordered_qty,
            uom,
            po_value,
            CASE
                WHEN uom = 'L' THEN ordered_qty
                WHEN uom = 'NOS' THEN ordered_qty * 210
                ELSE NULL
            END AS qty_in_litres,
            CAST(
                ROUND(
                    CASE
                        WHEN uom = 'L' THEN po_value / NULLIF(ordered_qty, 0)
                        WHEN uom = 'NOS' THEN po_value / NULLIF(ordered_qty * 210, 0)
                        ELSE NULL
                    END
                , 2)
            AS DECIMAL(12,2)) AS price_per_litre
        FROM po_data
        WHERE LOWER(item_description) LIKE '%%transformer oil%%'
          AND LOWER(item_description) NOT LIKE '%%sampling%%'
          AND LOWER(item_description) NOT LIKE '%%dga%%'
          AND sign_flag = '1'
          AND uom IN ('L', 'NOS')
          AND po_creation_date >= CURRENT_DATE - (%(days)s::integer * INTERVAL '1 day')
        ORDER BY po_creation_date DESC
    """
    return DBConnector.execute_query(sql, {"days": days})


def _weighted_transformer_oil_inr_kl(rows: list[dict]) -> float | None:
    """Quantity-weighted INR/KL from PO rows; drops out-of-band implied INR/L."""
    lo, hi = _transformer_oil_inr_per_litre_bounds()
    total_rs = 0.0
    total_l = 0.0
    for r in rows:
        ppl = r.get("price_per_litre")
        litres = r.get("qty_in_litres")
        if ppl is None or litres is None:
            continue
        ppl_f = float(ppl)
        litres_f = float(litres)
        if litres_f <= 0 or not (lo <= ppl_f <= hi):
            continue
        total_rs += float(r.get("po_value") or 0)
        total_l += litres_f
    if total_l <= 0:
        return None
    return round((total_rs / total_l) * 1000, 0)


def _fetch_transformer_oil_from_db(days: int = 30):
    """
    Internal transformer-oil reference from PO data (INR/KL).
    Uses quantity-weighted average over bulk oil lines (L / barrel-NOS).
    """
    rows = _transformer_oil_po_rows(days)
    avg_inr_kl = _weighted_transformer_oil_inr_kl(rows)
    if avg_inr_kl is None:
        raise RuntimeError(f"No valid transformer oil PO prices in last {days} days")
    return {
        "price_inr_kl": avg_inr_kl,
        "source": INTERNAL_HISTORICAL_SOURCE,
        "status": INTERNAL_HISTORICAL_STATUS,
        "db_rows_30d": rows,
    }


def _fetch_transformer_oil_po_avg_inr_kl(days: int | None = None):
    """Weighted INR/KL from transformer-oil PO lines, or None."""
    if days is None:
        lookback_raw = (os.getenv("INTERNAL_PO_PRICE_LOOKBACK_DAYS") or "365").strip()
        try:
            days = int(lookback_raw)
        except ValueError:
            days = 365
    try:
        return float(_fetch_transformer_oil_from_db(days=days)["price_inr_kl"])
    except Exception:
        return None


def _aggregate_internal_po_metrics(rows: list[dict]) -> dict:
    """Weighted average INR/kg by material from classified KG PO lines."""
    rs_sum = defaultdict(float)
    qty_sum = defaultdict(float)
    line_count = defaultdict(int)
    rs_sum_f = defaultdict(float)
    qty_sum_f = defaultdict(float)
    line_count_f = defaultdict(int)

    for r in rows:
        mat = r.get("material")
        if not mat:
            continue
        rs = float(r.get("po_value_rs") or 0)
        q = float(r.get("ordered_qty") or 0)
        if q <= 0:
            continue
        pkg = float(r.get("price_per_kg") or 0) if r.get("price_per_kg") is not None else (rs / q)
        rs_sum[mat] += rs
        qty_sum[mat] += q
        line_count[mat] += 1
        if _internal_po_price_per_kg_in_band(mat, pkg):
            rs_sum_f[mat] += rs
            qty_sum_f[mat] += q
            line_count_f[mat] += 1

    out = {}
    for mat, total_rs in rs_sum.items():
        q = qty_sum[mat]
        if q <= 0:
            continue
        qf = qty_sum_f[mat]
        total_rs_f = rs_sum_f[mat]
        bounds = _internal_po_inr_kg_bounds(mat)
        if bounds and qf > 0 and line_count_f[mat] < line_count[mat]:
            lo, hi = bounds
            log.info(
                "Internal PO %s: excluded %s/%s lines outside INR/kg band [%.0f, %.0f]",
                mat,
                line_count[mat] - line_count_f[mat],
                line_count[mat],
                lo,
                hi,
            )
        if qf > 0:
            avg_kg = total_rs_f / qf
            used_rs, used_q, used_lines = total_rs_f, qf, line_count_f[mat]
        else:
            if bounds and line_count[mat]:
                lo, hi = bounds
                log.warning(
                    "Internal PO %s: all %s lines outside INR/kg band [%.0f, %.0f]; "
                    "using unfiltered weighted average (widen env bounds if needed)",
                    mat,
                    line_count[mat],
                    lo,
                    hi,
                )
            avg_kg = total_rs / q
            used_rs, used_q, used_lines = total_rs, q, line_count[mat]

        out[mat] = {
            "avg_price_per_kg_inr": avg_kg,
            "total_po_value_rs": used_rs,
            "total_ordered_kg": used_q,
            "line_count": used_lines,
        }
    return out


def _internal_po_lookback_days(env_key: str, default: int) -> int:
    raw = (os.getenv(env_key) or str(default)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _fetch_internal_po_material_rows(fetch_days: int) -> list[dict]:
    date_clause = ""
    if fetch_days > 0:
        date_clause = (
            f"\n  AND po_creation_date >= CURRENT_DATE - ({fetch_days} * INTERVAL '1 day')"
        )
    sql = PO_INTERNAL_MATERIALS_SQL + date_clause + "\nORDER BY po_creation_date DESC"
    return DBConnector.execute_query(sql)


def _rows_since(rows: list[dict], min_date: date | None) -> list[dict]:
    if min_date is None:
        return rows
    out = []
    for r in rows:
        pod = r.get("po_creation_date")
        if pod is None:
            continue
        if isinstance(pod, datetime):
            pod = pod.date()
        if pod >= min_date:
            out.append(r)
    return out


def _fetch_internal_po_windowed_metrics() -> tuple[dict, dict]:
    """
    Long and recent weighted PO averages from the same po_data extract.
    long  — INTERNAL_PO_PRICE_LOOKBACK_DAYS (default 365)
    recent — INTERNAL_PO_RECENT_LOOKBACK_DAYS (default 90)
    """
    long_days = _internal_po_lookback_days("INTERNAL_PO_PRICE_LOOKBACK_DAYS", 365)
    recent_days = _internal_po_lookback_days("INTERNAL_PO_RECENT_LOOKBACK_DAYS", 90)
    fetch_days = max(long_days, recent_days, 1)
    rows = _fetch_internal_po_material_rows(fetch_days)
    today = date.today()
    long_cut = today - timedelta(days=long_days) if long_days > 0 else None
    recent_cut = today - timedelta(days=recent_days) if recent_days > 0 else None
    return (
        _aggregate_internal_po_metrics(_rows_since(rows, long_cut)),
        _aggregate_internal_po_metrics(_rows_since(rows, recent_cut)),
    )


def _fetch_internal_po_material_metrics():
    """Weighted average INR/kg (long lookback only)."""
    long_days = _internal_po_lookback_days("INTERNAL_PO_PRICE_LOOKBACK_DAYS", 365)
    rows = _fetch_internal_po_material_rows(long_days if long_days > 0 else 365)
    if long_days > 0:
        rows = _rows_since(rows, date.today() - timedelta(days=long_days))
    return _aggregate_internal_po_metrics(rows)


def fetch_tradingeconomics_spot_price(commodity_slug: str):
    """
    Scrape spot price from TradingEconomics commodity page.
    Supported slugs: copper, aluminum.
    Returns normalized USD/MT for compatibility with current pipeline.
    """
    try:
        url = f"https://tradingeconomics.com/commodity/{commodity_slug}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.text

        # Example phrases in page body:
        # "Copper fell to 6.01 USD/Lbs ..."
        # "Aluminum fell to 3,529.15 USD/T ..."
        match = re.search(
            r"\b(?:Copper|Aluminum)\b.*?\bto\s+([0-9,]+(?:\.[0-9]+)?)\s+USD\/([A-Za-z]+)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None

        raw_price = float(match.group(1).replace(",", ""))
        unit = match.group(2).upper()

        if commodity_slug == "copper" and unit.startswith("LB"):
            # USD/Lbs -> USD/MT
            price_usd_mt = raw_price * 2204.62262
        else:
            # Aluminum page commonly uses USD/T (tonne), already USD/MT scale.
            price_usd_mt = raw_price

        return {
            "price_usd_mt": round(price_usd_mt, 4),
            "source": "TradingEconomics (page scrape)",
            "status": "live",
        }
    except Exception as e:
        log.warning(f"TradingEconomics page scrape failed ({commodity_slug}): {e}")
        return None


def fetch_tradingeconomics_steel_inr_mt():
    """
    Scrape steel commodity spot value from TradingEconomics.
    Uses direct numeric extraction and assumes site unit scale maps to INR/MT
    for current report compatibility.
    """
    try:
        url = "https://tradingeconomics.com/commodity/steel"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.text

        # Pattern examples:
        # "Steel decreased ... to 3162 CNY/T"
        # "Steel ... to 56000 INR/MT"
        match = re.search(
            r"\bSteel\b.*?\bto\s+([0-9,]+(?:\.[0-9]+)?)\s+([A-Za-z]{3})\/([A-Za-z]+)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None

        raw_price = float(match.group(1).replace(",", ""))
        ccy = match.group(2).upper()
        unit = match.group(3).upper()

        # If already INR per tonne, use directly.
        if ccy == "INR" and (unit.startswith("T") or unit.startswith("MT")):
            return {
                "price_inr_mt": round(raw_price, 0),
                "source": "TradingEconomics (page scrape)",
                "status": "live",
            }

        # TradingEconomics steel is often in CNY/T. Convert via INR/CNY proxy.
        if ccy == "CNY" and (unit.startswith("T") or unit.startswith("MT")):
            # Approx conversion for fallback path; keeps data live-driven.
            inr_per_cny = 11.5
            return {
                "price_inr_mt": round(raw_price * inr_per_cny, 0),
                "source": "TradingEconomics steel (CNY/T converted)",
                "status": "computed",
            }
        return None
    except Exception as e:
        log.warning(f"TradingEconomics steel scrape failed: {e}")
        return None


def _fetch_alpha_vantage_commodity_series(function_name: str, interval: str):
    """
    Fetch Alpha Vantage commodity series and return parsed points.
    Returns list of {"date": str, "value": float}, sorted newest first.
    """
    url = "https://www.alphavantage.co/query"
    params = {
        "function": function_name,
        "interval": interval,
        "apikey": ALPHA_VANTAGE_KEY,
    }
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    points = []
    for d in data.get("data", []):
        raw = d.get("value")
        if raw in (None, "."):
            continue
        try:
            points.append({"date": d.get("date"), "value": float(raw)})
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda p: p.get("date") or "", reverse=True)
    return points


def _history_values_oldest_first(daily_points: list[dict], n: int = 90) -> list[float]:
    """Last n daily closes, oldest → newest (required by trend_engine)."""
    if not daily_points:
        return []
    window = daily_points[:n]
    return [float(p["value"]) for p in reversed(window)]


def _avg_recent(points: list[dict], n: int) -> float | None:
    if not points:
        return None
    vals = [float(p["value"]) for p in points[: max(1, n)]]
    if not vals:
        return None
    return sum(vals) / len(vals)

# ─────────────────────────────────────────────────────────────────────────────
# 1. COPPER — LME via Alpha Vantage
# ─────────────────────────────────────────────────────────────────────────────
def fetch_copper_lme():
    """Fetch LME Copper price in USD/MT from Alpha Vantage."""
    try:
        daily_points = _fetch_alpha_vantage_commodity_series("COPPER", "daily")
        monthly_points = _fetch_alpha_vantage_commodity_series("COPPER", "monthly")
        if not daily_points:
            raise RuntimeError("No daily copper points returned from Alpha Vantage")
        latest = daily_points[0]
        history = _history_values_oldest_first(daily_points, 90)
        avg_usd_mt = _avg_recent(monthly_points, MARKET_BENCHMARK_AVG_MONTHS)
        benchmark_usd_mt = float(avg_usd_mt) if avg_usd_mt is not None else float(latest["value"])

        return {
            "price_usd_mt": benchmark_usd_mt,
            "date":         latest["date"],
            "history_90d":  history,
            "source":       f"LME via Alpha Vantage ({MARKET_BENCHMARK_AVG_MONTHS}M avg)",
            "status":       "live"
        }
    except Exception as e:
        log.warning(f"Copper LME fetch failed: {e}. Trying TradingEconomics page.")
        te = fetch_tradingeconomics_spot_price("copper")
        if te and te.get("price_usd_mt"):
            p = float(te["price_usd_mt"])
            return {
                "price_usd_mt": p,
                "date":         str(datetime.now().date()),
                "history_90d":  [p] * 90,
                "source":       te.get("source", "TradingEconomics (page scrape)"),
                "status":       "live"
            }
        return {
            "price_usd_mt": FALLBACK_PRICES["copper_lme_usd_mt"],
            "date":         str(datetime.now().date()),
            "history_90d":  [FALLBACK_PRICES["copper_lme_usd_mt"]] * 90,
            "source":       "Fallback",
            "status":       "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 2. ALUMINIUM — LME via Alpha Vantage
# ─────────────────────────────────────────────────────────────────────────────
def fetch_aluminium_lme():
    """Fetch LME Aluminium price in USD/MT."""
    try:
        daily_points = _fetch_alpha_vantage_commodity_series("ALUMINUM", "daily")
        monthly_points = _fetch_alpha_vantage_commodity_series("ALUMINUM", "monthly")
        if not daily_points:
            raise RuntimeError("No daily aluminium points returned from Alpha Vantage")
        latest = daily_points[0]
        history = _history_values_oldest_first(daily_points, 90)
        avg_usd_mt = _avg_recent(monthly_points, MARKET_BENCHMARK_AVG_MONTHS)
        benchmark_usd_mt = float(avg_usd_mt) if avg_usd_mt is not None else float(latest["value"])

        return {
            "price_usd_mt": benchmark_usd_mt,
            "date":         latest["date"],
            "history_90d":  history,
            "source":       f"LME via Alpha Vantage ({MARKET_BENCHMARK_AVG_MONTHS}M avg)",
            "status":       "live"
        }
    except Exception as e:
        log.warning(f"Aluminium LME fetch failed: {e}. Trying TradingEconomics page.")
        te = fetch_tradingeconomics_spot_price("aluminum")
        if te and te.get("price_usd_mt"):
            p = float(te["price_usd_mt"])
            return {
                "price_usd_mt": p,
                "history_90d":  [p] * 90,
                "source":       te.get("source", "TradingEconomics (page scrape)"),
                "status":       "live"
            }
        return {
            "price_usd_mt": FALLBACK_PRICES["aluminium_lme_usd_mt"],
            "history_90d":  [FALLBACK_PRICES["aluminium_lme_usd_mt"]] * 90,
            "source":       "Fallback",
            "status":       "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 3. USD / INR — RBI (Free, no key needed)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_usd_inr():
    """Fetch live USD/INR rate from exchangerate-api (free tier)."""
    try:
        # Free public FX API — no key required
        url = "https://open.er-api.com/v6/latest/USD"
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        rate = data["rates"]["INR"]
        return {
            "rate": round(rate, 4),
            "date": data.get("time_last_update_utc", str(datetime.now())),
            "source": "ExchangeRate-API",
            "status": "live"
        }
    except Exception as e:
        log.warning(f"FX rate fetch failed: {e}. Using fallback.")
        return {
            "rate":   FALLBACK_PRICES["usd_inr"],
            "source": "Fallback",
            "status": "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 4. WORLD BANK — Copper & Aluminium Historical Index (Free)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_worldbank_commodity(indicator, name):
    """
    Fetch 12-month history from World Bank Commodity API.
    indicator: PCOPP (Copper), PALUM (Aluminium), PIORECR (Iron Ore)
    """
    try:
        url = f"https://api.worldbank.org/v2/en/indicator/{indicator}"
        params = {"format": "json", "mrv": 12, "frequency": "M"}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        entries = data[1] if len(data) > 1 else []
        history = [
            {"date": e["date"], "value": e["value"]}
            for e in entries if e["value"] is not None
        ]
        return {
            "name":      name,
            "indicator": indicator,
            "history":   history,
            "latest":    history[0] if history else None,
            "source":    "World Bank",
            "status":    "live"
        }
    except Exception as e:
        log.warning(f"World Bank {indicator} fetch failed: {e}")
        return {"name": name, "indicator": indicator, "history": [], "status": "failed"}

# ─────────────────────────────────────────────────────────────────────────────
# 5. Financial Modeling Prep — Commodity quotes
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_fmp_commodities_list():
    """Fetch tracked commodity symbols from FMP."""
    if not FMP_API_KEY:
        return []
    try:
        url = "https://financialmodelingprep.com/stable/commodities-list"
        r = requests.get(url, params={"apikey": FMP_API_KEY}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"FMP commodities-list fetch failed: {e}")
        return []


def fetch_fmp_commodity_quote(commodity: str):
    """
    Fetch commodity quote from Financial Modeling Prep.
    commodity: lower-case keyword like 'copper' or 'aluminum'
    """
    if not FMP_API_KEY:
        return {"commodity": commodity, "status": "skipped", "reason": "missing_api_key"}
    try:
        commodities = _fetch_fmp_commodities_list()
        symbol = None
        for item in commodities:
            name = str(item.get("name", "")).lower()
            if commodity in name:
                symbol = item.get("symbol")
                break

        # Fallback symbol guesses if list lookup misses
        if not symbol:
            symbol = {
                "copper": "HGUSD",
                "aluminum": "ALIUSD",
            }.get(commodity)
        if not symbol:
            return {"commodity": commodity, "status": "failed", "reason": "symbol_not_found"}

        url = "https://financialmodelingprep.com/stable/quote"
        params = {"symbol": symbol, "apikey": FMP_API_KEY}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            item = data[0]
            return {
                "commodity":     commodity,
                "symbol":        symbol,
                "name":          item.get("name"),
                "price":         item.get("price"),
                "change_1d":     item.get("change"),
                "change_pct_1d": item.get("changesPercentage"),
                "day_high":      item.get("dayHigh"),
                "day_low":       item.get("dayLow"),
                "year_high":     item.get("yearHigh"),
                "year_low":      item.get("yearLow"),
                "forecast_12m":  None,
                "source":        "Financial Modeling Prep",
                "status":       "live"
            }
    except Exception as e:
        log.warning(f"FMP commodity quote {commodity} failed: {e}")
        return {"commodity": commodity, "status": "failed"}

# ─────────────────────────────────────────────────────────────────────────────
# 6. MCX INDIA — INR Commodity Prices (via NSE/BSE data or MCX API)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_mcx_copper_inr(usd_inr_rate):
    """
    MCX copper price in INR/MT.
    Primary: MCX API (if key available)
    Fallback: Convert LME USD to INR + 3% import premium
    """
    try:
        if MCX_API_KEY:
            url = "https://www.mcxindia.com/api/market-data"
            headers = {"Authorization": f"Bearer {MCX_API_KEY}"}
            params  = {"commodity": "COPPER", "segment": "SPOT"}
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return {
                "price_inr_mt": data["spot_price"],
                "source": "MCX India",
                "status": "live"
            }
        else:
            # Fallback: LME price × FX rate × 1.03 (import duty + premium)
            copper_lme = fetch_copper_lme()
            inr_price  = copper_lme["price_usd_mt"] * usd_inr_rate * 1.03
            return {
                "price_inr_mt": round(inr_price, 0),
                "source":       "Computed (LME × FX × 1.03)",
                "status":       "computed"
            }
    except Exception as e:
        log.warning(f"MCX copper failed: {e}")
        return {
            "price_inr_mt": FALLBACK_PRICES["copper_lme_usd_mt"] * usd_inr_rate,
            "source": "Fallback",
            "status": "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 7. CRGO STEEL — Via Fastmarkets API or computed from MEPS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_crgo_steel_price():
    """
    CRGO (Cold Rolled Grain Oriented) Steel price in INR/MT.
    Primary: Fastmarkets API (if licensed)
    Fallback: Ministry of Steel India public price notification
    """
    try:
        if os.getenv("FASTMARKETS_KEY"):
            url = "https://api.fastmarkets.com/v1/prices/electrical-steel"
            headers = {"Authorization": f"Bearer {os.getenv('FASTMARKETS_KEY')}"}
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return {
                "price_inr_mt": data["india_crgo_price_inr"],
                "grade":        data.get("grade", "M4"),
                "source":       "Fastmarkets",
                "status":       "live"
            }
        else:
            # Public scrape from Ministry of Steel India price update
            # or use a fixed reference updated manually each quarter
            return {
                "price_inr_mt": FALLBACK_PRICES["crgo_steel_inr_mt"],
                "grade":        "M4",
                "source":       "Reference price (update weekly)",
                "status":       "manual"
            }
    except Exception as e:
        log.warning(f"CRGO Steel price fetch failed: {e}")
        return {
            "price_inr_mt": FALLBACK_PRICES["crgo_steel_inr_mt"],
            "source": "Fallback",
            "status": "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 8. TRANSFORMER OIL — Via Platts or crude oil index
# ─────────────────────────────────────────────────────────────────────────────
def fetch_transformer_oil_price():
    """
    Transformer Oil (mineral insulating oil) price in INR/KL.
    Primary: Platts API (if licensed)
    Fallback: Last 30 days PO data from PostgreSQL
    """
    try:
        if PLATTS_KEY:
            url = "https://api.platts.com/market-data/assessments/latest"
            headers = {"Authorization": f"Bearer {PLATTS_KEY}"}
            params  = {"symbol": "AAVPP00"}  # Platts transformer oil symbol
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            live_kl = payload["data"][0]["value_inr"]
            return {
                "price_inr_kl": live_kl,
                "source": "Platts",
                "status": "live",
                "po_avg_inr_kl": _fetch_transformer_oil_po_avg_inr_kl(30),
            }
        # If Platts key is unavailable, use DB (internal historical) directly.
        db_oil = _fetch_transformer_oil_from_db(days=30)
        db_oil["po_avg_inr_kl"] = db_oil["price_inr_kl"]
        return db_oil
    except Exception as e:
        log.warning(f"Transformer oil price fetch failed: {e}. Trying DB fallback.")
        try:
            db_oil = _fetch_transformer_oil_from_db(days=30)
            db_oil["po_avg_inr_kl"] = db_oil["price_inr_kl"]
            return db_oil
        except Exception as db_e:
            log.warning(f"Transformer oil DB fallback failed: {db_e}")
            return {
                "price_inr_kl": FALLBACK_PRICES["transformer_oil_inr_kl"],
                "source": "Fallback",
                "status": "fallback",
                "po_avg_inr_kl": None,
            }

# ─────────────────────────────────────────────────────────────────────────────
# 9. HR STEEL (Tank & Radiators) — SAIL / GeM Portal
# ─────────────────────────────────────────────────────────────────────────────
def fetch_hr_steel_price():
    """
    HR Steel coil price in INR/MT from GeM Portal L1 or SAIL price list.
    """
    try:
        gem_key = os.getenv("GEM_PORTAL_KEY")
        if gem_key:
            url = "https://api.gem.gov.in/v1/catalogue/prices"
            headers = {"Authorization": f"Bearer {gem_key}"}
            params  = {"category": "HR_STEEL_COIL", "sort": "price_asc", "limit": 1}
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return {
                "price_inr_mt": data["items"][0]["unit_price"],
                "vendor":       data["items"][0]["vendor_name"],
                "source":       "GeM Portal L1",
                "status":       "live"
            }
        else:
            te_steel = fetch_tradingeconomics_steel_inr_mt()
            if te_steel and te_steel.get("price_inr_mt"):
                return te_steel
            return {
                "price_inr_mt": FALLBACK_PRICES["hr_steel_inr_mt"],
                "source":       "Reference price (SAIL list)",
                "status":       "manual"
            }
    except Exception as e:
        log.warning(f"HR Steel price fetch failed: {e}. Trying TradingEconomics steel.")
        te_steel = fetch_tradingeconomics_steel_inr_mt()
        if te_steel and te_steel.get("price_inr_mt"):
            return te_steel
        return {
            "price_inr_mt": FALLBACK_PRICES["hr_steel_inr_mt"],
            "source": "Fallback",
            "status": "fallback"
        }

# ─────────────────────────────────────────────────────────────────────────────
# 10. MASTER FETCH — All market data in one call
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all_market_data():
    """
    Fetches all commodity prices, FX rate, and historical trends.
    Returns unified market_data dict used by analysis and PDF modules.
    """
    log.info("Fetching all market data...")
    results = {}

    internal_long: dict = {}
    internal_recent: dict = {}
    try:
        internal_long, internal_recent = _fetch_internal_po_windowed_metrics()
    except Exception as e:
        log.warning("Internal PO historical material pricing skipped: %s", e)

    def _internal_inr_mt(material_label: str, bucket: dict | None = None):
        row = (bucket or internal_long).get(material_label)
        if not row:
            return None
        return round(float(row["avg_price_per_kg_inr"]) * 1000, 0)

    def _po_avg_inr_mt(label: str):
        return _internal_inr_mt(label, internal_long)

    results["copper_po_avg_inr_mt"] = _po_avg_inr_mt("Copper")
    results["aluminium_po_avg_inr_mt"] = _po_avg_inr_mt("Aluminium")
    results["crgo_po_avg_inr_mt"] = _po_avg_inr_mt("CRGO")
    results["amorphous_po_avg_inr_mt"] = _po_avg_inr_mt("Amorphous")
    results["hr_steel_po_avg_inr_mt"] = _po_avg_inr_mt("HR Steel")

    # FX rate first (needed for conversions)
    fx = fetch_usd_inr()
    results["usd_inr"]       = fx["rate"]
    results["usd_inr_source"] = fx["source"]

    # Commodities
    time.sleep(1)  # Avoid API rate limits
    copper = fetch_copper_lme()
    results["copper_lme_usd_mt"]    = copper["price_usd_mt"]
    results["copper_history_90d"]   = copper["history_90d"]
    cu_inr_i = _internal_inr_mt("Copper")
    # Prefer live LME/spot for headline INR; use internal PO history only when live is unavailable.
    if cu_inr_i is not None and str(copper.get("status") or "").lower() != "live":
        results["copper_lme_inr_mt"] = cu_inr_i
        results["copper_source"] = INTERNAL_HISTORICAL_SOURCE
        results["copper_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["copper_lme_inr_mt"] = round(copper["price_usd_mt"] * fx["rate"] * 1.03, 0)
        results["copper_source"] = copper["source"]
        results["copper_status"] = copper["status"]
    # Keep independent benchmark for variance math even if headline uses internal historical.
    results["copper_market_benchmark_inr_mt"] = round(copper["price_usd_mt"] * fx["rate"] * 1.03, 0)
    results["copper_market_benchmark_source"] = copper.get("source")
    results["copper_market_benchmark_status"] = copper.get("status")

    time.sleep(1)
    aluminium = fetch_aluminium_lme()
    results["aluminium_lme_usd_mt"] = aluminium["price_usd_mt"]
    results["aluminium_history_90d"] = aluminium["history_90d"]
    al_inr_i = _internal_inr_mt("Aluminium")
    if al_inr_i is not None and str(aluminium.get("status") or "").lower() != "live":
        results["aluminium_lme_inr_mt"] = al_inr_i
        results["aluminium_source"] = INTERNAL_HISTORICAL_SOURCE
        results["aluminium_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["aluminium_lme_inr_mt"] = round(
            aluminium["price_usd_mt"] * fx["rate"] * 1.03, 0
        )
        results["aluminium_source"] = aluminium["source"]
        results["aluminium_status"] = aluminium.get("status")
    results["aluminium_market_benchmark_inr_mt"] = round(
        aluminium["price_usd_mt"] * fx["rate"] * 1.03, 0
    )
    results["aluminium_market_benchmark_source"] = aluminium.get("source")
    results["aluminium_market_benchmark_status"] = aluminium.get("status")

    time.sleep(1)
    crgo = fetch_crgo_steel_price()
    results["crgo_market_benchmark_inr_mt"] = crgo["price_inr_mt"]
    results["crgo_market_benchmark_source"] = crgo.get("source")
    results["crgo_market_benchmark_status"] = crgo.get("status")
    crgo_inr_i = _internal_inr_mt("CRGO")
    crgo_recent_i = _internal_inr_mt("CRGO", internal_recent)
    if crgo_inr_i is not None:
        results["crgo_steel_inr_mt"] = crgo_inr_i
        results["crgo_source"] = INTERNAL_HISTORICAL_SOURCE
        results["crgo_status"] = INTERNAL_HISTORICAL_STATUS
        results["crgo_market_benchmark_inr_mt"] = crgo_recent_i or crgo_inr_i
        results["crgo_market_benchmark_source"] = INTERNAL_HISTORICAL_SOURCE
        results["crgo_market_benchmark_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["crgo_steel_inr_mt"] = crgo["price_inr_mt"]
        results["crgo_source"] = crgo["source"]
        results["crgo_status"] = crgo.get("status")

    am_inr_i = _internal_inr_mt("Amorphous")
    am_recent_i = _internal_inr_mt("Amorphous", internal_recent)
    if am_inr_i is not None:
        results["amorphous_core_inr_mt"] = am_inr_i
        results["amorphous_source"] = INTERNAL_HISTORICAL_SOURCE
        results["amorphous_status"] = INTERNAL_HISTORICAL_STATUS
        results["amorphous_market_benchmark_inr_mt"] = am_recent_i or am_inr_i
        results["amorphous_market_benchmark_source"] = INTERNAL_HISTORICAL_SOURCE
        results["amorphous_market_benchmark_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["amorphous_core_inr_mt"] = FALLBACK_PRICES["amorphous_core_inr_mt"]
        results["amorphous_source"] = "Reference price (manual)"
        results["amorphous_status"] = "manual"
        results["amorphous_market_benchmark_inr_mt"] = FALLBACK_PRICES["amorphous_core_inr_mt"]
        results["amorphous_market_benchmark_source"] = "Reference price (manual)"
        results["amorphous_market_benchmark_status"] = "manual"

    oil_recent_days = _internal_po_lookback_days("INTERNAL_PO_RECENT_LOOKBACK_DAYS", 90)
    oil_po_avg_kl = _fetch_transformer_oil_po_avg_inr_kl()
    oil_recent_kl = _fetch_transformer_oil_po_avg_inr_kl(days=oil_recent_days)
    oil = fetch_transformer_oil_price()
    results["transformer_oil_inr_kl"] = oil["price_inr_kl"]
    results["oil_source"] = oil["source"]
    results["transformer_oil_po_avg_inr_kl"] = oil_po_avg_kl or oil.get("po_avg_inr_kl")
    if str(oil.get("status") or "").lower() == "internal_historical":
        results["transformer_oil_market_benchmark_inr_kl"] = (
            oil_recent_kl or oil.get("po_avg_inr_kl") or oil["price_inr_kl"]
        )
        results["transformer_oil_market_benchmark_source"] = INTERNAL_HISTORICAL_SOURCE
        results["transformer_oil_market_benchmark_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["transformer_oil_market_benchmark_inr_kl"] = oil.get("po_avg_inr_kl") or oil["price_inr_kl"]
        results["transformer_oil_market_benchmark_source"] = oil.get("source")
        results["transformer_oil_market_benchmark_status"] = oil.get("status")
    if oil.get("db_rows_30d") is not None:
        results["transformer_oil_db_rows_30d"] = oil["db_rows_30d"]
    results["oil_status"] = oil.get("status")

    hr = fetch_hr_steel_price()
    results["hr_steel_market_benchmark_inr_mt"] = hr["price_inr_mt"]
    results["hr_steel_market_benchmark_source"] = hr.get("source")
    results["hr_steel_market_benchmark_status"] = hr.get("status")
    hr_inr_i = _internal_inr_mt("HR Steel")
    hr_recent_i = _internal_inr_mt("HR Steel", internal_recent)
    if hr_inr_i is not None:
        results["hr_steel_inr_mt"] = hr_inr_i
        results["hr_steel_source"] = INTERNAL_HISTORICAL_SOURCE
        results["hr_steel_status"] = INTERNAL_HISTORICAL_STATUS
        results["hr_steel_market_benchmark_inr_mt"] = hr_recent_i or hr_inr_i
        results["hr_steel_market_benchmark_source"] = INTERNAL_HISTORICAL_SOURCE
        results["hr_steel_market_benchmark_status"] = INTERNAL_HISTORICAL_STATUS
    else:
        results["hr_steel_inr_mt"] = hr["price_inr_mt"]
        results["hr_steel_source"] = hr["source"]
        results["hr_steel_status"] = hr.get("status")

    # World Bank historical indexes
    time.sleep(1)
    results["worldbank_copper"]     = fetch_worldbank_commodity("PCOPP", "Copper")
    results["worldbank_aluminium"]  = fetch_worldbank_commodity("PALUM", "Aluminium")

    # FMP commodity quotes
    if FMP_API_KEY:
        time.sleep(1)
        results["forecast_copper"]    = fetch_fmp_commodity_quote("copper")
        results["forecast_aluminium"] = fetch_fmp_commodity_quote("aluminum")

    results["fetched_at"] = datetime.now().isoformat()
    log.info(f"Market data fetched: copper={results['copper_lme_usd_mt']} USD/MT, "
             f"FX={results['usd_inr']}")
    return results
