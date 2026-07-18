#!/usr/bin/env python3
"""
fetch_watchlist_metrics.py  --  ISA Watchlist & Stock Sleeve Metrics Pull
Version: 2.0  |  2026-06-01

Pulls Part A/B scorecard metrics + overlays for all tickers in watchlist_tickers.json.
Supports three source pipelines — routing is driven by each entry's "source_pipeline" field:

  "growth_stock"  — standard 14-metric Part A + 13-metric Part B via screener_core.py
                    Max score /54. Hard gates: ROIC >8%, FCF positive >=3yr.
  "energy"        — 10-metric Part A + 8-metric Part B via energy_screener.py
                    Max score /36. Gates: revenue >$50M, growth >0%, EBITDA >0.
                    No gross margin gate. CapEx intensity is a positive signal.
  "vci"           — ACS score already stored in watchlist_tickers.json from VCI run.
                    Only current price is fetched (for in-window check).
                    Not re-scored via yfinance pipelines.

Sections processed from watchlist_tickers.json:
  "watchlist"      — ranked candidates (growth_stock or energy pipeline)
  "vci_watchlist"  — VCI asymmetric candidates (vci pipeline)
  "stock_sleeve"   — held positions (growth_stock pipeline by default)

Outputs:
  watchlist_metrics_mmm_yyyy.json — raw scored metrics per ticker, enriched with:
    - _kind, _source_pipeline, _entry_level, _entry_currency, _rank
    - _in_window flag (current_price <= entry_level * (1 + threshold/100))
    - _purchase_date, _cost_per_share, _shares (stock sleeve only)
    - _acs_score, _vci_run_date, _nvidia_signals, _classification (VCI only)

Usage (standalone):
    python3 fetch_watchlist_metrics.py [--watchlist /path/to/watchlist_tickers.json]
                                       [--out /path/to/output.json]
                                       [--month-label jun_2026]

Called by: monthly_isa_prerun.py (Step 4)
Depends on: screener_core.py, energy_screener.py (must be in the same directory)
"""

import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import importlib.util as _iutil
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date

log = logging.getLogger("fetch_watchlist_metrics")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Import screener_core (growth stock pipeline)
# ---------------------------------------------------------------------------
sys.path.insert(0, SCRIPT_DIR)
try:
    import screener_core as sc
    import fv_composite as _fvc     # Fix Pack A6 (P2) — THE shared FV composite (screen = deploy)
    import expected_return as _erm  # Fix Pack A2 (P2) — E[r] object on the pre-run path too
except ImportError as e:
    print(f"ERROR: Cannot import screener_core.py from {SCRIPT_DIR}: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Import energy_screener (energy pipeline — non-fatal if missing)
# ---------------------------------------------------------------------------
_ENERGY_SCREENER_AVAILABLE = False
es = None
try:
    _es_path = os.path.join(SCRIPT_DIR, "energy_screener.py")
    if os.path.exists(_es_path):
        _es_spec = _iutil.spec_from_file_location("energy_screener", _es_path)
        es = _iutil.module_from_spec(_es_spec)
        _es_spec.loader.exec_module(es)
        _ENERGY_SCREENER_AVAILABLE = True
        log.info("energy_screener imported successfully")
    else:
        log.warning("energy_screener.py not found — energy tickers will be skipped")
except Exception as _ee:
    log.warning(f"energy_screener import failed: {_ee} — energy tickers will be skipped")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Watchlist pulls are small (10-20 tickers) — conservative settings
FETCH_WORKERS    = 8    # Increased from 5 — handles expanded candidate_pool ticker set
                        # safely within Yahoo Finance rate limits at this concurrency
FETCH_CHUNK      = 30   # Updated to reflect larger expected ticker set
FETCH_COOLDOWN   = 15
OVERLAY_TIME_CAP = 120

# High-score thresholds per pipeline (triggers analyst disparity flag)
HIGH_SCORE_GROWTH  = 40   # /54
HIGH_SCORE_ENERGY  = 28   # /36 (~78% — proportional to growth threshold)

STRONG_RATINGS = {"strongbuy", "strong buy", "buy"}


# ---------------------------------------------------------------------------
# Ticker data fetch — full financial data (used for growth stock + energy)
# ---------------------------------------------------------------------------
def _fetch_all_for_ticker(ticker_sym: str) -> tuple[str, dict, str | None]:
    """
    Fetch info + annual statements + quarterly + scoring data + history for one ticker.
    Returns (ticker_sym, data_dict, error_str | None).
    """
    try:
        import yfinance as yf

        tk   = yf.Ticker(ticker_sym)
        info = tk.info or {}
        if not info or len(info) < 5:
            return ticker_sym, {}, "empty_info"

        data = {
            "info":                    info,
            "income_stmt":             tk.income_stmt,
            "cashflow":                tk.cashflow,
            "balance_sheet":           tk.balance_sheet,
            "quarterly_income_stmt":   tk.quarterly_income_stmt,
            "quarterly_cashflow":      tk.quarterly_cashflow,
            "quarterly_balance_sheet": tk.quarterly_balance_sheet,
            "earnings_estimate":       tk.earnings_estimate,
            "growth_estimates":        tk.growth_estimates,
            "analyst_price_targets":   tk.analyst_price_targets,
            "eps_revisions":           tk.eps_revisions,
            "eps_trend":               tk.eps_trend,
            "upgrades_downgrades":     tk.upgrades_downgrades,
            "recommendations_summary": tk.recommendations_summary,
        }

        # Price history (5yr for overlays)
        try:
            data["history"] = tk.history(period="5y")
        except Exception:
            try:
                data["history"] = tk.history(period="2y")
            except Exception:
                data["history"] = None

        # Next earnings date
        try:
            cal = tk.calendar
            if cal is not None and not (isinstance(cal, dict) and not cal):
                if isinstance(cal, dict):
                    dv = cal.get("Earnings Date") or cal.get("earningsDate")
                    if dv:
                        if isinstance(dv, (list, tuple)):
                            dv = dv[0]
                        data["next_earnings"] = str(dv)[:10]
                    else:
                        data["next_earnings"] = "Unknown"
                else:
                    data["next_earnings"] = "Unknown"
            else:
                data["next_earnings"] = "Unknown"
        except Exception:
            try:
                import pandas as pd
                ed = tk.earnings_dates
                if ed is not None and not ed.empty:
                    future = ed[ed.index > pd.Timestamp.now()]
                    data["next_earnings"] = str(future.index[0])[:10] if not future.empty else "Unknown"
                else:
                    data["next_earnings"] = "Unknown"
            except Exception:
                data["next_earnings"] = "Unknown"

        return ticker_sym, data, None

    except Exception as e:
        return ticker_sym, {}, str(e)


def fetch_all_tickers(tickers: list[str]) -> tuple[dict, dict]:
    """
    Fetch all data for a list of tickers concurrently.
    Returns (results: {ticker: data_dict}, errors: {ticker: error_str}).
    """
    results, errors = {}, {}
    log.info(f"Fetching data for {len(tickers)} tickers...")
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_all_for_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            sym, data, err = fut.result()
            if err or not data:
                errors[sym] = err or "no_data"
                log.warning(f"  {sym}: fetch failed — {err}")
            else:
                results[sym] = data
                log.info(f"  {sym}: fetched")
    return results, errors


# ---------------------------------------------------------------------------
# In-window price check
# ---------------------------------------------------------------------------
def check_in_window(ticker_data: dict, entry_level: float | None,
                    entry_currency: str, threshold_pct: float = 10.0) -> dict:
    """
    Check if current price is at or within threshold_pct above entry level.
    Returns dict with: current_price, entry_level, gap_pct, in_window, currency.
    """
    result = {
        "current_price":   None,
        "entry_level":     entry_level,
        "entry_currency":  entry_currency,
        "gap_pct":         None,
        "in_window":       False,
        "pct_above_entry": None,
    }

    if entry_level is None:
        result["in_window_note"] = "no_entry_level"
        return result

    info = ticker_data.get("info", {})
    prices = sc.apply_pence_correction(info)
    current = prices["current_price"]
    result["current_price"] = current

    if current is None or current == 0:
        result["in_window_note"] = "price_unavailable"
        return result

    pct_above = (current - entry_level) / entry_level * 100
    result["pct_above_entry"] = round(pct_above, 2)
    result["gap_pct"] = round(pct_above, 2)
    result["in_window"] = pct_above <= threshold_pct
    result["in_window_note"] = "in_range" if result["in_window"] else "above_entry"
    return result


# ---------------------------------------------------------------------------
# PIPELINE: GROWTH STOCK — score via screener_core
# ---------------------------------------------------------------------------
def score_ticker_growth(ticker_sym: str, data: dict) -> dict:
    """
    Run full Part A + Part B scoring + overlays for a growth stock ticker.
    Uses screener_core.py. Base max score /54; up to /58 for semiconductor_hardware
    and semiconductor_equipment companies where b2b data has been injected into
    info["_book_to_bill_trailing_2q"] and info["_backlog_ttm"] via dispatch_score_ticker().

    b2b data injection happens at the dispatcher level (dispatch_score_ticker) from
    watchlist_tickers.json metadata — not inside this function. This function reads
    whatever is in info at call time; it does not fetch or validate b2b fields.

    Returns a merged scored dict with source_pipeline="growth_stock".
    """
    info  = data.get("info", {})
    inc   = data.get("income_stmt")
    cf    = data.get("cashflow")
    bal   = data.get("balance_sheet")
    inc_q = data.get("quarterly_income_stmt")

    # Part A
    pa = sc.score_part_a(ticker_sym, info, inc, cf, bal, inc_q)

    # Part B
    pb = sc.score_part_b(ticker_sym, info, inc, cf, bal, inc_q)

    # Merge (pb overrides pa on key conflicts — matches screener_core behaviour)
    scored = {**pa, **pb}
    # Forward axis (Part 3 §13) — same shared compute as the weekly screen; passes the forward data explicitly
    sc.compute_forward_axis(scored, {**info, "eps_trend": data.get("eps_trend"), "growth_estimates": data.get("growth_estimates")}, inc_q, data.get("history"))

    # Finalise Part B score
    roic_s = scored.get("score_roic", 0) or 0
    nd_s   = scored.get("score_nd_ebitda", 0) or 0
    ic_s   = scored.get("score_int_cov", 0) or 0
    new_b  = scored.get("_part_b_new_scores_sum", 0) or 0
    part_b = roic_s + nd_s + ic_s + new_b
    scored["part_b_score"] = part_b

    # Part B status
    nd_mand_fail = scored.get("_nd_mand_fail", False)
    if nd_mand_fail or scored.get("final_status") in (
            "HARD_GATE_FAIL", "MANDATORY_MINIMUM_FAIL", "UNRESOLVED_HARD_GATE_NOT_RANKABLE"):
        if nd_mand_fail:
            scored["final_status"] = "MANDATORY_MINIMUM_FAIL"
        scored["part_b_status"] = "Avoid"
    elif part_b >= sc.PART_B_STRONG_THRESHOLD:
        scored["part_b_status"] = "Strong Buy"
    elif part_b >= sc.PART_B_ACCEPTABLE_MIN:
        scored["part_b_status"] = "Fair / Mixed"
    else:
        scored["part_b_status"] = "Avoid"

    scored["total_score"] = (scored.get("part_a_score") or 0) + part_b

    # Next earnings
    scored["next_earnings"] = data.get("next_earnings", scored.get("next_earnings", "Unknown"))

    # Overlays
    geo = sc.geography_group(ticker_sym)
    pa_out = {k: v for k, v in scored.items() if k.startswith("score_") or k == "roic"}
    try:
        ovl = sc.run_overlays(ticker_sym, info, inc, cf, bal, data, pa_out, geo)
        scored.update(ovl)
    except Exception as e:
        log.warning(f"Overlays failed for {ticker_sym}: {e}")
        scored["overlay_status"] = f"error:{e}"

    # Jul-2026: populate the CANONICAL est_rev_direction on the watchlist/held path using the SAME
    # conservative merge as screener_core._score_ticker (line ~3258) — the watchlist path bypasses
    # _score_ticker, so the field was left None. run_overlays already set est_rev_direction_raw.
    # Deteriorating if EITHER source flags it (capital protection); improving only when neither
    # deteriorates and at least one improves. (NOTE: this is data hygiene — it does NOT change the
    # disqualifier gate, whose None-fallback dn>up already equals raw's net<0 threshold.)
    _raw = str(scored.get("est_rev_direction_raw") or "").lower()
    _scb = scored.get("score_b_est_rev")
    if _raw == "deteriorating" or _scb == 0:
        scored["est_rev_direction"] = "deteriorating"
    elif _raw == "improving" or _scb == 2:
        scored["est_rev_direction"] = "improving"
    else:
        scored["est_rev_direction"] = "neutral"

    # Jul-2026 (Raj): recent-return capture for the reversal-vs-12-1m review flag (best-effort).
    try:
        _h = data.get("history"); _cl = None
        if _h is not None:
            _cl = _h["Close"].dropna() if hasattr(_h, "__getitem__") and "Close" in getattr(_h, "columns", []) else None
        if _cl is not None and len(_cl) > 22:
            scored["ret_5d_pct"] = round((float(_cl.iloc[-1]) / float(_cl.iloc[-6]) - 1) * 100, 2)
            scored["ret_1m_pct"] = round((float(_cl.iloc[-1]) / float(_cl.iloc[-22]) - 1) * 100, 2)
    except Exception:
        pass

    # Company metadata
    scored["company"]   = info.get("longName", info.get("shortName", ticker_sym))
    scored["sector"]    = info.get("sector", "")
    scored["industry"]  = info.get("industry", "")
    scored["currency"]  = info.get("currency", "")
    scored["market_cap"] = sc.safe_float(info.get("marketCap"))

    # ── Fix Pack A6/D7 (P2): stamp the UNIFIED FV anatomy on every growth row — the same
    # fv_composite the screen and entry_level_builder use. implied_upside_fv is THE field
    # capital logic reads; the consensus-target gap survives as display_target_gap ONLY.
    # (The raw `target_upside` key set inside the shared scoring fns survives one more cycle
    # for old-file readers — migration note in run_context; removal rides P3 with the proxy path.)
    try:
        _fv = _fvc.fv_composite_for_row(scored)
        scored["implied_upside_fv"]   = _fv["implied_upside_fv"]
        scored["display_target_gap"]  = _fv["display_target_gap"]
        scored["fair_value_composite"] = _fv["fair_value"]
        scored["fv_basis"]            = _fv["fv_basis"]
        scored["fv_conf"]             = _fv["fv_conf"]
        scored["consensus_upside_capped"] = _fv["consensus_upside_capped"]
    except Exception as _e:
        log.warning(f"fv_composite failed for {ticker_sym}: {_e}")
    # Fix Pack A2 (P2): E[r] on the pre-run row (same module as the screen; rerank re-stamps
    # at the live price — this baseline makes the anatomy visible even for un-reranked names).
    try:
        _erd = _erm.expected_return_for_row(scored)
        scored["expected_return_12_24m"] = _erd["expected_return_12_24m"]
        scored["er_confidence"]          = _erd["er_confidence"]
        scored["er_basis"]               = _erd["er_basis"]
    except Exception as _e:
        log.warning(f"expected_return failed for {ticker_sym}: {_e}")

    # Analyst rating (text key — used by normalise_adapter.py analyst summary)
    scored["analyst_rating"] = info.get("recommendationKey", "")

    scored["source_pipeline"] = "growth_stock"
    return scored


# ---------------------------------------------------------------------------
# PIPELINE: ENERGY — score via energy_screener
# ---------------------------------------------------------------------------
def score_ticker_energy(ticker_sym: str, data: dict) -> dict:
    """
    Score an energy ticker using energy_screener.py's pipeline.
    Applies 3 energy gates, then scores Part A (10 metrics, max 20pts)
    and Part B (8 metrics, max 16pts). Total max /36.
    Returns a merged scored dict with source_pipeline="energy".
    If energy_screener is unavailable, returns an error dict.
    """
    if not _ENERGY_SCREENER_AVAILABLE or es is None:
        log.error(f"energy_screener not available — cannot score {ticker_sym}")
        return {
            "source_pipeline":  "energy",
            "error":            "energy_screener_unavailable",
            "part_a_score":     None,
            "part_b_score":     None,
            "total_score":      None,
            "part_a_status":    "Error — energy_screener not available",
            "part_b_status":    "Error",
        }

    info = data.get("info", {})
    inc  = data.get("income_stmt")
    cf   = data.get("cashflow")
    bal  = data.get("balance_sheet")

    # Apply energy gates (watchlist_entry param is present in signature but unused by gates)
    try:
        gate_data = es.apply_energy_gates(ticker_sym, info, inc, {})
    except Exception as e:
        log.warning(f"Energy gates failed for {ticker_sym}: {e}")
        gate_data = {"gate_pass": None, "gate_code": "GATE_ERROR", "gate_reason": str(e)}

    scored = {
        "gate_pass":   gate_data.get("gate_pass"),
        "gate_code":   gate_data.get("gate_code", ""),
        "gate_reason": gate_data.get("gate_reason", ""),
    }

    # Score Part A regardless of gate result (for diagnostic visibility)
    try:
        pa = es.score_part_a(ticker_sym, info, inc, cf, bal, gate_data)
        scored.update(pa)
    except Exception as e:
        log.warning(f"Energy Part A failed for {ticker_sym}: {e}")
        pa = {"part_a_score": 0, "part_a_grade": "Not Growth", "part_a_max": 20}
        scored.update(pa)

    # Score Part B (regardless of gate — provides full picture for Watch/Acceptable names)
    try:
        pb = es.score_part_b(ticker_sym, info, inc, bal, pa)
        scored.update(pb)
    except Exception as e:
        log.warning(f"Energy Part B failed for {ticker_sym}: {e}")
        pb = {"part_b_score": 0, "part_b_grade": "Avoid", "part_b_max": 16}
        scored.update(pb)

    # Total score
    pa_s = scored.get("part_a_score") or 0
    pb_s = scored.get("part_b_score") or 0
    scored["total_score"] = pa_s + pb_s

    # Status strings for display (mirrors part_a_grade / part_b_grade)
    scored["part_a_status"] = scored.get("part_a_grade", "")
    scored["part_b_status"] = scored.get("part_b_grade", "")

    # If gate failed, override statuses for clarity
    if gate_data.get("gate_pass") is False:
        scored["part_b_status"] = f"Gate Fail — {gate_data.get('gate_code', '')}"
    elif gate_data.get("gate_pass") is None:
        scored["part_a_status"] = "Data Unresolved"
        scored["part_b_status"] = "Data Unresolved"

    # Analyst rating text (for consistency with analyst_summary in normalise_adapter.py)
    scored["analyst_rating"] = info.get("recommendationKey", "")

    # P3 (18-Jul-26): raw target_upside alias DELETED (A6 shim retired) — consumers read
    # display_target_gap / implied_upside_fv; energy Part B keeps upside_pct natively.
    scored["display_target_gap"] = scored.get("upside_pct")   # Fix Pack D7 — display-only name

    # Next earnings
    scored["next_earnings"] = data.get("next_earnings", "Unknown")

    # Company metadata
    scored["company"]    = info.get("longName", info.get("shortName", ticker_sym))
    scored["sector"]     = info.get("sector", "")
    scored["industry"]   = info.get("industry", "")
    scored["currency"]   = info.get("currency", "")
    scored["market_cap"] = sc.safe_float(info.get("marketCap"))

    scored["source_pipeline"] = "energy"
    return scored


# ---------------------------------------------------------------------------
# PIPELINE: VCI — pass-through (price only from yfinance)
# ---------------------------------------------------------------------------
def build_vci_entry(ticker_sym: str, data: dict, meta: dict) -> dict:
    """
    Build a scored dict for a VCI ticker.

    VCI tickers are NOT scored via yfinance growth or energy pipelines.
    Their ACS score (0-100) was computed by the VCI pipeline and is stored
    in watchlist_tickers.json. Only current price is extracted from yfinance
    for the in-window check.

    The full VCI scorecard lives in project_vci_output_mmm_yyyy.md — readable
    by Claude at the monthly review. normalise_adapter.py renders it using the VCI
    formatting path.
    """
    info = data.get("info", {})
    prices = sc.apply_pence_correction(info)

    return {
        "source_pipeline":  "vci",
        # ACS score from stored VCI run data
        "acs_score":        meta.get("acs_score"),
        "vci_run_date":     meta.get("vci_run_date"),
        "nvidia_signals":   meta.get("nvidia_signals", ""),
        "classification":   meta.get("classification", ""),
        # Price data (for in-window check and display)
        "current_price":    prices["current_price"],
        # Not applicable for VCI — leave for analyst display in normalise_adapter.py
        "display_target_gap": None,   # Fix Pack D7 (P3: raw target_upside key deleted)
        "analyst_rating":   info.get("recommendationKey", ""),
        "num_analysts":     sc.safe_float(info.get("numberOfAnalystOpinions")),
        "next_earnings":    data.get("next_earnings", "Unknown"),
        # Company metadata
        "company":          info.get("longName", info.get("shortName", ticker_sym)),
        "sector":           info.get("sector", ""),
        "industry":         info.get("industry", ""),
        "currency":         info.get("currency", ""),
        "market_cap":       sc.safe_float(info.get("marketCap")),
        # No Part A/B score — pipeline not applicable
        "part_a_score":     None,
        "part_b_score":     None,
        "total_score":      None,
        "part_a_status":    "VCI pipeline — see VCI output file",
        "part_b_status":    "VCI pipeline — see VCI output file",
    }


# ---------------------------------------------------------------------------
# Dispatcher — routes to the correct scoring function per pipeline
# ---------------------------------------------------------------------------
def dispatch_score_ticker(ticker_sym: str, data: dict, meta: dict) -> dict:
    """
    Route to the appropriate scorer based on source_pipeline in metadata.

    Enhancement 2: For growth_stock pipeline, inject book-to-bill and backlog data
    from watchlist metadata (sourced from earnings call disclosures stored in
    watchlist_tickers.json) into the info dict before scoring. These inject into
    info["_book_to_bill_trailing_2q"] and info["_backlog_ttm"] — the keys that
    screener_core.score_part_b() reads when b2b_applicable=True.

    Injection is safe for all companies: screener_core classifies sector_bucket
    internally and only uses the injected values when b2b_applicable=True.
    """
    pipeline = meta.get("source_pipeline", "growth_stock")

    if pipeline == "energy":
        return score_ticker_energy(ticker_sym, data)
    elif pipeline == "vci":
        return build_vci_entry(ticker_sym, data, meta)
    else:
        # "growth_stock" or any unrecognised value — use growth stock path.
        # Inject b2b data from meta into info before scoring (Enhancement 2).
        # Modifies data["info"] in-place — safe since data is local to this call.
        b2b_val     = meta.get("book_to_bill_trailing_2q")
        backlog_val = meta.get("backlog_ttm")
        if b2b_val is not None or backlog_val is not None:
            info = data.get("info", {})
            if b2b_val is not None:
                info["_book_to_bill_trailing_2q"] = float(b2b_val)
                log.debug(f"  {ticker_sym}: injecting book_to_bill_trailing_2q={b2b_val}")
            if backlog_val is not None:
                info["_backlog_ttm"] = float(backlog_val)
                log.debug(f"  {ticker_sym}: injecting backlog_ttm={backlog_val}")
            data["info"] = info  # ensure the modified info is used
        return score_ticker_growth(ticker_sym, data)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run(watchlist_path: str, out_path: str, month_label: str) -> dict:
    """
    Full fetch + score pipeline for all watchlist, VCI, and stock sleeve tickers.
    Returns the full output dict (also written to out_path).
    """
    # Load watchlist_tickers.json
    if not os.path.exists(watchlist_path):
        raise FileNotFoundError(f"watchlist_tickers.json not found: {watchlist_path}")
    with open(watchlist_path, encoding="utf-8") as f:
        wl_config = json.load(f)

    threshold_pct  = wl_config.get("in_window_threshold_pct", 10.0)
    watchlist      = wl_config.get("watchlist", [])
    vci_watchlist  = wl_config.get("vci_watchlist", [])
    sleeve         = [s for s in wl_config.get("stock_sleeve", [])
                      if s.get("include_in_metrics_pull", True)]
    candidate_pool = wl_config.get("candidate_pool", [])  # NEW

    log.info(f"Config: {len(watchlist)} watchlist | {len(vci_watchlist)} VCI | "
             f"{len(sleeve)} sleeve | {len(candidate_pool)} candidate_pool")

    # ---------------------------------------------------------------------------
    # Build ticker metadata map  {ticker -> metadata dict}
    # Precedence: watchlist < vci_watchlist < stock_sleeve (sleeve entries win on overlap)
    # ---------------------------------------------------------------------------
    ticker_meta: dict[str, dict] = {}

    # Main watchlist (growth_stock or energy)
    for entry in watchlist:
        t = entry["ticker"]
        ticker_meta[t] = {
            "kind":            "watchlist",
            "source_pipeline": entry.get("source_pipeline", "growth_stock"),
            "rank":            entry.get("rank"),
            "name":            entry.get("name", t),
            "exchange":        entry.get("exchange", ""),
            "entry_level":     entry.get("entry_level"),
            "entry_currency":  entry.get("entry_currency", "USD"),
            "sector_hint":     entry.get("sector", ""),
            "status":          entry.get("status", ""),
            "thesis_break":    entry.get("thesis_break_summary", ""),
            # Enhancement 2: book-to-bill and backlog data from earnings call disclosures
            # Only used for semiconductor_equipment / semiconductor_hardware companies.
            # None = not disclosed (produces unresolved/0 score for b2b metrics).
            "book_to_bill_trailing_2q": entry.get("book_to_bill_trailing_2q"),
            "backlog_ttm":              entry.get("backlog_ttm"),
        }

    # VCI watchlist
    for entry in vci_watchlist:
        t = entry["ticker"]
        # Overwrite if already present in main watchlist (VCI takes priority for promoted names)
        ticker_meta[t] = {
            "kind":            "vci_watchlist",
            "source_pipeline": "vci",
            "rank":            entry.get("rank"),
            "name":            entry.get("name", t),
            "exchange":        entry.get("exchange", ""),
            "entry_level":     entry.get("entry_level"),
            "entry_currency":  entry.get("entry_currency", "USD"),
            "sector_hint":     entry.get("sector", ""),
            "status":          entry.get("status", ""),
            "thesis_break":    entry.get("thesis_break_summary", ""),
            # VCI-specific fields stored in JSON entry
            "acs_score":       entry.get("acs_score"),
            "vci_run_date":    entry.get("vci_run_date"),
            "nvidia_signals":  entry.get("nvidia_signals", ""),
            "classification":  entry.get("classification", ""),
        }

    # Stock sleeve
    for s in sleeve:
        t = s["ticker"]
        existing = ticker_meta.get(t, {})
        # Stock sleeve adds purchase fields; preserves existing watchlist fields if present
        existing.update({
            "kind":            "stock_sleeve",
            "source_pipeline": s.get("source_pipeline",
                                     existing.get("source_pipeline", "growth_stock")),
            "name":            s.get("name", t),
            "exchange":        s.get("exchange", existing.get("exchange", "")),
            "purchase_date":   s.get("purchase_date"),
            "cost_per_share":  s.get("cost_per_share_usd"),
            "shares":          s.get("shares"),
            "note":            s.get("note", ""),
            # Preserve watchlist entry_level if already set
            "entry_level":     existing.get("entry_level"),
            "entry_currency":  existing.get("entry_currency", "USD"),
        })
        ticker_meta[t] = existing

    # Candidate pool (newly eligible names beyond top-10)
    # These entries are scored with the same pipeline as their path indicates.
    # kind = "candidate_pool" so normalise_adapter.py excludes them from s5_watchlist_rows
    # and conviction_ranking, but step9_pre_builder.py processes them separately.
    # Precedence rule: if a candidate_pool ticker is already in watchlist/vci/sleeve,
    # the existing entry takes priority (sleeve wins, then watchlist, then vci).
    for entry in candidate_pool:
        t = entry.get("ticker")
        if not t:
            continue
        if t in ticker_meta:
            # Already registered from watchlist, vci_watchlist, or stock_sleeve — skip.
            # The more specific classification takes priority.
            log.debug(f"  Candidate pool ticker {t} already registered as "
                      f"{ticker_meta[t].get('kind')} — skipping duplicate")
            continue
        ticker_meta[t] = {
            "kind":            "candidate_pool",
            "source_pipeline": entry.get("source_pipeline", "growth_stock"),
            "rank":            None,          # no rank assigned yet
            "name":            entry.get("name", t),
            "exchange":        entry.get("exchange", ""),
            "entry_level":     entry.get("entry_level"),
            "entry_currency":  entry.get("entry_currency", "USD"),
            "sector_hint":     entry.get("sector", ""),
            "status":          "candidate_pool",
            "thesis_break":    "",
            # Pass normalised_score through so step9_pre_builder can sort the pool
            "normalised_score": entry.get("normalised_score"),
            # Enhancement 2: b2b fields for candidate pool entries
            "book_to_bill_trailing_2q": entry.get("book_to_bill_trailing_2q"),
            "backlog_ttm":              entry.get("backlog_ttm"),
        }

    all_tickers = list(ticker_meta.keys())
    log.info(f"Tickers to fetch: {all_tickers}")
    if not all_tickers:
        log.warning("No tickers found in watchlist_tickers.json")
        return {}

    # Validate energy screener availability for any energy tickers
    energy_tickers = [t for t, m in ticker_meta.items() if m.get("source_pipeline") == "energy"]
    if energy_tickers and not _ENERGY_SCREENER_AVAILABLE:
        log.warning(
            f"energy_screener unavailable — {len(energy_tickers)} energy ticker(s) will score as errors: "
            f"{energy_tickers}"
        )

    # ---------------------------------------------------------------------------
    # Fetch all data (full yfinance pull for all tickers including VCI)
    # VCI tickers only use info.currentPrice — the full fetch provides this
    # ---------------------------------------------------------------------------
    raw_data, fetch_errors = fetch_all_tickers(all_tickers)

    # ---------------------------------------------------------------------------
    # Score / build each ticker
    # ---------------------------------------------------------------------------
    scored_results: dict[str, dict] = {}
    scoring_errors: dict[str, str] = {}

    for ticker in all_tickers:
        data = raw_data.get(ticker)
        if data is None:
            scoring_errors[ticker] = fetch_errors.get(ticker, "fetch_not_available")
            log.warning(f"Skipping {ticker} — data unavailable ({scoring_errors[ticker]})")
            continue

        meta     = ticker_meta[ticker]
        pipeline = meta.get("source_pipeline", "growth_stock")

        try:
            scored = dispatch_score_ticker(ticker, data, meta)

            # Enrich with watchlist metadata (prefixed _ to distinguish from scored fields)
            scored["_kind"]            = meta.get("kind", "unknown")
            scored["_source_pipeline"] = pipeline
            scored["_rank"]            = meta.get("rank")
            scored["_entry_level"]     = meta.get("entry_level")
            scored["_entry_currency"]  = meta.get("entry_currency", "USD")
            scored["_status"]          = meta.get("status", "")
            scored["_thesis_break"]    = meta.get("thesis_break", "")
            scored["_purchase_date"]   = meta.get("purchase_date")
            scored["_cost_per_share"]  = meta.get("cost_per_share")
            scored["_shares"]          = meta.get("shares")
            scored["_note"]            = meta.get("note", "")

            # VCI-specific metadata (also stored as _ fields for normalise_adapter.py)
            if pipeline == "vci":
                scored["_acs_score"]      = meta.get("acs_score")
                scored["_vci_run_date"]   = meta.get("vci_run_date")
                scored["_nvidia_signals"] = meta.get("nvidia_signals", "")
                scored["_classification"] = meta.get("classification", "")

            # In-window check (works for all pipelines — uses current_price vs entry_level)
            window = check_in_window(
                data, meta.get("entry_level"), meta.get("entry_currency", "USD"), threshold_pct
            )
            # Override current_price in scored dict if window check produced a value
            # (in case the scoring function returned None for current_price)
            if scored.get("current_price") is None and window.get("current_price") is not None:
                scored["current_price"] = window["current_price"]
            scored["_in_window"]       = window["in_window"]
            scored["_gap_pct"]         = window.get("gap_pct")
            scored["_pct_above_entry"] = window.get("pct_above_entry")
            scored["_in_window_note"]  = window.get("in_window_note", "")
            scored["ticker"] = ticker
            # Volatility / ATR technical-anchor inputs (history already pulled at fetch;
            # zero extra network calls). Consumed by entry_level_builder.py (Step 7.25).
            try:
                from enrich_volatility import compute_volatility_metrics
                _vm = compute_volatility_metrics(data.get("history"), scored.get("current_price"))
                scored["_realised_vol"] = _vm["realised_vol"]
                scored["_atr_pct"]      = _vm["atr_pct"]
                scored["_vol_profile"]  = _vm["vol_profile"]
                scored["_vol_source"]   = _vm["vol_source"]
            except Exception:
                scored["_vol_profile"] = "unknown"
            scored_results[ticker] = scored

            # Log summary line per pipeline
            if pipeline == "energy":
                log.info(
                    f"  {ticker} [ENERGY]: A={scored.get('part_a_score','?')}/20 "
                    f"B={scored.get('part_b_score','?')}/16 "
                    f"Total={scored.get('total_score','?')}/36 "
                    f"Gate={scored.get('gate_code','?')} "
                    f"In-window={scored.get('_in_window','?')}"
                )
            elif pipeline == "vci":
                log.info(
                    f"  {ticker} [VCI]: ACS={scored.get('acs_score','?')}/100 "
                    f"Classification={scored.get('classification','?')} "
                    f"In-window={scored.get('_in_window','?')}"
                )
            else:
                _bucket = scored.get("sector_bucket", "?")
                _b2b_a  = scored.get("b2b_applicable", False)
                _b2b_s  = f" B2B={scored.get('book_to_bill_trailing_2q','?')}x" if _b2b_a else ""
                _max    = 58 if (_b2b_a and (scored.get("score_b_book_to_bill") not in (None, 0)
                                             or scored.get("score_b_backlog_ev") not in (None, 0))) else 54
                log.info(
                    f"  {ticker} [GROWTH]: A={scored.get('part_a_score','?')} "
                    f"B={scored.get('part_b_score','?')} "
                    f"Total={scored.get('total_score','?')}/{_max} "
                    f"Bucket={_bucket}{_b2b_s} "
                    f"In-window={scored.get('_in_window','?')}"
                )

        except Exception as e:
            scoring_errors[ticker] = f"scoring_exception:{e}"
            log.warning(f"Scoring failed for {ticker} [{pipeline}]: {e}")

    # ---------------------------------------------------------------------------
    # Summary statistics (pipeline-segmented)
    # ---------------------------------------------------------------------------
    in_window_all = [t for t, s in scored_results.items() if s.get("_in_window")]

    # Growth stock high-score (>=40/54)
    high_score_growth = [
        t for t, s in scored_results.items()
        if s.get("_source_pipeline", "growth_stock") == "growth_stock"
        and (s.get("total_score") or 0) >= 40
    ]
    # Energy high-score (>=28/36 — ~78% proportional to growth threshold)
    high_score_energy = [
        t for t, s in scored_results.items()
        if s.get("_source_pipeline") == "energy"
        and (s.get("total_score") or 0) >= HIGH_SCORE_ENERGY
    ]
    # VCI deployment-ready (ACS >=75)
    high_conviction_vci = [
        t for t, s in scored_results.items()
        if s.get("_source_pipeline") == "vci"
        and (s.get("acs_score") or s.get("_acs_score") or 0) >= 75
    ]

    # ---------------------------------------------------------------------------
    # Build output
    # ---------------------------------------------------------------------------
    output = {
        "_meta": {
            "month_label":               month_label,
            "produced_at":               datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tickers_requested":         all_tickers,
            "tickers_scored":            list(scored_results.keys()),
            "tickers_failed":            list(scoring_errors.keys()),
            "fetch_errors":              fetch_errors,
            "scoring_errors":            scoring_errors,
            "in_window_threshold_pct":   threshold_pct,
            "vci_count":                 len(vci_watchlist),
            "sleeve_count":              len(sleeve),
            "in_window_tickers":         in_window_all,
            "high_score_tickers":        high_score_growth,   # growth stock >=40/54
            "high_score_energy":         high_score_energy,   # energy >=28/36
            "high_conviction_vci":       high_conviction_vci, # VCI ACS >=75
            "energy_screener_available": _ENERGY_SCREENER_AVAILABLE,
            "candidate_pool_count": len([t for t, m in ticker_meta.items()
                                         if m.get("kind") == "candidate_pool"]),
            "pipeline_counts": {
                "growth_stock": len([t for t, m in ticker_meta.items()
                                     if m.get("source_pipeline", "growth_stock") == "growth_stock"]),
                "energy":       len([t for t, m in ticker_meta.items()
                                     if m.get("source_pipeline") == "energy"]),
                "vci":          len([t for t, m in ticker_meta.items()
                                     if m.get("source_pipeline") == "vci"]),
            },
            # Enhancement 2: b2b data injection summary
            "b2b_data_injected": {
                "tickers_with_b2b": [
                    t for t, m in ticker_meta.items()
                    if m.get("book_to_bill_trailing_2q") is not None
                    or m.get("backlog_ttm") is not None
                ],
                "b2b_scored": [
                    t for t, s in scored_results.items()
                    if s.get("b2b_applicable") and s.get("score_b_book_to_bill") not in (None, 0)
                ],
                "backlog_ev_scored": [
                    t for t, s in scored_results.items()
                    if s.get("b2b_applicable") and s.get("score_b_backlog_ev") not in (None, 0)
                ],
                "b2b_applicable_unscored": [
                    t for t, s in scored_results.items()
                    if s.get("b2b_applicable")
                    and s.get("book_to_bill_status") == "unresolved"
                    and s.get("backlog_ev_status") == "unresolved"
                ],
            },
        },
        "tickers": scored_results,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    log.info(f"\nWatchlist metrics written: {out_path}")
    log.info(f"  Scored: {len(scored_results)} | Failed: {len(scoring_errors)}")
    log.info(f"  In-window: {in_window_all}")
    log.info(f"  High score growth (>=40/54): {high_score_growth}")
    log.info(f"  High score energy (>=28/36): {high_score_energy}")
    log.info(f"  VCI deployment-ready (ACS>=75): {high_conviction_vci}")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch and score Part A/B metrics for all watchlist, VCI, and stock sleeve tickers."
    )
    parser.add_argument("--watchlist", default=None,
                        help="Path to watchlist_tickers.json. Defaults to same folder as this script.")
    parser.add_argument("--out", default=None,
                        help="Output JSON path. Defaults to watchlist_metrics_mmm_yyyy.json.")
    parser.add_argument("--month-label", default=None,
                        help="Month label e.g. jun_2026. Defaults to current month.")
    parser.add_argument("--preflight", action="store_true",
                        help="Local-primary preflight (yfinance/dev-shm/Yahoo). On failure prints "
                             "FALLBACK_TO_COMPOSIO and exits 3. Default off = pre-run unchanged.")
    args = parser.parse_args()

    watchlist_path = args.watchlist or os.path.join(SCRIPT_DIR, "watchlist_tickers.json")
    month_label    = args.month_label or date.today().strftime("%b_%Y").lower()
    out_path       = args.out or os.path.join(SCRIPT_DIR, f"watchlist_metrics_{month_label}.json")

    # Local-primary guardrail parity (opt-in). Fails over to Composio (exit 3) when the local
    # sandbox can't fetch — the same decision screener_local makes for the growth path.
    if getattr(args, "preflight", False):
        try:
            import isa_env_guard as _guard
            _guard.run_preflight_or_fallback(outputs_dir=os.path.dirname(out_path))
        except SystemExit:
            raise
        except Exception:
            pass

    run(watchlist_path, out_path, month_label)


if __name__ == "__main__":
    main()
