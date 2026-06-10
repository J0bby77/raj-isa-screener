#!/usr/bin/env python3
"""
build_energy_email.py  --  ISA Energy Growth Stock Analysis HTML email builder
Version: 1.0  |  2026-05-31

Generates a compliant HTML email body for the ENERGY screener output.
Adapted from build_email.py with energy-specific scoring (Part A max 20,
Part B max 16, Total max 36) and energy-relevant metric columns.

Usage:
    python3 build_energy_email.py \\
        --group ENERGY \\
        --run_date "22-Jun-26" \\
        --full_data /path/YYYYMMDD_ENERGY_full_data.csv \\
        --gates /path/YYYYMMDD_ENERGY_gate_results.csv \\
        --retrospective /path/YYYYMMDD_ENERGY_retrospective.md \\
        --output /path/YYYYMMDD_ENERGY_email_body.html \\
        [--run_qa /path/YYYYMMDD_ENERGY_run_qa.csv]

Output:
    HTML file — pass contents as GMAIL_SEND_EMAIL body with is_html:true.

Email Template Rules (identical to build_email.py):
    Rule 1 -- No Unicode above U+007F
    Rule 2 -- No <head> or <style> blocks
    Rule 3 -- No flexbox / grid (table layout)
    Rule 4 -- All styles inline
    Rule 5 -- No DOCTYPE/html/head/body wrappers
    Rule 6 -- is_html: true always required
"""

import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import csv
import math
import os
import re
import sys
from datetime import datetime

# ── Status codes (must match energy_screener.py) ─────────────────────────────
STATUS_STRONG_BUY = "ENERGY_STRONG_BUY"
STATUS_WATCH      = "ENERGY_WATCH"
STATUS_ACCEPTABLE = "ENERGY_ACCEPTABLE"
STATUS_NOT_GROWTH = "ENERGY_NOT_GROWTH"
STATUS_GATE_FAIL  = "ENERGY_GATE_FAIL"
STATUS_DATA_ISSUE = "ENERGY_DATA_ISSUE"

# ── Thresholds (must match energy_screener.py) ────────────────────────────────
PART_A_STRONG_GROWTH = 14
PART_B_STRONG_BUY    = 11
PART_A_MAX           = 20
PART_B_MAX           = 16
TOTAL_MAX            = 36


# ── HTML entity safety (Rule 1) ───────────────────────────────────────────────
ENTITY_MAP = [
    ("≥", "&ge;"), ("≤", "&le;"), ("—", "&mdash;"), ("–", "&ndash;"),
    ("−", "&minus;"), ("§", "&sect;"), ("→", "&rarr;"), ("×", "&times;"),
    ("£", "&pound;"), ("€", "&euro;"), ("±", "&plusmn;"), ("≠", "&ne;"),
    ("©", "&copy;"), ("®", "&reg;"), ("'", "&rsquo;"), ("'", "&lsquo;"),
    (""", "&ldquo;"), (""", "&rdquo;"), ("…", "&hellip;"), ("°", "&deg;"),
]


def safe_entities(text):
    if not text:
        return ""
    for char, entity in ENTITY_MAP:
        text = text.replace(char, entity)
    result = []
    for c in text:
        if ord(c) > 127:
            result.append(f"&#{ord(c)};")
        else:
            result.append(c)
    return "".join(result)


def verify_entities(html):
    violations = [c for c in html if ord(c) > 127]
    if violations:
        print(f"WARNING: {len(violations)} non-ASCII characters remain.")
        return False
    return True


# ── Number formatters ─────────────────────────────────────────────────────────
def sf(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def pct_str(v):
    """Format as percentage. v may already be formatted ('15.20%') or raw float."""
    if not v or v in ("", "nan", "None"):
        return "N/A"
    s = str(v).strip()
    if s.endswith("%"):
        try:
            return f"{float(s[:-1]):.1f}%"
        except Exception:
            return s
    f = sf(v)
    if f is None:
        return "N/A"
    # If value is already in percent form (e.g. 15.2 not 0.152)
    if abs(f) > 1.5:
        return f"{f:.1f}%"
    return f"{f * 100:.1f}%"


def score_str(v):
    f = sf(v)
    if f is None:
        return "N/A"
    return str(int(round(f)))


def upside_html(v):
    s = str(v).strip() if v else ""
    if s.endswith("%"):
        try:
            f = float(s[:-1])
        except Exception:
            return "N/A"
    else:
        f_raw = sf(v)
        if f_raw is None:
            return "N/A"
        f = f_raw * 100 if abs(f_raw) <= 1.5 else f_raw
    sign = "+" if f >= 0 else ""
    colour = "#1a6b2a" if f >= 0 else "#c0392b"
    return f'<span style="color:{colour}">{sign}{f:.1f}%</span>'


def mult_str(v):
    s = str(v).strip() if v else ""
    if s.endswith("x"):
        return s
    f = sf(v)
    if f is None:
        return "N/A"
    return f"{f:.1f}x"


def rating_display(raw):
    if not raw or raw in ("N/A", "nan", "None", ""):
        return "N/A"
    return raw.replace("_", " ").title()


# ── CSV loading ───────────────────────────────────────────────────────────────
def load_csv(path):
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k: (v.strip() if v else "") for k, v in row.items()})
    return rows


def gf(row, *keys, default=""):
    """Get first non-empty value from a row by trying multiple key names."""
    for k in keys:
        v = row.get(k, "")
        if v and v not in ("nan", "None"):
            return v
    return default


# ── Classification ────────────────────────────────────────────────────────────
def is_strong_buy(row):
    status = gf(row, "final_status")
    if status == STATUS_STRONG_BUY:
        return True
    a = sf(gf(row, "part_a_score"))
    b = sf(gf(row, "part_b_score"))
    if a is not None and b is not None:
        return a >= PART_A_STRONG_GROWTH and b >= PART_B_STRONG_BUY
    return False


def is_gate_excluded(row):
    return gf(row, "final_status") in (STATUS_GATE_FAIL, STATUS_DATA_ISSUE)


def get_counts(full_data):
    total      = len(full_data)
    strong     = sum(1 for r in full_data if is_strong_buy(r))
    watch      = sum(1 for r in full_data if gf(r, "final_status") == STATUS_WATCH)
    acceptable = sum(1 for r in full_data if gf(r, "final_status") == STATUS_ACCEPTABLE)
    not_growth = sum(1 for r in full_data if gf(r, "final_status") == STATUS_NOT_GROWTH)
    gate_fail  = sum(1 for r in full_data if gf(r, "final_status") == STATUS_GATE_FAIL)
    data_issue = sum(1 for r in full_data if gf(r, "final_status") == STATUS_DATA_ISSUE)
    scored     = strong + watch + acceptable + not_growth
    return {
        "total": total, "scored": scored, "strong": strong,
        "watch": watch, "acceptable": acceptable, "not_growth": not_growth,
        "gate_fail": gate_fail, "data_issue": data_issue,
    }


# ── Section builders ──────────────────────────────────────────────────────────

def build_coverage_line(counts, run_date):
    total   = counts["total"]
    scored  = counts["scored"]
    pct_a   = f"{scored/total*100:.0f}%" if total else "N/A"
    text = (
        f"<strong style=\"color:#1a6b2a\">{counts['strong']} Strong "
        f"{'Buy' if counts['strong']==1 else 'Buys'}</strong> | "
        f"{scored} of {total} watchlist companies analysed ({pct_a}) | "
        f"{counts['watch']} Watch | {counts['acceptable']} Acceptable | "
        f"{counts['not_growth']} Not Growth | {counts['gate_fail']} Gate Fails | "
        f"{counts['data_issue']} Data Issues"
    )
    html = (
        f'<p style="background:#f0f4fa;padding:10px 14px;border-left:4px solid #1a3a6b;'
        f'font-size:13px;margin-bottom:20px">{safe_entities(text)}</p>'
    )
    # Re-inject the strong tag (safe ASCII)
    sb = counts["strong"]
    sb_label = "Strong Buy" if sb == 1 else "Strong Buys"
    html = html.replace(
        safe_entities(f"<strong style=\"color:#1a6b2a\">{sb} {sb_label}</strong>"),
        f'<strong style="color:#1a6b2a">{sb} {sb_label}</strong>'
    )
    return html


def build_kpi_tiles(strong_buys, all_scored):
    sb_count = len(strong_buys)
    scored   = len(all_scored)

    top_score = "N/A"
    top_ticker = "N/A"
    if strong_buys:
        top = strong_buys[0]
        top_score  = score_str(gf(top, "total_score")) + f"/{TOTAL_MAX}"
        top_ticker = gf(top, "ticker")

    # Avg upside of top 10
    top10 = strong_buys[:10]
    upsides = []
    for r in top10:
        v = gf(r, "upside_pct")
        if v and v.endswith("%"):
            try:
                upsides.append(float(v[:-1]))
            except Exception:
                pass
    avg_upside = f"{sum(upsides)/len(upsides):.1f}%" if upsides else "N/A"

    return (
        '<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-bottom:24px">\n'
        '<tr>\n'
        f'<td style="width:25%;padding:0 6px 0 0">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#1a6b2a;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{sb_count}</td></tr>\n'
        f'  <tr><td style="color:#c8e6c9;font-size:11px">Energy Strong Buys</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#1a3a6b;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{safe_entities(top_score)}</td></tr>\n'
        f'  <tr><td style="color:#b3c6e6;font-size:11px">Top Score ({safe_entities(top_ticker)})</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#6b3a1a;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{scored}</td></tr>\n'
        f'  <tr><td style="color:#e6c9b3;font-size:11px">Watchlist Scored</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        f'<td style="width:25%;padding:0 0 0 6px">\n'
        f'  <table cellpadding="12" cellspacing="0" border="0" style="width:100%;background:#4a1a6b;border-radius:6px;text-align:center">\n'
        f'  <tr><td style="color:#fff;font-size:28px;font-weight:bold">{safe_entities(avg_upside)}</td></tr>\n'
        f'  <tr><td style="color:#d4b3e6;font-size:11px">Avg Upside (Top 10)</td></tr>\n'
        f'  </table>\n'
        f'</td>\n'
        '</tr>\n</table>\n'
    )


def build_top10_table(strong_buys):
    """
    12-column energy Top 10 table.
    Columns: Rank | Ticker | Company | Sector | Part A | Part B | Total |
             Rev Growth | EBITDA Mgn | ROIC* | EV/EBITDA | Upside
    *ROIC not directly scored but informational — use roe as proxy if available.
    """
    if not strong_buys:
        return '<p style="color:#666;font-style:italic">No Energy Strong Buy candidates this run.</p>\n'

    headers = [
        "Rank", "Ticker", "Company", "Sector/Type",
        f"Part A\n(/{PART_A_MAX})", f"Part B\n(/{PART_B_MAX})", f"Total\n(/{TOTAL_MAX})",
        "Rev Growth", "EBITDA Mgn", "EV/EBITDA", "ND/EBITDA", "Upside"
    ]
    widths = ["4%","6%","18%","12%","5%","5%","6%","8%","9%","8%","8%","8%"]

    header_html = "".join(
        f'<th style="background:#1a3a6b;color:#fff;padding:8px 6px;text-align:center;'
        f'font-size:12px;white-space:nowrap;width:{widths[i]}">{safe_entities(col)}</th>'
        for i, col in enumerate(headers)
    )

    rows_html = ""
    for rank, row in enumerate(strong_buys[:10], 1):
        bg = "#f9f9f9" if rank % 2 == 0 else "#ffffff"
        ticker  = safe_entities(gf(row, "ticker") or "N/A")
        company = safe_entities(gf(row, "company") or "N/A")
        sector  = safe_entities(gf(row, "sector", "sub_type") or "N/A")
        a       = score_str(gf(row, "part_a_score"))
        b       = score_str(gf(row, "part_b_score"))
        total   = score_str(gf(row, "total_score"))
        rev_g   = safe_entities(pct_str(gf(row, "rev_growth_ttm")))
        ebitda_m= safe_entities(pct_str(gf(row, "ebitda_margin")))
        ev_eb   = safe_entities(mult_str(gf(row, "ev_ebitda")))
        nd_eb   = safe_entities(mult_str(gf(row, "nd_ebitda")))
        upside  = upside_html(gf(row, "upside_pct"))

        cs = f'padding:7px 6px;border-bottom:1px solid #e8e8e8;background:{bg};font-size:12px;text-align:center'
        rows_html += (
            f'<tr>'
            f'<td style="{cs}">{rank}</td>'
            f'<td style="{cs};font-weight:bold">{ticker}</td>'
            f'<td style="{cs};text-align:left">{company}</td>'
            f'<td style="{cs};text-align:left">{sector}</td>'
            f'<td style="{cs}">{a}</td>'
            f'<td style="{cs}">{b}</td>'
            f'<td style="{cs};font-weight:bold;color:#1a6b2a">{total}</td>'
            f'<td style="{cs}">{rev_g}</td>'
            f'<td style="{cs}">{ebitda_m}</td>'
            f'<td style="{cs}">{ev_eb}</td>'
            f'<td style="{cs}">{nd_eb}</td>'
            f'<td style="{cs}">{upside}</td>'
            f'</tr>\n'
        )

    return (
        f'<h3 style="color:#1a3a6b;margin-bottom:8px">Top Energy Stocks &mdash; By Total Score</h3>\n'
        '<table style="width:100%;border-collapse:collapse;margin-bottom:24px" '
        'cellpadding="0" cellspacing="0" border="0">\n'
        f'<tr>{header_html}</tr>\n'
        f'{rows_html}'
        '</table>\n'
    )


def build_top3_picks(strong_buys):
    if not strong_buys:
        return '<p style="color:#666;font-style:italic">No Energy Strong Buy candidates for Top 3 section.</p>\n'

    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">Top 3 Energy Picks</h3>\n'

    for rank, row in enumerate(strong_buys[:3], 1):
        mb = "24px" if rank == 3 else "16px"
        ticker  = safe_entities(gf(row, "ticker") or "N/A")
        company = safe_entities(gf(row, "company") or "N/A")
        sector  = safe_entities(gf(row, "sector") or "N/A")
        sub_type= safe_entities(gf(row, "sub_type") or "")
        total   = score_str(gf(row, "total_score"))
        region  = safe_entities(gf(row, "region") or "")

        price_raw  = sf(gf(row, "current_price"))
        target_raw = sf(gf(row, "target_mean"))
        analyst_c  = gf(row, "analyst_count") or "N/A"
        analyst_r  = rating_display(gf(row, "analyst_rating") or "N/A")

        price_str  = f"${price_raw:.2f}"  if price_raw  else "N/A"
        target_str = f"${target_raw:.2f}" if target_raw else "N/A"
        upside_str = upside_html(gf(row, "upside_pct"))

        notes = safe_entities(gf(row, "notes") or "")
        if not notes:
            notes = (
                f"{company} scores {total}/{TOTAL_MAX} on the energy growth scorecard. "
                f"Category: {sub_type.replace('_',' ')}. "
                f"Refer to the SUMMARY tab in the Excel workbook for full metric detail."
            )

        # Mini metrics
        rev_g    = pct_str(gf(row, "rev_growth_ttm"))
        ebitda_m = pct_str(gf(row, "ebitda_margin"))
        gross_m  = pct_str(gf(row, "gross_margin"))
        roe      = pct_str(gf(row, "roe"))
        ev_eb    = mult_str(gf(row, "ev_ebitda"))
        nd_eb    = mult_str(gf(row, "nd_ebitda"))
        fwd_pe   = gf(row, "fwd_pe") or "N/A"
        capex_i  = pct_str(gf(row, "capex_intensity"))

        html += (
            f'<table cellpadding="14" cellspacing="0" border="0" '
            f'style="width:100%;border:1px solid #c8e6c9;border-radius:6px;'
            f'margin-bottom:{mb};border-collapse:separate">\n'
            f'<tr><td>\n'
            f'<div style="margin-bottom:6px">'
            f'<strong style="font-size:16px;color:#1a3a6b">{rank}. {ticker} &mdash; {company}</strong> '
            f'<span style="background:#1a6b2a;color:#fff;padding:2px 8px;border-radius:3px;'
            f'font-size:11px;font-weight:bold">{total}/{TOTAL_MAX}</span> '
            f'<span style="color:#888;font-size:12px">&nbsp;{sector} / {sub_type.replace("_"," ")} [{region}]</span>'
            f'</div>\n'
            f'<div style="font-size:12px;color:#555;margin-bottom:10px">'
            f'Price: {safe_entities(price_str)} &nbsp;|&nbsp; '
            f'Target: {safe_entities(target_str)} &nbsp;|&nbsp; '
            f'Upside: {upside_str} &nbsp;|&nbsp; '
            f'{safe_entities(str(analyst_c))} analysts &mdash; {safe_entities(analyst_r)}'
            f'</div>\n'
            f'<p style="font-size:13px;margin:0 0 10px 0">{notes}</p>\n'
            f'<div style="font-size:11px;color:#555;border-top:1px solid #e8e8e8;padding-top:8px">'
            f'Rev Growth: <strong>{safe_entities(rev_g)}</strong> &nbsp;|&nbsp; '
            f'EBITDA Mgn: <strong>{safe_entities(ebitda_m)}</strong> &nbsp;|&nbsp; '
            f'Gross Mgn: <strong>{safe_entities(gross_m)}</strong> &nbsp;|&nbsp; '
            f'ROE: <strong>{safe_entities(roe)}</strong> &nbsp;|&nbsp; '
            f'EV/EBITDA: <strong>{safe_entities(ev_eb)}</strong> &nbsp;|&nbsp; '
            f'ND/EBITDA: <strong>{safe_entities(nd_eb)}</strong> &nbsp;|&nbsp; '
            f'Fwd PE: <strong>{safe_entities(str(fwd_pe))}</strong> &nbsp;|&nbsp; '
            f'Capex/Rev: <strong>{safe_entities(capex_i)}</strong>'
            f'</div>\n'
            f'</td></tr>\n</table>\n'
        )

    return html


def build_watch_list_section(all_rows):
    """Section 4 — Watch list and near-misses."""
    html = '<h3 style="color:#1a3a6b;margin-bottom:8px">Key Observations</h3>\n'

    watch = [r for r in all_rows if gf(r, "final_status") == STATUS_WATCH]
    acceptable = [r for r in all_rows if gf(r, "final_status") == STATUS_ACCEPTABLE]
    gate_fails = [r for r in all_rows if gf(r, "final_status") == STATUS_GATE_FAIL]

    obs = []

    if watch:
        watch_list = ", ".join(
            f"{safe_entities(gf(r,'ticker'))} "
            f"(A:{score_str(gf(r,'part_a_score'))}/B:{score_str(gf(r,'part_b_score'))})"
            for r in sorted(watch, key=lambda x: -(sf(gf(x,"total_score")) or 0))[:6]
        )
        obs.append(
            f"<strong>Watch List ({len(watch)}):</strong> {watch_list}. "
            f"These pass gates and have Strong Growth Part A (A&ge;{PART_A_STRONG_GROWTH}), "
            f"but Part B buy signal is below threshold. Monitor for entry timing."
        )

    if acceptable:
        acc_list = ", ".join(
            f"{safe_entities(gf(r,'ticker'))} (A:{score_str(gf(r,'part_a_score'))})"
            for r in acceptable[:4]
        )
        obs.append(
            f"<strong>Acceptable Growth ({len(acceptable)}):</strong> {acc_list}. "
            f"These companies pass energy gates but have Acceptable rather than Strong Growth profiles."
        )

    if gate_fails:
        fail_list = ", ".join(
            f"{safe_entities(gf(r,'ticker'))} ({safe_entities(gf(r,'gate_code'))})"
            for r in gate_fails[:5]
        )
        obs.append(
            f"<strong>Gate Exclusions ({len(gate_fails)}):</strong> {fail_list}. "
            f"These companies failed the energy hard gates (revenue scale, growth momentum, or positive EBITDA). "
            f"Review gate_results CSV for full reasons. Consider whether any are thesis-relevant near-misses."
        )

    html += '<ul style="margin:0 0 24px 0;padding-left:20px">\n' if obs else ""
    for o in obs:
        html += f'<li style="margin-bottom:8px;font-size:13px">{o}</li>\n'
    if obs:
        html += '</ul>\n'
    else:
        html += (
            '<p style="font-size:13px;color:#555;margin-bottom:24px">'
            'No additional key observations for this run.</p>\n'
        )

    return html


def build_dq_section(run_qa_rows):
    html = (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Data Quality</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #fde8c8;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
    )

    issues = []
    fetch_errors = ""
    data_issues  = ""
    for row in run_qa_rows:
        m = row.get("metric", "")
        v = row.get("value", "")
        if m == "fetch_errors" and v and v != "0":
            fetch_errors = v
        if m == "data_issues" and v and v != "0":
            data_issues = v

    if fetch_errors:
        issues.append(
            f'<span style="color:#c0392b;font-weight:bold">[M]</span> '
            f'{safe_entities(fetch_errors)} yfinance fetch error(s) this run. '
            f'Verify ticker symbols in energy_watchlist.json and retry on next run.'
        )
    if data_issues:
        issues.append(
            f'<span style="color:#e67e22;font-weight:bold">[S]</span> '
            f'{safe_entities(data_issues)} companies had insufficient data to apply energy gates.'
        )

    if issues:
        for issue in issues:
            html += f'<p style="font-size:13px;margin:0 0 8px 0">{issue}</p>\n'
    else:
        html += '<p style="font-size:13px;color:#555;margin:0">No data quality issues encountered this run.</p>\n'

    html += '</td></tr>\n</table>\n'
    return html


def build_source_section(run_date):
    return (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Source Data</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #dde4f0;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
        f'<p style="font-size:13px;margin:0 0 8px 0">'
        f'<strong>Data source:</strong> yfinance (primary for all metrics). '
        f'Energy screener does not use index constituent files &mdash; data sourced directly from yfinance '
        f'for the curated watchlist. Fallback sourcing (Finnhub, Finviz) not applied in energy runs &mdash; '
        f'watchlist is small enough that yfinance coverage is typically complete.</p>\n'
        f'<p style="font-size:13px;margin:0 0 8px 0">'
        f'<strong>Gates:</strong> Gate 1 (Revenue &gt;$50M) | Gate 2 (Revenue momentum &gt;0% TTM or &gt;8% fwd) | '
        f'Gate 3 (EBITDA positive). No gross margin gate for energy sector.</p>\n'
        f'<p style="font-size:13px;margin:0">'
        f'<strong>Thresholds:</strong> Empirically calibrated May 2026 from 25-company financial data pull. '
        f'Part A max {PART_A_MAX} | Part B max {PART_B_MAX} | Total max {PART_A_MAX + PART_B_MAX}. '
        f'Recalibrate annually or after major thesis shift.'
        f'</p>\n'
        '</td></tr>\n</table>\n'
    )


def build_retrospective_section(retro_path, run_date):
    html = (
        '<h3 style="color:#1a3a6b;margin-bottom:8px">Retrospective</h3>\n'
        '<table cellpadding="14" cellspacing="0" border="0" '
        'style="width:100%;border:1px solid #dde4f0;border-radius:6px;margin-bottom:24px">\n'
        '<tr><td>\n'
    )

    retro_filename = os.path.basename(retro_path) if retro_path else ""
    if retro_filename:
        html += (
            f'<p style="color:#555;font-style:italic;margin:0 0 12px 0">'
            f'File saved: Investment Analysis/{safe_entities(retro_filename)}</p>\n'
        )

    items = []
    if retro_path and os.path.exists(retro_path):
        with open(retro_path, encoding="utf-8") as f:
            content = f.read()
        reco_match = re.search(r'## Recommendations?(.*?)(?=^##|\Z)', content, re.DOTALL | re.MULTILINE)
        if reco_match:
            reco_text = reco_match.group(1).strip()
            item_matches = re.findall(r'\d+\.\s+\*\*(.*?)\*\*[:\s]+(.*?)(?=\n\d+\.|\Z)', reco_text, re.DOTALL)
            for title, body in item_matches[:5]:
                items.append((title.strip(), body.strip()))

    if items:
        for i, (title, body) in enumerate(items, 1):
            html += (
                f'<p style="font-size:13px;margin:0 0 8px 0">'
                f'<strong>Item {i}:</strong> '
                f'<strong>{safe_entities(title)}:</strong> '
                f'{safe_entities(body[:350])}'
                f'</p>\n'
            )
    else:
        html += (
            f'<p style="font-size:13px;color:#555;margin:0">'
            f'Retrospective written for ENERGY run ({safe_entities(run_date)}). '
            f'See {safe_entities(retro_filename or "file")} for full detail.</p>\n'
        )

    html += '</td></tr>\n</table>\n'
    return html


def build_footer(run_date):
    return (
        f'<p style="font-size:11px;color:#999;border-top:1px solid #eee;padding-top:12px">'
        f'ISA Energy Growth Stock Analysis &mdash; {safe_entities(run_date)} &mdash; ENERGY | claude-sonnet-4-6<br>'
        f'Scorecard: Part A max {PART_A_MAX} (Strong &ge;{PART_A_STRONG_GROWTH}) | '
        f'Part B max {PART_B_MAX} (Strong Buy &ge;{PART_B_STRONG_BUY}) | '
        f'Total max {TOTAL_MAX}. Thresholds calibrated May 2026.<br>'
        f'Not investment advice. Verify against primary sources before acting.'
        f'</p>\n'
    )


# ── Main assembler ────────────────────────────────────────────────────────────

def build_email_body(run_date, full_data, gate_data, retro_path=None, run_qa_rows=None):
    run_qa_rows = run_qa_rows or []

    strong_buys = sorted(
        [r for r in full_data if is_strong_buy(r)],
        key=lambda r: -(sf(gf(r, "total_score")) or 0)
    )
    all_scored = [r for r in full_data if not is_gate_excluded(r)]
    counts = get_counts(full_data)

    parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;'
        'max-width:900px;margin:0 auto;padding:20px">\n',
        f'<h2 style="color:#1a3a6b;border-bottom:2px solid #1a3a6b;padding-bottom:8px;margin-bottom:16px">'
        f'ISA Energy Growth Stock Analysis &mdash; {safe_entities(run_date)}'
        f'</h2>\n',
        # Section 1 — Coverage
        build_coverage_line(counts, run_date),
        # KPI tiles
        build_kpi_tiles(strong_buys, all_scored),
        # Section 2 — Top 10
        build_top10_table(strong_buys),
        # Section 3 — Top 3 picks
        build_top3_picks(strong_buys),
        # Section 4 — Observations
        build_watch_list_section(full_data),
        # Section 5 — DQ
        build_dq_section(run_qa_rows),
        # Section 6 — Source
        build_source_section(run_date),
        # Section 7 — Retrospective
        build_retrospective_section(retro_path, run_date),
        # Footer
        build_footer(run_date),
        '</div>\n',
    ]
    return "".join(parts)


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build ISA Energy Analysis HTML email body.")
    parser.add_argument("--group",         required=False, default="ENERGY")
    parser.add_argument("--run_date",      required=True,  help="Display date e.g. '22-Jun-26'")
    parser.add_argument("--full_data",     required=True)
    parser.add_argument("--gates",         required=True)
    parser.add_argument("--output",        required=True)
    parser.add_argument("--retrospective", default=None)
    parser.add_argument("--run_qa",        default=None)
    args = parser.parse_args()

    print(f"Loading full_data: {args.full_data}")
    full_data = load_csv(args.full_data)
    print(f"  Loaded {len(full_data)} rows")

    gate_data   = load_csv(args.gates)
    run_qa_rows = load_csv(args.run_qa) if args.run_qa else []

    print("Building energy email body...")
    body = build_email_body(
        run_date=args.run_date,
        full_data=full_data,
        gate_data=gate_data,
        retro_path=args.retrospective,
        run_qa_rows=run_qa_rows,
    )

    ok = verify_entities(body)
    if not ok:
        print("ERROR: Non-ASCII characters detected. Fix before sending.")
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="ascii", errors="xmlcharrefreplace") as f:
        f.write(body)

    sb_count = sum(1 for r in full_data if is_strong_buy(r))
    print(f"Email body written: {args.output}")
    print(f"  Body length: {len(body):,} chars")
    print(f"  Strong Buys: {sb_count}")
    print(f"  Entity check: {'PASS' if ok else 'FAIL'}")
    print()
    print("=" * 60)
    print("GMAIL_SEND_EMAIL parameters:")
    print(f"  recipient_email: rjobanputra@sky.com")
    print(f"  sender_email:    raj.a.jobanputra@gmail.com")
    print(f"  subject:         ISA Energy Analysis -- {args.run_date} | {sb_count} Strong Buys (RETRO SAVED)")
    print(f"  body:            <contents of {args.output}>")
    print(f"  is_html:         true   <-- MANDATORY")
    print("=" * 60)


if __name__ == "__main__":
    main()
