#!/usr/bin/env python3
"""
portfolio_analytics.py  --  ISA Portfolio Analytics Engine
Version: 1.0  |  2026-05-31

Reads portfolio_data_mmm_yyyy.json + target_weights.json and produces
analytics_data_mmm_yyyy.json. Called by monthly_isa_prerun.py.

Computes (all deterministic — no web lookups required):
  - Per-fund drift vs target weight and policy band
  - Signal classification: Hold / Watch / Rebalancing candidate
  - Bucket totals (B1/B2/B3) vs bucket bands
  - VUAG + Vanguard US consolidation flag
  - Rebalancing candidates: trade sizes in £ to return to target
  - Stock sleeve phase status (Phase 1 / Phase 2 / transition imminent)
  - Stock sleeve return (simple or annualised — requires trades_log data if available)
  - Section A/B/C framework skeleton (B = indicative until trades log read)
  - Overlap check structure: lists which stocks to check against fund top-10 holdings
  - Capital deployment summary: cash available, stock sleeve headroom, constraints

Does NOT compute:
  - Estimated fund returns (requires live web data — done by Claude at Step 8A)
  - Stock thesis checks (judgment — done by Claude at Step 8)
  - Macro regime classification (judgment — done by Claude at Step 7)

Usage:
    python3 portfolio_analytics.py
        --portfolio portfolio_data_mmm_yyyy.json
        --weights   target_weights.json
        [--trades-log path/to/project_isa_trades_log.md]
        [--prior-portfolio portfolio_data_prior_mmm_yyyy.json]
        [--out analytics_data_mmm_yyyy.json]
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime

# G1/G2 — fund-return sourcing + 12% gate + fund actions (single source; additive, flag-gated).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scoring_config as _cfg
    import fund_returns as _fr
except Exception:
    _cfg = None
    _fr = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STANDING_ORDER = 1250.0
CASH_BUFFER_MIN = 150.0
AJ_BELL_DEALING_COST = 5.0       # £5 per trade
FX_COST_PCT = 0.0075             # 0.75% for USD purchases

# Minimum trade size (below this dealing costs make trade uneconomical)
MIN_ECONOMIC_TRADE = 500.0


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------
def classify_signal(
    actual_pct: float,
    target_pct: float,
    band_low: float,
    band_high: float,
    below_threshold: bool,
    consecutive_months_below_threshold: int = 0,
    consecutive_months_outside_band: int = 0,
) -> str:
    """
    Returns one of: Hold | Watch | Research trigger | Rebalancing candidate
    Per Step 8A.1 signal logic.
    """
    drift = actual_pct - target_pct
    outside_low  = actual_pct < band_low
    outside_high = actual_pct > band_high

    outside_band = outside_low or outside_high
    band_breach_magnitude = 0.0
    if outside_low:
        band_breach_magnitude = band_low - actual_pct
    elif outside_high:
        band_breach_magnitude = actual_pct - band_high

    # Rebalancing candidate: outside band by >2pp
    if band_breach_magnitude > 0.02:
        return "Rebalancing candidate"

    # Watch: outside band by <=2pp
    if outside_band:
        return "Watch"

    # Within band — check return signals
    if below_threshold:
        if consecutive_months_below_threshold >= 2:
            return "Research trigger"
        return "Watch"

    return "Hold"


def drift_direction(actual_pct: float, target_pct: float) -> str:
    d = actual_pct - target_pct
    if d > 0.001:
        return "overweight"
    if d < -0.001:
        return "underweight"
    return "at_target"


# ---------------------------------------------------------------------------
# Rebalancing trade size calculator
# ---------------------------------------------------------------------------
def calc_rebalance_trade(
    fund_name: str,
    actual_pct: float,
    target_pct: float,
    total_portfolio_value: float,
) -> dict:
    """
    Returns the trade needed to bring a fund back to its target weight.
    Positive = buy; negative = sell.
    """
    diff_pct   = target_pct - actual_pct
    trade_gbp  = diff_pct * total_portfolio_value
    action     = "BUY" if trade_gbp > 0 else "SELL"
    abs_trade  = abs(trade_gbp)
    economic   = abs_trade >= MIN_ECONOMIC_TRADE

    return {
        "fund":            fund_name,
        "drift_pct":       round(actual_pct - target_pct, 4),
        "drift_pp":        round((actual_pct - target_pct) * 100, 2),
        "trade_action":    action,
        "trade_gbp":       round(trade_gbp, 2),
        "abs_trade_gbp":   round(abs_trade, 2),
        "economic":        economic,
        "dealing_cost":    AJ_BELL_DEALING_COST if economic else 0.0,
        "note":            (
            f"Trade to target: {action} £{abs_trade:,.0f}. "
            + ("Economical." if economic else f"Below £{MIN_ECONOMIC_TRADE:,.0f} minimum — uneconomical to execute.")
        ),
    }


# ---------------------------------------------------------------------------
# Stock sleeve return (if trades log available)
# ---------------------------------------------------------------------------
def parse_trades_log_positions(trades_log_path: str) -> list:
    """
    Parses project_isa_trades_log.md for open stock positions.
    Returns list of {ticker, purchase_date, cost_gbp}.
    Minimal regex parse — robust to format changes.
    """
    if not trades_log_path or not os.path.exists(trades_log_path):
        return []

    positions = []
    try:
        with open(trades_log_path, encoding="utf-8") as f:
            text = f.read()

        # Look for rows in the open positions table:
        # | TICKER | Date | ... | Cost £ | ...
        # Pattern: pipe-delimited table row with a ticker and date
        row_pattern = re.compile(
            r"\|\s*([A-Z]{1,6})\s*\|"          # ticker
            r"[^|]*\|"                           # name
            r"\s*(\d{2}-\w{3}-\d{4})\s*\|",    # purchase date dd-Mon-YYYY
            re.MULTILINE
        )
        for m in row_pattern.finditer(text):
            ticker = m.group(1).strip()
            date_str = m.group(2).strip()
            try:
                purchase_date = datetime.strptime(date_str, "%d-%b-%Y").date()
            except ValueError:
                continue
            positions.append({
                "ticker":        ticker,
                "purchase_date": purchase_date,
            })
    except Exception:
        pass

    return positions


def calc_stock_sleeve_return(
    stocks: list,
    trades_log_positions: list,
    run_date: date,
) -> dict:
    """
    Calculates stock sleeve return check per Section 4B methodology.
    Returns dict with result, method, annualised_return, status, note.
    """
    if not stocks:
        return {
            "method":       "none",
            "result":       "No stock sleeve positions",
            "status":       "indicative",
            "note":         "Stock sleeve is empty.",
        }

    total_value = sum(s["value_gbp"] for s in stocks)
    total_cost  = sum(s["cost_gbp"]  for s in stocks)

    if total_cost == 0:
        return {
            "method": "none",
            "result": "Cost basis unavailable",
            "status": "indicative",
        }

    simple_return_pct = round((total_value - total_cost) / total_cost * 100, 2)

    # Build date map from trades log
    date_map = {p["ticker"]: p["purchase_date"] for p in trades_log_positions}

    # Check if we have >=3 positions with >=6 months history
    mature_positions = []
    for s in stocks:
        purchase_date = date_map.get(s["ticker"])
        if purchase_date:
            days_held = (run_date - purchase_date).days
            if days_held >= 180:   # 6 months
                mature_positions.append({
                    **s,
                    "days_held": days_held,
                    "purchase_date": purchase_date,
                })

    use_annualised = len(stocks) >= 3 and len(mature_positions) >= 3

    if use_annualised:
        # Cost-weighted annualised return across mature positions
        weighted_ann = 0.0
        weight_total = 0.0
        for p in mature_positions:
            if p["cost_gbp"] and p["cost_gbp"] > 0 and p["days_held"] > 0:
                ann = ((p["value_gbp"] / p["cost_gbp"]) ** (365 / p["days_held"]) - 1) * 100
                weighted_ann += ann * p["cost_gbp"]
                weight_total += p["cost_gbp"]
        if weight_total > 0:
            annualised_return = round(weighted_ann / weight_total, 2)
        else:
            annualised_return = None

        method = "annualised"
        result_str = f"{annualised_return:.1f}% p.a. (cost-weighted, {len(mature_positions)} positions)"
    else:
        annualised_return = None
        method = "simple_indicative"
        result_str = f"{simple_return_pct:.1f}% simple return (indicative — sleeve <6 months or <3 positions)"

    # Status classification
    if method == "simple_indicative":
        status = "indicative"
        status_label = "Indicative only"
    elif annualised_return is not None:
        if annualised_return >= 18.0:
            status = "on_track"
            status_label = "On track"
        elif annualised_return >= 15.0:
            status = "watch"
            status_label = "Watch"
        else:
            status = "flag"
            status_label = "Flag"
    else:
        status = "indicative"
        status_label = "Indicative only"

    return {
        "method":            method,
        "simple_return_pct": simple_return_pct,
        "annualised_return": annualised_return,
        "result":            result_str,
        "status":            status,
        "status_label":      status_label,
        "vs_18pct_assumption": "On track (>=18%)" if (annualised_return or 0) >= 18 else (
            "Watch (15-18%)" if (annualised_return or 0) >= 15 else
            ("Flag (<15%)" if method == "annualised" and annualised_return is not None else "Indicative")
        ),
        "total_value_gbp":   round(total_value, 2),
        "total_cost_gbp":    round(total_cost, 2),
        "positions":         len(stocks),
        "mature_positions":  len(mature_positions),
    }


# ---------------------------------------------------------------------------
# Overlap check structure
# ---------------------------------------------------------------------------
def build_overlap_check_structure(stocks: list, funds: list) -> dict:
    """
    Produces the overlap check input for Claude to execute at Step 8A.
    Lists each stock in the stock sleeve with instructions to check
    its presence in each fund's top-10 holdings.
    Claude runs the actual lookup; this pre-structures the check.
    """
    stock_tickers = [s["ticker"] for s in stocks]
    fund_names    = [f["name"] for f in funds]

    checks = []
    for s in stocks:
        checks.append({
            "stock_ticker": s["ticker"],
            "stock_name":   s["name"],
            "stock_weight_pct": s.get("weight_pct", 0),
            "instruction":  (
                f"Check whether {s['ticker']} appears in the top-10 holdings of any fund. "
                f"If found: compute combined effective weight = direct {s.get('weight_pct',0):.2f}% "
                f"+ (fund weight × fund's allocation to {s['ticker']}). "
                f"Flag if combined effective weight > 5.0%."
            ),
            "flag_threshold_pct": 5.0,
        })

    return {
        "stock_tickers":    stock_tickers,
        "fund_names":       fund_names,
        "checks":           checks,
        "instruction":      (
            "Use AJ Bell or Morningstar fund holdings data to check each stock in the table. "
            "Only check top-10 holdings per fund — marginal positions not worth retrieval time. "
            "Format result as: OVERLAP: [Stock] — direct [X.X%] + [Fund] est. [Y.Y%] = combined [N.N%] — [WITHIN LIMIT / FLAG >5%]. "
            "If no stock sleeve names appear in any fund top-10, state: No material overlap detected."
        ),
    }


# ---------------------------------------------------------------------------
# Capital deployment summary
# ---------------------------------------------------------------------------
def build_capital_summary(
    portfolio: dict,
    target_weights: dict,
    stock_sleeve_pct: float,
) -> dict:
    total = portfolio["summary"]["total_value_gbp"]
    cash_effective = portfolio["summary"]["cash_effective_gbp"]
    cash_deployable = portfolio["summary"]["cash_deployable_gbp"]

    phase = target_weights["_meta"]["current_phase"]
    phase2_trigger = target_weights["thresholds"]["phase_transition_pct"]
    stock_target_low  = target_weights["stock_sleeve"]["phase1_target_low"] if phase == 1 else target_weights["stock_sleeve"]["phase2_target_low"]
    stock_target_high = target_weights["stock_sleeve"]["phase1_target_high"] if phase == 1 else target_weights["stock_sleeve"]["phase2_target_high"]

    stock_value = portfolio["summary"]["stock_sleeve_value_gbp"]
    stock_headroom_to_high = max(0, stock_target_high * total - stock_value)

    max_new_position_size = target_weights["thresholds"]["max_stock_position_pct"] * total
    typical_position_low  = target_weights["thresholds"]["typical_stock_position_low"] * total
    typical_position_high = target_weights["thresholds"]["typical_stock_position_high"] * total

    # Standing order note
    so_note = (
        f"Standing order: £{STANDING_ORDER:,.0f}/month reserved for stock sleeve. "
        f"Does NOT flow to funds even if no stock action taken — adds to cash buffer."
    )

    return {
        "total_portfolio_gbp":          total,
        "cash_effective_gbp":           cash_effective,
        "cash_deployable_gbp":          cash_deployable,
        "standing_order_monthly":       STANDING_ORDER,
        "cash_buffer_min":              CASH_BUFFER_MIN,
        "stock_sleeve_current_pct":     stock_sleeve_pct,
        "stock_sleeve_current_value_gbp": stock_value,
        "stock_sleeve_target_band_low":  stock_target_low,
        "stock_sleeve_target_band_high": stock_target_high,
        "stock_sleeve_headroom_gbp":     round(stock_headroom_to_high, 2),
        "max_new_position_gbp":          round(max_new_position_size, 2),
        "typical_position_low_gbp":      round(typical_position_low, 2),
        "typical_position_high_gbp":     round(typical_position_high, 2),
        "standing_order_note":           so_note,
        "phase":                         phase,
        "phase_transition_trigger_pct":  phase2_trigger,
    }


# ---------------------------------------------------------------------------
# Main analytics engine
# ---------------------------------------------------------------------------
def run_analytics(
    portfolio: dict,
    target_weights: dict,
    prior_portfolio: dict = None,
    trades_log_path: str = None,
    run_date: date = None,
) -> dict:
    if run_date is None:
        run_date = date.today()

    total_value = portfolio["summary"]["total_value_gbp"]
    funds_list  = portfolio.get("funds", [])
    stocks_list = portfolio.get("stocks", [])
    fund_sleeve_pct  = portfolio["summary"]["fund_sleeve_pct"] / 100
    stock_sleeve_pct = portfolio["summary"]["stock_sleeve_pct"] / 100

    tw = target_weights["funds"]
    phase = target_weights["_meta"]["current_phase"]

    # ---------------------------------------------------------------------------
    # Per-fund drift table (Section A basis)
    # ---------------------------------------------------------------------------
    fund_drift_rows = []
    rebalancing_candidates = []
    bucket_actuals = {"B1": 0.0, "B2": 0.0, "B3": 0.0, "unknown": 0.0}

    for fund in funds_list:
        ticker = fund["ticker"]
        actual_pct = fund["value_gbp"] / total_value  # as fraction

        if ticker in tw:
            fw = tw[ticker]
            target_pct  = fw["target_pct"]
            band_low    = fw["band_low"]
            band_high   = fw["band_high"]
            bucket      = fw["bucket"]
            min_return  = fw["min_expected_return"]
            fund_name   = fw["name"]
            pending_sale = fw.get("pending_sale", False)
        else:
            # Unknown fund — not in target weights
            target_pct  = None
            band_low    = None
            band_high   = None
            bucket      = "unknown"
            min_return  = None
            fund_name   = fund["name"]
            pending_sale = False

        bucket_actuals[bucket if bucket in bucket_actuals else "unknown"] += actual_pct

        drift_pp = round((actual_pct - (target_pct or 0)) * 100, 2)

        if target_pct is not None and band_low is not None:
            signal = classify_signal(
                actual_pct, target_pct, band_low, band_high,
                below_threshold=False,  # estimated return unknown until Claude looks up
            )
        else:
            signal = "Unknown — not in target weights"

        row = {
            "ticker":          ticker,
            "name":            fund_name,
            "bucket":          bucket,
            "target_pct":      round(target_pct * 100, 1) if target_pct is not None else None,
            "actual_pct":      round(actual_pct * 100, 2),
            "drift_pp":        drift_pp,
            "band_low_pct":    round(band_low * 100, 1) if band_low is not None else None,
            "band_high_pct":   round(band_high * 100, 1) if band_high is not None else None,
            "band_breach":     (
                "Yes" if (band_low is not None and (actual_pct < band_low or actual_pct > band_high)) else "No"
                if band_low is not None else "N/A"
            ),
            "min_return_pct":  round(min_return * 100, 1) if min_return is not None else None,
            "est_return_pct":  None,   # populated by Claude at Step 8A
            "est_return_source": None, # "Morningstar fwd" or "trailing 3yr"
            "below_threshold": None,   # populated by Claude after estimated return retrieved
            "signal":          signal,
            "signal_note":     (
                "Est. return unknown — signal may upgrade to Watch or Research trigger once Claude retrieves fund data."
                if signal == "Hold" else
                "Note: estimated return not yet retrieved — drift-based signal only."
            ),
            "value_gbp":       fund["value_gbp"],
            "gain_pct":        fund["gain_pct"],
            "pending_sale":    pending_sale,
        }

        fund_drift_rows.append(row)

        # Rebalancing candidates
        if signal == "Rebalancing candidate":
            trade = calc_rebalance_trade(
                fund_name, actual_pct, target_pct if target_pct is not None else actual_pct, total_value
            )
            rebalancing_candidates.append({**row, "rebalance_trade": trade})

    # ---------------------------------------------------------------------------
    # Bucket totals
    # ---------------------------------------------------------------------------
    bucket_total_rows = []
    bucket_targets = target_weights.get("bucket_totals", {})
    for bucket_key in ["B1", "B2", "B3"]:
        actual = bucket_actuals.get(bucket_key, 0.0)
        bt = bucket_targets.get(bucket_key, {})
        target = bt.get("phase1_target_pct", 0.0)
        band_l = bt.get("phase1_band_low", 0.0)
        band_h = bt.get("phase1_band_high", 0.0)
        drift  = round((actual - target) * 100, 2)
        breach = "Yes" if (actual < band_l or actual > band_h) else "No"
        bucket_total_rows.append({
            "bucket":        bucket_key,
            "target_pct":    round(target * 100, 1),
            "actual_pct":    round(actual * 100, 2),
            "drift_pp":      drift,
            "band_low_pct":  round(band_l * 100, 1),
            "band_high_pct": round(band_h * 100, 1),
            "band_breach":   breach,
        })

    # ---------------------------------------------------------------------------
    # Phase status check (Section 8A.5)
    # ---------------------------------------------------------------------------
    phase2_trigger = target_weights["thresholds"]["phase_transition_pct"]
    prior_stock_pct = None
    if prior_portfolio:
        prior_stock_pct = prior_portfolio.get("summary", {}).get("stock_sleeve_pct", 0) / 100

    current_above_trigger = stock_sleeve_pct >= phase2_trigger

    if prior_stock_pct is not None:
        prior_above_trigger = prior_stock_pct >= phase2_trigger
    else:
        prior_above_trigger = None

    if current_above_trigger and prior_above_trigger:
        phase_status = "TRANSITION_CONFIRMED"
        phase_note   = (
            f"PHASE TRANSITION CONFIRMED — stock sleeve at {stock_sleeve_pct*100:.1f}% for two consecutive months. "
            f"Claude must shift to Phase 2 target weights at Step 8A.5."
        )
    elif current_above_trigger and prior_above_trigger is None:
        phase_status = "ABOVE_TRIGGER_PRIOR_UNKNOWN"
        phase_note   = (
            f"Stock sleeve at {stock_sleeve_pct*100:.1f}% — above {phase2_trigger*100:.0f}% trigger. "
            f"Prior month data not available — cannot confirm consecutive. "
            f"Check prior portfolio manually at Step 8A.5."
        )
    elif current_above_trigger and not prior_above_trigger:
        phase_status = "ABOVE_TRIGGER_FIRST_MONTH"
        phase_note   = (
            f"Stock sleeve at {stock_sleeve_pct*100:.1f}% — crossed {phase2_trigger*100:.0f}% trigger this month. "
            f"Transition NOT YET CONFIRMED — must remain above {phase2_trigger*100:.0f}% for a second consecutive month."
        )
    else:
        pct_below = round((phase2_trigger - stock_sleeve_pct) * 100, 1)
        phase_status = "PHASE_1_ACTIVE"
        phase_note   = (
            f"Phase 1 active — stock sleeve at {stock_sleeve_pct*100:.1f}%. "
            f"{pct_below:.1f}pp below {phase2_trigger*100:.0f}% trigger. Transition not yet confirmed."
        )

    # ---------------------------------------------------------------------------
    # Stock sleeve return check
    # ---------------------------------------------------------------------------
    trades_log_positions = parse_trades_log_positions(trades_log_path)
    stock_return_check = calc_stock_sleeve_return(stocks_list, trades_log_positions, run_date)

    # ---------------------------------------------------------------------------
    # Section A: Fund sleeve weighted average return (skeleton — Claude fills est_return_pct per fund)
    # ---------------------------------------------------------------------------
    section_a = {
        "description": (
            "Fund sleeve weighted average return. "
            "Claude must populate est_return_pct for each fund row (from AJ Bell/Morningstar) "
            "then compute: sum(actual_pct * est_return_pct) across all funds. "
            "Compare to 12% threshold. PASS if >=12%, FAIL if <12%."
        ),
        "fund_rows":           fund_drift_rows,
        "threshold_pct":       12.0,
        "weighted_avg_return": None,   # computed by Claude after filling estimated returns
        "result":              None,   # PASS or FAIL — filled by Claude
        "status":              "pending_estimated_returns",
    }

    # G1/G2 — real fund-return sourcing + 12% gate + fund actions (flag-gated; additive).
    # When off, Section A stays pending (Claude fills returns manually — unchanged behaviour).
    fund_actions = []
    if _fr is not None and getattr(_cfg, "FUND_RETURN_SOURCING", False):
        try:
            _cache = _fr.default_cache_path(SCRIPT_DIR)
            _returns = _fr.source_fund_returns(funds_list, cache_path=_cache, fetch=True)
            for _row in fund_drift_rows:
                _k = (_row.get("ticker") or _row.get("name") or "").upper()
                _ri = _returns.get(_k, {})
                if _ri.get("est_return_pct") is not None:
                    _row["est_return_pct"] = _ri["est_return_pct"]
                    _row["est_return_source"] = _ri["source"]
                    if _row.get("min_return_pct") is not None:
                        _row["below_threshold"] = _ri["est_return_pct"] < _row["min_return_pct"]
                _fa = _fr.classify_fund_action(_row, _ri)
                if _fa:
                    fund_actions.append(_fa)
            _gate = _fr.compute_fund_gate(funds_list, _returns)
            section_a["weighted_avg_return"] = _gate["weighted_avg_return"]
            section_a["result"]              = _gate["result"]
            section_a["status"]              = _gate["status"]
            section_a["coverage_pct"]        = _gate["coverage_pct"]
            section_a["pending_funds"]       = _gate["pending_funds"]
            section_a["sourced_mechanically"] = True
        except Exception as _ex:
            section_a["sourcing_error"] = str(_ex)

    # ---------------------------------------------------------------------------
    # Section B: Stock sleeve aggregate return
    # ---------------------------------------------------------------------------
    section_b = {
        "description":  "Stock sleeve aggregate return check vs 18% p.a. assumption.",
        "threshold_pct": 18.0,
        **stock_return_check,
    }

    # ---------------------------------------------------------------------------
    # Section C: Total ISA return (skeleton)
    # ---------------------------------------------------------------------------
    section_c = {
        "description": (
            "Total ISA estimated return = (fund sleeve weighted avg × fund sleeve %) + "
            "(stock sleeve return × stock sleeve %). "
            "Compare to 14% working target. Claude computes after Section A is complete."
        ),
        "fund_sleeve_pct":    round(fund_sleeve_pct * 100, 2),
        "stock_sleeve_pct":   round(stock_sleeve_pct * 100, 2),
        "cash_pct":           portfolio["summary"]["cash_pct"],
        "fund_return_input":  None,   # from Section A — filled by Claude
        "stock_return_input": stock_return_check.get("annualised_return") or stock_return_check.get("simple_return_pct"),
        "total_return":       None,   # computed by Claude
        "threshold_pct":      14.0,
        "status":             "pending_section_a",
    }

    # ---------------------------------------------------------------------------
    # Overlap check structure
    # ---------------------------------------------------------------------------
    overlap_check = build_overlap_check_structure(stocks_list, funds_list)

    # ---------------------------------------------------------------------------
    # Capital summary
    # ---------------------------------------------------------------------------
    capital_summary = build_capital_summary(portfolio, target_weights, stock_sleeve_pct)

    # ---------------------------------------------------------------------------
    # Flags and validation
    # ---------------------------------------------------------------------------
    flags = []
    if portfolio["flags"].get("concentration_over_12_5pct"):
        flags.append({
            "type":    "CONCENTRATION",
            "message": f"Position(s) over 12.5% cap: {portfolio['flags']['concentration_over_12_5pct']}",
            "action":  "Review at Step 8",
        })
    if portfolio["flags"].get("vuag_plus_vanguard_us_exceeds_12_5pct"):
        combined_pct = portfolio["flags"]["vuag_plus_vanguard_us_combined_pct"]
        flags.append({
            "type":    "VUAG_COMBINED_EXPOSURE",
            "message": f"VUAG + Vanguard US Eq Idx combined: {combined_pct:.1f}% — exceeds 12.5% cap",
            "action":  "Consolidate Vanguard US Eq Idx into VUAG — see fund notes",
        })
    if rebalancing_candidates:
        for rc in rebalancing_candidates:
            flags.append({
                "type":    "REBALANCING_CANDIDATE",
                "message": f"{rc['name']} — drift {rc['drift_pp']:+.1f}pp outside band; "
                           f"trade: {rc['rebalance_trade']['trade_action']} £{rc['rebalance_trade']['abs_trade_gbp']:,.0f}",
                "action":  "Escalate to Step 10 Category 7",
            })
    if phase_status in ("TRANSITION_CONFIRMED", "ABOVE_TRIGGER_FIRST_MONTH"):
        flags.append({
            "type":    "PHASE_STATUS",
            "message": phase_note,
            "action":  "Execute Step 8A.5 phase transition check",
        })

    return {
        "_meta": {
            "source_portfolio": portfolio["_meta"]["source_file"],
            "run_month":        portfolio["_meta"]["run_month"],
            "month_label":      portfolio["_meta"]["month_label"],
            "run_date":         run_date.isoformat(),
            "computed_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "phase":            phase,
        },
        "portfolio_summary": {
            "total_value_gbp":       portfolio["summary"]["total_value_gbp"],
            "fund_sleeve_pct":       round(fund_sleeve_pct * 100, 2),
            "stock_sleeve_pct":      round(stock_sleeve_pct * 100, 2),
            "cash_pct":              portfolio["summary"]["cash_pct"],
            "cash_effective_gbp":    portfolio["summary"]["cash_effective_gbp"],
            "cash_deployable_gbp":   portfolio["summary"]["cash_deployable_gbp"],
        },
        "fund_drift_table": {
            "rows":    fund_drift_rows,
            "note":    (
                "est_return_pct and below_threshold columns are blank — Claude fills these at Step 8A "
                "by retrieving fund data from AJ Bell/Morningstar as a single parallel batch. "
                "Signal column may upgrade from Hold → Watch or Research trigger once return data is available."
            ),
        },
        "bucket_totals":         bucket_total_rows,
        "rebalancing_candidates": rebalancing_candidates,
        "phase_status": {
            "status":            phase_status,
            "note":              phase_note,
            "current_pct":       round(stock_sleeve_pct * 100, 2),
            "prior_pct":         round(prior_stock_pct * 100, 2) if prior_stock_pct is not None else None,
            "trigger_pct":       round(phase2_trigger * 100, 1),
        },
        "section_a": section_a,
        "section_b": section_b,
        "section_c": section_c,
        "overlap_check": overlap_check,
        "capital_summary": capital_summary,
        "fund_actions":    fund_actions,
        "flags":           flags,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="ISA Portfolio Analytics Engine — drift, signals, rebalancing."
    )
    parser.add_argument("--portfolio",     required=True,
                        help="Path to portfolio_data_mmm_yyyy.json")
    parser.add_argument("--weights",       default=None,
                        help="Path to target_weights.json. Defaults to Investment Analysis folder.")
    parser.add_argument("--prior-portfolio", default=None,
                        help="Path to prior month portfolio_data JSON (for phase transition check).")
    parser.add_argument("--trades-log",    default=None,
                        help="Path to project_isa_trades_log.md (for stock sleeve return calc).")
    parser.add_argument("--out",           default=None,
                        help="Output path. Defaults to analytics_data_mmm_yyyy.json.")
    args = parser.parse_args()

    # Load portfolio
    if not os.path.exists(args.portfolio):
        print(f"ERROR: Portfolio JSON not found: {args.portfolio}")
        sys.exit(1)
    with open(args.portfolio, encoding="utf-8") as f:
        portfolio = json.load(f)

    # Load target weights
    weights_path = args.weights or os.path.join(SCRIPT_DIR, "target_weights.json")
    if not os.path.exists(weights_path):
        print(f"ERROR: target_weights.json not found: {weights_path}")
        sys.exit(1)
    with open(weights_path, encoding="utf-8") as f:
        target_weights = json.load(f)

    # Load prior portfolio (optional)
    prior_portfolio = None
    if args.prior_portfolio and os.path.exists(args.prior_portfolio):
        with open(args.prior_portfolio, encoding="utf-8") as f:
            prior_portfolio = json.load(f)

    # Run
    analytics = run_analytics(
        portfolio, target_weights,
        prior_portfolio=prior_portfolio,
        trades_log_path=args.trades_log,
        run_date=date.today(),
    )

    # Output
    if args.out:
        out_path = args.out
    else:
        month_label = portfolio["_meta"]["month_label"]
        out_path = os.path.join(SCRIPT_DIR, f"analytics_data_{month_label}.json")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(analytics, f, indent=2, ensure_ascii=False)

    # Summary
    print(f"\nAnalytics complete: {analytics['_meta']['run_month']}")
    print(f"  Total value:       £{analytics['portfolio_summary']['total_value_gbp']:>12,.2f}")
    print(f"  Fund sleeve:       {analytics['portfolio_summary']['fund_sleeve_pct']:.1f}%")
    print(f"  Stock sleeve:      {analytics['portfolio_summary']['stock_sleeve_pct']:.1f}%")
    print(f"  Phase status:      {analytics['phase_status']['status']}")
    print(f"  Rebalancing cands: {len(analytics['rebalancing_candidates'])}")
    if analytics["flags"]:
        print(f"\n  FLAGS ({len(analytics['flags'])}):")
        for fl in analytics["flags"]:
            print(f"    [{fl['type']}] {fl['message']}")
    print(f"\n  Stock sleeve B check: {analytics['section_b'].get('result','N/A')}")
    print(f"    Status: {analytics['section_b'].get('status_label', analytics['section_b'].get('status',''))}")
    print(f"\nOutput written: {out_path}")
    return out_path



if __name__ == "__main__":
    main()
