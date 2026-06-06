#!/usr/bin/env python3
"""
build_monthly_isa_email.py  --  Monthly ISA Portfolio Review HTML email builder
Version: 1.0  |  2026-05-25

Pre-built script. Called by the Monthly ISA Portfolio Review scheduled task after
all analysis steps are complete. Claude writes a JSON data file during the run;
this script converts it to a compliant HTML email body for GMAIL_SEND_EMAIL.

Usage:
    python3 build_monthly_isa_email.py \\
        --data /path/to/email_data_mmm_yyyy.json \\
        --output /path/to/email_body.html

Output:
    A .html file containing ONLY the email body fragment (no DOCTYPE/html/head/body tags).
    Pass file contents as the `body` parameter of GMAIL_SEND_EMAIL with is_html=true.

Email HTML Rules enforced here:
    Rule 1 -- No Unicode above U+007F (all replaced with HTML entities)
    Rule 2 -- No <head> or <style> blocks (all styles inline)
    Rule 3 -- No display:flex (KPI rows use <table> layout)
    Rule 4 -- All styles inline on each element
    Rule 5 -- No DOCTYPE/html/head/body wrappers in output

Section order (edit SECTION_ORDER list below to reorder sections 4/5 etc.):
    1  Decision Summary
    2  Monthly Capital Allocation
    3  Investment Cases
    4  Position Liquidation Tracker       <-- swap 4 and 5 by editing SECTION_ORDER
    5  Watchlist -- Next Three Months Pipeline
    6  Portfolio Snapshot
    7  Existing Stock Sleeve Review
    8  Fund Portfolio Review (incl. Step 8A)
    9  Macro and Geopolitical Context
    10 Tax Year and ISA Allowance Tracker
    11 Retrospective

JSON schema: see email_data_monthly_isa_TEMPLATE.json in the same folder.
"""

import argparse
import json
import os
import sys


# ---------------------------------------------------------------------------
# Colour palette (dark theme, all inline)
# ---------------------------------------------------------------------------
C = {
    "bg_outer":      "#0f1117",
    "bg_section":    "#131926",
    "bg_th":         "#1e293b",
    "border":        "#1e293b",
    "border_cell":   "#1a2234",
    "text":          "#e2e8f0",
    "text_h1":       "#f8fafc",
    "text_h2":       "#94a3b8",
    "text_h3":       "#cbd5e1",
    "text_muted":    "#64748b",
    "green":         "#4ade80",
    "red":           "#f87171",
    "amber":         "#fbbf24",
    "blue":          "#60a5fa",
    "pill_buy_bg":   "#064e3b",
    "pill_buy_fg":   "#6ee7b7",
    "pill_sell_bg":  "#450a0a",
    "pill_sell_fg":  "#fca5a5",
    "pill_hold_bg":  "#1e3a5f",
    "pill_hold_fg":  "#93c5fd",
    "pill_mon_bg":   "#2d2a00",
    "pill_mon_fg":   "#fde68a",
    "pill_warn_bg":  "#2d1b00",
    "pill_warn_fg":  "#fdba74",
    "pill_grey_bg":  "#1e293b",
    "pill_grey_fg":  "#94a3b8",
    "impact_xl_bg":  "#450a0a",
    "impact_xl_fg":  "#fca5a5",
    "impact_l_bg":   "#2d1b00",
    "impact_l_fg":   "#fdba74",
    "impact_m_bg":   "#1e3a5f",
    "impact_m_fg":   "#93c5fd",
    "impact_s_bg":   "#1e293b",
    "impact_s_fg":   "#94a3b8",
    "impact_xs_bg":  "#131926",
    "impact_xs_fg":  "#64748b",
    "cat_acc_bg":    "#064e3b",
    "cat_acc_fg":    "#6ee7b7",
    "cat_com_bg":    "#1e3a5f",
    "cat_com_fg":    "#93c5fd",
    "cat_eff_bg":    "#2d2a00",
    "cat_eff_fg":    "#fde68a",
    "cat_eff2_bg":   "#2d1b00",
    "cat_eff2_fg":   "#fdba74",
}

# Section order -- edit to reorder (use section key names below)
SECTION_ORDER = [
    "s1_decision_summary",
    "s2_capital_allocation",
    "s3_investment_cases",
    "s4_liquidation_tracker",     # swap with s5 to put watchlist first
    "s5_watchlist",
    "s6_portfolio_snapshot",
    "s7_stock_sleeve",
    "s8_fund_review",
    "s9_macro",
    "s10_tax_tracker",
    "s11_retrospective",
]

SECTION_NUMBERS = {k: i + 1 for i, k in enumerate(SECTION_ORDER)}

SECTION_TITLES = {
    "s1_decision_summary":    "Decision Summary",
    "s2_capital_allocation":  "Monthly Capital Allocation",
    "s3_investment_cases":    "Investment Cases",
    "s4_liquidation_tracker": "Position Liquidation Tracker",
    "s5_watchlist":           "Watchlist &mdash; Next Three Months Pipeline",
    "s6_portfolio_snapshot":  "Portfolio Snapshot",
    "s7_stock_sleeve":        "Existing Stock Sleeve Review",
    "s8_fund_review":         "Fund Portfolio Review",
    "s9_macro":               "Macro and Geopolitical Context",
    "s10_tax_tracker":        "Tax Year and ISA Allowance Tracker",
    "s11_retrospective":      "Retrospective",
}


# ---------------------------------------------------------------------------
# HTML entity safety
# ---------------------------------------------------------------------------
ENTITY_MAP = [
    ("≥", "&ge;"),
    ("≤", "&le;"),
    ("—", "&mdash;"),
    ("–", "&ndash;"),
    ("−", "&minus;"),
    ("§", "&sect;"),
    ("→", "&rarr;"),
    ("×", "&times;"),
    ("£", "&pound;"),
    ("€", "&euro;"),
    ("±", "&plusmn;"),
    ("≠", "&ne;"),
    ("©", "&copy;"),
    ("®", "&reg;"),
    ("’", "&rsquo;"),
    ("‘", "&lsquo;"),
    ("“", "&ldquo;"),
    ("”", "&rdquo;"),
    ("…", "&hellip;"),
    ("°", "&deg;"),
    ("½", "&frac12;"),
    ("¼", "&frac14;"),
    ("¾", "&frac34;"),
    ("²", "&sup2;"),
    ("³", "&sup3;"),
]


def se(text):
    """safe_entities: replace all non-ASCII with HTML entities."""
    if not text:
        return ""
    text = str(text)
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
        print(f"WARNING: {len(violations)} non-ASCII character(s) remain.")
        print(f"  First: U+{ord(violations[0]):04X} ({violations[0]!r})")
        return False
    return True


# ---------------------------------------------------------------------------
# Common HTML primitives
# ---------------------------------------------------------------------------
def h1(text):
    return (
        f'<h1 style="font-size:22px;font-weight:700;color:{C["text_h1"]};'
        f'margin:0 0 4px 0;font-family:Arial,sans-serif">{text}</h1>\n'
    )


def h2(num, title):
    return (
        f'<h2 style="font-size:15px;font-weight:700;color:{C["text_h2"]};'
        f'text-transform:uppercase;letter-spacing:.08em;margin:32px 0 12px 0;'
        f'padding-bottom:6px;border-bottom:1px solid {C["border"]};'
        f'font-family:Arial,sans-serif">{num} &middot; {title}</h2>\n'
    )


def h3(text):
    return (
        f'<h3 style="font-size:14px;font-weight:600;color:{C["text_h3"]};'
        f'margin:16px 0 8px 0;font-family:Arial,sans-serif">{text}</h3>\n'
    )


def section_wrap(content):
    return (
        f'<div style="background:{C["bg_section"]};border-radius:8px;'
        f'padding:16px 20px;margin-bottom:8px">\n'
        f'{content}'
        f'</div>\n'
    )


def para(text, muted=False, small=False):
    colour = C["text_muted"] if muted else C["text"]
    size = "12px" if small else "14px"
    return (
        f'<p style="margin:6px 0;font-size:{size};color:{colour};'
        f'font-family:Arial,sans-serif">{text}</p>\n'
    )


def hr():
    return f'<hr style="border:none;border-top:1px solid {C["border"]};margin:20px 0" />\n'


def pill(text, kind="grey"):
    """kind: buy|sell|hold|monitor|warn|grey"""
    bg_key = f"pill_{kind}_bg"
    fg_key = f"pill_{kind}_fg"
    bg = C.get(bg_key, C["pill_grey_bg"])
    fg = C.get(fg_key, C["pill_grey_fg"])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;'
        f'font-size:11px;font-weight:600;background:{bg};color:{fg}">'
        f'{se(text)}</span>'
    )


def signal_colour(val):
    """Return colour string for a signed value string like '+12.3%' or '-4%'."""
    s = str(val).strip()
    if s.startswith("+"):
        return C["green"]
    if s.startswith("-"):
        return C["red"]
    return C["text"]


def coloured(val, colour=None):
    c = colour or signal_colour(val)
    return f'<span style="color:{c}">{se(val)}</span>'


def ms_stars(rating):
    """Convert numeric rating 1-5 or string to star display."""
    try:
        n = int(str(rating).strip())
    except (ValueError, TypeError):
        return se(str(rating)) if rating else "&mdash;"
    return "★" * n  # will be entity-encoded: &#9733;


def table_start(col_headers, col_widths=None):
    """Returns opening table tag + header row."""
    html = (
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px;'
        f'font-size:13px" cellpadding="0" cellspacing="0" border="0">\n'
        f'<thead><tr>\n'
    )
    for i, h in enumerate(col_headers):
        w = f'width:{col_widths[i]};' if col_widths and i < len(col_widths) else ""
        html += (
            f'<th style="{w}background:{C["bg_th"]};color:{C["text_h2"]};'
            f'font-weight:600;padding:8px 10px;text-align:left;font-size:12px;'
            f'text-transform:uppercase;letter-spacing:.05em;'
            f'font-family:Arial,sans-serif">{se(h)}</th>\n'
        )
    html += "</tr></thead>\n<tbody>\n"
    return html


def table_row(cells, last=False):
    border = "" if last else f'border-bottom:1px solid {C["border_cell"]};'
    html = "<tr>\n"
    for cell in cells:
        html += (
            f'<td style="{border}padding:7px 10px;vertical-align:top;'
            f'font-size:13px;color:{C["text"]};font-family:Arial,sans-serif">'
            f'{cell}</td>\n'
        )
    html += "</tr>\n"
    return html


def table_end():
    return "</tbody>\n</table>\n"


def kpi_row(kpis):
    """
    kpis: list of {label, value, sub, style}
    style: normal|positive|negative|warning|info
    """
    style_colours = {
        "positive": C["green"],
        "negative": C["red"],
        "warning":  C["amber"],
        "info":     C["blue"],
        "normal":   C["text_h1"],
    }
    n = len(kpis)
    if n == 0:
        return ""
    col_pct = f"{100 // n}%"

    html = (
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;margin-bottom:16px">\n<tr>\n'
    )
    for i, kpi in enumerate(kpis):
        pad_left  = "0" if i == 0 else "6px"
        pad_right = "0" if i == n - 1 else "6px"
        val_colour = style_colours.get(kpi.get("style", "normal"), C["text_h1"])
        html += (
            f'<td style="width:{col_pct};padding:0 {pad_right} 0 {pad_left}">\n'
            f'<table cellpadding="12" cellspacing="0" border="0" '
            f'style="width:100%;background:{C["bg_th"]};border-radius:6px">\n'
            f'<tr><td style="color:{C["text_muted"]};font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.06em;'
            f'font-family:Arial,sans-serif">{se(kpi.get("label",""))}</td></tr>\n'
            f'<tr><td style="color:{val_colour};font-size:20px;font-weight:700;'
            f'font-family:Arial,sans-serif">{se(kpi.get("value",""))}</td></tr>\n'
            f'<tr><td style="color:{C["text_muted"]};font-size:11px;'
            f'font-family:Arial,sans-serif">{se(kpi.get("sub",""))}</td></tr>\n'
            f'</table>\n</td>\n'
        )
    html += "</tr>\n</table>\n"
    return html


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_legend():
    """Conviction score legend and investment category definitions — before Section 1."""
    html = (
        f'<div style="background:{C["bg_section"]};border-radius:8px;'
        f'padding:16px 20px;margin-bottom:24px">\n'
        f'<p style="font-size:11px;font-weight:700;color:{C["text_h2"]};'
        f'text-transform:uppercase;letter-spacing:.08em;margin:0 0 10px 0;'
        f'font-family:Arial,sans-serif">CONVICTION SCORE LEGEND</p>\n'
    )
    legend_rows = [
        ("75&ndash;100", "High Conviction",   "Strong case across valuation, moat, growth, and risk/reward. Deploy now if portfolio capacity exists and entry is compelling."),
        ("60&ndash;74",  "Medium Conviction",  "Solid case with one or two unresolved elements. Consider a smaller initial position or wait for a better entry or catalyst confirmation."),
        ("45&ndash;59",  "Watch but Wait",     "Promising but not compelling enough to deploy today. Monitor for entry level, catalyst, or metric improvement."),
        ("&lt;45",       "No Action",          "Does not clear the hurdle on current evidence. Do not deploy."),
    ]
    html += table_start(["Score", "Classification", "Meaning"], ["10%", "20%", "70%"])
    for i, (score, cls, meaning) in enumerate(legend_rows):
        html += table_row([score, f"<strong>{cls}</strong>", meaning], last=(i == len(legend_rows) - 1))
    html += table_end()

    html += (
        f'<p style="font-size:11px;font-weight:700;color:{C["text_h2"]};'
        f'text-transform:uppercase;letter-spacing:.08em;margin:12px 0 10px 0;'
        f'font-family:Arial,sans-serif">INVESTMENT CATEGORY DEFINITIONS</p>\n'
    )
    cat_rows = [
        ("1", "Long-term compounder",   "Durable moat, high/improving ROIC, recurring revenue, proven ability to compound value over a full market cycle. Typically 3&ndash;7+ year hold."),
        ("2", "Cyclical growth",         "Strong fundamentals temporarily discounted by macro cycle or sentiment. Operating leverage and rerating catalyst as cycle turns. Typically 18&ndash;36 month hold."),
        ("3", "Undervalued / rerating",  "Trading below credible fair value due to misunderstood competitive position, overstated risk, or structural change not yet in consensus. Recovery or rerating is the primary return driver."),
        ("4", "Speculative high-growth", "Exceptional revenue growth and large TAM, but limited profitability track record. Higher risk. Position size disciplined accordingly."),
        ("5", "Hybrid",                  "Two or more categories apply simultaneously. Which categories and how they interact is stated explicitly."),
    ]
    html += table_start(["#", "Category", "Definition"], ["4%", "22%", "74%"])
    for i, (num, cat, defn) in enumerate(cat_rows):
        html += table_row([num, f"<strong>{cat}</strong>", defn], last=(i == len(cat_rows) - 1))
    html += table_end()
    html += "</div>\n"
    return html


def build_s1(data):
    """Section 1 — Decision Summary"""
    d = data.get("s1_decision_summary", {})
    inner = ""

    # intro line
    if d.get("intro"):
        inner += para(se(d["intro"]))

    # actions table
    actions = d.get("actions", [])
    if actions:
        pill_map = {
            "buy":     ("buy",  "BUY"),
            "sell":    ("sell", "SELL"),
            "hold":    ("hold", "HOLD"),
            "monitor": ("mon",  "MONITOR"),
        }
        conv_colour = {
            "high":    C["green"],
            "medium":  C["amber"],
            "low":     C["red"],
            "broken":  C["red"],
            "normal":  C["text"],
        }
        inner += table_start(["Action", "Stock", "Size", "Conviction", "Timing", "Note"])
        for i, a in enumerate(actions):
            ak = a.get("action", "hold").lower()
            pkind, plabel = pill_map.get(ak, ("grey", a.get("action", "—").upper()))
            ck = a.get("conviction_level", "normal")
            c_colour = conv_colour.get(ck, C["text"])
            inner += table_row([
                pill(plabel, pkind),
                f'<strong>{se(a.get("name",""))}</strong>',
                se(a.get("size", "—")),
                f'<span style="color:{c_colour}">{se(a.get("conviction","—"))}</span>',
                se(a.get("timing", "—")),
                se(a.get("note", "—")),
            ], last=(i == len(actions) - 1))
        inner += table_end()

    if d.get("net_effect"):
        inner += para(f'<strong>Net effect:</strong> {se(d["net_effect"])}')

    return inner


def build_s2(data):
    """Section 2 — Monthly Capital Allocation"""
    d = data.get("s2_capital_allocation", {})
    inner = ""

    inner += kpi_row(d.get("kpis", []))

    items = d.get("items", [])
    if items:
        pill_map = {"buy": "buy", "sell": "sell", "hold": "hold",
                    "pass": "grey", "monitor": "mon", "reserve": "warn"}
        inner += table_start(["Priority", "Allocation", "Amount", "Rationale"])
        for i, item in enumerate(items):
            alloc_type = item.get("allocation_type", "grey")
            alloc_pill = pill(item.get("allocation", "—"), pill_map.get(alloc_type, "grey"))
            inner += table_row([
                se(item.get("priority", "")),
                alloc_pill,
                se(item.get("amount", "—")),
                se(item.get("rationale", "")),
            ], last=(i == len(items) - 1))
        inner += table_end()

    if d.get("notes"):
        inner += para(se(d["notes"]), muted=True)

    return inner


def build_s3(data):
    """Section 3 — Investment Cases"""
    cases = data.get("s3_investment_cases", [])
    if not cases:
        return para("No investment cases this month.", muted=True)

    inner = ""
    signal_map = {"green": C["green"], "amber": C["amber"], "red": C["red"]}
    action_pills = {"BUY": "buy", "SELL": "sell", "HOLD": "hold", "MONITOR": "mon"}

    for case in cases:
        action = case.get("action", "").upper()
        ticker = se(case.get("ticker", ""))
        name   = se(case.get("name", ""))
        conv   = se(case.get("conviction", ""))

        # Sub-heading
        pill_kind = action_pills.get(action, "grey")
        inner += (
            h3(f'{pill(action, pill_kind)} {ticker} &mdash; {name}'
               + (f' &nbsp;<span style="color:{C["text_muted"]};font-size:12px">'
                  f'Conviction {conv}</span>' if conv else ""))
        )

        # Metrics table
        metrics = case.get("metrics_table", [])
        if metrics:
            inner += table_start(["Metric", "Value", "Assessment"])
            for i, m in enumerate(metrics):
                sig = signal_map.get(m.get("signal", ""), C["text"])
                inner += table_row([
                    se(m.get("label", "")),
                    f'<strong style="color:{sig}">{se(m.get("value",""))}</strong>',
                    f'<span style="color:{sig}">{se(m.get("assessment",""))}</span>',
                ], last=(i == len(metrics) - 1))
            inner += table_end()

        # Narrative paragraphs (se() encodes any £ etc. while preserving <strong> and other ASCII tags)
        for p_text in case.get("paragraphs", []):
            inner += para(se(p_text))

        if case.get("separator_after", False):
            inner += hr()

    return inner


def build_s4(data):
    """Section 4 — Position Liquidation and Consolidation Tracker"""
    d = data.get("s4_liquidation_tracker", {})
    inner = ""

    items = d.get("items", [])
    if items:
        inner += table_start([
            "Position", "Shares", "Current Value", "Cost",
            "Gain/Loss", "Action", "Earliest Sale", "Reason"
        ])
        for i, item in enumerate(items):
            gl = se(item.get("gain_loss", "—"))
            gl_sign = item.get("gain_loss_sign", "neutral")
            gl_colour = C["green"] if gl_sign == "positive" else (C["red"] if gl_sign == "negative" else C["text"])
            action_type = item.get("action_type", "sell").lower()
            pill_map = {"sell": "sell", "hold": "hold", "monitor": "mon", "buy": "buy"}
            inner += table_row([
                f'<strong>{se(item.get("ticker",""))}</strong>',
                se(item.get("shares", "—")),
                se(item.get("current_value", "—")),
                se(item.get("cost", "—")),
                f'<span style="color:{gl_colour}">{gl}</span>',
                pill(item.get("action", "—"), pill_map.get(action_type, "sell")),
                se(item.get("earliest_sale", "—")),
                se(item.get("reason", "—")),
            ], last=(i == len(items) - 1))
        inner += table_end()
    else:
        inner += para("No active liquidation or consolidation candidates this month.", muted=True)

    if d.get("notes"):
        inner += para(se(d["notes"]), muted=True)

    return inner


def build_s5(data):
    """Section 5 — Watchlist"""
    d = data.get("s5_watchlist", {})
    inner = ""

    items = d.get("items", [])
    if items:
        score_colours = {"high": C["green"], "medium": C["amber"], "low": C["text_muted"]}
        status_pills  = {"buy": "buy", "hold": "hold", "watchlist": "grey", "monitor": "mon", "sell": "sell"}
        inner += table_start(["#", "Ticker", "Company", "Score", "Sector", "Entry Level", "Status"])
        for i, item in enumerate(items):
            sc = item.get("score_level", "normal")
            sc_col = score_colours.get(sc, C["text"])
            sk = item.get("status_type", "watchlist")
            inner += table_row([
                se(str(item.get("rank", i + 1))),
                f'<strong>{se(item.get("ticker",""))}</strong>',
                se(item.get("name", "—")),
                f'<span style="color:{sc_col}">{se(item.get("score","—"))}</span>',
                se(item.get("sector", "—")),
                se(item.get("entry_level", "—")),
                pill(item.get("status", "—"), status_pills.get(sk, "grey")),
            ], last=(i == len(items) - 1))
        inner += table_end()

    if d.get("excluded"):
        inner += para(f'<strong>Excluded (ineligible):</strong> {se(d["excluded"])}')

    # Detail blocks for top names
    for detail in d.get("detail_items", []):
        ticker = se(detail.get("ticker", ""))
        name   = se(detail.get("name", ""))
        inner += h3(f'{ticker} &mdash; {name}')
        for p_text in detail.get("paragraphs", []):
            inner += para(se(p_text))

    return inner


def build_s6(data):
    """Section 6 — Portfolio Snapshot"""
    d = data.get("s6_portfolio_snapshot", {})
    inner = ""

    inner += kpi_row(d.get("kpis", []))

    # Holdings table
    holdings = d.get("holdings", [])
    if holdings:
        inner += table_start(["Investment", "Value (&pound;)", "Cost (&pound;)", "Gain %", "Weight %", "&#9733; MS"])
        for i, h in enumerate(holdings):
            gp  = se(h.get("gain_pct", "—"))
            gsc = C["green"] if h.get("gain_sign") == "positive" else (C["red"] if h.get("gain_sign") == "negative" else C["text"])
            tag = " &#9670;Stock" if h.get("is_stock") else ""
            stars_raw = h.get("ms_rating", "")
            if stars_raw and str(stars_raw).isdigit():
                stars = se("★" * int(stars_raw))
            else:
                stars = se(str(stars_raw)) if stars_raw else "&mdash;"
            inner += table_row([
                se(h.get("name", "")) + (f'<span style="color:{C["text_muted"]}">{tag}</span>' if tag else ""),
                se(h.get("value", "—")),
                se(h.get("cost", "—")),
                f'<span style="color:{gsc}">{gp}</span>',
                se(h.get("weight_pct", "—")),
                stars,
            ], last=(i == len(holdings) - 1))
        inner += table_end()

    if d.get("notes"):
        inner += para(se(d["notes"]), muted=True)

    # Performance table
    perf = d.get("performance", [])
    if perf:
        inner += h3(se(d.get("performance_header", "Portfolio Performance vs Benchmark")))
        inner += table_start(["Period", "Portfolio", "Benchmark", "Alpha"])
        for i, row in enumerate(perf):
            p_col = signal_colour(row.get("portfolio", ""))
            a_col = signal_colour(row.get("alpha", ""))
            inner += table_row([
                se(row.get("period", "")),
                f'<span style="color:{p_col}">{se(row.get("portfolio",""))}</span>',
                se(row.get("benchmark", "")),
                f'<span style="color:{a_col}">{se(row.get("alpha",""))}</span>',
            ], last=(i == len(perf) - 1))
        inner += table_end()

    if d.get("risk_metrics"):
        inner += para(f'<strong>Risk metrics:</strong> {se(d["risk_metrics"])}')

    return inner


def build_s7(data):
    """Section 7 — Existing Stock Sleeve Review"""
    d = data.get("s7_stock_sleeve", {})
    inner = ""

    inner += kpi_row(d.get("kpis", []))

    holdings = d.get("holdings", [])
    if holdings:
        pill_map = {"buy": "buy", "sell": "sell", "hold": "hold", "monitor": "mon", "warn": "warn"}
        inner += table_start(["Stock", "Shares", "Value", "Cost", "Gain %", "Weight %", "Status"])
        for i, h in enumerate(holdings):
            gp  = se(h.get("gain_pct", "—"))
            gsc = C["green"] if h.get("gain_sign") == "positive" else (C["red"] if h.get("gain_sign") == "negative" else C["text"])
            st  = h.get("status_type", "hold").lower()
            status_label = se(h.get("status_note", h.get("status", "")))
            inner += table_row([
                f'<strong>{se(h.get("ticker",""))}</strong> {se(h.get("name",""))}',
                se(h.get("shares", "—")),
                se(h.get("value", "—")),
                se(h.get("cost", "—")),
                f'<span style="color:{gsc}">{gp}</span>',
                se(h.get("weight_pct", "—")),
                f'{pill(h.get("status","—"), pill_map.get(st,"grey"))} {status_label}',
            ], last=(i == len(holdings) - 1))
        inner += table_end()

    if d.get("notes"):
        inner += para(se(d["notes"]))

    return inner


def build_s8(data):
    """Section 8 — Fund Portfolio Review (incl. Step 8A)"""
    d = data.get("s8_fund_review", {})
    inner = ""

    # Main fund table
    funds = d.get("funds", [])
    if funds:
        signal_pills = {
            "hold":                 "hold",
            "watch":                "mon",
            "rebalancing candidate":"warn",
            "research trigger":     "warn",
            "research triggered":   "warn",
        }
        inner += table_start([
            "Fund", "Value (&pound;)", "Wt %",
            "1yr %", "&#9733; MS",
            "Bucket", "Target", "Drift", "Band",
            "Est. Ret.", "Signal", "Status"
        ], [
            "20%", "7%", "6%", "6%", "5%",
            "5%", "5%", "5%", "5%",
            "6%", "8%", "12%"
        ])
        for i, f in enumerate(funds):
            sl  = f.get("signal", "hold").lower()
            sp  = signal_pills.get(sl, "grey")
            stl = f.get("status_level", "ok")
            ok_icon = {"ok": "&#9989;", "warning": "&#128993;", "alert": "&#128308;"}.get(stl, "")
            inner += table_row([
                se(f.get("name", "")),
                se(f.get("value", "—")),
                se(f.get("weight_pct", "—")),
                se(f.get("perf_1yr", "—")),
                se(f.get("ms_rating", "—")),
                se(f.get("bucket", "—")),
                se(f.get("target_pct", "—")),
                se(f.get("drift", "—")),
                se(f.get("band", "—")),
                se(f.get("est_return", "—")),
                pill(f.get("signal", "—").title(), sp),
                f'{ok_icon} {se(f.get("status_html",""))}',
            ], last=(i == len(funds) - 1))
        inner += table_end()

    # Step 8A summary block
    s8a = d.get("step8a_summary", {})
    if s8a:
        inner += h3("Step 8A &mdash; Systematic Fund Allocation Summary")

        def _8a_line(label, result, value, ok_colour=C["green"], fail_colour=C["red"]):
            col = ok_colour if result in ("PASS", "On track") else fail_colour
            return para(
                f'<strong>{label}:</strong> '
                f'<span style="color:{col}">{se(result)}</span> '
                f'({se(value)})'
            )

        sa = s8a.get("section_a", {})
        sb = s8a.get("section_b", {})
        sc = s8a.get("section_c", {})

        if sa:
            col_a = C["green"] if sa.get("result") == "PASS" else C["red"]
            inner += para(
                f'<strong>Section A &mdash; Fund sleeve weighted avg return:</strong> '
                f'<span style="color:{col_a}">{se(sa.get("result",""))} '
                f'({se(sa.get("value",""))})</span> vs 12% threshold'
            )
        if sb:
            st_col = {"on_track": C["green"], "watch": C["amber"], "flag": C["red"]}.get(
                sb.get("status",""), C["text"])
            inner += para(
                f'<strong>Section B &mdash; Stock sleeve aggregate return:</strong> '
                f'<span style="color:{st_col}">{se(sb.get("result",""))} '
                f'({se(sb.get("value",""))})</span> vs 18% target'
            )
        if sc:
            st_col = {"on_track": C["green"], "watch": C["amber"], "flag": C["red"]}.get(
                sc.get("status",""), C["text"])
            inner += para(
                f'<strong>Section C &mdash; Total ISA estimated return:</strong> '
                f'<span style="color:{st_col}">{se(sc.get("result",""))} '
                f'({se(sc.get("value",""))})</span> vs 14% working target'
            )
        if s8a.get("overlap_check"):
            inner += para(
                f'<strong>Overlap check:</strong> {se(s8a["overlap_check"])}'
            )
        if s8a.get("regime"):
            inner += para(
                f'<strong>Regime / tilt:</strong> {se(s8a["regime"])}'
            )
        if s8a.get("alt_research"):
            inner += para(
                f'<strong>Alternative fund research:</strong> {se(s8a["alt_research"])}'
            )

    if d.get("xray_summary"):
        inner += para(
            f'<strong>X-Ray sector weights:</strong> {se(d["xray_summary"])}'
        )

    return inner


def build_s9(data):
    """Section 9 — Macro and Geopolitical Context"""
    d = data.get("s9_macro", {})
    items = d.get("items", [])
    if not items:
        return para("No material macro developments this month.", muted=True)

    inner = ""
    for item in items:
        inner += h3(se(item.get("title", "")))
        for p_text in item.get("paragraphs", [item.get("body", "")]):
            if p_text:
                inner += para(se(p_text))
    return inner


def build_s10(data):
    """Section 10 — Tax Year and ISA Allowance Tracker"""
    d = data.get("s10_tax_tracker", {})
    inner = ""

    inner += kpi_row(d.get("kpis", []))

    items = d.get("items", [])
    if items:
        status_colours = {
            "done":      C["green"],
            "pending":   C["amber"],
            "scheduled": C["blue"],
        }
        inner += table_start(["Component", "Amount", "Status"])
        for i, item in enumerate(items):
            st  = item.get("status_type", "done")
            col = status_colours.get(st, C["text"])
            inner += table_row([
                se(item.get("component", "")),
                se(item.get("amount", "—")),
                f'<span style="color:{col}">{se(item.get("status",""))}</span>',
            ], last=(i == len(items) - 1))
        inner += table_end()

    if d.get("notes"):
        inner += para(se(d["notes"]))

    return inner


def build_s11(data):
    """
    Section 11 — Retrospective
    Required per item: title | problem | action | category | impact
    category: accuracy | completeness | efficiency | effectiveness
    impact:   XL | L | M | S | XS
    """
    d = data.get("s11_retrospective", {})
    items = d.get("items", [])
    if not items:
        return para("No retrospective items this month.", muted=True)

    # Impact badge colours
    impact_colours = {
        "XL": (C["impact_xl_bg"], C["impact_xl_fg"]),
        "L":  (C["impact_l_bg"],  C["impact_l_fg"]),
        "M":  (C["impact_m_bg"],  C["impact_m_fg"]),
        "S":  (C["impact_s_bg"],  C["impact_s_fg"]),
        "XS": (C["impact_xs_bg"], C["impact_xs_fg"]),
    }
    # Category badge colours
    cat_colours = {
        "accuracy":       (C["cat_acc_bg"],  C["cat_acc_fg"]),
        "completeness":   (C["cat_com_bg"],  C["cat_com_fg"]),
        "efficiency":     (C["cat_eff_bg"],  C["cat_eff_fg"]),
        "effectiveness":  (C["cat_eff2_bg"], C["cat_eff2_fg"]),
    }

    def badge(text, bg, fg):
        return (
            f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;'
            f'font-size:11px;font-weight:600;background:{bg};color:{fg}">'
            f'{se(text)}</span>'
        )

    inner = ""
    for idx, item in enumerate(items, 1):
        title  = se(item.get("title", f"Item {idx}"))
        prob   = item.get("problem", "")
        action = item.get("action", "")
        cat    = item.get("category", "").lower()
        impact = item.get("impact", "").upper()

        # Build badges
        cat_bg, cat_fg = cat_colours.get(cat, (C["pill_grey_bg"], C["pill_grey_fg"]))
        imp_bg, imp_fg = impact_colours.get(impact, (C["impact_s_bg"], C["impact_s_fg"]))

        cat_badge    = badge(cat.title() if cat else "—", cat_bg, cat_fg)
        impact_badge = badge(f"Impact: {impact}" if impact else "Impact: —", imp_bg, imp_fg)

        inner += (
            f'<div style="border-left:3px solid {C["border"]};'
            f'padding:10px 14px;margin-bottom:14px">\n'
            f'<p style="margin:0 0 6px 0;font-size:13px;font-weight:700;'
            f'color:{C["text_h1"]};font-family:Arial,sans-serif">'
            f'{idx}. {title} &nbsp;{cat_badge} &nbsp;{impact_badge}</p>\n'
        )
        if prob:
            inner += (
                f'<p style="margin:0 0 4px 0;font-size:13px;color:{C["text"]};'
                f'font-family:Arial,sans-serif">'
                f'<span style="color:{C["text_muted"]}">Problem:</span> {se(prob)}</p>\n'
            )
        if action:
            inner += (
                f'<p style="margin:0;font-size:13px;color:{C["text"]};'
                f'font-family:Arial,sans-serif">'
                f'<span style="color:{C["text_muted"]}">Action:</span> {se(action)}</p>\n'
            )
        inner += "</div>\n"

    return inner


# ---------------------------------------------------------------------------
# Header and footer
# ---------------------------------------------------------------------------
def build_header(meta):
    run_date  = se(meta.get("run_date_display", ""))
    data_date = se(meta.get("data_date", ""))
    broker    = se(meta.get("broker", ""))
    tax_year  = se(meta.get("tax_year", ""))
    tax_month = se(meta.get("tax_year_month", ""))
    meta_line = " &nbsp;|&nbsp; ".join(filter(None, [
        f"Run date: {run_date}" if run_date else "",
        f"Data as at: {data_date}" if data_date else "",
        f"Broker: {broker}" if broker else "",
        f"Tax Year: {tax_year} {tax_month}".strip() if tax_year else "",
    ]))
    return (
        h1("Monthly ISA Portfolio Review")
        + f'<p style="font-size:12px;color:{C["text_muted"]};margin-bottom:28px;'
        f'font-family:Arial,sans-serif">{meta_line}</p>\n'
    )


def build_footer(meta):
    run_date  = se(meta.get("run_date_display", ""))
    data_date = se(meta.get("data_date", ""))
    broker    = se(meta.get("broker", ""))
    return (
        f'<hr style="border:none;border-top:1px solid {C["border"]};margin:20px 0" />\n'
        f'<p style="font-size:11px;color:{C["text_muted"]};text-align:center;'
        f'margin-top:24px;font-family:Arial,sans-serif">'
        f'Generated automatically by Monthly ISA Portfolio Review'
        f'{(" &mdash; " + run_date) if run_date else ""}'
        f' &nbsp;|&nbsp; {broker}'
        f'{(" &nbsp;|&nbsp; Data as at " + data_date) if data_date else ""}<br>'
        f'This is a personal financial record, not investment advice. '
        f'Citi preclearance required before executing any individual stock trade.'
        f'</p>\n'
    )


# ---------------------------------------------------------------------------
# Section dispatch
# ---------------------------------------------------------------------------
SECTION_BUILDERS = {
    "s1_decision_summary":   build_s1,
    "s2_capital_allocation": build_s2,
    "s3_investment_cases":   build_s3,
    "s4_liquidation_tracker":build_s4,
    "s5_watchlist":          build_s5,
    "s6_portfolio_snapshot": build_s6,
    "s7_stock_sleeve":       build_s7,
    "s8_fund_review":        build_s8,
    "s9_macro":              build_s9,
    "s10_tax_tracker":       build_s10,
    "s11_retrospective":     build_s11,
}


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------
def build_email_body(data):
    meta   = data.get("meta", {})
    parts  = []

    # Outer wrapper (dark background, max-width)
    parts.append(
        f'<div style="font-family:Arial,sans-serif;font-size:14px;'
        f'color:{C["text"]};background:{C["bg_outer"]};'
        f'max-width:760px;margin:0 auto;padding:24px 16px">\n'
    )

    # Header
    parts.append(build_header(meta))

    # Conviction legend
    parts.append(build_legend())

    # Sections in order
    for section_key in SECTION_ORDER:
        num   = SECTION_NUMBERS.get(section_key, "?")
        title = SECTION_TITLES.get(section_key, section_key)
        builder = SECTION_BUILDERS.get(section_key)
        if builder is None:
            continue
        content = builder(data)
        parts.append(h2(num, title))
        parts.append(section_wrap(content))

    # Footer
    parts.append(build_footer(meta))
    parts.append("</div>\n")

    return "".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Build Monthly ISA Portfolio Review HTML email body from JSON data file."
    )
    parser.add_argument("--data",   required=True,
                        help="Path to email_data_mmm_yyyy.json produced during the run")
    parser.add_argument("--output", required=True,
                        help="Output path for HTML body file e.g. email_body_jun2026.html")
    args = parser.parse_args()

    # Load JSON
    if not os.path.exists(args.data):
        print(f"ERROR: Data file not found: {args.data}")
        sys.exit(1)
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    print("Building email body...")
    body = build_email_body(data)

    # Verify no non-ASCII remains
    ok = verify_entities(body)
    if not ok:
        print("ERROR: Non-ASCII characters detected. Fix before sending.")
        sys.exit(1)

    # Write output
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="ascii", errors="xmlcharrefreplace") as f:
        f.write(body)

    meta       = data.get("meta", {})
    run_month  = meta.get("run_month_label", "Mmm YYYY")
    char_count = len(body)
    print(f"Email body written: {args.output}")
    print(f"  Body length: {char_count:,} characters")
    print(f"  Entity check: {'PASS' if ok else 'FAIL'}")
    print()
    print("=" * 65)
    print("GMAIL_SEND_EMAIL parameters:")
    print(f"  recipient_email: rjobanputra@sky.com")
    print(f"  sender_email:    raj.a.jobanputra@gmail.com")
    print(f"  subject:         Monthly ISA Portfolio Review -- {run_month}")
    print(f"  body:            <contents of {args.output}>")
    print("  is_html:         true   (MANDATORY -- always pass is_html=true)")
    print("=" * 65)


if __name__ == "__main__":
    main()
