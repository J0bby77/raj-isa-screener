#!/usr/bin/env python3
"""
sync_vci_watchlist.py  --  VCI Watchlist Sync + Part A Refresh
Version: 1.1  |  2026-06-10 (ACS8 sourced from scorer; A10/A11 + bottleneck-FV passed through and persisted; reliability guard never degrades a score on a missing/inconsistent breakdown)

Purpose:
    Reads project_isa_vci_watchlist.md (the authoritative VCI memory file,
    updated by update_vci_watchlist.py after each VCI run), refreshes VCI
    Part A scores by calling vci_acs_scorer.py, recomputes ACS using fresh
    Part A and stored Part B dimension scores, and writes the updated VCI
    candidate data back into watchlist_tickers.json's vci_watchlist section.

    Called by monthly_isa_prerun.py as Step 5.

Usage:
    python sync_vci_watchlist.py \\
      --watchlist-md  "/path/to/project_isa_vci_watchlist.md" \\
      --watchlist-json "watchlist_tickers.json" \\
      --inv-dir       "/path/to/Investment Analysis" \\
      [--dry-run]

Outputs:
    watchlist_tickers.json  --  updated in-place: vci_watchlist section refreshed
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime


# ---------------------------------------------------------------------------
# Markdown parser — reads project_isa_vci_watchlist.md
# ---------------------------------------------------------------------------

def _header_map(header_cells):
    """Map known column names -> index from a detected header row. Returns {} if not a header."""
    aliases = {
        "rank": "rank", "#": "rank",
        "ticker": "ticker", "symbol": "ticker",
        "company": "company", "name": "company",
        "exchange": "exchange", "exch": "exchange",
        "acs": "acs_score", "acs score": "acs_score",
        "3yr pos": "three_yr_pos_pct", "3yr pos %": "three_yr_pos_pct",
        "3yr_pos_pct": "three_yr_pos_pct", "3yr position": "three_yr_pos_pct",
        "nvidia signals": "nvidia_signals", "nvidia": "nvidia_signals",
        "classification": "classification", "class": "classification",
        "entry level": "entry_level_str", "entry": "entry_level_str",
        "last scored": "last_scored", "scored": "last_scored",
        "status": "status", "note": "note", "notes": "note",
    }
    cmap = {}
    for i, c in enumerate(header_cells):
        key = aliases.get(c.strip().lower())
        if key and key not in cmap:
            cmap[key] = i
    # Require at least ticker + acs to treat as a usable header
    return cmap if ("ticker" in cmap and "acs_score" in cmap) else {}


def _parse_by_header(table_lines):
    """Parse rows using a detected header map. Returns list[dict] or None if no header."""
    cmap = {}
    rows = []
    for line in table_lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s\-\|:]+\|$", line):
            continue  # separator
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cmap:
            cmap = _header_map(cells)
            if cmap:
                continue  # this was the header row
            else:
                continue  # skip until a header is found
        def g(key, default=""):
            i = cmap.get(key)
            return cells[i] if (i is not None and i < len(cells)) else default
        try:
            rank = int(re.search(r"\d+", g("rank", "")).group())
        except (AttributeError, ValueError):
            continue
        ticker = g("ticker")
        if not ticker or ticker.lower() in ("ticker", ""):
            continue
        acs_m = re.search(r"\d+", g("acs_score", ""))
        exchange = ticker.split(".")[-1] if "." in ticker else g("exchange").upper()
        entry_str = g("entry_level_str")
        currency = "GBP" if ("p" in entry_str.lower() or "£" in entry_str or ".L" in ticker) else "USD"
        rows.append({
            "rank": rank, "ticker": ticker, "company": g("company"),
            "exchange": exchange,
            "acs_score": int(acs_m.group()) if acs_m else None,
            "three_yr_pos_pct": g("three_yr_pos_pct"),
            "nvidia_signals": g("nvidia_signals"),
            "classification": g("classification").upper(),
            "entry_level_str": entry_str, "entry_currency": currency,
            "entry_level_midpoint": _parse_entry_level_midpoint(entry_str, currency),
            "last_scored": g("last_scored"), "status": g("status"), "note": g("note"),
        })
    return rows or None


def parse_vci_watchlist_md(md_path: str) -> list[dict]:
    """
    Parse the markdown table under '## Current Asymmetric Watchlist' in the
    VCI watchlist memory file.

    Returns a list of dicts with keys:
        rank, ticker, company, exchange, acs_score, three_yr_pos_pct,
        nvidia_signals, classification, entry_level_str, entry_currency,
        entry_level_midpoint, last_scored, status, note
    """
    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    # Locate the heading, then collect the contiguous block of pipe-table lines that
    # follow it (robust to spacing/column variation, unlike a single multiline regex).
    lines = content.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"\s*#{1,6}\s*Current Asymmetric Watchlist", ln, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        print("  WARNING: Could not find 'Current Asymmetric Watchlist' heading in md file.")
        return []

    table_lines = []
    seen_table = False
    for ln in lines[start:]:
        if ln.strip().startswith("|"):
            seen_table = True
            table_lines.append(ln)
        elif seen_table and ln.strip() == "":
            break  # blank line ends the table block
        elif seen_table:
            break  # non-table content ends the block
        elif ln.strip().startswith("#"):
            break  # next heading before any table -> no table here
    if not table_lines:
        print("  WARNING: No pipe-table found under 'Current Asymmetric Watchlist'.")
        return []

    header_rows = _parse_by_header(table_lines)
    if header_rows:
        return header_rows

    table_text = "\n".join(table_lines)
    rows = []
    for line in table_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s\-\|]+\|$", line):
            continue  # separator row
        cols = [c.strip() for c in line.strip("|").split("|")]
        if not cols or cols[0].lower() in ("rank", "#", ""):
            continue  # header row
        if len(cols) < 5:
            continue

        try:
            rank = int(cols[0])
        except ValueError:
            continue

        ticker = cols[1] if len(cols) > 1 else ""
        company = cols[2] if len(cols) > 2 else ""

        # Exchange may be embedded as TICKER.L or separate column
        exchange = ""
        if "." in ticker:
            parts = ticker.split(".")
            exchange = parts[-1]
        elif len(cols) > 3 and cols[3].upper() in ("LSE", "NYSE", "NASDAQ", "AIM", "TSX"):
            exchange = cols[3].upper()

        # ACS score
        acs_raw = cols[3] if len(cols) > 3 else ""
        # If exchange was in col 3, ACS is in col 4
        if exchange and cols[3].upper() == exchange:
            acs_raw = cols[4] if len(cols) > 4 else ""
        acs_score = None
        m = re.search(r"(\d+)", acs_raw)
        if m:
            acs_score = int(m.group(1))

        # Try to find remaining fields flexibly (indices may shift by 1 if exchange col present)
        offset = 1 if (exchange and len(cols) > 5 and cols[3].upper() == exchange) else 0

        three_yr_pos_pct_raw = cols[4 + offset] if len(cols) > 4 + offset else ""
        nvidia_signals = cols[5 + offset] if len(cols) > 5 + offset else ""
        classification = cols[6 + offset] if len(cols) > 6 + offset else ""
        entry_level_str = cols[7 + offset] if len(cols) > 7 + offset else ""
        last_scored = cols[8 + offset] if len(cols) > 8 + offset else ""
        status = cols[9 + offset] if len(cols) > 9 + offset else ""
        note = cols[10 + offset] if len(cols) > 10 + offset else ""

        # Parse entry level — handle ranges like "130p–145p" or "$12–$15" or "£1.30–£1.45"
        entry_currency = "USD"
        if "p" in entry_level_str.lower() or "£" in entry_level_str or ".L" in ticker:
            entry_currency = "GBP"
        elif "$" in entry_level_str:
            entry_currency = "USD"

        entry_midpoint = _parse_entry_level_midpoint(entry_level_str, entry_currency)

        rows.append({
            "rank": rank,
            "ticker": ticker,
            "company": company,
            "exchange": exchange,
            "acs_score": acs_score,
            "three_yr_pos_pct": three_yr_pos_pct_raw,
            "nvidia_signals": nvidia_signals,
            "classification": classification.upper(),
            "entry_level_str": entry_level_str,
            "entry_currency": entry_currency,
            "entry_level_midpoint": entry_midpoint,
            "last_scored": last_scored,
            "status": status,
            "note": note,
        })

    return rows


def _parse_entry_level_midpoint(entry_str: str, currency: str) -> float | None:
    """
    Parse entry level string to midpoint float.
    Handles: "130p–145p", "$12–$15", "£1.30", "1.35", "130p"
    Returns value in the natural unit (GBP as £ not pence, USD as $).
    """
    # Strip currency symbols
    s = entry_str.replace("£", "").replace("$", "").replace(",", "").strip()

    # Handle pence notation
    in_pence = False
    if "p" in s.lower():
        in_pence = True
        s = s.lower().replace("p", " ").strip()

    # Handle range: "130–145" or "130 - 145"
    range_match = re.search(r"([\d\.]+)\s*[–\-–]\s*([\d\.]+)", s)
    if range_match:
        lo, hi = float(range_match.group(1)), float(range_match.group(2))
        val = (lo + hi) / 2
    else:
        num_match = re.search(r"[\d\.]+", s)
        if not num_match:
            return None
        val = float(num_match.group())

    if in_pence:
        val = val / 100  # convert pence to pounds

    return round(val, 4)


# ---------------------------------------------------------------------------
# Refresh Part A via vci_acs_scorer.py subprocess
# ---------------------------------------------------------------------------

def refresh_part_a(ticker: str, inv_dir: str,
                   a10_override: str | None = None,
                   a11_override: str | None = None,
                   fv_override: str | None = None,
                   catalyst: bool = False) -> dict | None:
    """
    Call vci_acs_scorer.py for a single ticker.
    Returns parsed JSON output or None on failure.
    """
    scorer_path = os.path.join(inv_dir, "vci_acs_scorer.py")
    if not os.path.exists(scorer_path):
        print(f"  WARNING: vci_acs_scorer.py not found at {scorer_path}")
        return None

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                     prefix=f"vci_refresh_{ticker}_") as tmp:
        tmp_path = tmp.name

    cmd = [sys.executable, scorer_path, ticker, "--json-out", tmp_path]
    if a10_override:
        cmd += ["--a10", a10_override]
    if a11_override:
        cmd += ["--a11", a11_override]
    if fv_override:
        cmd += ["--bottleneck-fv", fv_override]
    if catalyst:
        cmd += ["--catalyst", ticker]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode != 0:
            print(f"  WARNING: vci_acs_scorer.py failed for {ticker}: {result.stderr[:200]}")
            return None
        if not os.path.exists(tmp_path):
            print(f"  WARNING: vci_acs_scorer.py produced no output for {ticker}")
            return None
        with open(tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = data[0] if data else None
        return data
    except subprocess.TimeoutExpired:
        print(f"  WARNING: vci_acs_scorer.py timed out for {ticker}")
        return None
    except Exception as e:
        print(f"  WARNING: Exception refreshing {ticker}: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# ACS recomputation
# ---------------------------------------------------------------------------

_FV_INPUTS_CACHE = {}


def _fv_inputs_lib(inv_dir: str) -> dict:
    """v2 (E2): load structured §10.2 fv_inputs per ticker from vci_fv_inputs.json (cached).
    Enables the FV confidence interval + avoids the scalar-only manual-confirm for named candidates."""
    p = os.path.join(inv_dir, "vci_fv_inputs.json")
    if p in _FV_INPUTS_CACHE:
        return _FV_INPUTS_CACHE[p]
    lib = {}
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        lib = {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, dict)}
    except Exception:
        lib = {}
    _FV_INPUTS_CACHE[p] = lib
    return lib


def recompute_acs(fresh_part_a_data: dict, existing_entry: dict) -> tuple[int, str]:
    """
    Recompute total ACS from fresh Part A (ACS4, ACS8) and stored Part B dimensions.

    Reliability guard (v1.1): a refresh must never DEGRADE a score because the stored
    breakdown is missing or inconsistent. If the stored breakdown does not parse to all
    nine dimensions, or the reconstructed total deviates from the stored ACS by >3, the
    stored ACS is carried forward unchanged. Only when the breakdown is internally
    consistent do we substitute the freshly-sourced ACS4 and ACS8.

    ACS dimensions:
        ACS1-3, ACS5-7, ACS9 = Part B (carried forward from stored breakdown)
        ACS4 = quantitative financial threshold (Part A; refreshed)
        ACS8 = entry risk-reward / 3yr range or FV-asymmetry (Part A; refreshed)
    """
    stored_breakdown = existing_entry.get("acs_breakdown", "") or ""
    stored_acs = existing_entry.get("acs_score")

    def parse_dim(dim_name: str):
        m = re.search(rf"{dim_name}:([0-9]+(?:\.[0-9]+)?)", stored_breakdown)
        return float(m.group(1)) if m else None

    dims = {n: parse_dim(f"ACS{n}") for n in range(1, 10)}
    a10_stored = parse_dim("A10")
    a11_stored = parse_dim("A11")

    fresh_acs4 = fresh_part_a_data.get("acs4")
    fresh_acs8 = fresh_part_a_data.get("acs8", fresh_part_a_data.get("range_position_score"))
    a10_fresh = fresh_part_a_data.get("a10_score")
    a11_fresh = fresh_part_a_data.get("a11_score")

    partb_present = all(dims[n] is not None for n in (1, 2, 3, 5, 6, 7, 9))
    reconstructable = partb_present and dims[4] is not None and dims[8] is not None
    reliable = False
    if reconstructable and isinstance(stored_acs, (int, float)):
        recon = sum(dims[n] for n in range(1, 10))
        reliable = abs(recon - stored_acs) <= 3

    if not reliable:
        # Carry forward — never degrade on unreliable/absent breakdown.
        return (int(stored_acs) if isinstance(stored_acs, (int, float)) else 0), stored_breakdown

    new_acs4 = fresh_acs4 if fresh_acs4 is not None else dims[4]
    new_acs8 = fresh_acs8 if fresh_acs8 is not None else dims[8]
    new_acs = round(dims[1] + dims[2] + dims[3] + new_acs4 + dims[5] + dims[6]
                    + dims[7] + new_acs8 + dims[9])

    if existing_entry.get("floor_applied"):
        floor_acs = existing_entry.get("floor_acs", existing_entry.get("acs_score", new_acs))
        if isinstance(floor_acs, (int, float)) and new_acs > floor_acs:
            new_acs = int(floor_acs)

    a10v = a10_fresh if a10_fresh is not None else (a10_stored if a10_stored is not None else 0)
    a11v = a11_fresh if a11_fresh is not None else (a11_stored if a11_stored is not None else 0)
    def _i(x):
        return int(x) if float(x).is_integer() else x
    new_breakdown = (
        f"ACS1:{_i(dims[1])} ACS2:{_i(dims[2])} ACS3:{_i(dims[3])} ACS4:{_i(new_acs4)} "
        f"ACS5:{_i(dims[5])} ACS6:{_i(dims[6])} ACS7:{_i(dims[7])} ACS8:{_i(new_acs8)} ACS9:{_i(dims[9])} "
        f"A10:{_i(a10v)} A11:{_i(a11v)}"
    )
    return int(new_acs), new_breakdown

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync VCI watchlist from project_isa_vci_watchlist.md and refresh Part A scores."
    )
    parser.add_argument("--watchlist-md",   required=True,
                        help="Path to project_isa_vci_watchlist.md")
    parser.add_argument("--watchlist-json", required=True,
                        help="Path to watchlist_tickers.json (updated in-place)")
    parser.add_argument("--inv-dir",        required=True,
                        help="Investment Analysis folder (contains vci_acs_scorer.py)")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Parse and score but do not write to watchlist_tickers.json")
    parser.add_argument("--portfolio-data", required=False, default=None,
                        help="Path to portfolio_data_mmm_yyyy.json — held names are dropped from the VCI watchlist")
    args = parser.parse_args()

    if not os.path.exists(args.watchlist_md):
        print(f"WARNING: {args.watchlist_md} not found — VCI watchlist carries forward unchanged.")
        sys.exit(0)

    if not os.path.exists(args.watchlist_json):
        print(f"ERROR: watchlist_tickers.json not found at {args.watchlist_json}")
        sys.exit(1)

    # 1. Parse VCI watchlist md
    print(f"  Reading {os.path.basename(args.watchlist_md)}...")
    md_entries = parse_vci_watchlist_md(args.watchlist_md)
    if not md_entries:
        print("  No VCI watchlist entries found in md file — carrying forward unchanged.")
        sys.exit(0)
    print(f"  Found {len(md_entries)} VCI candidate(s) in md file.")

    # 2. Load watchlist_tickers.json
    with open(args.watchlist_json, encoding="utf-8") as f:
        wt = json.load(f)

    existing_vci = {e["ticker"]: e for e in wt.get("vci_watchlist", [])}

    # 3. For each ticker: refresh Part A, recompute ACS, build updated entry
    updated_entries = []
    summary_lines = []

    for md_entry in md_entries:
        ticker = md_entry["ticker"]
        print(f"  [{ticker}] Refreshing Part A via vci_acs_scorer.py...")

        existing = existing_vci.get(ticker, {})

        # A10/A11 manual scores: prefer explicit entry fields, fall back to the
        # breakdown string. Without these the refresh would drop the web/Finnhub-sourced
        # manual metrics and silently deflate Part A / ACS4.
        def _entry_dim(field, dim):
            v = existing.get(field)
            if v is None and existing.get("acs_breakdown"):
                m = re.search(rf"{dim}:([0-9]+)", existing["acs_breakdown"])
                v = int(m.group(1)) if m else None
            return v
        a10_val = _entry_dim("a10_score", "A10")
        a11_val = _entry_dim("a11_score", "A11")
        a10_override = f"{ticker}:{int(a10_val)}" if a10_val is not None else None
        a11_override = f"{ticker}:{int(a11_val)}" if a11_val is not None else None

        # Bottleneck fair value (ACS8 primary path) + persisted catalyst-premium flag
        fv_val = existing.get("bottleneck_fair_value_gbp") if existing.get("entry_currency") == "GBP" \
                 else existing.get("bottleneck_fair_value_usd")
        if fv_val is None:
            fv_val = existing.get("bottleneck_fair_value_usd") or existing.get("bottleneck_fair_value_gbp")
        fv_override = f"{ticker}:{fv_val}" if fv_val is not None else None
        catalyst = bool(existing.get("acs8_catalyst_premium"))

        # Call scorer
        scorer_result = refresh_part_a(ticker, args.inv_dir, a10_override, a11_override,
                                       fv_override=fv_override, catalyst=catalyst)

        old_acs = existing.get("acs_score", md_entry["acs_score"])
        fresh_part_a_score = None

        if scorer_result:
            fresh_part_a_score = scorer_result.get("part_a_score",
                                  scorer_result.get("raw_score"))
            new_acs, new_breakdown = recompute_acs(scorer_result, existing)
        else:
            # Carry forward stored values
            print(f"    Scorer failed — carrying forward stored ACS for {ticker}")
            new_acs = old_acs or md_entry.get("acs_score") or 0
            new_breakdown = existing.get("acs_breakdown", "")
            fresh_part_a_score = existing.get("part_a_score")

        delta = (new_acs or 0) - (old_acs or 0)
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        summary_lines.append(
            f"    {ticker}: ACS {old_acs} → {new_acs} ({delta_str})"
            + (f" | Part A refresh: {fresh_part_a_score}" if fresh_part_a_score is not None else "")
        )

        # Build updated entry, carrying forward fields not in md
        entry_level = md_entry["entry_level_midpoint"] or existing.get("entry_level")

        updated_entry = {
            "rank":             md_entry["rank"],
            "ticker":           ticker,
            "name":             md_entry["company"] or existing.get("name", ticker),
            "exchange":         md_entry["exchange"] or existing.get("exchange", ""),
            "entry_level":      entry_level,
            "entry_currency":   md_entry["entry_currency"],
            "source_pipeline":  "vci",
            "acs_score":        new_acs,
            "vci_run_date":     md_entry["last_scored"] or existing.get("vci_run_date", ""),
            "nvidia_signals":   md_entry["nvidia_signals"] or existing.get("nvidia_signals", ""),
            "classification":   md_entry["classification"] or existing.get("classification", ""),
            "status":           md_entry["status"] or existing.get("status", ""),
            "part_a_score":     fresh_part_a_score if fresh_part_a_score is not None
                                else existing.get("part_a_score"),
            "part_b_score":     existing.get("part_b_score"),
            "acs_breakdown":    new_breakdown or existing.get("acs_breakdown", ""),
            "a10_score":        (scorer_result.get("a10_score") if scorer_result else None)
                                if (scorer_result and scorer_result.get("a10_score") is not None)
                                else existing.get("a10_score"),
            "a11_score":        (scorer_result.get("a11_score") if scorer_result else None)
                                if (scorer_result and scorer_result.get("a11_score") is not None)
                                else existing.get("a11_score"),
            "three_yr_pos_pct": (round(scorer_result["three_yr_pos_pct"], 1)
                                 if (scorer_result and scorer_result.get("three_yr_pos_pct") is not None)
                                 else existing.get("three_yr_pos_pct")),
            "acs8_catalyst_premium": existing.get("acs8_catalyst_premium", False),
            "floor_applied":    existing.get("floor_applied", False),
            "floor_reason":     existing.get("floor_reason", ""),
            "thesis_break_summary": existing.get("thesis_break_summary", ""),
            "rescore_date":     existing.get("rescore_date", ""),
            "rescore_catalyst": existing.get("rescore_catalyst", ""),
        }

        # Carry forward fair value fields
        for fv_key in ("bottleneck_fair_value_gbp", "bottleneck_fair_value_usd"):
            if fv_key in existing:
                updated_entry[fv_key] = existing[fv_key]

        # --- Forward-led VCI fields (§14.2) ------------------------------------------------
        # Persist the §10 win-case FV inputs + deployability fields so monthly_isa_prerun.py
        # Step 5 can RE-PRICE fv_asymmetry / VCI_Source_Score at the Saturday live price and
        # re-rank (the recompute lives in the pre-run — sync just carries the raw inputs through).
        # NB: the old ×1.40 stale-entry multiplier is DELETED (never re-add) — a stale entry is
        # now surfaced as an asymmetry-below-floor flag at re-price time, not a price nudge.
        # v2 (E2, LIVE): prefer structured §10.2 inputs from vci_fv_inputs.json (by ticker)
        _fvrec = (_fv_inputs_lib(args.inv_dir).get(ticker)
                  or _fv_inputs_lib(args.inv_dir).get(str(ticker).split(".")[0]) or {})
        updated_entry["asset_structure"]         = _fvrec.get("asset_structure") or existing.get("asset_structure", "single_asset")
        updated_entry["fv_inputs"]               = _fvrec.get("fv_inputs") or existing.get("fv_inputs", {})
        # bottleneck FV per share in the entry currency == the win-case FV used for asymmetry
        updated_entry["bottleneck_fv_per_share"] = fv_val if fv_val is not None else existing.get("bottleneck_fv_per_share")
        updated_entry["fv_source"]               = existing.get("fv_source", "modeled" if fv_val is not None else "estimated")
        # catalyst / monitoring inputs the re-price consumes (carried forward from the VCI run)
        updated_entry["has_catalyst"]            = bool(existing.get("has_catalyst", catalyst))
        updated_entry["days_to_catalyst"]        = existing.get("days_to_catalyst")
        updated_entry["signal_count"]            = existing.get("signal_count", 0)
        updated_entry["mgmt_unstable"]           = bool(existing.get("mgmt_unstable", False))
        updated_entry["falls_on_beat"]           = bool(existing.get("falls_on_beat", False))
        # last-known deployability (advisory; OVERWRITTEN by the pre-run re-price each Saturday)
        updated_entry["fv_asymmetry"]            = existing.get("fv_asymmetry")
        updated_entry["vci_source_score"]        = existing.get("vci_source_score")
        updated_entry["deploy_eligible"]         = existing.get("deploy_eligible")
        # --- v2 enhancement fields (carried forward; re-priced by the pre-run Step 6.5) ---
        # E3: quality-ex-ACS8 for the VCI Source Score quality term. ACS8 == upside-to-FV ==
        # asymmetry-1, so it must NOT feed the rank's quality term (double-count with the asymmetry
        # term). Freeze ACS8 at a neutral constant: acs_ex_acs8 = ACS - ACS8 + neutral. This keeps
        # the 0-100 quality scale but makes quality invariant to asymmetry (proven in test E3).
        _acs8v = (scorer_result.get("acs8") if scorer_result else None)
        if _acs8v is None:
            _m8 = re.search(r"ACS8:([0-9]+(?:\.[0-9]+)?)", new_breakdown or "")
            _acs8v = float(_m8.group(1)) if _m8 else None
        _acs8_neutral = 5.0   # neutral ACS8 midpoint (0-10 dim); configurable via VCI_ACS8_NEUTRAL
        updated_entry["acs_ex_acs8"] = (int(round(new_acs - float(_acs8v) + _acs8_neutral))
                                        if (new_acs is not None and _acs8v is not None)
                                        else new_acs)
        updated_entry["catalyst_type"]           = _fvrec.get("catalyst_type") or existing.get("catalyst_type")         # E1 prior key
        updated_entry["catalyst_domain"]         = _fvrec.get("catalyst_domain") or existing.get("catalyst_domain")     # E4 correlation
        updated_entry["p_thesis"]                = existing.get("p_thesis")              # E1 (base-rate seeded)
        updated_entry["L"]                       = existing.get("L")                     # E1
        updated_entry["floor_source"]            = existing.get("floor_source")
        updated_entry["fv_p25"]                  = existing.get("fv_p25")                # E2
        updated_entry["fv_p50"]                  = existing.get("fv_p50")
        updated_entry["fv_p75"]                  = existing.get("fv_p75")
        updated_entry["fv_asymmetry_p25"]        = existing.get("fv_asymmetry_p25")
        updated_entry["fv_crosscheck_warn"]      = existing.get("fv_crosscheck_warn")    # E2
        updated_entry["revision_velocity"]       = existing.get("revision_velocity")     # E6
        updated_entry["adv_usd"]                 = existing.get("adv_usd")               # E5
        updated_entry["size_liquidity_capped"]   = existing.get("size_liquidity_capped")
        updated_entry["expected_loss_pct_isa"]   = existing.get("expected_loss_pct_isa") # E4
        updated_entry["bottleneck_fv_per_share_prev"] = existing.get("bottleneck_fv_per_share_prev")  # E7
        updated_entry["price_prev"]              = existing.get("price_prev")            # E7
        updated_entry["asymmetry_compression_cause"] = existing.get("asymmetry_compression_cause")

        updated_entries.append(updated_entry)

    # Sort by rank
    updated_entries.sort(key=lambda e: e["rank"])

    # 3b. Drop names already held in the portfolio (suffix-insensitive). The md
    #     still lists them until the next VCI run migrates them to the VCI
    #     portfolio, so the monthly pre-run excludes them here every time.
    def _base(t):
        t = str(t or "").strip().upper()
        return t.split(".")[0] if "." in t else t
    held_base = set()
    if args.portfolio_data and os.path.exists(args.portfolio_data):
        try:
            with open(args.portfolio_data, encoding="utf-8") as pf:
                pdata = json.load(pf)
            held_base = {_base(s.get("ticker", "")) for s in pdata.get("stocks", []) if s.get("ticker")}
        except Exception as e:
            print(f"  WARNING: could not read portfolio_data for held-check: {e}")
    if held_base:
        before = len(updated_entries)
        dropped = [e["ticker"] for e in updated_entries if _base(e.get("ticker", "")) in held_base]
        updated_entries = [e for e in updated_entries if _base(e.get("ticker", "")) not in held_base]
        # re-rank contiguously after removal
        for i, e in enumerate(sorted(updated_entries, key=lambda x: x["rank"]), 1):
            e["rank"] = i
        if dropped:
            print(f"  Dropped {before - len(updated_entries)} held name(s) from VCI watchlist: {dropped}")

    # 4. Write back to watchlist_tickers.json
    if not args.dry_run:
        wt["vci_watchlist"] = updated_entries
        # Update meta
        if "_meta" in wt:
            wt["_meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            wt["_meta"]["updated_by_run"] = "sync_vci_watchlist.py"
        elif "_vci_watchlist_meta" in wt:
            wt["_vci_watchlist_meta"]["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(args.watchlist_json, "w", encoding="utf-8") as f:
            json.dump(wt, f, indent=2, ensure_ascii=False)
        print(f"  VCI watchlist written: {len(updated_entries)} entries → {args.watchlist_json}")
    else:
        print(f"  [DRY RUN] Would write {len(updated_entries)} VCI entries to {args.watchlist_json}")

    print("  ACS changes:")
    for line in summary_lines:
        print(line)

    # Validate: all entries have acs_score
    missing_acs = [e["ticker"] for e in updated_entries if not e.get("acs_score")]
    if missing_acs:
        print(f"  WARNING: acs_score missing for: {missing_acs}")
    else:
        print(f"  Validation: all {len(updated_entries)} entries have acs_score populated.")


if __name__ == "__main__":
    main()
