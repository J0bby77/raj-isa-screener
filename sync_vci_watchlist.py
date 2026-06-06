#!/usr/bin/env python3
"""
sync_vci_watchlist.py  --  VCI Watchlist Sync + Part A Refresh
Version: 1.0  |  2026-06-04

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

    # Find the table section
    section_match = re.search(
        r"##\s*Current Asymmetric Watchlist.*?(\|.*?\|(?:\n\|.*?\|)*)",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        print("  WARNING: Could not find 'Current Asymmetric Watchlist' table in md file.")
        return []

    table_text = section_match.group(1)
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
                   a11_override: str | None = None) -> dict | None:
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

def recompute_acs(fresh_part_a_data: dict, existing_entry: dict) -> tuple[int, str]:
    """
    Recompute total ACS from fresh Part A scores and stored Part B scores.
    Returns (new_acs, new_acs_breakdown_string).

    ACS dimensions:
        ACS1–ACS3, ACS5–ACS7, ACS9  = Part B (carry forward from stored)
        ACS4 = quantitative financial threshold (Part A)
        ACS8 = 3yr price range position (Part A)
    """
    stored_breakdown = existing_entry.get("acs_breakdown", "") or ""

    # Parse stored Part B dimensions from breakdown string
    # Format example: "ACS1:8 ACS2:6 ACS3:7 ACS4:12 ACS5:8 ACS6:6 ACS7:5 ACS8:8 ACS9:4"
    def parse_dim(dim_name: str, default: int = 0) -> int:
        m = re.search(rf"{dim_name}:(\d+)", stored_breakdown)
        return int(m.group(1)) if m else default

    acs1 = parse_dim("ACS1")
    acs2 = parse_dim("ACS2")
    acs3 = parse_dim("ACS3")
    acs5 = parse_dim("ACS5")
    acs6 = parse_dim("ACS6")
    acs7 = parse_dim("ACS7")
    acs9 = parse_dim("ACS9")

    # Fresh Part A scores
    acs4 = fresh_part_a_data.get("acs4", fresh_part_a_data.get("part_a_threshold_score", 0))
    acs8 = fresh_part_a_data.get("acs8", fresh_part_a_data.get("range_position_score", 0))

    new_acs = acs1 + acs2 + acs3 + acs4 + acs5 + acs6 + acs7 + acs8 + acs9

    # Apply floor cap if applicable
    if existing_entry.get("floor_applied"):
        floor_acs = existing_entry.get("floor_acs", existing_entry.get("acs_score", new_acs))
        if isinstance(floor_acs, (int, float)) and new_acs > floor_acs:
            new_acs = int(floor_acs)

    new_breakdown = (
        f"ACS1:{acs1} ACS2:{acs2} ACS3:{acs3} ACS4:{acs4} "
        f"ACS5:{acs5} ACS6:{acs6} ACS7:{acs7} ACS8:{acs8} ACS9:{acs9}"
    )

    return new_acs, new_breakdown


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

        # Extract A10/A11 overrides from existing breakdown
        a10_override = None
        a11_override = None
        if existing.get("acs_breakdown"):
            m10 = re.search(r"A10:(\d+)", existing["acs_breakdown"])
            m11 = re.search(r"A11:(\d+)", existing["acs_breakdown"])
            if m10:
                a10_override = f"{ticker}:{m10.group(1)}"
            if m11:
                a11_override = f"{ticker}:{m11.group(1)}"

        # Call scorer
        scorer_result = refresh_part_a(ticker, args.inv_dir, a10_override, a11_override)

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

        updated_entries.append(updated_entry)

    # Sort by rank
    updated_entries.sort(key=lambda e: e["rank"])

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
