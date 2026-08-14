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

from datetime import date as dt_date
import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import json
import datetime
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Scripts (same folder as this orchestrator)
SCRIPTS = {
    "extract_portfolio":      os.path.join(SCRIPT_DIR, "extract_portfolio.py"),
    "extract_xray":           os.path.join(SCRIPT_DIR, "extract_xray.py"),
    "extract_transactions":   os.path.join(SCRIPT_DIR, "extract_transactions.py"),  # Step 1b (26-Jul-26)
    "extract_cash_statement": os.path.join(SCRIPT_DIR, "extract_cash_statement.py"),  # Step 1b-2 (05-Aug-26)
    "fund_action_stack":      os.path.join(SCRIPT_DIR, "fund_action_stack.py"),      # Step 6.05 (05-Aug-26)
    "lookthrough":            os.path.join(SCRIPT_DIR, "lookthrough.py"),            # Step 6.06 (06-Aug-26)
    "concentration_clusters": os.path.join(SCRIPT_DIR, "concentration_clusters.py"),  # Step 6.07 (06-Aug-26)
    "return_architecture":    os.path.join(SCRIPT_DIR, "return_architecture.py"),     # Step 6.08 (06-Aug-26)
    "holding_period_return":  os.path.join(SCRIPT_DIR, "holding_period_return.py"),  # Tier-1 item 1
    "derive_required_return": os.path.join(SCRIPT_DIR, "derive_required_return.py"),
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
    candidates = []
    # 01-Aug-26 ROOT-CAUSE FIX: the pre-run executes in the LINUX sandbox, where HOME is
    # /sessions/<session> and the Windows AppData tree below does NOT exist -- so every
    # candidate failed and MEMORY_BASE pointed at nothing. The memory dir IS mounted, at
    # /sessions/<session>/mnt/.auto-memory. The earlier "fix" (dropping the '..' climb out
    # of the OneDrive tree) corrected the wrong environment and never took effect. The
    # consequence was NOT limited to the visible Step 5 skip: find_memory_file
    # ("project_isa_trades_log.md") also returned None SILENTLY, so analytics ran with no
    # purchase dates. Sandbox-native locations are probed FIRST; the Windows paths remain
    # for a native/desktop invocation.
    if os.environ.get("ISA_MEMORY_DIR"):
        candidates.append(os.environ["ISA_MEMORY_DIR"])
    candidates += sorted(_g.glob("/sessions/*/mnt/.auto-memory"))
    # SCRIPT_DIR is <mnt>/ISA/Investment Analysis -> ../../.auto-memory
    candidates.append(os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", ".auto-memory")))
    candidates.append(os.path.join(base, "5240c546-04fc-4dfa-9e3c-ac4943abb0ca",
                                   "f7637f5f-1fa6-4075-a7d9-50bc4a878712",
                                   "spaces", SPACE, "memory"))
    candidates += _g.glob(os.path.join(base, "*", "*", "spaces", SPACE, "memory"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


MEMORY_BASE = _resolve_memory_base()


def _previous_month_label(month_label: str) -> str:
    """'sep_2026' -> 'aug_2026'. MOA is retrospective: it attributes the month whose
    decisions have already been made, never the one being prepared."""
    _m = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]
    mon, _, yr = month_label.lower().partition("_")
    if mon not in _m or not yr.isdigit():
        raise ValueError(f"unrecognised month label {month_label!r} - refusing to guess (R4.1)")
    i = _m.index(mon)
    return f"{_m[i - 1]}_{int(yr) - 1}" if i == 0 else f"{_m[i - 1]}_{yr}"


def find_memory_file(pattern: str) -> str | None:
    """Find latest memory file matching a glob-style prefix."""
    import glob as _glob
    candidates = _glob.glob(os.path.join(MEMORY_BASE, pattern))
    return max(candidates, default=None, key=os.path.getmtime) if candidates else None


# ---------------------------------------------------------------------------
# Run a script as a subprocess
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CAPTURE LAYER ITEM 2 (Dashboard Spec 7.6A) — run manifest + fail-loud.
#
# Instrumentation is CENTRAL, not per-call-site: every pre-run step goes through run_script or
# run_script_rc, so recording there captures duration, exit code and output size for all of
# them without 16 opportunities to forget one. Steps declare rows/coverage explicitly via
# _mf_measure() where they know it.
#
# The stage `print(f"\n[N/9] ...")` lines are deliberately left untouched — consistency_check
# pair M5 parses them to prove every executed stage has a Run_Context table row. _mf_begin()
# sits alongside them rather than replacing them.
# ---------------------------------------------------------------------------
try:
    import run_manifest as _RM
except Exception:                                    # pragma: no cover
    _RM = None

MANIFEST = None          # run_manifest.Manifest, created in main()
_MF_CUR = None           # {"id","name","t0","rows_in","rows_out","coverage","non_null","notes"}


def _mf_begin(step_id, name):
    """Open a manifest step. Closing the previous one first means a step that never reached
    its own _mf_end (an early return, an exception swallowed downstream) is recorded as it
    actually was, rather than vanishing."""
    global _MF_CUR
    if MANIFEST is None:
        return
    _mf_end()
    _MF_CUR = {"id": str(step_id), "name": name, "t0": time.time(), "rows_in": None,
               "rows_out": None, "coverage": None, "non_null": None, "notes": [],
               "status": None}


def _mf_measure(rows_in=None, rows_out=None, coverage=None, non_null_share=None,
                note=None, status=None):
    """Declare what the current step actually produced. A step that declares nothing is
    recorded DEGRADED by run_manifest — silence is no longer indistinguishable from success."""
    if _MF_CUR is None:
        return
    if rows_in is not None:
        _MF_CUR["rows_in"] = rows_in
    if rows_out is not None:
        _MF_CUR["rows_out"] = rows_out
    if coverage is not None:
        _MF_CUR["coverage"] = coverage
    if non_null_share is not None:
        _MF_CUR["non_null"] = non_null_share
    if note:
        _MF_CUR["notes"].append(str(note)[:300])
    if status:
        _MF_CUR["status"] = status


def _mf_probe_json(path, count_path=None, non_null_key=None, note=None, add=False):
    """Declare a step's output from the file it just wrote.

    Every step used to end without saying what it produced, and run_manifest correctly
    reports that as DEGRADED — but if 11 of 16 steps are permanently amber then amber
    conveys nothing and the alarm has been rebuilt in a new colour. The remedy is to make
    the steps declare, not to soften the rule.

    count_path: dotted path to a list, e.g. "country_exposure" or "fund_drift_table.rows".
    non_null_key: key within each element that must be present for the row to count as real.
    """
    try:
        if not os.path.exists(path):
            _mf_measure(rows_out=0, note=f"{os.path.basename(path)} not written")
            return
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        node = d
        if count_path:
            for part in count_path.split("."):
                node = (node or {}).get(part) if isinstance(node, dict) else None
        rows = node if isinstance(node, list) else (list(node) if isinstance(node, dict) else [])
        n = len(rows)
        nn = None
        if non_null_key and isinstance(node, list) and n:
            nn = sum(1 for e in rows if isinstance(e, dict)
                     and e.get(non_null_key) is not None) / n
        if add and _MF_CUR is not None and _MF_CUR.get("rows_out"):
            n += _MF_CUR["rows_out"]
        _mf_measure(rows_out=n, non_null_share=nn, note=note or f"{count_path or 'root'}={n}")
    except Exception as e:
        _mf_measure(note=f"probe failed on {os.path.basename(path)}: {e}")


def _mf_end():
    global _MF_CUR
    if MANIFEST is None or _MF_CUR is None:
        return
    c = _MF_CUR
    _MF_CUR = None
    MANIFEST.record(c["id"], c["name"], status=c["status"], rows_in=c["rows_in"],
                    rows_out=c["rows_out"], coverage=c["coverage"],
                    non_null_share=c["non_null"], duration_s=round(time.time() - c["t0"], 2),
                    notes=c["notes"])


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
        _t0 = time.time()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        success = result.returncode == 0
        _mf_measure(note=f"{name} rc={result.returncode} in {time.time()-_t0:.1f}s",
                    status=(None if success else "ERROR"))
        return success, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        _mf_measure(note=f"{name} TIMEOUT after 120s", status="ERROR")
        return False, "", f"Script timed out after 120s: {name}"
    except Exception as e:
        _mf_measure(note=f"{name} raised {type(e).__name__}: {e}", status="ERROR")
        return False, "", str(e)


def run_script_rc(name: str, args: list[str], dry_run: bool = False):
    """As run_script, but returns the RETURN CODE rather than a bool.

    Needed by Step 1b: extract_transactions exits 3 when no transaction export
    exists, which is a degradation (fall back to holdings-delta inference), not
    a failure. Collapsing that to False would make a routine "Raj hasn't saved
    the file yet" indistinguishable from a crash."""
    script_path = SCRIPTS[name]
    if not os.path.exists(script_path):
        return 127, "", f"Script not found: {script_path}"
    cmd = [sys.executable, script_path] + args
    if dry_run:
        print(f"  [DRY RUN] Would run: {' '.join(cmd)}")
        return 0, "[dry run]", ""
    try:
        _t0 = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        _mf_measure(note=f"{name} rc={result.returncode} in {time.time()-_t0:.1f}s")
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        _mf_measure(note=f"{name} TIMEOUT after 120s", status="ERROR")
        return 124, "", f"Script timed out after 120s: {name}"
    except Exception as e:
        _mf_measure(note=f"{name} raised {type(e).__name__}: {e}", status="ERROR")
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def metrics_coverage(metrics_path: str, watchlist_path: str) -> tuple[int, list[str]]:
    """(n_scored, missing_tickers) for a metrics file against the CURRENT watchlist config.

    Shared by the Step 6 idempotence check and the Step 6 failure branch so both judge
    "are these metrics usable?" by exactly one rule. `missing` is authoritative: a file with
    plenty of tickers that predates a watchlist refresh is NOT usable.
    """
    n, missing = 0, []
    try:
        with open(metrics_path, encoding="utf-8") as f:
            got = set(json.load(f).get("tickers", {}).keys())
        n = len(got)
        with open(watchlist_path, encoding="utf-8") as f:
            wl = json.load(f)
        needed = ({e.get("ticker") for e in wl.get("watchlist", [])}
                  | {e.get("ticker") for e in wl.get("vci_watchlist", [])}
                  | {s.get("ticker") for s in wl.get("stock_sleeve", [])}
                  | {p.get("ticker") for p in wl.get("candidate_pool", [])})
        missing = sorted(t for t in needed if t and t not in got)
    except Exception:
        return 0, []
    return n, missing


def _skip_fetch_reason(args, metrics_path: str, watchlist_path: str) -> str | None:
    """Return a human reason to skip Step 6, or None to fetch. See the Step 6 call site."""
    if getattr(args, "skip_fetch", False):
        return "--skip-fetch requested"
    if not os.path.exists(metrics_path):
        return None
    n, missing = metrics_coverage(metrics_path, watchlist_path)
    if n > 0 and not missing:
        return f"metrics already cover all {n} current tickers"
    return None


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
def _compliance_block():
    """Employer PAD regime state for the review session (compliance.py is authoritative).
    Staged into run_context so the session never re-derives compliance rules from prose."""
    try:
        import compliance
        b = compliance.as_dict()
        b["status_line"] = compliance.status_line()
        b["execution_reminder"] = compliance.execution_reminder()
        return b
    except Exception as e:                       # fail SAFE: assume the restrictive regime
        return {"regime": "CITI_PT", "active": True, "error": str(e)[:200],
                "status_line": "Compliance module unavailable - assume preclearance REQUIRED."}


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
            "transactions_data":    os.path.join(SCRIPT_DIR, f"transactions_data_{month_label}.json"),
            "transaction_ledger":   os.path.join(SCRIPT_DIR, "transaction_ledger.json"),
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
            "drawdown_state":       os.path.join(SCRIPT_DIR, "drawdown_state.json"),
            "factor_map":           os.path.join(SCRIPT_DIR, "factor_map.json"),
            "target_state":         os.path.join(SCRIPT_DIR, "target_state.json"),
            "calibration_report":   os.path.join(SCRIPT_DIR, f"calibration_report_{month_label}.md"),
            "score_panel":          os.path.join(SCRIPT_DIR, "score_panel.csv"),
            # CAPTURE LAYER — the permanent per-run record set (Items 1-4).
            "run_manifest":         os.path.join(SCRIPT_DIR, f"run_manifest_{month_label}.json"),
            "gate_variables":       os.path.join(SCRIPT_DIR, "gate_variables.csv"),
            "step9_conviction":     os.path.join(SCRIPT_DIR, f"step9_conviction_{month_label}.json"),
            "shadow_ledger":        os.path.join(SCRIPT_DIR, "shadow_ledger.json"),
        },
        "summary": summary,
        "compliance": _compliance_block(),
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
def refresh_counterfactual_prices(store_path, fetch_fn=None, month_str=None,
                                  challenger_fn=None, sleeve_value_now=None,
                                  mu_value_now=None, _print=print):
    """WP-1/WP-4 (26-Jul-26) - refresh vuag_price_now + iwmo_price_now in ONE yfinance
    batch (closes the gap: no code refresh site existed - was prose-only), then append
    the month's freeze_history entry (idempotent). Fail-safe: fetch failure -> store
    untouched, WARNING printed, returns None (A14 email line degrades gracefully)."""
    import json as _json
    try:
        store = _json.load(open(store_path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        _print("WARNING A14 refresh: store unreadable (%s)" % type(e).__name__)
        return None
    if fetch_fn is None:
        def fetch_fn(tickers):
            import yfinance as yf
            out = {}
            for t in tickers:
                h = yf.Ticker(t).history(period="5d")["Close"]
                out[t] = (float(h.iloc[-1]) / 100.0, h.index[-1].date().isoformat())
            return out
    try:
        px = fetch_fn(("VUAG.L", "IWMO.L"))
        store["vuag_price_now"], store["vuag_price_now_date"] = px["VUAG.L"]
        store["iwmo_price_now"], store["iwmo_price_now_date"] = px["IWMO.L"]
    except Exception as e:
        _print("WARNING A14 refresh: fetch failed (%s) - store untouched" % type(e).__name__)
        return None
    if challenger_fn and month_str:
        ch = challenger_fn(store.get("trades"), store.get("vuag_price_now"),
                           store.get("iwmo_price_now"), sleeve_value_now, mu_value_now)
        hist = store.setdefault("freeze_history", [])
        if not any(h.get("month") == month_str for h in hist):
            v, i = ch.get("vs_vuag_exmu_pp"), ch.get("vs_iwmo_exmu_pp")
            hist.append({"month": month_str, "beats_vuag_exmu": (v is not None and v > 0),
                         "beats_iwmo_exmu": (i is not None and i > 0)})
    _json.dump(store, open(store_path, "w", encoding="utf-8"), indent=2)
    return store


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
    parser.add_argument("--skip-fetch",      action="store_true",
                        help="Skip the Step 6 metrics fetch and reuse the existing "
                             "watchlist_metrics file. Step 6 also self-skips whenever that "
                             "file already covers every current ticker, so the second "
                             "orchestrator pass is idempotent without this flag.")
    parser.add_argument("--skip-vci-sync",   action="store_true",
                        help="Skip Step 5 (sync_vci_watchlist) and reuse the vci_watchlist "
                             "section already in watchlist_tickers.json. Added 01-Aug-26: once "
                             "MEMORY_BASE was fixed, Step 5 actually executes and costs ~14s of "
                             "network time, pushing a full pass past the 45s bash ceiling. Run "
                             "the orchestrator once to land Steps 1-5, then re-run with this "
                             "flag (Step 6 self-skips on metrics coverage) to rebuild Steps 7-9.")
    parser.add_argument("--skip-moa", action="store_true",
                        help="skip the Missed-Opportunity Attribution stage (retrospective, "
                             "resumable next month; never blocks the pre-run)")
    parser.add_argument("--skip-universe-prices", action="store_true",
                        help="Skip the Capture Layer Item 5 resumable price-cache extension "
                             "(one chunk per run). The cache is resumable, so skipping only "
                             "defers work; it never loses any.")
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
    transactions_path       = os.path.join(SCRIPT_DIR, f"transactions_data_{month_label}.json")
    txn_ledger_path         = os.path.join(SCRIPT_DIR, "transaction_ledger.json")
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

    # CAPTURE LAYER ITEM 2 — open the run manifest before any step runs.
    global MANIFEST
    if _RM is not None:
        MANIFEST = _RM.Manifest(month_label, script_dir=SCRIPT_DIR)
    manifest_path = os.path.join(SCRIPT_DIR, f"run_manifest_{month_label}.json")

    # ---------------------------------------------------------------------------
    # Step 1: Extract portfolio
    # ---------------------------------------------------------------------------
    print(f"\n[1/9] Extracting portfolio from xlsx...")
    _mf_begin("1", "extract_portfolio")
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
    # Step 1b: Import the monthly transaction export into the persistent ledger.
    # Gives Step 1.5 the ACTUAL dealing record (date, fill price, cost) instead
    # of a holdings delta. Absent file => WARN + fall back; never an ERROR.
    # ---------------------------------------------------------------------------
    txn_status = "ABSENT"
    txn_data = {}
    _mf_probe_json(portfolio_path, "stocks", "value_gbp", "stocks extracted")
    _mf_probe_json(portfolio_path, "funds", "value_gbp", "funds extracted", add=True)
    # ── Step 1b-2 — cash statement (05-Aug-2026, register H13) ───────────────────────
    # The ISA allowance was wrong by GBP5,000 for four months because contributions were being
    # read from the Transaction History, which is a DEALING record and contains no cash
    # deposits. This stage reads the document that does. Missing file = WARN, never ERROR,
    # exactly as for 1b -- the allowance then degrades to UNRECONCILED rather than to a guess.
    print("\n[1b-2] Reading cash statement (allowance + fees + FX, golden source)...")
    _mf_begin("1b-2", "extract_cash_statement")
    try:
        import extract_cash_statement as _ecs
        _cs = _ecs.parse(folder=isa_folder)
        _cs["ledger_reconciliation"] = _ecs.reconcile_with_ledger(_cs, txn_ledger_path)
        _cs_path = os.path.join(SCRIPT_DIR, f"cash_statement_{month_label}.json")
        with open(_cs_path, "w", encoding="utf-8") as _f:
            json.dump(_cs, _f, indent=2, default=str)
        _al = _cs.get("allowance")
        if not _cs.get("source_files"):
            _mf_measure(status="SKIPPED",
                        note="no 'Cash Statement*.xlsx' saved this month -- ISA allowance "
                             "degrades to UNRECONCILED (policy, not degradation)")
            warnings.append("Step 1b-2: no Cash Statement export found -- the ISA allowance "
                            "cannot be reconciled. The Transaction History CANNOT substitute: "
                            "it is a dealing record with no cash-deposit rows.")
        else:
            _bad = [k for k, v in (_cs.get("invariants") or {}).items() if not v.get("ok")]
            _mf_measure(status="OK" if _cs.get("reconciled") else "WARN",
                        note=f"allowance used GBP{(_al or {}).get('used_gbp')} / remaining "
                             f"GBP{(_al or {}).get('remaining_gbp')}; invariants "
                             f"{'all green' if not _bad else 'FAILED: ' + ','.join(_bad)}")
            summary["cash_statement"] = {
                "source_files": _cs.get("source_files"),
                "allowance_used_gbp": (_al or {}).get("used_gbp"),
                "allowance_remaining_gbp": (_al or {}).get("remaining_gbp"),
                "opening_balance_gbp": _cs.get("opening_balance_gbp"),
                "closing_balance_gbp": _cs.get("closing_balance_gbp"),
                "fx_rate_pct": _cs.get("fx_rate_pct"),
                "invariants_failed": _bad,
                "ledger_unmatched": len((_cs.get("ledger_reconciliation") or {}).get("unmatched") or []),
                "ledger_pending": len((_cs.get("ledger_reconciliation") or {}).get("pending_ledger_update") or []),
            }
            for _b in _bad:
                warnings.append(f"Step 1b-2: cash-statement invariant {_b} FAILED -- the "
                                f"allowance figure is not trustworthy this month")
            _lr = _cs.get("ledger_reconciliation") or {}
            for _u in (_lr.get("unmatched") or []):
                warnings.append(f"Step 1b-2 (I10): cash-statement trade not in the dealing "
                                f"ledger: {_u['date']} GBP{_u['amount_gbp']} "
                                f"{_u['description']}")
            print(f"  allowance GBP{(_al or {}).get('used_gbp')} used / "
                  f"GBP{(_al or {}).get('remaining_gbp')} remaining | FX "
                  f"{_cs.get('fx_rate_pct')}% | invariants "
                  f"{'green' if not _bad else 'FAILED ' + ','.join(_bad)}")
    except Exception as _e:
        _mf_measure(status="ERROR", note=f"{type(_e).__name__}: {_e}")
        warnings.append(f"Step 1b-2 (extract_cash_statement): {type(_e).__name__}: {_e}")
        print(f"  FAILED (non-fatal): {_e}")

    # ── Step 6.05 — Fund Action Stack (register C4 + C5, 05-Aug-2026) ────────────────
    # The fund sleeve is 85.1% of the ISA and had no ownership floor and no opportunity-cost
    # test, while the 7.9% stock sleeve had both. This stage produces the fund analogue: a
    # Fund Retention Score, a binary dominance test, and the anchor rule ("every holding clears
    # its bucket minimum on REALISED evidence or is dead money"). It ranks and recommends; it
    # never trades.
    print("\n[6.05] Fund action stack (FRS + dominance + anchor rule)...")
    _mf_begin("6.05", "fund_action_stack")
    try:
        import fund_action_stack as _fas
        _port = None
        try:
            with open(portfolio_path, encoding="utf-8") as _pf:
                _port = json.load(_pf)
        except Exception:
            pass
        _fa = _fas.build(as_of=run_date if isinstance(run_date, dt_date) else None,
                         portfolio=_port)
        _fa_path = os.path.join(SCRIPT_DIR, f"fund_action_stack_{month_label}.json")
        with open(_fa_path, "w", encoding="utf-8") as _f:
            json.dump(_fa, _f, indent=2, default=str)
        _s = _fa["summary"]
        _mf_measure(status="OK",
                    note=f"{_s['n_funds']} funds: HOLD/ADD {_s['hold_add']}, RETAIN-ONLY "
                         f"{_s['retain_only']}, DEAD MONEY {_s['dead_money']} "
                         f"(GBP{_s['dead_money_value_gbp']:,.0f}), UNSCORED {_s['unscored']}")
        summary["fund_action_stack"] = _s
        for f in _fa.get("anchor_rule_failures", []):
            warnings.append(
                f"Step 6.05 ANCHOR RULE: {f['name']} realised 5y {f['realised_5y_ann']:.2f}% is "
                f"{f['shortfall_pp']}pp BELOW its {f['bucket']} minimum of "
                f"{f['bucket_minimum_pct']:.1f}% — Category 7 agenda item, not a silent hold.")
        for d in _fa.get("fund_dominance", []):
            _tag = ("ESCALATE" if d.get("escalate")
                    else "review only — second source unavailable or contradicting")
            warnings.append(f"Step 6.05 DOMINANCE ({_tag}): {d['statement']}")
        _xc = _fa.get("xray_cross_check", {})
        for nm in _xc.get("disputed", []):
            warnings.append(
                f"Step 6.05 DISPUTED: {nm} — the golden source and the X-Ray fall on OPPOSITE "
                f"sides of its bucket minimum. No verdict published; do not act on either "
                f"figure alone.")
        # ── a DECLARED window that produced nothing for the whole universe is a build fault ──
        for _w, _c in (_fa.get("window_coverage") or {}).items():
            if _c.get("alarm"):
                warnings.append(f"Step 6.05 WINDOW COVERAGE: {_c['note']}")
        # ── Tier-1 item 5: publish what the basis choice would change ───────────────────
        _bs = _fa.get("return_adequacy_basis_study") or {}
        for _b in _bs.get("basis_sensitive", []):
            warnings.append(
                f"Step 6.05 BASIS-SENSITIVE: {_b['name']} (GBP{_b['value_gbp']:,.0f}) bands "
                f"differently depending on the return statistic — "
                + ", ".join(f"{k}={v}" for k, v in _b["bands"].items())
                + ". This is Raj's calibration decision, not the code's.")
        # ── Tier-1 item 1: the money-weighted overlay ───────────────────────────────────
        _mw = _fa.get("money_weighted_returns") or {}
        if (_mw.get("summary") or {}).get("usable_as_anchor") == 0:
            warnings.append(
                "Step 6.05 MWR: every holding's money-weighted span is below the "
                f"{(_fa.get('return_adequacy_config') or {}).get('mwr_min_span_years')}y anchor "
                "minimum, so return adequacy is still scored on trailing windows. The "
                "money-weighted figures are REPORTED for every holding.")
        print(f"  HOLD/ADD {_s['hold_add']} | RETAIN-ONLY {_s['retain_only']} | DEAD MONEY "
              f"{_s['dead_money']} (GBP{_s['dead_money_value_gbp']:,.0f}) | anchor failures "
              f"{_s['anchor_failures']} | disputed {len(_xc.get('disputed', []))} | "
              f"basis-sensitive {_bs.get('n_basis_sensitive', 0)}")
        # ── L2: declared peer-group blocks (06-Aug-2026) ─────────────────────────────
        try:
            import fund_pair_test as _fpt
            _cds = _fpt.category_declaration_status()
            summary["fund_categories"] = {"declared": _cds["n_declared"], "of": _cds["of"]}
            if _cds["n_outstanding"]:
                warnings.append(
                    f"Step 6.05 PEER GROUPS: {_cds['n_declared']} of {_cds['of']} funds carry a "
                    f"usable declared category. {_cds['consequence']} Outstanding: "
                    + "; ".join(f"{r['sedol']} ({r['category_name'] or 'no name'}, "
                                f"{r['status']})" for r in _cds["rows"]
                                if not r["usable_for_verdict"]))
        except Exception as _e:                                # noqa: BLE001
            warnings.append(f"Step 6.05 peer-group status unavailable: {type(_e).__name__}: {_e}")

        # ── trust NAV observation cadence (06-Aug-2026) ──────────────────────────────
        # ⚑ The discount series has ONE point and needs twelve. The NAV is not in any feed
        # this framework has, so the only mechanism available is a run that REFUSES TO BE
        # SILENT about the gap. Without this the clock simply never starts.
        for _c in (_fa.get("trust_capture_status") or []):
            if _c.get("capture_due"):
                warnings.append(f"Step 6.05 TRUST NAV CAPTURE DUE: {_c['request']}")
            elif _c.get("remaining"):
                warnings.append(
                    f"Step 6.05 TRUST NAV: {_c['sedol']} has {_c['observations']} of "
                    f"{_c['target']} observations; no percentile or z-score can be published "
                    f"until then (projected {_c.get('projected_complete')}). Latest "
                    f"{_c.get('latest_observation')}.")
    except Exception as _e:
        _mf_measure(status="ERROR", note=f"{type(_e).__name__}: {_e}")
        warnings.append(f"Step 6.05 (fund_action_stack): {type(_e).__name__}: {_e}")
        print(f"  FAILED (non-fatal): {_e}")

    # ── Step 6.06 — look-through (register H9 + H10, 06-Aug-2026) ────────────────────
    # H10: the overlap check is now READ from the X-Ray's published Top 10 Underlying Holdings
    # instead of hand-computed (the hand-calc reported AVGO 4.04% against a published 4.31%).
    # H9: the marginal test that did not exist — what putting money INTO a fund does to the
    # portfolio's factor concentration. It runs BEFORE Step 8 writes any recommendation,
    # because a gate consulted afterwards is a rationalisation.
    print("\n[6.06] Look-through (H10 published overlap + H9 marginal allocation gate)...")
    _mf_begin("6.06", "lookthrough")
    try:
        import lookthrough as _lt
        _lt_path = os.path.join(SCRIPT_DIR, f"lookthrough_{month_label}.json")
        _ltr = _lt.build(portfolio_path=portfolio_path, xray_path=xray_path,
                         stack_path=os.path.join(SCRIPT_DIR,
                                                 f"fund_action_stack_{month_label}.json"),
                         out_path=_lt_path)
        _h10, _h9 = _ltr["h10_overlap_check"], _ltr["h9_marginal_allocation"]
        summary["lookthrough"] = {"h10_status": _h10.get("status"),
                                  "h10_flags": len(_h10.get("flags", [])),
                                  "h9_block": _h9.get("blocked"), "h9_flag": _h9.get("flagged"),
                                  "h9_unknown": _h9.get("unknown")}
        _mf_measure(status="OK" if _h10.get("status") == "OK" else "DEGRADED",
                    note=f"H10 {_h10.get('status')} ({len(_h10.get('flags', []))} flags); "
                         f"H9 block={_h9.get('blocked')} flag={_h9.get('flagged')} "
                         f"unknown={_h9.get('unknown')}")
        if _h10.get("status") != "OK":
            warnings.append(f"Step 6.06 H10: overlap check {_h10.get('status')} — "
                            f"{_h10.get('note', '')}")
        for _f in _h10.get("flags", []):
            warnings.append(
                f"Step 6.06 OVERLAP: {_f['ticker']} effective look-through weight "
                f"{_f.get('lookthrough_total_pct') or ('<=' + str(_f.get('upper_bound_pct')))}% "
                f"exceeds the {_h10['flag_threshold_pct']}% flag.")
        for _a in _h9.get("assessments", []):
            if _a["verdict"] in ("BLOCK", "FLAG", "UNKNOWN"):
                warnings.append(f"Step 6.06 H9 {_a['verdict']}: new money into {_a['name']} — "
                                f"{_a['reason']}")
        print(f"  H10 {_h10.get('status')} | flags {len(_h10.get('flags', []))} | "
              f"H9 BLOCK {_h9.get('blocked')} FLAG {_h9.get('flagged')} "
              f"UNKNOWN {_h9.get('unknown')}")
        # ── H9 name-level store (06-Aug-2026) ────────────────────────────────────────
        # The name-level test was UNKNOWN by design because no per-fund holdings existed.
        # `fund_holdings_declared.json` now exists; what is still missing is DATA, and the
        # difference between "cannot be built" and "has not been filled in" only stays visible
        # if the run says so every month.
        try:
            _ds = _lt.declaration_status()
            summary["fund_holdings_declared"] = {"absent": _ds["n_absent"],
                                                 "partial": _ds["n_partial"], "of": _ds["of"]}
            if _ds["n_absent"]:
                warnings.append(
                    f"Step 6.06 H9 NAME-LEVEL: {_ds['n_absent']} of {_ds['of']} funds have NO "
                    f"declared holdings and {_ds['n_partial']} are partial, so the name-level "
                    f"look-through cannot run for them. {_ds['request']} Absent: "
                    + ", ".join(r["sedol"] for r in _ds["rows"] if r["status"] == "ABSENT"))
        except Exception as _e:                                # noqa: BLE001
            warnings.append(f"Step 6.06 H9 store status unavailable: {type(_e).__name__}: {_e}")

    except Exception as _e:
        _mf_measure(status="ERROR", note=f"{type(_e).__name__}: {_e}")
        warnings.append(f"Step 6.06 (lookthrough): {type(_e).__name__}: {_e}")
        print(f"  FAILED (non-fatal): {_e}")

    # ── Step 6.07 — concentration clusters (register M7 + L1, 06-Aug-2026) ───────────
    # Raj asked whether the single-fund cap should rise to ~20% "as long as the overall
    # portfolio remains diversified". Nothing measured whether it does. This measures it and
    # SETS NO LIMIT — his instruction was to build the measurement first and set the number
    # against two runs of real data. Grouping is by CORRELATION CLUSTER, not by manager name:
    # Artemis European and Artemis UK correlate across different geographies (one process), while
    # JPM UK and Artemis UK correlate 0.917 (the same factor bet, different implementations).
    print("\n[6.07] Concentration clusters (effective bets + risk contribution)...")
    _mf_begin("6.07", "concentration_clusters")
    try:
        import concentration_clusters as _cc
        _cc_path = os.path.join(SCRIPT_DIR, f"concentration_{month_label}.json")
        _ccr = _cc.build(_port, None, run_date if isinstance(run_date, dt_date) else None,
                         _cc_path, True)
        if _ccr.get("status") != "OK":
            _mf_measure(status="DEGRADED", note=_ccr.get("reason", "insufficient"))
            warnings.append(f"Step 6.07: concentration not measured — {_ccr.get('reason')}")
        else:
            _eb = _ccr["effective_bets"]; _cv = _ccr["coverage"]
            summary["concentration"] = {
                "coverage_pct": _cv["measured_pct_of_isa"], "n_clusters": _ccr["n_clusters"],
                "largest_cluster_pct": _ccr["largest_cluster_pct_of_isa"],
                "effective_bets_by_risk": _eb["by_risk_principal_portfolios"],
                "pc1_variance_pct": _eb["pc1_share_of_variance_pct"]}
            _mf_measure(status="OK",
                        note=f"{_cv['n_measured']} holdings / {_ccr['n_clusters']} clusters over "
                             f"{_cv['measured_pct_of_isa']}% of the ISA; effective bets by risk "
                             f"{_eb['by_risk_principal_portfolios']}, PC1 "
                             f"{_eb['pc1_share_of_variance_pct']}% of variance")
            # ⚑ the finding that reframes the whole question, surfaced every run
            if (_eb.get("by_risk_principal_portfolios") or 99) < 2.0:
                warnings.append(
                    f"Step 6.07 CONCENTRATION: {_cv['n_measured']} holdings in "
                    f"{_ccr['n_clusters']} correlation clusters, but an effective number of bets "
                    f"BY RISK of {_eb['by_risk_principal_portfolios']} and PC1 carrying "
                    f"{_eb['pc1_share_of_variance_pct']}% of portfolio variance. On a risk basis "
                    f"the measured sleeve is close to a SINGLE bet, so a per-fund or per-cluster "
                    f"weight cap would regulate something that is not the risk.")
            if _cv["excluded_pct_of_isa"] > 5:
                warnings.append(
                    f"Step 6.07 COVERAGE: {_cv['excluded_pct_of_isa']}% of the ISA has no usable "
                    f"return series (" + ", ".join(
                        f"{e['sedol']} {e['weight_pct']}%" for e in _ccr["excluded"][:4])
                    + "). Their contribution to concentration is UNKNOWN, not zero.")
            _h = _ccr.get("history_rows") or {}
            if not _h.get("ready"):
                warnings.append(
                    f"Step 6.07 HISTORY: {_h.get('total_runs')} run(s) recorded; "
                    f"{_h.get('runs_needed_before_setting_a_limit')} needed before a "
                    f"concentration limit is set against observed data (register L1).")
            print(f"  clusters {_ccr['n_clusters']} | effective bets by risk "
                  f"{_eb['by_risk_principal_portfolios']} | PC1 "
                  f"{_eb['pc1_share_of_variance_pct']}% | coverage {_cv['measured_pct_of_isa']}%")
    except Exception as _e:
        _mf_measure(status="ERROR", note=f"{type(_e).__name__}: {_e}")
        warnings.append(f"Step 6.07 (concentration_clusters): {type(_e).__name__}: {_e}")
        print(f"  FAILED (non-fatal): {_e}")

    # ── Step 6.08 — return architecture (ranked build item #1, 06-Aug-2026) ──────────
    # ⚑ Section C — the one number that answers "is this on track for £1m" — has NEVER been
    # computed. It shipped as `total_return: null, status: "pending_section_a"` and rendered as
    # "[Claude fills]". Section A was computed, but from `est_return`: prose typed by hand a
    # month earlier, which scores Scottish Mortgage 14.0% on a realised 5y of 0.22% (register
    # C4). This step replaces both with arithmetic, against ONE declared expected-return input
    # per holding, gated on thresholds DERIVED from the A19 anchor.
    print("\n[6.08] Return architecture (Section A/B/C + shortfall + levers)...")
    _mf_begin("6.08", "return_architecture")
    try:
        import return_architecture as _ra
        _ra_path = os.path.join(SCRIPT_DIR, f"return_architecture_{month_label}.json")
        _fas_json = None
        try:
            with open(os.path.join(SCRIPT_DIR, f"fund_action_stack_{month_label}.json"),
                      encoding="utf-8") as _f:
                _fas_json = json.load(_f)
        except Exception:                                      # noqa: BLE001
            _fas_json = None
        _met_json = None
        try:
            with open(os.path.join(SCRIPT_DIR, f"watchlist_metrics_{month_label}.json"),
                      encoding="utf-8") as _f:
                _met_json = json.load(_f)
        except Exception:                                      # noqa: BLE001
            _met_json = None
        _rar = _ra.build(run_date if isinstance(run_date, dt_date) else None,
                         _port, _fas_json, None, _met_json, _ra_path)
        _sa, _sb, _sc = _rar["section_a"], _rar["section_b"], _rar["section_c"]
        summary["return_architecture"] = {
            "basis": _rar["operative_basis"], "anchor_pct": _rar["anchor"]["operative_pct"],
            "section_a_pct": _sa["value_pct"], "section_a_verdict": _sa.get("verdict"),
            "section_b_pct": _sb["value_pct"], "section_b_verdict": _sb.get("verdict"),
            "section_c_pct": _sc["value_pct"], "section_c_verdict": _sc.get("verdict"),
            "shortfall_pp": _sc.get("shortfall_pp"), "coverage": _sc.get("coverage")}
        _bad = [i for i in _rar["invariants"] if not i["holds"]]
        _mf_measure(status="OK" if not _bad else "DEGRADED",
                    note=(f"Section C {_sc['value_pct']}% vs anchor "
                          f"{_rar['anchor']['operative_pct']}% -> {_sc.get('verdict')} "
                          f"(shortfall {_sc.get('shortfall_pp')}pp); "
                          f"{len(_rar['invariants']) - len(_bad)}/{len(_rar['invariants'])} "
                          f"invariants hold"))
        for _i in _bad:
            errors.append(f"Step 6.08 INVARIANT {_i['invariant']} FAILED: {_i['detail']}")
        if _sc.get("verdict") in ("Flag", "Watch"):
            _top = (_rar["shortfall_attribution"]["rows"] or [])[:3]
            warnings.append(
                f"Step 6.08 SECTION C {str(_sc.get('verdict')).upper()}: total ISA expected "
                f"return {_sc['value_pct']}% against a required {_rar['anchor']['operative_pct']}% "
                f"— short by {_sc.get('shortfall_pp')}pp on the '{_rar['operative_basis']}' basis. "
                f"Largest drags: " + "; ".join(
                    f"{r['asset_id']} {r['contribution_to_shortfall_pp']:+.2f}pp" for r in _top))
        for _l in _rar["levers"]:
            if not _l["feasible"] and _l["lever"] == "deploy_idle_cash":
                warnings.append(f"Step 6.08 CASH: {_l['blocked_reason']}")
        if _rar["thresholds"]["divergences"]:
            for _d in _rar["thresholds"]["divergences"]:
                warnings.append(
                    f"Step 6.08 THRESHOLD DRIFT: {_d['threshold']} derived {_d['derived_pct']}% "
                    f"vs the frozen constant {_d['legacy_pct']}% ({_d['delta_pp']:+}pp) — the "
                    f"derived value is operative; target_weights.json is now stale prose")
        _bmd = _rar.get("bucket_minimum_divergence") or {}
        for _r in (_bmd.get("rows") or []):
            if _r.get("agree") is False:
                warnings.append(
                    f"Step 6.08 BUCKET MINIMUM {_r['bucket']}: policy file says "
                    f"{_r['policy_pct']}%, fund_action_stack uses {_r['in_force_pct']}%. "
                    f"{_bmd.get('diagnosis')} Left in force pending Raj (one constant).")
        for _d in (_rar.get("defects_observed") or []):
            warnings.append(f"Step 6.08 {_d['code']}: {_d['detail']}")
        print(f"  A {_sa['value_pct']}% {_sa.get('verdict')} | B {_sb['value_pct']}% "
              f"{_sb.get('verdict')} | C {_sc['value_pct']}% {_sc.get('verdict')} "
              f"(short {_sc.get('shortfall_pp')}pp) | invariants "
              f"{len(_rar['invariants']) - len(_bad)}/{len(_rar['invariants'])}")
    except Exception as _e:
        _mf_measure(status="ERROR", note=f"{type(_e).__name__}: {_e}")
        errors.append(f"Step 6.08 (return_architecture): {type(_e).__name__}: {_e}")
        print(f"  FAILED: {_e}")

    print("\n[1b] Importing transaction history...")
    _mf_begin("1b", "extract_transactions")
    rc, stdout, stderr = run_script_rc(
        "extract_transactions",
        ["--isa-folder", isa_folder, "--ledger", txn_ledger_path,
         "--out", transactions_path],
        dry_run=args.dry_run,
    )
    if rc == 3:
        # Policy, not degradation: "missing transaction export = WARN, never ERROR"
        # (project_isa_transaction_ledger). Declaring it SKIPPED keeps the manifest honest
        # without manufacturing an alarm Raj cannot act on.
        _mf_measure(status="SKIPPED", note="no Transaction History export saved this month "
                                           "-- policy fallback to holdings-delta inference")
        txn_status = "ABSENT"
        warnings.append(
            "Step 1b: no 'Transaction History MM-YYYY.xlsx' found in the ISA "
            "folder -- execution reconciliation falls back to holdings-delta "
            "inference, and dealing costs/fill prices are unavailable this month."
        )
        print("  No transaction export found -- reconciliation degrades to holdings-delta.")
    elif rc != 0:
        txn_status = "ERROR"
        warnings.append(f"Step 1b (extract_transactions): {stderr or stdout or 'unknown error'}")
        print(f"  FAILED (non-fatal): {stderr or stdout}")
    else:
        print(stdout.strip())
        if os.path.exists(transactions_path):
            try:
                with open(transactions_path, encoding="utf-8") as _tf:
                    txn_data = json.load(_tf)
                txn_status = txn_data.get("_meta", {}).get("status", "OK")
                _ms = txn_data.get("month_summary", {})
                _br = txn_data.get("broker_reconciliation", {})
                summary["transactions"] = {
                    "status":              txn_status,
                    "source_file":         txn_data.get("_meta", {}).get("source_file"),
                    "n_trades":            _ms.get("n_trades"),
                    "buys":                _ms.get("buys"),
                    "sells":               _ms.get("sells"),
                    "dealing_costs_gbp":   _ms.get("total_dealing_costs_gbp"),
                    "net_cash_impact_gbp": _ms.get("net_cash_impact_gbp"),
                    "distributions_gbp":   _ms.get("distributions_gbp"),
                    "broker_reconciliation": _br.get("status"),
                    "ledger_entries":      txn_data.get("ledger_meta", {}).get("total_entries"),
                    "cost_calibration":    txn_data.get("cost_calibration", {}),
                }
                for w in txn_data.get("warnings", []):
                    warnings.append(f"Step 1b: {w}")
                if _br.get("status") == "INCOMPLETE_WINDOW":
                    warnings.append(
                        "Step 1b: transaction ledger does not yet span the "
                        "holding period of %d of %d positions -- seed it with a "
                        "full history export (extract_transactions.py --seed) "
                        "to enable the completeness check."
                        % (_br.get("n_missing_from_ledger", 0),
                           _br.get("n_compared", 0)))
                elif _br.get("status") == "MISMATCH":
                    # The ledger disagrees with the broker portfolio file. Either a
                    # monthly export was never saved, or a row mis-parsed. Surface
                    # it loudly -- a silently incomplete ledger is worse than none.
                    flags.append({
                        "type": "TRANSACTION_LEDGER_INCOMPLETE",
                        "message": ("Ledger-implied holdings do not match the broker "
                                    "portfolio file: "
                                    + "; ".join(
                                        f"{d['ticker']} broker {d['broker_quantity']} "
                                        f"vs ledger {d['ledger_quantity']}"
                                        for d in _br.get("differences", [])[:6])),
                    })
            except Exception as exc:
                txn_status = "ERROR"
                warnings.append(f"Step 1b: could not read {os.path.basename(transactions_path)}: {exc}")

    # ---------------------------------------------------------------------------
    # Step 1.5: Reconcile prior recommendations vs broker truth (recommendations != executions).
    # The system never assumes a prior recommendation was executed; it confirms from THIS month's
    # actual holdings (broker file). Additive — no-op until a decision ledger exists.
    # ---------------------------------------------------------------------------
    if not errors and os.path.exists(portfolio_path):
        ledger_path = os.path.join(SCRIPT_DIR, "decision_ledger.json")
        if os.path.exists(ledger_path):
            print("\n[1.5] Reconciling prior decision-ledger recommendations vs broker holdings...")
            _mf_begin("1.5", "ledger_reconciliation")
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
                # Transaction-truth reconciliation (26-Jul-26). Confirms from the
                # actual dealing record where available -- date, fill price and
                # dealing cost included -- and degrades to the original
                # holdings-delta inference when no export exists.
                _txns = _dl_mod.load_transactions(transactions_path)
                _res = _dl_mod.reconcile_executions_from_transactions(
                    ledger_path, _txns, _held, prior_holdings=_prior_h,
                    date=run_date.isoformat())
                _rc = _res["counts"]
                summary["ledger_reconcile"] = _rc
                summary["ledger_reconcile_source"] = _res["source"]
                summary["ledger_reconcile_confirmed"] = _res["confirmed"]
                if _res["off_framework"]:
                    # Trades with no matching recommendation: acted outside the
                    # framework. Holdings-diffing cannot see these reliably.
                    summary["off_framework_trades"] = _res["off_framework"]
                    flags.append({
                        "type": "OFF_FRAMEWORK_TRADE",
                        "message": "; ".join(
                            f"{o['type']} {o['ticker']} {o['quantity']} @ {o['price']} "
                            f"on {o['date']} (GBP {o['amount_gbp']}) -- no ledger recommendation"
                            for o in _res["off_framework"][:6]),
                    })
                print(f"  Reconciled prior recommendations vs broker truth: {_rc}")
                # ── Fix Pack A13 (P2): OVERRIDE LOG — broker-truth changes NOT matching a ledger
                # recommendation. Two classes: (a) a recommendation marked not_executed (Raj
                # declined the framework), (b) a holdings change with NO ledger entry (Raj acted
                # outside the framework). Each gets 3/6/12-mo counterfactual slots that this
                # step prices on later runs from live prices (zero new fetch — pre-run prices
                # everything). Appended to run_context summary.override_log; email §11 renders
                # the one-line cumulative summary; trades-log section mirrors it (P7d: a closed
                # position with a blank reason renders "reason UNBACKFILLED", never
                # "reconciled and closed").
                try:
                    _led = _dl_mod.load_ledger(ledger_path)
                    _ov = []
                    _stamp = run_date.isoformat()
                    _recs = {str(e.get("ticker", "")).upper(): e for e in _led.get("entries", [])}
                    for _e3 in _led.get("entries", []):
                        if _e3.get("execution_status") == "not_executed" and \
                           str(_e3.get("executed_confirmed_date") or _stamp)[:7] == _stamp[:7]:
                            _ov.append({"date": _stamp, "ticker": _e3.get("ticker"),
                                        "action": f"declined_{_e3.get('decision')}",
                                        "framework_state": _e3.get("scores_at_decision"),
                                        "gates": _e3.get("gates_at_decision"),
                                        "counterfactual": {"3m": None, "6m": None, "12m": None},
                                        "pnl_vs_framework_gbp": None})
                    if _prior_h is not None:
                        for _t4, _q4 in _held.items():
                            if _t4 not in _prior_h and _t4 not in _recs:
                                _ov.append({"date": _stamp, "ticker": _t4,
                                            "action": "bought_outside_framework",
                                            "framework_state": None, "gates": None,
                                            "counterfactual": {"3m": None, "6m": None, "12m": None},
                                            "pnl_vs_framework_gbp": None})
                        for _t4 in _prior_h:
                            if _t4 not in _held and not any(
                                    e.get("decision") == "sell" and
                                    str(e.get("ticker", "")).upper() == _t4
                                    for e in _led.get("entries", [])):
                                _ov.append({"date": _stamp, "ticker": _t4,
                                            "action": "sold_outside_framework",
                                            "framework_state": None, "gates": None,
                                            "counterfactual": {"3m": None, "6m": None, "12m": None},
                                            "pnl_vs_framework_gbp": None})
                    if _ov:
                        summary["override_log"] = _ov
                        print(f"  A13: {len(_ov)} override(s) logged: "
                              f"{[o['ticker'] + ':' + o['action'] for o in _ov]}")
                except Exception as _oex:
                    warnings.append(f"A13 override log skipped: {_oex}")
            except Exception as _ex:
                warnings.append(f"Step 1.5 (ledger reconcile) skipped: {_ex}")
                print(f"  WARNING: {_ex}")

    # ---------------------------------------------------------------------------
    # Step 2: Extract X-Ray
    # ---------------------------------------------------------------------------
    _mf_probe_json(txn_ledger_path, "entries", "date", "transaction ledger entries")
    print(f"\n[2/9] Extracting X-Ray from PDF...")
    _mf_begin("2", "extract_xray")
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
    _mf_probe_json(xray_path, "country_exposure", "equity_pct", "X-Ray country rows")
    print(f"\n[3/9] Running portfolio analytics...")
    _mf_begin("3", "portfolio_analytics")
    if errors:
        print("  SKIPPED -- portfolio extraction failed (required input).")
        warnings.append("Step 3 (analytics) skipped -- portfolio extraction failed.")
    else:
        analytics_args = [
            "--portfolio", portfolio_path,
            "--out",       analytics_path,
        ]
        # H10 (06-Aug-2026): the overlap check now reads the PUBLISHED X-Ray look-through table
        # instead of asking for a hand calculation. Passed here rather than defaulted inside
        # portfolio_analytics so that an absent X-Ray shows up as a stated absence in the run
        # output rather than as a quietly missing section.
        if xray_path and os.path.exists(xray_path):
            analytics_args += ["--xray", xray_path]
        else:
            warnings.append(
                "Step 3 (analytics): X-Ray JSON not available, so the H10 look-through overlap "
                "check is ABSENT this run. It is NOT falling back to the retired hand-calc, "
                "which reported AVGO 4.04% against a published 4.31%.")
        if prior_port_path:
            analytics_args += ["--prior-portfolio", prior_port_path]
        if trades_log_path:
            analytics_args += ["--trades-log", trades_log_path]
        else:
            # 01-Aug-26: previously a SILENT omission. Without the trades log,
            # parse_trades_log_positions() returns [] and the stock-sleeve return is
            # computed with no purchase dates, and the Step 8 thesis-break conditions are
            # absent -- with nothing printed. Degraded data must be visible.
            warnings.append(
                "Step 3 (analytics): project_isa_trades_log.md not found at MEMORY_BASE "
                f"({MEMORY_BASE}) -- stock-sleeve return computed WITHOUT purchase dates and "
                "Step 8 thesis-break conditions are unavailable. Treat sleeve return as indicative.")
            print(f"  WARNING: trades log not found -- sleeve return degraded. MEMORY_BASE={MEMORY_BASE}")
            degraded = True

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
    _mf_probe_json(analytics_path, "fund_drift_table.rows", "ticker", "drift rows")
    print(f"\n[4/9] Updating watchlist via update_watchlist.py...")
    _mf_begin("4", "update_watchlist")
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
            # ── WP-G (29-Jul-2026): CALIBRATION PREFLIGHT ────────────────────────────────────
            # The pre-run consumes SUMMARY tabs produced by screens that ran under whatever
            # calibration was live AT THAT TIME. On 29-Jul-2026 the forward-axis bucket weights
            # changed (price .70 -> thirds) while the next screen was not until 07-Aug, so the
            # 01-Aug pre-run would have ranked candidates under a config that no longer existed
            # and nothing could detect it. This checks every ingested workbook's calibration
            # stamp against live config, and the pool size against its own trailing median.
            # WARN-ONLY by design: it annotates and degrades, it never halts the review.
            try:
                import calibration_guard as _cg
                _live = _cg.config_fingerprint()
                summary["calibration_fingerprint"] = _live["hash"]
                # WP-M7 (29-Jul-2026): pass the REAL stamps. This was hardcoded to None, so the
                # guard could only ever return UNSTAMPED and the whole fingerprint mechanism was
                # inert at the one place it mattered. update_watchlist now carries the per-file
                # calibration stamp off the SUMMARY rows.
                _stamp_map = watchlist_promotion_log.get("calibration_stamps") or {}
                _stale_files, _unstamped_files = [], []
                for _fn, _sts in _stamp_map.items():
                    for _st in (_sts or [None]):
                        _v = _cg.compare_fingerprint({"hash": _st, "params": {}} if _st else None,
                                                     live=_live)["verdict"]
                        if _v == "STALE":
                            _stale_files.append(f"{_fn} ({_st})")
                        elif _v == "UNSTAMPED":
                            _unstamped_files.append(_fn)
                summary["calibration_files"] = {"stale": _stale_files,
                                                "unstamped": _unstamped_files,
                                                "checked": len(_stamp_map)}
                if _stale_files:
                    warnings.append(
                        "Step 4 CALIBRATION STALE: %d ingested workbook(s) were scored under a "
                        "SUPERSEDED calibration (live %s): %s. Their SUMMARY ranking is not "
                        "current -- restamp via restamp_screener_outputs.py + restamp_write.py "
                        "before trusting the candidate pool."
                        % (len(_stale_files), _live["hash"], ", ".join(_stale_files[:6])))
                    print("  CALIBRATION STALE in %d file(s): %s"
                          % (len(_stale_files), ", ".join(_stale_files[:4])))
                if _unstamped_files:
                    print("  Calibration: %d file(s) predate the fingerprint (unverifiable): %s"
                          % (len(_unstamped_files), ", ".join(_unstamped_files[:4])))
                _one = sorted({_st for _sts in _stamp_map.values() for _st in (_sts or []) if _st})
                _pf = _cg.preflight("PRERUN_POOL", n_rows,
                                    stamped_fingerprint=({"hash": _one[0], "params": {}}
                                                         if len(_one) == 1 else None),
                                    store=os.path.join(SCRIPT_DIR, _cg.POOL_STORE_DEFAULT))
                summary["calibration_preflight"] = _pf
                if _pf.get("attention_required"):
                    warnings.append("Step 4 CALIBRATION: " + _pf["headline"][:400])
                    print("  CALIBRATION ATTENTION: " + _pf["headline"][:200])
                else:
                    print(f"  Calibration preflight OK (live {_live['hash']}, pool {n_rows}).")
                _cg.record_pool("PRERUN_POOL", run_date.isoformat(), n_rows,
                                store=os.path.join(SCRIPT_DIR, _cg.POOL_STORE_DEFAULT),
                                fingerprint_hash=_live["hash"])
            except Exception as _cgerr:
                warnings.append(f"Step 4 calibration preflight unavailable (non-fatal): {_cgerr}")
                print(f"  WARNING: calibration preflight unavailable: {_cgerr}")
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
    _mf_probe_json(watchlist_config_path, "watchlist", "ticker", "watchlist entries")
    print(f"\n[5/9] Syncing VCI watchlist from project_isa_vci_watchlist.md...")
    _mf_begin("5", "sync_vci_watchlist")
    vci_md_path = find_memory_file("project_isa_vci_watchlist.md")
    _vci_existing = 0
    try:
        with open(watchlist_config_path, encoding="utf-8") as _f:
            _vci_existing = len((json.load(_f) or {}).get("vci_watchlist") or [])
    except Exception:
        _vci_existing = 0
    if args.skip_vci_sync:
        print(f"  SKIPPED (--skip-vci-sync) -- reusing {_vci_existing} existing vci_watchlist entr(y/ies).")
        if _vci_existing == 0:
            warnings.append("Step 5 SKIPPED via --skip-vci-sync but watchlist_tickers.json holds "
                            "NO vci_watchlist entries -- run one pass without the flag.")
            degraded = True
    elif not vci_md_path:
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
    _mf_probe_json(watchlist_config_path, "vci_watchlist", "ticker", "VCI entries")
    print(f"\n[6/9] Fetching watchlist & sleeve metrics (yfinance)...")
    _mf_begin("6", "fetch_watchlist_metrics")
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
    elif _skip_fetch_reason(args, watchlist_metrics_path, watchlist_config_path):
        # IDEMPOTENCE (31-Jul-2026). The pre-run prose runs this orchestrator TWICE — once to
        # build the base data, once after the metrics fetch to rebuild Steps 7-9 on live scores.
        # Re-fetching on the second pass costs ~20s of the 45s bash budget and can only make the
        # data staler-or-equal, so a populated metrics file that already covers the CURRENT
        # ticker set short-circuits Step 6. `--skip-fetch` forces the same path unconditionally.
        _reason = _skip_fetch_reason(args, watchlist_metrics_path, watchlist_config_path)
        print(f"  SKIPPED -- {_reason} (existing metrics preserved).")
        if not os.path.exists(watchlist_metrics_path):
            # --skip-fetch on a cold folder (the fast first pass of the two-phase pre-run):
            # downstream steps require the file to exist, so seed the documented placeholder.
            with open(watchlist_metrics_path, "w", encoding="utf-8") as f:
                json.dump({"_meta": {"month_label": month_label}, "tickers": {},
                           "_warning": "--skip-fetch: metrics not pulled on this pass"}, f)
            print("  Placeholder metrics file written (fetch deferred to the standalone STEP 2).")
        try:
            with open(watchlist_metrics_path, encoding="utf-8") as f:
                _wm = json.load(f)
            summary["watchlist_tickers_scored"] = len(_wm.get("tickers", {}))
            summary["in_window_names"] = _wm.get("_meta", {}).get("in_window_tickers", [])
            print("  Scored " + str(summary["watchlist_tickers_scored"])
                  + " tickers (from existing file) | In-window: "
                  + str(summary["in_window_names"]))
        except Exception as _e:
            warnings.append(f"Step 6 skip: could not summarise existing metrics ({_e}).")
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
            # ARCHITECTURE (corrected 31-Jul-2026): the metrics fetch is LOCAL-PRIMARY. It runs
            # in this sandbox via yfinance on tmpfs; Composio is fallback-only and there is NO
            # out-of-band transfer step. The previous note here claimed the opposite and told the
            # operator to "run the Composio metrics pull + transfer", contradicting the pre-run
            # prompt. It also made a REAL failure look benign: fetch_watchlist_metrics raised
            # NameError on FETCH_WORKERS at every invocation (undefined name, fixed 31-Jul), and
            # this branch reported it as "local yfinance unavailable (expected)". A fetch failure
            # is now always surfaced as a WARNING naming the actual error.
            # Reusing stale metrics is still permitted (better than nothing) but never silent.
            metrics_n, missing = metrics_coverage(watchlist_metrics_path, watchlist_config_path)
            if metrics_n > 0:
                print(f"  WARNING: local metrics fetch FAILED ({msg.splitlines()[0][:160]}) "
                      f"-- falling back to the existing metrics file ({metrics_n} tickers).")
                warnings.append(f"Step 6 (fetch_watchlist) FAILED: {msg.splitlines()[0][:200]} "
                                f"-- reusing pre-existing metrics ({metrics_n} tickers). Scores may "
                                f"be stale; investigate before relying on Step 9 output.")
                degraded = True
                if missing:
                    warnings.append(f"Step 6: reused metrics do not cover {len(missing)} current "
                                    f"name(s), which will be unscored: {missing[:20]}"
                                    + (" ..." if len(missing) > 20 else ""))
            else:
                warnings.append(f"Step 6 (fetch_watchlist): {msg} AND no usable metrics file present "
                                "-- downstream scoring will be empty. Re-run the local fetch "
                                "(see the pre-run task STEP 2 recipe); use the Composio fallback "
                                "only if the local sandbox cannot reach Yahoo at all.")
                print(f"  WARNING: {msg} (no metrics available)")
                degraded = True
                if not os.path.exists(watchlist_metrics_path):
                    with open(watchlist_metrics_path, "w", encoding="utf-8") as f:
                        json.dump({"_meta": {"month_label": month_label}, "tickers": {},
                                   "_warning": "local fetch failed, no prior metrics: " + msg}, f)
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
    # Step 6 coverage — tickers priced / tickers the current watchlist needs. This is the
    # number STOXX600 publishes a ranking on without saying so; here it is declared.
    try:
        _n6, _miss6 = metrics_coverage(watchlist_metrics_path, watchlist_config_path)
        _need6 = _n6 + len(_miss6)
        _mf_measure(rows_in=_need6 or None, rows_out=_n6,
                    coverage=(_n6 / _need6) if _need6 else None,
                    note=(f"missing: {_miss6[:12]}" if _miss6 else None))
    except Exception as _e6:
        _mf_measure(note=f"coverage probe failed: {_e6}")

    print(f"\n[6.5] VCI forward-led re-price (fv_asymmetry / VCI_Source_Score at live price)...")
    _mf_begin("6.5", "vci_reprice")
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
    _mf_probe_json(watchlist_metrics_path, "tickers", None, "VCI re-price / metrics tickers")
    print(f"\n[7/9] Scoring Part A/B and building email structures...")
    _mf_begin("7", "normalise_adapter")
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
    # Step 7 coverage — how many scored rows carry a LIVE total score. `rows exist but the
    # values do not` is the exact shape the FETCH_WORKERS bug produced, so non_null_share is
    # declared separately from the row count and run_manifest ERRORs on it.
    try:
        with open(watchlist_scored_path, encoding="utf-8") as _f7:
            _sc7 = json.load(_f7)
        _cr7 = _sc7.get("conviction_ranking", []) or []
        _live7 = sum(1 for e in _cr7
                     if (e.get("total_score_54") or e.get("total_score_50")
                         or e.get("total_score_36")) is not None)
        _mf_measure(rows_out=len(_cr7),
                    coverage=(_live7 / len(_cr7)) if _cr7 else None,
                    non_null_share=(_live7 / len(_cr7)) if _cr7 else None,
                    note=f"{_live7}/{len(_cr7)} rows carry a live total score")
    except Exception as _e7:
        _mf_measure(note=f"scored-file probe failed: {_e7}")

    print(f"\n[7.25] Building composite entry levels...")
    _mf_begin("7.25", "entry_level_builder")
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
    _mf_probe_json(entry_audit_path, "entries", "ticker", "entry levels built")
    print(f"\n[7.5] Re-ranking watchlist on live re-scored values...")
    _mf_begin("7.5", "rerank_watchlist")
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
                            "(watchlist left on screening-score order). Step 6 produced no usable "
                            "metrics -- fix the LOCAL fetch first (31-Jul-26: the old 'metrics not yet "
                            "transferred' wording described a retired Composio hand-off and masked a "
                            "real local-fetch failure).")
            print("  SKIPPED -- no live scores present (re-run the local metrics fetch, pre-run STEP 2).")
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
    _mf_probe_json(watchlist_config_path, "watchlist", "ticker", "re-ranked watchlist")
    print(f"\n[8/9] Building Step 9 pre-scored output...")
    _mf_begin("8", "step9_pre_builder")
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
    # Step 8 coverage — deployment_priority_rank is the PRIMARY candidate ranking for Step 9.
    # An empty one is not "no opportunities", it is a failed build, and run_manifest ERRORs.
    try:
        with open(step9_pre_path, encoding="utf-8") as _f8:
            _s9 = json.load(_f8)
        _dpr = _s9.get("deployment_priority_rank", []) or []
        _scored8 = sum(1 for e in _dpr if e.get("source_score") is not None)
        _mf_measure(rows_out=len(_dpr),
                    coverage=(_scored8 / len(_dpr)) if _dpr else None,
                    non_null_share=(_scored8 / len(_dpr)) if _dpr else None,
                    note=f"deployment_priority_rank={len(_dpr)}, source_score present on {_scored8}")
        # The action stack is a separate consumer with its own refusal threshold.
        _as_path = os.path.join(SCRIPT_DIR, f"action_stack_{month_label}.json")
        if os.path.exists(_as_path) and _RM is not None:
            with open(_as_path, encoding="utf-8") as _fa:
                _asj = json.load(_fa)
            _stack = _asj.get("stack", _asj if isinstance(_asj, list) else []) or []
            _cov_a = (_scored8 / len(_dpr)) if _dpr else None
            _allowed, _why = _RM.gate_emission("action_stack", coverage=_cov_a,
                                               rows_out=len(_stack), raise_on_refuse=False)
            if not _allowed:
                # Honour the SAME registered acknowledgement that covers the step whose
                # coverage this refusal is judging. Acknowledging a condition in one place
                # while hard-blocking on it in another yields a run that cannot proceed and
                # cannot explain itself.
                _ack = None
                if MANIFEST is not None:
                    _ack = (_RM.acknowledgement_for("action_stack", _why, MANIFEST.acks)
                            or _RM.acknowledgement_for("8", _why, MANIFEST.acks))
                    if _ack and _ack.get("_expired"):
                        _ack = None
                if _ack:
                    warnings.append(f"ACTION STACK emission ACKNOWLEDGED under "
                                    f"{_ack['registry_id']} until {_ack['_expiry_date']} "
                                    f"(true status: REFUSED). " + _why)
                    print(f"  ACKNOWLEDGED ({_ack['registry_id']}, expires "
                          f"{_ack['_expiry_date']}): " + _why)
                else:
                    errors.append("ACTION STACK REFUSED TO EMIT: " + _why)
                    print("  ERROR: " + _why)
            elif _why:
                warnings.append(_why)
    except Exception as _e8:
        _mf_measure(note=f"step9_pre probe failed: {_e8}")

    # ---------------------------------------------------------------------------
    # Step 8b (Capture Layer Item 3 / Dashboard Spec 7.6.2): conviction skeleton.
    # Emits step9_conviction_[mmm_yyyy].json with every MACHINE-computed field filled and
    # every JUDGEMENT field explicitly null. The review session completes D8/D9/D10 with
    # their rationales before the email sends; conviction_capture.write() refuses the file
    # until it has. Prefilling a plausible default would be worse than no capture at all --
    # it would look like reasoning.
    # ---------------------------------------------------------------------------
    # R4 (Aug-2026 retrospective item 4): log the (t1_qualified x est_rev_direction) joint
    # distribution every month, so "every T1-qualified name has NEUTRAL revisions while every
    # improving-revision name failed a gate" is MEASURED rather than noticed once. Registered
    # as CAP-3; reviewed after six runs. Nothing is changed here (H7).
    try:
        import gate_variables as _gv
        with open(step9_pre_path, encoding="utf-8") as _f:
            _s9r4 = json.load(_f)
        _mx4 = {}
        try:
            with open(watchlist_metrics_path, encoding="utf-8") as _fm:
                _mx4 = json.load(_fm)
        except Exception as _mxe:
            # Do NOT let this fail silently: null levels here look identical to measured
            # zeros downstream, and that conflation is the defect this whole file exists to
            # remove. Record the reason on the run.
            flags.append({"type": "GATE_VARIABLES_LEVELS_UNAVAILABLE",
                          "message": f"watchlist_metrics unreadable ({_mxe}); monthly "
                                     f"gate_variables rows will carry NO measured levels."})
        _n4, _t4 = _gv.log_monthly_t1_distribution(_s9r4, run_date.isoformat(), metrics=_mx4)
        _ct = _gv.revisions_crosstab(group="MONTHLY_T1", run_date=run_date.isoformat())
        summary["t1_revisions_crosstab"] = _ct
        print(f"  [R4] A4 qualification x 30d revisions logged ({_n4} names): "
              f"improving_and_qualified={_ct.get('improving_and_qualified')} of "
              f"{_ct.get('improving_total')} improving in the universe")
        if _ct.get("improving_total") and not _ct.get("improving_and_qualified"):
            flags.append({
                "type": "FORWARD_SIGNAL_ABSENT_FROM_QUALIFIED_SET",
                "message": ("No A4-qualified name carries improving 30-day revisions this "
                            f"month, while {_ct['improving_total']} name(s) in the universe do "
                            "and each failed a gate. In a forward-led framework that is worth "
                            "reading before Step 9. EVIDENCE ONLY - registered as CAP-3, "
                            "reviewed after six runs; no gate changes on one month."),
            })
    except Exception as _r4e:
        warnings.append(f"R4 revisions crosstab skipped: {_r4e}")

    print("\n[8b] Building Step 9 conviction skeleton (judgement capture)...")
    _mf_begin("8b", "conviction_capture")
    try:
        import conviction_capture as _cc
        _regime = None
        try:
            _regime = (summary.get("macro") or {}).get("regime")
        except Exception:
            pass
        _cdoc = _cc.prefill(month_label, here=SCRIPT_DIR, regime=_regime)
        _cpath = os.path.join(SCRIPT_DIR, f"step9_conviction_{month_label}.json")
        _cerrs = _cc.validate(_cdoc, strict_judgement=False)
        if _cerrs:
            warnings.append("Step 8b conviction skeleton structural errors: "
                            + "; ".join(_cerrs[:4]))
        with open(_cpath, "w", encoding="utf-8") as _cf:
            json.dump(_cdoc, _cf, indent=2, ensure_ascii=False)
        _mf_measure(rows_out=len(_cdoc["names"]),
                    coverage=(1.0 if not _cerrs else 0.0),
                    note=f"names={len(_cdoc['names'])}, "
                         f"not_progressed={len(_cdoc['not_progressed'])}")
        summary["conviction_capture"] = {
            "path": _cpath, "names": len(_cdoc["names"]),
            "not_progressed": len(_cdoc["not_progressed"]),
            "state": "SKELETON_AWAITING_JUDGEMENT",
        }
        print(f"  Conviction skeleton: {len(_cdoc['names'])} names, "
              f"{len(_cdoc['not_progressed'])} not-progressed -> {os.path.basename(_cpath)}")
        print("  Step 9 must fill D8/D9/D10 score + rationale for every name before the email "
              "sends.")
    except Exception as _cce:
        warnings.append(f"Step 8b (conviction skeleton) failed: {_cce}")
        _mf_measure(status="ERROR", note=f"conviction prefill failed: {_cce}")
        print(f"  WARNING: conviction skeleton failed: {_cce}")

    # ---------------------------------------------------------------------------
    # Step 8c (Capture Layer Item 4): freeze this month's action stack immutably into the
    # shadow ledger, then mark every cohort. Freezing happens HERE, at run time, because a
    # stack reconstructed later is a stack chosen with hindsight. Marking is best-effort and
    # resumable -- a price-fetch shortfall must never block the review.
    # ---------------------------------------------------------------------------
    print("\n[8c] Freezing action stack into the shadow ledger (Books A/B/C)...")
    _mf_begin("8c", "shadow_ledger")
    try:
        import shadow_ledger as _sl
        _coh, _written = _sl.freeze(month_label, here=SCRIPT_DIR)
        print(f"  Cohort {month_label}: {_coh['n_buys']} BUY recommendations, "
              f"hash {_coh['stack_hash']} "
              f"({'frozen' if _written else 'already frozen — identical'})")
        _mf_measure(rows_out=_coh["n_buys"], coverage=1.0,
                    note=f"stack_hash={_coh['stack_hash']} written={_written}")
        if not args.dry_run:
            try:
                _marks = _sl.mark(asof=run_date.isoformat(), here=SCRIPT_DIR)
                summary["shadow_ledger"] = {
                    "cohorts": len(_marks),
                    "books": {m: {b: v["return"] for b, v in mk["books"].items()}
                              for m, mk in _marks.items()},
                }
                print("  " + _sl.report(here=SCRIPT_DIR).replace("\n", "\n  ")[:1400])
            except Exception as _sme:
                warnings.append(f"Shadow ledger marking incomplete (resumable): {_sme}")
                print(f"  NOTE: marking incomplete (resumable): {_sme}")
    except _sl.ImmutableCohortError as _ice:   # noqa: F821
        errors.append("SHADOW LEDGER IMMUTABILITY: " + str(_ice))
        _mf_measure(status="ERROR", note=str(_ice)[:300])
        print("  ERROR: " + str(_ice))
    except Exception as _sle:
        warnings.append(f"Step 8c (shadow ledger) failed: {_sle}")
        _mf_measure(status="ERROR", note=f"shadow freeze failed: {_sle}")
        print(f"  WARNING: shadow ledger failed: {_sle}")

    print(f"\n[9/9] Pre-populating email JSON...")
    _mf_begin("9", "email_prefill")
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
    # ---------------------------------------------------------------------------
    # Step 9b.5 (WP-C, 29-Jul-2026): read the between-run position-alert store written by the
    # weekly EPS snapshot task. DETECTION ONLY -- surfaced as Step 5 context for the session;
    # never an action, and never a sell signal (C-1). Absence of the file is normal and silent.
    # ---------------------------------------------------------------------------
    _alerts_path = os.path.join(SCRIPT_DIR, "position_alerts.json")
    _alerts_block = {"status": "NONE"}
    try:
        if os.path.exists(_alerts_path):
            with open(_alerts_path, encoding="utf-8") as _af:
                _ap = json.load(_af)
            _al = _ap.get("alerts") or []
            _stale = (_ap.get("as_of_date") or "") < (run_date - timedelta(days=14)).isoformat()
            _alerts_block = {
                "status": "ALERTS" if _al else "NONE",
                "as_of": _ap.get("as_of_date"),
                "n_alerts": len(_al),
                "n_held": sum(1 for a in _al if a.get("class") == "HELD"),
                "n_in_min_hold": sum(1 for a in _al if a.get("in_min_hold_window")),
                "alerts": _al,
                "stale": bool(_stale),
                "doctrine": _ap.get("doctrine"),
            }
            if _stale:
                warnings.append("Position alerts store is >14 days old (as_of %s) -- the weekly EPS "
                                "snapshot task may not be running." % _ap.get("as_of_date"))
            if _al:
                print("  POSITION ALERTS: %d (%d held, %d inside min-hold window -- context only)"
                      % (len(_al), _alerts_block["n_held"], _alerts_block["n_in_min_hold"]))
        else:
            _alerts_block = {"status": "NO_STORE",
                             "note": "position_alerts.json absent -- weekly alert task has not written yet."}
    except Exception as _pae:
        warnings.append("Position alerts read: " + str(_pae))
        _alerts_block = {"status": "ERROR", "error": str(_pae)[:200]}
    summary["position_alerts"] = _alerts_block

    # Step 9c (Jul-26 Part 9): Calibration report — surface each signal's forward-return IC by horizon
    # (1m/3m/6m/12m, filling left-to-right as the score panel matures). Evidence only; never blocks.
    # ---------------------------------------------------------------------------
    _mf_probe_json(email_path, None, None, "email data top-level sections")
    print(f"\n[cal] Calibration report (signal IC by horizon)...")
    _mf_begin("cal", "calibration_report")
    calib_report_path = os.path.join(SCRIPT_DIR, f"calibration_report_{month_label}.md")
    calib_summary = {}
    try:
        _panel_store = os.path.join(SCRIPT_DIR, "score_panel.csv")
        if os.path.exists(_panel_store):
            # WP-B (29-Jul-26): batched+cached price fetch (the old per-ticker call could not
            # finish inside run_script's 120s budget once the panel passed ~1k rows) and a
            # store-growth assertion (a learning loop that silently stops writing is the exact
            # failure the VCI store hit Apr-Jul 26). NOT_DONE is a WARN, never fatal: the price
            # cache is resumable and the next run continues it.
            ok, stdout, stderr = run_script(
                "calibration_report",
                ["--store", _panel_store, "--asof", run_date.isoformat(), "--out", calib_report_path,
                 "--price_cache", os.path.join(SCRIPT_DIR, "calibration_prices.csv"),
                 "--growth-state", os.path.join(SCRIPT_DIR, "calibration_state.json"),
                 "--assert-growth"],
                dry_run=args.dry_run,
            )
            _out = (stdout or "") + (stderr or "")
            if "PANEL_STALE" in _out:
                # 02-Aug-2026 (Aug retrospective item 3): PANEL_STALE is an ERROR, not a WARN,
                # whenever a weekly screen has demonstrably run MORE RECENTLY than the newest
                # row in the panel. That comparison is free: both dates are already on disk.
                #
                # Why it has to be an error. The panel is simultaneously the input to the
                # calibration IC report, the C-1 entry-stability gate and the A5v3 sightings
                # test. When it stops growing, THREE separate safeguards degrade together and
                # not one of them raises. A warning in a list of twenty warnings is not a
                # control; it is a note.
                #
                # A stale panel with NO newer screen is a different fact -- no screen has run,
                # which is expected between weekly slots -- and stays a warning.
                _newest_panel, _newest_screen = None, None
                try:
                    import csv as _csv, glob as _glob, re as _re
                    from datetime import datetime as _dtm
                    _pp = os.path.join(SCRIPT_DIR, "score_panel.csv")
                    if os.path.exists(_pp):
                        with open(_pp, encoding="utf-8", newline="") as _f:
                            for _row in _csv.DictReader(_f):
                                _d = str(_row.get("run_date", ""))[:10]
                                if _d and (_newest_panel is None or _d > _newest_panel):
                                    _newest_panel = _d
                    _MON = {m: i for i, m in enumerate(
                        ["jan","feb","mar","apr","may","jun",
                         "jul","aug","sep","oct","nov","dec"], start=1)}
                    for _pat in (os.path.join(SCRIPT_DIR, "Growth Stock Analysis*.xlsx"),
                                 os.path.join(SCRIPT_DIR, "archive", "*",
                                              "Growth Stock Analysis*.xlsx")):
                        for _fp in _glob.glob(_pat):
                            _m = _re.search(r"W-e (\d{2})-([A-Za-z]{3})-(\d{2})",
                                            os.path.basename(_fp))
                            if not _m:
                                continue
                            _iso = "20%s-%02d-%s" % (_m.group(3),
                                                     _MON.get(_m.group(2).lower(), 0),
                                                     _m.group(1))
                            if _newest_screen is None or _iso > _newest_screen:
                                _newest_screen = _iso
                except Exception:
                    pass

                if _newest_screen and _newest_panel and _newest_screen > _newest_panel:
                    errors.append(
                        "PANEL_STALE (ERROR): the newest weekly screen workbook is dated "
                        f"{_newest_screen} but score_panel.csv stops at {_newest_panel}. A "
                        "screen ran and did NOT call score_panel_logger. Three safeguards "
                        "degrade together on this one input -- the calibration IC report, the "
                        "C-1 entry-stability gate and the A5v3 sightings test -- and none of "
                        "them raises on its own. Re-run that screen's logging step before "
                        "trusting any of the three.")
                    print(f"  ERROR: PANEL_STALE -- screen {_newest_screen} newer than panel "
                          f"{_newest_panel}; a weekly screen is not logging.")
                else:
                    warnings.append(
                        "Calibration: score_panel.csv did not grow since the last calibration "
                        f"run (newest panel row {_newest_panel or 'unknown'}; newest screen "
                        f"{_newest_screen or 'none found'}). No NEWER screen was found, so this "
                        "is consistent with no screen having run yet -- not evidence of a "
                        "logging failure.")
                    print("  WARNING: PANEL_STALE -- panel did not grow, but no newer screen "
                          "exists either.")
            if "NOT_DONE" in _out:
                warnings.append("Calibration: price cache incomplete this run (resumable) -- IC table "
                                "will fill on the next run; no report written.")
                print("  NOTE: price cache incomplete (resumable) -- continuing.")
                calib_summary = {"status": "PRICE_CACHE_INCOMPLETE", "report_path": calib_report_path}
            elif ok:
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

    # ---------------------------------------------------------------------------
    # Step 9d (Fix Pack Jul-2026 Doc A, P2): mechanical pre-run asserts.
    # A10 X-Ray schema · P7a MoM baseline · A11/P6 fund-returns cache · A22 allowance
    # surface · A20 reversal worklist · A19 anchor derivation/coherence · A18 checker.
    # Failures land in errors[]/warnings[] per the existing ERROR protocol (invariant 3).
    # ---------------------------------------------------------------------------
    # Calibration coverage — the panel is the input to the IC report, the C-1 entry-stability
    # gate and the A5v3 sightings test. When it stops growing all three degrade together and
    # none of them errors, which is why its size is declared rather than assumed.
    try:
        _panel_p = os.path.join(SCRIPT_DIR, "score_panel.csv")
        _n_panel = 0
        if os.path.exists(_panel_p):
            with open(_panel_p, encoding="utf-8", errors="ignore") as _pf:
                _n_panel = max(sum(1 for _ in _pf) - 1, 0)
        _mf_measure(rows_out=_n_panel, note=f"score_panel rows={_n_panel}")
    except Exception as _ecal:
        _mf_measure(note=f"panel probe failed: {_ecal}")

    # CAPTURE LAYER ITEM 5 — extend the price cache toward the FULL constituent universe,
    # one resumable chunk per run. Item 1 records gate variables for every constituent so
    # rule_frictions can ask "did the names our gates BLOCKED subsequently perform?"; that
    # question needs prices for the blocked names, which is exactly what the panel-derived
    # cache never had. Best-effort and resumable: a shortfall defers work, it never loses any.
    if not args.skip_universe_prices and not args.dry_run:
        try:
            import calibration_universe as _cu
            for _g in ("STOXX600", "F250-SPI"):
                _r = _cu.extend(_g, chunk=120, period="6mo")
                if _r.get("fetched"):
                    print(f"  [prices] {_g}: +{_r['fetched']} "
                          f"(resolution {_r.get('batch_resolution')}), "
                          f"remaining {_r['remaining']}")
                if _r.get("remaining"):
                    break            # one group per run; the cache resumes next month
            _cov = _cu.coverage()
            summary["universe_price_coverage"] = _cov
            if _cov.get("acceptance") == "FAIL":
                warnings.append(
                    "Universe price coverage below the 85%% floor: "
                    + ", ".join(f"{g}={v.get('resolution_vs_universe')}"
                                for g, v in (_cov.get("groups") or {}).items()))
        except Exception as _upe:
            warnings.append(f"Universe price extension skipped: {_upe}")

    # ── MISSED-OPPORTUNITY ATTRIBUTION (§7.2) ────────────────────────────────────────────
    # Wired 12-Aug-2026, register ISA-0003. Until then MOA was ARCHIVED by the Run_Context,
    # contracted by the dashboard and served at /api/v1/missed-opportunity - and produced by
    # NO RUN. The framework was archiving a file nothing made.
    #
    # It runs against the PREVIOUS month, not this one: build() reads step9_pre_* and
    # entry_level_audit_*, which only exist once a month's decisions have been made. Asking
    # it about the month the pre-run is preparing would give it nothing to attribute.
    #
    # Best-effort like the universe-price stage: MOA is retrospective, so a failure defers
    # the question by a month and loses nothing. It must never block a pre-run.
    if not args.skip_moa and not args.dry_run:
        try:
            import missed_opportunity_diag as _moa
            _prev = _previous_month_label(month_label)
            _doc = _moa.build(_prev, fetch=True)
            # 12-Aug-2026: this read HERE, which is not defined in this module (the
            # convention is SCRIPT_DIR, line 62). Inside a broad `except Exception` that
            # only appends a warning, so the MOA stage would have raised NameError and
            # SKIPPED SILENTLY on every pre-run - the same absent-execution class the stage
            # was wired to close one day earlier. See ISA-0210.
            _out = os.path.join(SCRIPT_DIR, f"missed_opportunity_{_prev}.json")
            with open(_out, "w", encoding="utf-8") as _fh:
                json.dump(_doc, _fh, indent=1)
            summary["missed_opportunity"] = {"month": _prev, "path": _out}
            print(f"  [MOA] {_prev}: written -> {os.path.basename(_out)}")
        except Exception as _moe:
            warnings.append(f"Missed-opportunity attribution skipped for the prior month: {_moe}")

    # TWO-REGIME RESOLUTION (02-Aug-2026). macro_regime (Step 4 judgement, forward, economic)
    # and market_regime (drawdown_monitor, mechanical, lagging price) are DIFFERENT VARIABLES,
    # not two readings of one. The pre-run emits both under their namespaced names plus the
    # advisory Category-8 composite, so the review never has to decide which "regime" was meant.
    try:
        import regime_resolver as _rr
        _macro_prev = None
        try:                                    # last month's Step 4 call, for continuity only
            import glob as _g2
            _prevs = sorted(_g2.glob(os.path.join(SCRIPT_DIR, "run_context_*.json")))
            for _pf in reversed(_prevs):
                if month_label in _pf:
                    continue
                with open(_pf, encoding="utf-8") as _f:
                    _macro_prev = ((json.load(_f).get("summary") or {})
                                   .get("regimes") or {}).get("macro_regime")
                if _macro_prev:
                    break
        except Exception:
            pass
        _reg = _rr.resolve(macro_regime=_macro_prev, state_path=os.path.join(
            SCRIPT_DIR, "drawdown_state.json"))
        _reg["macro_regime_prior_month"] = _macro_prev
        _reg["macro_regime_note"] = (
            "macro_regime is a STEP 4 OUTPUT and is not known at pre-run time. The value above "
            "is last month's, carried for continuity only. Step 4 must state this month's call "
            "as REGIME: [Expansion|Slowdown|Contraction|Recovery], and Step 8 Category 8 must "
            "re-resolve with it.")
        summary["regimes"] = _reg
        print(f"  [regime] macro={_reg['macro_regime'] or 'PENDING Step 4'} (prior month) | "
              f"market={_reg['market_regime']} | {_reg['relationship']['state']}")
        print(f"           Category 8 (advisory): "
              f"{'ELIGIBLE' if _reg['category8']['eligible'] else 'not eligible'} "
              f"-- {_reg['category8']['reason'][:110]}")
    except Exception as _rge:
        warnings.append(f"Two-regime resolution skipped: {_rge}")

    _w_before_9d = len(warnings)
    _e_before_9d = len(errors)
    print("\n[9d] Fix Pack P2 asserts (A10/A11/A18/A19/A20/A22 + P7a)...")
    _mf_begin("9d", "mechanical_asserts")

    # — A10: X-Ray country schema assert (parser whitelists; surface its warnings, assert rows) —
    try:
        with open(xray_path, encoding="utf-8") as f:
            _xr = json.load(f)
        _pw = (_xr.get("_meta") or {}).get("parse_warnings") or []
        if _pw:
            warnings.append(f"A10 xray parse_warnings ({len(_pw)}): " + "; ".join(map(str, _pw[:5])))

        def _pct_ok(v, allow_none=True):
            if v is None:
                return allow_none
            return isinstance(v, (int, float)) and 0.0 <= v <= 100.0

        _rows = _xr.get("country_exposure") or []
        _bad = [r for r in _rows
                if not _pct_ok(r.get("equity_pct"), allow_none=False)
                or not _pct_ok(r.get("benchmark_pct"))]
        if _bad:
            errors.append(f"A10 xray schema: {len(_bad)} invalid country rows (e.g. {_bad[0]})")
            print(f"  A10 FAILED: {len(_bad)} invalid country rows")
        elif _rows:
            _tot = sum(r["equity_pct"] for r in _rows)
            if not (60.0 <= _tot <= 130.0):
                warnings.append(f"A10 xray: country equity_pct total {round(_tot, 1)} implausible (expect ~100)")
            print(f"  A10 OK: {len(_rows)} country rows, total {round(_tot, 1)}")
        else:
            warnings.append("A10 xray: country_exposure EMPTY after whitelist — check PDF layout")
    except Exception as _e:
        warnings.append(f"A10 xray assert skipped: {_e}")

    # — P7a: SUSPECT_BASELINE — a near-zero MoM move across a full month means a stale baseline —
    try:
        if prior_port_path and os.path.exists(prior_port_path) and os.path.exists(portfolio_path):
            with open(portfolio_path, encoding="utf-8") as f:
                _cur = json.load(f)
            with open(prior_port_path, encoding="utf-8") as f:
                _pri = json.load(f)
            _cv = (_cur.get("summary") or {}).get("total_value_gbp")
            _pv = (_pri.get("summary") or {}).get("total_value_gbp")
            if _cv is not None and _pv is not None and abs(_cv - _pv) < 5.0:
                flags.append({"type": "SUSPECT_BASELINE",
                              "message": (f"|MoM| = £{abs(_cv - _pv):.2f} < £5 across a month (P7a) — "
                                          f"baseline may be stale/carried; verify prior file "
                                          f"{os.path.basename(prior_port_path)}")})
                print(f"  P7a FLAG: SUSPECT_BASELINE (|MoM| £{abs(_cv - _pv):.2f})")
    except Exception as _e:
        warnings.append(f"P7a baseline check skipped: {_e}")

    # — A11/P6: fund-returns cache — INVOKE fund_returns (the July failure: path defined, never
    #   called), write returns back into analytics, DEGRADED status is a hard warning —
    fund_cache_status = "OK"
    try:
        import fund_returns as _fr
        with open(portfolio_path, encoding="utf-8") as f:
            _pd2 = json.load(f)
        _funds = _pd2.get("funds", [])
        # WP-5 (26-Jul-26): hedge-bucket exclusion from the Section-A gate set
        try:
            _tw5 = json.load(open(os.path.join(SCRIPT_DIR, "target_weights.json"),
                                  encoding="utf-8")).get("funds", {})
            _funds = [f for f in _funds
                      if (_tw5.get(f.get("ticker"), {}) or {}).get("bucket") != "hedge"]
        except Exception:
            pass
        _cache_path = os.path.join(SCRIPT_DIR, "fund_returns_cache.json")
        if _funds:
            _rets = _fr.source_fund_returns(_funds, cache_path=_cache_path,
                                            fetch=not args.dry_run)
            _n_pend = sum(1 for v in _rets.values() if v.get("pending"))
            _n_stale = sum(1 for v in _rets.values() if v.get("stale"))
            if not os.path.exists(_cache_path) or _n_pend or _n_stale:
                fund_cache_status = "DEGRADED"
                warnings.append(f"A11 fund_cache_status=DEGRADED — pending {_n_pend}, stale {_n_stale}, "
                                f"cache exists={os.path.exists(_cache_path)}. Section A/C are "
                                f"LOW-CONFIDENCE until fund_returns_cache.json is seeded "
                                f"(fund_returns.py set — quarterly, Morningstar/Trustnet 3yr ann.)")
            # write back into analytics: fund_drift_table.rows + section_a.fund_rows
            if os.path.exists(analytics_path):
                with open(analytics_path, encoding="utf-8") as f:
                    _ana = json.load(f)
                _touched = 0
                for _tbl in (( _ana.get("fund_drift_table") or {}).get("rows") or [],
                             ( _ana.get("section_a") or {}).get("fund_rows") or []):
                    for _row in _tbl:
                        _ri = _rets.get(_fr._key(_row))
                        if not _ri or _ri.get("est_return_pct") is None:
                            continue
                        _row["est_return_pct"] = _ri["est_return_pct"]
                        _row["est_return_source"] = _ri.get("source")
                        _mr = _row.get("min_return_pct")
                        if _mr is not None:
                            _row["below_threshold"] = bool(_ri["est_return_pct"] < _mr)
                        _touched += 1
                # Section A verdict per D8 bands (A11): PASS >= bands.pass /
                # INCONCLUSIVE bands.inconclusive..pass / FAIL < bands.inconclusive.
                # INCONCLUSIVE needs 2 consecutive months before the Research trigger —
                # persistence is read by the review from prior analytics verdicts.
                try:
                    import scoring_config as _sc_cfg
                    _bands = getattr(_sc_cfg, "FUND_GATE_BANDS", {"pass": 13.0, "inconclusive": 11.0})
                except Exception:
                    _bands = {"pass": 13.0, "inconclusive": 11.0}
                _gate = _fr.compute_fund_gate(_funds, _rets)
                _wavg = _gate.get("weighted_avg_return")
                if _gate.get("result") == "PENDING" or _wavg is None:
                    _verdict = "PENDING"
                elif _wavg >= _bands["pass"]:
                    _verdict = "PASS"
                elif _wavg >= _bands["inconclusive"]:
                    _verdict = "INCONCLUSIVE"
                else:
                    _verdict = "FAIL"
                _ana.setdefault("section_a", {}).update({
                    "weighted_avg_return": _wavg,
                    "coverage_pct": _gate.get("coverage_pct"),
                    "pending_funds": _gate.get("pending_funds"),
                    "verdict": _verdict,
                    "verdict_bands": _bands,
                    "verdict_note": ("D8 bands: PASS >= {p}% / INCONCLUSIVE {i}-{p}% / FAIL < {i}%; "
                                     "INCONCLUSIVE requires 2 consecutive months before the Research "
                                     "trigger.").format(p=_bands["pass"], i=_bands["inconclusive"]),
                    "fund_cache_status": fund_cache_status,
                })
                _ana["fund_cache_status"] = fund_cache_status

                # ── Step 6.08 OVERRIDE — the return architecture is the authority ─────────
                # ⚑ Everything above computes Section A from `est_return`, which register C4
                # proved is not merely noisy but INVERTED (Scottish Mortgage: 14.0% est on a
                # 0.22% realised 5y, the highest score in its bucket). It is retained because
                # the C4 evidence must keep accumulating and because `below_threshold` still
                # drives the drift-table signal — but it no longer decides anything.
                # Section A/B/C now come from return_architecture (Step 6.08): ONE declared
                # expected-return input per holding, thresholds derived from the A19 anchor,
                # and the arithmetic asserted by six invariants. The est-based figure is kept
                # beside it as `est_basis_corroborator` so the two can visibly disagree.
                try:
                    _rap = os.path.join(SCRIPT_DIR, f"return_architecture_{month_label}.json")
                    with open(_rap, encoding="utf-8") as _f:
                        _rar = json.load(_f)
                    _bad_inv = [i for i in _rar.get("invariants", []) if not i.get("holds")]
                    if _bad_inv:
                        warnings.append(
                            "A11/6.08: return_architecture invariants FAILED — Section A/B/C "
                            "left on the est_return basis rather than adopting arithmetic that "
                            "does not reconcile: "
                            + "; ".join(i["invariant"] for i in _bad_inv))
                    else:
                        _sa_new, _sb_new = _rar["section_a"], _rar["section_b"]
                        _sc_new = _rar["section_c"]
                        _prev = dict(_ana.get("section_a") or {})
                        _ana["section_a"].update({
                            "est_basis_corroborator": {
                                "weighted_avg_return": _prev.get("weighted_avg_return"),
                                "verdict": _prev.get("verdict"),
                                "role": ("RETIRED as a decision input (register C4) — retained "
                                         "so the evidence keeps accumulating"),
                            },
                            "weighted_avg_return": _sa_new["value_pct"],
                            "verdict": _sa_new.get("verdict"),
                            "verdict_bands": _sa_new.get("bands"),
                            "coverage_pct": (None if _sa_new.get("coverage") is None
                                             else round(100 * _sa_new["coverage"], 2)),
                            "basis": _rar["operative_basis"],
                            "basis_note": _rar["basis_study"]["definitions"][_rar["operative_basis"]],
                            "source": "return_architecture (Step 6.08)",
                        })
                        _ana["section_b"].update({
                            "value_pct": _sb_new["value_pct"], "verdict": _sb_new.get("verdict"),
                            "bands": _sb_new.get("bands"),
                            "basis": _rar["operative_basis"],
                            "source": "return_architecture (Step 6.08)",
                            "realised_indicative": {
                                "result": _ana["section_b"].get("result"),
                                "note": ("the realised sleeve gain is a MEASUREMENT OF THE PAST "
                                         "and was previously fed into Section C as if it were a "
                                         "forward annual rate; it is kept here as context only"),
                            },
                        })
                        _ana["section_c"] = {
                            **(_ana.get("section_c") or {}),
                            "total_return": _sc_new["value_pct"],
                            "verdict": _sc_new.get("verdict"),
                            "bands": _sc_new.get("bands"),
                            "anchor_pct": _sc_new.get("anchor_pct"),
                            "shortfall_pp": _sc_new.get("shortfall_pp"),
                            "coverage_pct": (None if _sc_new.get("coverage") is None
                                             else round(100 * _sc_new["coverage"], 2)),
                            "basis": _rar["operative_basis"],
                            "status": "computed",
                            "source": "return_architecture (Step 6.08)",
                            "shortfall_attribution": _rar["shortfall_attribution"]["rows"][:8],
                            "levers": _rar["levers"],
                            "levers_note": _rar["not_summable_note"],
                        }
                        # ⚑ ONE DOCUMENT. `return_architecture_{month}.json` is keyed on the
                        # PRE-RUN's month_label ("aug_2026"), while portfolio_data's
                        # `_meta.month_label` is the DATA month ("jul_2026" for a 31-Jul
                        # valuation). Two variables with the same name meaning different
                        # things — so downstream consumers must never resolve this file by
                        # guessing a label. The payload is carried inside analytics instead.
                        _ana["return_architecture"] = {
                            "as_of": _rar["as_of"],
                            "operative_basis": _rar["operative_basis"],
                            "anchor": _rar["anchor"],
                            "thresholds": _rar["thresholds"],
                            "expected_return_inputs": _rar["expected_return_inputs"],
                            "basis_study": _rar["basis_study"],
                            "bucket_minimum_divergence": _rar.get("bucket_minimum_divergence"),
                            "invariants": _rar["invariants"],
                            "source_file": os.path.basename(_rap),
                        }
                        summary["section_c_verdict"] = _sc_new.get("verdict")
                        summary["section_c_pct"] = _sc_new["value_pct"]
                        _verdict = _sa_new.get("verdict") or _verdict
                        print(f"  A11/6.08: Section A/B/C adopted from return_architecture "
                              f"(A {_sa_new['value_pct']}% {_sa_new.get('verdict')} | "
                              f"C {_sc_new['value_pct']}% {_sc_new.get('verdict')}); est-based "
                              f"figure retained as a corroborator")
                except FileNotFoundError:
                    warnings.append(
                        "A11/6.08: return_architecture output missing — Section A stays on the "
                        "est_return basis, which register C4 shows is inverted. Section C will "
                        "again be uncomputed. Investigate Step 6.08.")
                except Exception as _e608:                     # noqa: BLE001
                    warnings.append(f"A11/6.08 adoption FAILED: {type(_e608).__name__}: {_e608}")

                with open(analytics_path, "w", encoding="utf-8") as f:
                    json.dump(_ana, f, indent=2, ensure_ascii=False)
                summary["fund_cache_status"] = fund_cache_status
                summary["section_a_verdict"] = _verdict
                print(f"  A11: fund returns written back ({_touched} rows), Section A verdict "
                      f"{_verdict}, cache {fund_cache_status}")
        else:
            warnings.append("A11: no funds in portfolio_data — fund gate skipped")
    except Exception as _e:
        warnings.append(f"A11 fund-returns step FAILED: {_e}")
        fund_cache_status = "DEGRADED"
        summary["fund_cache_status"] = fund_cache_status

    # — A23 (02-Aug-2026, Aug retrospective item 1): NO Path-B/VCI holding may appear in the
    #   Global Action Stack. The stack scores held names on the Path A forward Source Score;
    #   an option-like pre-revenue platform scored that way ALWAYS returns dead-money, so the
    #   defect recurs every month and always points one way — sell the asymmetric sleeve. In
    #   Aug-2026 it ranked ABCL the #1 SELL: a 6/6-signal position, four weeks before its
    #   Phase 2 binary readout, inside its own min-hold, at a 19% loss.
    #
    #   This is an ERROR, not a warning. A manual override caught it once; a rule that depends
    #   on being noticed is not a rule.
    try:
        _as_p = os.path.join(SCRIPT_DIR, f"action_stack_{month_label}.json")
        _wt_p = os.path.join(SCRIPT_DIR, "watchlist_tickers.json")
        if os.path.exists(_as_p) and os.path.exists(_wt_p):
            with open(_as_p, encoding="utf-8") as _f:
                _asj = json.load(_f)
            with open(_wt_p, encoding="utf-8") as _f:
                _wtj = json.load(_f)
            sys.path.insert(0, SCRIPT_DIR)
            from rerank_watchlist import classify_holding_path as _chp
            _vw = _wtj.get("vci_watchlist") or []
            _paths = {e.get("ticker"): _chp(e, vci_watchlist=_vw)
                      for e in (_wtj.get("stock_sleeve") or []) if e.get("ticker")}
            _vci_tickers = {e.get("ticker") for e in _vw if e.get("ticker")}
            _stack = _asj.get("stack") or []
            _bad, _unknown = [], []
            for _r in _stack:
                _t = _r.get("ticker")
                _cls = _paths.get(_t)
                if _cls == "B" or _t in _vci_tickers or str(_r.get("route", "")).lower() == "vci":
                    _bad.append(f"{_t} (action={_r.get('action')}, rank={_r.get('rank')}, "
                                f"source_score={_r.get('source_score')})")
                elif _cls == "UNKNOWN":
                    _unknown.append(_t)
            if _bad:
                errors.append(
                    "A23 PATH-B LEAK INTO ACTION STACK: " + "; ".join(_bad) +
                    ". Path-B/VCI holdings are assessed on ACS and T1-T7, never on the Path A "
                    "Source Score. Scoring them here can only ever produce a SELL. Fix the "
                    "path/source_pipeline classification in watchlist_tickers.json and re-run "
                    "Step 7.5 -- do NOT override the stack entry by hand.")
                print("  A23: ERROR -- Path-B holding(s) in the action stack: " + "; ".join(_bad))
            elif _unknown:
                warnings.append(
                    f"A23: holdings with an UNRESOLVED path excluded from the action stack: "
                    f"{_unknown}. They were NOT scored (unknown never defaults to Path A), but "
                    f"they are also not being assessed on either track until classified.")
                print(f"  A23: {len(_unknown)} holding(s) unclassified and excluded: {_unknown}")
            else:
                print(f"  A23: action stack clean -- no Path-B/VCI holding present "
                      f"({len(_stack)} rows checked)")
    except Exception as _a23e:
        warnings.append(f"A23 path-leak assert skipped: {_a23e}")

    # — A22: surface broker-reconciled allowance (extract_portfolio.parse_contributions) —
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            _pd3 = json.load(f)
        _contrib = _pd3.get("contributions") or {}
        summary["allowance_used_gbp"] = _contrib.get("allowance_used_gbp")
        summary["allowance_remaining_gbp"] = _contrib.get("allowance_remaining_gbp")
        summary["allowance_reconciled"] = _contrib.get("allowance_reconciled", False)
        summary["allowance_note"] = _contrib.get("coverage_note")
        if not _contrib.get("allowance_reconciled"):
            warnings.append(f"A22 allowance UNRECONCILED: {_contrib.get('coverage_note')} — "
                            f"email §10 must not print a confident figure")
        print(f"  A22: allowance_reconciled={_contrib.get('allowance_reconciled')} "
              f"used={_contrib.get('allowance_used_gbp')}")
    except Exception as _e:
        warnings.append(f"A22 allowance surface skipped: {_e}")

    # — WP-1/WP-4 (26-Jul-26): A14 challenger-price refresh + freeze history; H-6 orphan scan —
    try:
        from email_prefill import compute_challenger_counterfactuals as _ccf
        with open(portfolio_path, encoding="utf-8") as f:
            _pd4 = json.load(f)
        _slv = (_pd4.get("summary") or {}).get("stock_sleeve_value_gbp")
        _muv = next((x.get("value_gbp") for x in (_pd4.get("stocks") or [])
                     if str(x.get("ticker", "")).upper() == "MU"), 0.0)
        import datetime as _dt4
        _st4 = refresh_counterfactual_prices(
            os.path.join(SCRIPT_DIR, "sleeve_counterfactual.json"),
            month_str=_dt4.datetime.now().strftime("%Y-%m"), challenger_fn=_ccf,
            sleeve_value_now=_slv, mu_value_now=_muv)
        print(f"  A14: challenger prices refreshed ({'ok' if _st4 else 'SKIPPED - fetch failed'})")
    except Exception as _e:
        warnings.append(f"A14 refresh skipped: {_e}")
    try:
        from vci_learning import orphan_check as _ocf
        _oc = _ocf()
        if _oc["count"]:
            warnings.append("H-6 ORPHAN-SUSPECT: " + ", ".join(_oc["orphans"]))
            print("  H-6 ORPHAN-SUSPECT: " + ", ".join(_oc["orphans"]))
        else:
            print("  H-6 orphan scan: OK")
    except Exception as _e:
        warnings.append(f"H-6 orphan scan skipped: {_e}")

    # — A20: reversal-cause WORKLIST — every top-N name carrying the reversal flag gets a
    #   targeted Step 9/10 per-ticker pull; staging it here makes a skipped pull DETECTABLE
    #   (non-empty worklist vs empty pull log -> Checkpoint-D fails) —
    reversal_worklist = []
    try:
        if os.path.exists(step9_pre_path):
            with open(step9_pre_path, encoding="utf-8") as f:
                _s9 = json.load(f)
            for _sect in ("main_watchlist", "candidate_pool"):
                for _tier, _lst in (_s9.get(_sect) or {}).items():
                    for _e2 in _lst or []:
                        if "recent_reversal_vs_12_1m" in (_e2.get("review_flags") or []):
                            reversal_worklist.append({
                                "ticker": _e2.get("ticker"), "tier": _tier, "section": _sect,
                                "flag": "recent_reversal_vs_12_1m",
                                "required": "targeted per-ticker news+filings pull at Step 9/10 "
                                            "(NOT Step 3); record cause + thesis-relevance"})
            summary["reversal_flag_tickers"] = [w["ticker"] for w in reversal_worklist]
            print(f"  A20: reversal worklist = {[w['ticker'] for w in reversal_worklist] or 'empty'}")
    except Exception as _e:
        warnings.append(f"A20 reversal worklist skipped: {_e}")

    # — A19: required-return anchor. D-2/D-3/D-4 (12-Aug-2026) REPLACE the April-only rule. —
    #
    # ⚑ WHAT CHANGED AND WHY IT IS SAFER. The old step re-derived only when `run_date.month == 4`
    # and otherwise ran `--check`, which errored on ANY drift above 0.2pp. Under the D-2 two-speed
    # cadence that is wrong in both directions: legitimate reported drift between windows would
    # raise a blocking error, and a scheduled 30-Sep window falling outside April would move
    # nothing. Worse, the condition lived HERE, in the orchestrator — the same shape as the
    # `screener_local` divergence and the "PRICE_MOM_SCORING never took effect" defect.
    #
    # So the calendar condition is DELETED from the orchestrator. The deriver is now run on EVERY
    # pre-run and decides for itself: the reported anchor refreshes unconditionally, and the
    # operative anchor moves only on a cadence authority it names (SCHEDULED_WINDOW / BREAK_GLASS
    # / FLOW_TRIGGER_D4 / INITIALISE). `--check` afterwards is then a pure verification and fails
    # only on genuine staleness, an unapplied update, or an unevaluable D-4 trigger (R14.2:
    # the control moved from "documented rule" to "refusal", which is as far left as it goes).
    try:
        ok, stdout, stderr = run_script("derive_required_return", [], dry_run=args.dry_run)
        if ok:
            _tail = [ln for ln in (stdout or "").strip().splitlines() if ln.strip()]
            for _ln in _tail[-5:]:
                print("  A19: " + _ln.strip()[:160])
            summary["anchor_rederived"] = True
            if "SCHEDULED_WINDOW" in (stdout or "") or "BREAK_GLASS" in (stdout or "") \
                    or "FLOW_TRIGGER_D4" in (stdout or ""):
                summary["anchor_operative_moved"] = True
                print("  A19: ⚑ the OPERATIVE anchor MOVED this run — every anchor-derived gate "
                      "moved with it (D-2)")
            if "DEGRADED" in (stdout or ""):
                warnings.append("A19/D-3: the anchor valuation basis is DEGRADED to spot — "
                                "fewer than 3 month-end observations on file. Expected until "
                                "31-Aug-2026 lands; it is reported, never silent.")
        else:
            errors.append(f"A19 anchor derivation FAILED: {(stderr or stdout or '')[-300:]}")
        ok, stdout, stderr = run_script("derive_required_return", ["--check"], dry_run=args.dry_run)
        if not ok:
            errors.append(f"A19 anchor check FAILED (stale reported value, an unapplied operative "
                          f"update, or an unevaluable D-4 flow trigger): "
                          f"{(stdout or stderr or '').strip()[-300:]}")
            print("  A19 CHECK FAILED")
        else:
            print("  A19: anchor CHECK OK (two-speed cadence coherent)")
    except Exception as _e:
        warnings.append(f"A19 anchor step skipped: {_e}")

    # — A18: prose<->config consistency checker — mismatches are ERRORS (blocking-visible) —
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import consistency_check as _cchk
        _mismatches = _cchk.check_all()
        if _mismatches:
            for _m in _mismatches:
                errors.append(f"A18 consistency: {_m}")
            print(f"  A18: {len(_mismatches)} MISMATCH(ES) -> errors[]")
        else:
            print("  A18: ALL PAIRS GREEN")
    except Exception as _e:
        warnings.append(f"A18 consistency check skipped: {_e}")

    if reversal_worklist:
        summary["reversal_worklist"] = reversal_worklist

    # — B1/B7: drawdown ladder + regime state (drawdown_monitor.py; every scheduled run) —
    try:
        import drawdown_monitor as _ddm
        _cash = (summary.get("cash_effective_gbp") or 0.0)
        _mmf = 0.0
        try:
            import scoring_config as _sc3
            _ceq = set(getattr(_sc3, "CASH_EQUIVALENT_TICKERS", []) or [])
            if _ceq and os.path.exists(portfolio_path):
                with open(portfolio_path, encoding="utf-8") as f:
                    _pd4 = json.load(f)
                _mmf = sum((h.get("value_gbp") or 0.0)
                           for h in _pd4.get("funds", []) + _pd4.get("stocks", [])
                           if h.get("ticker") in _ceq)
        except Exception:
            pass
        _reserve = _ddm.compute_reserve(_cash, _mmf, 0.0,
                                        getattr(_sc3, "DRAWDOWN_BUFFER_GBP", 500.0)
                                        if "_sc3" in dir() else 500.0)
        import yfinance as _yf
        _h = _yf.Ticker("VUAG.L").history(period="2y")
        _closes = list(_h["Close"].dropna()) if _h is not None and len(_h) else []
        if _closes:
            _dstate = _ddm.load_state()
            _dstate, _fired = _ddm.update_ladder(_dstate, _closes)
            _regime, _rbasis = _ddm.classify_regime(_closes, _dstate["drawdown_pct"])
            _dstate["regime_state"], _dstate["regime_basis"] = _regime, _rbasis
            _dstate["reserve_gbp"] = _reserve
            _ddm.save_state(_dstate)
            _dblock = _ddm.emit_block(_dstate, _fired, _reserve)
            summary["drawdown"] = _dblock
            summary["regime_state"] = _regime
            if _fired:
                flags.append({"type": "DRAWDOWN_TRIGGER",
                              "message": json.dumps(_dblock.get("DRAWDOWN_TRIGGER"))})
            print(f"  B1: {_dblock['standing_line']}")
        else:
            warnings.append("B1 drawdown monitor: no VUAG history this run")
    except Exception as _e:
        warnings.append(f"B1 drawdown monitor skipped: {_e}")

    # — B2: MMF cash-yield sweep rule (D14) — mechanical SWEEP/reverse-sweep line —
    try:
        import scoring_config as _sc4
        _ceq = list(getattr(_sc4, "CASH_EQUIVALENT_TICKERS", []) or [])
        _min = float(getattr(_sc4, "MMF_SWEEP_MIN_GBP", 1500.0))
        _deployable = summary.get("cash_deployable_gbp") or 0.0
        if not _ceq:
            summary["mmf_sweep"] = {"status": "NOT_CONFIGURED",
                                    "note": ("B2: CASH_EQUIVALENT_TICKERS empty — instrument "
                                             "selection is Raj's one-off JUDGMENT (GBP MMF UCITS, "
                                             "OCF<=0.15%, AUM>=£500m, on AJ Bell)")}
        elif _deployable >= _min:
            summary["mmf_sweep"] = {"status": "SWEEP",
                                    "amount_gbp": round(_deployable - _min / 3, 2),
                                    "instrument": _ceq[0],
                                    "note": (f"idle cash £{_deployable:,.0f} >= £{_min:,.0f} and no "
                                             f"committed action within {getattr(_sc4, 'MMF_SWEEP_IDLE_DAYS', 10)} "
                                             f"trading days -> mechanical sweep (MMF counts as cash "
                                             f"everywhere; sells settle T+2)")}
            print(f"  B2: SWEEP line emitted ({summary['mmf_sweep']['amount_gbp']})")
        else:
            summary["mmf_sweep"] = {"status": "NO_ACTION", "deployable_gbp": _deployable}
    except Exception as _e:
        warnings.append(f"B2 sweep rule skipped: {_e}")

    # — B3: factor look-through concentration (writes into analytics like A11) —
    try:
        import factor_lookthrough as _flt
        with open(portfolio_path, encoding="utf-8") as f:
            _pd5 = json.load(f)
        _fres = _flt.compute(_pd5, _flt.load_map())
        summary["factor_lookthrough"] = {k: _fres[k] for k in
                                         ("ai_complex_effective_weight_pct", "cap_pct", "breach",
                                          "unclassified", "email_line")}
        if _fres.get("semis"):
            summary["factor_lookthrough"]["semis"] = _fres["semis"]
        if os.path.exists(analytics_path):
            with open(analytics_path, encoding="utf-8") as f:
                _ana2 = json.load(f)
            _ana2["factor_lookthrough"] = _fres
            with open(analytics_path, "w", encoding="utf-8") as f:
                json.dump(_ana2, f, indent=2, ensure_ascii=False)
        if _fres.get("breach"):
            flags.append({"type": "FACTOR_CAP_BREACH",
                          "message": (_fres["email_line"] + " — Step 8 MUST include a Category-6 "
                                      "de-concentration option; factor-raising BUYs BLOCKED at "
                                      "Checkpoint-D while in breach (B3)")})
        if _fres.get("unclassified"):
            warnings.append(f"B3: unclassified names need a one-line factor_map entry: "
                            f"{_fres['unclassified']}")
        print(f"  B3: {_fres['email_line']}")
    except Exception as _e:
        warnings.append(f"B3 factor look-through skipped: {_e}")

    # Write run_context
    if errors:
        status = "ERROR" if len(errors) >= 2 else "PARTIAL"
    elif degraded:
        status = "PARTIAL"
    else:
        status = "OK"
    # Step 9d declares its own result: how many of the mechanical asserts raised. Zero raised
    # is a real, positive outcome and must be distinguishable from "9d never ran".
    try:
        _n_raised = (len(warnings) - _w_before_9d) + (len(errors) - _e_before_9d)
        _mf_measure(rows_out=7, coverage=1.0,
                    note=f"7 assert families evaluated (A10/A11/A18/A19/A20/A22/P7a); "
                         f"{_n_raised} raised a warning or error")
    except Exception:
        pass

    # CAPTURE LAYER ITEM 2 — close the last open step and write the manifest BEFORE
    # run_context, so run_context can carry the manifest's own verdict rather than a
    # separately-derived one.
    _mf_end()
    manifest_dict = {}
    if MANIFEST is not None:
        try:
            manifest_dict = MANIFEST.to_dict()
            MANIFEST.write(manifest_path)
            summary["run_manifest"] = {
                "path": manifest_path,
                "run_status": manifest_dict.get("run_status"),
                "counts": manifest_dict.get("counts"),
                "config_fingerprint": manifest_dict.get("config_fingerprint"),
            }
            print("  Manifest: " + manifest_path + "  status=" + str(manifest_dict.get("run_status")))
            # THE INVERTED DEFAULT: a manifest ERROR is an ERROR. Under the previous policy a
            # step that produced nothing raised a WARN and the run reported success.
            for _me in manifest_dict.get("errors", []):
                if _me not in errors:
                    errors.append("MANIFEST " + _me)
            for _md in manifest_dict.get("degraded", []):
                _w = "MANIFEST DEGRADED " + _md
                if _w not in warnings:
                    warnings.append(_w)
            if manifest_dict.get("run_status") == "ERROR":
                status = "ERROR"
            elif manifest_dict.get("run_status") == "DEGRADED" and status == "OK":
                status = "PARTIAL"
        except Exception as _mex:
            warnings.append("Run manifest write failed: " + str(_mex))

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
