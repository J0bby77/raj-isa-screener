#!/usr/bin/env python3
"""
build_email.py  --  ISA Growth Stock Analysis HTML email body builder
Version: 1.0  |  2026-05-24

Pre-built script. Called from analysis bash scripts after scoring + retrospective writing.
Generates a compliant HTML email body string for GMAIL_SEND_EMAIL (Composio).

Usage:
    python3 build_email.py \
        --group NASDAQ \
        --run_date "22-May-26" \
        --full_data /path/YYYYMMDD_NASDAQ_full_data.csv \
        --gates /path/YYYYMMDD_NASDAQ_yf_gate_results.csv \
        --retrospective /path/YYYYMMDD_NASDAQ_retrospective.md \
        --output /path/YYYYMMDD_NASDAQ_email_body.html \
        [--run_qa /path/YYYYMMDD_NASDAQ_run_qa.csv] \
        [--unresolved /path/YYYYMMDD_NASDAQ_unresolved_metrics.csv] \
        [--tech_fails /path/YYYYMMDD_NASDAQ_technical_failures.csv] \
        [--constituent /path/YYYYMMDD_NASDAQ_constituent_master.csv]

Output:
    A .html file containing ONLY the email body fragment (no DOCTYPE/html/head/body tags).
    Pass file contents as the `body` parameter of GMAIL_SEND_EMAIL with is_html=true.

Email Template Rules applied here:
    Rule 1 -- No Unicode above U+007F (all replaced with HTML entities)
    Rule 2 -- No <head> or <style> blocks (all styles inline)
    Rule 3 -- No flexbox / grid (KPI row uses <table> layout)
    Rule 4 -- All styles inline (no class references)
    Rule 5 -- No DOCTYPE/html/head/body wrappers
    Rule 6 -- is_html: true reminder printed to stdout after generation
"""

import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import csv
import os
import re
import sys
from datetime import datetime


# ---------------------------------------------------------------------------
# Field name aliases: maps alternative CSV column names -> canonical names
# ---------------------------------------------------------------------------
FIELD_MAP = {
    # Identity
    "symbol": "ticker",
    "Symbol": "ticker",
    "Ticker": "ticker",
    "TICKER": "ticker",
    "Name": "company",
    "Company": "company",
    "COMPANY": "company",
    "company_name": "company",
    "Sector": "sector",
    "SECTOR": "sector",
    "Industry": "industry",
    "INDUSTRY": "industry",
    "Index": "index_name",
    "INDEX": "index_name",
    "index": "index_name",
    "Final Status": "final_status",
    "FinalStatus": "final_status",
    "Status": "final_status",
    "STATUS": "final_status",
    # Scores
    "part_a": "part_a_score",
    "PartA": "part_a_score",
    "Part A Score": "part_a_score",
    "part_a_total": "part_a_score",
    "part_b": "part_b_score",
    "PartB": "part_b_score",
    "Part B Score": "part_b_score",
    "part_b_total": "part_b_score",
    "Total": "total_score",
    "TOTAL": "total_score",
    "total": "total_score",
    "Score": "total_score",
    "score": "total_score",
    # Growth
    "RevCAGR": "rev_cagr",
    "Rev CAGR": "rev_cagr",
    "revenue_cagr": "rev_cagr",
    "rev_cagr_3yr": "rev_cagr",
    "GrossMargin": "gross_margin",
    "Gross Margin": "gross_margin",
    "gross_margin_pct": "gross_margin",
    "GM": "gross_margin",
    # Valuation/risk
    "ROIC": "roic",
    "roic_pct": "roic",
    "FCFYield": "fcf_yield",
    "FCF Yield": "fcf_yield",
    "fcf_yield_pct": "fcf_yield",
    "Upside": "upside_pct",
    "UPSIDE": "upside_pct",
    "target_upside": "upside_pct",
    "upside": "upside_pct",
    "TargetUpside": "upside_pct",
    "CurrentPrice": "current_price",
    "current_price_corrected": "current_price",
    "Price": "current_price",
    "TargetMean": "target_mean",
    "target_mean_price": "target_mean",
    "Target (mean)": "target_mean",
    "AnalystRating": "analyst_rating",
    "analyst_rating": "analyst_rating",
    "recommendationKey": "analyst_rating",
    "AnalystCount": "analyst_count",
    "analyst_count": "analyst_count",
    "numberOfAnalystOpinions": "analyst_count",
    "EpsCAGR": "eps_cagr",
    "eps_cagr_3yr": "eps_cagr",
    "EPS CAGR": "eps_cagr",
    "NetDebtEBITDA": "nd_ebitda",
    "net_debt_ebitda": "nd_ebitda",
    "Net Debt/EBITDA": "nd_ebitda",
    "Commentary": "commentary",
    "Qualitative Commentary": "commentary",
    "qualitative_commentary": "commentary",
    "comment": "commentary",
    # Gate status
    "gate_code": "gate_code",
    "Gate": "gate_code",
    "GATE": "gate_code",
    # Overlay
    "EstRevDirection": "est_rev_direction",
    "est_rev_direction": "est_rev_direction",
    "ROICvsWACC": "roic_vs_wacc",
    "roic_vs_wacc_spread": "roic_vs_wacc",
    "WACC": "wacc_pct",
    "wacc_pct": "wacc_pct",
    # DQ
    "dq_flag": "dq_flag",
    "DQ Flag": "dq_flag",
    "Severity": "severity",
    "severity": "severity",
    "issue": "issue",
    "Issue": "issue",
    "source_attempted": "source_attempted",
    # Run QA
    "metric": "metric",
    "value": "value",
    "Value": "value",
}

# Status codes indicating strong buy
STRONG_BUY_STATUSES = {
    "CANDIDATE_RANKABLE", "LOW_CONFIDENCE_SCORED",
    "STRONG_BUY", "Strong Buy"
}

# Gate exclusion codes
GATE_EXCLUSION_CODES = {
    "Gate 1", "Gate 2", "Gate 3", "Gate 4", "MktCap",
    "GATE1", "GATE2", "GATE3", "GATE4",
    "PRE_SCREEN_EXCLUDED", "STRUCTURAL_NON_APPLICABLE"
}


# ---------------------------------------------------------------------------
# HTML entity safety
# ---------------------------------------------------------------------------
ENTITY_MAP = [
    ("≥", "&ge;"),     # >=
    ("≤", "&le;"),     # <=
    ("—", "&mdash;"),  # em dash
    ("–", "&ndash;"),  # en dash
    ("−", "&minus;"),  # minus sign
    ("§", "&sect;"),   # section
    ("→", "&rarr;"),   # right arrow
    ("×", "&times;"),  # multiply
    ("£", "&pound;"),  # pound
    ("€", "&euro;"),   # euro
    ("±", "&plusmn;"), # plus-minus
    ("≠", "&ne;"),     # not equal
    ("©", "&copy;"),   # copyright
    ("®", "&reg;"),    # registered
    ("’", "&rsquo;"),  # right single quote
    ("‘", "&lsquo;"),  # left single quote
    ("“", "&ldquo;"),  # left double quote
    ("”", "&rdquo;"),  # right double quote
    ("…", "&hellip;"), # ellipsis
    ("°", "&deg;"),    # degree
    ("²", "&sup2;"),   # superscript 2
    ("³", "&sup3;"),   # superscript 3
]


def safe_entities(text: str) -> str:
    """Replace all non-ASCII characters with HTML entities. Must be called on
    every string written into the email body."""
    if not text:
        return ""
    for char, entity in ENTITY_MAP:
        text = text.replace(char, entity)
    # Catch any remaining non-ASCII characters with numeric entities
    result = []
    for c in text:
        if ord(c) > 127:
            result.append(f"&#{ord(c)};")
        else:
            result.append(c)
    return "".join(result)


def verify_entities(html: str) -> bool:
    """Return True if no non-ASCII bytes remain in the body string."""
    violations = [c for c in html if ord(c) > 127]
    if violations:
        print(f"WARNING: {len(violations)} non-ASCII characters remain in HTML body.")
        print(f"  First violation: U+{ord(violations[0]):04X} ({violations[0]!r})")
        return False
    return True


# ---------------------------------------------------------------------------
# Number formatting helpers
# ---------------------------------------------------------------------------
def sf(v):
    """Safe float."""
    try:
        f = float(v)
        import math
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def pct(v, decimals=1):
    """Format decimal as percentage string, e.g. 0.154 -> '15.4%'."""
    f = sf(v)
    if f is None:
        return "N/A"
    return f"{f * 100:.{decimals}f}%"


def pct_upside(v):
    """Format upside with sign and colour HTML."""
    f = sf(v)
    if f is None:
        return "N/A"
    pct_val = f * 100
    sign = "+" if pct_val >= 0 else ""
    colour = "#1a6b2a" if pct_val >= 0 else "#c0392b"
    return f'<span style="color:{colour}">{sign}{pct_val:.1f}%</span>'


def mult(v, decimals=1):
    """Format as multiple, e.g. 12.4 -> '12.4x'."""
    f = sf(v)
    if f is None:
        return "N/A"
    return f"{f:.{decimals}f}x"


def price_fmt(v, currency_sym=""):
    """Format price to 2dp with optional currency symbol."""
    f = sf(v)
    if f is None:
        return "N/A"
    return f"{currency_sym}{f:.2f}"


def score_int(v):
    """Format score as integer."""
    f = sf(v)
    if f is None:
        return "N/A"
    return str(int(round(f)))


def rating_display(raw):
    """Humanise analyst rating from recommendationKey."""
    if not raw or raw == "N/A":
        return "N/A"
    return raw.replace("_", " ").title()


# ---------------------------------------------------------------------------
# CSV loading with field normalisation
# ---------------------------------------------------------------------------
def load_csv(path):
    """Load CSV into list of dicts with normalised field names."""
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalised = {}
            for k, v in row.items():
                canonical = FIELD_MAP.get(k, k)
                normalised[canonical] = v.strip() if v else ""
            rows.append(normalised)
    return rows


def get_field(row, *fields, default=""):
    """Try multiple field names in order; return first non-empty value."""
    for f in fields:
        canonical = FIELD_MAP.get(f, f)
        v = row.get(canonical) or row.get(f, "")
        if v and v != "nan" and v != "None":
            return v
    return default


# ---------------------------------------------------------------------------
# Data classification
# ---------------------------------------------------------------------------
def is_strong_buy(row):
    a = sf(get_field(row, "part_a_score"))
    b = sf(get_field(row, "part_b_score"))
    status = get_field(row, "final_status")
    if a is not None and b is not None:
        return a >= 22 and b >= 19
    return status in STRONG_BUY_STATUSES


def is_gate_passer(row):
    """True if stock passed gates (was scored, regardless of strong buy)."""
    status = get_field(row, "final_status")
    gate = get_field(row, "gate_code")
    if gate in GATE_EXCLUSION_CODES:
        return False
    if status in ("PRE_SCREEN_EXCLUDED", "STRUCTURAL_NON_APPLICABLE",
                  "GATE_DATA_UNRESOLVED", "TECHNICAL_SOURCE_FAILURE"):
        return False
    return True


def get_coverage_counts(full_data, gate_data):
    """Compute coverage statistics from full data + gate results."""
    counts = {
        "total_constituents": 0,
        "analysed": 0,
        "strong_buys": 0,
        "fair_mixed": 0,
        "acceptable": 0,
        "pre_screen_excluded": 0,
        "hard_gate_fail": 0,
        "insufficient_data": 0,
        "not_screened": 0,
    }

    # Count from gate data for total constituents
    all_rows = full_data + gate_data
    seen = set()
    for row in all_rows:
        t = get_field(row, "ticker")
        if t and t not in seen:
            seen.add(t)

    counts["total_constituents"] = max(len(seen), len(full_data))

    for row in full_data:
        status = get_field(row, "final_status")
        a = sf(get_field(row, "part_a_score"))
        b = sf(get_field(row, "part_b_score"))

        # Strong Buy is threshold-based (Part A >= 22 AND Part B >= 19), matching the
        # SUMMARY tab / KPI tile / is_strong_buy(). Check it FIRST so a qualifying stock
        # is never mis-bucketed into hard_gate_fail (which previously undercounted the
        # headline by 1 when a Strong Buy also carried a MANDATORY_MINIMUM_FAIL status).
        if is_strong_buy(row):
            counts["analysed"] += 1
            counts["strong_buys"] += 1
        elif status in ("PRE_SCREEN_EXCLUDED", "STRUCTURAL_NON_APPLICABLE"):
            counts["pre_screen_excluded"] += 1
        elif status in ("HARD_GATE_FAIL", "MANDATORY_MINIMUM_FAIL"):
            counts["hard_gate_fail"] += 1
        elif status in ("GATE_DATA_UNRESOLVED", "TECHNICAL_SOURCE_FAILURE"):
            counts["insufficient_data"] += 1
        else:
            counts["analysed"] += 1
            if a is not None and b is not None:
                if a >= 22:
                    counts["fair_mixed"] += 1
                elif a >= 14:
                    counts["acceptable"] += 1

    # Count gate exclusions from gate data not already in full_data
    full_tickers = {get_field(r, "ticker") for r in full_data}
    for row in gate_data:
        t = get_field(row, "ticker")
        if t and t not in full_tickers:
            status = get_field(row, "final_status")
            gate = get_field(row, "gate_code")
            if gate in GATE_EXCLUSION_CODES or status == "PRE_SCREEN_EXCLUDED":
                counts["pre_screen_excluded"] += 1

    return counts


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def build_coverage_line(counts, group, run_date):
    """Section 1 — Coverage stats bar."""
    total = counts["total_constituents"]
    analysed = counts["analysed"]
    pct_analysed = f"{(analysed / total * 100):.0f}%" if total else "N/A"
    sb = counts["strong_buys"]
    fm = counts["fair_mixed"]
    acc = counts["acceptable"]
    excl = counts["pre_screen_excluded"]
    hgf = counts["hard_gate_fail"]
    insuf = counts["insufficient_data"]
    ns = counts["not_screened"]

    text = (
        f"<strong style=\"color:#1a6b2a\">{sb} Strong Buy{'s' if sb != 1 else ''}</strong> | "
        f"{analysed} of {total} total constituents analysed ({pct_analysed}) | "
        f"{fm} Fair/Mixed | {acc} Acceptable | "
        f"{excl} Pre-Screen Excluded | {hgf} Hard Gate Fails | "
        f"{insuf} Insufficient Data | {ns} Not Screened"
    )
    html = (
        f'<p style="background:#f0f4fa;padding:10px 14px;border-left:4px solid #1a3a6b;'
        f'font-size:13px;margin-bottom:20px">{safe_entities(text)}</p>'
    )
    # Replace the strong tag we built manually (it's safe ASCII)
    html = html.replace(
        safe_entities(f"<strong style=\"color:#1a6b2a\">{sb} Strong Buy{'s' if sb != 1 else ''}</strong>"),
        f'<strong style="color:#1a6b2a">{sb} Strong Buy{"s" if sb != 1 else ""}</strong>'
    )
    return html


def _cfg_get(name, default):
    try:
        import scoring_config as _c; return getattr(_c, name, default)
    except Exception:
        return default

def _source_score(row):
    """S5 forward-led screen Source Score (0-1) — mirrors build_excel SUMMARY (_src)."""
    sw = _cfg_get("SUMMARY_SOURCE_WEIGHTS", {"forward":0.45,"quality":0.30,"valuation":0.25})
    fwd = sf(get_field(row,"forward_axis_score")) or 0.0
    a   = sf(get_field(row,"part_a_score")) or 0.0
    b   = sf(get_field(row,"part_b_score")) or 0.0
    return sw.get("forward",0.45)*fwd/100.0 + sw.get("quality",0.30)*a/28.0 + sw.get("valuation",0.25)*b/22.0

def _summary_eligible(row):
    """S5 SUMMARY viability eligibility — mirrors build_excel (floor + Part B + not-deteriorating + not-fail)."""
    a = sf(get_field(row,"part_a_score")); b = sf(get_field(row,"part_b_score"))
    if a is None or b is None: return False
    paf = _cfg_get("FORWARD_ELIG_PART_A_FLOOR", 10)
    st = str(get_field(row,"final_status") or "").upper()
    ok = st not in {"HARD_GATE_FAIL","MANDATORY_MINIMUM_FAIL","UNRESOLVED_HARD_GATE_NOT_RANKABLE"}
    notdet = str(get_field(row,"est_rev_direction") or "").lower() != "deteriorating"
    stage = str(get_field(row,"revision_stage") or "")
    stage_ok = stage not in set(_cfg_get("SUMMARY_STAGE_EXCLUDE", []))
    return a >= paf and b >= 14 and notdet and ok and stage_ok

def build_kpi_tiles(strong_buys, gate_passers):
    """KPI tiles row using table layout (Rule 3 — no flexbox)."""
    sb_count = len(strong_buys)
    gp_count = len(gate_passers)

    # Top score
    if strong_buys:
        top = strong_buys[0]
        top_score = score_int(get_field(top, "total_score"))
        top_fwd = score_int(get_field(top, "forward_axis_score"))
        top_ticker = get_field(top, "ticker")
        top_max = int(sf(get_field(top, "total_max")) or 50)   # P2-6: max-aware (/50 base, /54 semis)
    else:
        top_score = "N/A"
        top_fwd = "N/A"
        top_ticker = "N/A"
        top_max = 50

    # Avg upside of top 10
    top10 = strong_buys[:10]
    upsides = [sf(get_field(r, "upside_pct")) for r in top10]
    upsides = [u for u in upsides if u is not None]
    avg_upside = f"{sum(upsides) / len(upsides) * 100:.1f}%" if upsides else "N/A"

    html = (
        '<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-bottom:24px">\n'
        '<tr>\n'
        f'<td style="width:25%;padding:0 6px 0 0">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#1a6b2a;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{sb_count}</td></tr>\n'
        f'  <tr><td style="color:#c8e6c9;font-size:11px">Candidates (fwd-led)</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#1a3a6b;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{top_fwd}/100</td></tr>\n'
        f'  <tr><td style="color:#b3c6e6;font-size:11px">Top Forward ({safe_entities(top_ticker)})</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#6b3a1a;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{gp_count}</td></tr>\n'
        f'  <tr><td style="color:#e6c9b3;font-size:11px">Gate Passers Scored</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 0 0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#4a1a6b;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{safe_entities(avg_upside)}</td></tr>\n'
        f'  <tr><td style="color:#d4b3e6;font-size:11px">Avg Upside (Top 10)</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        '</tr>\n'
        '</table>\n'
    )
    return html


def build_top10_table(strong_buys):
    """Section 2 — Top 10 stocks table (exactly 12 columns per Run_Context spec)."""
    if not strong_buys:
        return '<p style="color:#666;font-style:italic">No SUMMARY candidates identified this run.</p>\n'

    header_cols = [
        "Rank", "Ticker", "Company", "Sector",
        "Fwd /100", "Src", "Stage",
        "Part A", "Part B", "Total", "ROIC", "Upside"
    ]
    widths = ["4%", "6%", "16%", "11%", "6%", "5%", "11%", "5%", "5%", "6%", "7%", "8%"]

    # Header row
    header_html = "".join(
        f'<th style="background:#1a3a6b;color:#fff;padding:8px 6px;text-align:center;'
        f'font-size:12px;white-space:nowrap;width:{widths[i]}">{safe_entities(col)}</th>'
        for i, col in enumerate(header_cols)
    )

    # Data rows
    rows_html = ""
    for rank, row in enumerate(strong_buys[:10], 1):
        bg = "#f9f9f9" if rank % 2 == 0 else "#ffffff"
        ticker = safe_entities(get_field(row, "ticker") or "N/A")
        company = safe_entities(get_field(row, "company") or "N/A")
        sector = safe_entities(get_field(row, "sector") or "N/A")
        a = score_int(get_field(row, "part_a_score"))
        b = score_int(get_field(row, "part_b_score"))
        total = score_int(get_field(row, "total_score"))
        fwd = score_int(get_field(row, "forward_axis_score"))
        src = round(_source_score(row) * 100)
        stage = safe_entities(str(get_field(row, "revision_stage") or "—")[:11])
        roic_raw = sf(get_field(row, "roic"))
        upside_raw = sf(get_field(row, "upside_pct"))
        roic_str = pct(roic_raw) if roic_raw is not None else "N/A"

        # Upside with colour
        if upside_raw is not None:
            sign = "+" if upside_raw >= 0 else ""
            upside_colour = "#1a6b2a" if upside_raw >= 0 else "#c0392b"
            upside_str = f'<span style="color:{upside_colour}">{sign}{upside_raw*100:.1f}%</span>'
        else:
            upside_str = "N/A"

        cell_style = f'padding:7px 6px;border-bottom:1px solid #e8e8e8;background:{bg};font-size:12px;text-align:center'

        rows_html += (
            f'<tr>'
            f'<td style="{cell_style}">{rank}</td>'
            f'<td style="{cell_style};font-weight:bold">{ticker}</td>'
            f'<td style="{cell_style};text-align:left">{company}</td>'
            f'<td style="{cell_style};text-align:left">{sector}</td>'
            f'<td style="{cell_style};font-weight:bold;color:#6a1b9a">{fwd}</td>'
            f'<td style="{cell_style};font-weight:bold">{src}</td>'
            f'<td style="{cell_style};text-align:left;font-size:11px">{stage}</td>'
            f'<td style="{cell_style}">{a}</td>'
            f'<td style="{cell_style}">{b}</td>'
            f'<td style="{cell_style};color:#555">{total}</td>'
            f'<td style="{cell_style}">{safe_entities(roic_str)}</td>'
            f'<td style="{cell_style}">{upside_str}</td>'
            f'</tr>\n'
        )

    return (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Top 10 Stocks &mdash; By Source Score (forward-led)</h3>\n'
        '<table style="width:100%;border-collapse:collapse;margin-bottom:24px" '
        'cellpadding="0" cellspacing="0" border="0">\n'
        f'<tr>{header_html}</tr>\n'
        f'{rows_html}'
        '</table>\n'
    )


def _currency_sym(row):
    """Infer currency symbol from ticker suffix or currency field."""
    ticker = get_field(row, "ticker", default="")
    currency = get_field(row, "currency", default="")
    if currency in ("GBP", "GBp"):
        return "&pound;"
    if currency == "EUR":
        return "&euro;"
    if ticker.endswith(".L"):
        return "&pound;"
    if ticker.endswith((".DE", ".PA", ".MI", ".AS", ".SW", ".MA")):
        return "&euro;"
    if ticker.endswith((".TO", ".TSX")):
        return "CA$"
    if ticker.endswith(".SA"):
        return "R$"
    if ticker.endswith(".MX"):
        return "MX$"
    return "$"  # USD default


def build_top3_picks(strong_buys):
    """Section 3 — Top 3 picks with green-bordered tables."""
    if not strong_buys:
        return '<p style="color:#666;font-style:italic">No Strong Buy candidates for Top 3 section.</p>\n'

    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">Top 3 Picks</h3>\n'

    for rank, row in enumerate(strong_buys[:3], 1):
        margin_bottom = "24px" if rank == 3 else "16px"
        ticker = safe_entities(get_field(row, "ticker") or "N/A")
        company = safe_entities(get_field(row, "company") or "N/A")
        sector = safe_entities(get_field(row, "sector") or "N/A")
        industry = safe_entities(get_field(row, "industry") or "N/A")
        total = score_int(get_field(row, "total_score"))
        tmax = int(sf(get_field(row, "total_max")) or 50)   # P2-6: max-aware total denominator
        curr_sym = _currency_sym(row)

        # Price info
        price_raw = sf(get_field(row, "current_price"))
        target_raw = sf(get_field(row, "target_mean"))
        upside_raw = sf(get_field(row, "upside_pct"))
        analyst_count = get_field(row, "analyst_count") or "N/A"
        analyst_rating = rating_display(get_field(row, "analyst_rating") or "N/A")

        price_str = f"{curr_sym}{price_raw:.2f}" if price_raw is not None else "N/A"
        target_str = f"{curr_sym}{target_raw:.2f}" if target_raw is not None else "N/A"

        if upside_raw is not None:
            sign = "+" if upside_raw >= 0 else ""
            upside_colour = "#1a6b2a" if upside_raw >= 0 else "#c0392b"
            upside_str = f'<span style="color:{upside_colour};font-weight:bold">{sign}{upside_raw*100:.1f}%</span>'
        else:
            upside_str = "N/A"

        # Commentary
        commentary_raw = get_field(row, "commentary") or ""
        if not commentary_raw or commentary_raw in ("N/A", "nan", "None", ""):
            commentary_raw = (
                f"{company} scores {total}/{tmax} with strong Part A growth quality and Part B valuation metrics. "
                f"Sector: {sector}. Industry: {industry}. "
                f"Refer to SUMMARY tab in Excel for full overlay data and detailed analysis."
            )
        commentary = safe_entities(commentary_raw)

        # Mini metrics
        rev_cagr = pct(sf(get_field(row, "rev_cagr")))
        gm = pct(sf(get_field(row, "gross_margin")))
        roic = pct(sf(get_field(row, "roic")))
        fcf_y = pct(sf(get_field(row, "fcf_yield")))
        nd_ebitda_raw = sf(get_field(row, "nd_ebitda"))
        nd_ebitda = f"{nd_ebitda_raw:.1f}x" if nd_ebitda_raw is not None else "N/A"
        eps_cagr = pct(sf(get_field(row, "eps_cagr")))

        html += (
            f'<table cellpadding="14" cellspacing="0" border="0" '
            f'style="width:100%;border:1px solid #c8e6c9;border-radius:6px;'
            f'margin-bottom:{margin_bottom};border-collapse:separate">\n'
            f'<tr><td>\n'
            # Header line
            f'<div style="margin-bottom:6px">'
            f'<strong style="font-size:16px;color:#1a3a6b">{rank}. {ticker} &mdash; {company}</strong> '
            f'<span style="background:#1a6b2a;color:#fff;padding:2px 8px;border-radius:3px;'
            f'font-size:11px;font-weight:bold">{total}/{tmax}</span> '
            f'<span style="color:#888;font-size:12px">&nbsp;{sector} / {industry}</span>'
            f'</div>\n'
            # Price info row
            f'<div style="font-size:12px;color:#555;margin-bottom:10px">'
            f'Price: {price_str} &nbsp;|&nbsp; '
            f'Target: {target_str} &nbsp;|&nbsp; '
            f'Upside: {upside_str} &nbsp;|&nbsp; '
            f'{safe_entities(analyst_count)} analysts &mdash; {safe_entities(analyst_rating)}'
            f'</div>\n'
            # Commentary
            f'<p style="font-size:13px;margin:0 0 10px 0">{commentary}</p>\n'
            # Mini metrics
            f'<div style="font-size:11px;color:#555;border-top:1px solid #e8e8e8;padding-top:8px">'
            f'Rev CAGR: <strong>{safe_entities(rev_cagr)}</strong> &nbsp;|&nbsp; '
            f'GM: <strong>{safe_entities(gm)}</strong> &nbsp;|&nbsp; '
            f'ROIC: <strong>{safe_entities(roic)}</strong> &nbsp;|&nbsp; '
            f'FCF Yield: <strong>{safe_entities(fcf_y)}</strong> &nbsp;|&nbsp; '
            f'ND/EBITDA: <strong>{safe_entities(nd_ebitda)}</strong> &nbsp;|&nbsp; '
            f'EPS CAGR: <strong>{safe_entities(eps_cagr)}</strong>'
            f'</div>\n'
            f'</td></tr>\n'
            f'</table>\n'
        )

    return html


def build_key_observations(strong_buys, full_data, group):
    """Section 4 — Key observations."""
    observations = []

    # Check for sector concentration among strong buys
    if strong_buys:
        sector_counts = {}
        for row in strong_buys:
            s = get_field(row, "sector") or "Unknown"
            sector_counts[s] = sector_counts.get(s, 0) + 1
        top_sector = max(sector_counts, key=sector_counts.get)
        top_pct = sector_counts[top_sector] / len(strong_buys) * 100
        if top_pct > 40:
            observations.append(
                f"<strong>Sector concentration:</strong> {safe_entities(top_sector)} accounts for "
                f"{top_pct:.0f}% of Strong Buys ({sector_counts[top_sector]} of {len(strong_buys)}). "
                f"Consider diversification when acting on these results."
            )

    # Near misses: stocks with total >= 38 but not strong buy
    near_misses = []
    for row in full_data:
        if is_strong_buy(row):
            continue
        a = sf(get_field(row, "part_a_score"))
        b = sf(get_field(row, "part_b_score"))
        total = sf(get_field(row, "total_score"))
        if total is not None and total >= 38:
            near_misses.append(row)

    if near_misses:
        nm_list = ", ".join(
            f"{safe_entities(get_field(r, 'ticker'))} "
            f"(A:{score_int(get_field(r, 'part_a_score'))}/B:{score_int(get_field(r, 'part_b_score'))})"
            for r in near_misses[:5]
        )
        observations.append(
            f"<strong>Near-misses (&ge;38 total, not Strong Buy):</strong> {nm_list}. "
            f"These stocks passed all gates but fell short on Part A &ge;22 or Part B &ge;19."
        )

    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">Key Observations</h3>\n'

    if observations:
        html += '<ul style="margin:0 0 24px 0;padding-left:20px">\n'
        for obs in observations:
            html += f'<li style="margin-bottom:8px;font-size:13px">{obs}</li>\n'
        html += '</ul>\n'
    else:
        html += (
            '<p style="font-size:13px;color:#555;margin-bottom:24px">'
            'No additional key observations for this run.</p>\n'
        )

    return html


def build_dq_section(unresolved_rows, tech_fail_rows, run_qa_rows):
    """Section 5 — Data Quality issues."""
    html = (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Data Quality</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #fde8c8;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
    )

    issues = []

    # Unresolved metrics summary
    if unresolved_rows:
        metric_counts = {}
        for row in unresolved_rows:
            m = get_field(row, "metric") or get_field(row, "field") or "unknown"
            metric_counts[m] = metric_counts.get(m, 0) + 1
        top_metrics = sorted(metric_counts.items(), key=lambda x: -x[1])[:5]
        top_str = "; ".join(f"{safe_entities(m)} ({c})" for m, c in top_metrics)
        issues.append(
            f'<span style="color:#c0392b;font-weight:bold">[M]</span> '
            f'{len(unresolved_rows)} unresolved metric record(s) across scored stocks. '
            f'Most common: {top_str}. Scored 0 per policy.'
        )

    # Technical failures
    if tech_fail_rows:
        issues.append(
            f'<span style="color:#c0392b;font-weight:bold">[M]</span> '
            f'{len(tech_fail_rows)} technical source failure(s) (yfinance rate limit or timeout). '
            f'These stocks received TECHNICAL_SOURCE_FAILURE status and were not scored.'
        )

    # Run QA flags from run_qa
    if run_qa_rows:
        for row in run_qa_rows:
            metric = get_field(row, "metric") or ""
            value = get_field(row, "value") or ""
            if "WARNING" in metric.upper() or "FAILURE" in metric.upper() or "ERROR" in metric.upper():
                issues.append(
                    f'<span style="color:#e67e22;font-weight:bold">[S]</span> '
                    f'Run QA flag: {safe_entities(metric)} = {safe_entities(value)}'
                )

    if issues:
        for issue in issues:
            html += f'<p style="font-size:13px;margin:0 0 8px 0">{issue}</p>\n'
    else:
        html += '<p style="font-size:13px;color:#555;margin:0">No data quality issues encountered this run.</p>\n'

    html += '</td></tr>\n</table>\n'
    return html


def build_source_section(run_qa_rows, group, gate_data):
    """Section 6 — Source data performance table + Gate 4 sector summary."""
    html = (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Source Data</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #dde4f0;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
    )

    # Gate 4 sector summary from gate_data.
    # NOTE: gate_results.csv does not carry a `sector` column for gate-excluded stocks
    # (they are excluded before the scoring fetch that resolves sector), so sectors are
    # frequently unavailable here. Only render the per-sector table for stocks whose
    # sector actually resolved, and never flag a concentration warning on the "Unknown"
    # bucket — otherwise an all-Unknown distribution falsely renders "100% concentration".
    gate4_exclusions = [r for r in gate_data if get_field(r, "gate_code") in ("Gate 4", "GATE4")]
    if gate4_exclusions:
        total_g4 = len(gate4_exclusions)
        known_counts = {}
        unknown = 0
        for row in gate4_exclusions:
            s = get_field(row, "sector")
            if s:
                known_counts[s] = known_counts.get(s, 0) + 1
            else:
                unknown += 1

        if not known_counts:
            # No sector data available in inputs — state the count, point to the workbook,
            # and do NOT emit a misleading sector table or concentration warning.
            html += (
                f'<p style="font-size:13px;margin:0 0 10px 0"><strong>Gate 4 eliminations:</strong> '
                f'{total_g4}. Per-sector breakdown not available in the email inputs '
                f'(gate results carry no sector field); see the Excel DIAGNOSTICS tab for the '
                f'sector-stratified Gate 4 summary.</p>\n'
            )
        else:
            sorted_sectors = sorted(known_counts.items(), key=lambda x: -x[1])
            note = ""
            if unknown:
                note = (f' <span style="color:#777">({unknown} of {total_g4} had no '
                        f'resolved sector and are omitted below)</span>')
            html += (
                f'<p style="font-size:13px;margin:0 0 10px 0"><strong>Gate 4 Sector Distribution '
                f'({total_g4} eliminations):</strong>{note}</p>\n'
                '<table style="width:60%;border-collapse:collapse;margin-bottom:12px" '
                'cellpadding="0" cellspacing="0" border="0">\n'
                '<tr style="background:#1a3a6b;color:#fff">'
                '<th style="padding:5px 8px;text-align:left;font-size:11px">Sector</th>'
                '<th style="padding:5px 8px;text-align:center;font-size:11px">Count</th>'
                '<th style="padding:5px 8px;text-align:center;font-size:11px">%</th>'
                '</tr>\n'
            )
            for i, (sector, count) in enumerate(sorted_sectors[:8]):
                bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
                pct_val = count / total_g4 * 100
                # Flag concentration only on a genuinely named sector exceeding 35%.
                concentration_flag = ' <span style="color:#c0392b">&nbsp;&#9888; concentration</span>' if pct_val > 35 else ""
                html += (
                    f'<tr style="background:{bg}">'
                    f'<td style="padding:4px 8px;font-size:12px">{safe_entities(sector)}</td>'
                    f'<td style="padding:4px 8px;font-size:12px;text-align:center">{count}</td>'
                    f'<td style="padding:4px 8px;font-size:12px;text-align:center">'
                    f'{pct_val:.1f}%{concentration_flag}</td>'
                    f'</tr>\n'
                )
            html += '</table>\n'

    # Source performance from run_qa
    source_rows = []
    for row in run_qa_rows:
        metric = get_field(row, "metric") or ""
        if "source" in metric.lower() or "coverage" in metric.lower() or "rate" in metric.lower():
            source_rows.append(row)

    if source_rows:
        html += (
            '<p style="font-size:13px;margin:0 0 8px 0"><strong>Source Performance:</strong></p>\n'
            '<table style="width:100%;border-collapse:collapse;margin-bottom:8px" '
            'cellpadding="0" cellspacing="0" border="0">\n'
            '<tr style="background:#1a3a6b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left;font-size:11px">Metric</th>'
            '<th style="padding:6px 8px;text-align:left;font-size:11px">Value</th>'
            '</tr>\n'
        )
        for i, row in enumerate(source_rows[:10]):
            bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
            html += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:4px 8px;font-size:12px">{safe_entities(get_field(row, "metric"))}</td>'
                f'<td style="padding:4px 8px;font-size:12px">{safe_entities(get_field(row, "value"))}</td>'
                f'</tr>\n'
            )
        html += '</table>\n'
    else:
        html += (
            '<p style="font-size:13px;color:#555;margin:0">'
            f'Source performance data for {safe_entities(group)} run. '
            f'See {safe_entities(group)}_run_qa.csv in Investment Analysis folder for full detail.</p>\n'
        )

    html += '</td></tr>\n</table>\n'
    return html


def build_retrospective_section(retro_path, group, run_date):
    """Section 7 — Retrospective summary from .md file."""
    html = (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Retrospective</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #dde4f0;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
    )

    retro_filename = ""
    if retro_path:
        retro_filename = os.path.basename(retro_path)

    if retro_filename:
        html += (
            f'<p style="color:#555;font-style:italic;margin:0 0 12px 0">'
            f'File saved: Investment Analysis/{safe_entities(retro_filename)}</p>\n'
        )

    # Parse retrospective items from .md file
    items = []
    if retro_path and os.path.exists(retro_path):
        with open(retro_path, encoding="utf-8") as f:
            content = f.read()

        # Extract recommendations section
        reco_match = re.search(
            r'## Recommendations?(.*?)(?=^##|\Z)',
            content, re.DOTALL | re.MULTILINE
        )
        if reco_match:
            reco_text = reco_match.group(1).strip()
            # Extract numbered items
            item_matches = re.findall(r'\d+\.\s+\*\*(.*?)\*\*[:\s]+(.*?)(?=\n\d+\.|\Z)', reco_text, re.DOTALL)
            for title, body in item_matches[:5]:
                items.append((title.strip(), body.strip()))

        # Fallback: extract any issues section
        if not items:
            issues_match = re.search(
                r'## (?:Issues|Data Quality Issues|Execution Notes)(.*?)(?=^##|\Z)',
                content, re.DOTALL | re.MULTILINE
            )
            if issues_match:
                issues_text = issues_match.group(1).strip()
                issue_matches = re.findall(
                    r'###\s+\[?\w+\]?\s*(.*?)\n(.*?)(?=###|\Z)',
                    issues_text, re.DOTALL
                )
                for title, body in issue_matches[:5]:
                    items.append((title.strip(), body.strip()[:200]))

    if items:
        for i, (title, body) in enumerate(items, 1):
            html += (
                f'<p style="font-size:13px;margin:0 0 8px 0">'
                f'<strong>Item {i}:</strong> '
                f'<strong>{safe_entities(title)}:</strong> '
                f'{safe_entities(body[:300])}'
                f'</p>\n'
            )
    else:
        html += (
            f'<p style="font-size:13px;color:#555;margin:0">'
            f'Retrospective file written for {safe_entities(group)} run ({safe_entities(run_date)}). '
            f'See file for full detail: {safe_entities(retro_filename or "not found")}.'
            f'</p>\n'
        )

    html += '</td></tr>\n</table>\n'
    return html


def build_footer(group, run_date):
    """Footer — required on every email."""
    return (
        f'<p style="font-size:11px;color:#999;border-top:1px solid #eee;padding-top:12px">'
        f'Generated by ISA Growth Stock Analysis &mdash; {safe_entities(run_date)} &mdash; '
        f'{safe_entities(group)} | claude-sonnet-4-6<br>'
        f'Not investment advice. Verify against primary sources before acting.'
        f'</p>\n'
    )


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------
def build_email_body(
    group, run_date, full_data, gate_data,
    retro_path=None, run_qa_rows=None, unresolved_rows=None, tech_fail_rows=None
):
    """Assemble the complete HTML email body string."""
    run_qa_rows = run_qa_rows or []
    unresolved_rows = unresolved_rows or []
    tech_fail_rows = tech_fail_rows or []

    # Classify stocks
    # S5: headline ranking MIRRORS the Excel SUMMARY — count-based, forward-led Source Score.
    _sb_all = [r for r in full_data if is_strong_buy(r)]  # informational Strong-Buy set (badge secondary under S5)
    if _cfg_get("SUMMARY_COUNT_BASED", False):
        _flr = _cfg_get("SUMMARY_SOURCE_FLOOR", 0.0)
        strong_buys = sorted([r for r in full_data if _summary_eligible(r) and _source_score(r) * 100 >= _flr],
                             key=lambda r: -_source_score(r))[:int(_cfg_get("SUMMARY_TARGET_COUNT", 30))]
    else:
        strong_buys = sorted(_sb_all, key=lambda r: -(sf(get_field(r, "total_score")) or 0))
    gate_passers = [r for r in full_data if is_gate_passer(r)]
    counts = get_coverage_counts(full_data, gate_data)

    # Outer container
    html_parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;'
        'max-width:900px;margin:0 auto;padding:20px">\n',
        # Main heading
        f'<h2 style="color:#1a3a6b;border-bottom:2px solid #1a3a6b;padding-bottom:8px;margin-bottom:16px">'
        f'ISA Growth Stock Analysis &mdash; {safe_entities(group)} | {safe_entities(run_date)}'
        f'</h2>\n',
        # Section 1 — Coverage stats (FIRST in body)
        build_coverage_line(counts, group, run_date),
        # KPI tiles (immediately after coverage stats)
        build_kpi_tiles(strong_buys, gate_passers),
        # Section 2 — Top 10
        build_top10_table(strong_buys),
        # Section 3 — Top 3
        build_top3_picks(strong_buys),
        # Section 4 — Key Observations
        build_key_observations(strong_buys, full_data, group),
        # Section 5 — DQ
        build_dq_section(unresolved_rows, tech_fail_rows, run_qa_rows),
        # Section 6 — Source Data
        build_source_section(run_qa_rows, group, gate_data),
        # Section 7 — Retrospective
        build_retrospective_section(retro_path, group, run_date),
        # Footer
        build_footer(group, run_date),
        '</div>\n',
    ]

    return "".join(html_parts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Build ISA Growth Stock Analysis HTML email body."
    )
    parser.add_argument("--group", required=True,
                        help="Group label e.g. NASDAQ, SP500, STOXX600")
    parser.add_argument("--run_date", required=True,
                        help="Human-readable run date e.g. '22-May-26'")
    parser.add_argument("--full_data", required=True,
                        help="Path to {YYYYMMDD}_{GROUP}_full_data.csv")
    parser.add_argument("--gates", required=True,
                        help="Path to {YYYYMMDD}_{GROUP}_yf_gate_results.csv")
    parser.add_argument("--output", required=True,
                        help="Output path for HTML body file e.g. email_body.html")
    parser.add_argument("--retrospective", default=None,
                        help="Path to {YYYYMMDD}_{GROUP}_retrospective.md")
    parser.add_argument("--run_qa", default=None,
                        help="Path to {YYYYMMDD}_{GROUP}_run_qa.csv")
    parser.add_argument("--unresolved", default=None,
                        help="Path to {YYYYMMDD}_{GROUP}_unresolved_metrics.csv")
    parser.add_argument("--tech_fails", default=None,
                        help="Path to {YYYYMMDD}_{GROUP}_technical_failures.csv")
    parser.add_argument("--constituent", default=None,
                        help="Path to {YYYYMMDD}_{GROUP}_constituent_master.csv (optional)")

    args = parser.parse_args()

    # Load data
    print(f"Loading full_data: {args.full_data}")
    full_data = load_csv(args.full_data)
    print(f"  Loaded {len(full_data)} rows")

    print(f"Loading gate results: {args.gates}")
    gate_data = load_csv(args.gates)
    print(f"  Loaded {len(gate_data)} rows")

    run_qa_rows      = load_csv(args.run_qa)      if args.run_qa      else []
    unresolved_rows  = load_csv(args.unresolved)  if args.unresolved  else []
    tech_fail_rows   = load_csv(args.tech_fails)  if args.tech_fails  else []

    html_body = build_email_body(
        group        = args.group,
        run_date     = args.run_date,
        full_data    = full_data,
        gate_data    = gate_data,
        retro_path   = args.retrospective,
        run_qa_rows  = run_qa_rows,
        unresolved_rows = unresolved_rows,
        tech_fail_rows  = tech_fail_rows,
    )

    with open(args.output, "w", encoding="ascii", errors="xmlcharrefreplace") as fh:
        fh.write(html_body)

    print(f"[build_email] Saved: {args.output}")
    print(f"[build_email] REMINDER: pass body with is_html=true in GMAIL_SEND_EMAIL")


if __name__ == "__main__":
    main()
