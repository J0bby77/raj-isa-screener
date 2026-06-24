#!/usr/bin/env python3
"""
normalise_adapter.py  --  ISA Part A/B Formatter (was the former pre-run formatter), Conviction Scorer & Email Prep
Version: 2.0  |  2026-06-01

Reads watchlist_metrics_mmm_yyyy.json and produces watchlist_scored_mmm_yyyy.json.
Supports three source pipelines — routing is driven by each entry's "_source_pipeline" field:

  "growth_stock"  — standard 14-metric Part A (max 28) + 11-metric Part B (max 22) = /50
                    Conviction: 10-dimension framework /100 at Step 9.
                    Disparity flag: analyst rating != buy when total >= 40.
  "energy"        — 10-metric Part A (max 20) + 8-metric Part B (max 16) = /36
                    Different thresholds: CapEx intensity positive, 52wk position inverted,
                    no FCF hard gate, no gross margin hard gate.
                    Disparity flag: analyst rating != buy when total >= 28.
  "vci"           — ACS /100 from VCI pipeline (no Part A/B scoring tables).
                    Deployment threshold: ACS >=75. NVIDIA-class: ACS >=85.
                    Disparity: not applicable.

Responsibilities:
  1. Format Part A / Part B into email-ready table rows per pipeline
  2. Compute combined scores, Part A/B statuses
  3. Detect analyst disparity (pipeline-aware thresholds)
  4. Detect in-window names
  5. Build email-ready s5 (watchlist rows), s7 (sleeve rows), s3 (investment case skeletons)
  6. Build conviction ranking (pipeline-aware score display)

Usage (standalone):
    python3 the former pre-run formatter
        --metrics watchlist_metrics_mmm_yyyy.json
        [--out watchlist_scored_mmm_yyyy.json]

Called by: monthly_isa_prerun.py (Step 5)
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import scoring_config as _cfg

# ---------------------------------------------------------------------------
# GROWTH STOCK thresholds (must match screener_core.py constants exactly)
# ---------------------------------------------------------------------------
PART_A_STRONG_THRESHOLD   = 22    # /28
PART_A_ACCEPTABLE_MIN     = 14
PART_B_STRONG_THRESHOLD   = 16    # /22 (canonical in scoring_config; overridden before run())
PART_B_ACCEPTABLE_MIN     = 11
HIGH_SCORE_THRESHOLD      = 37    # /50 (canonical in scoring_config; overridden before run())

# Analyst rating values that do NOT trigger disparity flag
STRONG_RATINGS = {"strongbuy", "strong buy", "buy"}

# Growth stock conviction brackets (/50 → preliminary; refined to /100 at Step 9)
# Claude overrides with the 10-dimension conviction score at Step 9.
SCORE_TO_PRELIM_CONVICTION = [
    (50, "High Conviction",   "high",   "[Claude: refine to /100 at Step 9]"),
    (44, "Medium Conviction", "medium", "[Claude: refine to /100 at Step 9]"),
    (38, "Watch but Wait",    "low",    "[Claude: refine to /100 at Step 9]"),
    (0,  "No Action",         "low",    "[Claude: refine to /100 at Step 9]"),
]

# ---------------------------------------------------------------------------
# ENERGY thresholds (must match energy_screener.py constants exactly)
# ---------------------------------------------------------------------------
ENERGY_PART_A_STRONG      = 14    # /20
ENERGY_PART_A_ACCEPTABLE  = 8
ENERGY_PART_B_STRONG      = 11    # /16
ENERGY_PART_B_WATCH       = 6
ENERGY_HIGH_SCORE         = 28    # /36 (~78%, proportional to growth 37/50) — disparity trigger

# Energy conviction brackets (/36 → preliminary bracket)
# Calibrated to energy_screener's ENERGY_STRONG_BUY / ENERGY_WATCH logic.
SCORE_TO_PRELIM_CONVICTION_ENERGY = [
    (30, "High Conviction",   "high",   "[Claude: refine to /100 at Step 9]"),
    (25, "Medium Conviction", "medium", "[Claude: refine to /100 at Step 9]"),
    (18, "Watch but Wait",    "low",    "[Claude: refine to /100 at Step 9]"),
    (0,  "No Action",         "low",    "[Claude: refine to /100 at Step 9]"),
]

# ---------------------------------------------------------------------------
# VCI thresholds (must match vci_acs_scorer.py / VCI run spec)
# ---------------------------------------------------------------------------
VCI_DEPLOYMENT_THRESHOLD  = 75    # ACS >= 75 → deployment eligible
VCI_NVIDIA_CLASS          = 85    # ACS >= 85 → NVIDIA-class

# VCI conviction brackets (ACS /100)
SCORE_TO_PRELIM_CONVICTION_VCI = [
    (85, "NVIDIA-Class / Very High Conviction", "high",
     "ACS >=85 — NVIDIA-class pattern confirmed; highest priority for deployment"),
    (75, "High Conviction",                     "high",
     "ACS >=75 — deployment threshold met; proceed to Step 9 conviction score"),
    (60, "Medium / Pipeline Watch",             "medium",
     "ACS 60-74 — pipeline candidate; not yet at deployment threshold; monitor catalysts"),
    (0,  "Below Threshold",                     "low",
     "ACS <60 — insufficient asymmetric conviction; observe only"),
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_pct(v, decimals=1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v)*100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"

def fmt_x(v, decimals=1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}×"
    except (TypeError, ValueError):
        return "—"

def fmt_val(v, decimals=2, prefix="") -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    try:
        return f"{prefix}{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "—"

def fmt_price(v, currency="USD") -> str:
    """Format a price with appropriate symbol."""
    if v is None:
        return "—"
    symbols = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$"}
    sym = symbols.get(currency.upper(), "")
    try:
        return f"{sym}{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"

def score_signal(score: int) -> str:
    """Map 2/1/0 score to email signal colour string."""
    return {2: "green", 1: "amber", 0: "red"}.get(score, "amber")

def assessment_text(score: int, resolved: bool = True) -> str:
    """Map score to human-readable assessment."""
    if not resolved:
        return "Unresolved"
    return {2: "Strong", 1: "Acceptable", 0: "Weak"}.get(score, "Weak")

def is_resolved(val) -> bool:
    """Returns True if a metric value is available (not None/NaN)."""
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    return True


# ---------------------------------------------------------------------------
# Part A — 14 metric row builders
# ---------------------------------------------------------------------------
def build_part_a_table(s: dict) -> list[dict]:
    """
    Build Part A metrics table (14 rows) for email s3 investment case.
    Each row: {label, value, assessment, signal, score, threshold_note}.
    """
    rows = []

    def row(label, value_fmt, score, resolved, threshold_note=""):
        ass = assessment_text(score, resolved)
        sig = score_signal(score) if resolved else "amber"
        return {
            "label":          label,
            "value":          value_fmt if resolved else "Unresolved",
            "assessment":     ass,
            "signal":         sig,
            "score":          score,
            "max_score":      2,
            "threshold_note": threshold_note,
        }

    # 1. ROIC (hard gate)
    roic = s.get("roic")
    hg   = s.get("roic_hardgate", "")
    rows.append({
        **row("ROIC", fmt_pct(roic), s.get("score_roic", 0), is_resolved(roic),
              "Strong >15% | Acceptable 8–15% | Weak <8%"),
        "hard_gate": True,
        "hard_gate_status": hg,
        "label": "ROIC (Hard Gate)",
    })

    # 2. FCF Positive Years (hard gate)
    fcf_pos = s.get("fcf_positive_years")
    fcf_hg  = s.get("fcf_hardgate", "")
    rows.append({
        **row("FCF Positive Years", f"{int(fcf_pos)} of 5yr" if is_resolved(fcf_pos) else "—",
              s.get("score_fcf_pos", 0), is_resolved(fcf_pos),
              "Strong >=4yr | Acceptable 3yr | Weak <3yr"),
        "hard_gate": True,
        "hard_gate_status": fcf_hg,
        "label": "FCF Positive Years (Hard Gate)",
    })

    # 3. Revenue CAGR
    rc = s.get("rev_cagr")
    basis = s.get("revenue_cagr_basis", "3yr")
    rows.append(row(f"Revenue CAGR ({basis})", fmt_pct(rc),
                    s.get("score_rev_cagr", 0), is_resolved(rc),
                    "Strong >=15% | Acceptable >=5% | Weak <5%"))

    # 4. Recent Revenue Growth
    rg = s.get("recent_rev_growth")
    rb = s.get("recent_rev_basis", "")
    rows.append(row(f"Recent Revenue Growth ({rb})", fmt_pct(rg),
                    s.get("score_recent_rev", 0), is_resolved(rg),
                    "Strong >=12% | Acceptable >=3% | Weak <3%"))

    # 5. EPS CAGR
    ec = s.get("eps_cagr")
    unreliable = s.get("eps_cagr_math_unreliable", False)
    rows.append(row("EPS CAGR 3yr",
                    fmt_pct(ec) if (is_resolved(ec) and not unreliable) else ("Math unreliable" if unreliable else "—"),
                    s.get("score_eps_cagr", 0), is_resolved(ec) and not unreliable,
                    "Strong >=15% | Acceptable >=5% | Weak <5%"))

    # 6. Share Count Change
    sc_chg = s.get("share_count_change")
    sc_fmt = "Buyback" if (is_resolved(sc_chg) and sc_chg < 0) else fmt_pct(sc_chg)
    rows.append(row("Share Count Change (ann.)", sc_fmt,
                    s.get("score_share_count", 0), is_resolved(sc_chg),
                    "Strong: shrinking (buyback) | Acceptable: 0–1% dilution | Weak: >1%"))

    # 7. FCF CAGR
    fc = s.get("fcf_cagr")
    unreliable_fc = s.get("fcf_cagr_math_unreliable", False)
    rows.append(row("FCF CAGR 3yr",
                    fmt_pct(fc) if (is_resolved(fc) and not unreliable_fc) else ("Math unreliable" if unreliable_fc else "—"),
                    s.get("score_fcf_cagr", 0), is_resolved(fc) and not unreliable_fc,
                    "Strong >=12% | Acceptable >=3% | Weak <3%"))

    # 8. FCF Margin
    fm = s.get("fcf_margin")
    rows.append(row("FCF Margin", fmt_pct(fm),
                    s.get("score_fcf_margin", 0), is_resolved(fm),
                    "Strong >=10% | Acceptable >=5% | Weak <5%"))

    # 9. Gross Margin
    gm = s.get("gross_margin")
    gm_lbl = s.get("gross_margin_label", "")
    rows.append(row(f"Gross Margin ({gm_lbl})", fmt_pct(gm),
                    s.get("score_gross_margin", 0), is_resolved(gm),
                    "Strong >40% | Acceptable 20–40% | Weak <20%"))

    # 10. Operating Margin
    om = s.get("operating_margin")
    rows.append(row("Operating Margin", fmt_pct(om),
                    s.get("score_op_margin", 0), is_resolved(om),
                    "Strong >=15% | Acceptable >=8% | Weak <8%"))

    # 11. Operating Margin Trend
    omt = s.get("op_margin_trend")
    omt_fmt = (f"{omt*100:+.1f}pp (3yr)" if is_resolved(omt) else "—")
    rows.append(row("Operating Margin Trend (3yr)", omt_fmt,
                    s.get("score_op_margin_trend", 0), is_resolved(omt),
                    "Strong >+2pp | Acceptable within ±2pp | Weak declining >2pp"))

    # 12. Net Debt / EBITDA
    nde = s.get("net_debt_ebitda")
    if is_resolved(nde) and nde < 0:
        nde_fmt = "Net Cash"
    else:
        nde_fmt = fmt_x(nde)
    rows.append(row("Net Debt / EBITDA", nde_fmt,
                    s.get("score_nd_ebitda", 0), is_resolved(nde),
                    "Strong <=1.5× (or net cash) | Acceptable <=3.0× | Weak >3×"))

    # 13. Interest Coverage
    ic = s.get("interest_coverage")
    ic_lbl = s.get("interest_coverage_label", "")
    if ic_lbl == "NET_CASH_NO_MATERIAL_INTEREST_BURDEN":
        ic_fmt = "Net Cash"
    else:
        ic_fmt = fmt_x(ic)
    rows.append(row("Interest Coverage", ic_fmt,
                    s.get("score_int_cov", 0), True,
                    "Strong >=8× (or net cash) | Acceptable >=3× | Weak <3×"))

    # 14. CapEx Intensity
    ci = s.get("capex_intensity")
    rows.append(row("CapEx Intensity", fmt_pct(ci),
                    s.get("score_capex", 0), is_resolved(ci),
                    "Strong <8% of revenue | Acceptable <=15% | Weak >15%"))

    return rows


# ---------------------------------------------------------------------------
# Part B — 13 metric row builders
# ---------------------------------------------------------------------------
def build_part_b_table(s: dict) -> list[dict]:
    """
    Build Part B metrics table (13 rows) for email s3 investment case.
    Includes 3 reused from Part A (ROIC, ND/EBITDA, Int Coverage) + 10 new.
    """
    rows = []

    def row(label, value_fmt, score, resolved, threshold_note="", mandatory=False):
        ass = assessment_text(score, resolved)
        sig = score_signal(score) if resolved else "amber"
        return {
            "label":          label + (" (Mandatory Min)" if mandatory else ""),
            "value":          value_fmt if resolved else "Unresolved",
            "assessment":     ass,
            "signal":         sig,
            "score":          score,
            "max_score":      2,
            "threshold_note": threshold_note,
            "mandatory":      mandatory,
        }

    # Reused from Part A (mandatory minimums)
    roic = s.get("roic")
    rows.append(row("ROIC", fmt_pct(roic), s.get("score_roic", 0), is_resolved(roic),
                    "Strong >15% | Acceptable 8–15% | Weak <8%", mandatory=True))

    nde = s.get("net_debt_ebitda")
    nde_fmt = "Net Cash" if (is_resolved(nde) and nde < 0) else fmt_x(nde)
    nd_mand_fail = s.get("_nd_mand_fail", False)
    rows.append({
        **row("Net Debt / EBITDA", nde_fmt, s.get("score_nd_ebitda", 0), is_resolved(nde),
              "Strong <=1.5× | Acceptable <=3.0× | Mandatory fail if >3×", mandatory=True),
        "mandatory_fail": nd_mand_fail,
    })

    ic_lbl = s.get("interest_coverage_label", "")
    ic = s.get("interest_coverage")
    ic_fmt = "Net Cash" if ic_lbl == "NET_CASH_NO_MATERIAL_INTEREST_BURDEN" else fmt_x(ic)
    rows.append(row("Interest Coverage (reused)", ic_fmt, s.get("score_int_cov", 0), True,
                    "Strong >=8× (or net cash) | Acceptable >=3× | Weak <3×", mandatory=True))

    # New Part B metrics
    fwd_pe = s.get("fwd_pe")
    rows.append(row("Forward P/E", fmt_x(fwd_pe),
                    s.get("score_b_fwd_pe", 0), is_resolved(fwd_pe),
                    "Strong <20× | Acceptable <=30× | Weak >30×"))

    ev_eb = s.get("ev_ebitda")
    rows.append(row("EV/EBITDA", fmt_x(ev_eb),
                    s.get("score_b_ev_ebitda", 0), is_resolved(ev_eb),
                    "Strong <12× | Acceptable <=20× | Weak >20×"))

    pfcf = s.get("price_fcf")
    rows.append(row("Price / FCF", fmt_x(pfcf),
                    s.get("score_b_price_fcf", 0), is_resolved(pfcf),
                    "Strong <20× | Acceptable <=35× | Weak >35×"))

    fcfy = s.get("fcf_yield")
    rows.append(row("FCF Yield", fmt_pct(fcfy),
                    s.get("score_b_fcf_yield", 0), is_resolved(fcfy),
                    "Strong >5% | Acceptable >=3% | Weak <3%"))

    ey = s.get("earnings_yield")
    rows.append(row("Earnings Yield", fmt_pct(ey),
                    s.get("score_b_earn_yield", 0), is_resolved(ey),
                    "Strong >6% | Acceptable >=3% | Weak <3%"))

    pos52 = s.get("position_52wk")
    pos52_fmt = f"{pos52*100:.0f}% of 52wk range" if is_resolved(pos52) else "—"
    rows.append(row("Price vs 52-week Range", pos52_fmt,
                    s.get("score_b_52wk", 0), is_resolved(pos52),
                    "Strong: <15% above 52wk low | Acceptable: <30% | Weak: >=30%"))

    dpf = s.get("div_payout_fcf")
    dpf_fmt = "No dividend" if (is_resolved(dpf) and dpf == 0.0) else fmt_pct(dpf)
    rows.append(row("Dividend Payout / FCF", dpf_fmt,
                    s.get("score_b_div_payout", 0), True,
                    "Strong: no div or <60% payout | Acceptable: <=85% | Weak: >85% or div on -FCF"))

    feg = s.get("fwd_eps_growth")
    rows.append(row("Forward EPS Growth", fmt_pct(feg),
                    s.get("score_b_fwd_eps", 0), is_resolved(feg),
                    "Strong >12% | Acceptable >=5% | Weak <5%"))

    tu = s.get("target_upside")
    rows.append(row("Consensus Target Upside", fmt_pct(tu),
                    s.get("score_b_target_upside", 0), is_resolved(tu),
                    "Strong >20% | Acceptable >=10% | Weak <10%"))

    sn = s.get("stress_nd_ebitda")
    si = s.get("stress_int_cov")
    stress_fmt = f"ND/EBITDA {fmt_x(sn)} | Int Cov {fmt_x(si)} (at -25% EBITDA)" if (is_resolved(sn) or is_resolved(si)) else "—"
    rows.append(row("Downside Stress Test", stress_fmt,
                    s.get("score_b_stress", 0), True,
                    "Strong: ND/EBITDA <2× AND int cov >5× | Acceptable: <3×, >3× | Weak otherwise"))

    return rows


# ---------------------------------------------------------------------------
# ENERGY Part A — 10 metric row builders (max 20 pts)
# ---------------------------------------------------------------------------
def build_energy_part_a_table(s: dict) -> list[dict]:
    """
    Build Energy Part A metrics table (10 rows, max 20 pts) for email s3.
    Thresholds must match energy_screener.py constants.
    Key differences from growth stock Part A:
      - EBITDA margin replaces ROIC as the primary profitability signal
      - CapEx intensity is a POSITIVE signal (heavy investment = growth)
      - Revenue scale is an absolute size check
      - No hard gate rows — energy gates are applied before scoring
    """
    rows = []

    def row(label, value_fmt, score, resolved, threshold_note=""):
        ass = assessment_text(score, resolved)
        sig = score_signal(score) if resolved else "amber"
        return {
            "label":          label,
            "value":          value_fmt if resolved else "Unresolved",
            "assessment":     ass,
            "signal":         sig,
            "score":          score,
            "max_score":      2,
            "threshold_note": threshold_note,
        }

    # 1. Revenue Growth TTM
    rg = s.get("rev_growth_ttm")
    rows.append(row(
        "Revenue Growth TTM", fmt_pct(rg),
        s.get("score_rev_growth_ttm", 0), is_resolved(rg),
        "Strong >=20% | Acceptable >=8% | Weak <8%"
    ))

    # 2. Revenue CAGR 3yr
    rc = s.get("rev_cagr")
    rows.append(row(
        "Revenue CAGR 3yr", fmt_pct(rc),
        s.get("score_rev_cagr", 0), is_resolved(rc),
        "Strong >=12% | Acceptable >=5% | Weak <5%"
    ))

    # 3. EBITDA Margin (replaces gross margin as primary profitability signal)
    em = s.get("ebitda_margin")
    rows.append(row(
        "EBITDA Margin", fmt_pct(em),
        s.get("score_ebitda_margin", 0), is_resolved(em),
        "Strong >=20% | Acceptable >=5% | Weak <5% — no gross margin gate for energy"
    ))

    # 4. Gross Margin (scoring signal only — NOT a gate for energy)
    gm = s.get("gross_margin")
    rows.append(row(
        "Gross Margin (scoring only — not a gate)", fmt_pct(gm),
        s.get("score_gross_margin", 0), is_resolved(gm),
        "Strong >=25% | Acceptable >=12% | Weak <12% — informational; does not exclude"
    ))

    # 5. EBITDA Growth YoY
    eg = s.get("ebitda_growth")
    rows.append(row(
        "EBITDA Growth YoY", fmt_pct(eg),
        s.get("score_ebitda_growth", 0), is_resolved(eg),
        "Strong >=20% | Acceptable >=5% | Weak <5%"
    ))

    # 6. FCF Status (energy-specific: positive/near-breakeven/negative)
    fcf        = s.get("fcf")
    fcf_margin = s.get("fcf_margin")
    if is_resolved(fcf):
        if fcf > 0:
            fcf_fmt = f"Positive ({fmt_pct(fcf_margin)} margin)"
        elif is_resolved(fcf_margin) and fcf_margin > -0.10:
            fcf_fmt = f"Near breakeven ({fmt_pct(fcf_margin)} margin)"
        else:
            fcf_fmt = f"Negative ({fmt_pct(fcf_margin)} margin)"
    else:
        fcf_fmt = "—"
    rows.append(row(
        "FCF Status", fcf_fmt,
        s.get("score_fcf", 0), is_resolved(fcf),
        "Positive = 2pts | Near breakeven (>-10% margin, capex cycle) = 1pt | Deeply negative = 0pts"
    ))

    # 7. Return on Equity (ROE)
    roe = s.get("roe")
    rows.append(row(
        "Return on Equity (ROE)", fmt_pct(roe),
        s.get("score_roe", 0), is_resolved(roe),
        "Strong >=15% | Acceptable >=5% | Weak <5%"
    ))

    # 8. CapEx Intensity — INVERTED vs standard (high capex = growth investment signal)
    capex = s.get("capex_intensity")
    rows.append(row(
        "CapEx Intensity (growth investment signal)", fmt_pct(capex),
        s.get("score_capex", 0), is_resolved(capex),
        "Strong >=8% of revenue | Acceptable >=3% | Weak <3% — HIGH capex signals capacity expansion"
    ))

    # 9. Revenue Scale
    rev_scale = s.get("rev_scale")
    if is_resolved(rev_scale):
        if rev_scale >= 1e9:
            scale_fmt = f"${rev_scale / 1e9:.1f}B"
        elif rev_scale >= 1e6:
            scale_fmt = f"${rev_scale / 1e6:.0f}M"
        else:
            scale_fmt = f"${rev_scale:,.0f}"
    else:
        scale_fmt = "—"
    rows.append(row(
        "Revenue Scale (TTM)", scale_fmt,
        s.get("score_rev_scale", 0), is_resolved(rev_scale),
        "Strong >$1B | Acceptable $200M–$1B | Weak <$200M"
    ))

    # 10. Forward Growth (best of revenue growth / EPS growth)
    fg = s.get("fwd_growth")
    rows.append(row(
        "Forward Growth (rev / EPS — best)", fmt_pct(fg),
        s.get("score_fwd_growth", 0), is_resolved(fg),
        "Strong >=25% | Acceptable >=10% | Weak <10%"
    ))

    return rows


# ---------------------------------------------------------------------------
# ENERGY Part B — 8 metric row builders (max 16 pts)
# ---------------------------------------------------------------------------
def build_energy_part_b_table(s: dict) -> list[dict]:
    """
    Build Energy Part B metrics table (8 rows, max 16 pts) for email s3.
    Key differences from growth stock Part B:
      - 52-week position scoring is INVERTED (high position = momentum positive)
      - Net Debt / EBITDA tolerance is higher (<=5x vs <=3x) — capex-cycle leverage
      - Analyst recommendation uses numeric mean (1-5 scale) not text key
      - No stress test row, no dividend payout row, no FCF yield row
    """
    rows = []

    def row(label, value_fmt, score, resolved, threshold_note=""):
        ass = assessment_text(score, resolved)
        sig = score_signal(score) if resolved else "amber"
        return {
            "label":          label,
            "value":          value_fmt if resolved else "Unresolved",
            "assessment":     ass,
            "signal":         sig,
            "score":          score,
            "max_score":      2,
            "threshold_note": threshold_note,
        }

    # 1. EV / EBITDA
    ev_eb = s.get("ev_ebitda")
    rows.append(row(
        "EV / EBITDA", fmt_x(ev_eb),
        s.get("score_ev_ebitda", 0), is_resolved(ev_eb),
        "Strong <=15x | Acceptable <=35x | Weak >35x"
    ))

    # 2. Net Debt / EBITDA (higher tolerance for energy capex cycle)
    nd = s.get("nd_ebitda")
    nd_fmt = "Net Cash" if (is_resolved(nd) and nd < 0) else fmt_x(nd)
    rows.append(row(
        "Net Debt / EBITDA", nd_fmt,
        s.get("score_nd_ebitda", 0), is_resolved(nd),
        "Strong <=2x | Acceptable <=5x | Weak >5x — higher tolerance vs standard (capex-cycle leverage)"
    ))

    # 3. Analyst Recommendation (mean 1-5 scale: 1=Strong Buy, 5=Strong Sell)
    rec_mean = s.get("recommendation_mean")
    rec_fmt = f"{rec_mean:.2f} / 5.0 (lower = stronger buy)" if is_resolved(rec_mean) else "—"
    rows.append(row(
        "Analyst Recommendation (mean)", rec_fmt,
        s.get("score_analyst_rec", 0), is_resolved(rec_mean),
        "Strong <=2.5 (Buy/Strong Buy) | Acceptable <=3.2 (Hold/Mild Buy) | Weak >3.2"
    ))

    # 4. Price vs Consensus Target (upside)
    upside = s.get("upside_pct") or s.get("target_upside")
    rows.append(row(
        "Price vs Consensus Target", fmt_pct(upside),
        s.get("score_upside", 0), is_resolved(upside),
        "Strong >=20% upside | Acceptable >=5% | Weak <5%"
    ))

    # 5. 52-Week Position — MOMENTUM signal (INVERTED vs standard growth stock)
    pos52 = s.get("pos_52wk")
    pos52_fmt = f"{pos52 * 100:.0f}% of 52wk range" if is_resolved(pos52) else "—"
    rows.append(row(
        "52-Week Position (momentum — INVERTED)", pos52_fmt,
        s.get("score_52wk", 0), is_resolved(pos52),
        "Strong >=80% of range | Acceptable >=50% | Weak <50% — HIGH position = momentum POSITIVE (inverse of standard)"
    ))

    # 6. Forward P/E
    fwd_pe = s.get("fwd_pe")
    rows.append(row(
        "Forward P/E", fmt_x(fwd_pe),
        s.get("score_fwd_pe", 0), is_resolved(fwd_pe),
        "Strong <=20x | Acceptable <=40x | Weak >40x"
    ))

    # 7. Forward EPS Growth
    eps_g = s.get("eps_growth")
    rows.append(row(
        "Forward EPS Growth", fmt_pct(eps_g),
        s.get("score_eps_growth", 0), is_resolved(eps_g),
        "Strong >=20% | Acceptable >=8% | Weak <8%"
    ))

    # 8. Analyst Coverage Count (depth of coverage = quality of price discovery)
    ac = s.get("analyst_count")
    ac_fmt = str(int(ac)) + " analysts" if is_resolved(ac) else "—"
    rows.append(row(
        "Analyst Coverage (count)", ac_fmt,
        s.get("score_analyst_count", 0), is_resolved(ac),
        "Strong >=10 analysts | Acceptable >=5 | Weak <5"
    ))

    return rows


# ---------------------------------------------------------------------------
# VCI summary section (no Part A/B tables — ACS scorecard format)
# ---------------------------------------------------------------------------
def build_vci_summary_section(s: dict) -> dict:
    """
    Build VCI ACS summary for email s3 investment case.
    No Part A/B tables — the full VCI scorecard lives in the VCI output file.
    Returns a structured dict for Claude to render and expand at Step 9/10.
    """
    acs  = s.get("acs_score") or s.get("_acs_score")
    conv = prelim_conviction_bracket_vci(acs)

    return {
        "pipeline":            "vci",
        "acs_score":           acs,
        "acs_max":             100,
        "acs_display":         f"ACS {acs}/100" if acs is not None else "ACS —/100",
        "classification":      s.get("classification") or s.get("_classification", ""),
        "nvidia_signals":      s.get("nvidia_signals") or s.get("_nvidia_signals", ""),
        "vci_run_date":        s.get("vci_run_date") or s.get("_vci_run_date", ""),
        "conviction_bracket":  conv,
        "deployment_ready":    (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD,
        "deployment_note": (
            f"Deployment threshold met (ACS {acs} >=75). Proceed to Step 9 conviction score."
            if (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD
            else f"Below deployment threshold (ACS {acs or 0} <75). Monitor for catalyst."
        ),
        "part_a_table":        None,   # not applicable for VCI
        "part_b_table":        None,   # not applicable for VCI
        "scorecard_location":  "Full VCI scorecard (B1-B12 dimensions) is in project_vci_output_mmm_yyyy.md",
        "pipeline_note": (
            "VCI pipeline: scored using ACS (Asymmetric Conviction Score) /100 via 9-dimension composite. "
            "Standard growth stock or energy Part A/B tables do not apply. "
            "Thesis-break logic uses T1-T7 narrative rules, not metric-threshold triggers."
        ),
    }


# ---------------------------------------------------------------------------
# Analyst metadata row
# ---------------------------------------------------------------------------
def build_analyst_summary(s: dict) -> dict:
    rating   = (s.get("analyst_rating") or "").lower().strip()
    n_anal   = s.get("num_analysts")
    t_price  = s.get("target_price_mean")
    c_price  = s.get("current_price")
    currency = s.get("currency", "USD")

    implied_upside = None
    if t_price and c_price and c_price > 0:
        implied_upside = (t_price - c_price) / c_price

    disparity_flag = (
        rating not in STRONG_RATINGS
        and (s.get("total_score") or 0) >= HIGH_SCORE_THRESHOLD
    )

    return {
        "analyst_rating":       s.get("analyst_rating", "—"),
        "analyst_rating_clean": rating.replace("strongbuy", "Strong Buy").replace("buy", "Buy").title() if rating else "—",
        "num_analysts":         int(n_anal) if is_resolved(n_anal) else None,
        "consensus_target":     fmt_price(t_price, currency),
        "current_price":        fmt_price(c_price, currency),
        "implied_upside":       fmt_pct(implied_upside),
        "disparity_flag":       disparity_flag,
        "disparity_note": (
            f"Consensus rating is '{rating}' — not strong buy. "
            f"Investment case MUST explicitly address why conviction remains high despite the disparity."
            if disparity_flag else ""
        ),
    }


# ---------------------------------------------------------------------------
# Overlay summary
# ---------------------------------------------------------------------------
def build_overlay_summary(s: dict) -> dict:
    ovl = {}
    overlay_status = s.get("overlay_status", "not_run")
    ovl["overlay_status"] = overlay_status
    ovl["overlays_unresolved"] = s.get("overlays_unresolved", "")

    # ROIC vs WACC
    roic = s.get("roic")
    wacc = s.get("wacc_pct")
    spread = s.get("roic_vs_wacc_spread")
    if is_resolved(roic) and is_resolved(wacc):
        ovl["roic_vs_wacc"] = {
            "roic_pct": fmt_pct(roic),
            "wacc_pct": f"{wacc:.1f}%",
            "spread": f"{spread:+.1f}pp" if is_resolved(spread) else "—",
            "assessment": "Positive — value creation confirmed" if (spread or 0) > 0 else "Negative — destroys value",
            "signal": "green" if (spread or 0) > 5 else ("amber" if (spread or 0) > 0 else "red"),
        }
    else:
        ovl["roic_vs_wacc"] = {"status": s.get("wacc_status", "not_computed")}

    # Valuation vs Own History
    pe_prem = s.get("val_hist_pe_premium_disc")
    pfcf_prem = s.get("val_hist_pfcf_premium_disc")
    val_status = s.get("val_hist_status", "unresolved")
    if val_status == "computed":
        ovl["valuation_vs_history"] = {
            "pe_premium_disc": f"{pe_prem:+.1f}% vs 3yr avg" if is_resolved(pe_prem) else "—",
            "pfcf_premium_disc": f"{pfcf_prem:+.1f}% vs 3yr avg" if is_resolved(pfcf_prem) else "—",
            "pe_status": s.get("val_hist_pe_status", ""),
            "3yr_avg_pe": s.get("val_hist_pe_3yr_avg"),
            "current_pe": s.get("val_hist_current_pe"),
            "assessment": (
                "Historically cheap — potential valuation catalyst" if (pe_prem or 0) < -15
                else "Historically expensive — momentum or limited upside" if (pe_prem or 0) > 15
                else "Trading near historical average"
            ),
            "signal": "green" if (pe_prem or 0) < -15 else ("red" if (pe_prem or 0) > 15 else "amber"),
        }
    else:
        ovl["valuation_vs_history"] = {"status": val_status}

    # Estimate revisions
    est_dir = s.get("est_rev_direction", "")
    if est_dir:
        ovl["estimate_revisions"] = {
            "direction": est_dir,
            "eps_up_30d": s.get("est_rev_eps_up_30d"),
            "eps_down_30d": s.get("est_rev_eps_down_30d"),
            "consensus_trend": s.get("est_rev_consensus_trend", ""),
            "signal": "green" if est_dir == "improving" else ("red" if est_dir == "deteriorating" else "amber"),
        }

    # PEG 3yr
    peg = s.get("peg_3yr_cagr")
    if is_resolved(peg) and s.get("peg_3yr_status") == "calculated":
        ovl["peg_3yr"] = {
            "peg_cagr": f"{peg:.3f}",
            "analyst_count": s.get("peg_3yr_analyst_count"),
            "assessment": "Attractive (<1.0)" if peg < 1.0 else "Expensive (>2.0)" if peg > 2.0 else "Fair (1–2)",
            "signal": "green" if peg < 1.0 else ("red" if peg > 2.0 else "amber"),
        }

    # Trailing P/E
    tpe = s.get("trailing_pe")
    if is_resolved(tpe) and s.get("trailing_pe_status") not in ("unresolved", "negative_earnings"):
        ovl["trailing_pe"] = {
            "value": fmt_x(tpe),
            "status": s.get("trailing_pe_status", ""),
        }

    return ovl


# ---------------------------------------------------------------------------
# Conviction bracket functions — one per pipeline
# ---------------------------------------------------------------------------
def prelim_conviction_bracket(total_score, total_max=None) -> dict:
    """Growth preliminary conviction bracket; max-aware (/50 base, /54 semi-hardware)."""
    ts = total_score or 0
    tmax = total_max or _cfg.GROWTH_TOTAL_MAX
    for threshold, label, level, note in _cfg.conviction_brackets(tmax):
        if ts >= threshold:
            return {
                "total_score": ts, "total_max": tmax,
                "bracket":          label,
                "level":            level,
                "note":             note,
                "conviction_score": "[Claude fills /100 at Step 9]",
            }
    return {"total_score": ts, "total_max": tmax, "bracket": "No Action", "level": "low",
            "conviction_score": "[Claude fills /100 at Step 9]"}


def prelim_conviction_bracket_energy(total_score: int | None) -> dict:
    """Energy preliminary conviction bracket (/36)."""
    ts = total_score or 0
    for threshold, label, level, note in SCORE_TO_PRELIM_CONVICTION_ENERGY:
        if ts >= threshold:
            return {
                "total_score_36":   ts,
                "bracket":          label,
                "level":            level,
                "note":             note,
                "conviction_score": "[Claude fills /100 at Step 9]",
            }
    return {"total_score_36": ts, "bracket": "No Action", "level": "low",
            "conviction_score": "[Claude fills /100 at Step 9]"}


def prelim_conviction_bracket_vci(acs_score: int | None) -> dict:
    """VCI conviction bracket (ACS /100). Returns deployment status and tier."""
    acs = acs_score or 0
    for threshold, label, level, note in SCORE_TO_PRELIM_CONVICTION_VCI:
        if acs >= threshold:
            return {
                "acs_score":        acs,
                "bracket":          label,
                "level":            level,
                "note":             note,
                "conviction_score": f"ACS {acs}/100",
                "deployment_ready": acs >= VCI_DEPLOYMENT_THRESHOLD,
                "nvidia_class":     acs >= VCI_NVIDIA_CLASS,
            }
    return {
        "acs_score":        acs,
        "bracket":          "Below Threshold",
        "level":            "low",
        "note":             "ACS <60 — insufficient asymmetric conviction; observe only",
        "conviction_score": f"ACS {acs}/100",
        "deployment_ready": False,
        "nvidia_class":     False,
    }


def get_conviction_bracket(s: dict) -> dict:
    """Dispatcher: return the correct conviction bracket dict for any pipeline."""
    pipeline = s.get("_source_pipeline", "growth_stock")
    if pipeline == "energy":
        return prelim_conviction_bracket_energy(s.get("total_score"))
    elif pipeline == "vci":
        acs = s.get("acs_score") or s.get("_acs_score")
        return prelim_conviction_bracket_vci(acs)
    else:
        return prelim_conviction_bracket(s.get("total_score"), s.get("total_max"))


# ---------------------------------------------------------------------------
# Email s5 watchlist table row — pipeline-aware
# ---------------------------------------------------------------------------
def build_s5_row(ticker: str, s: dict, wl_entry: dict | None) -> dict:
    """
    Build an email s5 watchlist row. Score display and conviction label adapt
    to the source_pipeline: growth_stock (/50), energy (/36), or vci (ACS/100).
    """
    rank     = s.get("_rank") or (wl_entry.get("rank") if wl_entry else None)
    entry    = s.get("_entry_level")
    ecur     = s.get("_entry_currency", "USD")
    c_price  = s.get("current_price")
    currency = s.get("currency", ecur)
    pipeline = s.get("_source_pipeline", "growth_stock")

    conv = get_conviction_bracket(s)

    if pipeline == "energy":
        score_display = (
            f"{s.get('part_a_score', '?')}/20 + {s.get('part_b_score', '?')}/16 "
            f"({s.get('total_score', '?')}/36) | {conv['bracket']}"
        )
    elif pipeline == "vci":
        acs = s.get("acs_score") or s.get("_acs_score")
        score_display = (
            f"ACS {acs}/100 | {conv['bracket']}"
            + (" ✓ Deploy" if conv.get("deployment_ready") else "")
        )
    else:
        # growth_stock
        score_display = (
            f"{s.get('part_a_score', '?')}/{s.get('part_b_score', '?')} "
            f"({s.get('total_score', '?')}/{s.get('total_max',50)}) | Conv: {conv['conviction_score']}"
        )

    return {
        "rank":              rank or "—",
        "ticker":            ticker,
        "name":              s.get("company") or s.get("_name", ticker),
        "pipeline":          pipeline,
        "score":             score_display,
        "score_level":       conv["level"],
        "sector":            s.get("sector") or s.get("_sector_hint", "—"),
        "entry_level":       fmt_price(entry, ecur) if entry else "—",
        "in_window":         s.get("_in_window", False),
        "gap_pct":           (f"{s.get('_pct_above_entry', 0):+.1f}% vs entry"
                              if is_resolved(s.get("_pct_above_entry")) else "—"),
        "current_price":     fmt_price(c_price, currency),
        "status":            s.get("_status", "Watchlist"),
        "status_type":       "watchlist",
        "next_earnings":     s.get("next_earnings", "—"),
        "analyst_disparity": s.get("_analyst_disparity_flag", False),
    }


# ---------------------------------------------------------------------------
# Email s7 stock sleeve row
# ---------------------------------------------------------------------------
def build_s7_row(ticker: str, s: dict) -> dict:
    c_price  = s.get("current_price")
    currency = s.get("currency", "USD")
    cost     = s.get("_cost_per_share")
    shares   = s.get("_shares")
    gain_pct_raw = None
    if is_resolved(c_price) and is_resolved(cost) and cost > 0:
        gain_pct_raw = (c_price - cost) / cost
    gain_sign = "positive" if (gain_pct_raw or 0) >= 0 else "negative"
    est_value = round(c_price * shares, 2) if (is_resolved(c_price) and is_resolved(shares)) else None
    est_cost  = round(cost * shares, 2)    if (is_resolved(cost) and is_resolved(shares)) else None

    pipeline = s.get("_source_pipeline", "growth_stock")
    conv     = get_conviction_bracket(s)

    # Score display adapts to pipeline
    if pipeline == "energy":
        score_summary = (f"{s.get('part_a_score','?')}/20 + {s.get('part_b_score','?')}/16 "
                         f"= {s.get('total_score','?')}/36")
    elif pipeline == "vci":
        acs = s.get("acs_score") or s.get("_acs_score")
        score_summary = f"ACS {acs}/100"
    else:
        score_summary = (f"{s.get('part_a_score','?')}/28 + {s.get('part_b_score','?')}/{s.get('part_b_max',22)} "
                         f"= {s.get('total_score','?')}/{s.get('total_max',50)}")

    return {
        "ticker":        ticker,
        "name":          s.get("company", ticker),
        "pipeline":      pipeline,
        "shares":        str(shares) if is_resolved(shares) else "—",
        "value":         fmt_price(est_value, "GBP") if est_value else "[Claude: from AJ Bell file]",
        "cost":          fmt_price(est_cost, "GBP") if est_cost else "[Claude: from AJ Bell file]",
        "gain_pct":      fmt_pct(gain_pct_raw) if is_resolved(gain_pct_raw) else "[Claude: from AJ Bell file]",
        "gain_sign":     gain_sign,
        "weight_pct":    "[Claude: from portfolio_data JSON]",
        "status":        "Hold",
        "status_type":   "hold",
        "status_note":   "[Claude: update thesis status at Step 8]",
        "next_earnings": s.get("next_earnings", "—"),
        "analyst_rating": s.get("analyst_rating", "—"),
        "target_upside": fmt_pct(s.get("target_upside")),
        "score_summary": score_summary,
        "total_score":   s.get("total_score"),
        "total_max":     s.get("total_max"),
        "part_a_score":  s.get("part_a_score"),
        "part_b_score":  s.get("part_b_score"),
    }


# ---------------------------------------------------------------------------
# Email s3 investment case skeleton — dispatcher + per-pipeline builders
# ---------------------------------------------------------------------------
def build_s3_skeleton(ticker: str, s: dict) -> dict:
    """Route to the appropriate s3 skeleton builder based on source_pipeline."""
    pipeline = s.get("_source_pipeline", "growth_stock")
    if pipeline == "energy":
        return _build_s3_skeleton_energy(ticker, s)
    elif pipeline == "vci":
        return _build_s3_skeleton_vci(ticker, s)
    else:
        return _build_s3_skeleton_growth(ticker, s)


def _build_s3_skeleton_growth(ticker: str, s: dict) -> dict:
    """Growth stock s3 investment case skeleton (/50 scorecard)."""
    part_a_table = build_part_a_table(s)
    part_b_table = build_part_b_table(s)
    analyst      = build_analyst_summary(s)
    overlays     = build_overlay_summary(s)
    conv         = prelim_conviction_bracket(s.get("total_score"))

    # Build metrics_table for email s3 (combined A+B summary for the case header)
    header_metrics = [
        {"label": "Part A Score",       "value": f"{s.get('part_a_score','?')}/28",  "assessment": s.get("part_a_status",""),  "signal": "green" if (s.get("part_a_score") or 0) >= PART_A_STRONG_THRESHOLD else "amber"},
        {"label": "Part B Score",       "value": f"{s.get('part_b_score','?')}/{s.get('part_b_max',22)}",  "assessment": s.get("part_b_status",""),  "signal": "green" if (s.get("part_b_score") or 0) >= PART_B_STRONG_THRESHOLD else "amber"},
        {"label": "Combined Score",     "value": f"{s.get('total_score','?')}/{s.get('total_max',50)}",   "assessment": conv["bracket"],             "signal": conv["level"] if conv["level"] != "low" else "amber"},
        {"label": "Conviction Score",   "value": conv["conviction_score"],            "assessment": conv["note"],                "signal": "amber"},
        {"label": "Current Price",      "value": fmt_price(s.get("current_price"), s.get("currency","USD")), "assessment": "", "signal": ""},
        {"label": "Entry Level",        "value": fmt_price(s.get("_entry_level"), s.get("_entry_currency","USD")), "assessment": "In range ✓" if s.get("_in_window") else "Above entry", "signal": "green" if s.get("_in_window") else "amber"},
        {"label": "Target Upside",      "value": fmt_pct(s.get("target_upside")),    "assessment": analyst["analyst_rating_clean"], "signal": "green" if (s.get("target_upside") or 0) > 0.20 else "amber"},
        {"label": "Analyst Count",      "value": str(analyst.get("num_analysts","—")), "assessment": "", "signal": ""},
        {"label": "Next Earnings",      "value": s.get("next_earnings","—"),          "assessment": "", "signal": ""},
        {"label": "ROIC vs WACC",       "value": overlays.get("roic_vs_wacc",{}).get("spread","—"),
                                         "assessment": overlays.get("roic_vs_wacc",{}).get("assessment",""),
                                         "signal": overlays.get("roic_vs_wacc",{}).get("signal","amber")},
        {"label": "Val vs History (P/E)", "value": overlays.get("valuation_vs_history",{}).get("pe_premium_disc","—"),
                                          "assessment": overlays.get("valuation_vs_history",{}).get("assessment",""),
                                          "signal": overlays.get("valuation_vs_history",{}).get("signal","amber")},
    ]

    return {
        "action":          "BUY",
        "ticker":          ticker,
        "name":            s.get("company", ticker),
        "conviction":      conv["conviction_score"],
        "part_a_score":    s.get("part_a_score"),
        "part_b_score":    s.get("part_b_score"),
        "total_score":     s.get("total_score"),
        "part_a_status":   s.get("part_a_status",""),
        "part_b_status":   s.get("part_b_status",""),
        "metrics_table":   header_metrics,
        "part_a_table":    part_a_table,
        "part_b_table":    part_b_table,
        "analyst":         analyst,
        "overlays":        overlays,
        "paragraphs": [
            f"<strong>Investment thesis:</strong> [Claude fills at Step 10 — why this, why now, structural growth driver, moat, management quality, industry context]",
            f"<strong>Valuation framing:</strong> [Claude fills — base/bull/bear cases, key assumptions, expected return vs downside, why market may be mispricing]",
            f"<strong>Portfolio fit:</strong> [Claude fills — before-and-after on sector/geo/currency/concentration, fund overlap, Citigroup overlap]",
            f"<strong>Execution:</strong> [Claude fills — trade timing, limit price, shares to buy, dealing cost, FX cost, cash remaining, preclearance reminder, 30-day hold]",
        ] + ([
            f"<strong>Analyst disparity:</strong> {analyst['disparity_note']}"
        ] if analyst["disparity_flag"] else []),
        "separator_after": False,
    }


def _build_s3_skeleton_energy(ticker: str, s: dict) -> dict:
    """Energy stock s3 investment case skeleton (/36 scorecard)."""
    part_a_table = build_energy_part_a_table(s)
    part_b_table = build_energy_part_b_table(s)
    analyst      = build_analyst_summary(s)
    conv         = prelim_conviction_bracket_energy(s.get("total_score"))

    # Gate status for header
    gate_pass   = s.get("gate_pass")
    gate_code   = s.get("gate_code", "")
    gate_reason = s.get("gate_reason", "")
    gate_signal = "green" if gate_pass else ("amber" if gate_pass is None else "red")

    header_metrics = [
        {"label": "Energy Gate",       "value": gate_code or ("PASS" if gate_pass else "FAIL"),
         "assessment": gate_reason or ("All gates passed" if gate_pass else "Gate failed"),
         "signal": gate_signal},
        {"label": "Part A Score",      "value": f"{s.get('part_a_score', '?')}/20",
         "assessment": s.get("part_a_status", ""),
         "signal": "green" if (s.get("part_a_score") or 0) >= ENERGY_PART_A_STRONG else "amber"},
        {"label": "Part B Score",      "value": f"{s.get('part_b_score', '?')}/16",
         "assessment": s.get("part_b_status", ""),
         "signal": "green" if (s.get("part_b_score") or 0) >= ENERGY_PART_B_STRONG else "amber"},
        {"label": "Combined Score",    "value": f"{s.get('total_score', '?')}/36",
         "assessment": conv["bracket"],
         "signal": conv["level"] if conv["level"] != "low" else "amber"},
        {"label": "Conviction Score",  "value": conv["conviction_score"],
         "assessment": conv["note"], "signal": "amber"},
        {"label": "Current Price",     "value": fmt_price(s.get("current_price"), s.get("currency", "USD")),
         "assessment": "", "signal": ""},
        {"label": "Entry Level",       "value": fmt_price(s.get("_entry_level"), s.get("_entry_currency", "USD")),
         "assessment": "In range ✓" if s.get("_in_window") else "Above entry",
         "signal": "green" if s.get("_in_window") else "amber"},
        {"label": "Consensus Upside",  "value": fmt_pct(s.get("target_upside")),
         "assessment": analyst.get("analyst_rating_clean", "—"),
         "signal": "green" if (s.get("target_upside") or 0) > 0.20 else "amber"},
        {"label": "Next Earnings",     "value": s.get("next_earnings", "—"),
         "assessment": "", "signal": ""},
        {"label": "EBITDA Margin",     "value": fmt_pct(s.get("ebitda_margin")),
         "assessment": "", "signal": "green" if (s.get("ebitda_margin") or 0) >= 0.20 else "amber"},
        {"label": "EV/EBITDA",         "value": fmt_x(s.get("ev_ebitda")),
         "assessment": "", "signal": "green" if is_resolved(s.get("ev_ebitda")) and s.get("ev_ebitda") <= 15 else "amber"},
    ]

    return {
        "action":           "BUY",
        "ticker":           ticker,
        "name":             s.get("company", ticker),
        "pipeline":         "energy",
        "conviction":       conv["conviction_score"],
        "part_a_score":     s.get("part_a_score"),
        "part_b_score":     s.get("part_b_score"),
        "total_score":      s.get("total_score"),
        "score_max":        36,
        "part_a_status":    s.get("part_a_status", ""),
        "part_b_status":    s.get("part_b_status", ""),
        "gate_pass":        gate_pass,
        "gate_code":        gate_code,
        "gate_reason":      gate_reason,
        "metrics_table":    header_metrics,
        "part_a_table":     part_a_table,
        "part_b_table":     part_b_table,
        "analyst":          analyst,
        "overlays":         {"note": "Overlays (ROIC vs WACC, valuation history) not applicable for energy pipeline"},
        "paragraphs": [
            "<strong>Investment thesis:</strong> [Claude fills — energy transition angle, contracted revenue vs merchant exposure, data centre / AI demand catalyst, capacity growth plan]",
            "<strong>Valuation framing:</strong> [Claude fills — EV/EBITDA vs sector peers, regulated vs merchant premium/discount, capex cycle stage, base/bull/bear cases]",
            "<strong>Portfolio fit:</strong> [Claude fills — energy sleeve allocation, currency, sector diversification vs existing holdings, Citigroup overlap check]",
            "<strong>Execution:</strong> [Claude fills — trade timing, limit price, shares to buy, dealing cost, FX cost, cash remaining, preclearance reminder, 30-day hold]",
        ] + ([
            f"<strong>Analyst disparity:</strong> {analyst['disparity_note']}"
        ] if analyst.get("disparity_flag") else []),
        "separator_after":  False,
    }


def _build_s3_skeleton_vci(ticker: str, s: dict) -> dict:
    """VCI asymmetric candidate s3 investment case skeleton (ACS /100)."""
    acs      = s.get("acs_score") or s.get("_acs_score")
    conv     = prelim_conviction_bracket_vci(acs)
    analyst  = build_analyst_summary(s)
    vci_sect = build_vci_summary_section(s)

    header_metrics = [
        {"label": "ACS Score",          "value": f"ACS {acs}/100",
         "assessment": conv["bracket"],
         "signal": "green" if (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD else "amber"},
        {"label": "Classification",     "value": s.get("classification") or s.get("_classification", "—"),
         "assessment": "", "signal": ""},
        {"label": "NVIDIA Signals",     "value": s.get("nvidia_signals") or s.get("_nvidia_signals", "—"),
         "assessment": "NVIDIA-class" if (acs or 0) >= VCI_NVIDIA_CLASS else "",
         "signal": "green" if (acs or 0) >= VCI_NVIDIA_CLASS else "amber"},
        {"label": "Deployment Ready",   "value": "Yes ✓" if (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD else "Not yet",
         "assessment": vci_sect["deployment_note"],
         "signal": "green" if (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD else "amber"},
        {"label": "VCI Run Date",       "value": s.get("vci_run_date") or s.get("_vci_run_date", "—"),
         "assessment": "", "signal": ""},
        {"label": "Current Price",      "value": fmt_price(s.get("current_price"), s.get("currency", "USD")),
         "assessment": "", "signal": ""},
        {"label": "Entry Level",        "value": fmt_price(s.get("_entry_level"), s.get("_entry_currency", "USD")),
         "assessment": "In range ✓" if s.get("_in_window") else "Above entry",
         "signal": "green" if s.get("_in_window") else "amber"},
        {"label": "Next Earnings",      "value": s.get("next_earnings", "—"),
         "assessment": "", "signal": ""},
    ]

    return {
        "action":           "BUY (asymmetric sub-sleeve)",
        "ticker":           ticker,
        "name":             s.get("company", ticker),
        "pipeline":         "vci",
        "conviction":       conv["conviction_score"],
        "acs_score":        acs,
        "score_max":        100,
        "classification":   s.get("classification") or s.get("_classification", ""),
        "nvidia_signals":   s.get("nvidia_signals") or s.get("_nvidia_signals", ""),
        "deployment_ready": (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD,
        "metrics_table":    header_metrics,
        "part_a_table":     None,   # not applicable — see VCI output file
        "part_b_table":     None,   # not applicable — see VCI output file
        "vci_summary":      vci_sect,
        "analyst":          analyst,
        "paragraphs": [
            "<strong>Asymmetric thesis:</strong> [Claude fills — VCI B1-B12 scorecard narrative, NVIDIA signals, why the risk/reward is asymmetric, platform inflection catalyst]",
            "<strong>Entry and sizing:</strong> [Claude fills — position sizing within 5% asymmetric sub-sleeve cap, limit price, ACS threshold for add/trim, max loss tolerance]",
            "<strong>Thesis monitoring:</strong> [Claude fills — T1-T7 thesis-break conditions from VCI output, re-score trigger, next catalyst date]",
            "<strong>Portfolio fit:</strong> [Claude fills — asymmetric sleeve budget remaining, sector/geo, Citigroup overlap, correlation to fund sleeve]",
            "<strong>Execution:</strong> [Claude fills — trade timing, shares, dealing cost, FX cost, cash remaining, preclearance reminder, 30-day hold]",
        ],
        "separator_after":  False,
    }


# ---------------------------------------------------------------------------
# Main scoring pipeline — pipeline-aware
# ---------------------------------------------------------------------------
# ---- SINGLE SOURCE OF TRUTH: thresholds from scoring_config.py (no private copies) ----
PART_A_STRONG_THRESHOLD   = _cfg.GROWTH_PART_A_STRONG
PART_A_ACCEPTABLE_MIN     = _cfg.GROWTH_PART_A_ACCEPTABLE
PART_B_STRONG_THRESHOLD   = _cfg.GROWTH_PART_B_STRONG
PART_B_ACCEPTABLE_MIN     = _cfg.GROWTH_PART_B_ACCEPTABLE
HIGH_SCORE_THRESHOLD      = _cfg.GROWTH_HIGH_SCORE
SCORE_TO_PRELIM_CONVICTION = _cfg.GROWTH_CONVICTION_BRACKETS
ENERGY_PART_A_STRONG      = _cfg.ENERGY_PART_A_STRONG
ENERGY_PART_A_ACCEPTABLE  = _cfg.ENERGY_PART_A_ACCEPTABLE
ENERGY_PART_B_STRONG      = _cfg.ENERGY_PART_B_STRONG
ENERGY_PART_B_WATCH       = _cfg.ENERGY_PART_B_WATCH
ENERGY_HIGH_SCORE         = _cfg.ENERGY_HIGH_SCORE
SCORE_TO_PRELIM_CONVICTION_ENERGY = _cfg.ENERGY_CONVICTION_BRACKETS
STRONG_RATINGS            = _cfg.STRONG_RATINGS

def run(metrics_path: str, out_path: str) -> dict:
    """
    Format all tickers from watchlist_metrics JSON into email-ready structures.
    Routes each ticker to the correct table builder and conviction bracket
    based on its _source_pipeline field: growth_stock | energy | vci.
    """
    if not os.path.exists(metrics_path):
        raise FileNotFoundError(f"metrics JSON not found: {metrics_path}")
    with open(metrics_path, encoding="utf-8") as f:
        metrics_data = json.load(f)

    tickers_data = metrics_data.get("tickers", {})
    month_label  = metrics_data.get("_meta", {}).get("month_label", "unknown")

    watchlist_rows          = []   # s5 — all watchlist + VCI candidates
    sleeve_rows             = []   # s7 — stock sleeve
    s3_skeletons            = []   # in-window buy cases + sleeve review
    in_window_names         = []
    analyst_disparity_names = []

    for ticker, s in tickers_data.items():
        kind     = s.get("_kind", "unknown")
        pipeline = s.get("_source_pipeline", "growth_stock")

        # ── Analyst disparity check (pipeline-aware thresholds) ──────────────
        analyst = build_analyst_summary(s)
        rating  = (s.get("analyst_rating") or "").lower().strip()

        if pipeline == "energy":
            # Energy: disparity triggers at >=28/36 (~78%, proportional to growth 37/50)
            analyst["disparity_flag"] = (
                rating not in STRONG_RATINGS
                and (s.get("total_score") or 0) >= ENERGY_HIGH_SCORE
            )
        elif pipeline == "vci":
            # VCI: analyst disparity not applicable — VCI uses ACS, not analyst consensus
            analyst["disparity_flag"] = False
        # else: growth_stock — disparity already computed correctly by build_analyst_summary

        s["_analyst_disparity_flag"] = analyst["disparity_flag"]
        if analyst["disparity_flag"]:
            analyst_disparity_names.append(ticker)

        # ── In-window tracking ───────────────────────────────────────────────
        if s.get("_in_window"):
            in_window_names.append(ticker)

        # ── s5 watchlist row (all non-sleeve tickers) ───────────────────────
        if kind in ("watchlist", "vci_watchlist", "unknown"):
            wl_row = build_s5_row(ticker, s, None)
            watchlist_rows.append(wl_row)

        # ── s7 stock sleeve row ──────────────────────────────────────────────
        if kind == "stock_sleeve":
            sleeve_rows.append(build_s7_row(ticker, s))

        # ── s3 investment case skeleton ──────────────────────────────────────
        # Build for: (a) any in-window watchlist/VCI candidate; (b) all sleeve members
        if s.get("_in_window") or kind == "stock_sleeve":
            skel = build_s3_skeleton(ticker, s)
            if kind == "stock_sleeve":
                skel["_for_step"] = "Step 8 — existing position review"
            elif kind == "vci_watchlist":
                skel["_for_step"] = "Step 9/10 — VCI asymmetric in-window candidate"
            else:
                skel["_for_step"] = "Step 9/10 — in-window buy candidate"
            s3_skeletons.append(skel)

    # Sort watchlist rows by rank (None ranks go last)
    watchlist_rows.sort(key=lambda r: (0, int(r["rank"])) if str(r.get("rank", "")).lstrip("-").isdigit() else (1, 10**6))

    # ── Conviction ranking table — pipeline-aware ────────────────────────────
    # Includes all watchlist + vci_watchlist entries
    conviction_ranking = []
    for ticker, s in tickers_data.items():
        if s.get("_kind") not in ("watchlist", "vci_watchlist"):
            continue

        pipeline     = s.get("_source_pipeline", "growth_stock")
        entry_level  = s.get("_entry_level")
        ecur         = s.get("_entry_currency", "USD")
        c_price      = s.get("current_price")
        currency_s   = s.get("currency", ecur)
        conv         = get_conviction_bracket(s)

        base = {
            "rank":              s.get("_rank", "—"),
            "ticker":            ticker,
            "name":              s.get("company", ticker),
            "pipeline":          pipeline,
            "bracket":           conv["bracket"],
            "in_window":         s.get("_in_window", False),
            "entry_level":       fmt_price(entry_level, ecur),
            "current_price":     fmt_price(c_price, currency_s),
            "gap_pct":           (f"{s.get('_pct_above_entry', 0):+.1f}%"
                                 if is_resolved(s.get("_pct_above_entry")) else "—"),
            "analyst_rating":    s.get("analyst_rating", "—"),
            "analyst_disparity": s.get("_analyst_disparity_flag", False),
        }

        if pipeline == "energy":
            base.update({
                "total_score_36": s.get("total_score"),
                "part_a_score":   s.get("part_a_score"),
                "part_b_score":   s.get("part_b_score"),
                "score_display":  f"{s.get('total_score', '?')}/36",
                "conviction_score": "[Claude fills /100 at Step 9]",
                "target_upside":  fmt_pct(s.get("target_upside")),
                "action": (
                    "In range — score at Step 9" if s.get("_in_window")
                    else "Monitor entry level"
                ),
            })
        elif pipeline == "vci":
            acs = s.get("acs_score") or s.get("_acs_score")
            base.update({
                "acs_score":        acs,
                "score_display":    f"ACS {acs}/100",
                "classification":   s.get("classification") or s.get("_classification", ""),
                "nvidia_signals":   s.get("nvidia_signals") or s.get("_nvidia_signals", ""),
                "conviction_score": f"ACS {acs}/100",
                "deployment_ready": (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD,
                "action": (
                    "Deployment threshold met — Step 9" if (acs or 0) >= VCI_DEPLOYMENT_THRESHOLD
                    else "Below VCI deployment threshold — monitor catalysts"
                ),
            })
        else:
            # growth_stock
            base.update({
                "total_score_50": s.get("total_score"),
                "part_a_score":   s.get("part_a_score"),
                "part_b_score":   s.get("part_b_score"),
                "score_display":  f"{s.get('total_score', '?')}/{s.get('total_max',50)}",
                "conviction_score": "[Claude fills /100 at Step 9]",
                "target_upside":  fmt_pct(s.get("target_upside")),
                "action": (
                    "In range — score at Step 9" if s.get("_in_window")
                    else "Monitor entry level"
                ),
            })

        conviction_ranking.append(base)

    conviction_ranking.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 999))

    # ── Output assembly ──────────────────────────────────────────────────────
    # Segment in-window names by pipeline for the meta summary
    in_window_by_pipeline = {
        "growth_stock": [t for t in in_window_names
                         if tickers_data.get(t, {}).get("_source_pipeline", "growth_stock") == "growth_stock"],
        "energy":       [t for t in in_window_names
                         if tickers_data.get(t, {}).get("_source_pipeline") == "energy"],
        "vci":          [t for t in in_window_names
                         if tickers_data.get(t, {}).get("_source_pipeline") == "vci"],
    }

    output = {
        "_meta": {
            "source_file":              os.path.basename(metrics_path),
            "month_label":              month_label,
            "produced_at":              datetime.now().strftime("%Y-%m-%d %H:%M"),
            "in_window_tickers":        in_window_names,
            "in_window_by_pipeline":    in_window_by_pipeline,
            "analyst_disparity_tickers": analyst_disparity_names,
            "watchlist_count":          len(watchlist_rows),
            "sleeve_count":             len(sleeve_rows),
            "s3_skeletons_count":       len(s3_skeletons),
            "pipeline_counts": {
                "growth_stock": len([t for t, s in tickers_data.items()
                                     if s.get("_source_pipeline", "growth_stock") == "growth_stock"]),
                "energy":       len([t for t, s in tickers_data.items()
                                     if s.get("_source_pipeline") == "energy"]),
                "vci":          len([t for t, s in tickers_data.items()
                                     if s.get("_source_pipeline") == "vci"]),
            },
        },
        "conviction_ranking":   conviction_ranking,
        "s5_watchlist_rows":    watchlist_rows,
        "s7_sleeve_rows":       sleeve_rows,
        "s3_case_skeletons":    s3_skeletons,
        # Per-ticker enriched metrics passthrough. step9_pre_builder.py reads
        # scored["tickers"] to tier in-window names (T1/T2/T3 + VCI tiers).
        # Without this key its categorisation loop iterates nothing -> empty step9_pre.
        "tickers":              tickers_data,

        "instructions_for_run": {
            "step_9": (
                "Read conviction_ranking. Note pipeline field per entry — "
                "growth_stock entries use 10-dimension conviction score /100; "
                "energy entries use same 10-dimension framework adapted for energy; "
                "vci entries use ACS score already encoded in conviction_score. "
                "For each in-window name: fill conviction_score. Confirm or override Step 10 selection."
            ),
            "step_10": (
                "For the decided action: read s3_case_skeletons entry for that ticker. "
                "Quantitative scorecard (Part A/B or ACS) is pre-populated. "
                "Fill all [Claude fills] paragraphs. "
                "VCI skeletons: reference project_vci_output_mmm_yyyy.md for full B1-B12 dimensions. "
                "If analyst_disparity_flag is True, the disparity note is already embedded."
            ),
            "step_8": (
                "For each stock sleeve member: read s7_sleeve_rows for current price, "
                "target upside, analyst rating, and score_summary (pipeline-labelled). "
                "Cross-reference with trades log thesis-break conditions. "
                "Note pipeline field — energy and growth stock thresholds differ."
            ),
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    print(f"Watchlist scored: {out_path}")
    print(f"  In-window names:         {in_window_names}")
    if in_window_by_pipeline["energy"]:
        print(f"    Energy in-window:      {in_window_by_pipeline['energy']}")
    if in_window_by_pipeline["vci"]:
        print(f"    VCI in-window:         {in_window_by_pipeline['vci']}")
    print(f"  Analyst disparity flags: {analyst_disparity_names}")
    print(f"  Watchlist rows (s5):     {len(watchlist_rows)}")
    print(f"  Stock sleeve rows (s7):  {len(sleeve_rows)}")
    print(f"  Investment case skeletons (s3): {len(s3_skeletons)}")
    if conviction_ranking:
        print(f"\n  Conviction ranking (preliminary):")
        for cr in conviction_ranking:
            iw    = " ← IN WINDOW" if cr.get("in_window") else ""
            pipe  = cr.get("pipeline", "growth_stock")
            score = cr.get("score_display", "?")
            print(f"    #{str(cr.get('rank','?')):>3} {cr['ticker']:8s} [{pipe[:6]:6s}]  {score:12s}  {cr['bracket']}{iw}")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Format Part A/B (growth stock /50), Energy (/36), and VCI (ACS /100) scores "
            "into email-ready conviction ranking and investment case skeletons."
        )
    )
    parser.add_argument("--metrics", required=True,
                        help="Path to watchlist_metrics_mmm_yyyy.json")
    parser.add_argument("--out", default=None,
                        help="Output path. Defaults to watchlist_scored_mmm_yyyy.json.")
    args = parser.parse_args()

    month_label = args.metrics.replace("watchlist_metrics_", "").replace(".json", "")
    out_path    = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.metrics)),
        f"watchlist_scored_{month_label}.json"
    )
    run(args.metrics, out_path)


if __name__ == "__main__":
    main()
