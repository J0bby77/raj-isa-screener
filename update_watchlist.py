#!/usr/bin/env python3
"""
update_watchlist.py  --  Watchlist Promotion, Score Refresh, and Ranking
Version: 1.0  |  2026-06-02

Runs as Step 4 of the monthly_isa_prerun.py pipeline (after portfolio_analytics.py,
before fetch_watchlist_metrics.py).

Purpose:
  Mechanically apply the Step 3 Watchlist Promotion Framework to produce an updated
  watchlist_tickers.json before the metrics fetch runs, so newly promoted stocks are
  in-window-checked in the same monthly cycle they are identified.

Inputs:
  --portfolio-data   portfolio_data_mmm_yyyy.json  (for portfolio duplicate check)
  --watchlist-json   watchlist_tickers.json         (current watchlist to update)
  --inv-dir          Investment Analysis folder path  (to locate growth stock xlsx)
  --out-json         watchlist_tickers.json          (writes back in place)

Outputs:
  - Updated watchlist_tickers.json (in place)
  - watchlist_promotion_log dict printed as JSON on stdout (embedded in run_context by orchestrator)
  - Deletes all Growth Stock Analysis*.xlsx files after processing

Phase logic:
  Phase 1 — Path classification (energy vs growth stock)
  Phase 2 — Hard exclusions (portfolio duplicates, unchanged watchlist dups, China VIE)
  Phase 3 — Candidate pool assembly (deduplicate by ticker, keep highest score)
  Phase 4 — Existing watchlist score refresh (delta_score, part_b_driver)
  Phase 5 — Provisional entry levels for new Tier 1 candidates
  Phase 6 — Combined ranking and top-10 selection
  Phase 7 — Write outputs and delete xlsx files
"""

import argparse
import glob
import json
import math
import os
import shutil
import sys
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max watchlist size
MAX_WATCHLIST = 10

# Score history: keep at most this many months
MAX_SCORE_HISTORY = 3

# China VIE hard exclusion list
CHINA_VIE_EXCLUSIONS = {"BZ", "PDD", "NTES", "ATAT", "BEKE", "DIDI", "TUYA", "LAIX"}

# Path A scorecard max (Growth_Stock_Checklist.pdf: 14+13 metrics, 2pts each)
PATH_A_MAX = 54

# Path C scorecard max (energy_screener.py: 10+8 metrics, 2pts each)
PATH_C_MAX = 36

# Score gate thresholds
NORMALISED_SCORE_PROMOTION_GATE = 70.0   # new: was effectively ~50%
NORMALISED_SCORE_PROBATION_FLOOR = 60.0  # existing names 60-69 = probation
NORMALISED_SCORE_HARD_REMOVE_BELOW = 60.0  # existing names <60 = remove unless override

# Archive (replaces delete)
XLSX_ARCHIVE_SUBDIR = "archive"  # created inside inv_dir if not present


# ---------------------------------------------------------------------------
# SUMMARY tab reader
# ---------------------------------------------------------------------------

def read_xlsx_summary_tab(xlsx_path: str) -> list[dict]:
    """
    Read the SUMMARY tab from a growth stock analysis xlsx file.
    Returns a list of row dicts. Tolerates missing columns gracefully.
    Requires openpyxl.
    """
    try:
        import openpyxl
    except ImportError:
        print("  [update_watchlist] openpyxl not installed — attempting install...")
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "openpyxl", "--break-system-packages", "-q"],
            capture_output=True
        )
        import openpyxl

    rows = []
    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    except Exception as e:
        print(f"  [update_watchlist] Cannot open {os.path.basename(xlsx_path)}: {e}")
        return rows

    # Find SUMMARY sheet (case-insensitive)
    sheet = None
    for name in wb.sheetnames:
        if name.strip().upper() == "SUMMARY":
            sheet = wb[name]
            break

    if sheet is None:
        print(f"  [update_watchlist] No SUMMARY tab in {os.path.basename(xlsx_path)} — skipped")
        wb.close()
        return rows

    # Read header row (row 1)
    headers = []
    for cell in next(sheet.iter_rows(min_row=1, max_row=1)):
        val = cell.value
        headers.append(str(val).strip() if val is not None else "")

    def col(row_vals: list, *names: str):
        """Return first matching column value, case-insensitive."""
        for name in names:
            for i, h in enumerate(headers):
                if h.lower() == name.lower() and i < len(row_vals):
                    return row_vals[i]
        return None

    # Read data rows
    for row in sheet.iter_rows(min_row=2, values_only=True):
        row_vals = list(row)
        if not any(v for v in row_vals):
            continue  # skip blank rows

        ticker = col(row_vals, "Ticker", "Symbol", "Stock")
        if not ticker or str(ticker).strip() == "":
            continue
        ticker = str(ticker).strip().upper()

        company = col(row_vals, "Company", "Name", "Company Name")
        sector  = col(row_vals, "Sector", "Industry", "GICS Sector")
        exchange = col(row_vals, "Exchange", "Market")

        # Scores
        def safe_int(v):
            try:
                return int(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None

        def safe_float(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        part_a = safe_int(col(row_vals, "Part A", "Part A Score", "Part A Total"))
        part_b = safe_int(col(row_vals, "Part B", "Part B Score", "Part B Total"))
        total  = safe_int(col(row_vals, "Total", "Total Score", "Combined Score"))

        # Derive total if missing
        if total is None and part_a is not None and part_b is not None:
            total = part_a + part_b

        # Consensus target / current price (for provisional entry level)
        target_price   = safe_float(col(row_vals, "Target Price", "Consensus Target", "Price Target", "Fair Value"))
        current_price  = safe_float(col(row_vals, "Price", "Current Price", "Last Price"))
        currency       = col(row_vals, "Currency") or "USD"

        rows.append({
            "ticker":       ticker,
            "company":      str(company).strip() if company else ticker,
            "sector":       str(sector).strip() if sector else "",
            "exchange":     str(exchange).strip() if exchange else "NASDAQ",
            "currency":     str(currency).strip() if currency else "USD",
            "part_a":       part_a,
            "part_b":       part_b,
            "total":        total,
            "target_price": target_price,
            "current_price": current_price,
            "source_file":  os.path.basename(xlsx_path),
        })

    wb.close()
    return rows


# ---------------------------------------------------------------------------
# Phase 1: Path classification
# ---------------------------------------------------------------------------

def classify_path(row: dict, filename: str) -> str | None:
    """
    Returns 'A', 'C', or None (misrouted energy — exclude from promotion).
    'C' for energy/clean-tech/renewables; 'A' for everything else.
    Misrouted energy in a non-ENERGY file returns None (log and exclude).
    """
    fname_upper = filename.upper()
    is_energy_file = "ENERGY" in fname_upper

    sector = (row.get("sector") or "").lower()
    energy_sectors = {
        "energy", "utilities", "oil", "gas", "renewables", "clean-tech",
        "cleantech", "nuclear", "power generation", "upstream", "midstream",
        "downstream", "coal", "lng", "solar", "wind", "hydro",
    }
    is_energy_sector = any(kw in sector for kw in energy_sectors)

    if is_energy_file:
        return "C"
    elif is_energy_sector:
        # Misrouted — energy stock in a non-energy file
        return None  # caller logs as path_c_misrouted
    else:
        return "A"


# ---------------------------------------------------------------------------
# Phase 4: Part B driver classification
# ---------------------------------------------------------------------------

VALUATION_METRICS_PART_B_INDICES = {
    # Metrics 4-8 in Part B are typically valuation metrics:
    # fwd P/E, EV/EBITDA, P/FCF, FCF Yield, Earnings Yield, 52wk position
    # ROIC (index 0), ND/EBITDA (index 1), Int Coverage (index 2) = quality metrics
    "valuation_indices": {3, 4, 5, 6, 7, 8},  # fwd metrics, 52wk range
    "quality_indices":   {0, 1, 2},             # ROIC, ND/EBITDA, Int Coverage
}


def classify_part_b_driver(prior_total: int, new_total: int,
                            prior_part_a: int | None, new_part_a: int | None,
                            prior_part_b: int | None, new_part_b: int | None) -> str | None:
    """
    Classify what drove a score change.
    Returns one of: 'valuation_led', 'quality_concern', 'broad_deterioration',
                    'broad_improvement', 'valuation_improvement', None (no material change).
    Uses Part A/B delta to attribute: Part A = quality metrics; Part B = valuation/resilience.
    This is a heuristic from SUMMARY tab data; score_partab.py provides metric-level detail.
    """
    if new_total is None or prior_total is None:
        return None

    delta = new_total - prior_total
    if abs(delta) < 2:
        return None  # immaterial change — no attribution needed

    part_a_delta = (new_part_a - prior_part_a) if (new_part_a is not None and prior_part_a is not None) else 0
    part_b_delta = (new_part_b - prior_part_b) if (new_part_b is not None and prior_part_b is not None) else 0

    if delta < 0:
        # Score declined
        if part_b_delta < -1 and part_a_delta >= -1:
            return "valuation_led"        # Part B down, Part A stable → valuation repricing
        elif part_a_delta < -1 and part_b_delta >= -1:
            return "quality_concern"      # Part A down, Part B stable → quality erosion
        elif part_a_delta < -1 and part_b_delta < -1:
            return "broad_deterioration"  # Both down → systemic decline
        else:
            return "valuation_led"        # default for decline
    else:
        # Score improved
        if part_a_delta > 1 and part_b_delta <= 1:
            return "quality_improvement"   # Part A up → quality improving
        elif part_b_delta > 1 and part_a_delta <= 1:
            return "valuation_improvement" # Part B up → valuation more attractive
        else:
            return "broad_improvement"    # Both up


# ---------------------------------------------------------------------------
# Normalised score
# ---------------------------------------------------------------------------

def normalised_score(total: int | None, path: str) -> float | None:
    if total is None:
        return None
    max_score = PATH_A_MAX if path == "A" else PATH_C_MAX
    return round(total / max_score * 100, 1)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(portfolio_path: str, watchlist_path: str, inv_dir: str, out_path: str,
        dry_run: bool = False) -> dict:
    """
    Execute the 7-phase watchlist update. Returns the watchlist_promotion_log dict.
    """
    run_date = date.today().strftime("%Y-%m-%d")
    month_label = date.today().strftime("%b-%Y")

    # Load watchlist_tickers.json
    with open(watchlist_path, encoding="utf-8") as f:
        wl_data = json.load(f)

    # Load portfolio_data for duplicate check
    portfolio_tickers = set()
    if portfolio_path and os.path.exists(portfolio_path):
        try:
            with open(portfolio_path, encoding="utf-8") as f:
                port = json.load(f)
            for s in port.get("stocks", []):
                t = s.get("ticker", "")
                if t:
                    portfolio_tickers.add(t.upper())
            # Also from watchlist stock_sleeve
        except Exception as e:
            print(f"  [update_watchlist] Warning: could not read portfolio_data: {e}")

    # Also add stock_sleeve tickers from watchlist_tickers.json itself
    for s in wl_data.get("stock_sleeve", []):
        t = s.get("ticker", "")
        if t:
            portfolio_tickers.add(t.upper())

    # Current watchlist entries (dict by ticker for easy lookup)
    current_watchlist = {e["ticker"].upper(): e for e in wl_data.get("watchlist", [])}

    promotion_log = {
        "run_date":             run_date,
        "additions":            [],
        "removals":             [],
        "score_updates":        [],
        "misrouted_energy":     [],
        "stale_scores":         [],
        "rejected_candidates":  [],   # names failing the 70% gate
        "duplicate_ticker_log": [],   # cross-file duplicate appearances
        "xlsx_files_deleted":   [],   # kept for backward compatibility — always empty after archiving
        "xlsx_files_archived":  [],   # replaces xlsx_files_deleted
        "xlsx_files_read":      [],
        "path_c_misrouted_log": [],
    }

    # -----------------------------------------------------------------------
    # Find all Growth Stock Analysis xlsx files
    # -----------------------------------------------------------------------
    xlsx_pattern = os.path.join(inv_dir, "Growth Stock Analysis*.xlsx")
    xlsx_files = sorted(glob.glob(xlsx_pattern))

    if not xlsx_files:
        print("  [update_watchlist] No Growth Stock Analysis xlsx files found — nothing to promote.")

    # -----------------------------------------------------------------------
    # Phase 1 & 2 & 3: Read files, classify paths, apply exclusions
    # -----------------------------------------------------------------------
    candidate_pool: dict[str, dict] = {}  # ticker → best row

    for xlsx_path in xlsx_files:
        fname = os.path.basename(xlsx_path)
        print(f"  [update_watchlist] Reading: {fname}")
        promotion_log["xlsx_files_read"].append(fname)
        rows = read_xlsx_summary_tab(xlsx_path)

        for row in rows:
            ticker = row["ticker"]

            # Phase 1: classify path
            path = classify_path(row, fname)
            if path is None:
                # Misrouted energy stock
                promotion_log["misrouted_energy"].append({
                    "ticker": ticker,
                    "file":   fname,
                    "note":   "Energy sector in non-ENERGY file — scored on Path A; advisory only",
                })
                promotion_log["path_c_misrouted_log"].append(ticker)
                continue

            row["path"] = path

            # Phase 2: hard exclusions
            if ticker in portfolio_tickers:
                continue  # already held

            if ticker in CHINA_VIE_EXCLUSIONS:
                continue  # China VIE

            total = row.get("total")
            if total is None:
                continue  # no score — can't rank

            # Watchlist duplicate check: only exclude if not a material score improvement
            if ticker in current_watchlist:
                prior_total = current_watchlist[ticker].get("total") or 0
                # Allow into candidate pool — Phase 4 will handle score refresh
                # (existing names always refreshed if they appear in SUMMARY)

            # Phase 3: deduplicate — keep highest normalised score per ticker
            # Normalised score comparison handles cross-path duplicates correctly
            # (a Path A score of 40/54 vs Path C score of 28/36 both normalise to ~74%)
            new_ns = normalised_score(total, row.get("path", "A")) or 0.0
            existing_ns = (
                normalised_score(
                    candidate_pool[ticker].get("total", 0),
                    candidate_pool[ticker].get("path", "A")
                ) or 0.0
            ) if ticker in candidate_pool else 0.0

            if ticker not in candidate_pool or new_ns > existing_ns:
                # If duplicate appeared, log it
                if ticker in candidate_pool and new_ns != existing_ns:
                    promotion_log.setdefault("duplicate_ticker_log", []).append({
                        "ticker": ticker,
                        "kept_file": row.get("source_file", ""),
                        "kept_score": new_ns,
                        "replaced_score": existing_ns,
                    })
                candidate_pool[ticker] = row

    # -----------------------------------------------------------------------
    # Phase 4: Existing watchlist score refresh
    # -----------------------------------------------------------------------
    updated_existing: dict[str, dict] = {}

    for ticker, entry in current_watchlist.items():
        override_reason = entry.get("eligibility_override_reason")  # None or string

        if ticker in candidate_pool:
            # This month's SUMMARY has a score for this ticker
            new_row   = candidate_pool[ticker]
            new_total = new_row.get("total")
            new_a     = new_row.get("part_a")
            new_b     = new_row.get("part_b")
            new_path  = new_row.get("path", entry.get("path", "A"))

            prior_total = entry.get("total")
            prior_a     = entry.get("part_a")
            prior_b     = entry.get("part_b")

            # Push current score to history
            history = list(entry.get("score_history", []))
            if prior_total is not None:
                if not history or history[0].get("month") != month_label:
                    history.insert(0, {
                        "month":      month_label,
                        "part_a":     prior_a,
                        "part_b":     prior_b,
                        "total":      prior_total,
                        "normalised": entry.get("normalised_score"),
                    })
                    history = history[:MAX_SCORE_HISTORY]

            delta = (new_total - prior_total) if (new_total is not None and prior_total is not None) else None
            # Updated call: pass part_a deltas for better attribution
            pb_driver = classify_part_b_driver(
                prior_total, new_total, prior_a, new_a, prior_b, new_b
            )
            ns = normalised_score(new_total, new_path)

            # --- Probation / removal logic for existing names ---
            probation_flag = False
            removal_flag   = False
            if ns is not None:
                if ns < NORMALISED_SCORE_HARD_REMOVE_BELOW and not override_reason:
                    removal_flag = True
                elif ns < NORMALISED_SCORE_PROMOTION_GATE and not override_reason:
                    probation_flag = True
            # (_removal_flag entries are excluded in Phase 6 ranking)

            updated_entry = dict(entry)
            updated_entry.update({
                "path":             new_path,
                "total":            new_total,
                "part_a":           new_a,
                "part_b":           new_b,
                "normalised_score": ns,
                "score_history":    history,
                "delta_score":      delta,
                "probation_flag":   probation_flag,
                "_removal_flag":    removal_flag,  # underscore prefix = internal, not persisted to JSON
            })
            if pb_driver:
                updated_entry["part_b_driver"] = pb_driver
            elif "part_b_driver" in updated_entry:
                del updated_entry["part_b_driver"]

            updated_existing[ticker] = updated_entry

            if delta is not None:
                promotion_log["score_updates"].append({
                    "ticker":       ticker,
                    "delta_score":  delta,
                    "part_b_driver": pb_driver,
                    "probation":    probation_flag,
                    "removed":      removal_flag,
                })
        else:
            # Not in this month's SUMMARY tabs — carry forward
            updated_entry = dict(entry)
            history = list(entry.get("score_history", []))
            stale = False
            if history and len(history) >= MAX_SCORE_HISTORY:
                stale = True
                updated_entry["stale_score_flag"] = True
                promotion_log["stale_scores"].append({
                    "ticker":       ticker,
                    "last_screened": history[0].get("month", "unknown"),
                })
            # Existing names absent from SUMMARY are not removed automatically.
            # They carry forward at their last known score unless stale + below floor.
            ns = updated_entry.get("normalised_score")
            removal_flag = False
            probation_flag = False
            if stale and ns is not None and ns < NORMALISED_SCORE_HARD_REMOVE_BELOW and not override_reason:
                removal_flag = True
            elif ns is not None and ns < NORMALISED_SCORE_PROMOTION_GATE and not override_reason:
                probation_flag = True
            updated_entry["probation_flag"]  = probation_flag
            updated_entry["_removal_flag"]   = removal_flag
            updated_existing[ticker] = updated_entry

    # -----------------------------------------------------------------------
    # Phase 5: Provisional entry levels for new candidates
    # -----------------------------------------------------------------------
    new_candidates: dict[str, dict] = {}

    for ticker, row in candidate_pool.items():
        if ticker in current_watchlist:
            continue  # already handled in Phase 4

        path  = row.get("path", "A")
        total = row.get("total", 0)
        ns    = normalised_score(total, path) or 0.0
        override = row.get("eligibility_override_reason")

        # GATE: normalised_score >= 70 for automatic promotion
        # Below 70: reject unless explicit override_reason set in candidate data
        if ns < NORMALISED_SCORE_PROMOTION_GATE and not override:
            promotion_log["rejected_candidates"].append({
                "ticker":           ticker,
                "normalised_score": ns,
                "path":             path,
                "source_file":      row.get("source_file", ""),
                "reason":           f"normalised_score {ns:.1f} < {NORMALISED_SCORE_PROMOTION_GATE} threshold",
            })
            continue

        # Provisional entry level
        target  = row.get("target_price")
        current = row.get("current_price")
        if target and target > 0:
            entry_level = round(target * 0.75, 2)
        elif current and current > 0:
            entry_level = round(current * 0.88, 2)
        else:
            entry_level = None

        new_candidates[ticker] = {
            "ticker":                  ticker,
            "name":                    row.get("company", ticker),
            "exchange":                row.get("exchange", "NASDAQ"),
            "entry_level":             entry_level,
            "entry_currency":          row.get("currency", "USD"),
            "entry_level_provisional": True,
            "sector":                  row.get("sector", ""),
            "source_pipeline":         "growth_stock" if path == "A" else "energy",
            "path":                    path,
            "total":                   total,
            "part_a":                  row.get("part_a"),
            "part_b":                  row.get("part_b"),
            "normalised_score":        ns,
            "delta_score":             None,
            "score_history":           [],
            "status":                  "Watchlist — newly promoted",
            "thesis_break_summary":    "[Claude fills at Step 10 — thesis-break conditions]",
            "probation_flag":          False,
            "eligibility_override_reason": override,
        }

    # -----------------------------------------------------------------------
    # Phase 6: Combined ranking and top-10 selection
    # -----------------------------------------------------------------------
    combined: dict[str, dict] = {}
    combined.update(updated_existing)
    for ticker, entry in new_candidates.items():
        if ticker not in combined:
            combined[ticker] = entry

    def rank_key(item):
        return -(item.get("normalised_score") or 0)

    # Separate removal candidates before ranking
    removal_candidates = {t: e for t, e in combined.items() if e.get("_removal_flag")}
    rankable = {t: e for t, e in combined.items() if not e.get("_removal_flag")}

    ranked = sorted(rankable.values(), key=rank_key)

    # Top MAX_WATCHLIST
    new_wl  = ranked[:MAX_WATCHLIST]
    removed = ranked[MAX_WATCHLIST:]

    # Log names removed by score displacement or removal_flag
    new_wl_tickers  = {e["ticker"] for e in new_wl}
    old_wl_tickers  = set(current_watchlist.keys())

    added_tickers   = new_wl_tickers - old_wl_tickers
    removed_tickers = old_wl_tickers - new_wl_tickers

    for ticker in added_tickers:
        entry = combined[ticker]
        promotion_log["additions"].append({
            "ticker":           ticker,
            "normalised_score": entry.get("normalised_score"),
            "path":             entry.get("path"),
            "source_file":      candidate_pool.get(ticker, {}).get("source_file", ""),
        })

    for e in removed:
        if e["ticker"] in old_wl_tickers:
            new_top10_ns = (new_wl[-1].get("normalised_score") or 0) if new_wl else 0
            promotion_log["removals"].append({
                "ticker": e["ticker"],
                "reason": (
                    f"ranked out — normalised score {e.get('normalised_score', 'N/A')}, "
                    f"below new #10 at {new_top10_ns}"
                ),
            })

    # Explicitly removed by floor / stale+floor combination
    for ticker, e in removal_candidates.items():
        if ticker in old_wl_tickers:
            promotion_log["removals"].append({
                "ticker": ticker,
                "reason": (
                    f"removed — normalised_score {e.get('normalised_score', 'N/A')} "
                    f"below hard floor ({NORMALISED_SCORE_HARD_REMOVE_BELOW}) "
                    + ("and no override" if not e.get("eligibility_override_reason") else "")
                    + (" and stale score" if e.get("stale_score_flag") else "")
                ),
            })

    # Assign ranks to new_wl
    for i, entry in enumerate(new_wl, 1):
        entry["rank"] = i

    # Strip internal (_-prefixed) fields before writing to JSON
    INTERNAL_FIELDS = {"_removal_flag"}
    for entry in new_wl:
        for f in INTERNAL_FIELDS:
            entry.pop(f, None)

    # --- Build candidate_pool list: all >=70 that are NOT in new_wl ---
    new_wl_set = {e["ticker"] for e in new_wl}
    pool_entries = []
    for ticker, row in candidate_pool.items():
        ns = normalised_score(row.get("total", 0), row.get("path", "A")) or 0.0
        override = row.get("eligibility_override_reason")
        if ns < NORMALISED_SCORE_PROMOTION_GATE and not override:
            continue  # below gate — already in rejected_candidates log
        if ticker in new_wl_set:
            continue  # already in top-10 — not duplicated in candidate_pool
        if ticker in {s.get("ticker", "") for s in wl_data.get("stock_sleeve", [])}:
            continue  # already held
        if ticker in {e.get("ticker", "") for e in wl_data.get("vci_watchlist", [])}:
            continue  # already in VCI watchlist

        target  = row.get("target_price")
        current_p = row.get("current_price")
        if target and target > 0:
            entry_level = round(target * 0.75, 2)
        elif current_p and current_p > 0:
            entry_level = round(current_p * 0.88, 2)
        else:
            entry_level = None

        pool_entries.append({
            "ticker":                  ticker,
            "name":                    row.get("company", ticker),
            "exchange":                row.get("exchange", "NASDAQ"),
            "path":                    row.get("path", "A"),
            "source_pipeline":         "growth_stock" if row.get("path", "A") == "A" else "energy",
            "normalised_score":        round(ns, 1),
            "total":                   row.get("total"),
            "part_a":                  row.get("part_a"),
            "part_b":                  row.get("part_b"),
            "entry_level":             entry_level,
            "entry_currency":          row.get("currency", "USD"),
            "entry_level_provisional": True,
            "sector":                  row.get("sector", ""),
            "candidate_pool_month":    month_label,
            "eligibility_override_reason": override,
        })

    # Sort pool by normalised_score descending
    pool_entries.sort(key=lambda x: -(x.get("normalised_score") or 0))

    # -----------------------------------------------------------------------
    # Phase 7: Write outputs
    # -----------------------------------------------------------------------
    if not dry_run:
        wl_data["watchlist"] = new_wl

        # Write candidate_pool (ephemeral — overwrites any prior month's pool)
        wl_data["candidate_pool"] = pool_entries
        wl_data["_candidate_pool_meta"] = {
            "last_updated":       run_date,
            "pool_month":         month_label,
            "gate_threshold_pct": NORMALISED_SCORE_PROMOTION_GATE,
            "pool_size":          len(pool_entries),
            "note": "EPHEMERAL — wiped and rewritten each pre-run by update_watchlist.py. Do not manually edit.",
        }

        wl_data["_meta"]["last_updated"]   = run_date
        wl_data["_meta"]["updated_by_run"] = f"{month_label} pre-run — update_watchlist.py"

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wl_data, f, indent=2, ensure_ascii=False)

        print(f"  [update_watchlist] watchlist_tickers.json written: {len(new_wl)} watchlist + {len(pool_entries)} candidate_pool entries")

        # Archive xlsx files instead of deleting
        archive_dir = os.path.join(inv_dir, XLSX_ARCHIVE_SUBDIR, run_date[:7])  # e.g. archive/2026-06/
        if xlsx_files:
            os.makedirs(archive_dir, exist_ok=True)

        for xlsx_path in xlsx_files:
            try:
                dest = os.path.join(archive_dir, os.path.basename(xlsx_path))
                shutil.move(xlsx_path, dest)
                fname = os.path.basename(xlsx_path)
                promotion_log["xlsx_files_archived"].append({
                    "file":        fname,
                    "archived_to": dest,
                })
                print(f"  [update_watchlist] Archived: {fname} → {archive_dir}")
            except Exception as e:
                print(f"  [update_watchlist] Could not archive {os.path.basename(xlsx_path)}: {e}")
                # Fall back to delete if move fails (network drive unavailable etc.)
                try:
                    os.remove(xlsx_path)
                    promotion_log["xlsx_files_deleted"].append(os.path.basename(xlsx_path))
                    print(f"  [update_watchlist] Fallback deleted: {os.path.basename(xlsx_path)}")
                except Exception as e2:
                    print(f"  [update_watchlist] Could not delete either: {e2}")
    else:
        print(f"  [update_watchlist] [DRY RUN] Would write {len(new_wl)} watchlist + {len(pool_entries)} pool entries")
        print(f"  [update_watchlist] [DRY RUN] Would archive {len(xlsx_files)} xlsx file(s) to archive/{run_date[:7]}/")

    # Print summary
    print(f"  [update_watchlist] Additions: {[e['ticker'] for e in promotion_log['additions']]}")
    print(f"  [update_watchlist] Removals:  {[e['ticker'] for e in promotion_log['removals']]}")
    print(f"  [update_watchlist] Score updates: {len(promotion_log['score_updates'])}")
    print(f"  [update_watchlist] Candidate pool: {len(pool_entries)} entries")
    print(f"  [update_watchlist] Rejected (below 70% gate): {len(promotion_log['rejected_candidates'])}")
    if promotion_log["misrouted_energy"]:
        print(f"  [update_watchlist] Misrouted energy: {promotion_log['path_c_misrouted_log']}")
    if promotion_log["stale_scores"]:
        print(f"  [update_watchlist] Stale scores: {[e['ticker'] for e in promotion_log['stale_scores']]}")
    if promotion_log["duplicate_ticker_log"]:
        print(f"  [update_watchlist] Cross-file duplicates resolved: {len(promotion_log['duplicate_ticker_log'])}")

    # Emit the promotion log as JSON on stdout for the orchestrator to capture
    print(json.dumps(promotion_log))

    return promotion_log


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Watchlist promotion, score refresh, and ranking. "
            "Runs as Step 4 of monthly_isa_prerun.py pipeline."
        )
    )
    parser.add_argument("--portfolio-data", required=False, default=None,
                        help="Path to portfolio_data_mmm_yyyy.json (for portfolio duplicate check)")
    parser.add_argument("--watchlist-json", required=True,
                        help="Path to watchlist_tickers.json (current watchlist)")
    parser.add_argument("--inv-dir", required=True,
                        help="Investment Analysis folder path (to locate Growth Stock Analysis xlsx files)")
    parser.add_argument("--out-json", required=True,
                        help="Output path for updated watchlist_tickers.json (usually same as input)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be done without writing or deleting files")
    args = parser.parse_args()

    run(
        portfolio_path = args.portfolio_data or "",
        watchlist_path = args.watchlist_json,
        inv_dir        = args.inv_dir,
        out_path       = args.out_json,
        dry_run        = args.dry_run,
    )


if __name__ == "__main__":
    main()
