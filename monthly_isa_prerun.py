#!/usr/bin/env python3
"""
monthly_isa_prerun.py  --  Monthly ISA Review Pre-Run Orchestrator
Version: 3.0  |  2026-06-04

Master script. Runs the day before the Monthly ISA Portfolio Review.
Schedule: Saturday before the first Sunday of each month, at 14:30.
The main review task runs the following morning (Sunday).

Pipeline (in order):
  Step 1: extract_portfolio.py       -> portfolio_data_mmm_yyyy.json
  Step 2: extract_xray.py            -> xray_data_mmm_yyyy.json
  Step 3: portfolio_analytics.py     -> analytics_data_mmm_yyyy.json
  Step 4: update_watchlist.py        -> updated watchlist_tickers.json + promotion log
  Step 5: sync_vci_watchlist.py      -> watchlist_tickers.json (vci_watchlist section refreshed)
  Step 6: fetch_watchlist_metrics.py -> watchlist_metrics_mmm_yyyy.json
  Step 7: normalise_adapter.py            -> watchlist_scored_mmm_yyyy.json
  Step 8: step9_pre_builder.py       -> step9_pre_mmm_yyyy.json
  Step 9: email_prefill.py           -> email_data_mmm_yyyy.json (pre-filled skeleton)
  Step 10: write run_context_mmm_yyyy.json (staging file with all paths + summary)

Each step saves its output immediately. If a step fails, the script stops and
reports the error clearly -- the review task will read the error from the staging
file and report it rather than running blind.

On success, the review task reads run_context_mmm_yyyy.json as its first pre-run
read instead of Step 2 (xlsx parse) and Step 3 (xray parse) separately.

Usage:
    python3 monthly_isa_prerun.py [--isa-folder /path/to/ISA] [--dry-run]

Outputs (all to Investment Analysis folder):
    portfolio_data_mmm_yyyy.json
    xray_data_mmm_yyyy.json
    analytics_data_mmm_yyyy.json
    watchlist_metrics_mmm_yyyy.json
    watchlist_scored_mmm_yyyy.json
    step9_pre_mmm_yyyy.json
    email_data_mmm_yyyy.json
    run_context_mmm_yyyy.json   <- review task reads this first
"""

import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Scripts (same folder as this orchestrator)
SCRIPTS = {
    "extract_portfolio":      os.path.join(SCRIPT_DIR, "extract_portfolio.py"),
    "extract_xray":           os.path.join(SCRIPT_DIR, "extract_xray.py"),
    "analytics":              os.path.join(SCRIPT_DIR, "portfolio_analytics.py"),
    "update_watchlist_py":    os.path.join(SCRIPT_DIR, "update_watchlist.py"),
    "sync_vci_watchlist":     os.path.join(SCRIPT_DIR, "sync_vci_watchlist.py"),    # NEW Step 5
    "fetch_watchlist":        os.path.join(SCRIPT_DIR, "fetch_watchlist_metrics.py"),
    "normalise_adapter":           os.path.join(SCRIPT_DIR, "normalise_adapter.py"),
    "rerank_watchlist":       os.path.join(SCRIPT_DIR, "rerank_watchlist.py"),       # NEW Step 7.5
    "entry_level_builder":    os.path.join(SCRIPT_DIR, "entry_level_builder.py"),    # NEW Step 7.25
    "step9_pre_builder":      os.path.join(SCRIPT_DIR, "step9_pre_builder.py"),     # NEW Step 8
    "email_prefill":          os.path.join(SCRIPT_DIR, "email_prefill.py"),
    "calibration_report":     os.path.join(SCRIPT_DIR, "calibration_report.py"),   # Jul-26 Part 9c
}

# Memory files (read by analytics for prior portfolio and trades log)
# These paths use the Windows path as passed through the bash mount.
def _resolve_memory_base() -> str:
    """Locate the Cowork memory dir. The previous relative ".." climb from
    SCRIPT_DIR landed in the OneDrive tree, so memory files were never found and
    Step 5 was skipped. Anchor at the USER HOME and fall back to a glob on the
    stable memory-space id so session/project id drift does not break discovery."""
    import glob as _g
    home  = os.path.expanduser("~")
    SPACE = "aa27f2f8-c3d3-4862-ba9a-a67b7f6d74b9"
    base  = os.path.join(home, "AppData", "Roaming", "Claude", "local-agent-mode-sessions")
    candidates = [os.path.join(base, "5240c546-04fc-4dfa-9e3c-ac4943abb0ca",
                               "f7637f5f-1fa6-4075-a7d9-50bc4a878712",
                               "spaces", SPACE, "memory")]
    candidates += _g.glob(os.path.join(base, "*", "*", "spaces", SPACE, "memory"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


MEMORY_BASE = _resolve_memory_base()


def find_memory_file(pattern: str) -> str | None:
    """Find latest memory file matching a glob-style prefix."""
    import glob as _glob
    candidates = _glob.glob(os.path.join(MEMORY_BASE, pattern))
    return max(candidates, default=None, key=os.path.getmtime) if candidates else None


# ---------------------------------------------------------------------------
# Run a script as a subprocess
# ---------------------------------------------------------------------------
def run_script(name: str, args: list[str], dry_run: bool = False) -> tuple[bool, str, str]:
    """
    Run a Python script. Returns (success, stdout, stderr).
    """
    script_path = SCRIPTS[name]
    if not os.path.exists(script_path):
        return False, "", f"Script not found: {script_path}"

    cmd = [sys.executable, script_path] + args
    if dry_run:
        print(f"  [DRY RUN] Would run: {' '.join(cmd)}")
        return True, "[dry run]", ""

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        success = result.returncode == 0
        return success, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Script timed out after 120s: {name}"
    except Exception as e:
        return False, "", str(e)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def validate_json_output(path: str, required_keys: list[str]) -> tuple[bool, str]:
    """Check output JSON exists and has required top-level keys."""
    if not os.path.exists(path):
        return False, f"Output file not found: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        missing = [k for k in required_keys if k not in data]
        if missing:
            return False, f"Missing keys in output: {missing}"
        return True, "OK"
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"
    except Exception as e:
        return False, str(e)


def validate_portfolio_value(portfolio_path: str) -> tuple[bool, str]:
    """Sanity check: portfolio value must be > 50,000 and < 10,000,000."""
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            data = json.load(f)
        total = data.get("summary", {}).get("total_value_gbp", 0)
        if total < 50_000:
            return False, f"Portfolio value suspiciously low: GBP {total:,.2f} -- check xlsx file"
        if total > 10_000_000:
            return False, f"Portfolio value suspiciously high: GBP {total:,.2f} -- check xlsx file"
        return True, f"Portfolio value: GBP {total:,.2f}"
    except Exception as e:
        return False, str(e)


def check_large_month_on_month_change(analytics_path: str, portfolio_path: str) -> list[str]:
    """
    Warn if total portfolio value has changed by more than 15% vs prior month.
    (Prior value comes from analytics prior_portfolio if available.)
    """
    warnings = []
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            port = json.load(f)
        with open(analytics_path, encoding="utf-8") as f:
            ana = json.load(f)
        current = port.get("summary", {}).get("total_value_gbp", 0)
        phase   = ana.get("phase_status", {})
        prior_pct = phase.get("prior_pct")
        if prior_pct is not None:
            # Can't get prior absolute value from pct alone, skip
            pass
    except Exception:
        pass
    return warnings


# ---------------------------------------------------------------------------
# Staging file writer
# ---------------------------------------------------------------------------
def write_run_context(
    month_label:            str,
    run_month:              str,
    portfolio_path:         str,
    xray_path:              str,
    analytics_path:         str,
    watchlist_metrics_path: str,
    watchlist_scored_path:  str,
    step9_pre_path:         str,
    email_path:             str,
    summary:                dict,
    flags:                  list,
    warnings:               list,
    status:                 str,
    error_message:          str = "",
) -> str:
    """Write the run_context_mmm_yyyy.json staging file."""
    ctx = {
        "_meta": {
            "description": (
                "Pre-run staging file produced by monthly_isa_prerun.py. "
                "Read by the Monthly ISA Portfolio Review task as its first pre-run read. "
                "Contains paths to all extracted data files and a summary for immediate use."
            ),
            "produced_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "month_label": month_label,
            "run_month":   run_month,
            "status":      status,   # "OK" or "ERROR" or "PARTIAL"
        },
        "files": {
            "portfolio_data":       portfolio_path,
            "xray_data":            xray_path,
            "analytics_data":       analytics_path,
            "watchlist_metrics":    watchlist_metrics_path,
            "watchlist_scored":     watchlist_scored_path,
            "step9_pre":            step9_pre_path,
            "entry_level_audit":    os.path.join(SCRIPT_DIR, f"entry_level_audit_{month_label}.json"),
            "email_data":           email_path,
            "target_weights":       os.path.join(SCRIPT_DIR, "target_weights.json"),
            "watchlist_tickers":    os.path.join(SCRIPT_DIR, "watchlist_tickers.json"),
            "action_stack":         os.path.join(SCRIPT_DIR, f"action_stack_{month_label}.json"),
            "decision_ledger":      os.path.join(SCRIPT_DIR, "decision_ledger.json"),
            "ai_disruption":        os.path.join(SCRIPT_DIR, "ai_disruption.json"),
            "fund_returns_cache":   os.path.join(SCRIPT_DIR, "fund_returns_cache.json"),
            "calibration_report":   os.path.join(SCRIPT_DIR, f"calibration_report_{month_label}.md"),
            "score_panel":          os.path.join(SCRIPT_DIR, "score_panel.csv"),
        },
        "summary": summary,
        "flags":   flags,
        "warnings": warnings,
        "error":   error_message,
        "instructions_for_review_task": (
            "1. Read this file first (replaces Step 2 xlsx/xray parse at runtime). "
            "2. Read files.portfolio_data for full holdings, cash, sleeve breakdown. "
            "3. Read files.analytics_data for drift table, signals, phase status, rebalancing candidates. "
            "4. Read files.watchlist_scored for pre-formatted Part A/B tables, conviction ranking, "
            "in-window flags (DISPLAY-ONLY), s5 watchlist rows, s7 sleeve rows, and s3 investment case skeletons. "
            "5. Read files.step9_pre — contains: "
            "(a) main_watchlist.T1/T2/T3: tier assignments for top-10 watchlist names. "
            "    T1 entries have strategic_conviction_score (7/10 pre-computed dimensions), "
            "    decision_bucket label, and risk_flags block (entry_window_score is DISPLAY-ONLY, not a ranking input). "
            "(b) candidate_pool.T1/T2/T3: same tier/score structure for additional names "
            "    passing the quality floor (normalised Part A+B >= 60) but outside top-10. "
            "(c) deployment_priority_rank: combined flat list of ALL eligible names "
            "    (watchlist + candidate_pool) sorted by SOURCE SCORE descending (forward-led; price window REMOVED). "
            "    Use this as the PRIMARY candidate ranking for Step 9. Tiers T1/T2/T3 are SOURCE-SCORE bands, NOT price bands; entry levels are display-only and never reorder this list. "
            "(d) vci_watchlist.T1_A/T2_A/T3_A: VCI candidates, unchanged structure. ""(e) files.action_stack: Global Action Stack — HELD Path-A positions scored on the SAME Source Score as candidates; AUTHORITATIVE for held decisions: SELL if source<50 or thesis-break/disqualifier, TRIM if a candidate beats a held name by >=15 and capital is tight, TOP_UP/HOLD if source>=65. Path-B/VCI holdings (e.g. ONT) excluded — assessed on ACS. "
            "At Step 9, complete the 3 session-dependent dimensions: macro_resilience, "
            "portfolio_fit (use portfolio_overlap flags from each entry as inputs), execution. "
            "6. Read files.email_data -- pre-filled skeleton. Fill ALL [Claude fills] placeholders during the run. "
            "7. Run build_monthly_isa_email.py after completing all sections. "
            "8. If status == ERROR: read the error field, report to Raj, do not proceed with incomplete data. "
            "POST-RUN: update watchlist_tickers.json with any ranking changes, entry level updates, "
            "additions/removals, and new stock sleeve purchases. "
            "NOTE: update_watchlist.py handles promotion/ranking automatically at pre-run; "
            "Claude only updates stock_sleeve purchases/sales and entry_level revisions post-run."
        ),
    }

    out_path = os.path.join(SCRIPT_DIR, f"run_context_{month_label}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ctx, f, indent=2, ensure_ascii=False)
    return out_path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Monthly ISA Pre-Run Orchestrator -- runs day before the main review."
    )
    parser.add_argument("--isa-folder",      default=None,
                        help="ISA root folder (parent of Investment Analysis). Auto-detected if omitted.")
    parser.add_argument("--prior-portfolio", default=None,
                        help="Path to prior month portfolio JSON for phase transition check.")
    parser.add_argument("--dry-run",         action="store_true",
                        help="Print commands without executing them.")
    args = parser.parse_args()

    isa_folder = args.isa_folder or os.path.dirname(SCRIPT_DIR)
    run_date   = date.today()
    month_label = run_date.strftime("%b_%Y").lower()
    run_month   = run_date.strftime("%b %Y")

    print("=" * 65)
    print(f"Monthly ISA Pre-Run  |  {run_month}  |  {datetime.now().strftime('%H:%M')}")
    print("=" * 65)

    # Ensure yfinance is available (not pre-installed in fresh Cowork sessions)
    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("  Installing yfinance...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "yfinance", "--break-system-packages", "-q"],
            capture_output=True
        )

    # Output paths
    portfolio_path          = os.path.join(SCRIPT_DIR, f"portfolio_data_{month_label}.json")
    xray_path               = os.path.join(SCRIPT_DIR, f"xray_data_{month_label}.json")
    analytics_path          = os.path.join(SCRIPT_DIR, f"analytics_data_{month_label}.json")
    watchlist_metrics_path  = os.path.join(SCRIPT_DIR, f"watchlist_metrics_{month_label}.json")
    watchlist_scored_path   = os.path.join(SCRIPT_DIR, f"watchlist_scored_{month_label}.json")
    step9_pre_path          = os.path.join(SCRIPT_DIR, f"step9_pre_{month_label}.json")
    entry_audit_path        = os.path.join(SCRIPT_DIR, f"entry_level_audit_{month_label}.json")
    email_path              = os.path.join(SCRIPT_DIR, f"email_data_{month_label}.json")
    watchlist_config_path   = os.path.join(SCRIPT_DIR, "watchlist_tickers.json")

    # Find memory files (optional -- best effort)
    trades_log_path  = find_memory_file("project_isa_trades_log.md")
    prior_port_path  = args.prior_portfolio

    if not prior_port_path:
        # Try to find last month's portfolio JSON.
        # NOTE: sort CHRONOLOGICALLY, not lexicographically -- month
        # abbreviations (jun/may/...) do not sort by date alphabetically,
        # which previously caused the wrong (older) prior to be selected.
        import glob as _glob
        _MON = {m: i for i, m in enumerate(
            ["jan","feb","mar","apr","may","jun",
             "jul","aug","sep","oct","nov","dec"], start=1)}
        def _mkey(path):
            b = os.path.basename(path)
            mm = re.search(r"portfolio_data_([a-z]{3})_(\d{4})", b, re.I)
            if not mm:
                return (0, 0)
            return (int(mm.group(2)), _MON.get(mm.group(1).lower(), 0))
        candidates = _glob.glob(os.path.join(SCRIPT_DIR, "portfolio_data_*.json"))
        # Exclude current month
        candidates = [c for c in candidates if month_label not in c]
        candidates = sorted(candidates, key=_mkey)
        prior_port_path = candidates[-1] if candidates else None

    errors   = []
    warnings = []
    summary  = {}
    flags    = []
    degraded = False   # True -> status downgraded to PARTIAL (step ran but data incomplete)
    watchlist_promotion_log = {}

    # ---------------------------------------------------------------------------
    # Step 1: Extract portfolio
    # ---------------------------------------------------------------------------
    print(f"\n[1/9] Extracting portfolio from xlsx...")
    ok, stdout, stderr = run_script(
        "extract_portfolio",
        ["--isa-folder", isa_folder, "--out", portfolio_path],
        dry_run=args.dry_run,
    )
    if not ok:
        msg = stderr or stdout or "Unknown error in extract_portfolio"
        errors.append(f"Step 1 (extract_portfolio): {msg}")
        print(f"  FAILED: {msg}")
    else:
        print(stdout.strip())
        valid, vmsg = validate_json_output(
            portfolio_path, ["_meta", "summary", "funds", "stocks", "cash"]
        )
        if not valid:
            errors.append(f"Step 1 validation: {vmsg}")
            print(f"  Validation FAILED: {vmsg}")
        else:
            val_ok, val_msg = validate_portfolio_value(portfolio_path)
            if not val_ok:
                errors.append(f"Step 1 sanity: {val_msg}")
                print(f"  Sanity check FAILED: {val_msg}")
            else:
                print(f"  Validation: {val_msg}")
                # Populate summary from portfolio
                with open(portfolio_path, encoding="utf-8") as f:
                    port_data = json.load(f)
                s = port_data["summary"]
                summary = {
                    "total_value_gbp":       s["total_value_gbp"],
                    "cash_effective_gbp":    s["cash_effective_gbp"],
                    "cash_deployable_gbp":   s["cash_deployable_gbp"],
                    "stock_sleeve_pct":      s["stock_sleeve_pct"],
                    "fund_sleeve_pct":       s["fund_sleeve_pct"],
                    "num_stocks":            s["num_stock_positions"],
                    "num_funds":             s["num_fund_positions"],
                    "data_date":             port_data["_meta"]["data_date"],
                    "source_file":           port_data["_meta"]["source_file"],
                }
                if port_data["flags"].get("concentration_over_12_5pct"):
                    flags.append({
                        "type": "CONCENTRATION",
                        "message": f"Position(s) over 12.5%: {port_data['flags']['concentration_over_12_5pct']}",
                    })

    # ---------------------------------------------------------------------------
    # Step 1.5: Reconcile prior recommendations vs broker truth (recommendations != executions).
    # The system never assumes a prior recommendation was executed; it confirms from THIS month's
    # actual holdings (broker file). Additive — no-op until a decision ledger exists.
    # ---------------------------------------------------------------------------
    if not errors and os.path.exists(portfolio_path):
        ledger_path = os.path.join(SCRIPT_DIR, "decision_ledger.json")
        if os.path.exists(ledger_path):
            print("\n[1.5] Reconciling prior decision-ledger recommendations vs broker holdings...")
            try:
                sys.path.insert(0, SCRIPT_DIR)
                import decision_ledger as _dl_mod
                with open(portfolio_path, encoding="utf-8") as _pf:
                    _pd = json.load(_pf)
                _held = {s.get("ticker"): s.get("quantity") for s in _pd.get("stocks", []) if s.get("ticker")}
                _prior_h = None
                if prior_port_path and os.path.exists(prior_port_path):
                    try:
                        with open(prior_port_path, encoding="utf-8") as _ppf:
                            _ppd = json.load(_ppf)
                        _prior_h = {s.get("ticker"): s.get("quantity") for s in _ppd.get("stocks", []) if s.get("ticker")}
                    except Exception:
                        _prior_h = None
                _rc = _dl_mod.reconcile_executions(ledger_path, _held, prior_holdings=_prior_h, date=run_date.isoformat())
                summary["ledger_reconcile"] = _rc
                print(f"  Reconciled prior recommendations vs broker truth: {_rc}")
            except Exception as _ex:
                warnings.append(f"Step 1.5 (ledger reconcile) skipped: {_ex}")
                print(f"  WARNING: {_ex}")

    # ---------------------------------------------------------------------------
    # Step 2: Extract X-Ray
    # ---------------------------------------------------------------------------
    print(f"\n[2/9] Extracting X-Ray from PDF...")
    ok, stdout, stderr = run_script(
        "extract_xray",
        ["--isa-folder", isa_folder, "--out", xray_path],
        dry_run=args.dry_run,
    )
    if not ok:
        msg = stderr or stdout or "Unknown error in extract_xray"
        # X-Ray is important but not fatal -- warn and continue
        warnings.append(f"Step 2 (extract_xray): {msg}")
        print(f"  WARNING: {msg}")
        # Create minimal xray JSON so downstream steps don't crash
        if not os.path.exists(xray_path):
            with open(xray_path, "w", encoding="utf-8") as f:
                json.dump({
                    "_meta": {"month_label": month_label, "report_date": "unknown"},
                    "_warning": "X-Ray extraction failed -- Claude must retrieve manually at Step 6",
                    "asset_allocation": {}, "country_exposure": [], "world_regions": {},
                    "sector_weights": {}, "trailing_returns": {}, "fund_holdings": [],
                }, f, indent=2)
    else:
        print(stdout.strip())
        valid, vmsg = validate_json_output(xray_path, ["_meta", "sector_weights"])
        if not valid:
            warnings.append(f"Step 2 validation: {vmsg}")
            print(f"  Validation WARNING: {vmsg}")
        else:
            print(f"  Validation: {vmsg}")
            if not errors:  # only if portfolio step succeeded
                with open(xray_path, encoding="utf-8") as f:
                    xray_data = json.load(f)
                tr = xray_data.get("trailing_returns", {})
                if "1yr" in tr:
                    r = tr["1yr"]
                    summary["xray_1yr_return_pct"] = r.get("portfolio_pct")
                    summary["xray_1yr_benchmark_pct"] = r.get("benchmark_pct")

    # ---------------------------------------------------------------------------
    # Step 3: Analytics
    # ---------------------------------------------------------------------------
    print(f"\n[3/9] Running portfolio analytics...")
    if errors:
        print("  SKIPPED -- portfolio extraction failed (required input).")
        warnings.append("Step 3 (analytics) skipped -- portfolio extraction failed.")
    else:
        analytics_args = [
            "--portfolio", portfolio_path,
            "--out",       analytics_path,
        ]
        if prior_port_path:
            analytics_args += ["--prior-portfolio", prior_port_path]
        if trades_log_path:
            analytics_args += ["--trades-log", trades_log_path]

        ok, stdout, stderr = run_script(
            "analytics", analytics_args, dry_run=args.dry_run
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in portfolio_analytics"
            errors.append(f"Step 3 (analytics): {msg}")
            print(f"  FAILED: {msg}")
        else:
            print(stdout.strip())
            valid, vmsg = validate_json_output(
                analytics_path,
                ["_meta", "fund_drift_table", "phase_status", "capital_summary"]
            )
            if not valid:
                errors.append(f"Step 3 validation: {vmsg}")
                print(f"  Validation FAILED: {vmsg}")
            else:
                print(f"  Validation: {vmsg}")
                with open(analytics_path, encoding="utf-8") as f:
                    ana_data = json.load(f)
                phase = ana_data.get("phase_status", {})
                summary["phase_status"] = phase.get("status")
                summary["rebalancing_candidates"] = len(ana_data.get("rebalancing_candidates", []))
                flags.extend(ana_data.get("flags", []))

    # ---------------------------------------------------------------------------
    # Step 4: Update watchlist (promotion/removal/score-delta)
    # ---------------------------------------------------------------------------
    print(f"\n[4/9] Updating watchlist via update_watchlist.py...")
    if errors:
        print("  SKIPPED -- prior step(s) failed.")
        warnings.append("Step 4 (update_watchlist) skipped -- prior step failures.")
    elif not os.path.exists(watchlist_config_path):
        warnings.append("Step 4 (update_watchlist): watchlist_tickers.json not found -- skipping.")
        print("  WARNING: watchlist_tickers.json not found -- skipped.")
    else:
        ok, stdout, stderr = run_script(
            "update_watchlist_py",
            [
                "--portfolio-data", portfolio_path,
                "--watchlist-json", watchlist_config_path,
                "--inv-dir",        SCRIPT_DIR,
                "--out-json",       watchlist_config_path,
            ],
            dry_run=args.dry_run,
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in update_watchlist"
            warnings.append(f"Step 4 (update_watchlist): {msg}")
            print(f"  WARNING: {msg}")
        else:
            print(stdout.strip())
            try:
                import json as _json
                for line in stdout.splitlines():
                    if line.strip().startswith("{") and "additions" in line:
                        watchlist_promotion_log = _json.loads(line)
                        break
            except Exception:
                pass
            if watchlist_promotion_log:
                n_add = len(watchlist_promotion_log.get("additions", []))
                n_rem = len(watchlist_promotion_log.get("removals", []))
                n_upd = len(watchlist_promotion_log.get("score_updates", []))
                print(f"  Watchlist updated: +{n_add} added | -{n_rem} removed | {n_upd} score updates")
            # Guardrail: a SUMMARY tab that parses to zero rows is a silent parser failure.
            n_files = len(watchlist_promotion_log.get("xlsx_files_read", []))
            n_rows  = watchlist_promotion_log.get("rows_parsed", 0)
            if n_files == 0:
                warnings.append("Step 4 guardrail: no Growth Stock Analysis xlsx found in working dir or month archive -- no candidates ingested.")
                degraded = True
            elif n_rows == 0:
                warnings.append(f"Step 4 guardrail: {n_files} analysis file(s) read but 0 candidate rows parsed -- check SUMMARY tab headers.")
                degraded = True
            for _k in ("sleeve_phantom_removed", "sleeve_added", "held_removed_from_watchlist", "held_removed_from_vci"):
                _v = watchlist_promotion_log.get(_k, [])
                if _v:
                    print(f"  Reconcile [{_k}]: {[x.get('ticker') for x in _v]}")

    # ---------------------------------------------------------------------------
    # Step 5: Sync VCI watchlist from memory file + refresh Part A scores
    # ---------------------------------------------------------------------------
    print(f"\n[5/9] Syncing VCI watchlist from project_isa_vci_watchlist.md...")
    vci_md_path = find_memory_file("project_isa_vci_watchlist.md")
    if not vci_md_path:
        warnings.append("Step 5 (sync_vci_watchlist): project_isa_vci_watchlist.md not found at resolved MEMORY_BASE -- VCI watchlist not synced. (Held names are still removed at Step 4.)")
        print(f"  WARNING: project_isa_vci_watchlist.md not found -- skipped. MEMORY_BASE={MEMORY_BASE}")
        degraded = True
    elif not os.path.exists(watchlist_config_path):
        warnings.append("Step 5 (sync_vci_watchlist): watchlist_tickers.json not found -- skipped.")
        print(f"  WARNING: watchlist_tickers.json not found -- skipped.")
    else:
        ok, stdout, stderr = run_script(
            "sync_vci_watchlist",
            [
                "--watchlist-md",   vci_md_path,
                "--watchlist-json", watchlist_config_path,
                "--inv-dir",        SCRIPT_DIR,
                "--portfolio-data", portfolio_path,
            ],
            dry_run=args.dry_run,
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in sync_vci_watchlist"
            warnings.append(f"Step 5 (sync_vci_watchlist): {msg}")
            print(f"  WARNING: {msg}")
        else:
            print(stdout.strip())

    # ---------------------------------------------------------------------------
    # Step 6: Fetch watchlist + stock sleeve metrics (yfinance pull)
    # ---------------------------------------------------------------------------
    print(f"\n[6/9] Fetching watchlist & sleeve metrics (yfinance)...")
    if not os.path.exists(watchlist_config_path):
        warnings.append("Step 6: watchlist_tickers.json not found -- skipping metrics pull. "
                        "Create it in Investment Analysis folder.")
        print(f"  WARNING: watchlist_tickers.json not found -- skipped.")
        # Create empty placeholder so downstream steps don't crash
        with open(watchlist_metrics_path, "w", encoding="utf-8") as f:
            json.dump({"_meta": {"month_label": month_label}, "tickers": {},
                       "_warning": "watchlist_tickers.json missing -- metrics not pulled"}, f)
    elif errors:
        print("  SKIPPED -- prior step(s) failed.")
        warnings.append("Step 6 (fetch_watchlist) skipped -- prior step failures.")
        with open(watchlist_metrics_path, "w", encoding="utf-8") as f:
            json.dump({"_meta": {"month_label": month_label}, "tickers": {}}, f)
    else:
        ok, stdout, stderr = run_script(
            "fetch_watchlist",
            [
                "--watchlist",    watchlist_config_path,
                "--out",          watchlist_metrics_path,
                "--month-label",  month_label,
            ],
            dry_run=args.dry_run,
        )
        # Watchlist pull is non-fatal (yfinance may fail for some tickers)
        if not ok:
            msg = stderr or stdout or "Unknown error in fetch_watchlist_metrics"
            # ARCHITECTURE: fetch_watchlist_metrics.py runs in Composio (remote) and the
            # metrics JSON is transferred to this folder out of band. A local yfinance
            # ImportError is EXPECTED and benign IF a populated metrics file is already
            # present. Only degrade when NO usable metrics exist (transfer never happened).
            metrics_n = 0
            metrics_tickers = []
            if os.path.exists(watchlist_metrics_path):
                try:
                    with open(watchlist_metrics_path, encoding="utf-8") as f:
                        _m = json.load(f)
                    metrics_tickers = list(_m.get("tickers", {}).keys())
                    metrics_n = len(metrics_tickers)
                except Exception:
                    metrics_n = 0
            if metrics_n > 0:
                # Check the transferred metrics actually cover the CURRENT ticker set.
                try:
                    with open(watchlist_config_path, encoding="utf-8") as f:
                        _wl = json.load(f)
                    needed = ({e.get("ticker") for e in _wl.get("watchlist", [])}
                              | {e.get("ticker") for e in _wl.get("vci_watchlist", [])}
                              | {s.get("ticker") for s in _wl.get("stock_sleeve", [])}
                              | {p.get("ticker") for p in _wl.get("candidate_pool", [])})
                    missing = sorted(t for t in needed if t and t not in set(metrics_tickers))
                except Exception:
                    missing = []
                print(f"  NOTE: local yfinance unavailable (expected) -- using Composio-transferred metrics ({metrics_n} tickers).")
                if missing:
                    warnings.append(f"Step 6: using Composio-transferred metrics ({metrics_n} tickers), but "
                                    f"{len(missing)} current name(s) are NOT covered and will be unscored: {missing[:20]}"
                                    + (" ..." if len(missing) > 20 else ""))
                    degraded = True
            else:
                warnings.append(f"Step 6 (fetch_watchlist): {msg} AND no Composio-transferred metrics file present "
                                "-- downstream scoring will be empty. Run the Composio metrics pull + transfer.")
                print(f"  WARNING: {msg} (no metrics available)")
                degraded = True
                if not os.path.exists(watchlist_metrics_path):
                    with open(watchlist_metrics_path, "w", encoding="utf-8") as f:
                        json.dump({"_meta": {"month_label": month_label}, "tickers": {},
                                   "_warning": "fetch failed and no transfer: " + msg}, f)
        else:
            print(stdout.strip())
            valid, vmsg = validate_json_output(watchlist_metrics_path, ["_meta", "tickers"])
            if not valid:
                warnings.append("Step 6 validation: " + vmsg)
                print("  Validation WARNING: " + vmsg)
            else:
                with open(watchlist_metrics_path, encoding="utf-8") as f:
                    wm_data = json.load(f)
                n_scored = len(wm_data.get("tickers", {}))
                in_window = wm_data.get("_meta", {}).get("in_window_tickers", [])
                summary["watchlist_tickers_scored"] = n_scored
                summary["in_window_names"] = in_window
                print("  Scored " + str(n_scored) + " tickers | In-window: " + str(in_window))

    # ---------------------------------------------------------------------------
    # Step 6.5: VCI forward-led re-price (§11.3 / §14.2)
    #   The VCI run scored/ranked on the 2nd-Sunday price; price drifts by the Saturday
    #   pre-run, so a stale fv_asymmetry is a WRONG deployment gate. Recompute fv_asymmetry
    #   and VCI_Source_Score for every vci_watchlist name at the CURRENT (Saturday) live price
    #   via vci_deploy_eval, re-rank by VCI_Source_Score, and write the fields back into
    #   watchlist_tickers.json so Step 8 (step9_pre_builder) consumes fresh, not stale, values.
    #   ACS is NOT re-scored here (stickier); only the price-driven terms refresh.
    # ---------------------------------------------------------------------------
    print(f"\n[6.5] VCI forward-led re-price (fv_asymmetry / VCI_Source_Score at live price)...")
    if not os.path.exists(watchlist_config_path):
        print("  SKIPPED -- watchlist_tickers.json not found.")
    else:
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import vci_deploy_eval as _vde
            try:
                import scoring_config as _sc
            except Exception:
                _sc = None

            with open(watchlist_config_path, encoding="utf-8") as f:
                _wt = json.load(f)
            _vci = _wt.get("vci_watchlist", []) or []

            # live-price lookup from the Step-6 metrics pull (current_price), with fallbacks
            _px = {}
            if os.path.exists(watchlist_metrics_path):
                try:
                    with open(watchlist_metrics_path, encoding="utf-8") as f:
                        _wm = json.load(f)
                    for _t, _row in (_wm.get("tickers", {}) or {}).items():
                        _v = (_row or {}).get("current_price")
                        if _v is None:
                            _pr = (_row or {}).get("_prices") or {}
                            _v = _pr.get("current") or _pr.get("last") or _pr.get("close")
                        if _v is not None:
                            _px[str(_t).upper()] = float(_v)
                except Exception as _e:
                    print(f"  NOTE: could not read live prices from metrics ({_e}); using stored fallbacks.")

            def _base_t(t):
                t = str(t or "").upper()
                return t.split(".")[0] if "." in t else t

            def _price_lookup(t):
                tu = str(t or "").upper()
                if tu in _px:
                    return _px[tu]
                bt = _base_t(tu)
                for k, v in _px.items():
                    if _base_t(k) == bt:
                        return v
                # last-resort fallback: the entry's own stored price / entry level
                for e in _vci:
                    if str(e.get("ticker", "")).upper() == tu:
                        return e.get("price") or e.get("entry_level")
                return None

            if _vci:
                # normalise the acs field vci_deploy_eval expects (entries store acs_score)
                for e in _vci:
                    if e.get("acs") is None:
                        e["acs"] = e.get("acs_score")
                # v2: portfolio value for E5 liquidity sizing (best-effort; None -> no cap)
                _pv = None
                try:
                    if os.path.exists(portfolio_path):
                        with open(portfolio_path, encoding="utf-8") as _pf:
                            _pd = json.load(_pf)
                        _pv = _pd.get("total_value") or _pd.get("portfolio_value") \
                              or (_pd.get("summary", {}) or {}).get("total_value")
                except Exception:
                    _pv = None
                _ranked = _vde.refresh_at_live_price(_vci, price_lookup=_price_lookup, portfolio_value=_pv)
                # v2 E4: sleeve binary risk-budget headroom over the deploy-eligible set
                try:
                    import vci_risk_budget as _vrb
                    _open = [e for e in _ranked if e.get("deploy_eligible")]
                    summary["vci_binary_risk_committed"] = _vrb.committed_risk(_open)
                    summary["vci_binary_risk_budget"] = getattr(_sc, "VCI_SLEEVE_BINARY_RISK_BUDGET", None) if _sc else None
                except Exception:
                    pass
                # write recomputed deployability fields back, preserve one canonical order
                for i, e in enumerate(_ranked, 1):
                    e["vci_rank"] = i
                _wt["vci_watchlist"] = _ranked
                if not args.dry_run:
                    with open(watchlist_config_path, "w", encoding="utf-8") as f:
                        json.dump(_wt, f, indent=2, default=str)
                _elig = [e for e in _ranked if e.get("deploy_eligible")]
                print(f"  Re-priced {len(_ranked)} VCI name(s); {len(_elig)} deploy-eligible. "
                      f"Top by VCI_Source_Score: "
                      + ", ".join(f"{e.get('ticker')}({e.get('vci_source_score')})" for e in _ranked[:3]))
                summary["vci_repriced"] = len(_ranked)
                summary["vci_deploy_eligible"] = [e.get("ticker") for e in _elig]
            else:
                print("  No VCI watchlist names to re-price.")

            # surface calibration state (read-only) if the learning module has produced one
            _cal_path = getattr(_sc, "VCI_CALIBRATION_STATE_PATH", None) if _sc else None
            if _cal_path and os.path.exists(_cal_path):
                try:
                    with open(_cal_path, encoding="utf-8") as f:
                        _cal = json.load(f)
                    summary["vci_calibration_state"] = {
                        "calibration_gate_passed": _cal.get("calibration_gate_passed", False),
                        "resolved_outcomes": _cal.get("resolved_outcomes"),
                        "weights": _cal.get("weights"),
                    }
                    print(f"  VCI calibration state: gate_passed="
                          f"{_cal.get('calibration_gate_passed', False)} (read-only, advisory).")
                except Exception:
                    pass
        except Exception as e:
            warnings.append(f"Step 6.5 (VCI re-price): {e} -- VCI names carry forward stale deployability fields.")
            print(f"  WARNING: VCI re-price failed ({e}); carrying forward stored fields.")

    # ---------------------------------------------------------------------------
    # Step 7: Score Part A/B
    # ---------------------------------------------------------------------------
    print(f"\n[7/9] Scoring Part A/B and building email structures...")
    if not os.path.exists(watchlist_metrics_path):
        warnings.append("Step 7: watchlist_metrics JSON missing -- skipping scorer.")
        print("  WARNING: metrics file missing -- skipped.")
        with open(watchlist_scored_path, "w", encoding="utf-8") as f:
            json.dump({"_meta": {}, "conviction_ranking": [], "s5_watchlist_rows": [],
                       "s7_sleeve_rows": [], "s3_case_skeletons": []}, f)
    else:
        ok, stdout, stderr = run_script(
            "normalise_adapter",
            ["--metrics", watchlist_metrics_path, "--out", watchlist_scored_path],
            dry_run=args.dry_run,
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in normalise_adapter"
            warnings.append("Step 7 (normalise_adapter): " + msg)
            print("  WARNING: " + msg)
            if not os.path.exists(watchlist_scored_path):
                with open(watchlist_scored_path, "w", encoding="utf-8") as f:
                    json.dump({"_meta": {}, "conviction_ranking": [], "s5_watchlist_rows": [],
                               "s7_sleeve_rows": [], "s3_case_skeletons": []}, f)
        else:
            print(stdout.strip())
            valid, vmsg = validate_json_output(
                watchlist_scored_path,
                ["conviction_ranking", "s5_watchlist_rows", "s7_sleeve_rows"]
            )
            if not valid:
                warnings.append("Step 7 validation: " + vmsg)
                print("  Validation WARNING: " + vmsg)
            else:
                print("  Validation: " + vmsg)

    # ---------------------------------------------------------------------------
    # Step 7.25: Build / refresh composite entry levels (before tiering)
    # ---------------------------------------------------------------------------
    # entry_level_builder.py creates governed provisional entry levels for every
    # watchlist + candidate_pool name BEFORE rerank/step9 tier on price-vs-entry,
    # so high-scoring names with no manual entry no longer fall straight to T3.
    print(f"\n[7.25] Building composite entry levels...")
    if not os.path.exists(watchlist_metrics_path) or not os.path.exists(watchlist_config_path):
        warnings.append("Step 7.25 (entry_level_builder) skipped -- metrics or watchlist file missing.")
        print("  SKIPPED -- metrics or watchlist file missing.")
    else:
        ok, stdout, stderr = run_script(
            "entry_level_builder",
            ["--metrics",     watchlist_metrics_path,
             "--watchlist",   watchlist_config_path, "--watchlist-out", watchlist_config_path,
             "--scored",      watchlist_scored_path, "--scored-out",    watchlist_scored_path,
             "--audit-out",   entry_audit_path,
             "--month-label", month_label],
            dry_run=args.dry_run,
        )
        if not ok:
            warnings.append(f"Step 7.25 (entry_level_builder): {stderr or stdout}")
            print(f"  WARNING: {stderr or stdout}")
        else:
            print(stdout.strip())

    # ---------------------------------------------------------------------------
    # Inject recorded AI-disruption scores onto the scored tickers (after entry-level, before rerank/
    # step9) so deployment_flags can cap an existential name (E4 -> E3 -> E1). Additive; no-op until
    # assessments exist in ai_disruption.json.
    # ---------------------------------------------------------------------------
    _ai_store = os.path.join(SCRIPT_DIR, "ai_disruption.json")
    if os.path.exists(_ai_store) and os.path.exists(watchlist_scored_path):
        try:
            sys.path.insert(0, SCRIPT_DIR)
            import ai_disruption as _ai_mod
            with open(watchlist_scored_path, encoding="utf-8") as _sf:
                _scored = json.load(_sf)
            _n = 0
            for _t, _td in (_scored.get("tickers") or {}).items():
                _a = _ai_mod.get_assessment(_ai_store, _t)
                if _a and _a.get("score") is not None:
                    _td["ai_disruption_score"] = _a["score"]
                    _n += 1
            if _n:
                with open(watchlist_scored_path, "w", encoding="utf-8") as _sf:
                    json.dump(_scored, _sf, indent=2, ensure_ascii=False, default=str)
                print(f"  Injected AI-disruption scores onto {_n} scored ticker(s).")
        except Exception as _ex:
            warnings.append(f"AI-disruption injection skipped: {_ex}")

    # ---------------------------------------------------------------------------
    # Step 7.5: Re-rank the watchlist on LIVE re-scored values
    # ---------------------------------------------------------------------------
    # After fetch (Composio) + normalise_adapter produce live Part A/B for the watchlist
    # AND the candidate_pool, re-rank the top-10 on the live normalised score so the
    # watchlist reflects fresh data, not stale screening scores. step9_pre_builder
    # (below) reads the re-ranked watchlist_tickers.json, so downstream stays consistent.
    print(f"\n[7.5] Re-ranking watchlist on live re-scored values...")
    if not os.path.exists(watchlist_scored_path) or not os.path.exists(watchlist_config_path):
        warnings.append("Step 7.5 (rerank_watchlist) skipped -- scored or watchlist file missing.")
        print("  SKIPPED -- scored or watchlist file missing.")
    else:
        # Guard: don't re-rank off an empty/failed metrics scoring (would null the watchlist).
        try:
            with open(watchlist_scored_path, encoding="utf-8") as _f:
                _sc = json.load(_f)
            _live = sum(1 for e in _sc.get("conviction_ranking", [])
                        if (e.get("total_score_54") or e.get("total_score_50") or e.get("total_score_36")) is not None)
        except Exception:
            _live = 0
        if _live == 0:
            warnings.append("Step 7.5 (rerank): no live scores in conviction_ranking -- skipping re-rank "
                            "(watchlist left on screening-score order). Metrics likely not yet transferred.")
            print("  SKIPPED -- no live scores present (run the Composio metrics pull first).")
            degraded = True
        else:
            ok, stdout, stderr = run_script(
                "rerank_watchlist",
                ["--scored", watchlist_scored_path, "--watchlist", watchlist_config_path,
                 "--metrics", watchlist_metrics_path],
                dry_run=args.dry_run,
            )
            if not ok:
                warnings.append(f"Step 7.5 (rerank_watchlist): {stderr or stdout}")
                print(f"  WARNING: {stderr or stdout}")
            else:
                print(stdout.strip())

    # ---------------------------------------------------------------------------
    # H1 fix — RE-inject AI-disruption scores after rerank. rerank's end-of-run refresh regenerates
    # watchlist_scored from the metrics (which don't carry the AI score), dropping the pre-rerank
    # injection — so step9's gate_flags would lose ai_existential. Re-stamp here, before step9 reads it.
    # ---------------------------------------------------------------------------
    if os.path.exists(_ai_store) and os.path.exists(watchlist_scored_path):
        try:
            sys.path.insert(0, SCRIPT_DIR)
            import ai_disruption as _ai_mod
            with open(watchlist_scored_path, encoding="utf-8") as _sf:
                _scored = json.load(_sf)
            _n = 0
            for _t, _td in (_scored.get("tickers") or {}).items():
                _a = _ai_mod.get_assessment(_ai_store, _t)
                if _a and _a.get("score") is not None:
                    _td["ai_disruption_score"] = _a["score"]
                    _n += 1
            if _n:
                with open(watchlist_scored_path, "w", encoding="utf-8") as _sf:
                    json.dump(_scored, _sf, indent=2, ensure_ascii=False, default=str)
        except Exception as _ex:
            warnings.append(f"AI-disruption re-injection (post-rerank) skipped: {_ex}")

    # ---------------------------------------------------------------------------
    # Step 8: Build Step 9 pre-scored output
    # ---------------------------------------------------------------------------
    print(f"\n[8/9] Building Step 9 pre-scored output...")
    if not os.path.exists(watchlist_scored_path):
        warnings.append("Step 8 (step9_pre_builder): watchlist_scored JSON missing -- skipping.")
        print(f"  WARNING: watchlist_scored JSON missing -- skipped.")
    else:
        ok, stdout, stderr = run_script(
            "step9_pre_builder",
            [
                "--scored",      watchlist_scored_path,
                "--watchlist",   watchlist_config_path,
                "--month-label", month_label,
                "--out",         step9_pre_path,
            ],
            dry_run=args.dry_run,
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in step9_pre_builder"
            warnings.append(f"Step 8 (step9_pre_builder): {msg}")
            print(f"  WARNING: {msg}")
        else:
            print(stdout.strip())
            valid, vmsg = validate_json_output(
                step9_pre_path,
                ["_meta", "main_watchlist", "vci_watchlist", "candidate_pool", "deployment_priority_rank"]
            )
            if not valid:
                warnings.append(f"Step 8 validation: {vmsg}")
                print(f"  Validation WARNING: {vmsg}")
            else:
                print(f"  Validation: {vmsg}")

    # ---------------------------------------------------------------------------
    # Step 9: Email prefill
    # ---------------------------------------------------------------------------
    print(f"\n[9/9] Pre-populating email JSON...")
    if errors:
        print("  SKIPPED -- prior step(s) failed.")
        warnings.append("Step 9 (email_prefill) skipped -- prior step failures.")
    else:
        ok, stdout, stderr = run_script(
            "email_prefill",
            ["--portfolio", portfolio_path, "--analytics", analytics_path,
             "--xray", xray_path, "--scored", watchlist_scored_path, "--out", email_path],
            dry_run=args.dry_run,
        )
        if not ok:
            msg = stderr or stdout or "Unknown error in email_prefill"
            warnings.append("Step 9 (email_prefill): " + msg)
            print("  WARNING: " + msg)
        else:
            print(stdout.strip())
            valid, vmsg = validate_json_output(
                email_path,
                ["meta", "s6_portfolio_snapshot", "s7_stock_sleeve", "s8_fund_review"]
            )
            if not valid:
                warnings.append("Step 9 validation: " + vmsg)
                print("  Validation WARNING: " + vmsg)
            else:
                print("  Validation: " + vmsg)

    # ---------------------------------------------------------------------------
    # Step 9c (Jul-26 Part 9): Calibration report — surface each signal's forward-return IC by horizon
    # (1m/3m/6m/12m, filling left-to-right as the score panel matures). Evidence only; never blocks.
    # ---------------------------------------------------------------------------
    print(f"\n[cal] Calibration report (signal IC by horizon)...")
    calib_report_path = os.path.join(SCRIPT_DIR, f"calibration_report_{month_label}.md")
    calib_summary = {}
    try:
        _panel_store = os.path.join(SCRIPT_DIR, "score_panel.csv")
        if os.path.exists(_panel_store):
            ok, stdout, stderr = run_script(
                "calibration_report",
                ["--store", _panel_store, "--asof", run_date.isoformat(), "--out", calib_report_path],
                dry_run=args.dry_run,
            )
            if ok:
                calib_summary = {"report_path": calib_report_path, "ic_table": stdout.strip()}
                print(stdout.strip()[:800])
            else:
                warnings.append("Calibration report: " + (stderr or stdout or "unknown error"))
                print("  WARNING: " + (stderr or stdout or "unknown error"))
        else:
            calib_summary = {"note": "score_panel.csv not present yet -- screens begin logging it this cycle."}
            print("  SKIPPED -- no score_panel.csv yet (screens start logging the panel this cycle).")
    except Exception as _ce:
        warnings.append("Calibration report step: " + str(_ce))
        print("  WARNING: calibration step: " + str(_ce))
    summary["calibration"] = calib_summary

    # Write run_context
    if errors:
        status = "ERROR" if len(errors) >= 2 else "PARTIAL"
    elif degraded:
        status = "PARTIAL"
    else:
        status = "OK"
    error_msg = "; ".join(errors) if errors else ""
    print("Writing run_context_" + month_label + ".json...")
    if watchlist_promotion_log:
        summary["watchlist_promotion_log"] = watchlist_promotion_log

    ctx_path = write_run_context(
        month_label, run_month, portfolio_path, xray_path, analytics_path,
        watchlist_metrics_path, watchlist_scored_path, step9_pre_path, email_path,
        summary, flags, warnings, status, error_msg,
    )
    print("  Written: " + ctx_path)

    print("=" * 65)
    print("Pre-Run Complete  |  Status: " + status + "  |  " + datetime.now().strftime("%H:%M"))
    print("=" * 65)

    if summary.get("total_value_gbp"):
        print("  Portfolio:     " + str(round(summary["total_value_gbp"], 2)))
        print("  Stock sleeve:  " + str(summary.get("stock_sleeve_pct", "?")))
        print("  Phase status:  " + str(summary.get("phase_status", "?")))

    if warnings:
        print("  Warnings: " + str(len(warnings)))
        for w in warnings:
            print("    - " + w)

    if errors:
        print("  ERRORS -- review task will be blocked:")
        for e in errors:
            print("    x " + e)
        sys.exit(1)

    print("  All outputs staged. Review task ready to run tomorrow (Sunday morning).")
    print("  Review task reads: " + ctx_path)


if __name__ == "__main__":
    main()
