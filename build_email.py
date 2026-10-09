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
    "target_price_mean": "target_mean",   # P2.1 (18-Jul-26): actual full_data column name
    "target_mean_price": "target_mean",
    "Target (mean)": "target_mean",
    "AnalystRating": "analyst_rating",
    "analyst_rating": "analyst_rating",
    "recommendationKey": "analyst_rating",
    "AnalystCount": "analyst_count",
    "num_analysts": "analyst_count",      # P2.1 (18-Jul-26): actual full_data column name
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
    """Jul-26: the 'Strong Buy' badge is now FORWARD-LED — SUMMARY-eligible AND Source Score at/above
    the floor (retires the legacy fixed Part-A / Part-B quality-badge gate). Falls back to status when the
    scored fields are absent."""
    a = sf(get_field(row, "part_a_score"))
    b = sf(get_field(row, "part_b_score"))
    status = get_field(row, "final_status")
    if a is not None and b is not None:
        try:
            return _summary_eligible(row) and _source_score(row) * 100 >= _cfg_get("SUMMARY_SOURCE_FLOOR", 70.0)
        except Exception:
            return status in STRONG_BUY_STATUSES
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


def get_coverage_counts(full_data, gate_data, unresolved_rows=None):
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

        # Strong Buy is now forward-led (is_strong_buy = SUMMARY-eligible + Source >= floor), matching the
        # SUMMARY tab / KPI tile. Check it FIRST so a qualifying stock
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
                _a_strong = _cfg_get("GROWTH_PART_A_STRONG", 22)
                _a_accept = _cfg_get("GROWTH_PART_A_ACCEPTABLE", 14)
                if a >= _a_strong:
                    counts["fair_mixed"] += 1
                elif a >= _a_accept:
                    counts["acceptable"] += 1

    # Review item 9 (18-Jul-26): GATE_DATA_UNRESOLVED rows live in the separate unresolved
    # CSV, not full_data — the funnel printed "0 Insufficient Data" while the retro logged 17.
    full_tickers_pre = {get_field(r, "ticker") for r in full_data}
    for row in (unresolved_rows or []):
        t = get_field(row, "ticker")
        if t and t not in full_tickers_pre:
            counts["insufficient_data"] += 1

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
def _cfg_get(name, default):
    try:
        import scoring_config as _c; return getattr(_c, name, default)
    except Exception:
        return default

import source_score as _ss  # Jul-26 Part 1: THE single Source Score + eligibility

def _source_score(row):
    """Forward-led screen Source Score (0-1) — thin wrapper over source_score (the single source of
    truth). build_email historically works in the 0-1 space, so divide the 0-100 canonical by 100."""
    return _ss.source_score_for_row(row, get=lambda r, k: get_field(r, k)) / 100.0

def _summary_eligible(row):
    """SUMMARY viability eligibility — thin wrapper over source_score.summary_eligible (single source)."""
    return _ss.summary_eligible(row, get=lambda r, k: get_field(r, k))


def _capital_upside(row):
    """Fix Pack A6/D7 (12-Jul-26): THE upside capital logic reads is implied_upside_fv
    (FV-composite basis). Consensus-target gap (upside_pct/target_upside) is DISPLAY-ONLY —
    kept here solely as a fallback for pre-Fix-Pack CSVs (June-era key shim, regression R3).
    Returns (value_fraction | None, basis_label)."""
    v = sf(get_field(row, "implied_upside_fv"))
    if v is not None:
        return v, "fv"
    # P2.1 (18-Jul-26): recompute via THE shared FV composite when the stamped column is blank
    # (e.g. a local-path run that missed the fixpack stamp) — the SAME recipe build_excel uses,
    # so email and Excel cannot desync. Only if the composite also has no answer do we fall
    # back to the display-only consensus gap (D7: kept solely for pre-Fix-Pack CSVs).
    try:
        import fv_composite as _fvc
        v = sf(_fvc.fv_composite_for_row(row, get=lambda r, k: get_field(r, k)).get("implied_upside_fv"))
        if v is not None:
            return v, "fv"
    except Exception:
        pass
    return sf(get_field(row, "upside_pct")), "target(display)"

def build_drawdown_line():
    """Review item 9 (18-Jul-26): B1 drawdown-ladder standing line, rendered MECHANICALLY from
    drawdown_state.json so every weekly email carries it (agent-prose insertion desynced:
    present in the 17-Jul MIDCAP email, missing from the 18-Jul SP500 email). Mirrors
    email_prefill's monthly Section-2 line; B7 regime shown once classifier has run."""
    try:
        import json as _j, os as _o
        _p = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "drawdown_state.json")
        ds = _j.load(open(_p)) or {}
    except Exception:
        ds = {}
    if ds.get("last_check"):
        txt = (f"Drawdown ladder (B1): {ds.get('drawdown_pct', 0):+.1f}% from 252d high | "
               f"tranches fired: {ds.get('tranches_fired', 0)} | "
               f"regime: {ds.get('regime_state') or 'n/a'} | as of {ds.get('last_check')}")
    else:
        txt = "Drawdown ladder (B1): state not yet seeded — first monitor run populates"
    return (f'<p style="background:#faf6ef;padding:8px 14px;border-left:4px solid #b8860b;'
            f'font-size:12px;margin-bottom:16px">{safe_entities(txt)}</p>\n')


_HELD_CACHE = None


def _held_tickers():
    """Review item 4 (18-Jul-26): current stock-sleeve holdings from the latest
    portfolio_data_*.json next to this script — flags top-10 names Raj already owns."""
    global _HELD_CACHE
    if _HELD_CACHE is not None:
        return _HELD_CACHE
    _HELD_CACHE = set()
    try:
        import glob as _g, json as _j, os as _o
        _d = _o.path.dirname(_o.path.abspath(__file__))
        import month_artefacts as _MA                      # ISA-0832: archive-aware
        _files = sorted(_MA.month_glob(_d, "portfolio_data_*.json"),
                        key=_o.path.getmtime, reverse=True)
        if _files:
            _pd = _j.load(open(_files[0]))
            _st = _pd.get("stocks")
            if isinstance(_st, dict):
                _HELD_CACHE = {str(k).upper() for k in _st}
            elif isinstance(_st, list):
                _HELD_CACHE = {str(x.get("ticker") if isinstance(x, dict) else x).upper()
                               for x in _st}
    except Exception:
        _HELD_CACHE = set()
    return _HELD_CACHE


def _currency_sym(row):
    """Infer currency symbol from ticker suffix or currency field."""
    ticker = get_field(row, "ticker", default="")
    currency = get_field(row, "currency", default="")
    # ISA-0763 (26-Sep-2026): the EVIDENCED currency decides first; suffix inference is only a
    # fallback for a row with no currency (a CHF .SW listing rendered as EUR before this).
    _cur_ev = str(currency or "").strip().upper()
    if _cur_ev and _cur_ev not in ("N/A", "NAN", "NONE", "UNKNOWN"):
        return {"GBP": "&pound;", "GBX": "&pound;", "EUR": "&euro;", "USD": "$"}.get(
            _cur_ev, _cur_ev + " ") if currency != "GBp" else "&pound;"
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
    # ISA-0582 (26-Sep-2026): the 26-Sep STOXX600 email printed ISS.CO (DKK) as "$293.20". "$" is
    # rendered only for an EVIDENCED USD quote; another known currency renders its ISO code; a
    # missing currency renders no symbol rather than a USD assertion.
    cur = str(currency or "").strip().upper()
    if cur == "USD":
        return "$"
    if cur and cur not in ("N/A", "NAN", "NONE", "UNKNOWN"):
        return cur + " "
    return ""


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

    # Fix Pack A1 (12-Jul-26): SUMMARY floor-based count + thin-tape warning (from run_qa)
    _sq = {str(get_field(r, "metric") or "").lower(): get_field(r, "value") for r in run_qa_rows}
    if _sq.get("summary_count") not in (None, ""):
        _thin = str(_sq.get("summary_thin_warning", "")).strip().lower() in ("true", "1", "yes")
        _warn_html = (' <span style="color:#c0392b;font-weight:bold">&#9888; SUMMARY_THIN_WARNING — '
                      'thin tape, floor not met by enough names</span>' if _thin else "")
        html += (f'<p style="font-size:13px;margin:0 0 10px 0"><strong>SUMMARY selection '
                 f'(floor-based, A1):</strong> {safe_entities(_sq.get("summary_count"))} rows at/above '
                 f'screen_source floor {safe_entities(_sq.get("summary_floor", ""))}'
                 f' (eligible {safe_entities(_sq.get("summary_eligible_count", "?"))}, '
                 f'cap {safe_entities(_sq.get("summary_cap", ""))}).{_warn_html}</p>\n')

    # Source performance from run_qa
    source_rows = []
    for row in run_qa_rows:
        metric = get_field(row, "metric") or ""
        if ("source" in metric.lower() or "coverage" in metric.lower() or "rate" in metric.lower()
                or "summary" in metric.lower()):
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


def build_footer(group, run_date):
    """Footer — required on every email."""
    return (
        f'<p style="font-size:11px;color:#999;border-top:1px solid #eee;padding-top:12px">'
        f'Generated by ISA Growth Stock Analysis &mdash; {safe_entities(run_date)} &mdash; '
        f'{safe_entities(group)} | claude-sonnet-4-6<br>'
        f'Not investment advice. Verify against primary sources before acting.'
        f'</p>\n'
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0762 / ISA-0755 (26-Sep-2026) — THE INSTITUTIONAL GROWTH-SCREEN EMAIL
# ═══════════════════════════════════════════════════════════════════════════════════════════
# The weekly email is a TRIAGE / DISCOVERY product feeding the monthly capital process. It answers:
# what did the screen discover, why did these names rank, what do they do, what changed, what is the
# forward E[r] and its margin to the authorised hurdle, what is the main contrary evidence, what is
# merely screen-eligible versus capital-authorised, what failed and why, and how far to trust the run.
# ⚑ It is a RENDERER (R20.2): completion comes from the screen_completion receipt, findings from the
#   retrospective findings ledger, E[r] from expected_return, the hurdle from isa_policy, eligibility
#   from t1_gates. It computes no decision of its own and grants no capital authority.
# ⚑ Vocabulary: SCREEN_ELIGIBLE / SCREEN_BLOCKED(...) - never "Actionable", "Strong Buy" or "Top Picks".
SCREEN_ELIGIBLE = "SCREEN_ELIGIBLE"
SCREEN_BLOCKED = "SCREEN_BLOCKED"
NOT_CAPITAL_AUTHORITY = ("Screen eligibility is not capital authority: capital is decided only by the "
                         "monthly review's underwriting, sizing and routing.")
def _h(text):
    """HTML-escape (<, >, &) THEN apply the email entity table - labels such as 'E[r]<15.7' are
    data, not markup."""
    import html as _html
    return safe_entities(_html.escape(str(text if text is not None else ""), quote=False))


STATE_COLOURS = {"COMPLETE": "#1a6b2a", "DEGRADED": "#b8860b", "INCOMPLETE": "#c0392b",
                 "FAILED": "#8b0000", "NO_RECEIPT": "#8b0000"}


def _hurdle():
    """The authorised hurdle = isa_policy.derived('ER_DEPLOY_FLOOR') (one home; ISA-0432)."""
    try:
        import isa_policy as _ip
        return float(_ip.derived("ER_DEPLOY_FLOOR"))
    except Exception:
        return None


def _er(row):
    """(E[r] % pa | None, unmeasured: bool) - stamped value first, else the SAME recompute the
    screen uses (D-24 §1.2); never a silently different number."""
    v = sf(get_field(row, "expected_return_12_24m"))
    unmeas = str(get_field(row, "er_status") or "") == "unmeasured"
    if v is None:
        try:
            import expected_return as _erm
            _tbl = _erm.load_anchor_table(required=False)
            _rec = _erm.expected_return_for_row(row, get=lambda r, k: get_field(r, k),
                                                anchor_table=_tbl, allow_missing_anchor_table=True)
            v = sf(_rec.get("expected_return_12_24m"))
            unmeas = _rec.get("er_status") == "unmeasured"
        except Exception:
            v = None
    return v, unmeas


def screen_status(row):
    """-> (label, reasons). SCREEN_ELIGIBLE only when t1_gates' screen-row gates all pass."""
    try:
        import t1_gates as _t1
        lab, reasons = _t1.gate_status_for_screen_row(row, get=lambda r, k: get_field(r, k))
    except Exception as exc:
        return "%s(gate unavailable)" % SCREEN_BLOCKED, ["gate unavailable: %s" % type(exc).__name__]
    if lab.startswith("PASS"):
        return SCREEN_ELIGIBLE + lab[4:], []
    return SCREEN_BLOCKED + lab[len("BLOCKED"):] if lab.startswith("BLOCKED") else SCREEN_BLOCKED, reasons


_WL_CACHE = None


def _watchlist_tickers():
    global _WL_CACHE
    if _WL_CACHE is None:
        _WL_CACHE = set()
        try:
            import json as _j
            _d = _j.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "watchlist_tickers.json"), encoding="utf-8"))
            for k in ("watchlist", "candidate_pool"):
                for e in (_d.get(k) or []):
                    if isinstance(e, dict) and e.get("ticker"):
                        _WL_CACHE.add(str(e["ticker"]).upper())
        except Exception:
            _WL_CACHE = set()
    return _WL_CACHE


def book_state(row):
    t = str(get_field(row, "ticker") or "").upper()
    if t in _held_tickers():
        return "HELD"
    if t in _watchlist_tickers():
        return "WATCHLIST"
    return "NEW"


def business_line(row):
    txt = get_field(row, "business_summary")
    return txt if txt else "DESCRIPTION_UNAVAILABLE"


def key_driver(row):
    """Up to two of the strongest ALREADY-COMPUTED forward drivers (no new scoring)."""
    out = []
    fwd = sf(get_field(row, "forward_axis_score"))
    if fwd is not None and fwd >= 70:
        out.append("forward axis %d/100" % round(fwd))
    stage = str(get_field(row, "revision_stage") or "")
    if stage in ("Accelerating", "Sustained"):
        out.append("revisions %s" % stage.lower())
    d = str(get_field(row, "est_rev_direction") or "").lower()
    if d in ("improving", "up", "rising"):
        out.append("estimates rising")
    up, basis = _capital_upside(row)
    if up is not None and up > 0 and basis == "fv":
        out.append("FV upside %+.0f%%" % (up * 100))
    if str(get_field(row, "momentum_state") or "") == "PX_ADVANCING":
        out.append("price advancing")
    return "; ".join(out[:2]) or "no dominant forward driver"


def key_concern(row, reasons=None):
    """The largest contrary signal, from computed fields and the screen gate's own reasons."""
    out = []
    for r in (reasons or []):
        out.append({"stage": "revision stage blocks", "late-cycle": "late-cycle valuation",
                    "gate-fail": "hard/mandatory gate fail", "E[r] missing": "E[r] missing",
                    "E[r] partial": "E[r] partially measured"}.get(r, r))
    d = str(get_field(row, "est_rev_direction") or "").lower()
    if d in ("deteriorating", "down", "falling"):
        out.append("estimates being cut")
    ms = str(get_field(row, "momentum_state") or "")
    if ms in ("PX_DECLINING", "PX_DETERIORATING"):
        out.append("price %s" % ms[3:].lower())
    nd = sf(get_field(row, "nd_ebitda"))
    if nd is not None and nd > 3:
        out.append("ND/EBITDA %.1fx" % nd)
    up, basis = _capital_upside(row)
    if up is not None and up < 0:
        out.append("FV implies %+.0f%%" % (up * 100))
    return "; ".join(dict.fromkeys(out).keys()) if out else "none flagged by the screen"


def signal_consistency(row, er=None, hurdle=None):
    """Typed diagnostic - never a blended composite. Sign-based, no invented magnitude:
    CONFLICTED  E[r] clears the hurdle while the FV composite says the price is ABOVE fair value,
                or E[r] is negative while the FV composite says it is BELOW fair value;
    REVIEW_REQUIRED  E[r] or the FV composite is missing / partially measured;
    CONSISTENT  otherwise."""
    if er is None:
        er, unmeas = _er(row)
    else:
        unmeas = str(get_field(row, "er_status") or "") == "unmeasured"
    hurdle = _hurdle() if hurdle is None else hurdle
    up, basis = _capital_upside(row)
    if er is None or unmeas or up is None or basis != "fv" or hurdle is None:
        return "REVIEW_REQUIRED", ("E[r] %s, FV %s" % ("partial" if unmeas else ("missing" if er is None else "ok"),
                                                       "missing" if up is None or basis != "fv" else "ok"))
    if er >= hurdle and up < 0:
        return "CONFLICTED", "E[r] %.1f%% clears hurdle %.1f%% but FV composite implies %+.0f%%" % (er, hurdle, up * 100)
    if er < 0 and up > 0:
        return "CONFLICTED", "E[r] %.1f%% negative but FV composite implies %+.0f%%" % (er, up * 100)
    return "CONSISTENT", "E[r] and FV composite agree in direction"


def change_info(rows, group, run_date_iso, panel_path=None):
    """{ticker: {state, prior, delta, rank_delta}} vs the prior run of the SAME group - only when
    both runs carry a comparable score-definition identity (ISA-0619). Otherwise INCOMPARABLE,
    never a fabricated delta. Diagnostic only: no persistence gate is (re)created here."""
    out = {str(get_field(r, "ticker")).upper(): {"state": "INCOMPARABLE"} for r in rows}
    try:
        import csv as _csv
        import score_definition as _sd
        p = panel_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_panel.csv")
        runs = {}
        with open(p, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                if str(r.get("group", "")).upper() != str(group).upper():
                    continue
                runs.setdefault(r["run_date"][:10], []).append(r)
        cur = runs.get(run_date_iso) or []
        prior_dates = sorted(d for d in runs if d < run_date_iso)
        if not cur or not prior_dates:
            return out
        prev = runs[prior_dates[-1]]
        h_cur = {x.get("score_definition_hash") for x in cur} - {""}
        h_prev = {x.get("score_definition_hash") for x in prev} - {""}
        if len(h_cur) != 1 or len(h_prev) != 1 or not _sd.compatible(next(iter(h_cur)), next(iter(h_prev))):
            return out
        def _rank(rs):
            srt = sorted(rs, key=lambda x: -(sf(x.get("source_score")) or -1))
            return {x["ticker"].upper(): (i + 1, sf(x.get("source_score"))) for i, x in enumerate(srt)}
        rc, rp = _rank(cur), _rank(prev)
        for t in out:
            if t not in rp:
                out[t] = {"state": "NEW_SINCE_PRIOR", "prior_run": prior_dates[-1]}
            elif t in rc and rc[t][1] is not None and rp[t][1] is not None:
                out[t] = {"state": "COMPARABLE", "prior": rp[t][1], "delta": rc[t][1] - rp[t][1],
                          "rank_delta": rp[t][0] - rc[t][0], "prior_run": prior_dates[-1]}
    except Exception:
        pass
    return out


def _chg_text(c):
    if not c or c.get("state") == "INCOMPARABLE":
        return "INCOMPARABLE"
    if c["state"] == "NEW_SINCE_PRIOR":
        return "new vs %s" % c.get("prior_run", "prior")
    return "%+.1f (rank %+d)" % (c["delta"], c["rank_delta"])


def _td(v, style=""):
    return '<td style="padding:6px 5px;border-bottom:1px solid #e8e8e8;font-size:11px;%s">%s</td>' % (style, v)


def build_executive_section(receipt, group, run_date, summary_rows, eligible, dq_counts, hurdle):
    """# Section 1 — Executive Screen Result."""
    st = (receipt or {}).get("state") or "NO_RECEIPT"
    col = STATE_COLOURS.get(st, "#8b0000")
    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">1. Executive Screen Result</h3>\n'
    if st != "COMPLETE":
        miss = ", ".join((receipt or {}).get("missing_mandatory") or []) or "-"
        anc = ", ".join((receipt or {}).get("ancillary_short") or []) or "-"
        why = (receipt or {}).get("why") or ("no completion receipt was produced for this run - the "
                                             "screen's persistence contract cannot be shown to have held")
        html += ('<p style="background:%s;color:#fff;padding:10px 14px;font-size:14px;font-weight:bold;'
                 'margin:0 0 10px 0">RUN INTEGRITY: %s &mdash; %s<br><span style="font-weight:normal;'
                 'font-size:12px">Missing mandatory: %s | Ancillary short: %s</span></p>\n'
                 % (col, _h(st), _h(why), _h(miss), _h(anc)))
    else:
        html += ('<p style="border-left:4px solid %s;background:#eef7ee;padding:8px 14px;font-size:12px;'
                 'margin:0 0 10px 0"><strong>Run integrity: COMPLETE</strong> &mdash; every mandatory '
                 'persistence component verified in the canonical stores.</p>\n' % col)
    tb = ((receipt or {}).get("trusted_build") or {}).get("build_id") or "UNKNOWN"
    route = (receipt or {}).get("route") or "UNDECLARED"
    pop = (receipt or {}).get("population") or {}
    if pop.get("state") in ("RECONCILED", "UNRECONCILED"):
        rej = "; ".join("%s %d" % (k, v) for k, v in list((pop.get("gate_rejected_by_code") or {}).items())[:5])
        by = pop.get("scored_by_status") or {}
        wf = ("Universe %d &rarr; gate-rejected %d (%s) &rarr; scored %d (rankable %d; mandatory-min fail %d; "
              "other %d) &rarr; SUMMARY %d &rarr; screen-eligible %d"
              % (pop["universe"], pop["gate_rejected"], _h(rej), pop["scored"],
                 by.get("CANDIDATE_RANKABLE", 0), by.get("MANDATORY_MINIMUM_FAIL", 0),
                 pop["scored"] - by.get("CANDIDATE_RANKABLE", 0) - by.get("MANDATORY_MINIMUM_FAIL", 0),
                 len(summary_rows), len(eligible)))
        wf_state = pop["state"]
    else:
        wf = "Population accounting UNMEASURED (no reconciled receipt): scored %d &rarr; SUMMARY %d &rarr; screen-eligible %d" % (
            dq_counts.get("scored", 0), len(summary_rows), len(eligible))
        wf_state = "UNMEASURED"
    books = {"HELD": 0, "WATCHLIST": 0, "NEW": 0}
    for r in summary_rows:
        books[book_state(r)] += 1
    top = summary_rows[0] if summary_rows else None
    if top is not None:
        er, unm = _er(top)
        mg = ("%+.1fpp vs hurdle" % (er - hurdle)) if (er is not None and hurdle is not None) else "margin n/a"
        strongest = "%s (%s) Source %d, E[r] %s, %s" % (
            get_field(top, "ticker"), get_field(top, "company") or "", round(_source_score(top) * 100),
            ("%.1f%%%s" % (er, "?" if unm else "")) if er is not None else "n/a", mg)
    else:
        strongest = "none - no SUMMARY candidate this run"
    if eligible:
        esc = ("%d screen-eligible candidate(s) &mdash; %s &mdash; go forward to the monthly capital process "
               "for underwriting. %s" % (len(eligible), ", ".join(get_field(r, "ticker") for r in eligible[:5]),
                                         NOT_CAPITAL_AUTHORITY))
    else:
        esc = "No screen-eligible candidate: nothing from this screen needs escalating to the monthly capital process."
    lines = [
        "<strong>Screen:</strong> %s | %s | route %s | Trusted Build %s" % (_h(group), _h(run_date),
                                                                          _h(route), _h(tb)),
        "<strong>Population (%s):</strong> %s" % (wf_state, wf),
        "<strong>SUMMARY book mix:</strong> %d held | %d watchlist/pool | %d new" % (books["HELD"], books["WATCHLIST"], books["NEW"]),
        "<strong>Strongest current signal:</strong> %s" % _h(strongest),
        "<strong>Authorised hurdle (ER_DEPLOY_FLOOR):</strong> %s" % (("%.1f%%" % hurdle) if hurdle is not None else "UNAVAILABLE"),
        "<strong>Data-quality exceptions:</strong> %d unresolved metric record(s), %d technical failure(s)" % (
            dq_counts.get("unresolved", 0), dq_counts.get("techfail", 0)),
        "<strong>Escalation:</strong> %s" % esc,
    ]
    html += "".join('<p style="font-size:13px;margin:0 0 5px 0">%s</p>\n' % l for l in lines)
    return html + '<div style="margin-bottom:18px"></div>\n'


def build_top10_table(strong_buys, hurdle=None, changes=None):
    """# Section 2 — Ranked Screen Candidates (the SUMMARY set, forward-led Source Score order).
    Source Score / stage / E[r] / hurdle margin are primary; Part A/B stay in the workbook only."""
    if not strong_buys:
        return ('<h3 style="color:#1a3a6b;margin-bottom:8px">2. Ranked Screen Candidates</h3>\n'
                '<p style="color:#666;font-style:italic">No SUMMARY candidates identified this run.</p>\n')
    hurdle = _hurdle() if hurdle is None else hurdle
    changes = changes or {}
    header_cols = [
        "Rank", "Ticker", "Company", "What the company does", "Source", "Stage", "E[r]",
        "Hurdle", "Margin", "Key forward driver", "Key concern / conflict", "Book",
        "Screen status", "Source chg"
    ]  # ISA-0762 (26-Sep-2026): 14 columns — contract in Run_Context ("exactly these 14")
    head = "".join('<th style="background:#1a3a6b;color:#fff;padding:6px 4px;font-size:11px;'
                   'text-align:left">%s</th>' % _h(c) for c in header_cols)
    body = ""
    for rank, row in enumerate(strong_buys[:10], 1):
        bg = "#f9f9f9" if rank % 2 == 0 else "#ffffff"
        er, unm = _er(row)
        er_s = ("%.1f%%%s" % (er, "?" if unm else "")) if er is not None else "\u2014"
        mg = (er - hurdle) if (er is not None and hurdle is not None) else None
        mg_s = ('<span style="color:%s;font-weight:bold">%+.1fpp</span>' % ("#1a6b2a" if mg >= 0 else "#c0392b", mg)
                if mg is not None else "&mdash;")
        lab, reasons = screen_status(row)
        cons, cwhy = signal_consistency(row, er=er, hurdle=hurdle)
        concern = key_concern(row, reasons)
        if cons != "CONSISTENT":
            concern = "%s: %s; %s" % (cons, cwhy, concern)
        body += ('<tr style="background:%s">' % bg
                 + _td(rank, "text-align:center")
                 + _td("<strong>%s</strong>" % _h(get_field(row, "ticker") or "N/A"))
                 + _td(_h(get_field(row, "company") or "N/A"))
                 + _td(_h(business_line(row)), "max-width:220px")
                 + _td("<strong>%d</strong>" % round(_source_score(row) * 100), "text-align:center")
                 + _td(_h(str(get_field(row, "revision_stage") or "—")))
                 + _td(_h(er_s), "text-align:center;color:#4a1a6b;font-weight:bold")
                 + _td(("%.1f%%" % hurdle) if hurdle is not None else "n/a", "text-align:center")
                 + _td(mg_s, "text-align:center")
                 + _td(_h(key_driver(row)))
                 + _td(_h(concern))
                 + _td(book_state(row), "text-align:center")
                 + _td(_h(lab), "font-weight:bold;color:%s" % ("#1a6b2a" if lab.startswith(SCREEN_ELIGIBLE) else "#c0392b"))
                 + _td(_h(_chg_text(changes.get(str(get_field(row, "ticker")).upper()))), "text-align:center")
                 + '</tr>\n')
    return ('<h3 style="color:#1a3a6b;margin-bottom:8px">2. Ranked Screen Candidates &mdash; by forward-led '
            'Source Score</h3>\n<p style="font-size:11px;color:#666;margin:0 0 6px 0">%s Part A/B diagnostics '
            'remain in the workbook.</p>\n<table style="width:100%%;border-collapse:collapse;margin-bottom:22px" '
            'cellpadding="0" cellspacing="0" border="0">\n<tr>%s</tr>\n%s</table>\n'
            % (_h(NOT_CAPITAL_AUTHORITY), head, body))


def build_candidate_briefs(summary_rows, eligible, hurdle=None):
    """# Section 3 — Top Candidates for Further Review (3 compact lines each)."""
    hurdle = _hurdle() if hurdle is None else hurdle
    pick = eligible[:5] if eligible else summary_rows[:3]
    title = ("3. Top Candidates for Further Review" if eligible else
             "3. Top Candidates for Further Review &mdash; none screen-eligible; strongest SUMMARY names shown with their blockers")
    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">%s</h3>\n' % title
    if not pick:
        return html + '<p style="font-size:13px;color:#555">No candidates this run.</p>\n'
    for row in pick:
        er, unm = _er(row)
        lab, reasons = screen_status(row)
        mg = ("%+.1fpp" % (er - hurdle)) if (er is not None and hurdle is not None) else "n/a"
        l1 = "<strong>%s &mdash; %s.</strong> %s" % (_h(get_field(row, "ticker")),
                                                      _h(get_field(row, "company") or ""),
                                                      _h(business_line(row)))
        l2 = ("<em>Forward evidence:</em> Source %d | stage %s | E[r] %s vs hurdle %s (margin %s) | drivers: %s"
              % (round(_source_score(row) * 100), _h(str(get_field(row, "revision_stage") or "—")),
                 ("%.1f%%%s" % (er, "?" if unm else "")) if er is not None else "&mdash;",
                 ("%.1f%%" % hurdle) if hurdle is not None else "n/a", mg, _h(key_driver(row))))
        cons, cwhy = signal_consistency(row, er=er, hurdle=hurdle)
        l3 = ("<em>Concern / state:</em> %s | %s (%s) | %s | %s"
              % (_h(key_concern(row, reasons)), cons, _h(cwhy), book_state(row), _h(lab)))
        html += ('<table cellpadding="10" cellspacing="0" border="0" style="width:100%%;border:1px solid #dde4f0;'
                 'margin-bottom:10px"><tr><td style="font-size:12px"><p style="margin:0 0 4px 0">%s</p>'
                 '<p style="margin:0 0 4px 0">%s</p><p style="margin:0">%s</p></td></tr></table>\n' % (l1, l2, l3))
    return html + '<div style="margin-bottom:14px"></div>\n'


def reject_candidates(full_data, summary_rows, hurdle=None, n=5):
    """The most decision-relevant rejected / conflicted names (opportunity-cost evidence)."""
    hurdle = _hurdle() if hurdle is None else hurdle
    in_sum = {str(get_field(r, "ticker")) for r in summary_rows}
    floor = _cfg_get("SUMMARY_SOURCE_FLOOR", 70.0)
    out = []
    for r in full_data:
        t = str(get_field(r, "ticker"))
        src = _source_score(r) * 100
        er, unm = _er(r)
        up, basis = _capital_upside(r)
        st = str(get_field(r, "final_status") or "").upper()
        blocker = None
        if src >= floor and er is not None and hurdle is not None and er < hurdle and not unm:
            blocker = "E[r] %.1f%% below hurdle %.1f%% despite Source %d" % (er, hurdle, round(src))
        elif er is not None and hurdle is not None and er >= hurdle and up is not None and basis == "fv" and up < 0:
            blocker = "CONFLICTED: E[r] %.1f%% clears hurdle but FV composite implies %+.0f%%" % (er, up * 100)
        elif src >= floor and (er is None or unm):
            blocker = "E[r] %s on a Source %d name" % ("missing" if er is None else "partially measured", round(src))
        elif st in ("MANDATORY_MINIMUM_FAIL", "HARD_GATE_FAIL") and (sf(get_field(r, "forward_axis_score")) or 0) >= 70:
            blocker = "%s blocks an otherwise strong forward candidate (forward %d)" % (st, round(sf(get_field(r, "forward_axis_score"))))
        if blocker and (t not in in_sum or blocker.startswith("CONFLICTED")):
            out.append((src, r, blocker))
    out.sort(key=lambda x: -x[0])
    return out[:n]


def build_rejects_section(full_data, summary_rows, hurdle=None):
    """# Section 4 — Important Rejects / Conflicts (exact blocker per name)."""
    rows = reject_candidates(full_data, summary_rows, hurdle)
    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">4. Important Rejects / Conflicts</h3>\n'
    if not rows:
        return html + '<p style="font-size:13px;color:#555;margin-bottom:20px">No decision-relevant reject or conflict this run.</p>\n'
    html += '<ul style="margin:0 0 20px 0;padding-left:18px">\n'
    for src, r, blk in rows:
        html += ('<li style="font-size:12px;margin-bottom:5px"><strong>%s</strong> (%s) &mdash; %s</li>\n'
                 % (_h(get_field(r, "ticker")), _h(get_field(r, "company") or ""), _h(blk)))
    return html + '</ul>\n'


def build_retrospective_section(run_label, receipt=None):
    """# Section 5 — Retrospective / Run Integrity: EVERY finding in the run's findings ledger
    (promoted AND observation-only) plus the completion components. Absent ledger = MISSING."""
    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">5. Retrospective / Run Integrity</h3>\n'
    try:
        import isa_retrospective_intake as _RI
        rf = _RI.run_findings(run_label) if run_label else {"state": "MISSING", "ledger": []}
    except Exception as exc:
        rf = {"state": "MISSING", "ledger": [], "why": str(exc)}
    st = rf["state"]
    if st == "MISSING":
        html += ('<p style="background:#c0392b;color:#fff;padding:8px 12px;font-size:13px">RETROSPECTIVE NOT '
                 'CAPTURED for %s: the run recorded neither findings nor an explicit clean result. This is an '
                 'incident, never "no findings" (ISA-0755).</p>\n' % _h(run_label or "this run"))
    elif st == "NO_FINDINGS_CAPTURED":
        html += '<p style="font-size:13px">Explicit clean result recorded: this run declared no findings.</p>\n'
    else:
        led = rf.get("ledger") or []
        if rf.get("legacy_count_only"):
            html += '<p style="font-size:12px;color:#8b0000">Findings count recorded without a ledger (pre-ISA-0764 capture).</p>\n'
        html += ('<table style="width:100%;border-collapse:collapse;margin-bottom:10px" cellpadding="0" '
                 'cellspacing="0" border="0"><tr>'
                 + "".join('<th style="background:#1a3a6b;color:#fff;padding:5px;font-size:11px;text-align:left">%s</th>' % c
                           for c in ("Id", "Severity", "Finding", "Disposition", "ISA item", "Rule / trigger"))
                 + '</tr>\n')
        for x in led:
            trig = x.get("rule") or ""
            if x.get("escalations"):
                trig += " [%s]" % ", ".join(x["escalations"])
            if x.get("recurrence"):
                trig += " recurs: %s" % ", ".join(x["recurrence"][-3:])
            html += ('<tr>' + _td(_h(x.get("finding_id") or "")) + _td(_h(x.get("severity") or ""))
                     + _td(_h(x.get("title") or "")) + _td(_h(x.get("disposition") or ""))
                     + _td(_h(x.get("isa_id") or "\u2014")) + _td(_h(trig)) + '</tr>\n')
        html += '</table>\n'
    comps = (receipt or {}).get("components") or {}
    if comps:
        html += '<p style="font-size:12px;margin:6px 0 2px 0"><strong>Completion receipt (%s, route %s):</strong></p>\n' % (
            _h((receipt or {}).get("state") or ""), _h((receipt or {}).get("route") or ""))
        html += '<ul style="margin:0 0 18px 0;padding-left:18px">\n'
        for k in ("workbook", "screen_history", "score_panel", "constituents", "regime", "capture_status",
                  "fallback", "retrospective", "source_performance"):
            c = comps.get(k) or {}
            html += '<li style="font-size:11px">%s: <strong>%s</strong> &mdash; %s</li>\n' % (
                k, _h(str(c.get("state"))), _h(str(c.get("why") or "")))
        html += '</ul>\n'
    else:
        html += '<p style="font-size:12px;color:#8b0000;margin-bottom:18px">No completion receipt: persistence state UNVERIFIED.</p>\n'
    return html


def email_subject(group, run_date, receipt, n_summary, n_eligible):
    """The subject is DERIVED from the receipt; it never asserts a persistence fact itself.
    ('RETRO SAVED' is retired - ISA-0755.)"""
    st = (receipt or {}).get("state") or "NO_RECEIPT"
    pre = "" if st == "COMPLETE" else "[INCIDENT: %s] " % st
    return ("%sISA Growth Screen - %s | %s | %d SUMMARY, %d screen-eligible | run %s"
            % (pre, group, run_date, n_summary, n_eligible, st))


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------
def build_email_body(
    group, run_date, full_data, gate_data,
    retro_path=None, run_qa_rows=None, unresolved_rows=None, tech_fail_rows=None,
    receipt=None, run_label=None, run_date_iso=None
):
    """Assemble the complete HTML email body string (ISA-0762 layout, 6 sections).
    `retro_path` is accepted for call-compatibility and IGNORED: findings are rendered from the
    durable findings ledger (ISA-0755/0764), never from a transient file."""
    del retro_path
    run_qa_rows = run_qa_rows or []
    unresolved_rows = unresolved_rows or []
    tech_fail_rows = tech_fail_rows or []
    # S5: the ranked set MIRRORS the Excel SUMMARY — count-based, forward-led Source Score.
    if _cfg_get("SUMMARY_COUNT_BASED", False):
        _sel, _sqa = _ss.select_summary(full_data, get=lambda r, k: get_field(r, k))
        summary_rows = [r for r, _sc in _sel]
    else:
        summary_rows = sorted([r for r in full_data if is_strong_buy(r)],
                              key=lambda r: -(_source_score(r)))
    eligible = [r for r in summary_rows if screen_status(r)[0].startswith(SCREEN_ELIGIBLE)]
    hurdle = _hurdle()
    changes = change_info(summary_rows, group, run_date_iso) if run_date_iso else {}
    dq_counts = {"unresolved": len(unresolved_rows), "techfail": len(tech_fail_rows), "scored": len(full_data)}
    html_parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;'
        'max-width:980px;margin:0 auto;padding:20px">\n',
        f'<h2 style="color:#1a3a6b;border-bottom:2px solid #1a3a6b;padding-bottom:8px;margin-bottom:16px">'
        f'ISA Growth Screen &mdash; {safe_entities(group)} | {safe_entities(run_date)}</h2>\n',
        # Section 1 — Executive Screen Result (completion state, waterfall, escalation)
        build_executive_section(receipt, group, run_date, summary_rows, eligible, dq_counts, hurdle),
        # B1 standing line (review item 9, 18-Jul-26 — mechanical, all groups)
        build_drawdown_line(),
        # Section 2 — Ranked Screen Candidates
        build_top10_table(summary_rows, hurdle=hurdle, changes=changes),
        # Section 3 — Top Candidates for Further Review
        build_candidate_briefs(summary_rows, eligible, hurdle=hurdle),
        # Section 4 — Important Rejects / Conflicts
        build_rejects_section(full_data, summary_rows, hurdle=hurdle),
        # Section 5 — Retrospective / Run Integrity
        build_retrospective_section(run_label, receipt),
        # Section 6 — Data quality and source appendix
        '<h3 style="color:#1a3a6b;margin-bottom:8px">6. Data Quality &amp; Source Appendix</h3>\n',
        build_dq_section(unresolved_rows, tech_fail_rows, run_qa_rows),
        build_source_section(run_qa_rows, group, gate_data),
        build_footer(group, run_date),
        '</div>\n',
    ]
    return "".join(html_parts), summary_rows, eligible


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
EXIT_COMPLETE, EXIT_INCIDENT, EXIT_NO_RECEIPT = 0, 2, 3


def main():
    parser = argparse.ArgumentParser(
        description="Build ISA Growth Screen HTML email body (renderer of the completion receipt)."
    )
    parser.add_argument("--group", required=True, help="Group label e.g. NASDAQ, SP500, STOXX600")
    parser.add_argument("--run_date", required=True, help="Human-readable run date e.g. '22-May-26'")
    parser.add_argument("--run_date_iso", default=None,
                        help="YYYY-MM-DD; defaults to parsing --run_date. Keys the completion receipt.")
    parser.add_argument("--full_data", required=True, help="Path to {YYYYMMDD}_{GROUP}_full_data.csv")
    parser.add_argument("--gates", required=True, help="Path to {YYYYMMDD}_{GROUP}_yf_gate_results.csv")
    parser.add_argument("--output", required=True, help="Output path for HTML body file")
    parser.add_argument("--retrospective", default=None,
                        help="DEPRECATED (ISA-0755): ignored - findings come from the durable ledger")
    parser.add_argument("--run_qa", default=None)
    parser.add_argument("--unresolved", default=None)
    parser.add_argument("--tech_fails", default=None)
    parser.add_argument("--constituent", default=None)
    args = parser.parse_args()

    iso = args.run_date_iso
    if not iso:
        for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                iso = datetime.strptime(args.run_date, fmt).date().isoformat()
                break
            except ValueError:
                continue
    receipt = None
    try:
        import screen_completion as _sc
        receipt = _sc.get(args.group, iso) if iso else None
    except Exception as exc:
        print(f"[build_email] completion receipt unreadable: {exc}")
    run_label = ("%s_%s" % (iso.replace("-", ""), args.group.upper())) if iso else None

    full_data = load_csv(args.full_data)
    gate_data = load_csv(args.gates)
    run_qa_rows = load_csv(args.run_qa) if args.run_qa else []
    unresolved_rows = load_csv(args.unresolved) if args.unresolved else []
    tech_fail_rows = load_csv(args.tech_fails) if args.tech_fails else []
    html_body, summary_rows, eligible = build_email_body(
        group=args.group, run_date=args.run_date, full_data=full_data, gate_data=gate_data,
        run_qa_rows=run_qa_rows, unresolved_rows=unresolved_rows, tech_fail_rows=tech_fail_rows,
        receipt=receipt, run_label=run_label, run_date_iso=iso)
    with open(args.output, "w", encoding="ascii", errors="xmlcharrefreplace") as fh:
        fh.write(html_body)
    subject = email_subject(args.group, args.run_date, receipt, len(summary_rows), len(eligible))
    with open(args.output + ".subject.txt", "w", encoding="ascii", errors="replace") as fh:
        fh.write(subject)
    print(f"[build_email] Saved: {args.output}")
    print(f"EMAIL_SUBJECT: {subject}")
    print(f"[build_email] REMINDER: send with the subject above and is_html=true in GMAIL_SEND_EMAIL")
    if receipt is None:
        print("NO_COMPLETION_RECEIPT: a normal success email is refused; the body built is an INCIDENT notice "
              "(run screen_completion.py --write first). Send it - suppressing it would hide the incident.")
        sys.exit(EXIT_NO_RECEIPT)
    sys.exit(EXIT_COMPLETE if receipt.get("state") == "COMPLETE" else EXIT_INCIDENT)


def _selftest() -> int:
    """ISA-0582 (26-Sep-2026): the email renders currency only from evidence."""
    n = 0
    assert _currency_sym({"ticker": "MU", "currency": "USD"}) == "$", "positive control"; n += 1
    assert _currency_sym({"ticker": "ISS.CO", "currency": "DKK"}) == "DKK ", \
        "negative control: ISS.CO DKK must not render $ (reproduced on demand from the 26-Sep STOXX600 email)"; n += 1
    assert _currency_sym({"ticker": "ZAB.WA", "currency": "PLN"}) == "PLN ", "negative control: PLN is not $"; n += 1
    assert _currency_sym({"ticker": "FRO", "currency": ""}) == "", "negative control: missing must not assert USD"; n += 1
    assert _currency_sym({"ticker": "ABI.BR", "currency": "EUR"}) == "&euro;", "positive control"; n += 1
    # ISA-0763: evidenced currency beats suffix inference
    assert _currency_sym({"ticker": "NESN.SW", "currency": "CHF"}) == "CHF ", \
        "negative control: a CHF .SW listing must not render EUR"; n += 1
    # ── ISA-0762 / ISA-0755 — the institutional layout and the receipt gate ──
    row = {"ticker": "TST", "company": "Test Co", "final_status": "CANDIDATE_RANKABLE",
           "part_a_score": 24, "part_b_score": 18, "forward_axis_score": 90, "revision_stage": "Accelerating",
           "expected_return_12_24m": 30.0, "implied_upside_fv": 0.25, "screen_source": 85,
           "val_hist_pe_premium_disc": 5.0, "business_summary": "Test Co makes widgets."}
    tbl = build_top10_table([row], hurdle=15.0)
    for col in ("Source", "E[r]", "Hurdle", "Margin", "What the company does", "Screen status"):
        assert ">%s<" % col in tbl, "primary column missing: %s" % col; n += 1
    for bad in ("Strong Buy", "Top Picks", "Top 3 Picks", "Actionable", ">Part A<", ">Part B<", ">Total<"):
        assert bad not in tbl, "negative control: misleading/legacy label in the primary table: %s" % bad; n += 1
    assert "+15.0pp" in tbl and "Test Co makes widgets." in tbl, "margin and description render"; n += 1
    assert business_line({"ticker": "X"}) == "DESCRIPTION_UNAVAILABLE", \
        "negative control: an absent description is typed UNAVAILABLE, never invented"; n += 1
    c_bad = {**row, "expected_return_12_24m": 30.0, "implied_upside_fv": -0.2}
    assert signal_consistency(c_bad, hurdle=15.0)[0] == "CONFLICTED", \
        "must-fire: E[r] above hurdle with a negative FV composite is CONFLICTED"; n += 1
    assert signal_consistency({**row, "implied_upside_fv": ""}, hurdle=15.0)[0] in ("REVIEW_REQUIRED", "CONSISTENT"), \
        "a missing FV input is never silently CONFLICTED"; n += 1
    assert signal_consistency(row, hurdle=15.0)[0] == "CONSISTENT", "positive control"; n += 1
    assert email_subject("G", "d", None, 1, 1).startswith("[INCIDENT: NO_RECEIPT]"), \
        "negative control: no receipt can never produce a normal subject"; n += 1
    assert email_subject("G", "d", {"state": "INCOMPLETE"}, 1, 1).startswith("[INCIDENT: INCOMPLETE]"); n += 1
    ok_subj = email_subject("G", "d", {"state": "COMPLETE"}, 1, 1)
    assert "INCIDENT" not in ok_subj and "RETRO SAVED" not in ok_subj, "COMPLETE subject is clean and retires RETRO SAVED"; n += 1
    ex = build_executive_section(None, "G", "d", [row], [row], {"scored": 1}, 15.0)
    assert "RUN INTEGRITY: NO_RECEIPT" in ex and NOT_CAPITAL_AUTHORITY.split(":")[0] in ex, \
        "negative control: a missing receipt is unmissable in Section 1"; n += 1
    ex2 = build_executive_section({"state": "COMPLETE", "population": {"state": "UNRECONCILED", "universe": 5,
                                   "gate_rejected": 1, "scored": 3, "scored_by_status": {}}}, "G", "d", [], [], {}, 15.0)
    assert "Population (UNRECONCILED)" in ex2, "an unreconciled waterfall is stated, not hidden"; n += 1
    # SCREEN_ELIGIBLE is a rendering label: no other module may consume it as authority
    import glob as _g
    _here = os.path.dirname(os.path.abspath(__file__))
    consumers = [os.path.basename(f) for f in _g.glob(os.path.join(_here, "*.py"))
                 if os.path.basename(f) not in ("build_email.py", "consistency_check.py")
                 and "SCREEN_ELIGIBLE" in open(f, encoding="utf-8", errors="ignore").read()]
    assert not consumers, "negative control: SCREEN_ELIGIBLE consumed outside the renderer: %s" % consumers; n += 1
    print("build_email selftest: %d assertions, 0 failed" % n)
    return n

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    main()
