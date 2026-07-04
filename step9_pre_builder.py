#!/usr/bin/env python3
"""
step9_pre_builder.py  --  Step 9 Pre-Scored Conviction Builder
Version: 1.0  |  2026-06-04

Purpose:
    Reads watchlist_scored_[mmm_yyyy].json (produced by normalise_adapter.py) and
    watchlist_tickers.json, assigns every watchlist name to a tier
    (T1/T2/T3 for main watchlist; T1-A/T2-A/T3-A for VCI candidates),
    computes 7 of 10 conviction dimensions for all T1 names from the scored
    metrics, and writes a structured step9_pre_[mmm_yyyy].json for the
    session to read at Step 9.

    Called by monthly_isa_prerun.py as Step 8.

Usage:
    python step9_pre_builder.py \\
      --scored   "watchlist_scored_mmm_yyyy.json" \\
      --watchlist "watchlist_tickers.json" \\
      --month-label jul_2026 \\
      --out      "step9_pre_jul_2026.json"
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

# Shared deployment-gate pre-flags (CONTRACTS #4 gate_flags / forward_axis_flags). Additive.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import deployment_flags as _dflags
except Exception:
    _dflags = None


# ---------------------------------------------------------------------------
# Sector type derivation
# ---------------------------------------------------------------------------

HEALTHCARE_KEYWORDS = (
    "biotech", "pharma", "medical", "health", "genomic", "diagnostic",
    "life science", "therapeutics", "oncology",
)
CYCLICAL_KEYWORDS = (
    "semiconductor", "memory", "cyclical", "auto", "industrial",
    "materials", "chemical", "mining", "commodity",
)


def derive_sector_type(ticker_entry_wt: dict) -> tuple[str, str]:
    """
    Returns (sector_type, sector_type_source).
    sector_type is one of: quality_compounder_saas | cyclical |
                            healthcare_tech | energy_adjacent
    """
    # 1. Explicit field
    macro_sector = ticker_entry_wt.get("macro_sector")
    if macro_sector:
        return macro_sector, "explicit"

    pipeline = ticker_entry_wt.get("source_pipeline", "").lower()
    industry = (ticker_entry_wt.get("industry") or "").lower()

    # 2. Pipeline inference
    if pipeline == "energy":
        return "energy_adjacent", "inferred"

    if pipeline == "vci":
        for kw in HEALTHCARE_KEYWORDS:
            if kw in industry:
                return "healthcare_tech", "inferred"
        return "quality_compounder_saas", "inferred"

    if pipeline == "growth_stock":
        for kw in CYCLICAL_KEYWORDS:
            if kw in industry:
                return "cyclical", "inferred"
        for kw in HEALTHCARE_KEYWORDS:
            if kw in industry:
                return "healthcare_tech", "inferred"
        return "quality_compounder_saas", "inferred"

    # 3. Default
    return "quality_compounder_saas", "default"


# ---------------------------------------------------------------------------
# Conviction dimension scoring — 7 pre-computable dimensions
# ---------------------------------------------------------------------------

def _get(d: dict, *keys, default=0):
    for k in keys:
        if k in d:
            return d[k] if d[k] is not None else default
    return default


def score_dim1_valuation(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 1 — Valuation (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        s = (_get(td, "score_b_fwd_pe") + _get(td, "score_b_ev_ebitda") +
             _get(td, "score_b_price_fcf") + _get(td, "score_b_fcf_yield") +
             _get(td, "score_b_target_upside"))
        basis = (f"fwd_pe={_get(td,'score_b_fwd_pe')}, ev_ebitda={_get(td,'score_b_ev_ebitda')}, "
                 f"price_fcf={_get(td,'score_b_price_fcf')}, fcf_yield={_get(td,'score_b_fcf_yield')}, "
                 f"target_upside={_get(td,'score_b_target_upside')}")
        return min(s, 10), basis
    elif pipeline == "energy":
        raw = _get(td, "score_ev_ebitda") + _get(td, "score_upside") + _get(td, "score_fwd_pe")
        s = min(round(raw * 10 / 6), 10)
        return s, f"ev_ebitda+upside+fwd_pe scaled to 10"
    elif pipeline == "vci":
        acs4 = _parse_acs_dim(acs_breakdown, "ACS4")
        s = min(round(acs4 / 15 * 10), 10)
        return s, f"ACS4={acs4} → scaled /15×10"
    return 0, "unknown pipeline"


def score_dim2_growth(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 2 — Growth Durability (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        raw = (_get(td, "score_rev_cagr") + _get(td, "score_recent_rev") +
               _get(td, "score_eps_cagr") + _get(td, "score_b_fwd_eps"))
        s = min(round(raw * 10 / 8), 10)
        basis = (f"rev_cagr={_get(td,'score_rev_cagr')}, recent_rev={_get(td,'score_recent_rev')}, "
                 f"eps_cagr={_get(td,'score_eps_cagr')}, fwd_eps={_get(td,'score_b_fwd_eps')} → scaled 8")
        return s, basis
    elif pipeline == "energy":
        raw = (_get(td, "score_rev_growth_ttm") + _get(td, "score_rev_cagr") +
               _get(td, "score_ebitda_growth") + _get(td, "score_fwd_growth"))
        s = min(round(raw * 10 / 8), 10)
        return s, "energy growth metrics scaled /8×10"
    elif pipeline == "vci":
        acs5 = _parse_acs_dim(acs_breakdown, "ACS5")
        s = min(round(acs5 / 10 * 10), 10)
        return s, f"ACS5={acs5} → scaled /10×10"
    return 0, "unknown pipeline"


def score_dim3_profitability(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 3 — Profitability/Quality (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        s = (_get(td, "score_roic") + _get(td, "score_fcf_margin") +
             _get(td, "score_gross_margin") + _get(td, "score_op_margin") +
             _get(td, "score_op_margin_trend"))
        basis = (f"roic={_get(td,'score_roic')}, fcf_margin={_get(td,'score_fcf_margin')}, "
                 f"gross_margin={_get(td,'score_gross_margin')}, op_margin={_get(td,'score_op_margin')}, "
                 f"op_trend={_get(td,'score_op_margin_trend')}")
        return min(s, 10), basis
    elif pipeline == "energy":
        raw = (_get(td, "score_ebitda_margin") + _get(td, "score_roe") +
               _get(td, "score_fcf") + _get(td, "score_gross_margin"))
        s = min(round(raw * 10 / 8), 10)
        return s, "energy profitability scaled /8×10"
    elif pipeline == "vci":
        acs4 = _parse_acs_dim(acs_breakdown, "ACS4")
        acs6 = _parse_acs_dim(acs_breakdown, "ACS6")
        s = min(round(acs4 * 0.4 + acs6 * 0.6), 10)
        return s, f"ACS4={acs4}×0.4 + ACS6={acs6}×0.6"
    return 0, "unknown pipeline"


def score_dim4_balance_sheet(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 4 — Balance Sheet (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        raw = _get(td, "score_nd_ebitda") + _get(td, "score_int_cov") + _get(td, "score_b_stress")
        if td.get("_nd_mand_fail"):
            raw = min(raw, 2)
        s = min(round(raw * 10 / 6), 10)
        basis = (f"nd_ebitda={_get(td,'score_nd_ebitda')}, int_cov={_get(td,'score_int_cov')}, "
                 f"stress={_get(td,'score_b_stress')} → scaled /6")
        if td.get("_nd_mand_fail"):
            basis += " [nd_mand_fail cap applied]"
        return s, basis
    elif pipeline == "energy":
        s = min(_get(td, "score_nd_ebitda") * 5, 10)
        return s, f"nd_ebitda={_get(td,'score_nd_ebitda')} × 5"
    elif pipeline == "vci":
        acs8 = _parse_acs_dim(acs_breakdown, "ACS8")
        acs4 = _parse_acs_dim(acs_breakdown, "ACS4")
        s = min(round(acs8 * 0.5 + min(acs4 / 15, 1) * 5), 10)
        return s, f"ACS8={acs8}×0.5 + min(ACS4/15,1)×5"
    return 0, "unknown pipeline"


def score_dim5_management(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 5 — Management Quality (0–10)"""
    td = ticker_data
    if pipeline == "vci":
        acs7 = _parse_acs_dim(acs_breakdown, "ACS7")
        s = min(round(acs7 / 10 * 10), 10)
        return s, f"ACS7={acs7} → scaled /10×10"

    # growth_stock and energy
    rating = (td.get("analyst_rating") or "").lower()
    if "strong" in rating and "buy" in rating:
        rating_score = 2
    elif "buy" in rating:
        rating_score = 2
    elif "hold" in rating:
        rating_score = 1
    else:
        rating_score = 0

    if pipeline == "growth_stock":
        raw = rating_score + _get(td, "score_share_count") + _get(td, "score_b_div_payout")
        s = min(round(raw * 10 / 6), 10)
        basis = (f"analyst={rating}({rating_score}), share_count={_get(td,'score_share_count')}, "
                 f"div_payout={_get(td,'score_b_div_payout')} → scaled /6")
    else:  # energy
        raw = rating_score + _get(td, "score_analyst_count")
        s = min(round(raw * 10 / 4), 10)
        basis = f"analyst={rating}({rating_score}), analyst_count={_get(td,'score_analyst_count')} → scaled /4"
    return s, basis


def score_dim6_moat(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 6 — Competitive Moat (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        pb = _get(td, "part_b_score")
        s = min(round(pb / 26 * 10), 10)
        return s, f"part_b_score={pb}/26 → scaled"
    elif pipeline == "energy":
        pb = _get(td, "part_b_score")
        s = min(round(pb / 16 * 10), 10)
        return s, f"part_b_score={pb}/16 → scaled"
    elif pipeline == "vci":
        acs1 = _parse_acs_dim(acs_breakdown, "ACS1")
        acs2 = _parse_acs_dim(acs_breakdown, "ACS2")
        acs3 = _parse_acs_dim(acs_breakdown, "ACS3")
        s = min(round((acs1 + acs2 + acs3) / 30 * 10), 10)
        return s, f"(ACS1={acs1}+ACS2={acs2}+ACS3={acs3})/30×10"
    return 0, "unknown pipeline"


def score_dim7_risk_reward(ticker_data: dict, pipeline: str, acs_breakdown: str) -> tuple[int, str]:
    """Dimension 7 — Risk/Reward Asymmetry (0–10)"""
    td = ticker_data
    if pipeline == "growth_stock":
        raw = (_get(td, "score_b_target_upside") + _get(td, "score_b_52wk") +
               _get(td, "score_b_stress"))
        s = min(round(raw * 10 / 6), 10)
        basis = (f"target_upside={_get(td,'score_b_target_upside')}, "
                 f"52wk={_get(td,'score_b_52wk')}, stress={_get(td,'score_b_stress')} → scaled /6")
        return s, basis
    elif pipeline == "energy":
        raw = _get(td, "score_upside") + _get(td, "score_52wk")
        s = min(round(raw * 10 / 4), 10)
        return s, f"upside={_get(td,'score_upside')}+52wk={_get(td,'score_52wk')} scaled /4"
    elif pipeline == "vci":
        acs8 = _parse_acs_dim(acs_breakdown, "ACS8")
        acs9 = _parse_acs_dim(acs_breakdown, "ACS9")
        s = min(round((acs8 / 10 * 0.6 + acs9 / 5 * 0.4) * 10), 10)
        return s, f"(ACS8={acs8}/10×0.6 + ACS9={acs9}/5×0.4)×10"
    return 0, "unknown pipeline"


def _parse_acs_dim(breakdown: str, dim: str) -> int:
    """Parse a single ACS dimension value from breakdown string."""
    m = re.search(rf"{dim}:(\d+)", breakdown or "")
    return int(m.group(1)) if m else 0


def compute_entry_window_score(pct_above_entry: float | None) -> int:
    """
    Quantified entry window attractiveness score (0–10).
    Used as a Capital Deployment sub-component pre-computed at pre-run.

      At or below entry level      → 10  (strongest entry)
      0–10% above entry level      → 7   (acceptable entry — T1 territory)
      10–20% above entry level     → 4   (watch — T2 territory)
      >20% above entry level       → 0   (do not chase — T3 territory)
      No entry level / data issue  → 0   (cannot assess)
    """
    if pct_above_entry is None:
        return 0
    if pct_above_entry <= 0:
        return 10
    if pct_above_entry <= 10:
        return 7
    if pct_above_entry <= 20:
        return 4
    return 0


def compute_decision_bucket(
    tier: str,
    pipeline: str,
    normalised_score: float | None,
    stale_score_flag: bool,
    probation_flag: bool,
    thesis_break_triggered: bool,
    acs_score: int | None = None,
    entry_provisional: bool = False,
    entry_missing: bool = False,
) -> str:
    """
    Returns a human-readable deployment decision label.
    Replaces the T1/T2/T3 tier label with an action-oriented bucket description
    while preserving the tier field for downstream compatibility.

    Main watchlist:
      T1 + no flags           → "Buy Now Candidate"
      T1 + probation          → "Buy Now — Probation (score 60–69)"
      T2                      → "Accumulate (Secondary Conviction)"
      T3                      → "Monitor / Watch"
      any + stale_score       → "Re-score Required"
      any + thesis_break      → "Thesis Review Required"
      normalised_score < 60   → "Remove / Reject"

    VCI:
      T1-A                    → "Deploy Now (Asymmetric)"
      T2-A                    → "Monitor Entry (Asymmetric)"
      T3-A, ACS >= 60         → "Watch (Asymmetric)"
      T3-A, ACS < 60          → "Below VCI Threshold"
    """
    # Thesis break overrides everything
    if thesis_break_triggered:
        return "Thesis Review Required"

    # Stale score overrides tier (can't trust the tier without current data)
    if stale_score_flag:
        return "Re-score Required"

    # VCI pipeline
    if pipeline == "vci":
        acs = acs_score or 0
        if tier == "T1-A" or tier == "T1_A":
            return "Deploy Now (Asymmetric)"
        elif tier in ("T2-A", "T2_A"):
            return "Monitor Entry (Asymmetric)"
        else:
            return "Watch (Asymmetric)" if acs >= 60 else "Below VCI Threshold"

    # Main watchlist and candidate_pool
    ns = normalised_score or 0
    if ns < 60 and not probation_flag:
        return "Remove / Reject"

    # Entry level still missing after entry_level_builder — surface as a process failure
    if entry_missing:
        return "Entry Level Required"

    if tier == "T1":
        if probation_flag:
            return "Buy Now — Probation (score 60-69)"
        # Provisional (pre-run auto-generated) entry levels can rank/tier but must
        # be confirmed at Step 9/10 before deployment — never auto "Buy Now".
        if entry_provisional:
            return "Buy Now — Confirm Entry (Provisional)"
        return "Buy Now Candidate"
    elif tier == "T2":
        return "Accumulate (Secondary Conviction)"
    else:
        return "Monitor / Watch"


def compute_portfolio_overlap_flags(ticker: str, ticker_scored: dict,
                                     sleeve_tickers: dict,
                                     sleeve_sectors: dict) -> dict:
    """
    Compute pre-run portfolio overlap flags from stock_sleeve data.

    stock_sleeve_overlap: True if this exact ticker is already held.
    sector_match_in_sleeve: list of sleeve tickers with matching sector.
    sector_concentration_flag: True if 2+ sleeve holdings share this ticker's sector.

    Note: fund_overlap_flag cannot be computed pre-run (requires fund holdings
    data from a web fetch). Claude checks fund overlap at session Step 9B
    (portfolio_fit dimension) using live fund holdings data.
    """
    stock_sleeve_overlap = ticker in sleeve_tickers

    ticker_sector = (ticker_scored.get("sector") or "").lower()
    sector_matches = []
    if ticker_sector:
        sector_matches = [
            t for t, s in sleeve_sectors.items()
            if s and (ticker_sector in s or s in ticker_sector)
            and t != ticker
        ]

    return {
        "stock_sleeve_overlap":      stock_sleeve_overlap,
        "sector_match_in_sleeve":    sector_matches,
        "sector_concentration_flag": len(sector_matches) >= 2,
        "fund_overlap_flag":         None,  # session-time check — Claude fills at Step 9B
    }


def compute_7_dimensions(ticker: str, ticker_scored: dict,
                          wt_entry: dict,
                          pct_vs_entry: float | None = None) -> dict:
    """
    Compute all 7 pre-scorable conviction dimensions.
    Returns dict with dimension breakdown, strategic_conviction_score, risk_flags, and analyst_disparity.
    """
    pipeline = wt_entry.get("source_pipeline", "growth_stock")
    acs_breakdown = wt_entry.get("acs_breakdown", "")

    dim1_s, dim1_b = score_dim1_valuation(ticker_scored, pipeline, acs_breakdown)
    dim2_s, dim2_b = score_dim2_growth(ticker_scored, pipeline, acs_breakdown)
    dim3_s, dim3_b = score_dim3_profitability(ticker_scored, pipeline, acs_breakdown)
    dim4_s, dim4_b = score_dim4_balance_sheet(ticker_scored, pipeline, acs_breakdown)
    dim5_s, dim5_b = score_dim5_management(ticker_scored, pipeline, acs_breakdown)
    dim6_s, dim6_b = score_dim6_moat(ticker_scored, pipeline, acs_breakdown)
    dim7_s, dim7_b = score_dim7_risk_reward(ticker_scored, pipeline, acs_breakdown)

    subtotal = dim1_s + dim2_s + dim3_s + dim4_s + dim5_s + dim6_s + dim7_s

    # analyst_disparity: True if consensus != strong buy
    rating = (ticker_scored.get("analyst_rating") or "").lower()
    analyst_disparity = not ("strong" in rating and "buy" in rating)

    # Build risk_flags dict — consolidates all pre-computable risk signals
    stale_flag = bool(wt_entry.get("stale_score_flag"))
    probation  = bool(wt_entry.get("probation_flag"))

    # Classify entry window status
    if pct_vs_entry is None:
        ew_status = "unknown"
    elif pct_vs_entry <= 0:
        ew_status = "at_or_below_entry"
    elif pct_vs_entry <= 10:
        ew_status = "acceptable"
    elif pct_vs_entry <= 20:
        ew_status = "watch"
    else:
        ew_status = "above_entry"

    # Delta score direction
    delta = ticker_scored.get("delta_score") or wt_entry.get("delta_score")
    if delta is None:
        delta_direction = "unknown"
    elif delta > 2:
        delta_direction = "improving"
    elif delta < -2:
        delta_direction = "deteriorating"
    else:
        delta_direction = "stable"

    # Next earnings proximity
    next_e = ticker_scored.get("next_earnings", "Unknown")
    binary_event_within_90d = None
    if next_e and next_e != "Unknown":
        try:
            from datetime import datetime as _dt
            earnings_dt = _dt.strptime(next_e[:10], "%Y-%m-%d")
            days_to = (earnings_dt - _dt.now()).days
            binary_event_within_90d = next_e if 0 <= days_to <= 90 else None
        except Exception:
            binary_event_within_90d = None

    risk_flags = {
        "stale_score":             stale_flag,
        "probation":               probation,
        "pct_above_entry":         pct_vs_entry,
        "entry_window_status":     ew_status,
        "delta_score_direction":   delta_direction,
        "part_b_driver":           wt_entry.get("part_b_driver"),
        "binary_event_within_90d": binary_event_within_90d,
        "analyst_disparity":       analyst_disparity,
    }

    return {
        "partial_conviction": {
            "valuation":        {"score": dim1_s, "basis": dim1_b},
            "growth_durability":{"score": dim2_s, "basis": dim2_b},
            "profitability":    {"score": dim3_s, "basis": dim3_b},
            "balance_sheet":    {"score": dim4_s, "basis": dim4_b},
            "management":       {"score": dim5_s, "basis": dim5_b},
            "moat":             {"score": dim6_s, "basis": dim6_b},
            "risk_reward":      {"score": dim7_s, "basis": dim7_b},
            "macro_resilience": {"score": None, "basis": "[Step 9B: apply regime classification]"},
            "portfolio_fit":    {"score": None, "basis": "[Step 9B: apply Steps 5/7 findings]"},
            "execution":        {"score": None, "basis": "[Step 9B: apply Step 2 cash + Step 7 preclearance]"},
        },
        "strategic_conviction_score": subtotal,   # renamed from partial_score_subtotal
        "analyst_disparity":          analyst_disparity,
        "risk_flags":                 risk_flags,
    }


# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------

def assign_main_tier(source_score: float | None,
                      t1_cut: float, t2_cut: float,
                      normalised_score: float | None = None) -> str:
    """Jul-2026 (Raj): FORWARD-LED tiering. Tier is a rank band on the Source Score
    (forward + revisions + implied-upside*confidence + quality), NOT the price window.
    Top names -> T1 (up to ~5, the review's deep-dive set), next -> T2, rest -> T3.
    The old price-vs-entry-level tiering has been removed entirely.
    HARD QUALITY FLOOR: a name below the removal floor (normalised_score < 60) can never
    be T1/T2 regardless of Source Score -- it is not deployable (prevents a low-quality,
    high-implied-upside 'cheap because it crashed' name from tiering as Buy Now)."""
    if normalised_score is not None and normalised_score < 60:
        return "T3"
    if source_score is None:
        return "T3"
    if t1_cut > 0 and source_score >= t1_cut:
        return "T1"
    if source_score >= t2_cut:
        return "T2"
    return "T3"


def assign_vci_tier(acs_score: int | None, current_price: float | None,
                     entry_level: float | None) -> tuple[str, bool]:
    """Returns (tier, nvidia_bypass_eligible)."""
    acs = acs_score or 0
    at_or_below_entry = (current_price is not None and entry_level is not None
                          and current_price <= entry_level)
    above_20pct = (current_price is not None and entry_level is not None
                    and current_price > entry_level * 1.20)

    # T3-A: ACS < 60 — never bypass eligible
    if acs < 60:
        return "T3-A", False

    # T1-A: ACS >= 75 AND at or below entry
    if acs >= 75 and at_or_below_entry:
        return "T1-A", False  # bypass_eligible set separately based on classification

    # T2-A: everything else (ACS 60-74, or ACS>=75 above entry)
    # But if >20% above entry: T2-A, no bypass regardless
    return "T2-A", False


def nvidia_bypass_flag(acs_score: int | None, classification: str,
                        tier: str, current_price: float | None,
                        entry_level: float | None) -> bool:
    """Compute nvidia_bypass_eligible per spec."""
    acs = acs_score or 0
    is_nvidia_class = "NVIDIA" in (classification or "").upper()

    # T3-A: never eligible
    if tier == "T3-A":
        return False

    # >20% above entry: never eligible
    above_20pct = (current_price is not None and entry_level is not None
                    and current_price > entry_level * 1.20)
    if above_20pct:
        return False

    # Must be ACS >= 85 and NVIDIA-CLASS
    if acs >= 85 and is_nvidia_class:
        return True

    return False


# ---------------------------------------------------------------------------
# T2 abbreviated score (5 dimensions)
# ---------------------------------------------------------------------------

def compute_t2_score(ticker_scored: dict, wt_entry: dict) -> dict:
    """Abbreviated 5-dimension T2 score (max 50, Portfolio Fit = null)."""
    pipeline = wt_entry.get("source_pipeline", "growth_stock")
    acs_breakdown = wt_entry.get("acs_breakdown", "")
    dim1_s, _ = score_dim1_valuation(ticker_scored, pipeline, acs_breakdown)
    dim2_s, _ = score_dim2_growth(ticker_scored, pipeline, acs_breakdown)
    dim6_s, _ = score_dim6_moat(ticker_scored, pipeline, acs_breakdown)
    dim7_s, _ = score_dim7_risk_reward(ticker_scored, pipeline, acs_breakdown)
    partial = dim1_s + dim2_s + dim6_s + dim7_s
    return {
        "valuation":         {"score": dim1_s},
        "growth_durability": {"score": dim2_s},
        "moat":              {"score": dim6_s},
        "risk_reward":       {"score": dim7_s},
        "portfolio_fit":     {"score": None, "basis": "[Step 9B]"},
        "_partial_4dim":     partial,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build Step 9 pre-scored conviction output from watchlist_scored JSON."
    )
    parser.add_argument("--scored",       required=True,
                        help="Path to watchlist_scored_mmm_yyyy.json")
    parser.add_argument("--watchlist",    required=True,
                        help="Path to watchlist_tickers.json")
    parser.add_argument("--month-label",  required=True,
                        help="Month label e.g. jul_2026")
    parser.add_argument("--out",          required=True,
                        help="Output path for step9_pre_mmm_yyyy.json")
    args = parser.parse_args()

    # Load scored JSON
    with open(args.scored, encoding="utf-8") as f:
        scored = json.load(f)

    # Load watchlist_tickers.json
    with open(args.watchlist, encoding="utf-8") as f:
        wt_raw = json.load(f)

    # Build lookup: ticker → watchlist_tickers entry
    # Priority order: stock_sleeve > watchlist > vci_watchlist > candidate_pool
    # (stock_sleeve wins if ticker appears in multiple sections)
    wt_lookup: dict[str, dict] = {}
    for entry in wt_raw.get("candidate_pool", []):    # lowest priority — add first
        wt_lookup[entry["ticker"]] = entry
    for entry in wt_raw.get("vci_watchlist", []):
        wt_lookup[entry["ticker"]] = entry
    for entry in wt_raw.get("watchlist", []):
        wt_lookup[entry["ticker"]] = entry
    for entry in wt_raw.get("stock_sleeve", []):      # highest priority — overwrites all
        wt_lookup[entry["ticker"]] = entry

    tickers_scored: dict = scored.get("tickers", {})
    conviction_ranking: list = scored.get("conviction_ranking", [])

    # Build delta_score and part_b_driver lookups from conviction_ranking
    cr_lookup: dict[str, dict] = {cr["ticker"]: cr for cr in conviction_ranking}

    # Build stock_sleeve lookup for overlap flag computation
    sleeve_tickers = {
        entry["ticker"]: entry
        for entry in wt_raw.get("stock_sleeve", [])
    }
    sleeve_sectors = {
        entry.get("ticker"): entry.get("sector", "").lower()
        for entry in wt_raw.get("stock_sleeve", [])
        if entry.get("sector")
    }

    # Jul-2026 (Raj): Source-Score rank-band cutoffs for forward-led tiering.
    # Growth (main watchlist + candidate_pool) names only; top ~5 -> T1, next ~5 -> T2.
    _growth_names = ({e["ticker"] for e in wt_raw.get("watchlist", [])}
                     | {e["ticker"] for e in wt_raw.get("candidate_pool", [])})
    _growth_scores = sorted(
        [(wt_lookup.get(t, {}).get("source_score") or 0.0) for t in _growth_names],
        reverse=True)
    _T1_CUT = _growth_scores[4] if len(_growth_scores) >= 5 else (_growth_scores[-1] if _growth_scores else 0.0)
    _T2_CUT = _growth_scores[9] if len(_growth_scores) >= 10 else (_growth_scores[-1] if _growth_scores else 0.0)

    # Categorise tickers
    main_t1, main_t2, main_t3 = [], [], []
    vci_t1a, vci_t2a, vci_t3a = [], [], []
    pool_t1, pool_t2, pool_t3  = [], [], []    # candidate_pool tiers

    for ticker, ts in tickers_scored.items():
        wt_entry = wt_lookup.get(ticker, {})
        pipeline = wt_entry.get("source_pipeline", ts.get("_pipeline", "growth_stock"))
        current_price = ts.get("current_price")
        entry_level = ts.get("_entry_level") or wt_entry.get("entry_level")
        entry_currency = wt_entry.get("entry_currency", "USD")
        path = wt_entry.get("path", "A")
        rank = wt_entry.get("rank") or cr_lookup.get(ticker, {}).get("rank", 99)
        delta_score = cr_lookup.get(ticker, {}).get("delta_score", 0)
        part_b_driver = cr_lookup.get(ticker, {}).get("part_b_driver")
        sector_type, sector_type_source = derive_sector_type(wt_entry)
        acs_breakdown = wt_entry.get("acs_breakdown", "")

        # Entry-level governance (from entry_level_builder.py)
        entry_provisional = bool(wt_entry.get("entry_level_provisional"))
        entry_status      = wt_entry.get("entry_level_status")
        entry_confidence  = wt_entry.get("entry_level_confidence")
        entry_confirm_req = bool(wt_entry.get("confirm_required")) or entry_provisional
        entry_missing     = (entry_level is None) or (entry_status == "missing_after_builder")

        pct_vs_entry = None
        if current_price and entry_level:
            pct_vs_entry = round((current_price - entry_level) / entry_level * 100, 1)

        # Compute portfolio overlap flags (used in all tier records)
        overlap_flags = compute_portfolio_overlap_flags(ticker, ts, sleeve_tickers, sleeve_sectors)

        # E3 — deployment-gate pre-flags (shared with rerank action-stack caps). Catalyst rebuts
        # the revision-cut disqualifier (Door-C carve-out). Judgement gates are surfaced, not applied.
        _has_catalyst = bool(wt_entry.get("confirmed_catalyst") or wt_entry.get("catalyst_protected"))
        _gf = (_dflags.compute_gate_flags(ts, _has_catalyst) if _dflags
               else {"disqualifier_flags": [], "review_flags": [], "forward_axis_flags": []})

        # Jul-2026 (Raj) — HARD FLAG so the review can't reflexively reject a name for trading
        # above consensus fair value. price_ahead_of_consensus = negative implied upside BUT net
        # revisions positive => the consensus target lags a rising estimate cycle (momentum ahead
        # of re-rating), a legitimate BUY, not overvaluation. The review MUST NOT block a capital
        # decision on the consensus gap alone when valuation_review_flag == DO_NOT_BLOCK.
        _sb = wt_entry.get("selection_basis", {}) or {}
        _impl_up = _sb.get("upside_to_fv")
        _rev01   = _sb.get("revisions")
        _net_rev_pos = (_rev01 is not None and _rev01 >= 0.5)
        _above_fv    = isinstance(_impl_up, (int, float)) and _impl_up < 0
        _price_ahead = bool(_above_fv and _net_rev_pos)
        _val_flag = ("PRICE_AHEAD_OF_CONSENSUS_DO_NOT_BLOCK" if _price_ahead
                     else "ABOVE_FAIR_VALUE_REVISIONS_NOT_POSITIVE_INVESTIGATE" if _above_fv
                     else "NORMAL")

        base_record = {
            "ticker":             ticker,
            "implied_upside_to_fv":     _impl_up,
            "net_revisions_positive":   _net_rev_pos,
            "price_ahead_of_consensus": _price_ahead,
            "valuation_review_flag":    _val_flag,
            "rank":               rank,
            "pipeline":           pipeline,
            "path":               path,
            "current_price":      current_price,
            "entry_level":        entry_level,
            "entry_currency":     entry_currency,
            "pct_vs_entry":       pct_vs_entry,
            "delta_score":        delta_score,
            "part_b_driver":      part_b_driver,
            "sector_type":        sector_type,
            "sector_type_source": sector_type_source,
            "normalised_score":   wt_entry.get("normalised_score"),
            "source_score":       wt_entry.get("source_score"),
            "entry_level_status":      entry_status,
            "entry_level_provisional": entry_provisional,
            "entry_level_confidence":  entry_confidence,
            "entry_level_confirm_required": entry_confirm_req,
            "entry_window_score": compute_entry_window_score(pct_vs_entry),
            "portfolio_overlap":  overlap_flags,
            # E3 — CONTRACTS #4 deployment-gate pre-flags + forward-axis tags + Step 9/10 checklist
            "dims_precomputed":      "7/10",
            "forward_axis_flags":    _gf["forward_axis_flags"],
            "gate_flags":            _gf["disqualifier_flags"] + _gf["review_flags"],
            "disqualifier_flags":    _gf["disqualifier_flags"],
            "review_flags":          _gf["review_flags"],
            "judgment_gates_pending": list(_dflags.JUDGMENT_GATES) if _dflags else [],
        }

        # Determine kind from scored data
        scored_kind = ts.get("_kind", "unknown")

        if pipeline == "vci":
            acs_score = wt_entry.get("acs_score") or ts.get("acs_score")
            classification = wt_entry.get("classification", "")
            tier, _ = assign_vci_tier(acs_score, current_price, entry_level)
            bypass = nvidia_bypass_flag(acs_score, classification, tier,
                                         current_price, entry_level)
            vci_record = {
                **base_record,
                "tier":                tier,
                "acs_score":           acs_score,
                "classification":      classification,
                "nvidia_signals":      wt_entry.get("nvidia_signals", ""),
                "vci_run_date":        wt_entry.get("vci_run_date", ""),
                "thesis_direction":    None,
                "thesis_break_summary": wt_entry.get("thesis_break_summary", ""),
                "nvidia_bypass_eligible": bypass,
                "decision_bucket":     compute_decision_bucket(
                    tier=tier,
                    pipeline="vci",
                    normalised_score=None,
                    stale_score_flag=False,
                    probation_flag=False,
                    thesis_break_triggered=False,
                    acs_score=acs_score,
                ),
            }
            if tier == "T1-A":
                vci_t1a.append(vci_record)
            elif tier == "T2-A":
                vci_t2a.append(vci_record)
            else:
                vci_t3a.append(vci_record)

        elif scored_kind == "candidate_pool":
            # Candidate pool entry: tier by Source Score rank band (forward-led), 7 dims
            tier = assign_main_tier(wt_entry.get("source_score"), _T1_CUT, _T2_CUT,
                                    normalised_score=wt_entry.get("normalised_score"))
            if tier == "T1":
                dim_data = compute_7_dimensions(ticker, ts, wt_entry, pct_vs_entry=pct_vs_entry)
                pool_record = {
                    **base_record,
                    "tier":                 "T1",
                    "total_score_54":       ts.get("total_score"),
                    **dim_data,
                    "decision_bucket":      compute_decision_bucket(
                        tier="T1",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible": False,
                }
                pool_t1.append(pool_record)
            elif tier == "T2":
                t2_score   = compute_t2_score(ts, wt_entry)
                t2_partial = t2_score.pop("_partial_4dim", 0)
                pool_record = {
                    **base_record,
                    "tier":                 "T2",
                    "t2_score":             t2_score,
                    "t2_partial":           t2_partial,
                    "entry_level_decision": None,
                    "decision_bucket":      compute_decision_bucket(
                        tier="T2",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible": False,
                }
                pool_t2.append(pool_record)
            else:
                pool_record = {
                    **base_record,
                    "tier":             "T3",
                    "thesis_break_summary": wt_entry.get("thesis_break_summary", ""),
                    "thesis_direction": None,
                    "entry_level_reassessment": None,
                    "decision_bucket":  compute_decision_bucket(
                        tier="T3",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible": False,
                }
                pool_t3.append(pool_record)

        else:
            # Main watchlist
            tier = assign_main_tier(wt_entry.get("source_score"), _T1_CUT, _T2_CUT,
                                    normalised_score=wt_entry.get("normalised_score"))
            if tier == "T1":
                dim_data = compute_7_dimensions(ticker, ts, wt_entry, pct_vs_entry=pct_vs_entry)
                t1_record = {
                    **base_record,
                    "tier":                    "T1",
                    "total_score_54":          ts.get("total_score"),
                    **dim_data,
                    "decision_bucket":         compute_decision_bucket(
                        tier="T1",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible":  False,
                }
                main_t1.append(t1_record)
            elif tier == "T2":
                t2_score = compute_t2_score(ts, wt_entry)
                t2_partial = t2_score.pop("_partial_4dim", 0)
                t2_record = {
                    **base_record,
                    "tier":               "T2",
                    "t2_score":           t2_score,
                    "t2_partial":         t2_partial,
                    "entry_level_decision": None,
                    "decision_bucket":    compute_decision_bucket(
                        tier="T2",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible": False,
                }
                main_t2.append(t2_record)
            else:  # T3
                t3_record = {
                    **base_record,
                    "tier":             "T3",
                    "thesis_break_summary": wt_entry.get("thesis_break_summary", ""),
                    "thesis_direction": None,
                    "entry_level_reassessment": None,
                    "decision_bucket":  compute_decision_bucket(
                        tier="T3",
                        pipeline=pipeline,
                        normalised_score=wt_entry.get("normalised_score"),
                        stale_score_flag=wt_entry.get("stale_score_flag", False),
                        probation_flag=wt_entry.get("probation_flag", False),
                        thesis_break_triggered=False,
                        entry_provisional=entry_provisional,
                        entry_missing=entry_missing,
                    ),
                    "nvidia_bypass_eligible": False,
                }
                main_t3.append(t3_record)

    # Sort main watchlist and VCI tiers by rank
    for lst in (main_t1, main_t2, main_t3, vci_t1a, vci_t2a, vci_t3a):
        lst.sort(key=lambda e: e.get("rank", 99))
    # Candidate pool sorted by normalised_score descending (no rank assigned)
    for lst in (pool_t1, pool_t2, pool_t3):
        lst.sort(key=lambda e: -(e.get("normalised_score") or 0))

    # --- Build deployment_priority_rank ---
    # Combined flat list: main watchlist T1/T2/T3 + candidate_pool T1/T2/T3
    # Sorted by tier (T1 first) then strategic_conviction_score desc

    def _deployment_sort_key(entry: dict) -> tuple:
        """Jul-2026 (Raj): FORWARD-LED. Order strictly by the Source Score (forward +
        revisions + implied-upside*confidence + quality), which now excludes the price
        window. Tiebreak: normalised_score, then ticker (deterministic)."""
        return (-(entry.get("source_score") or 0.0),
                -(entry.get("normalised_score") or 0.0),
                str(entry.get("ticker", "")))

    _deployment_pool = []
    # Jul-2026 (Raj): the deployment priority stack lists DEPLOYABLE names only. Exclude any
    # name below the quality floor (normalised_score < 60 -> "Remove / Reject"); it is not a
    # buy candidate and must not appear in the action stack even with a high Source Score.
    def _deployable(entry):
        return (entry.get("normalised_score") or 0) >= 60
    for entry in (main_t1 + main_t2 + main_t3):
        if _deployable(entry):
            _deployment_pool.append({**entry, "_source": "watchlist"})
    for entry in (pool_t1 + pool_t2 + pool_t3):
        if _deployable(entry):
            _deployment_pool.append({**entry, "_source": "candidate_pool"})

    _deployment_pool.sort(key=_deployment_sort_key)

    deployment_priority_rank = []
    for i, entry in enumerate(_deployment_pool, 1):
        deployment_priority_rank.append({
            "deployment_rank":            i,
            "ticker":                     entry.get("ticker"),
            "tier":                       entry.get("tier"),
            "source":                     entry.get("_source"),
            "source_score":               entry.get("source_score"),
            "valuation_review_flag":      entry.get("valuation_review_flag"),
            "price_ahead_of_consensus":   entry.get("price_ahead_of_consensus"),
            "implied_upside_to_fv":       entry.get("implied_upside_to_fv"),
            "normalised_score":           entry.get("normalised_score"),
            "strategic_conviction_score": entry.get("strategic_conviction_score"),
            "entry_window_score":         entry.get("entry_window_score"),
            "decision_bucket":            entry.get("decision_bucket"),
            "pct_vs_entry":               entry.get("pct_vs_entry"),
        })

    # Produce output
    output = {
        "_meta": {
            "month_label":               args.month_label,
            "produced_at":               datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tier_boundary_t2_pct":      20,
            "vci_t2a_acs_threshold":     60,
            "vci_deployment_threshold":  75,
            "in_window_count":           len(main_t1) + len([v for v in vci_t1a]) + len(pool_t1),
            "t1_count":                  len(main_t1),
            "t2_count":                  len(main_t2),
            "t3_count":                  len(main_t3),
            "t1a_count":                 len(vci_t1a),
            "t2a_count":                 len(vci_t2a),
            "t3a_count":                 len(vci_t3a),
            "pool_t1_count":             len(pool_t1),
            "pool_t2_count":             len(pool_t2),
            "pool_t3_count":             len(pool_t3),
            "deployment_priority_count": len(deployment_priority_rank),
        },
        "main_watchlist": {
            "T1": main_t1,
            "T2": main_t2,
            "T3": main_t3,
        },
        "vci_watchlist": {
            "T1_A": vci_t1a,
            "T2_A": vci_t2a,
            "T3_A": vci_t3a,
        },
        "candidate_pool": {
            "T1": pool_t1,
            "T2": pool_t2,
            "T3": pool_t3,
        },
        "deployment_priority_rank": deployment_priority_rank,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"  step9_pre written: {args.out}")
    print(f"  Main watchlist: T1={len(main_t1)}, T2={len(main_t2)}, T3={len(main_t3)}")
    print(f"  Candidate pool: T1={len(pool_t1)}, T2={len(pool_t2)}, T3={len(pool_t3)}")
    print(f"  VCI: T1-A={len(vci_t1a)}, T2-A={len(vci_t2a)}, T3-A={len(vci_t3a)}")
    print(f"  Deployment priority list: {len(deployment_priority_rank)} names")
    if deployment_priority_rank:
        print(f"  Top 3 deployment: "
              + " | ".join(f"#{r['deployment_rank']} {r['ticker']} [{r['tier']}] "
                           f"bucket={r['decision_bucket']}"
                           for r in deployment_priority_rank[:3]))


if __name__ == "__main__":
    main()
