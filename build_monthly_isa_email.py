#!/usr/bin/env python3
"""
build_monthly_isa_email.py  --  Monthly ISA Portfolio Review: the decision BRIEF (email) and the full REPORT
Version: 2.0  |  2026-10-25 (ISA-0898 / ISA-0900 / ISA-0642)

Pre-built script. Called by the Monthly ISA Portfolio Review scheduled task after all analysis steps are
complete. Claude writes a JSON data file during the run; this script renders it. It is a RENDERER (R20.2):
it computes no decision and no measure - every number arrives in the data or the run context.

Usage (the monthly contract, Run_Context Step 10):
    python3 build_monthly_isa_email.py --data email_data_mmm_yyyy.json --output email_body_mmm_yyyy.html --lean
        [--report-url https://claude.ai/...]     # once the report page is published; else it is attached

Outputs:
    --output             the BRIEF: the email body fragment (no DOCTYPE/html/head/body). Decision first, the
                         acceptable price range on every dealing decision, every warning row of the report
                         (ISA-0642 subset proof), the month in numbers, a pointer to the report.
    <stem>_full.html     the REPORT: a complete web page - ten chapters, technical detail collapsed, glossary.
                         (--full-output overrides the path.)  Without --lean, --output is the report fragment.
    <stem>_lean_report.json and an appended lean_history.jsonl row (coverage, size).

Writing standard (Run_Context "Writing standard", ISA-0898): every narrative in three layers - a one-line
verdict; "In plain English" for a reader with no financial knowledge (why this company, why now, what could
go wrong); then the evidence. Instrument NAMES lead, codes beneath. Negative figures are red with a minus
sign. The narrative gate (exit 4) refuses a send with a missing layer, a dealing verdict with no acceptable
price range, or jargon in plain-English text.

Email HTML Rules enforced on the BRIEF:
    Rule 1 -- No Unicode above U+007F (all replaced with HTML entities)
    Rule 2 -- No <head> or <style> blocks (all styles inline, explicit dark backgrounds on every block)
    Rule 3 -- No display:flex (tiles use <table> layout)
    Rule 4 -- All styles inline on each element
    Rule 5 -- No DOCTYPE/html/head/body wrappers in output

Data sections (SECTION_ORDER, asserted against Run_Context by consistency pair M2) map onto report chapters
(CHAPTERS): decision = s1 + s2 + decided s3; performance = s6 + account statement (ISA-0900); shares = s7;
funds = s8; opportunities = other s3 + s5 + s4; markets = s9; allowance = s10; learning = s11.

JSON schema: see email_data_monthly_isa_TEMPLATE.json in the same folder.
"""

import argparse
import re
import json
import os
import sys


# ---------------------------------------------------------------------------
# ISA-0898 (25-Oct-2026) - PRIVATE-BANK PRESENTATION LAYER
#
# Raj, 25-Oct-2026: "institutional in quality, content and formatting - think like I am a HNW and you are
# Coutts"; dark style; instrument NAMES lead and codes sit beneath; every negative is red with a minus sign;
# return windows in their own columns; every narrative in three layers (verdict / in plain English /
# evidence); charts only where a picture says what a table cannot.
#
# Two outputs are built from ONE data dict and ONE set of section builders:
#   * the REPORT (build_report_page) - a complete web page: chapters, collapsible technical detail, glossary;
#   * the BRIEF  (build_brief)       - the email: decision, what needs attention, the month in numbers, and a
#     pointer to the report. Inline styles only (Yahoo/Sky strip wrappers; Gmail mobile inverts colours when a
#     background is not explicit - every block therefore carries an explicit dark background and text colour).
# The brief carries EVERY warning row of the report (ISA-0642 subset proof, lean_report.warnings_coverage).
# ---------------------------------------------------------------------------
C = {
    "bg_outer":      "#0b0d10",
    "bg_page":       "#0f1216",
    "bg_section":    "#14181e",
    "bg_panel":      "#1a1f27",
    "bg_th":         "#1d232c",
    "border":        "#2b323d",
    "border_cell":   "#232a34",
    "text":          "#e6e1d5",
    "text_h1":       "#f6f1e6",
    "text_h2":       "#c8a96b",
    "text_h3":       "#ece6d8",
    "text_muted":    "#968f82",
    "gold":          "#c8a96b",
    "green":         "#6cbf8f",
    "red":           "#e2766a",
    "amber":         "#e6b14c",
    "blue":          "#86a9d6",
    "pill_buy_bg":   "#173527",
    "pill_buy_fg":   "#8fd6ab",
    "pill_sell_bg":  "#3d1b18",
    "pill_sell_fg":  "#f0a197",
    "pill_hold_bg":  "#1b2b40",
    "pill_hold_fg":  "#a9c4e6",
    "pill_mon_bg":   "#33290f",
    "pill_mon_fg":   "#ecc979",
    "pill_warn_bg":  "#3a2810",
    "pill_warn_fg":  "#efbe6b",
    "pill_grey_bg":  "#232a34",
    "pill_grey_fg":  "#b4ad9f",
    "impact_xl_bg":  "#3d1b18",
    "impact_xl_fg":  "#f0a197",
    "impact_l_bg":   "#3a2810",
    "impact_l_fg":   "#efbe6b",
    "impact_m_bg":   "#1b2b40",
    "impact_m_fg":   "#a9c4e6",
    "impact_s_bg":   "#232a34",
    "impact_s_fg":   "#b4ad9f",
    "impact_xs_bg":  "#14181e",
    "impact_xs_fg":  "#968f82",
    "cat_acc_bg":    "#173527",
    "cat_acc_fg":    "#8fd6ab",
    "cat_com_bg":    "#1b2b40",
    "cat_com_fg":    "#a9c4e6",
    "cat_eff_bg":    "#33290f",
    "cat_eff_fg":    "#ecc979",
    "cat_eff2_bg":   "#3a2810",
    "cat_eff2_fg":   "#efbe6b",
}
SANS = "Helvetica,Arial,sans-serif"      # no quoted family names: Yahoo rewrites quotes inside style=""
SERIF = "Georgia,serif"

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
# Common HTML primitives (ISA-0898 private-bank style; every block carries explicit colours)
# ---------------------------------------------------------------------------
def h1(text):
    return (f'<h1 style="font-family:{SERIF};font-size:30px;font-weight:400;line-height:1.2;'
            f'color:{C["text_h1"]};margin:6px 0 10px 0">{text}</h1>\n')


def eyebrow(text):
    return (f'<p style="font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:.14em;'
            f'text-transform:uppercase;color:{C["gold"]};margin:0 0 6px 0">{text}</p>\n')


def h2(num, title, anchor=None):
    _id = f' id="{anchor}"' if anchor else ""
    return (f'<div{_id} style="margin:44px 0 14px 0;padding-top:18px;border-top:1px solid {C["border"]}">'
            + eyebrow(f"{num}" if isinstance(num, str) and not str(num).isdigit() else f"Chapter {num}")
            + f'<h2 style="font-family:{SERIF};font-size:24px;font-weight:400;color:{C["text_h1"]};'
              f'margin:0">{title}</h2></div>\n')


def h3(text):
    return (f'<h3 style="font-family:{SANS};font-size:15px;font-weight:700;color:{C["text_h3"]};'
            f'margin:22px 0 8px 0">{text}</h3>\n')


def section_wrap(content):
    return (f'<div style="background:{C["bg_section"]};border:1px solid {C["border"]};border-radius:6px;'
            f'padding:18px 22px;margin:0 0 10px 0;color:{C["text"]}">\n{content}</div>\n')


def para(text, muted=False, small=False):
    colour = C["text_muted"] if muted else C["text"]
    size = "12px" if small else "14px"
    return (f'<p style="margin:7px 0;font-size:{size};line-height:1.55;color:{colour};'
            f'font-family:{SANS}">{text}</p>\n')


def verdict_line(text):
    """Layer 1 of the ISA-0898 writing standard: the one-line verdict."""
    return (f'<p style="margin:4px 0 10px 0;font-family:{SERIF};font-size:19px;line-height:1.4;'
            f'color:{C["text_h1"]}">{text}</p>\n')


def plain_panel(text, label="In plain English"):
    """Layer 2: written for a reader with no financial knowledge."""
    body = text if isinstance(text, str) else "".join(f"<br>{t}" for t in text)[4:]
    return (f'<div style="background:{C["bg_panel"]};border-left:3px solid {C["gold"]};padding:10px 14px;'
            f'margin:8px 0 12px 0"><p style="margin:0 0 4px 0;font-family:{SANS};font-size:11px;font-weight:700;'
            f'letter-spacing:.1em;text-transform:uppercase;color:{C["gold"]}">{label}</p>'
            f'<p style="margin:0;font-family:{SANS};font-size:14px;line-height:1.6;color:{C["text"]}">{body}</p>'
            f'</div>\n')


def labelled(label, text):
    return (f'<p style="margin:6px 0;font-family:{SANS};font-size:14px;line-height:1.6;color:{C["text"]}">'
            f'<span style="color:{C["gold"]};font-weight:700">{label}</span> {text}</p>\n')


def hr():
    return f'<hr style="border:none;border-top:1px solid {C["border"]};margin:20px 0" />\n'


def details(summary, content, open_=False):
    """Collapsible TECHNICAL DETAIL (report page only - the brief never contains one)."""
    if not content:
        return ""
    return (f'<details{" open" if open_ else ""} style="margin:12px 0;border:1px solid {C["border"]};'
            f'border-radius:6px;background:{C["bg_page"]}"><summary style="cursor:pointer;padding:9px 14px;'
            f'font-family:{SANS};font-size:13px;color:{C["text_muted"]}">{summary}</summary>'
            f'<div style="padding:4px 16px 12px 16px">{content}</div></details>\n')


def pill(text, kind="grey"):
    """kind: buy|sell|hold|monitor|warn|grey"""
    bg = C.get(f"pill_{kind}_bg", C["pill_grey_bg"])
    fg = C.get(f"pill_{kind}_fg", C["pill_grey_fg"])
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:11px;font-family:{SANS};'
            f'font-size:11px;font-weight:700;letter-spacing:.04em;background:{bg};color:{fg};white-space:nowrap">'
            f'{se(text)}</span>')


def flag(text, level):
    """A status cell that IS a warning (red/amber) or a clean verdict (green). Red/amber flags are
    warning rows for ISA-0642's subset proof; green is not."""
    col = {"red": C["red"], "amber": C["amber"], "green": C["green"]}.get(level, C["text_muted"])
    return f'<strong style="color:{col}">{se(text)}</strong>'


def signal_colour(val):
    """Return colour string for a signed value string like '+12.3%' or '-4%'."""
    s = str(val).strip()
    if s.startswith("+"):
        return C["green"]
    if s.startswith("-") or s.startswith("−") or s.startswith("&minus;"):
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
    return "&#9733;" * n


def name_cell(name, code=None, note=None):
    """ISA-0898 (and the 06-Oct feedback): the instrument NAME leads; the code sits beneath, never alone."""
    n = se(str(name)) if name else (se(str(code)) if code else "&mdash;")
    sub = []
    if code and name and str(code) != str(name):
        sub.append(se(str(code)))
    if note:
        sub.append(se(str(note)))
    return (f'<strong style="color:{C["text_h3"]}">{n}</strong>'
            + (f'<br><span style="font-size:11px;color:{C["text_muted"]}">{" &middot; ".join(sub)}</span>' if sub else ""))


_NEG_RE = re.compile(r"^\s*(?:-|&minus;|−)\s*(?:&pound;|£|\$|&euro;)?\s*\d")
_NUMERIC_RE = re.compile(r"^\s*(?:[+\-]|&minus;)?\s*(?:&pound;|£|\$|&euro;|GBP\s)?\s*[\d,]+(?:\.\d+)?\s*"
                         r"(?:%|pp|&times;|x|bn|m|k)?\s*$")


def _visible(cell):
    return re.sub(r"<[^>]+>", "", str(cell)).strip()


def table_start(col_headers, col_widths=None, align=None):
    """align: optional list of 'l'/'r' per column (numbers right-aligned)."""
    html = (f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;margin:6px 0 16px 0;'
            f'font-family:{SANS};font-size:13px;background:{C["bg_section"]}" cellpadding="0" cellspacing="0" border="0">\n'
            f'<thead><tr>\n')
    for i, h in enumerate(col_headers):
        w = f'width:{col_widths[i]};' if col_widths and i < len(col_widths) and col_widths[i] else ""
        ta = "right" if (align and i < len(align) and align[i] == "r") else "left"
        html += (f'<th style="{w}background:{C["bg_th"]};color:{C["text_muted"]};font-weight:700;padding:8px 10px;'
                 f'text-align:{ta};font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
                 f'overflow-wrap:anywhere;word-break:break-word;'
                 f'border-bottom:1px solid {C["border"]}">{h}</th>\n')
    html += "</tr></thead>\n<tbody>\n"
    return html


def table_row(cells, last=False, align=None):
    """Negative figures are red and keep their minus sign (06-Oct feedback point 5); numbers right-align."""
    border = "" if last else f'border-bottom:1px solid {C["border_cell"]};'
    html = "<tr>\n"
    for i, cell in enumerate(cells):
        vis = _visible(cell)
        ta = ("right" if (align and i < len(align) and align[i] == "r")
              or (align is None and _NUMERIC_RE.match(vis) and i > 0) else "left")
        if _NEG_RE.match(vis) and "color:" not in str(cell):
            cell = f'<span style="color:{C["red"]}">{cell}</span>'
        html += (f'<td style="{border}padding:8px 10px;vertical-align:top;text-align:{ta};font-size:13px;'
                 f'line-height:1.45;color:{C["text"]};background:{C["bg_section"]};'
                 f'overflow-wrap:anywhere;word-break:break-word">{cell}</td>\n')
    html += "</tr>\n"
    return html


def table_end():
    return "</tbody>\n</table></div>\n"


def kpi_row(kpis):
    """kpis: list of {label, value, sub, style}; style: normal|positive|negative|warning|info"""
    style_colours = {"positive": C["green"], "negative": C["red"], "warning": C["amber"],
                     "info": C["text_h1"], "normal": C["text_h1"]}
    n = len(kpis)
    if n == 0:
        return ""
    col_pct = f"{100 // n}%"
    # ISA-0898 (28-Oct-2026): fluid inline-block tiles - side by side at 760px, stacked on a phone (no flex)
    html = f'<div style="width:100%;margin:4px 0 4px 0;font-size:0;text-align:left;background:{C["bg_section"]}">\n'
    for i, kpi in enumerate(kpis):
        val = str(kpi.get("value", ""))
        vc = style_colours.get(kpi.get("style", "normal"), C["text_h1"])
        if _NEG_RE.match(_visible(se(val))):
            vc = C["red"]
        html += (f'<div style="display:inline-block;vertical-align:top;width:{col_pct};min-width:160px;max-width:100%;'
                 f'box-sizing:border-box;padding:0 6px 12px 0;font-size:13px;background:{C["bg_section"]}">\n'
                 f'<table cellpadding="14" cellspacing="0" border="0" style="width:100%;background:{C["bg_panel"]};'
                 f'border:1px solid {C["border"]};border-radius:6px">\n'
                 f'<tr><td style="font-family:{SANS};color:{C["gold"]};font-size:11px;font-weight:700;'
                 f'text-transform:uppercase;letter-spacing:.1em;padding-bottom:2px;background:{C["bg_panel"]}">'
                 f'{se(kpi.get("label", ""))}</td></tr>\n'
                 f'<tr><td style="font-family:{SERIF};color:{vc};font-size:24px;padding-top:2px;padding-bottom:2px;'
                 f'background:{C["bg_panel"]}">{se(val)}</td></tr>\n'
                 f'<tr><td style="font-family:{SANS};color:{C["text_muted"]};font-size:12px;line-height:1.45;'
                 f'padding-top:2px;background:{C["bg_panel"]}">{se(kpi.get("sub", ""))}</td></tr>\n'
                 f'</table>\n</div>\n')
    html += "</div>\n"
    return html


def band_bar(weight_pct, low, high, target=None):
    """The ONE chart in the fund table (ISA-0898: only where a picture says what a table cannot): where the
    fund's weight sits inside its permitted band, read at a glance down twelve rows."""
    try:
        w, lo, hi = float(weight_pct), float(low), float(high)
    except (TypeError, ValueError):
        return ""
    span = max(hi * 1.35, w * 1.1, 1.0)
    pos = lambda x: max(0.0, min(100.0, 100.0 * x / span))
    inside = lo <= w <= hi
    mk = C["green"] if inside else C["red"]
    tgt = (f'<span style="position:absolute;left:{pos(float(target)):.1f}%;top:1px;width:1px;height:8px;'
           f'background:{C["text_muted"]}"></span>') if target not in (None, "") else ""
    return (f'<span style="display:block;position:relative;width:120px;height:10px;margin-top:5px;'
            f'background:{C["bg_th"]};border-radius:2px">'
            f'<span style="position:absolute;left:{pos(lo):.1f}%;width:{pos(hi) - pos(lo):.1f}%;top:0;height:10px;'
            f'background:#2f3a2f;border-radius:2px"></span>{tgt}'
            f'<span style="position:absolute;left:calc({pos(w):.1f}% - 3px);top:-1px;width:6px;height:12px;'
            f'border-radius:2px;background:{mk}"></span></span>')


# ---------------------------------------------------------------------------
# Small parsers for strings the PRE-RUN composed (presentation only - nothing is recomputed)
# ---------------------------------------------------------------------------
def _pct_of(s):
    m = re.search(r"([+\-−]?\d+(?:\.\d+)?)\s*%", str(s or ""))
    return float(m.group(1).replace("−", "-")) if m else None


def _band_of(s):
    m = re.search(r"(\d+(?:\.\d+)?)\s*[–\-]\s*(\d+(?:\.\d+)?)", str(s or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def _windows_of(s):
    """'1y 19.8% · 3y 17.4% · 5y 10.3%' -> {'1y': '19.8%', ...}"""
    return {k: v for k, v in re.findall(r"(\d+y)\s+([+\-]?\d+(?:\.\d+)?%)", str(s or ""))}


def _dated_return(s):
    """'X-Ray +18.6% (30-Sep-26)' -> ('+18.6%', 'X-Ray, 30-Sep-26'); anything else passes through."""
    m = re.match(r"^\s*(.*?)\s*([+\-−]?\d+(?:\.\d+)?%)\s*\(([^)]+)\)\s*$", str(s or ""))
    if not m:
        return (str(s or "—"), "")
    return (m.group(2), ", ".join(x for x in (m.group(1).strip(), m.group(3).strip()) if x))


def _note_fields(note):
    """Legacy s7 status_note 'Analyst: x | Target upside: y | Next earnings: z | Score: n/50 | [...]' -> dict."""
    out = {}
    for part in str(note or "").split("|"):
        if ":" in part and not part.strip().startswith("["):
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


_INSTRUMENT_NAMES = None


def instrument_name(code):
    """SEDOL / ticker -> the instrument's NAME, from fund_universe.json (read-only). None when unknown."""
    global _INSTRUMENT_NAMES
    if _INSTRUMENT_NAMES is None:
        _INSTRUMENT_NAMES = {}
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fund_universe.json"),
                      encoding="utf-8") as f:
                for k, v in (json.load(f).get("funds") or {}).items():
                    if isinstance(v, dict) and v.get("name"):
                        _INSTRUMENT_NAMES[str(k)] = v["name"]
        except Exception:                                               # noqa: BLE001
            pass
    return _INSTRUMENT_NAMES.get(str(code or "").strip())


def _named(code):
    nm = instrument_name(code)
    return name_cell(nm, code) if nm else name_cell(None, code, note="name not on file")


_STATE_LABELS = {
    "NOT_CAPTURED_CONTEMPORANEOUSLY": "Not recorded at purchase",
    "NOT_DEFENSIBLY_QUANTIFIABLE": "Cannot be reliably estimated",
    "METHOD_CHANGED": "Method changed since purchase",
    "VALID_MECHANICAL": "Valid",
    "admissible": "Valid",
    "NO POSITIVE NEW SIZE": "No new money",
    "UNMEASURED": "Not yet measurable",
    "CAP_AT_CURRENT": "Hold at current size",
    "MISSING_ROW": "Missing",
    "UNVERIFIED_FLOWS": "Not measured: cash records do not cover the period",
    "NO_PRIOR_SNAPSHOT": "No earlier valuation",
    "INSUFFICIENT_HISTORY": "Not enough history",
}

# ISA-0898: summary lines carry the framework's key; the reader sees a plain measure name.
LINE_LABELS = {
    "mmf_sweep": "Idle cash moved to the cash fund",
    "step9_pre._meta.broker_dealability": "Companies the broker cannot deal online",
    "phase_status": "Stage of the share-picking programme",
    "concentration": "How concentrated the risks are",
    "position_alerts": "Price and earnings alerts",
    "watchlist_promotion_log": "Screen-stage changes (before the re-rank)",
    "watchlist_rerank": "Final watchlist changes (live re-rank)",
    "reversal_worklist": "Companies needing a fresh check",
    "vci_calibration_state": "Asymmetric-sleeve calibration",
    "process_concentration": "Reliance on one fund manager's method",
    "strategic_allocation": "Differences from the reference mix",
    "t4_mandate_drift": "Funds drifting from their stated style",
    "lookthrough": "Overlap between funds and shares",
    "fund_holdings_declared": "Fund holdings on file",
    "fund_categories": "Fund peer groups on file",
    "rebalancing_candidates": "Funds that may need rebalancing",
    "cash_statement": "Cash statement",
    "transactions": "Trades this period",
}


def state_cell(raw):
    """Plain-English label first, the framework's own state code beneath (traceability without jargon)."""
    raw_s = str(raw if raw not in (None, "") else "—")
    key = raw_s.split(" (")[0].strip()
    lab = _STATE_LABELS.get(key)
    if raw_s.startswith("ENTRY_CONTEMPORANEOUS") and "INTACT" in raw_s:
        lab = "Intact (recorded at purchase)"
    if not lab:
        return se(raw_s)
    return (f'{se(lab)}<br><span style="font-size:11px;color:{C["text_muted"]}">{se(raw_s)}</span>')


# ---------------------------------------------------------------------------
# Summary lines (ISA-0448): ONE renderer, as a Measure | Reading | Status table
# ---------------------------------------------------------------------------
def _summary_lines_html(sd, title=None):
    """The computed lines email_prefill.build_summary_lines placed on a section, as one table. A WARN line's
    status cell is an amber flag, so it is a warning row for the ISA-0642 subset proof."""
    if not isinstance(sd, dict):
        return ""
    lines = [ln for ln in (sd.get("summary_lines") or []) if isinstance(ln, dict) and ln.get("text")]
    if not lines:
        return ""
    out = h3(title) if title else ""
    out += table_start(["Measure", "Reading", "Status"], ["24%", "62%", "14%"], ["l", "l", "l"])
    for i, ln in enumerate(lines):
        warn = ln.get("severity") == "warn"
        _k = str(ln.get("key", ""))
        _lab = ln.get("label") or LINE_LABELS.get(_k) or _k.replace("_", " ").capitalize()
        out += table_row([f'<strong>{se(str(_lab))}</strong>',
                          se(str(ln["text"])),
                          flag("Needs attention", "amber") if warn else
                          f'<span style="color:{C["text_muted"]}">For information</span>'],
                         last=(i == len(lines) - 1), align=["l", "l", "l"])
    out += table_end()
    return out

# ---------------------------------------------------------------------------
# Section builders (ISA-0898). Each renders ONE data key; build_report_body composes them into chapters.
# Narratives use the three-layer writing standard (Run_Context "Writing standard"):
#   1 verdict (one line; a BUY/SELL verdict states the acceptable price range)
#   2 "In plain English" (a reader with no financial knowledge; why this company, why now, what could go wrong)
#   3 evidence (the numbers, the rules, the sources)
# ---------------------------------------------------------------------------
_ACTION_PILL = {"buy": "buy", "add": "buy", "top-up": "buy", "sell": "sell", "trim": "sell", "hold": "hold",
                "monitor": "mon", "withheld": "warn", "pass": "grey", "reserve": "warn", "fund": "hold"}


def _pill_for(action):
    a = str(action or "").strip().lower().split()[0] if action else ""
    return pill(str(action or "—").upper(), _ACTION_PILL.get(a, "grey"))


def _missing(label="Not stated"):
    return flag(label, "amber")


def _price_range(a):
    lo, hi, ccy = a.get("price_low"), a.get("price_max"), a.get("price_ccy") or ""
    if lo in (None, "") and hi in (None, ""):
        return None
    f = lambda v: (f"{ccy}{v}" if v not in (None, "") else "—")
    return f"{f(lo)} to {f(hi)} per {a.get('price_unit') or 'share'}"


_FUND_STATE = {"SELL_CASH_FUND": None, "CASH_COVERS": "green", "NO_PURCHASE": None}


def funding_box(fp):
    """ISA-0904 (Raj 26-Oct-2026): the cash-fund sale that funds the purchases - units, proceeds, order of dealing.
    The figures are mmf_funding.plan's (computed at build time from the FINAL decided actions); nothing here."""
    fp = fp or {}
    if not fp or fp.get("state") == "NO_PURCHASE":
        return ""
    st = fp.get("state")
    lvl = _FUND_STATE.get(st, "amber")
    head = ("Sell %s units of the cash fund first" % fp.get("units_to_sell") if st == "SELL_CASH_FUND" else
            "No cash-fund sale needed" if st == "CASH_COVERS" else "Funding not yet stated")
    body = se(fp.get("line") or "")
    det = ""
    if st in ("SELL_CASH_FUND", "CASH_COVERS", "INSUFFICIENT_CASH_FUND"):
        det = (f'<br><span style="font-size:12px;color:{C["text_muted"]}">Purchases &pound;{fp.get("buys_gbp", 0):,.2f}'
               + (f' less share sales &pound;{fp["share_sale_proceeds_gbp"]:,.2f}' if fp.get("share_sale_proceeds_gbp") else "")
               + f' &middot; broker cash &pound;{fp.get("broker_cash_gbp", 0):,.2f} less the &pound;{fp.get("reserve_gbp", 0):,.0f} reserve'
               + (f' and &pound;{fp["drawdown_reserved_gbp"]:,.2f} held for the drawdown plan' if fp.get("drawdown_reserved_gbp") else "")
               + (f' &middot; shortfall &pound;{fp.get("shortfall_gbp", 0):,.2f}' if fp.get("shortfall_gbp") else "") + "</span>")
    title = flag(head, lvl) if lvl else f'<strong style="color:{C["gold"]}">{se(head)}</strong>'
    return (f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:10px 0;background:{C["bg_panel"]};'
            f'border:1px solid {C["gold"]};border-radius:6px"><tr><td style="padding:12px 16px;background:{C["bg_panel"]};'
            f'font-family:{SANS};font-size:14px;line-height:1.55;color:{C["text"]}">'
            f'<p style="margin:0 0 4px 0;font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:.12em;'
            f'text-transform:uppercase;color:{C["gold"]}">Funding the purchases</p>'
            f'<p style="margin:0;font-family:{SANS};font-size:15px;color:{C["text"]}">{title}</p>'
            f'<p style="margin:4px 0 0 0;font-family:{SANS};font-size:13px;color:{C["text"]}">{body}{det}</p></td></tr></table>\n')


def build_s1(data):
    """The decision in focus: verdict, plain English, the action sheet for every decided action."""
    d = data.get("s1_decision_summary", {})
    out = ""
    if d.get("verdict"):
        out += verdict_line(se(d["verdict"]))
    if d.get("plain_english"):
        out += plain_panel(se(d["plain_english"]))
    elif d.get("intro"):
        out += para(se(d["intro"]))
    if d.get("key_points"):
        out += "".join(para("&#8226; " + se(p)) for p in d["key_points"])
    out += funding_box(d.get("funding_plan"))
    for i, a in enumerate(d.get("actions", []) or []):
        name = a.get("instrument") or a.get("name") or ""
        out += (f'<div style="border:1px solid {C["border"]};border-radius:6px;padding:12px 16px;margin:12px 0;'
                f'background:{C["bg_panel"]}">')
        out += para(f'{_pill_for(a.get("action"))} &nbsp;{name_cell(name, a.get("ticker"))}')
        if a.get("verdict"):
            out += verdict_line(se(a["verdict"]))
        if a.get("plain_english"):
            out += plain_panel(se(a["plain_english"]))
        _fr = ((data.get("s2_capital_allocation") or {}).get("capital_router") or {}).get("fund_reasons") or {}
        if a.get("ticker") in _fr:                 # ISA-0918: a fund purchase states the allocation rules' reason
            out += labelled("Why this fund:", se(_fr[a["ticker"]]))
        rng = _price_range(a)
        trade = str(a.get("action", "")).strip().upper().split(" ")[0] in ("BUY", "SELL", "ADD", "TRIM")
        rows = [
            ("Company, action and shares", se(a.get("quantity_line") or a.get("size") or "")
             or (_missing() if trade else "&mdash;")),
            ("Acceptable price range", se(rng) if rng else (_missing("Not stated - required before dealing") if trade else "&mdash;")),
            ("Latest prices", se(a.get("prices") or "") or (_missing() if trade else "&mdash;")),
            ("Size and why", se(" ".join(x for x in (a.get("size_tier"), a.get("size_why") or _conviction_shown(a.get("conviction")))
                                     if x))
             or "&mdash;"),
            ("How it is paid for", se(a.get("funding") or "") or (se(((d.get("funding_plan") or {}).get("line")) or "")
                                                                  if trade else "") or (_missing() if trade else "&mdash;")),
            ("All-in cost and readiness", se(" ".join(x for x in (a.get("all_in_cost"), a.get("readiness") or a.get("timing")) if x))
             or "&mdash;"),
        ]
        if trade or any(v != "&mdash;" for _k, v in rows):     # a HOLD with nothing to deal shows no empty sheet
            out += table_start(["Action sheet", "Detail"], ["28%", "72%"], ["l", "l"])
            for j, (k, v) in enumerate(rows):
                out += table_row([f"<strong>{k}</strong>", v], last=(j == len(rows) - 1), align=["l", "l"])
            out += table_end()
        if a.get("note"):
            out += para(se(a["note"]), muted=True, small=True)
        out += "</div>\n"
    if d.get("net_effect"):
        out += labelled("Net effect:", se(d["net_effect"]))
    if d.get("size_diagnostic"):
        sd = d["size_diagnostic"]
        t = table_start(sd.get("columns") or ["Option", "Shares", "Spend", "Resulting holding", "Permission"])
        for j, r in enumerate(sd.get("rows") or []):
            t += table_row([se(str(x)) for x in r], last=(j == len(sd["rows"]) - 1))
        t += table_end() + (para(se(sd.get("note", "")), muted=True, small=True) if sd.get("note") else "")
        out += details("All sizes considered &mdash; diagnostic, not an order", t)
    return out


def _render_capital_router(d):
    """ISA-0447 - the marginal-pound router, rendered inside Section 2 (as technical detail in the report).
    The binding refusals come first; an absent router renders as ABSENT, never as nothing."""
    v = d.get("capital_router") or {}
    if not v:
        return ""
    if v.get("absent_line"):
        return h3("Marginal-pound router") + para(se(v["absent_line"]))
    out = h3("Marginal-pound router &mdash; where this month&#39;s capital is routed")
    for k in ("new_capital_control_line", "fund_leg_line"):
        if v.get(k):
            out += para("<strong>" + se(v[k]) + "</strong>")
    if v.get("decision_engine_line"):
        out += para(se(v["decision_engine_line"]))
    if v.get("concentration_line"):
        _cl = se(v["concentration_line"])
        out += para(("<strong>" + _cl + "</strong>") if "DECISION REQUIRED" in v["concentration_line"] else _cl)
    for k in ("switch_contest_line", "appetite_guard_line"):
        if v.get(k):
            out += para(se(v[k]))
    for k in ("dealability_line", "engine_authority_line"):
        if v.get(k):
            out += para("<strong>" + se(v[k]) + "</strong>")
    if v.get("funding_park_line"):
        out += para(se(v["funding_park_line"]))
    for k in ("split_line", "split_reason_line", "executability_line", "freeze_line", "ranking_line", "c1_line"):
        if v.get(k):
            out += para(se(v[k]), small=(k in ("split_reason_line", "freeze_line", "ranking_line", "c1_line")))
    rows = v.get("allocation_rows") or []
    if v.get("allocation_line"):
        out += para(f'<strong>{se(v["allocation_line"])}</strong>')
    if v.get("fund_reasons"):                      # ISA-0918: the rules' own reason, per funded fund
        out += para("<strong>Why these funds received new money</strong> (the allocation rules decide new money; "
                    "the fund review ranking below only decides which fund is sold first):")
        out += "".join(para("&#8226; " + se(t)) for t in v["fund_reasons"].values())
    if rows:
        out += table_start(["Fund", "Allocated", "Phase", "Weight before", "Weight after", "Declared band", "Why"])
        for i, r in enumerate(rows):
            out += table_row([
                _named(r.get("sedol", "")),
                f'&pound;{(r.get("gbp") or 0):,.2f}',
                se(str(r.get("phase", "-"))),
                ("&mdash;" if r.get("weight_before") is None else f'{r["weight_before"]:.2f}%'),
                ("&mdash;" if r.get("weight_after") is None else f'{r["weight_after"]:.2f}%'),
                se(str(r.get("band", "-"))),
                se(str(r.get("why") or "-")),
            ], last=(i == len(rows) - 1))
        out += table_end()
    for label, key in (("Received nothing", "blocked_rows"), ("Not eligible", "refused_rows")):
        rr = v.get(key) or []
        if rr:
            out += para(f'<strong>{label}:</strong>', small=True)
            out += "".join(para("&#8226; " + se(r), muted=True, small=True) for r in rr)
    for k in ("band_line", "band_choice_line", "idle_line", "residual_routing_line", "waiting_room_line", "parity_line"):
        if v.get(k):
            out += para(se(v[k]), small=(k in ("band_choice_line", "parity_line")))
    for w in (v.get("warnings") or []):
        out += para("<strong>" + se(w) + "</strong>")
    return out


def _alloc_label(v):
    """'Cat 3 - Open new stock positions' -> 'Open new stock positions' (the category number is internal)."""
    return re.sub(r"^Cat\s*\d+\s*[-:]\s*", "", str(v or ""))


def build_s2(data):
    """How this month's money is used: capital tiles, plain English, the binding rules, the options compared."""
    d = data.get("s2_capital_allocation", {})
    out = kpi_row(d.get("kpis", []))
    if d.get("plain_english"):
        out += plain_panel(se(d["plain_english"]))
    _router = _render_capital_router(d)          # ISA-0447 - the binding constraints precede the options they bound
    if _router:
        out += details("How the capital rules applied this month (routing detail)", _router)
    items = d.get("items", [])
    if items:
        out += h3("The alternatives we compared")
        out += table_start(["#", "Alternative", "Amount", "Assessment"], ["5%", "27%", "13%", "55%"], ["l", "l", "r", "l"])
        for i, it in enumerate(items):
            out += table_row([se(it.get("priority", "")),
                              f'{_pill_for(it.get("allocation_type"))}<br><strong>'
                              f'{se(_alloc_label(it.get("allocation")))}</strong>',
                              se(it.get("amount", "—")),
                              (plain_panel(se(it["plain_english"]), "In plain English") if it.get("plain_english") else "")
                              + se(it.get("rationale", ""))],
                             last=(i == len(items) - 1), align=["l", "l", "r", "l"])
        out += table_end()
    out += _summary_lines_html(d, "Cash movements and dealing permissions")
    tech = ""
    if d.get("notes"):
        tech += para(se(d["notes"]), small=True)
    for _line in d.get("standing_lines", []):
        tech += para(f'&#9656; {se(_line)}', small=True)
    out += details("Background figures and working notes", tech)
    return out


_DECIDED = ("BUY", "SELL", "ADD", "TRIM", "WITHHELD", "TOP-UP")


def _conviction_shown(c):
    c = str(c or "")
    return c if c and not re.search(r"retired|n/a|\[Claude", c, re.I) else ""


def _case_card(case, full=True, names=None):
    action = str(case.get("action", "")).upper()
    name = case.get("name") or (names or {}).get(case.get("ticker")) or instrument_name(case.get("ticker")) \
        or case.get("ticker") or ""
    out = (f'<div style="border:1px solid {C["border"]};border-radius:6px;padding:14px 18px;margin:14px 0;'
           f'background:{C["bg_panel"]}">')
    out += para(f'{_pill_for(action)} &nbsp;{name_cell(name, case.get("ticker"))}'
                + (f' &nbsp;<span style="color:{C["text_muted"]};font-size:12px">Conviction '
                   f'{se(_conviction_shown(case.get("conviction")))}</span>' if _conviction_shown(case.get("conviction")) else ""))
    if case.get("verdict"):
        out += verdict_line(se(case["verdict"]))
    if case.get("plain_english"):
        out += plain_panel(se(case["plain_english"]))
    for lab, key in (("What it does.", "what_it_does"), ("Why this company, why now.", "why_now"),
                     ("What the numbers assume.", "model_assumes"), ("Why this size.", "why_this_size"),
                     ("What could go wrong.", "what_could_break"), ("Next check.", "next_check")):
        if case.get(key):
            out += labelled(lab, se(case[key]))
    if full and case.get("analyst_view"):
        out += table_start(["Analysts' view (external)", "Value"], ["55%", "45%"], ["l", "l"])
        rows = case["analyst_view"]
        for j, r in enumerate(rows):
            out += table_row([se(str(r[0])), se(str(r[1]))], last=(j == len(rows) - 1), align=["l", "l"])
        out += table_end()
    if full and case.get("scenarios"):
        out += table_start(["Our scenario", "12-month value", "Basis"], ["20%", "30%", "50%"], ["l", "l", "l"])
        rows = case["scenarios"]
        for j, r in enumerate(rows):
            out += table_row([f"<strong>{se(str(r[0]))}</strong>", se(str(r[1])), se(str(r[2]) if len(r) > 2 else "")],
                             last=(j == len(rows) - 1), align=["l", "l", "l"])
        out += table_end()
    # ISA-0905: the retired /100 conviction score is never shown, even from a legacy skeleton row
    metrics = [m for m in (case.get("metrics_table") or [])
               if str(m.get("label", "")).strip().lower() != "conviction score"]
    ev = ""
    if metrics:
        sig = {"green": C["green"], "amber": C["amber"], "red": C["red"]}
        ev += table_start(["Measure", "Value", "Assessment"], ["40%", "20%", "40%"], ["l", "r", "l"])
        for j, m in enumerate(metrics):
            col = sig.get(m.get("signal", ""), C["text"])
            ev += table_row([se(m.get("label", "")), f'<strong style="color:{col}">{se(m.get("value", ""))}</strong>',
                             f'<span style="color:{col}">{se(m.get("assessment", ""))}</span>'],
                            last=(j == len(metrics) - 1), align=["l", "r", "l"])
        ev += table_end()
    ev += "".join(para(se(p), small=True) for p in (case.get("paragraphs") or []))
    if case.get("sources"):
        ev += para("Sources: " + "; ".join(
            (f'<a href="{se(s.get("url"))}" style="color:{C["gold"]}">{se(s.get("title") or s.get("url"))}</a>'
             if isinstance(s, dict) and s.get("url") else se(str(s.get("title") if isinstance(s, dict) else s)))
            for s in case["sources"]), muted=True, small=True)
    out += details("Full analysis and evidence", ev) if full else ev
    out += "</div>\n"
    return out


def build_s3(data, which=None):
    """Investment cases. which='decided' (actions taken or withheld) | 'considered' (the rest) | None (all)."""
    cases = data.get("s3_investment_cases", []) or []
    if which == "decided":
        cases = [c for c in cases if str(c.get("action", "")).upper() in _DECIDED]
    elif which == "considered":
        cases = [c for c in cases if str(c.get("action", "")).upper() not in _DECIDED]
    if not cases:
        return para("No investment cases in this group this month.", muted=True) if which != "considered" else ""
    names = {}
    for a in (data.get("s1_decision_summary") or {}).get("actions") or []:
        if a.get("ticker") and (a.get("instrument") or a.get("name")):
            names[a["ticker"]] = a.get("instrument") or a.get("name")
    for h in ((data.get("s7_stock_sleeve") or {}).get("holdings") or []) + \
            ((data.get("s5_watchlist") or {}).get("items") or []):
        if h.get("ticker") and h.get("name"):
            names.setdefault(h["ticker"], h["name"])
    return "".join(_case_card(c, full=(which != "considered"), names=names) for c in cases)


def build_s4(data):
    """Positions under review for sale or consolidation."""
    d = data.get("s4_liquidation_tracker", {})
    items = d.get("items", [])
    out = ""
    if items:
        out += table_start(["Holding", "Shares", "Value", "Cost", "Gain / loss", "Action", "Earliest sale", "Why"],
                           ["16%", "7%", "9%", "9%", "9%", "9%", "14%", "27%"], ["l", "r", "r", "r", "r", "l", "l", "l"])
        for i, it in enumerate(items):
            why = it.get("reason")
            why_html = (f'<span style="color:{C["red"]};font-weight:700">reason UNBACKFILLED</span>'
                        if str(why or "").strip() in ("", "—", "-", "TBC", "tbc", "N/A") else se(why))
            if it.get("plain_english"):
                why_html = se(it["plain_english"]) + f'<br><span style="font-size:11px;color:{C["text_muted"]}">{why_html}</span>'
            out += table_row([name_cell(it.get("name"), it.get("ticker")), se(it.get("shares", "—")),
                              se(it.get("current_value", "—")), se(it.get("cost", "—")), se(it.get("gain_loss", "—")),
                              _pill_for(it.get("action")), se(it.get("earliest_sale", "—")), why_html],
                             last=(i == len(items) - 1), align=["l", "r", "r", "r", "r", "l", "l", "l"])
        out += table_end()
    else:
        out += para("No holding is currently a candidate for sale or consolidation.", muted=True)
    if d.get("notes"):
        out += para(se(d["notes"]), muted=True, small=True)
    return out


def build_s5(data):
    """The pipeline: growth watchlist and the asymmetric (VCI) sleeve, each in its own ranked table."""
    d = data.get("s5_watchlist", {})
    out = ""
    items = d.get("items", [])
    if items:
        out += h3("Growth watchlist &mdash; ranked by Source Score")
        out += table_start(["#", "Company", "Sector", "Score", "Expected return", "Trend", "Target buy", "Status"],
                           ["4%", "24%", "13%", "7%", "10%", "10%", "14%", "18%"], ["l", "l", "l", "r", "r", "l", "l", "l"])
        for i, it in enumerate(items):
            src = it.get("source_score", "—")
            out += table_row([se(str(it.get("rank", i + 1))), name_cell(it.get("name"), it.get("ticker")),
                              se(it.get("sector", "—")),
                              f'<strong>{(f"{src:.0f}" if isinstance(src, (int, float)) else se(str(src)))}</strong>',
                              se(str(it.get("expected_return_12_24m", "—"))), se(str(it.get("revision_stage") or "—")),
                              se(it.get("entry_level", "—")),
                              se(it.get("deploy_verdict") or it.get("status", "—"))],
                             last=(i == len(items) - 1), align=["l", "l", "l", "r", "r", "l", "l", "l"])
        out += table_end()
    vci = d.get("vci_sleeve", {}) or {}
    _names = {it.get("ticker"): it.get("name") for it in items if it.get("name")}      # ISA-0898: names, not codes
    vitems = vci.get("items", []) if isinstance(vci, dict) else (vci or [])
    if vitems:
        def _cell(v, mult=False):
            if v in (None, ""):
                return "&mdash;"
            if mult:
                try:
                    return f'{round(float(v), 2)}&times;'
                except (TypeError, ValueError):
                    return se(str(v))
            return se(str(v))
        out += h3("Asymmetric sleeve (VCI) &mdash; ranked by deployability")
        out += table_start(["#", "Company", "Quality score", "Upside multiple", "Minimum required", "Days to catalyst",
                            "Eligible now", "Status"], ["4%", "20%", "10%", "11%", "12%", "11%", "10%", "22%"],
                           ["l", "l", "r", "r", "r", "r", "l", "l"])
        for i, it in enumerate(vitems):
            elig = it.get("deploy_eligible")
            asym = it.get("fv_asymmetry_p25", it.get("fv_asymmetry"))
            out += table_row([
                se(str(it.get("rank", i + 1))), name_cell(it.get("name") or instrument_name(it.get("ticker"))
                                                          or _names.get(it.get("ticker")), it.get("ticker")),
                _cell(it.get("acs")), _cell(asym, mult=True),
                _cell(it.get("fv_floor"), mult=True) + ("<sup>d</sup>" if it.get("fv_floor") not in (None, "")
                                                        and str(it.get("floor_source") or "").startswith("derived")
                                                        else ""),                       # ISA-0823
                _cell(it.get("days_to_catalyst")),
                flag("Yes", "green") if elig is True else ("No" if elig is False else "&mdash;"),
                se(it.get("status", "—"))], last=(i == len(vitems) - 1), align=["l", "l", "r", "r", "r", "r", "l", "l"])
        out += table_end()
        notes = []
        if vci.get("calibration_gate"):
            notes.append(f'Calibration: {se(str(vci["calibration_gate"]))}.')
        if vci.get("risk_committed") is not None and vci.get("risk_budget"):
            notes.append(f'Binary-risk budget used: {round(float(vci["risk_committed"]), 2)}% of '
                         f'{se(str(vci["risk_budget"]))}% of the ISA.')
        notes.append('Upside multiple uses the cautious (25th-percentile) estimate. Minimum required is each name\'s OWN '
                     'eligibility floor: <sup>d</sup> = derived from its probability of success, its loss if wrong and '
                     'its catalyst horizon (names on the same assumptions with no dated catalyst share one value); '
                     'unmarked = the fixed tier (ISA-0823).')
        out += para(" ".join(notes), muted=True, small=True)
    out += _summary_lines_html(d, "Changes to the pipeline")
    ev = ""
    if d.get("excluded"):
        ev += para(f'<strong>Excluded (ineligible):</strong> {se(d["excluded"])}', small=True)
    for det in d.get("detail_items", []):
        ev += h3(name_cell(det.get("name"), det.get("ticker")))
        ev += "".join(para(se(p), small=True) for p in det.get("paragraphs", []))
    out += details("Notes on the leading names", ev)
    return out


_RISK_ROWS = (("whole_isa_risk_line", "How much the portfolio typically moves"),
              ("stress_line", "Severe crash test against your 50% loss limit"),
              ("wealth_line", "Largest fall from a peak, after deposits"),
              ("factor_line", "Exposure to artificial-intelligence companies"),
              ("semis_line", "Exposure to semiconductor companies"),
              ("exposure_line", "How much of the portfolio we can see inside funds"),
              ("scale_line", "Could the holdings be traded at a much larger size?"))


def _risk_status(key, line):
    s = str(line)
    if key == "stress_line":
        return flag("Above your limit", "red") if "BOOK_EXCEEDS_APPETITE" in s else flag("Within your limit", "green")
    if key == "factor_line":
        return (flag("Above the cap", "red") if "BREACH" in s else
                flag("Within the cap", "green") if "OK even at the upper bound" in s else flag("Not established", "amber"))
    if key == "semis_line":
        return (flag("Watch", "red") if "WATCH (" in s else
                flag("Not established", "amber") if ("NOT ESTABLISHED" in s or "UNAVAILABLE" in s) else flag("Within range", "green"))
    if "UNAVAILABLE" in s or "UNKNOWN" in s:
        return flag("Incomplete", "amber")
    return f'<span style="color:{C["text_muted"]}">Measured</span>'


def build_s6(data):
    """How the portfolio did: plain English, tiles, performance against the benchmark, risk and resilience."""
    d = data.get("s6_portfolio_snapshot", {})
    out = ""
    if d.get("plain_english"):
        out += plain_panel(se(d["plain_english"]))
    out += kpi_row(d.get("kpis", []))
    perf = d.get("performance", [])
    if perf:
        out += h3(se(d.get("performance_header", "Portfolio performance against the benchmark")))
        out += table_start(["Period", "Your portfolio", "Benchmark", "Difference"], ["46%", "18%", "18%", "18%"],
                           ["l", "r", "r", "r"])
        for i, r in enumerate(perf):
            _pv = str(r.get("portfolio", ""))
            out += table_row([se(r.get("period", "")), se(_pv) if re.search(r"\d", _pv) else state_cell(_pv),
                              se(r.get("benchmark", "")),
                              se(r.get("alpha", ""))], last=(i == len(perf) - 1), align=["l", "r", "r", "r"])
        out += table_end()
    rows = [(lab, d.get(k), k) for k, lab in _RISK_ROWS if d.get(k)]
    if rows:
        out += h3("Risk and resilience")
        out += table_start(["Question", "What the measurement says", "Status"], ["26%", "58%", "16%"], ["l", "l", "l"])
        for i, (lab, line, k) in enumerate(rows):
            out += table_row([f"<strong>{lab}</strong>", se(str(line)), _risk_status(k, line)],
                             last=(i == len(rows) - 1), align=["l", "l", "l"])
        out += table_end()
    out += _summary_lines_html(d, "Concentration and stage")
    notes = ""
    if d.get("notes"):
        notes += para(se(d["notes"]), small=True)
    if d.get("account_return_line"):
        notes += para(se(d["account_return_line"]), small=True)
    if d.get("factor_unclassified"):
        notes += para("Names awaiting an AI/semiconductor classification: " + se(str(d["factor_unclassified"])), small=True)
    out += details("Notes on these figures", notes)
    return out


def _render_v21(d):
    """ISA-0439 - the V2.1 engine block (technical detail in the shares chapter). A refusal renders AS a refusal."""
    v = d.get("v21") or {}
    if not v:
        return ""
    if v.get("absent_line"):
        return h3("V2.1 engine") + para(se(v["absent_line"]))
    out = h3("V2.1 engine")
    for k in ("policy_line", "ladder_line", "correlation_line", "ratchet_line", "s9_precondition_line", "s9_line",
              "stability_line", "stability_not_an_input_line", "stability_probe_line", "a20_line", "fixture_line",
              "min_hold_line"):
        if v.get(k):
            out += para(se(v[k]), small=(k in ("policy_line", "min_hold_line", "s9_precondition_line")))
    a20 = v.get("a20_rows") or []
    if a20:
        out += table_start(["Incumbent", "Challenger", "Verdict", "Raw gap", "Bar", "Adjusted"])
        fmt = (lambda x: "&mdash;" if x is None else f"{x:+.2f}pp")
        for i, r in enumerate(a20):
            out += table_row([se(str(r.get("incumbent") or "—")), se(str(r.get("challenger") or "—")),
                              se(str(r.get("verdict", ""))), fmt(r.get("raw_gap")),
                              ("&mdash;" if r.get("bar") is None else f"{r['bar']:.2f}pp"), fmt(r.get("advantage"))],
                             last=(i == len(a20) - 1))
        out += table_end()
    stab = v.get("stability_rows") or []
    if stab:
        out += table_start(["Perturbation", "Churn (&pound;)", "Churn %", "Destinations", "Order"])
        for i, r in enumerate(stab):
            out += table_row([se(str(r.get("perturbation", ""))), f"{r.get('churn_gbp', 0):,.0f}",
                              f"{r.get('churn_pct', 0):.1f}%", ("CHANGED" if r.get("destinations_changed") else "same"),
                              ("CHANGED" if r.get("order_changed") else "same")], last=(i == len(stab) - 1))
        out += table_end()
    s9 = v.get("s9_rows") or []
    if s9:
        out += table_start(["Fund", "Compared with", "Shortfall vs comparator", "Months", "Episodes", "State", "Action"])
        for i, r in enumerate(s9):
            out += table_row([_named(r.get("sedol", "")), se(str(r.get("comparator", ""))),
                              f"{r.get('drawdown_pct', 0):.1f}%", str(r.get("months", "")), str(r.get("episodes", "")),
                              state_cell(r.get("state", "")), state_cell(r.get("action", ""))], last=(i == len(s9) - 1))
        out += table_end()
    for k in ("correlation_short", "ratchet_excluded"):
        for r in (v.get(k) or []):
            out += para("&#8226; " + se(r), muted=True, small=True)
    rows = v.get("risk_monitor_rows") or []
    if rows:
        out += table_start(["Monitor", "Verdict", "Reading"])
        for i, r in enumerate(rows):
            out += table_row([se(r.get("measure", "")), se(r.get("verdict", "")), se(r.get("detail", ""))],
                             last=(i == len(rows) - 1))
        out += table_end()
    for w in (v.get("warnings") or []):
        out += para("<strong>" + se(w) + "</strong>")
    return out


_INTEGRITY_DROP = ("Correlation coverage:",)            # ISA-0899: the per-name list belongs to the dashboard


def build_s7(data):
    """Your shares: every held company, the alerts on them, original against current expectations."""
    d = data.get("s7_stock_sleeve", {})
    out = ""
    if d.get("plain_english"):
        out += plain_panel(se(d["plain_english"]))
    out += kpi_row(d.get("kpis", []))
    holdings = d.get("holdings", [])
    if holdings:
        out += table_start(["Company", "Shares", "Value", "Cost", "Gain", "Weight", "Analysts", "Next results",
                            "Score", "Our view"], ["17%", "6%", "9%", "9%", "7%", "7%", "9%", "10%", "6%", "20%"],
                           ["l", "r", "r", "r", "r", "r", "l", "l", "r", "l"])
        for i, h in enumerate(holdings):
            nf = _note_fields(h.get("status_note"))
            analyst = h.get("analyst") or nf.get("analyst") or "\u2014"
            nxt = h.get("next_earnings") or nf.get("next earnings") or "\u2014"
            score = h.get("score_display") or nf.get("score") or "\u2014"
            view = h.get("thesis_plain") or h.get("disposition")
            view_html = (se(view) if view else
                         f'{_pill_for(h.get("status"))}<br><span style="font-size:11px;color:{C["text_muted"]}">'
                         f'thesis update pending</span>')
            out += table_row([name_cell(h.get("name"), h.get("ticker")), se(h.get("shares", "\u2014")),
                              se(h.get("value", "\u2014")), se(h.get("cost", "\u2014")), se(h.get("gain_pct", "\u2014")),
                              se(h.get("weight_pct", "\u2014")), se(str(analyst).replace("_", " ")), se(str(nxt)),
                              se(str(score)), view_html], last=(i == len(holdings) - 1),
                             align=["l", "r", "r", "r", "r", "r", "l", "l", "r", "l"])
        out += table_end()
    out += _summary_lines_html(d, "Price and earnings alerts")
    hu = d.get("held_underwriting") or {}
    if hu.get("rows"):
        out += h3("What we expected at purchase, and what we expect now")
        out += table_start(["Company", "Expected return at purchase", "Expected now", "Change", "Thesis", "Status"],
                           ["16%", "17%", "15%", "18%", "18%", "16%"], ["l", "r", "r", "l", "l", "l"])
        for i, r in enumerate(hu["rows"]):
            out += table_row([name_cell(instrument_name(r.get("ticker")) or next(
                                  (h.get("name") for h in holdings if h.get("ticker") == r.get("ticker")), None), r.get("ticker")),
                              state_cell(r.get("original")), state_cell(r.get("current")), state_cell(r.get("delta")),
                              state_cell(r.get("thesis")), state_cell(r.get("validity"))],
                             last=(i == len(hu["rows"]) - 1), align=["l", "r", "r", "l", "l", "l"])
        out += table_end()
        if hu.get("line"):
            out += para(se(hu["line"]), muted=True, small=True)
    cf = d.get("vuag_counterfactual") or {}
    ch = cf.get("challengers") or {}
    if ch.get("vs_vuag_pp") is not None:
        out += h3("Your shares against simple index alternatives (since the sleeve began)")
        rows = [("Against the Vanguard S&amp;P 500 tracker (VUAG)", ch.get("vs_vuag_pp")),
                ("Against the iShares world momentum tracker (IWMO)", ch.get("vs_iwmo_pp")),
                ("Excluding Micron, against VUAG", ch.get("vs_vuag_exmu_pp")),
                ("Excluding Micron, against IWMO", ch.get("vs_iwmo_exmu_pp"))]
        out += table_start(["Comparison", "Difference"], ["70%", "30%"], ["l", "r"])
        rows = [r for r in rows if r[1] is not None]
        for i, (lab, v) in enumerate(rows):
            out += table_row([lab, f"{v:+.1f} percentage points"], last=(i == len(rows) - 1), align=["l", "r"])
        out += table_end()
    elif cf.get("line"):
        out += para(se(cf["line"]))
    if d.get("gold_pilot_line"):
        out += para(f'<strong>{se(d["gold_pilot_line"])}</strong>')
    tech = _render_v21(d)                                # ISA-0439 - live, as technical detail
    if d.get("notes"):
        tech += para(se(d["notes"]), small=True)
    for _il in d.get("integrity_lines") or []:
        if not str(_il).startswith(_INTEGRITY_DROP):
            tech += para(se(_il), muted=True, small=True)
    if hu.get("rows"):
        tech += para("Current underwriting cases: " + "; ".join(
            f"{se(r.get('ticker'))} {se(r.get('case'))}" for r in hu["rows"] if r.get("case")), muted=True, small=True)
    if cf.get("line"):
        tech += para(se(cf["line"]), muted=True, small=True)
    if d.get("probation_rule"):
        tech += para(se(d["probation_rule"]), muted=True, small=True)
    for pw in d.get("provenance_warnings") or []:
        tech += para("<strong>" + se(pw) + "</strong>", small=True)
    out += details("Technical detail: sizing engine, coverage and data integrity", tech)
    return out


def build_s8(data):
    """Your funds: holdings against their bands, the review order, the systematic summary, structure."""
    d = data.get("s8_fund_review", {})
    out = ""
    if d.get("plain_english"):
        out += plain_panel(se(d["plain_english"]))
    funds = d.get("funds", [])
    if funds:
        out += h3("Your funds against their permitted ranges")
        out += table_start(["Fund", "Value", "Weight", "Permitted range (target)", "Drift", "Last 12 months",
                            "Morningstar", "Signal"], ["24%", "10%", "8%", "20%", "8%", "12%", "8%", "10%"],
                           ["l", "r", "r", "l", "r", "r", "l", "l"])
        for i, f in enumerate(funds):
            lo, hi = _band_of(f.get("band"))
            w, tgt = _pct_of(f.get("weight_pct")), _pct_of(f.get("target_pct"))
            val, src = _dated_return(f.get("perf_1yr"))
            out += table_row([
                name_cell(f.get("name"), f.get("ticker")), se(f.get("value", "—")), se(f.get("weight_pct", "—")),
                f'{se(f.get("band", "—"))} ({se(f.get("target_pct", "—"))}){band_bar(w, lo, hi, tgt)}',
                se(f.get("drift", "—")),
                se(val) + (f'<br><span style="font-size:11px;color:{C["text_muted"]}">{se(src)}</span>' if src else ""),
                ms_stars(f.get("ms_rating")), se(str(f.get("signal", "—")).title())],
                last=(i == len(funds) - 1), align=["l", "r", "r", "l", "r", "r", "l", "l"])
        out += table_end()
        out += para("The bar shows each fund's permitted range (shaded), its target (thin line) and where it sits now "
                    "(green inside the range, red outside).", muted=True, small=True)
    fas = d.get("action_stack") or {}
    if fas.get("available"):
        out += h3("Fund review &mdash; the order in which funds would be reduced if money were needed")
        out += para("This ranking is a retention score built from past performance. It decides only which fund is sold "
                    "first if money is needed - past performance has not predicted these funds' future returns "
                    "(measured Aug-2026), so it does not decide where new money goes. New money follows the allocation "
                    "rules; their reason for each fund is in section 2.", muted=True, small=True)
        out += para(f"<strong>{se(fas.get('headline', ''))}</strong>"
                    + (f" &nbsp;&middot;&nbsp; as at {se(fas.get('as_of', ''))}" if fas.get("as_of") else ""), small=True)
        band_pill = {"HOLD/ADD": "buy", "RETAIN-ONLY": "mon", "WINDOW_SPLIT": "warn", "DEAD MONEY": "sell", "UNSCORED": "grey"}
        out += table_start(["Order", "Fund", "Retention band", "Score", "1 yr", "3 yr", "5 yr", "10 yr", "Periods agree?",
                            "Why it sits here", "If money is needed"],
                           ["5%", "17%", "9%", "6%", "6%", "6%", "6%", "6%", "8%", "18%", "13%"],
                           ["l", "l", "l", "r", "r", "r", "r", "r", "l", "l", "l"])
        rows = fas.get("rows", [])
        for i, r in enumerate(rows):
            wd = _windows_of(r.get("windows"))
            out += table_row([
                str(r.get("rank", "")), name_cell(r.get("name"), r.get("sedol")),
                pill(str(r.get("band", "")).replace("_", " "), band_pill.get(r.get("band"), "grey")),
                ("&mdash;" if r.get("frs") is None else f"{r['frs']:.1f}"),
                *[se(wd.get(k, "—")) for k in ("1y", "3y", "5y", "10y")],
                "No" if r.get("window_split") else "Yes",
                se(str(r.get("donor_why") or "—")), se(str(r.get("action", "")))],
                last=(i == len(rows) - 1), align=["l", "l", "l", "r", "r", "r", "r", "r", "l", "l", "l"])
        out += table_end()
        for _tdx in fas.get("trust_discounts") or []:
            if _tdx.get("discount_pct") is None:
                out += para(f'<strong style="color:{C["amber"]}">&#9888; {se(str(_tdx.get("name")))}: closed-end discount '
                            f'UNMEASURED</strong> (ISA-0127)', small=True)
                continue
            _pc = _tdx.get("percentile")
            _ord = (lambda n: "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")))
            out += para(f"<strong>{se(str(_tdx.get('name')))}:</strong> trades at a "
                        f"{abs(_tdx['discount_pct']):.1f}% {'discount' if _tdx['discount_pct'] < 0 else 'premium'} to the "
                        f"value of its assets (as at {se(str(_tdx.get('as_of')))})"
                        + (f", the {_ord(int(round(_pc)))} percentile of {_tdx.get('n')} observations (0 = widest)"
                           if _pc is not None else "")
                        + (f"; selling now would give up &pound;{abs(_tdx['crystallisation_gbp']):,.0f} of underlying value"
                           if _tdx.get("crystallisation_gbp") else "") + ".", small=True)
        _nf = fas.get("nav_freshness") or {}
        if _nf.get("headline"):
            _bad = bool(_nf.get("not_fresh")) or not _nf.get("available")
            out += para((f'<strong style="color:{C["amber"]}">&#9888; ' if _bad else "<strong>") + se(_nf["headline"])
                        + "</strong>", small=True)
        for f in fas.get("anchor_failures", []):
            out += para(f"<strong>Below its minimum:</strong> {se(str(f.get('name')))} returned "
                        f"{f.get('realised_5y_ann'):.2f}% a year over five years, {f.get('shortfall_pp')} percentage points "
                        f"below the {f.get('bucket_minimum_pct'):.1f}% its role requires &mdash; a review item, not a silent hold.")
        for nm in fas.get("disputed", []):
            out += para(f"<strong>Sources disagree:</strong> {se(str(nm))} &mdash; the two sources fall on opposite sides "
                        f"of its minimum on the same date; no verdict is published.", muted=True, small=True)
        for w in fas.get("window_conflicts", []):
            out += para(f"<strong>No ranking between</strong> {se(str(w.get('fund_name')))} and "
                        f"{se(str(w.get('versus_name')))}: which is better flips between the 3-year and 5-year periods, so "
                        f"neither is called the winner.", muted=True, small=True)
    elif fas:
        out += para(f"<strong>Fund review unavailable.</strong> {se(str(fas.get('note', '')))}", muted=True)
    s8a = d.get("step8a_summary", {})
    if s8a:
        out += h3("Is the portfolio on course? &mdash; the systematic summary")
        sa, sb, sc = s8a.get("section_a", {}), s8a.get("section_b", {}), s8a.get("section_c", {})
        rows = []

        def _res(v):
            r = str(v or "")
            col = ("green" if r in ("PASS", "On track") else "amber" if r.upper().startswith(("INCONCL", "WATCH", "INDICATIVE"))
                   else "red" if r in ("FAIL", "Flag") else None)
            return flag(r, col) if col else se(r)
        if sa:
            bands = str(sa.get("bands_note") or "")
            if "None" in bands:
                bands = "comparison bands UNAVAILABLE this run"
            rows.append(("Funds: weighted average return", _res(sa.get("result")), se(sa.get("value", "")), se(bands)))
        if sb:
            rows.append(("Shares: aggregate return", _res(sb.get("result")), se(sb.get("value", "")),
                         "the 18% target for the share sleeve"))
        if sc:
            rows.append(("Whole ISA: expected return", _res(sc.get("result")), se(sc.get("value", "")),
                         se(sc.get("anchor_note", "the required return"))))
        out += table_start(["Measure", "Result", "Value", "Compared with"], ["26%", "16%", "30%", "28%"], ["l", "l", "l", "l"])
        for i, r in enumerate(rows):
            out += table_row(list(r), last=(i == len(rows) - 1), align=["l", "l", "l", "l"])
        out += table_end()
        if str(sa.get("fund_cache_status", "")).upper() == "DEGRADED":
            out += para(f'<strong style="color:{C["red"]}">&#9888; fund return data DEGRADED</strong> &mdash; the fund and '
                        f'whole-ISA verdicts are low-confidence until the fund return cache is refreshed.', small=True)
        att = sc.get("attribution") or []
        if att:
            out += table_start(["Holding", "Weight", "Expected", "Pull on the required return"], None, ["l", "r", "r", "r"])
            for j, a in enumerate(att):
                out += table_row([_named(a.get("asset_id")) if instrument_name(a.get("asset_id")) else se(a.get("asset_id")),
                                  f'{a.get("weight_of_covered_pct"):.2f}%', f'{a.get("er_pct"):.1f}%',
                                  f'{a.get("contribution_to_shortfall_pp"):+.2f}pp'], last=(j == len(att) - 1))
            out += table_end()
        for l in (sc.get("levers") or []):
            out += para(f'&bull; <strong>{se(l.get("lever"))}</strong>: {l.get("delta_pp"):+.2f} percentage points &mdash; '
                        f'{se(l.get("mechanism", ""))}', small=True)
        for l in (sc.get("levers_blocked") or []):
            out += para(f'&bull; <strong>{se(l.get("lever"))}</strong>: {flag("Blocked", "amber")} &mdash; '
                        f'{se(l.get("blocked_reason", ""))}', small=True)
        for key, lab in (("overlap_check", "Overlap between funds and your shares"), ("regime", "Market regime and tilt"),
                         ("alt_research", "Alternative fund research")):
            if s8a.get(key):
                out += labelled(lab + ":", se(s8a[key]))
    out += _summary_lines_html(d, "How the fund sleeve is built")
    tech = ""
    if d.get("xray_summary"):
        tech += para(f'<strong>X-Ray sector weights:</strong> {se(d["xray_summary"])}', small=True)
    for f in funds:
        if f.get("est_return"):
            tech += para(f'{se(f.get("name"))}: legacy estimated return {se(f["est_return"])}', muted=True, small=True)
    if funds and funds[0].get("perf_1yr_basis"):
        tech += para("Last-12-months basis: " + se(funds[0]["perf_1yr_basis"]), muted=True, small=True)
    if (fas.get("donor_ordering") or {}).get("basis"):
        tech += para("Review order basis: " + se(str(fas["donor_ordering"]["basis"])), muted=True, small=True)
    if fas.get("method_note"):
        tech += para(se(fas["method_note"]), muted=True, small=True)
    if (sc if s8a else {}).get("basis"):
        tech += para("Whole-ISA expected return basis: " + se(sc["basis"]), muted=True, small=True)
    out += details("Technical detail: methods and legacy figures", tech)
    return out


def build_s9(data):
    """Markets and the economy: what happened, what it means for you, what would make us act."""
    d = data.get("s9_macro", {})
    items = d.get("items", [])
    if not items:
        return para("No material market or economic development this month.", muted=True)
    out = ""
    if any(it.get("so_what") or it.get("trigger") for it in items):
        out += table_start(["Development", "What it means for you", "What would make us act"], ["26%", "44%", "30%"],
                           ["l", "l", "l"])
        for i, it in enumerate(items):
            out += table_row([f"<strong>{se(it.get('title', ''))}</strong>", se(it.get("so_what") or "—"),
                              se(it.get("trigger") or "—")], last=(i == len(items) - 1), align=["l", "l", "l"])
        out += table_end()
    ev = ""
    for it in items:
        ev += h3(se(it.get("title", "")))
        for p_text in it.get("paragraphs", [it.get("body", "")]):
            if p_text:
                ev += para(se(p_text), small=True)
    out += details("The developments in detail", ev, open_=not out)
    return out


def build_s10(data):
    """Allowance, cash and costs."""
    d = data.get("s10_tax_tracker", {})
    out = kpi_row(d.get("kpis", []))
    items = d.get("items", [])
    if items:
        col = {"done": C["green"], "pending": C["amber"], "scheduled": C["blue"]}
        out += h3("Money paid in this tax year")
        out += table_start(["Date and method", "Amount", "Status"], ["60%", "20%", "20%"], ["l", "r", "l"])
        for i, it in enumerate(items):
            out += table_row([se(it.get("component", "")), se(it.get("amount", "—")),
                              f'<span style="color:{col.get(it.get("status_type", "done"), C["text"])}">'
                              f'{se(it.get("status", ""))}</span>'], last=(i == len(items) - 1), align=["l", "r", "l"])
        out += table_end()
    if d.get("notes"):
        out += para(se(d["notes"]), small=True)
    out += _summary_lines_html(d, "Cash statement and dealing")
    return out


def build_s11(data):
    """What we learned: retrospective items, each with a plain-English line."""
    d = data.get("s11_retrospective", {})
    items = d.get("items", [])
    out = ""
    if d.get("override_summary"):
        out += labelled("Overrides of the framework:", se(d["override_summary"]))
    impact = {"XL": (C["impact_xl_bg"], C["impact_xl_fg"]), "L": (C["impact_l_bg"], C["impact_l_fg"]),
              "M": (C["impact_m_bg"], C["impact_m_fg"]), "S": (C["impact_s_bg"], C["impact_s_fg"]),
              "XS": (C["impact_xs_bg"], C["impact_xs_fg"])}
    cats = {"accuracy": (C["cat_acc_bg"], C["cat_acc_fg"]), "completeness": (C["cat_com_bg"], C["cat_com_fg"]),
            "efficiency": (C["cat_eff_bg"], C["cat_eff_fg"]), "effectiveness": (C["cat_eff2_bg"], C["cat_eff2_fg"])}

    def badge(text, bg, fg):
        return (f'<span style="display:inline-block;padding:2px 8px;border-radius:11px;font-size:11px;font-weight:700;'
                f'background:{bg};color:{fg}">{se(text)}</span>')
    for idx, it in enumerate(items, 1):
        cat = str(it.get("category", "")).lower()
        imp = str(it.get("impact", "")).upper()
        cb, cf = cats.get(cat, (C["pill_grey_bg"], C["pill_grey_fg"]))
        ib, iff = impact.get(imp, (C["impact_s_bg"], C["impact_s_fg"]))
        out += (f'<div style="border-left:3px solid {C["gold"]};padding:8px 14px;margin:12px 0;background:{C["bg_panel"]}">'
                + para(f'<strong>{idx}. {se(it.get("title", ""))}</strong> &nbsp;{badge(cat.title() or "-", cb, cf)} '
                       f'&nbsp;{badge("Impact: " + (imp or "-"), ib, iff)}'))
        if it.get("plain_english"):
            out += para(se(it["plain_english"]))
        if it.get("problem"):
            out += labelled("Problem:", se(it["problem"]))
        if it.get("action"):
            out += labelled("Action:", se(it["action"]))
        out += "</div>\n"
    if not items:
        out += para("No retrospective items this month.", muted=True)
    tech = ""
    if d.get("ledger_echo"):
        tech += para(se(d["ledger_echo"]), muted=True, small=True)
    for _il in d.get("integrity_lines") or []:
        tech += para(se(_il), muted=True, small=True)
    out += details("Framework health (technical)", tech)
    return out


def build_legend():
    """Glossary: the scales and terms the report uses. (Raj 26-Oct-2026: the /100 conviction scale and the five
    investment categories are removed - the conviction score is retired from sizing and capital (D21; evidence_state)
    and no step uses the categories.)"""
    out = h3("Position sizes for individual shares")
    rows = [("Starting stake", "3.5%", "A new company with a sound case but thin evidence so far."),
            ("Normal", "4.5%", "The case has been confirmed by new evidence."),
            ("High", "5.5%", "Strong, repeated confirmation."),
            ("Earned maximum", "6.5%", "The ceiling for any one company; reached only on evidence, never on price alone.")]
    out += table_start(["Step", "Share of the ISA", "When"], ["22%", "18%", "60%"], ["l", "r", "l"])
    for i, r in enumerate(rows):
        out += table_row([f"<strong>{r[0]}</strong>", r[1], r[2]], last=(i == len(rows) - 1), align=["l", "r", "l"])
    out += table_end()
    out += h3("Terms")
    terms = [("Source Score", "The framework's 0-100 ranking of a company from the weekly screens; it orders the watchlist."),
             ("Expected return", "The framework's central estimate of the 12-month return, before it is checked by a person."),
             ("Permitted range", "The band of the portfolio each fund may occupy; outside it, the fund is reviewed."),
             ("Asymmetric sleeve (VCI)", "Small, high-risk positions where the possible gain is many times the possible loss."),
             ("Percentage points", "The difference between two percentages (12% vs 10% is 2 percentage points)."),
             ("Cash fund", "A money-market fund that holds cash-like deposits; treated as cash by the framework.")]
    out += table_start(["Term", "Meaning"], ["26%", "74%"], ["l", "l"])
    for i, r in enumerate(terms):
        out += table_row([f"<strong>{r[0]}</strong>", r[1]], last=(i == len(terms) - 1), align=["l", "l"])
    out += table_end()
    return out


def build_header(meta):
    """Report masthead. The anchor/contribution status line (ISA-0902: it was printed twice) now sits once, as
    technical detail in the decision chapter, and in plain words as the 'growth needed' tile."""
    meta_line = " &nbsp;&middot;&nbsp; ".join(x for x in (
        f'Prepared {se(meta.get("run_date_display"))}' if meta.get("run_date_display") else "",
        f'Data as at {se(meta.get("data_date"))}' if meta.get("data_date") else "",
        se(meta.get("broker") or ""),
        f'Tax year {se(meta.get("tax_year"))} {se(meta.get("tax_year_month") or "")}'.strip() if meta.get("tax_year") else "",
    ) if x)
    return (eyebrow("Monthly ISA Portfolio Review" + (" &middot; " + se(meta.get("run_month_label"))
                                                      if meta.get("run_month_label") else ""))
            + h1("Your portfolio report")
            + para(meta_line, muted=True, small=True))


def build_footer(meta):
    run_date = se(meta.get("run_date_display", ""))
    data_date = se(meta.get("data_date", ""))
    broker = se(meta.get("broker", ""))
    return (
        f'<hr style="border:none;border-top:1px solid {C["border"]};margin:24px 0 10px 0" />\n'
        f'<p style="font-size:11px;line-height:1.6;color:{C["text_muted"]};text-align:center;margin-top:12px;'
        f'font-family:{SANS}">'
        f'Prepared automatically by the Monthly ISA Portfolio Review'
        f'{(" &middot; " + run_date) if run_date else ""}'
        f'{(" &middot; " + broker) if broker else ""}'
        f'{(" &middot; data as at " + data_date) if data_date else ""}<br>'
        f'A personal financial record, not investment advice. '
        f'{_compliance_footer()}'
        + (f'<br><strong style="color:{C["amber"]}">{se(meta["conviction_gate_override"])}</strong>'
           if meta.get("conviction_gate_override") else "")
        + '</p>\n')


def _compliance_footer():
    """Regime-driven execution reminder (compliance.py is authoritative -- never hardcode Citi)."""
    try:
        import compliance
        return compliance.execution_reminder()
    except Exception:
        # Fail SAFE: if the module cannot be imported, state the restrictive rule.
        return "Citi preclearance required before executing any individual stock trade."


# ---------------------------------------------------------------------------
# Section dispatch (data keys -> builders; chapters compose them)
# ---------------------------------------------------------------------------
SECTION_BUILDERS = {
    "s1_decision_summary":   build_s1,
    "s2_capital_allocation": build_s2,
    "s3_investment_cases":   build_s3,
    "s4_liquidation_tracker": build_s4,
    "s5_watchlist":          build_s5,
    "s6_portfolio_snapshot": build_s6,
    "s7_stock_sleeve":       build_s7,
    "s8_fund_review":        build_s8,
    "s9_macro":              build_s9,
    "s10_tax_tracker":       build_s10,
    "s11_retrospective":     build_s11,
}


# ---------------------------------------------------------------------------
# Run manifest block (Capture Layer Item 2) - in the learning chapter
# ---------------------------------------------------------------------------
def build_manifest_block(data):
    """Render the pre-run's own vital signs. Absence renders NOTHING rather than a reassuring green bar."""
    mf = data.get("run_manifest")
    if not mf:
        path = (data.get("meta", {}) or {}).get("run_manifest_path")
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    mf = json.load(f)
            except Exception:
                mf = None
    if not mf:
        return ""
    try:
        import run_manifest as _rm
        return details("Pre-run vital signs (technical)", _rm.email_block_html(mf), open_=False)
    except Exception:
        st = mf.get("run_status", "UNKNOWN")
        c = mf.get("counts", {}) or {}
        return para(f'<strong>RUN {se(str(st))}</strong> &mdash; {c.get("OK", 0)} ok / {c.get("DEGRADED", 0)} degraded / '
                    f'{c.get("ERROR", 0)} error', small=True)


# ---------------------------------------------------------------------------
# DELIVERY (ISA-0898, replacing H11 section triage). The August-2026 report could not be SENT (195,926 bytes
# against a ~64KB transport limit); H11 answered with style compression and, failing that, dropping whole
# sections - October's lean copy dropped s6 and s7. ISA-0898 removes the trade-off: the EMAIL is a decision
# brief, built to fit, and the complete REPORT is a web page (published privately, or attached). Nothing is
# ever dropped from the report; the brief carries every warning row of it, verbatim (ISA-0642 subset proof).
# ---------------------------------------------------------------------------
LEAN_DEFAULT_BUDGET = 65536          # bytes; the transport limit the Aug send hit
LEAN_NEVER_OMIT = ("s1_decision_summary",)      # the decision is the first thing in the brief, always


_TAG_RE = re.compile(r"</?[A-Za-z!][^>]*>")      # a real tag; '<10.8%' in a sentence is text, not a tag


def _text_of(html):
    """Visible text, for proving two renderings say the same thing."""
    t = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = _TAG_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ⚑ ISA-0642 B10-R05 (1): a WARNING is any block-level row (<p>, <li>, <tr>) whose visible text carries one of
#   these state words - or (ISA-0898) which carries a red or amber flag. Mechanical, so the brief can be PROVEN to
#   carry every one: the warning set of the report must be a subset of the brief's (lean_report.warnings_coverage).
LEAN_WARNING_RE = re.compile(r"\b(REFUSED\w*|UNKNOWN|UNVERIFIED\w*|DEGRADED|CONFIRM AT DEALING|CONFIRM_AT_DEALING|"
                             r"BLOCKED|STALE|UNRECONCILED|INVALIDATED|NOT_DEALABLE\w*|ABSENT|"
                             r"BREACH\w*|\w*EXCEEDS_APPETITE|REVIEW_REQUIRED|REQUIRES_\w+|EXPIRED_\w+|MISMATCH|"
                             r"NOT ESTABLISHED|UNMEASURED|\w*UNEVALUATED|FAIL(?:ED|S)?)\b")
_LEAN_BLOCK_RE = re.compile(r"<(p|li|tr)\b[^>]*>(.*?)</\1>", re.S | re.I)
# ⚑ ISA-0898: a red or amber <strong> anywhere in the row is a warning (a status flag in a table, an alert line).
#   A GREEN verdict is a clean result and is NOT a warning - the brief need not repeat every 'within your limit'.
_LEAN_VERDICT_RE = re.compile(r'<strong style="color:(%s|%s)"' % (re.escape(C["red"]), re.escape(C["amber"])))


def _warning_rows(html_text):
    """[(text, colour or None)] - the warning rows of a body, in order, de-duplicated. Nested rows (a <p> inside a
    <td>) are read at the innermost block, so a table row and its own paragraph are one warning, not two."""
    import html as _h
    out, seen = [], set()
    for m in _LEAN_BLOCK_RE.finditer(html_text or ""):
        inner = m.group(2)
        if m.group(1).lower() == "tr" and re.search(r"<(p|li)\b", inner, re.I):
            continue                                    # its inner paragraphs are the rows
        t = re.sub(r"\s+", " ", _h.unescape(_TAG_RE.sub(" ", inner))).strip()
        if not t or t in seen:
            continue
        vm = _LEAN_VERDICT_RE.search(inner)
        if LEAN_WARNING_RE.search(t) or vm:
            seen.add(t)
            out.append((t, vm.group(1) if vm else None))
    return out


def _block_texts(html_text):
    """Every block row's normalised visible text (the brief's side of the subset proof)."""
    import html as _h
    return [re.sub(r"\s+", " ", _h.unescape(_TAG_RE.sub(" ", m.group(2)))).strip()
            for m in _LEAN_BLOCK_RE.finditer(html_text or "")]


def warning_lines(html_text):
    """The warning rows of a rendered body, as normalised visible text, in order, de-duplicated."""
    return [t for t, _ in _warning_rows(html_text)]


def report_counts_by_chapter(report_html):
    """{chapter: warning rows} measured on the RENDERED report, split at each chapter anchor (independent of the
    brief's own computation)."""
    import html as _h
    marks = [(report_html.find('id="%s"' % k), k) for k, _ in CHAPTERS]
    marks = sorted((p, k) for p, k in marks if p >= 0)
    out, seen = {}, set()
    for n, (p, k) in enumerate(marks):
        if k in ("attention", "glossary"):
            continue
        seg = report_html[p: marks[n + 1][0] if n + 1 < len(marks) else len(report_html)]
        for t, _c in _warning_rows(seg):
            if t not in seen:
                seen.add(t)
                out[k] = out.get(k, 0) + 1
    return out


def _with_warning_coverage(data, body, rep, report_html=None):
    """ISA-0642 as amended by Raj 26-Oct-2026 ("the section with lots of verbiage serves no purpose"). The REPORT is
    always complete - nothing is ever omitted from it - so no warning can be lost with a section; the BRIEF states, per
    chapter, how many items the report flags. Proof: the counts the brief states equal the counts measured on the
    rendered report, chapter by chapter."""
    import html as _h
    full = report_counts_by_chapter(report_html if report_html is not None else build_report_body(data))
    stated = {}
    for t in _block_texts(body):
        for k, title in CHAPTERS:
            m = re.fullmatch(re.escape(_h.unescape(title)) + r" (\d+)", t)
            if m:
                stated[k] = int(m.group(1))
    missing = ["%s: report %d, brief %s" % (k, n, stated.get(k)) for k, n in full.items() if stated.get(k) != n]
    missing += ["%s: brief states %d, report has none" % (k, n) for k, n in stated.items() if k not in full]
    rep["warnings_coverage"] = {"n_full": sum(full.values()), "n_lean": sum(stated.values()), "by_chapter": full,
                                "missing": missing, "ok": not missing,
                                "rule": "per chapter, the flagged-item count the brief states equals the count on the "
                                        "rendered report (the report is never abridged)"}
    return rep


def build_lean_body(data, budget=LEAN_DEFAULT_BUDGET, full_path=None, report_url=None):
    """-> (brief, report). The brief is built once and MEASURED against the transport budget; nothing is dropped
    to make it fit - a brief over budget is reported (within_budget False) and the caller must not assume the send."""
    report = build_report_body(data)
    body = build_brief(data, report_ref=report_url or full_path, report_html=report)
    n = len(body.encode())
    rep = {"bytes_final": n, "budget": budget, "within_budget": n <= budget, "sections_included": sorted(SECTION_BUILDERS),
           "sections_omitted": [], "full_bytes": len(report.encode()), "mode": "brief+report (ISA-0898)",
           "report_ref": report_url or full_path}
    return body, _with_warning_coverage(data, body, rep, report_html=report)


def build_email_body(data, lean=False, budget=LEAN_DEFAULT_BUDGET, full_path=None, lean_report=None,
                     include_sections=None, report_url=None):
    """lean=False -> the complete report (fragment); lean=True -> the brief. `include_sections` is accepted for
    compatibility and ignored: no section is ever omitted (ISA-0898)."""
    if not lean:
        return build_report_body(data)
    body, rep = build_lean_body(data, budget=budget, full_path=full_path, report_url=report_url)
    if lean_report is not None:
        lean_report.update(rep)
    return body


# ---------------------------------------------------------------------------
# ISA-0900 - account-level reporting measures (computed by report_measures.py in the pre-run; rendered here)
# ---------------------------------------------------------------------------
def _gbp(v, signed=False):
    if v is None:
        return "&mdash;"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return se(str(v))
    s = f"&pound;{abs(v):,.2f}"
    if v < 0:
        return f'<span style="color:{C["red"]}">&minus;{s}</span>'
    return ("+" + s) if (signed and v > 0) else s


def _pct(v, signed=True, dp=1):
    if v is None:
        return "&mdash;"
    v = float(v)
    s = f"{abs(v):.{dp}f}%"
    if v < 0:
        return f'<span style="color:{C["red"]}">&minus;{s}</span>'
    return ("+" if signed and v > 0 else "") + s


def _d(iso):
    try:
        import datetime as _dt
        return _dt.date.fromisoformat(str(iso)[:10]).strftime("%-d %b %Y")
    except Exception:                                                   # noqa: BLE001
        return se(str(iso or "")) or "&mdash;"


_BRIDGE_STATE = {"OK": ("Reconciles", "green"), "PROVISIONAL": ("Provisional", "amber"),
                 "UNRECONCILED": ("Does not reconcile", "red"), "NO_PRIOR_SNAPSHOT": ("No earlier valuation", "amber"),
                 "MISSING_MONTH_END": ("Month-end file missing", "amber"), "INCOMPLETE": ("Incomplete", "amber"),
                 "ERROR": ("Not computed", "red"), "ABSENT": ("Not computed", "red")}


def render_bridge(b, title):
    """Opening value -> money added -> what the investments did -> closing value, with the reconciliation check."""
    b = b or {}
    lab, lvl = _BRIDGE_STATE.get(b.get("state"), (str(b.get("state") or "Not computed"), "amber"))
    out = h3(f'{title} &nbsp;{flag(lab, lvl)}')
    if b.get("state") not in ("OK", "PROVISIONAL", "UNRECONCILED"):
        return out + para(se(b.get("why") or "Not available this month."), muted=True)
    p = b.get("parts") or {}
    rows = [("Value at the start", _d(b.get("from")), _gbp(b.get("opening_gbp"))),
            ("Money you paid in", "deposits and transfers in", _gbp(b.get("money_added_gbp"), signed=True)),
            ("Change in your shares", "price moves, after any buying and selling", _gbp(p.get("shares_gbp"), True)),
            ("Change in your funds", "price moves, after any buying and selling", _gbp(p.get("funds_gbp"), True)),
            ("Cash fund", "interest earned inside the money-market fund", _gbp(p.get("cash_fund_gbp"), True)),
            ("Income less charges", "dividends and interest, less platform and FX fees", _gbp(p.get("income_and_charges_gbp"), True))]
    if p.get("cash_posted_after_snapshot_gbp"):
        rows.append(("Timing difference", "cash the broker posted after a valuation was taken",
                     _gbp(p.get("cash_posted_after_snapshot_gbp"), True)))
    rows.append(("<strong>Value at the end</strong>", _d(b.get("to")), f'<strong>{_gbp(b.get("closing_gbp"))}</strong>'))
    out += table_start(["", "What it is", "Amount"], ["34%", "44%", "22%"], ["l", "l", "r"])
    for i, r in enumerate(rows):
        out += table_row(list(r), last=(i == len(rows) - 1), align=["l", "l", "r"])
    out += table_end()
    gain = b.get("investment_gain_gbp")
    note = (f'Investment gain over the period: <strong>{_gbp(gain, True)}</strong> (the change in value after removing '
            f'the money you paid in). Unexplained difference: {_gbp(b.get("residual_gbp"))} against a tolerance of '
            f'{_gbp(b.get("tolerance_gbp"))}.')
    if b.get("precision_note"):
        note += " " + se(b["precision_note"]) + "."
    if b.get("state") != "OK" and b.get("why"):
        note += f' {flag(lab, lvl)} &mdash; {se(b["why"])}.'
    out += para(note, small=True)
    return out


def render_contributions(b, n=5):
    """The ONE chart in the performance chapter: which holdings added or took away the most, in pounds."""
    rows = [c for c in ((b or {}).get("contributions") or []) if c.get("contribution_gbp")]
    if not rows:
        return ""
    rows = sorted(rows, key=lambda c: -c["contribution_gbp"])
    pick = rows[:n] + [c for c in rows[-n:] if c not in rows[:n] and c["contribution_gbp"] < 0]
    top = max(abs(c["contribution_gbp"]) for c in pick) or 1.0
    out = h3("What moved the value &mdash; the largest contributions")
    out += ('<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:4px 0 14px 0;'
            f'font-family:{SANS};font-size:13px;background:{C["bg_section"]}">')
    for c in pick:
        v = c["contribution_gbp"]
        w = max(1.0, 46.0 * abs(v) / top)
        col = C["green"] if v >= 0 else C["red"]
        bar = f'<span style="display:inline-block;height:12px;width:{w:.1f}%;background:{col};border-radius:2px"></span>'
        left = (f'<td style="width:50%;text-align:right;padding:5px 6px;background:{C["bg_section"]}">{bar}</td>'
                f'<td style="width:50%;padding:5px 6px;background:{C["bg_section"]}"></td>') if v < 0 else (
               f'<td style="width:50%;padding:5px 6px;background:{C["bg_section"]}"></td>'
               f'<td style="width:50%;padding:5px 6px;background:{C["bg_section"]}">{bar}</td>')
        out += (f'<tr><td style="width:34%;padding:5px 10px;color:{C["text"]};background:{C["bg_section"]}">'
                f'{name_cell(c.get("name"), c.get("code"))}</td>'
                f'<td style="width:46%;padding:0;background:{C["bg_section"]}"><table cellpadding="0" cellspacing="0" '
                f'border="0" style="width:100%"><tr>{left}</tr></table></td>'
                f'<td style="width:20%;text-align:right;padding:5px 10px;background:{C["bg_section"]}">'
                f'{_gbp(v, True)}</td></tr>')
    out += "</table>"
    out += para("Each bar is the holding's change in value over the period after removing any money put in or taken "
                "out by buying or selling. Green added value; red took it away.", muted=True, small=True)
    return out


def render_holdings_statement(hs):
    hs = hs or {}
    rows = hs.get("rows") or []
    if not rows:
        return ""
    out = h3(f'Holdings statement &mdash; as at {se(str(hs.get("as_of") or ""))}')
    out += table_start(["Holding", "Type", "Value", "Cost", "Gain", "Gain %", "Weight"],
                       ["30%", "11%", "13%", "13%", "12%", "10%", "11%"], ["l", "l", "r", "r", "r", "r", "r"])
    for r in rows:
        out += table_row([name_cell(r.get("name"), r.get("code")), se(r.get("kind")), _gbp(r.get("value_gbp")),
                          _gbp(r.get("cost_gbp")), _gbp(r.get("gain_gbp"), True), _pct(r.get("gain_pct")),
                          ("&mdash;" if r.get("weight_pct") is None else f'{r["weight_pct"]:.1f}%')],
                         align=["l", "l", "r", "r", "r", "r", "r"])
    if hs.get("broker_cash_gbp") is not None:
        out += table_row(["Cash held by the broker", "Cash", _gbp(hs["broker_cash_gbp"]), "&mdash;", "&mdash;", "&mdash;",
                          "&mdash;"], align=["l", "l", "r", "r", "r", "r", "r"])
    out += table_row(["<strong>Total</strong>", "", f'<strong>{_gbp(hs.get("total_value_gbp"))}</strong>', "", "", "", "100%"],
                     last=True, align=["l", "l", "r", "r", "r", "r", "r"])
    out += table_end()
    out += para(f'Source: {se(str(hs.get("source") or "broker valuation"))}. Gain is against what you paid, including '
                f'dealing costs.', muted=True, small=True)
    return out


_RH_WINDOWS = ("1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y")


def render_return_history(rh):
    rh = rh or {}
    rows = rh.get("rows") or []
    if not rows:
        return ""
    out = h3("How each holding has performed")
    out += table_start(["Holding", "1 month", "3 months", "6 months", "This year", "1 year", "3 years a yr",
                        "5 years a yr"], ["24%"] + ["10.8%"] * 7, ["l"] + ["r"] * 7)
    conflicts, na = [], 0
    for i, r in enumerate(rows):
        cells = [name_cell(r.get("name"), r.get("code"))]
        for w in _RH_WINDOWS:
            c = (r.get("cells") or {}).get(w) or {}
            if c.get("state") == "OK":
                cells.append(_pct(c.get("value")))
            elif c.get("state") == "SOURCE_CONFLICT":
                conflicts.append((r.get("name") or r.get("code"), w, c.get("why")))
                cells.append(f'{_pct(c.get("value"))}<sup style="color:{C["amber"]}">&Dagger;</sup>')
            else:
                na += 1
                cells.append(f'<span style="color:{C["text_muted"]}">n/a</span>')
        out += table_row(cells, last=(i == len(rows) - 1), align=["l"] + ["r"] * 7)
    out += table_end()
    note = se(rh.get("note") or "") + (f' Measured to {_d(rh.get("as_of"))}.' if rh.get("as_of") else "")
    if na:
        note += " n/a = no price history on file for that period (not zero)."
    out += para(note, muted=True, small=True)
    if conflicts:
        names = sorted({c[0] for c in conflicts})
        out += para(f'{flag("Sources disagree", "amber")} &Dagger; marks a figure where our price history and AJ Bell&#39;s '
                    f'X-Ray differ by more than 2 percentage points on the same date: {se(", ".join(names))}. Both are '
                    f'kept and neither is chosen until the difference is explained (ISA-0901).', small=True)
        out += details("The disagreements in detail",
                       "".join(para(f"{se(n)} ({se(w)}): {se(why)}", muted=True, small=True) for n, w, why in conflicts))
    return out


def render_report_measures(rm):
    """Group C, in the performance chapter. Absent is stated - never an empty space."""
    rm = rm or {}
    if rm.get("state") in (None, "ABSENT", "ERROR") and not rm.get("holdings"):
        return (h3("Account statement") + para(f'{flag("Not computed", "red")} &mdash; '
                                                + se(rm.get("why") or "the pre-run did not produce the account "
                                                     "statement measures this month (summary.report_measures ABSENT)."),
                                                small=True))
    out = render_bridge(rm.get("month_end_bridge"), "Last calendar month: how the value changed")
    out += render_bridge(rm.get("bridge"), "Since the last review: how the value changed")
    out += render_contributions(rm.get("bridge") if (rm.get("bridge") or {}).get("contributions") else
                                rm.get("month_end_bridge"))
    out += render_holdings_statement(rm.get("holdings"))
    out += render_return_history(rm.get("returns"))
    return out


# ---------------------------------------------------------------------------
# ISA-0898 - chapters, the attention list, the brief, the report page and the narrative gate
# ---------------------------------------------------------------------------
CHAPTERS = [
    ("decision",     "This month&#39;s decision"),
    ("attention",    "What needs your attention"),
    ("performance",  "How your portfolio did"),
    ("shares",       "Your shares"),
    ("funds",        "Your funds"),
    ("opportunities", "Opportunities we considered"),
    ("markets",      "Markets and the economy"),
    ("allowance",    "Allowance, cash and costs"),
    ("learning",     "What we learned this month"),
    ("glossary",     "Glossary"),
]
CHAPTER_TITLES = dict(CHAPTERS)

# ISA-0642: a WARNING row is a block row (<p>, <li>, <tr>) whose visible text carries a state word, or which
# carries a red or amber flag. A green verdict is a clean result, not a warning (ISA-0898).
_WARN_COLOUR_RE = re.compile(r'<strong style="color:(%s|%s)"' % (re.escape(C["red"]), re.escape(C["amber"])))


def _chapter_html(key, data, rm):
    s3_d = build_s3(data, "decided")
    if key == "decision":
        out = build_s1(data)
        out += h3("How this month&#39;s money is used") + build_s2(data)
        if (data.get("s3_investment_cases") or []):
            out += h3("The investment case for each decision") + s3_d
        anc = (data.get("meta") or {}).get("anchor_line")
        if anc:
            out += details("Target and contribution status (technical)", para(se(anc), small=True))
        return out
    if key == "performance":
        return build_s6(data) + render_report_measures(rm)
    if key == "shares":
        return build_s7(data)
    if key == "funds":
        return build_s8(data)
    if key == "opportunities":
        out = build_s3(data, "considered")
        out = (h3("Companies examined but not bought") + out) if out else ""
        out += h3("The pipeline") + build_s5(data)
        out += h3("Holdings being reviewed for sale") + build_s4(data)
        return out
    if key == "markets":
        return build_s9(data)
    if key == "allowance":
        return build_s10(data)
    if key == "learning":
        return build_s11(data) + build_manifest_block(data)
    if key == "glossary":
        return build_legend()
    return ""


def _warnings_by_chapter(built):
    """{chapter: [(text, colour)]} of the warning rows of each rendered chapter, de-duplicated across the report."""
    out, seen = {}, set()
    for key, html_ in built.items():
        for t, col in _warning_rows(html_):
            if t not in seen:
                seen.add(t)
                out.setdefault(key, []).append((t, col))
    return out


def brief_flag_counts(data):
    """{chapter: number of warning rows} of the complete report - the brief's statement of where to look."""
    rm = (data.get("s6_portfolio_snapshot") or {}).get("report_measures")
    w = _warnings_by_chapter({k: _chapter_html(k, data, rm) for k, _ in CHAPTERS if k not in ("attention", "glossary")})
    return {k: len(v) for k, v in w.items() if v}


def _attention_html(data, warn):
    s1 = data.get("s1_decision_summary") or {}
    out = ""
    pts = s1.get("attention_plain") or []
    if pts:
        out += plain_panel("".join(f"&#8226; {se(p)}<br>" for p in pts)[:-4], "What needs your attention, in plain English")
    else:
        out += para(_missing("The review has not written the plain-English attention list"), small=True)
    if warn:
        # counts and links only: the rows themselves sit in their chapters (and, verbatim, in the brief)
        out += table_start(["Where the flagged items are", "Items"], ["80%", "20%"], ["l", "r"])
        ks = [k for k, _ in CHAPTERS if warn.get(k)]
        for i, k in enumerate(ks):
            out += table_row([f'<a href="#{k}" style="color:{C["gold"]}">{CHAPTER_TITLES[k]}</a>', str(len(warn[k]))],
                             last=(i == len(ks) - 1), align=["l", "r"])
        out += table_end()
    return out


def build_report_body(data):
    """The COMPLETE report (fragment). Every chapter, every table, technical detail collapsed, glossary last."""
    meta = data.get("meta", {}) or {}
    rm = (data.get("s6_portfolio_snapshot") or {}).get("report_measures")
    built = {k: _chapter_html(k, data, rm) for k, _ in CHAPTERS if k != "attention"}
    warn = _warnings_by_chapter(built)
    built["attention"] = _attention_html(data, warn)
    parts = [f'<div style="font-family:{SANS};font-size:14px;color:{C["text"]};background:{C["bg_outer"]};'
             f'max-width:1080px;margin:0 auto;padding:28px 18px">\n', build_header(meta)]
    if data.get("_template_marker"):
        parts.append(para('<strong>NON-CURRENT EXAMPLE.</strong> This body was built from '
                          'email_data_monthly_isa_TEMPLATE.json (%s). Every figure in it is an illustrative '
                          'example, not this month\'s review.' % se(str(data.get("_template_marker")))))
    parts.append(headline_panel(data))
    parts.append(_contents(warn))
    for i, (k, title) in enumerate(CHAPTERS, 1):
        parts.append(h2(i, title, anchor=k))
        parts.append(section_wrap(built[k]))
    parts.append(build_footer(meta))
    parts.append("</div>\n")
    return "".join(parts)


def _contents(warn):
    cells = []
    for i, (k, t) in enumerate(CHAPTERS, 1):
        n = len(warn.get(k) or [])
        cells.append(f'<a href="#{k}" style="color:{C["text"]};text-decoration:none">{i}. {t}</a>'
                     + (f' <span style="color:{C["amber"]};font-size:11px">({n})</span>' if n else ""))
    half = (len(cells) + 1) // 2
    col = lambda xs: "".join(f'<p style="margin:3px 0;font-family:{SANS};font-size:13px">{x}</p>' for x in xs)
    return (f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:6px 0 4px 0"><tr>'
            f'<td style="width:50%;vertical-align:top">{col(cells[:half])}</td>'
            f'<td style="width:50%;vertical-align:top">{col(cells[half:])}</td></tr></table>'
            + para("Numbers in brackets are items flagged for your attention in that chapter.", muted=True, small=True))


def build_report_page(data, title=None):
    """The report as a complete web page (published privately, or attached when the run cannot publish)."""
    meta = data.get("meta", {}) or {}
    t = title or email_subject_for(meta)
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{se(t)}</title></head>\n"
            f'<body style="margin:0;background:{C["bg_outer"]};color:{C["text"]}">\n'
            + build_report_body(data) + "</body></html>\n")


# ---------------------------------------------------------------------------
# The BRIEF (the email): inline styles only, explicit dark backgrounds on every block
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The HEADLINE PANEL (Raj 26-Oct-2026): the very top of the brief and the report. Portfolio value with its split
# into funds / shares / cash; the investment gain this calendar year, last month and since the last review, each
# split into funds and shares (pounds and percent); progress towards the GBP 1m target. Every figure is the
# pre-run's (summary.report_measures, summary.account_return) - nothing is computed here (R20.2).
# ---------------------------------------------------------------------------
_COMP = (("Fund", "Investment funds", "#c8a96b"), ("Share", "Direct shares", "#86a9d6"),
         ("Cash", "Cash and money market", "#6b7480"))


def _big_gbp(v):
    if v is None:
        return "&mdash;"
    v = float(v)
    whole, pence = divmod(round(abs(v) * 100), 100)
    sign = "&minus;" if v < 0 else ""
    return f'{sign}&pound;{whole:,}<span style="font-size:18px">.{pence:02d}</span>'


def _gain_html(g, pct=None, big=False):
    if g is None:
        return "&mdash;"
    col = C["green"] if g >= 0 else C["red"]
    sgn = "+" if g >= 0 else "&minus;"
    txt = f"{sgn}&pound;{abs(g):,.0f}" + (f" &middot; {sgn}{abs(pct):.2f}%" if pct is not None else "")
    size = "28px" if big else "14px"
    fam = SERIF if big else SANS
    return f'<span style="font-family:{fam};font-size:{size};color:{col};font-weight:{400 if big else 700}">{txt}</span>'


def _sleeve_line(b):
    sl = (b or {}).get("sleeves") or {}
    bits = []
    for k, lab in (("funds", "funds"), ("shares", "shares")):
        x = sl.get(k) or {}
        if x.get("gain_gbp") is not None:
            g = x["gain_gbp"]
            col = C["green"] if g >= 0 else C["red"]
            sgn = "+" if g >= 0 else "&minus;"
            bits.append(f'{lab} <span style="color:{col}">{sgn}&pound;{abs(g):,.0f}'
                        + (f' ({sgn}{abs(x["pct"]):.1f}%)' if x.get("pct") is not None else "") + "</span>")
    return " &middot; ".join(bits)


def _panel_cell(inner, width, min_px=216):
    """ISA-0898 (28-Oct-2026): a FLUID tile - inline-block at `width` with a minimum width, so three sit across at
    760px and they stack one per row on a phone (measured 28-Oct: the table-cell version overflowed a 390px
    screen by 100px in the brief). No <style> block and no flex (Email HTML Rules 2/3)."""
    return (f'<div style="display:inline-block;vertical-align:top;width:{width};min-width:{min_px}px;max-width:100%;'
            f'box-sizing:border-box;padding:0 6px 12px 6px;font-size:13px;text-align:left;background:{C["bg_outer"]}">'
            f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;background:{C["bg_panel"]};'
            f'border:1px solid {C["border"]};border-radius:6px"><tr><td style="padding:16px 18px;vertical-align:top;'
            f'background:{C["bg_panel"]};font-family:{SANS};color:{C["text"]}">{inner}</td></tr></table></div>')


def _eyebrow_small(t):
    return (f'<p style="margin:0 0 8px 0;font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:.14em;'
            f'text-transform:uppercase;color:{C["gold"]}">{t}</p>')


def _small(t, muted=True):
    return (f'<p style="margin:6px 0 0 0;font-family:{SANS};font-size:12px;line-height:1.5;'
            f'color:{C["text_muted"] if muted else C["text"]}">{t}</p>')


def headline_panel(data):
    s6 = data.get("s6_portfolio_snapshot") or {}
    rm = s6.get("report_measures") or {}
    hs = rm.get("holdings") or {}
    # ── tile 1: value and composition ──
    total = hs.get("total_value_gbp")
    if total is None:
        kp = {str(k.get("label", "")).lower(): k for k in s6.get("kpis") or []}
        v = (kp.get("total isa value") or {}).get("value")
        t1 = _eyebrow_small("Portfolio value") + f'<p style="margin:0;font-family:{SERIF};font-size:30px;color:{C["text_h1"]}">{se(v or "&mdash;")}</p>'
        t1 += _small("The split by funds, shares and cash is not available this month (account statement not computed).")
    else:
        comp = {"Fund": 0.0, "Share": 0.0, "Cash": float(hs.get("broker_cash_gbp") or 0)}
        for r in hs.get("rows") or []:
            k = "Cash" if r.get("kind") == "Cash fund" else r.get("kind")
            if k in comp:
                comp[k] += float(r.get("value_gbp") or 0)
        bar = "".join(f'<td style="width:{max(0.5, 100 * comp[k] / total):.2f}%;height:6px;background:{col};font-size:0;'
                      f'line-height:0">&nbsp;</td>' for k, _l, col in _COMP if comp[k] > 0)
        t1 = _eyebrow_small("Portfolio value &middot; " + se(str(hs.get("as_of") or "")))
        t1 += f'<p style="margin:0 0 12px 0;font-family:{SERIF};font-size:30px;color:{C["text_h1"]}">{_big_gbp(total)}</p>'
        t1 += (f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:0 0 10px 0"><tr>{bar}</tr></table>'
               f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%">')
        for k, lab, col in _COMP:
            t1 += (f'<tr><td style="padding:4px 0;font-family:{SANS};font-size:13px;color:{C["text"]}">'
                   f'<span style="color:{col}">&#9632;</span> {lab}</td>'
                   f'<td style="padding:4px 0;text-align:right;font-family:{SANS};font-size:13px;color:{C["text"]};white-space:nowrap">'
                   f'&pound;{comp[k]:,.0f} &middot; {100 * comp[k] / total:.1f}%</td></tr>')
        t1 += "</table>"
    # ── tile 2: investment gain ──
    yb, mb, bb = rm.get("year_bridge") or {}, rm.get("month_end_bridge") or {}, rm.get("bridge") or {}
    ok = ("OK", "PROVISIONAL")
    t2 = _eyebrow_small("Investment gain &middot; this calendar year")
    if yb.get("state") in ok:
        t2 += _gain_html(yb.get("investment_gain_gbp"), yb.get("investment_gain_pct"), big=True)
        t2 += _small(se(yb.get("period") or "") + " &middot; deposits excluded" + (" &middot; provisional" if yb["state"] == "PROVISIONAL" else ""))
        if _sleeve_line(yb):
            t2 += _small(_sleeve_line(yb), muted=False)
    else:
        ytd = next((r for r in s6.get("performance") or [] if str(r.get("period", "")).startswith("Calendar YTD")), None)
        if ytd and "%" in str(ytd.get("portfolio")):
            t2 += f'<p style="margin:0;font-family:{SERIF};font-size:30px;color:{C["green"]}">{se(ytd["portfolio"])}</p>'
            t2 += _small("account return, deposits excluded")
        else:
            t2 += f'<p style="margin:0;font-family:{SERIF};font-size:26px;color:{C["text_h1"]}">Not measured</p>'
            t2 += _small(se((yb.get("why") if yb else None) or "the cash records do not yet cover the whole year, so a "
                            "return would count deposits as growth"))
    for b, lab in ((mb, "Last month"), (bb, "Since the last review")):
        if b.get("state") in ok + ("UNRECONCILED",):
            t2 += (f'<p style="margin:12px 0 0 0;font-family:{SANS};font-size:13px;color:{C["text"]}"><strong>{lab}</strong> '
                   f'<span style="color:{C["text_muted"]}">({_d(b.get("from"))} &rarr; {_d(b.get("to"))})</span><br>'
                   f'{_gain_html(b.get("investment_gain_gbp"), b.get("investment_gain_pct"))}'
                   + (f' &middot; <span style="color:{C["amber"]}">{_BRIDGE_STATE[b["state"]][0].lower()}</span>'
                      if b["state"] != "OK" else "") + "</p>")
            if _sleeve_line(b):
                t2 += _small(_sleeve_line(b), muted=False)
        elif b:
            t2 += _small(f"<strong>{lab}:</strong> " + se(b.get("why") or str(b.get("state"))))
    # ── tile 3: progress towards the target ──
    tp = rm.get("target_progress") or {}
    tgt = tp.get("target_floor_gbp")
    t3 = _eyebrow_small(("Progress towards &pound;%s million" % f"{tgt / 1e6:g}") if tgt else "Progress towards your target")
    if tp.get("state") == "OK" and tp.get("ahead_gbp") is not None:
        a = tp["ahead_gbp"]
        t3 += (f'<p style="margin:0;font-family:{SERIF};font-size:30px;color:{C["green"] if a >= 0 else C["red"]}">'
               f'&pound;{abs(a):,.0f} {"ahead" if a >= 0 else "behind"}</p>')
        t3 += _small(f'of the path from your {_d(tp.get("checkpoint_date"))} checkpoint (&pound;{tp["checkpoint_gbp"]:,.0f}) at '
                     f'{tp.get("operative_pct")}% a year, counting the &pound;{tp.get("flows_since_checkpoint_gbp", 0):,.0f} paid in since.',
                     muted=False)
    if tp.get("required_now_floor_pct") is not None:
        t3 += _small(f'Growth needed from today: <strong style="color:{C["text_h1"]}">{tp["required_now_floor_pct"]:.2f}% a year</strong> '
                     f'to {_d(tp.get("target_date"))}' + (f' (&pound;{tp["target_stretch_gbp"] / 1e6:g}m stretch: '
                                                          f'{tp["required_now_stretch_pct"]:.2f}%)' if tp.get("required_now_stretch_pct") else "")
                     + ". " + se((tp.get("basis") or "").capitalize()) + ".", muted=False)
    if tp.get("state") != "OK":
        req = re.search(r"required return now:\s*([\d.]+%)\s*floor\s*/\s*([\d.]+%)\s*stretch",
                        str((data.get("meta") or {}).get("anchor_line") or ""))
        t3 += (f'<p style="margin:0;font-family:{SERIF};font-size:26px;color:{C["text_h1"]}">{req.group(1)} a year</p>'
               + _small(f"growth needed (stretch {req.group(2)}); the distance from the target path is not measured this month")
               if req else _small("Target progress is not available this month."))
    return (f'<div style="width:100%;margin:6px 0 6px 0;font-size:0;text-align:center;background:{C["bg_outer"]}">'
            + _panel_cell(t1, "34%") + _panel_cell(t2, "33%") + _panel_cell(t3, "32%") + "</div>\n")

def _tile_values(data):
    s6 = data.get("s6_portfolio_snapshot") or {}
    kp = {str(k.get("label", "")).lower(): k for k in s6.get("kpis") or []}
    val = kp.get("total isa value") or {}
    ytd = next((r for r in s6.get("performance") or [] if str(r.get("period", "")).startswith("Calendar YTD")), None)
    req = re.search(r"required return now:\s*([\d.]+%)\s*floor\s*/\s*([\d.]+%)\s*stretch",
                    str((data.get("meta") or {}).get("anchor_line") or ""))
    _ytd_ok = bool(ytd) and "%" in str(ytd.get("portfolio"))
    return [{"label": "Portfolio value", "value": val.get("value") or "—", "sub": val.get("sub") or ""},
            {"label": "Return this calendar year",
             "value": ytd.get("portfolio") if _ytd_ok else "Not measured",
             "sub": "your account, after removing deposits" if _ytd_ok else
             "the cash records do not yet cover the whole year, so a return would count deposits as growth"},
            {"label": "Growth needed each year",
             "value": req.group(1) if req else "—",
             "sub": (f"to reach your target (stretch {req.group(2)})" if req else "target status not available")}]


def _brief_card(a):
    name = a.get("instrument") or a.get("name") or ""
    rng = _price_range(a)
    trade = str(a.get("action", "")).strip().upper().split(" ")[0] in ("BUY", "SELL", "ADD", "TRIM")
    out = (f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:10px 0;background:{C["bg_panel"]};'
           f'border:1px solid {C["border"]};border-radius:6px"><tr><td style="padding:12px 16px;background:{C["bg_panel"]};'
           f'color:{C["text"]};font-family:{SANS}">')
    out += para(f'{_pill_for(a.get("action"))} &nbsp;{name_cell(name, a.get("ticker"))}')
    if a.get("verdict"):
        out += verdict_line(se(a["verdict"]))
    if trade:
        out += labelled("Acceptable price:", se(rng) if rng else _missing("Not stated - required before dealing"))
    if a.get("plain_english"):
        out += para(se(a["plain_english"]))
    elif a.get("note"):
        out += para(se(a["note"]), muted=True, small=True)
    out += "</td></tr></table>\n"
    return out


def _numbers_rows(data):
    rm = (data.get("s6_portfolio_snapshot") or {}).get("report_measures") or {}
    rows = []
    for key, lab in ():                                   # the bridges are in the headline panel (Raj 26-Oct-2026)
        b = rm.get(key) or {}
        if b.get("state") in ("OK", "PROVISIONAL", "UNRECONCILED"):
            st, lvl = _BRIDGE_STATE[b["state"]]
            rows.append((f"{lab} ({_d(b.get('from'))} to {_d(b.get('to'))})",
                         f"{_gbp(b.get('investment_gain_gbp'), True)} investment gain; {_gbp(b.get('money_added_gbp'))} paid in",
                         flag(st, lvl)))
        elif b:
            st, lvl = _BRIDGE_STATE.get(b.get("state"), (str(b.get("state")), "amber"))
            rows.append((lab, se(b.get("why") or ""), flag(st, lvl)))
    if not rm or rm.get("state") == "ABSENT":
        rows.append(("Account statement", "not computed this month", flag("Not computed", "red")))
    elif rm.get("state") == "DEGRADED":
        bad = [k for k in ("bridge", "month_end_bridge", "year_bridge", "returns")
               if (rm.get(k) or {}).get("state") in ("ERROR", "UNRECONCILED")]
        rows.append(("Account statement", "these parts did not reconcile: " + ", ".join(bad), flag("Check", "red")))
    s10 = {str(k.get("label", "")).lower(): k for k in (data.get("s10_tax_tracker") or {}).get("kpis") or []}
    if s10.get("allowance remaining"):
        rows.append(("ISA allowance remaining", se(s10["allowance remaining"].get("value")),
                     se(s10["allowance remaining"].get("sub") or "")))
    s2k = {str(k.get("label", "")).lower(): k for k in (data.get("s2_capital_allocation") or {}).get("kpis") or []}
    if s2k.get("capital available"):
        rows.append(("Cash available to invest", se(s2k["capital available"].get("value")), "this month"))
    return rows


from html import escape as _esc


def build_brief(data, report_ref=None, report_html=None):
    """The email. Decision first; every warning row of the report carried (ISA-0642 subset proof)."""
    meta = data.get("meta", {}) or {}
    s1 = data.get("s1_decision_summary") or {}
    report_html = report_html if report_html is not None else build_report_body(data)
    bg = C["bg_outer"]
    out = [f'<div style="font-family:{SANS};font-size:14px;color:{C["text"]};background:{bg};max-width:760px;'
           f'margin:0 auto;padding:24px 16px">\n']
    out.append(eyebrow("Monthly ISA Portfolio Review &middot; " + se(meta.get("run_month_label") or "")))
    out.append(h1("Your decision brief"))
    out.append(para(" &nbsp;&middot;&nbsp; ".join(x for x in (
        f'Data as at {se(meta.get("data_date"))}' if meta.get("data_date") else "",
        se(meta.get("broker") or ""), f'Tax year {se(meta.get("tax_year"))} {se(meta.get("tax_year_month") or "")}'
        if meta.get("tax_year") else "") if x), muted=True, small=True))
    if data.get("_template_marker"):
        out.append(para('<strong>NON-CURRENT EXAMPLE.</strong> Built from the template (%s); every figure is '
                        'illustrative.' % se(str(data["_template_marker"]))))
    out.append(headline_panel(data))
    if s1.get("verdict"):
        out.append(verdict_line(se(s1["verdict"])))
    else:
        out.append(para(_missing("The month's verdict has not been written"), small=True))
    if s1.get("plain_english"):
        out.append(plain_panel(se(s1["plain_english"])))
    acts = s1.get("actions") or []
    if acts:
        out.append(h3("The decisions"))
        out.append(funding_box(s1.get("funding_plan")))
        out += [_brief_card(a) for a in acts]
    out.append(h3("What needs your attention"))
    pts = s1.get("attention_plain") or []
    if pts:
        out.append("".join(para("&#8226; " + se(p)) for p in pts))
    else:
        out.append(para(_missing("The review has not written the plain-English attention list"), small=True))
    # ISA-0898 (Raj 26-Oct-2026: the verbatim list "serves no purpose"): the brief states WHERE the report flags
    #   items and how many; the report - always complete, never abridged - holds each one in its chapter.
    counts = brief_flag_counts(data)
    if counts:
        t = table_start(["Where the full report flags items", "Items"], ["80%", "20%"], ["l", "r"])
        ks = [k for k, _ in CHAPTERS if counts.get(k)]
        for i, k in enumerate(ks):
            t += table_row([CHAPTER_TITLES[k], str(counts[k])], last=(i == len(ks) - 1), align=["l", "r"])
        out.append(t + table_end())
    nums = _numbers_rows(data)
    if nums:
        out.append(h3("The month in numbers"))
        t = table_start(["Measure", "Reading", "Status"], ["34%", "46%", "20%"], ["l", "l", "l"])
        for i, r in enumerate(nums):
            t += table_row(list(r), last=(i == len(nums) - 1), align=["l", "l", "l"])
        out.append(t + table_end())
    out.append(f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;margin:18px 0;background:{C["bg_panel"]};'
               f'border:1px solid {C["gold"]};border-radius:6px"><tr><td style="padding:14px 16px;background:{C["bg_panel"]};'
               f'font-family:{SANS};font-size:14px;color:{C["text"]}"><strong style="color:{C["gold"]}">The full report</strong>'
               f'<br>' + (f'<a href="{se(report_ref)}" style="color:{C["gold"]}">Open the full report</a>'
                          if report_ref and str(report_ref).startswith("http") else
                          f'Attached as <strong>{se(os.path.basename(str(report_ref)))}</strong>' if report_ref else
                          "Saved with this month&#39;s review files")
               + ' &mdash; every chapter, table and source, with the technical detail and glossary.</td></tr></table>')
    out.append(build_footer(meta))
    out.append("</div>\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# The narrative gate (ISA-0898): the three-layer writing standard, enforced
# ---------------------------------------------------------------------------
_JARGON_RE = re.compile(r"\b(ISA-\d+|[A-Z][A-Z0-9]*_[A-Z0-9_]+|E\[r\]|ER12|T[1-4]\b|ACS|FRS|VCI|D\d{1,2}\b|minimax\w*|"
                        r"Checkpoint|receipt|engine|EPS|EBITDA|P/E|ROIC|NAV|ADRs?|ADS|MMF|FX|pp\b|bps?\b|"
                        r"SEDOL|ISIN|CSH2|drawdown|alpha|beta|z-score|percentile)\b")


def narrative_issues(data):
    """[(where, problem)] - missing layers, a dealing verdict without a price range, jargon in plain-English text."""
    out = []
    s1 = data.get("s1_decision_summary") or {}

    def need(where, v, what):
        if not str(v or "").strip() or _PLACEHOLDER_RE.search(str(v)):
            out.append((where, f"{what} missing"))

    def plain(where, v):
        for m in _JARGON_RE.finditer(re.sub(r"<[^>]+>", " ", str(v or ""))):
            out.append((where, f"jargon in plain-English text: '{m.group(0)}'"))
    need("decision", s1.get("verdict"), "verdict (one line)")
    need("decision", s1.get("plain_english"), "'In plain English'")
    plain("decision", s1.get("plain_english"))
    for p in s1.get("attention_plain") or []:
        plain("attention", p)
    if not s1.get("attention_plain"):
        out.append(("attention", "'What needs your attention, in plain English' missing"))
    for a in s1.get("actions") or []:
        w = "decision: " + str(a.get("instrument") or a.get("name") or "?")[:40]
        need(w, a.get("verdict"), "verdict")
        need(w, a.get("plain_english"), "'In plain English'")
        plain(w, a.get("plain_english"))
        if str(a.get("action", "")).strip().upper().split(" ")[0] in ("BUY", "SELL", "ADD", "TRIM"):
            if a.get("price_low") in (None, "") or a.get("price_max") in (None, ""):
                out.append((w, "acceptable price range missing (price_low and price_max)"))
            try:
                float(str(a.get("amount_gbp")).replace(",", "").replace("\u00a3", ""))
            except (TypeError, ValueError):
                out.append((w, "amount_gbp missing - a number, all-in (ISA-0904 funds the purchases from it)"))
    for c in data.get("s3_investment_cases") or []:
        w = "case: " + str(c.get("name") or c.get("ticker") or "?")[:40]
        need(w, c.get("verdict"), "verdict")
        need(w, c.get("plain_english"), "'In plain English'")
        plain(w, c.get("plain_english"))
    for key, lab in (("s2_capital_allocation", "money used"), ("s6_portfolio_snapshot", "performance"),
                     ("s7_stock_sleeve", "shares"), ("s8_fund_review", "funds")):
        v = (data.get(key) or {}).get("plain_english")
        need(lab, v, "'In plain English'")
        plain(lab, v)
    for it in (data.get("s9_macro") or {}).get("items") or []:
        need("markets: " + str(it.get("title", ""))[:40], it.get("so_what"), "'What it means for you'")
        plain("markets", it.get("so_what"))
    for it in (data.get("s11_retrospective") or {}).get("items") or []:
        need("learning: " + str(it.get("title", ""))[:40], it.get("plain_english"), "'In plain English'")
        plain("learning", it.get("plain_english"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def approval_gate(rc: dict, current_state_fn=None) -> tuple:
    """ISA-0727 (M14) - is the capital plan in run_context still the plan for the CURRENT state?

    -> (validity dict, blocking message or None). The plan's `approval_binding` is compared with
    the state re-derived NOW by the SAME function the router used (capital_authorisation.
    input_state): a different build, config, broker book, feasible population, underwriting store,
    broker inputs or capital amount makes it STALE, and a STALE plan is not rendered as current.
    A plan with no binding (pre-control) is UNBOUND: reported, not blocked. A binding that cannot
    be re-checked is UNKNOWN and blocks (R4.3)."""
    import capital_authorisation as _ca_m14
    ab = (((rc or {}).get("summary") or {}).get("capital_destination") or {}).get("approval_binding")
    if not ab:
        return _ca_m14.approval_validity(None, {}), None
    try:
        if current_state_fn is None:
            import capital_destination as _cd_m14
            _s9m, _ = _cd_m14._latest_step9_pre()
            cur = _ca_m14.input_state(portfolio_path=_cd_m14.latest_portfolio_path(), step9=_s9m,
                                      amount_gbp=(ab.get("components") or {}).get("capital_amount_gbp"))
        else:
            cur = current_state_fn(ab)
        v = _ca_m14.approval_validity(ab, cur)
    except Exception as exc:                                            # noqa: BLE001
        v = {"state": "UNKNOWN", "changed": [], "why": "%s: %s" % (type(exc).__name__, exc)}
    if v["state"] == "VALID":
        return v, None
    return v, ("ISA-0727: the capital plan's approval is %s - %s. Re-run the pre-run router "
               "(Step 6.10) so the plan is formed on the current state." % (v["state"], v.get("why")))


_PLACEHOLDER_RE = re.compile(r"\[Claude[^\]\n]{0,200}\]")


def unfilled_placeholders(html: str) -> dict:
    """ISA-0793 (30-Sep-2026): every '[Claude ...]' token left in the COMPLETE report -> {token: count}.
    The prose Self-Check (Run_Context item 7b) was the only control; the 04-Oct SHADOW built rc 0
    with 56 of them. Scanned on the complete report because the review fills every section, including
    those the lean body omits (Sep-2026 full report: 0)."""
    out = {}
    for m in _PLACEHOLDER_RE.finditer(html or ""):
        out[m.group(0)] = out.get(m.group(0), 0) + 1
    return out


def email_subject_for(meta: dict, today=None) -> str:
    """ISA-0795 (Raj, 30-Sep-2026): the subject the prefill declared (RUN/review month, e.g.
    'Monthly ISA Portfolio Review - Oct 2026'). Email data without that field (built before
    TB-2026-09-30-10) falls back to the month this build runs in (the review day) - never to the
    data-month run_month_label."""
    s = (meta or {}).get("email_subject")
    if s:
        return s
    import datetime as _dt795
    return "Monthly ISA Portfolio Review - " + __import__("run_cycle").cycle_run_month(today)   # ISA-0910




def main():
    parser = argparse.ArgumentParser(
        description="Build Monthly ISA Portfolio Review HTML email body from JSON data file."
    )
    parser.add_argument("--data",   required=True,
                        help="Path to email_data_mmm_yyyy.json produced during the run")
    parser.add_argument("--output", required=True,
                        help="Output path for HTML body file e.g. email_body_jun2026.html")
    parser.add_argument("--month", help="Month label e.g. sep_2026, for the §7.6.2 "
                                        "conviction gate. Inferred from --data when omitted.")
    parser.add_argument("--allow-unrecorded-conviction", action="store_true",
                        help="Build anyway with Step 9 judgements unrecorded. Requires a "
                             "reason via --gate-override-reason, and is stamped into the "
                             "email footer so the gap is visible in the artefact itself.")
    parser.add_argument("--gate-override-reason", default="")
    # H11 (05-Aug-2026) — the August report built clean and could not be SENT. --lean makes the
    # emailed copy deliverable; the full report is written regardless, so nothing is ever lost.
    parser.add_argument("--allow-placeholders", action="store_true",
                        help="ISA-0793: SHADOW/rehearsal builds only - build even with unfilled "
                             "'[Claude ...]' placeholders. Never on a send.")
    parser.add_argument("--lean", action="store_true",
                        help="Emit the decision BRIEF as the email body and write the complete report "
                             "as a web page (ISA-0898). Nothing is omitted from the report.")
    parser.add_argument("--max-bytes", type=int, default=LEAN_DEFAULT_BUDGET,
                        help=f"Lean byte budget (default {LEAN_DEFAULT_BUDGET}). The August "
                             f"send failed at 195,926 bytes.")
    parser.add_argument("--full-output", default=None,
                        help="Where the COMPLETE report page is written when --lean is used. "
                             "Defaults to <output stem>_full.html; the brief names it (attachment) "
                             "unless --report-url is given.")
    parser.add_argument("--report-url", default=None,
                        help="ISA-0898: the private link of the published report page. When given, the "
                             "brief links to it; otherwise the brief says the report is attached.")
    args = parser.parse_args()

    # Load JSON
    if not os.path.exists(args.data):
        print(f"ERROR: Data file not found: {args.data}")
        sys.exit(1)
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    # ── §7.6.2 CONVICTION GATE ───────────────────────────────────────────────────────
    # Steps 9B-9E are session JUDGEMENTS. Before this gate they were written ONLY as prose
    # in the email, so the reasoning behind the month's most consequential decision was
    # unreconstructable the moment the email was read — acceptance criterion #19 could never
    # be met. The gate makes the record a PRECONDITION of the report rather than a by-product
    # of it. Overriding is possible, but it is dated, reasoned and stamped into the email.
    _month = args.month
    if not _month:
        _m = re.search(r"email_data_([a-z]{3}_\d{4})", os.path.basename(args.data))
        _month = _m.group(1) if _m else None
    _gate_note = None
    if _month:
        # ══════════════════════════════════════════════════════════════════════════════════
        # ISA-0698 (Raj, 16-Sep-2026, Option B) — CHECK THE JUDGEMENT PASS, THEN RENDER
        # ══════════════════════════════════════════════════════════════════════════════════
        # ⚑ This builder is a RENDERER (R20.2): it computes no refusal and no re-route. The step
        #   `python3 capital_destination.py --judgement-pass [mmm_yyyy]` gates the Step 9 record per
        #   name on the pre-judgement scope, re-runs the router with any refusals and writes the
        #   result INTO run_context, stamped with the record's sha256. Here: (1) a structurally
        #   invalid/absent record blocks (exit 2, override as before); (2) the pass stamp must
        #   match the CURRENT record, or the build refuses — a plan judged against an older
        #   record is not this month's decision; (3) §2's router block is re-rendered from the
        #   run context, its one source (ISA-0447).
        import hashlib as _hl
        _dir = os.path.dirname(os.path.abspath(args.data))
        _cpath = os.path.join(_dir, f"step9_conviction_{_month}.json")
        _rcpath = os.path.join(_dir, f"run_context_{_month}.json")
        _errs, _jg, _rc = [], None, None
        try:
            with open(_rcpath, encoding="utf-8") as _rf:
                _rc = json.load(_rf)
            _jg = ((_rc.get("summary") or {}).get("capital_destination") or {}).get("judgement_gate")
            # ISA-0722: the held-underwriting table, on EVERY path (renderer only, R20.2)
            import email_prefill as _ep_uw
            data.setdefault("s7_stock_sleeve", {})["held_underwriting"] = \
                _ep_uw.build_held_underwriting_block(
                    (_rc.get("summary") or {}).get("held_underwriting"),
                    (((_rc.get("summary") or {}).get("held_position_review") or {})
                     .get("thesis_lineage")))
            # ⚑ ISA-0860 FIX-ON-FINDING (12-Oct-2026): build_integrity_block was the DECLARED renderer of six
            #   summary keys and had ZERO callers - coverage, the symbol map and the integrity queue reached no
            #   email. Wired here (renderer only, R20.2); pair_summary_key_disposition now refuses an uncalled one.
            _smi = _rc.get("summary") or {}
            _ib = _ep_uw.build_integrity_block(
                fetch_report=_smi.get("fetch_report"), correlation_coverage=_smi.get("correlation_coverage"),
                integrity_report=_smi.get("integrity_report"), execution_ledger=_smi.get("execution_ledger"),
                symbol_map_refresh=_smi.get("symbol_map_refresh"), fetch_universe=_smi.get("fetch_universe"),
                instrument_eligibility=_smi.get("instrument_eligibility"),
                corporate_events=_smi.get("corporate_events"))
            data["s7_stock_sleeve"]["integrity_lines"] = _ib.get("s7_lines") or []
            data.setdefault("s11_retrospective", {})["integrity_lines"] = _ib.get("s11_lines") or []
            # ⚑ ISA-0448 (25-Oct-2026, found on the candidate shadow): Step 9's prefill runs BEFORE position_alerts,
            #   reversal_worklist, mmf_sweep and watchlist_promotion_log exist, so the summary lines are RE-DERIVED here
            #   from the FINAL run_context by the same renderer. A prefill line survives only for a key this pass
            #   cannot see (e.g. step9_pre unreadable) - never dropped silently.
            try:
                with open(os.path.join(_dir, f"step9_pre_{_month}.json"), encoding="utf-8") as _s9f:
                    _s9bt = json.load(_s9f)
            except Exception:                                           # noqa: BLE001
                _s9bt = {}
            _bt = _ep_uw.build_summary_lines(_smi, _s9bt)
            for _sec in set(_bt) | {k for k, v in data.items() if isinstance(v, dict) and v.get("summary_lines")}:
                if not isinstance(data.get(_sec), dict):
                    continue
                _new = list(_bt.get(_sec, []))
                _have = {ln.get("key") for ln in _new}
                _new += [ln for ln in (data[_sec].get("summary_lines") or []) if ln.get("key") not in _have]
                data[_sec]["summary_lines"] = _new
            # ⚑ ISA-0900: the account statement measures, from the FINAL run context (renderer only, R20.2)
            if _smi.get("report_measures") or \
                    (data.get("s6_portfolio_snapshot") or {}).get("report_measures", {}).get("state") in (None, "ABSENT"):
                data["s6_portfolio_snapshot"] = _ep_uw.apply_report_measures(
                    data.get("s6_portfolio_snapshot"), _smi.get("report_measures"))
        except FileNotFoundError:
            _errs.append(f"run_context_{_month}.json does not exist — the router plan and its "
                         f"judgement scope cannot be read")
        except Exception as _re:
            _errs.append(f"run_context unreadable: {_re}")
        try:
            with open(_cpath, "rb") as _cf:
                _craw = _cf.read()
        except FileNotFoundError:
            _craw = None
            _errs.append(f"step9_conviction_{_month}.json does not exist — the Step 9 judgement "
                         f"record was never written")
        if _rc is not None:
            _av, _am = approval_gate(_rc)
            data.setdefault("meta", {})["approval_validity"] = _av
            print(f"  ISA-0727 approval binding: {_av.get('state')} "
                  f"{('changed: ' + ', '.join(_av.get('changed') or [])) if _av.get('changed') else ''}")
            if _am:
                _errs.append(_am)
        if _craw is not None and _rc is not None:
            if not _jg:
                _errs.append("ISA-0698: the judgement pass has not been applied to run_context. "
                             f"Run: python3 capital_destination.py --judgement-pass {_month}")
            elif _jg.get("conviction_sha256") != _hl.sha256(_craw).hexdigest():
                _errs.append("ISA-0698: the judgement pass stamp is for a DIFFERENT version of "
                             "the Step 9 record (it changed after the pass). Re-run: "
                             f"python3 capital_destination.py --judgement-pass {_month}")
            elif _jg.get("blocking"):
                _errs.extend("conviction record: " + str(e) for e in _jg["blocking"])
        if _errs:
            if not args.allow_unrecorded_conviction:
                print(f"ERROR: §7.6.2 judgement gate FAILED — {len(_errs)} blocking issue(s).")
                for _e in _errs[:10]:
                    print(f"  BLOCK: {_e}")
                if len(_errs) > 10:
                    print(f"  ... and {len(_errs) - 10} more")
                print("\nFill the record via")
                print("  python3 conviction_capture.py --apply <judgements.json> --month "
                      f"{_month}")
                print(f"then run  python3 capital_destination.py --judgement-pass {_month}")
                print("or, if you accept the gap, re-run with --allow-unrecorded-conviction")
                print("and --gate-override-reason \"...\" (recorded in the email footer).")
                sys.exit(2)
            if not str(args.gate_override_reason).strip():
                print("ERROR: --allow-unrecorded-conviction requires --gate-override-reason.")
                print("An override without a stated reason is indistinguishable from a mistake.")
                sys.exit(2)
            _gate_note = (f"Step 9 judgement gate NOT satisfied ({len(_errs)} issue(s)) — "
                          f"overridden: {args.gate_override_reason.strip()}. No new stock "
                          f"capital may be placed from an unjudged plan (ISA-0698).")
            print(f"WARNING: {_gate_note}")
            data.setdefault("meta", {})["conviction_gate_override"] = _gate_note
            data.setdefault("s2_capital_allocation", {})["capital_router"] = {
                "absent_line": ("NOT COMPUTED — the ISA-0698 judgement pass is not applied to this "
                                "record, so the router plan is unjudged and no new stock capital "
                                "may be placed from it.")}
        else:
            import email_prefill as _ep
            _cds = (_rc.get("summary") or {}).get("capital_destination") or {}
            _cde_sum = (_rc.get("summary") or {}).get("capital_decision_engine")
            _cde_month = _month
            _blk = _ep.build_capital_router_block(_cds, (_rc.get("summary") or {}).get("waiting_room"))
            if _jg.get("rerouted"):
                _blk["split_reason_line"] = (
                    "ISA-0698 judgement gate: %s of %s capital-precondition candidate(s) REFUSED "
                    "new capital for missing/invalid D21 judgement (%s); this is the router RE-RUN "
                    "with those refusals. %s"
                    % (len(_jg.get("refused_capital") or []) if isinstance(_jg.get("refused_capital"), list)
                       else "ALL", (_jg.get("counts") or {}).get("required", "?"),
                       ", ".join(_jg["refused_capital"])[:300]
                       if isinstance(_jg.get("refused_capital"), list) else "scope UNKNOWN",
                       _blk.get("split_reason_line") or ""))
            # ISA-0807: re-read the LIVE control at build time - it may have been set after the
            # pre-run bundle. A blocking control the bundle did not carry still renders.
            try:
                import new_capital_control as _ncc_m
                _ncc_live = _ncc_m.status()
            except Exception as _ncc_e:                                 # noqa: BLE001
                _ncc_live = {"state": "UNKNOWN", "blocks_new_stock_capital": True,
                             "blocks_new_fund_capital": True,
                             "reasons": ["control unreadable: %s" % _ncc_e]}
            if _ncc_live.get("blocks_new_stock_capital") is not False and not _blk.get("new_capital_control_line"):
                _blk["new_capital_control_line"] = _ep.new_capital_control_line(
                    _ncc_live, _cds.get("held_pending_stock_decision_gbp"))
                _blk.setdefault("warnings", []).append(
                    "ISA-0807: the new-stock-capital control is %s at email build time but the "
                    "pre-run plan was formed without it - re-run the pre-run router before relying "
                    "on any stock figure in this section." % _ncc_live.get("state"))
            # ISA-0833: the fund verdict is re-read too; a refusal the bundle did not carry still renders.
            if _ncc_live.get("blocks_new_fund_capital") is not False and not _blk.get("fund_leg_line"):
                _blk["fund_leg_line"] = _ep.fund_leg_line(None, _ncc_live)
                _blk.setdefault("warnings", []).append(
                    "ISA-0833: new fund capital is refused at email build time (acceptance %s) - no fund "
                    "purchase from new money in this section is authorised."
                    % ((_ncc_live.get("fund_leg") or {}).get("acceptance_state") or "UNREADABLE"))
            # ISA-0804: the ONE Capital Decision Receipt, re-verified at build time against the
            # CURRENT inputs (a receipt formed on other inputs renders STALE, never as current).
            _cde_m = None
            try:
                import capital_decision_engine as _cde_m
                _cdv = _cde_m.verify_receipt(_cde_month) if _cde_month else {"state": "NO_MONTH"}
            except Exception as _cde_e:                                 # noqa: BLE001
                _cdv = {"state": "UNREADABLE", "why": str(_cde_e)}
            # ISA-0816: the review's case loop re-forms the receipt AFTER the pre-run bundle was
            # written; the email renders the receipt that VERIFIES now, never the bundle's copy.
            _cde_sum = _engine_summary_for_render(_cde_sum, _cdv,
                                                  getattr(_cde_m, "summary", None))
            _blk["decision_engine_line"] = _ep.decision_engine_line(_cde_sum, _cdv.get("state"))
            _blk["dealability_line"] = _ep.dealability_confirmation_line(_cde_sum)       # ISA-0860
            # ISA-0465/0700 (19-Oct-2026): the concentration SHADOW result + LIVE readiness, every run (Raj's reminder)
            try:
                import concentration_control as _ccm_e
                _rd465 = _ccm_e.live_readiness()
            except Exception as _rde:                                   # noqa: BLE001
                _rd465 = {"state": "UNREADABLE", "blockers": ["%s: %s" % (type(_rde).__name__, _rde)]}
            _blk["concentration_line"] = _ep.concentration_line(_cde_sum, _rd465)
            _blk["switch_contest_line"] = _ep.switch_contest_line(_cde_sum)                # ISA-0621
            _blk["appetite_guard_line"] = _ep.appetite_guard_line(_cde_sum)                # P-D ISA-0887/0888
            data.setdefault("meta", {})["capital_decision_engine"] = {
                "verification": _cdv.get("state"), "receipt_id": (_cde_sum or {}).get("receipt_id"),
                "state": (_cde_sum or {}).get("state"), "authority": (_cde_sum or {}).get("engine_authority")}
            data.setdefault("meta", {})["new_capital_control"] = {
                k: _ncc_live.get(k) for k in ("state", "blocks_new_stock_capital", "blocks_new_fund_capital", "record_id",
                                              "failed_contracts", "policy_state", "policy_hard_max")}
            data.setdefault("s2_capital_allocation", {})["capital_router"] = _blk
            data.setdefault("meta", {})["judgement_gate"] = {
                k: _jg.get(k) for k in ("scope_state", "counts", "refused_capital",
                                        "rerouted", "plan_source", "applied_at")}
            print(f"  §7.6.2 judgement gate (ISA-0698): scope {_jg.get('scope_state')}, counts "
                  f"{_jg.get('counts')}, refused new capital: {_jg.get('refused_capital')}")
    else:
        print("  §7.6.2 conviction gate: SKIPPED — month label could not be inferred; "
              "pass --month to enforce it.")

    # ⚑ ISA-0904 (Raj 26-Oct-2026): the cash-fund sale that funds the FINAL decided purchases - computed by
    #   mmf_funding (not by this renderer) from the run's portfolio snapshot, the D23 reserve and the drawdown
    #   reservation, at build time because the review's decisions are final only now.
    try:
        import mmf_funding as _mf0904
        _pp0904 = (os.path.join(os.path.dirname(os.path.abspath(args.data)), f"portfolio_data_{_month}.json")
                   if _month else None)
        _acts0904 = (data.get("s1_decision_summary") or {}).get("actions") or []
        if _pp0904 and os.path.exists(_pp0904):
            _fp0904 = _mf0904.from_files(_acts0904, _pp0904, root=os.path.dirname(os.path.abspath(__file__)))
        else:
            _fp0904 = {"state": "ABSENT", "line": "Funding cannot be stated: this run's portfolio snapshot was not found "
                                                  "beside the email data."}
    except Exception as _e0904:                                         # noqa: BLE001
        _fp0904 = {"state": "ERROR", "line": "Funding not computed (%s: %s) - check cash and the cash fund before dealing."
                                             % (type(_e0904).__name__, _e0904)}
    data.setdefault("s1_decision_summary", {})["funding_plan"] = _fp0904
    print("  ISA-0904 funding: %s - %s" % (_fp0904.get("state"), str(_fp0904.get("line"))[:160]))
    print("Building email body...")
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    # The COMPLETE report is built first and ALWAYS written, whether or not --lean is used.
    # Lean is a delivery constraint on the email, never on the record: the artefact on disk is
    # the record, and it must not depend on what fitted in a mail transport.
    full_body = build_report_body(data)
    full_path = args.full_output or (os.path.splitext(args.output)[0] + "_full.html")
    # ⚑ ISA-0793: an email with '[Claude fills ...]' text left in it is not a finished report.
    _ph = unfilled_placeholders(full_body)
    if _ph:
        print("  PLACEHOLDERS: %d unfilled '[Claude ...]' token(s) in the complete report:" % sum(_ph.values()))
        for _k, _v in sorted(_ph.items(), key=lambda kv: -kv[1])[:40]:
            print("    %3d x %s" % (_v, _k[:120]))
        if not args.allow_placeholders:
            print("ERROR (ISA-0793): fill every placeholder in email_data_[mmm_yyyy].json and rebuild. "
                  "No email body was written.")
            sys.exit(3)
        print("  PLACEHOLDERS ALLOWED (--allow-placeholders): SHADOW/rehearsal build - NOT sendable.")
    # ⚑ ISA-0642 B10-R05 (5): the template is a historical mock-up; its marker must be removed when populated.
    if data.get("_template_marker"):
        print("  TEMPLATE MARKER present (%s): the data still carries the template's example values." % data["_template_marker"])
        if not args.allow_placeholders:
            print("ERROR (ISA-0642): remove `_template_marker` only after every example value is replaced. "
                  "No email body was written.")
            sys.exit(3)
    # ⚑ ISA-0898: the three-layer writing standard is a precondition of a SENDABLE report, like the placeholders.
    _ni = narrative_issues(data)
    if _ni:
        print("  NARRATIVE GATE (ISA-0898): %d issue(s):" % len(_ni))
        for _w, _p in _ni[:40]:
            print("    - %s: %s" % (_w, _p))
        if not args.allow_placeholders:
            print("ERROR (ISA-0898): write the missing layers / price ranges / plain wording in "
                  "email_data_[mmm_yyyy].json and rebuild. No email body was written.")
            sys.exit(4)
        print("  NARRATIVE GAPS ALLOWED (--allow-placeholders): SHADOW/rehearsal build - NOT sendable.")
    lean_rep = {}
    if args.lean:
        with open(full_path, "w", encoding="ascii", errors="xmlcharrefreplace") as f:
            f.write(build_report_page(data))
        body, lean_rep = build_lean_body(data, budget=args.max_bytes, full_path=full_path,
                                         report_url=args.report_url)
        lean_rep["full_report"] = full_path
        lean_rep["narrative_issues"] = len(_ni)
    else:
        body = full_body

    # Verify no non-ASCII remains
    ok = verify_entities(body)
    if not ok:
        print("ERROR: Non-ASCII characters detected. Fix before sending.")
        sys.exit(1)

    with open(args.output, "w", encoding="ascii", errors="xmlcharrefreplace") as f:
        f.write(body)

    if args.lean:
        json.dump(lean_rep, open(os.path.splitext(args.output)[0] + "_lean_report.json", "w"),
                  indent=2)
        # ⚑ ISA-0642 B10-R05 (3): the omission pattern is APPENDED, never overwritten, so the drop
        #   order can be reviewed run over run (artefact contract: lean_history.jsonl APPEND_ONLY_HISTORY).
        try:
            import datetime as _dt0642
            _wc = lean_rep.get("warnings_coverage") or {}
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lean_history.jsonl"), "a",
                      encoding="utf-8") as _lh:
                _lh.write(json.dumps({"built_at": _dt0642.datetime.now().isoformat(timespec="seconds"),
                                      "email": os.path.basename(args.output),
                                      "subject_month": (data.get("meta") or {}).get("run_month_label"),
                                      "sections_omitted": [], "mode": "brief+report", "report_ref": lean_rep.get("report_ref"),
                                      "bytes_full": lean_rep.get("full_bytes"), "bytes_final": lean_rep.get("bytes_final"),
                                      "within_budget": lean_rep.get("within_budget"),
                                      "warnings_full": _wc.get("n_full"), "warnings_missing": len(_wc.get("missing") or [])}) + "\n")
        except Exception as _e0642:                                   # noqa: BLE001
            print("  WARN lean_history.jsonl not appended: %s: %s" % (type(_e0642).__name__, _e0642))
        _wc = lean_rep.get("warnings_coverage") or {}
        print("  BRIEF: flagged items stated per chapter %s (report total %s)%s" % (
              _wc.get("by_chapter"), _wc.get("n_full"),
              "" if _wc.get("ok", True) else "  \u2691 MISMATCH %s - do not send this copy" % (_wc.get("missing") or [])))
        print(f"  BRIEF: {lean_rep['bytes_final']:,} bytes (report {lean_rep['full_bytes']:,}); budget "
              f"{args.max_bytes:,} -> {'WITHIN' if lean_rep['within_budget'] else 'OVER'}")
        print(f"  REPORT page written to {full_path}"
              + (f" (brief links to {args.report_url})" if args.report_url else " (brief says: attached)"))
        if not lean_rep["within_budget"]:
            print("  \u2691 BRIEF OVER BUDGET - do not assume the send will succeed; send the report as the attachment "
                  "and investigate why the brief grew (ISA-0898).")

    meta       = data.get("meta", {})
    subject    = email_subject_for(meta)
    char_count = len(body)
    print(f"Email body written: {args.output}")
    print(f"  Body length: {char_count:,} characters")
    print(f"  Entity check: {'PASS' if ok else 'FAIL'}")
    print()
    print("=" * 65)
    print("GMAIL_SEND_EMAIL parameters:")
    print(f"  recipient_email: rjobanputra@sky.com")
    print(f"  sender_email:    raj.a.jobanputra@gmail.com")
    print(f"  subject:         {subject}")
    print(f"  body:            <contents of {args.output}>")
    print("  is_html:         true   (MANDATORY -- always pass is_html=true)")
    print("=" * 65)



def _engine_summary_for_render(bundle_sum, verification, summarise):
    """ISA-0816: choose the receipt view the email renders. A receipt that VERIFIES at build time is
    the source of truth (the review's case loop may have re-formed it after the pre-run bundle);
    otherwise the bundle's view renders with the failed verification state beside it. Pure."""
    rec = (verification or {}).get("receipt") if isinstance(verification, dict) else None
    if (verification or {}).get("state") == "VALID" and isinstance(rec, dict) and callable(summarise):
        out = dict(summarise(rec))
        out["rendered_from"] = "VERIFIED_RECEIPT_AT_BUILD"
        if isinstance(bundle_sum, dict) and bundle_sum.get("receipt_id") not in (None, out.get("receipt_id")):
            out["superseded_bundle_receipt_id"] = bundle_sum.get("receipt_id")
        return out
    return bundle_sum



def _selftest():
    """Renderer must-fires and negative controls (ISA-0722/0727/0793/0795/0804/0816/0642/0898/0900)."""
    import email_prefill as _ep
    # ISA-0804 — the engine receipt line reaches the rendered router section (renderer, not engine)
    _h804 = _render_capital_router({"capital_router": {"split_line": "x",
                                                       "decision_engine_line": "Capital decision engine (ISA-0804, SHADOW, receipt CDR-T)"}})
    assert "CDR-T" in _h804, "MUST-FIRE ISA-0804: the receipt line must render in the router section"
    _h465 = _render_capital_router({"capital_router": {"split_line": "x", "concentration_line": "Concentration caps ... DECISION REQUIRED (Raj)"}})
    assert "DECISION REQUIRED" in _h465 and "<strong>" in _h465, "MUST-FIRE ISA-0465: the decision prompt renders, emphasised"
    assert "ISA-0804" not in _render_capital_router({"capital_router": {"split_line": "x"}}), \
        "NEGATIVE CONTROL ISA-0804: no receipt line, nothing invented"
    # ISA-0816 — the verified receipt supersedes the bundle's copy; a non-VALID one never does
    _sm = lambda r: {"receipt_id": r["receipt_id"], "state": r["state"]}
    _v = _engine_summary_for_render({"receipt_id": "OLD", "state": "RESEARCH_PENDING"},
                                    {"state": "VALID", "receipt": {"receipt_id": "NEW", "state": "DECISION_COMPLETE"}}, _sm)
    assert _v["receipt_id"] == "NEW" and _v["superseded_bundle_receipt_id"] == "OLD", "MUST-FIRE ISA-0816"
    _v2 = _engine_summary_for_render({"receipt_id": "OLD"}, {"state": "STALE", "receipt": {"receipt_id": "NEW", "state": "X"}}, _sm)
    assert _v2["receipt_id"] == "OLD", "NEGATIVE CONTROL ISA-0816: a STALE receipt never replaces the bundle view"
    blk = _ep.build_held_underwriting_block({
        "state": "OK", "n_cases_held": 1, "n_expected": 1, "by_state": {"MISSING_ROW": ["ZZZ"]},
        "not_captured_original": ["ZZZ"],
        "rows": [{"ticker": "ZZZ", "original_state": "NOT_CAPTURED_CONTEMPORANEOUSLY",
                  "current_state": "MISSING_ROW", "admissible_for_positive_size": False,
                  "delta": {"comparable": False}, "current_case_id": "UWC-z"}]})
    html = build_s7({"s7_stock_sleeve": {"held_underwriting": blk}})
    assert "NOT_CAPTURED_CONTEMPORANEOUSLY" in html and "MISSING_ROW" in html and "UWC-z" in html, (
        "⚑ MUST-FIRE: every held row reaches section 7 with its STATE", html[:300])
    assert "Not recorded at purchase" in html, "ISA-0898: the state code carries its plain label"
    none = build_s7({"s7_stock_sleeve": {}})
    assert "What we expected at purchase" not in none, (
        "NEGATIVE CONTROL: no block -> no table (an empty table would read as 'all clean')")
    # ISA-0727 (M14) - the renderer refuses a stale plan
    _ab = {"input_snapshot_id": "ISN-a", "components": {"build_id": "TB-A", "config_roll": "c",
           "portfolio_snapshot": "p", "opportunity_set_id": "OPS-1", "underwriting_store": "u",
           "broker_inputs": "b", "capital_amount_gbp": 100.0}}
    _rc = {"summary": {"capital_destination": {"approval_binding": _ab}}}
    v, m = approval_gate(_rc, current_state_fn=lambda ab: ab)
    assert v["state"] == "VALID" and m is None, "NEGATIVE CONTROL ISA-0727: an unchanged state renders"
    v, m = approval_gate(_rc, current_state_fn=lambda ab: {"input_snapshot_id": "ISN-b", "components": dict(
        ab["components"], opportunity_set_id="OPS-2")})
    assert v["state"] == "STALE" and m and "opportunity_set_id" in m, \
        "⚑ MUST-FIRE ISA-0727: a plan formed on a different feasible population must not render as current"
    v, m = approval_gate(_rc, current_state_fn=lambda ab: 1 / 0)
    assert v["state"] == "UNKNOWN" and m, "MUST-FIRE ISA-0727: an un-checkable binding blocks, never passes"
    v, m = approval_gate({"summary": {"capital_destination": {}}})
    assert v["state"] == "UNBOUND" and m is None, "ISA-0727: a pre-control plan is UNBOUND - reported, not blocked"
    # ISA-0795 - the subject is the declared run-month subject, never the data month
    import datetime as _d795
    assert email_subject_for({"email_subject": "Monthly ISA Portfolio Review - Oct 2026",
                              "run_month_label": "Sep 2026"}) == "Monthly ISA Portfolio Review - Oct 2026", \
        "⚑ MUST-FIRE ISA-0795: the declared subject wins over the data-month label"
    assert email_subject_for({"run_month_label": "Sep 2026"}, today=_d795.date(2026, 10, 4)) \
        == "Monthly ISA Portfolio Review - Oct 2026", \
        "NEGATIVE CONTROL ISA-0795: legacy email data (no field) never falls back to the data month"
    # ISA-0793 - the placeholder gate
    assert unfilled_placeholders("<p>[Claude fills]</p><td>[Claude: thesis/performance note]</td>[Claude fills]") \
        == {"[Claude fills]": 2, "[Claude: thesis/performance note]": 1}, "⚑ MUST-FIRE ISA-0793: tokens counted"
    assert unfilled_placeholders("<p>Claude filled this: [see note] and [Clause 4]</p>") == {}, \
        "NEGATIVE CONTROL ISA-0793: ordinary bracketed text is not a placeholder"
    import inspect as _ins793
    _m = _ins793.getsource(main)
    assert "sys.exit(3)" in _m and _m.index("unfilled_placeholders(full_body)") < _m.index("open(args.output, \"w\""), \
        "ISA-0793: the gate runs BEFORE the email body file is written"
    assert "sys.exit(4)" in _m and _m.index("narrative_issues(data)") < _m.index("open(args.output, \"w\""), \
        "ISA-0898: the narrative gate runs BEFORE the email body file is written"
    # ── ISA-0642 / ISA-0898: the brief carries every warning row of the report ─────────────
    _pl = {"meta": {}, "s7_stock_sleeve": {"v21": {"absent_line": "PLANTED engine output ABSENT for the brief test"}}}
    _rep = {}
    _lb = build_email_body(_pl, lean=True, lean_report=_rep, full_path="x_full.html")
    _cov = _rep["warnings_coverage"]
    assert _cov["ok"] and _cov["by_chapter"].get("shares", 0) >= 1, ("⚑ MUST-FIRE ISA-0642: the s7 warning is counted "
                                                                      "under its chapter and the brief states it", _cov)
    assert "PLANTED engine output ABSENT" in build_report_body(_pl), "ISA-0642: the warning itself is in the report"
    assert "x_full.html" in _lb, "ISA-0898: the brief names the report"
    _bad = _with_warning_coverage(_pl, re.sub(r"(Your shares</td>\s*<td[^>]*>)(\d+)", lambda m: m.group(1) + "99", _lb), {})
    assert not _bad["warnings_coverage"]["ok"], "MUST-FIRE ISA-0642: a brief whose count disagrees with the report is caught"
    _red = _warning_rows('<p>%s</p><p><strong style="color:%s">Within your limit</strong></p>'
                         % (flag("Above your limit", "red"), C["green"]))
    assert [t for t, _ in _red] == ["Above your limit"], \
        ("ISA-0898: a red flag is a warning row (must-fire); a green verdict is not (negative control)", _red)
    assert "NON-CURRENT EXAMPLE" in build_email_body({"meta": {}, "_template_marker": "T"}), "MUST-FIRE ISA-0642 (5)"
    assert "NON-CURRENT EXAMPLE" in build_email_body({"meta": {}, "_template_marker": "T"}, lean=True), "MUST-FIRE ISA-0642 (5) brief"
    assert "NON-CURRENT EXAMPLE" not in build_email_body({"meta": {}}), "NEGATIVE CONTROL ISA-0642 (5)"
    _m0642 = _ins793.getsource(main)
    assert _m0642.index('data.get("_template_marker")') < _m0642.index("open(args.output, \"w\""), \
        "ISA-0642 (5): the template refusal runs BEFORE the email body file is written"
    _rb = build_report_body({"meta": {}})
    assert all(f'id="{k}"' in _rb for k, _ in CHAPTERS), "ISA-0898: every chapter is present, even when empty"
    # ── ISA-0898: the writing standard and the presentation contract ─────────────────────
    _buy = {"action": "BUY", "instrument": "Matador Resources", "ticker": "MTDR", "verdict": "Buy 40 shares",
            "plain_english": "An oil and gas producer bought because its costs are low."}
    _d = {"meta": {}, "s1_decision_summary": {"verdict": "v", "plain_english": "p", "attention_plain": ["a"],
                                              "actions": [_buy]}}
    _ni = narrative_issues(_d)
    assert any("price range" in p for _w, p in _ni), "⚑ MUST-FIRE ISA-0898: a BUY with no acceptable price range"
    _d["s1_decision_summary"]["actions"] = [dict(_buy, price_low="53.50", price_max="67.74", price_ccy="$", amount_gbp=5600.0)]
    assert not [p for w, p in narrative_issues(_d) if w.startswith("decision: Matador")], \
        "NEGATIVE CONTROL ISA-0898: a complete action passes the gate"
    assert "$53.50 to $67.74 per share" in build_brief(_d), "ISA-0898: the brief states the acceptable price range"
    _d["s1_decision_summary"]["plain_english"] = "The ER12 engine moved ISA-0123 to D21."
    assert sum("jargon" in p for _w, p in narrative_issues(_d)) >= 3, "⚑ MUST-FIRE ISA-0898: jargon in plain English"
    _fb = funding_box({"state": "SELL_CASH_FUND", "units_to_sell": 4, "line": "Sell 4 units of X first",
                       "buys_gbp": 100.0, "broker_cash_gbp": 10.0, "reserve_gbp": 250.0, "shortfall_gbp": 90.0})
    assert "Sell 4 units of the cash fund first" in _fb and "Funding the purchases" in _fb, "MUST-FIRE ISA-0904: the sale renders"
    assert funding_box({"state": "NO_PURCHASE"}) == "", "NEGATIVE CONTROL ISA-0904: nothing to fund -> nothing shown"
    assert "Funding not yet stated" in funding_box({"state": "AMOUNT_NOT_STATED", "line": "x"}), "ISA-0904: a gap is flagged"
    assert "Sell 4 units" in build_brief({"meta": {}, "s1_decision_summary": {"actions": [{"action": "BUY"}],
                                          "funding_plan": {"state": "SELL_CASH_FUND", "units_to_sell": 4, "line": "Sell 4 units"}}}), \
        "ISA-0904: the funding instruction is in the brief, with the decisions"
    assert "sys.exit(4)" in _m and _m.index("mmf_funding") < _m.index("open(args.output, \"w\""), \
        "ISA-0904: the funding plan is formed before the body is written"
    assert "Portfolio value" in headline_panel({"meta": {}}) and "Investment gain" in headline_panel({"meta": {}}), \
        "ISA-0898: the headline panel renders even with no measures (absence stated)"
    assert "B5TP8W8" in _named("B5TP8W8") and ("name not on file" in _named("B5TP8W8")
                                                 or instrument_name("B5TP8W8")), \
        "ISA-0898: a code never stands alone - it carries its name, or says the name is not on file"
    assert C["red"] in table_row(["Loss", "-5.0%"]), "ISA-0898: a negative figure is red"
    assert C["red"] not in table_row(["Gain", "+5.0%"]), "NEGATIVE CONTROL: a positive figure is not red"
    _s7c = build_s7({"s7_stock_sleeve": {"integrity_lines": ["Correlation coverage: 216 names ...", "Other line"]}})
    assert "Correlation coverage" not in _s7c and "Other line" in _s7c, "ISA-0899: the correlation dump is gone"
    _s8n = build_s8({"s8_fund_review": {"step8a_summary": {"section_a": {"result": "PASS", "value": "x",
                                                                         "bands_note": "bands None-None"}}}})
    assert "None" not in _text_of(_s8n), "ISA-0902: no 'None' leak in the fund summary"
    # ── ISA-0900: the account statement renders, and absence is stated ──────────────────
    _rmh = render_report_measures({"state": "OK", "bridge": {"state": "OK", "from": "2026-10-02", "to": "2026-10-22",
                                   "opening_gbp": 100.0, "closing_gbp": 90.0, "money_added_gbp": 0, "investment_gain_gbp": -10.0,
                                   "parts": {"shares_gbp": -10.0}, "residual_gbp": 0.0, "tolerance_gbp": 5.0,
                                   "contributions": [{"code": "MU", "name": "Micron", "contribution_gbp": -10.0}]}})
    assert "Reconciles" in _rmh and "Micron" in _rmh and C["red"] in _rmh, "ISA-0900: bridge + contribution render"
    assert "Not computed" in render_report_measures({"state": "ABSENT"}), "MUST-FIRE ISA-0900: absent is stated"
    # account-return renderer (email_prefill.apply_account_return)
    _ok = {"state": "OK", "twr_pct": 16.94, "mwr_pct": 17.53, "start": {"date": "2025-12-31", "value_gbp": 104781.0, "source": "Q4"},
           "end": {"date": "2026-10-02", "value_gbp": 160391.1, "source": "x"}, "flows_total_gbp": 34000.0, "basis": "APPROXIMATE TWR"}
    _s6 = _ep.apply_account_return({"performance": [{"period": "YTD", "portfolio": "+24.56%"}]},
                                   {"calendar_ytd": _ok, "tax_year_to_date": {"state": "UNVERIFIED_FLOWS", "why": "w"}})
    _p = [r["period"] for r in _s6["performance"]]
    assert _p[0].startswith("YTD (X-Ray") and "+16.94%" in [r["portfolio"] for r in _s6["performance"]] \
        and "UNVERIFIED_FLOWS" in [r["portfolio"] for r in _s6["performance"]], ("MUST-FIRE ISA-0642 (6) renderer", _s6)
    assert "ABSENT" in _ep.apply_account_return({}, None)["account_return_line"], "NEGATIVE CONTROL ISA-0642 (6): absent is stated"
    assert _ep.apply_report_measures({}, None)["report_measures"]["state"] == "ABSENT", "NEGATIVE CONTROL ISA-0900 feed"
    print("build_monthly_isa_email selftest OK (router/receipt/approval/subject/placeholder gates; ISA-0642 brief carries "
          "every report warning; ISA-0898 narrative gate, price range, names-first, red negatives, no None, correlation "
          "dump gone; ISA-0900 account statement)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    main()
