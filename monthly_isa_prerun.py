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

# ISA-0594 — ONE HOME for the wall-clock ceiling this pipeline must live inside. Measured, not
# assumed: the host kills a tool call at ~178s (observed 173954ms and 177988ms on 05-Sep-2026),
# and that applies to the SCHEDULED TASK too, which had been believed exempt.
HOST_SHELL_CEILING_S = 175.0

# ISA-0594 — plans deferred out of the pre-write path (see _run_plan_stability). A list, not a
# scalar, so "nothing was deferred" and "one plan was deferred" are distinguishable states.
_PLAN_STABILITY_PENDING = []

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




def _retract_assurance_warnings(warnings) -> None:
    """Drop ASSURANCE warnings that a later stage has made untrue (ISA-0594).

    ⚑ A WARNING THAT OUTLIVES ITS CONDITION IS A FALSE ONE. run_context is written up to three
    times in a run and each copy must describe ITS OWN completeness, so the escalation is
    retracted the moment the stage it names has actually run. Matching on the "ASSURANCE "
    prefix is the same string the ISA-0447 escalation contract is declared against, so the
    emit and the retract cannot drift apart."""
    warnings[:] = [w for w in warnings if not str(w).startswith("ASSURANCE ")]


def _run_plan_stability(_cd, _cdr, summary, warnings, _s610):
    """Step 6.10d, DEFERRED out of the pre-write path by ISA-0594.

    ⚑ THIS IS ASSURANCE ABOUT THE PLAN, NOT AN INPUT TO IT. `capital_destination.plan_stability`
    re-derives the plan across a perturbation grid to ask whether the lexicographic ranking is
    resolving economics or noise. It costs 37-43s, it changes no destination and no amount, and
    nothing downstream of Step 6.10 reads it. Running it BEFORE run_context was written meant a
    run that overran lost the entire staging file to a check about that file. It now runs after
    the provisional write, with the other assurance stages, and its verdict lands in the final
    rewrite. The body is unchanged from the 6.10d block it was lifted out of."""
    try:
        _ps12 = _cd.plan_stability(base_doc=_cdr)
        summary["plan_stability"] = {
            "state": _ps12.get("state"), "unstable": _ps12.get("unstable"),
            "reading": _ps12.get("reading"),
            "grid": [{k: g[k] for k in ("perturbation", "pounds_churned_gbp",
                                        "churn_share_of_plan", "receiver_set_changed",
                                        "order_changed")} for g in _ps12.get("grid", [])],
            "not_an_input": {k: v["read_by_code"] for k, v in
                             (_ps12.get("not_an_input") or {})
                             .get("quantities", {}).items()},
            "stock_side": _ps12.get("routed_to_stock_side"),
        }
        _s610.append("plan stability %s" % ("UNSTABLE: " + ", ".join(_ps12["unstable"])
                                            if _ps12.get("unstable") else "stable"))
        if _ps12.get("unstable"):
            warnings.append(
                "Step 6.10d PLAN UNSTABLE under %s: the capital plan changes its "
                "DESTINATIONS or their order under a perturbation this small, which "
                "means the lexicographic ranking is resolving noise rather than "
                "economics (A12). %s" % (", ".join(_ps12["unstable"]),
                                         _ps12.get("reading") or ""))
        _nai = [k for k, v in (_ps12.get("not_an_input") or {})
                .get("quantities", {}).items() if v.get("read_by_code")]
        if _nai:
            warnings.append(
                "Step 6.10d: %s now READ by the capital router. A12 was built on the "
                "measured fact that they were not, and the grid reports them as "
                "NOT_AN_INPUT — that statement is now false and the grid must start "
                "perturbing them for real." % ", ".join(sorted(_nai)))
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.10d (plan_stability): {type(_e).__name__}: {_e}")



def _plan_stability_only(args) -> int:
    """ISA-0594 — complete the one assurance stage that does not fit in the main call.

    Reads the run_context the main pass already wrote, runs the plan-stability grid against a
    freshly derived capital_destination document, merges the verdict in and rewrites the file.
    It REFUSES rather than inventing a context: no run_context means the main pass has not run,
    and a stage that reports on a run that did not happen is worse than one that did not run
    (R2.10, R4.9)."""
    # same derivation as main() — one shape for the label, not a second one (R4.4)
    month_label = date.today().strftime("%b_%Y").lower()
    path = os.path.join(SCRIPT_DIR, "run_context_%s.json" % month_label)
    if not os.path.exists(path):
        print("REFUSED: %s does not exist — run the main pre-run first." % os.path.basename(path))
        return 2
    with open(path, encoding="utf-8") as fh:
        ctx = json.load(fh)
    summary = ctx.get("summary") or {}
    warnings = list(ctx.get("warnings") or [])
    import capital_destination as _cd_ps
    t0 = time.time()
    print("[6.10d] Plan stability under perturbation (standalone completion, ISA-0594)...")
    _cdr = _cd_ps.build(out_path=os.path.join(SCRIPT_DIR,
                                              "capital_destination_%s.json" % month_label))
    lines = []
    _run_plan_stability(_cd_ps, _cdr, summary, warnings, lines)
    for line in lines:
        print("  " + line)
    # ⚑ WHAT THIS STAGE KNOWS IS WHAT THIS STAGE DID. The prior assurance block is the only
    # record of whether 9d and 6.99 ran, so it is READ, never assumed: hardcoding them as run
    # would let a main pass that died before 9d be reported COMPLETE by the one command that
    # cannot have observed them. It can only ever ADD 6.10d to what was already true.
    _retract_assurance_warnings(warnings)   # this command changes the assurance state
    ok = "plan_stability" in summary
    _prior = (summary.get("assurance") or {})
    _prior_run = list(_prior.get("stages_run") or [])
    _prior_pending = [x for x in (_prior.get("stages_not_yet_run") or [])
                      if not str(x).startswith("6.10d")]
    _this = "6.10d (plan stability under perturbation)"
    summary["assurance"] = {
        "state": ("COMPLETE" if (ok and not _prior_pending)
                  else "PARTIAL" if ok else _prior.get("state", "PENDING")),
        "stages_run": _prior_run + ([_this] if ok and _this not in _prior_run else []),
        "stages_not_yet_run": _prior_pending + ([] if ok else [_this]),
        "meaning": ("6.10d completed out of band by --plan-stability-only at %s; the grid reads "
                    "capital_destination from disk, so it measures the same plan the main pass "
                    "produced. Any stage still listed in stages_not_yet_run was NOT observed by "
                    "this command and remains absent, not clean (R2.10)."
                    % datetime.now().strftime("%Y-%m-%d %H:%M")),
    }
    if _prior_pending:
        warnings.append(
            "ASSURANCE PARTIAL — plan stability completed, but %s did not run in the main pass "
            "and remain ABSENT. Re-run the pre-run to obtain them." % ", ".join(_prior_pending))
    summary.setdefault("runtime", {})["plan_stability_completed_s"] = round(time.time() - t0, 1)
    # a staging note from an earlier write describes a state this command has just ended
    if str(ctx.get("error") or "").startswith(("provisional:", "interim:")):
        ctx["error"] = ""
    ctx["summary"] = summary
    # the ASSURANCE warnings written by the provisional/interim passes describe a state this run
    # has just changed. Drop them rather than leaving the file arguing with itself.
    if ok and not _prior_pending:
        _retract_assurance_warnings(warnings)
    ctx["warnings"] = warnings
    ctx.setdefault("_meta", {})["assurance_completed_at"] = \
        datetime.now().strftime("%Y-%m-%d %H:%M")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ctx, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    print("  run_context updated: assurance=%s  (%.0fs)"
          % (summary["assurance"]["state"], time.time() - t0))
    return 0 if ok else 1


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
# ISA-0429 (CRITICAL, 22-Aug-2026). A refreshed price was written into a store beside
# prices it must be COMMENSURABLE with, and nothing asserted commensurability. The
# default fetch divided by 100.0 on the belief that yfinance quotes LSE tickers in
# pence; for VUAG.L / IWMO.L it does not, so `vuag_price_now` read 1.0666 against
# trade-date prices of 95.36-108.82 and the counterfactual returned -98.9%. That fed
# the scaling-freeze unfreeze condition and DISABLED the D6 probation rule.
#
# THE CONTRACT: never assume the provider's unit. Choose the scale that reconciles
# with the reference already in the store, and REFUSE when none does. This is correct
# whether the provider quotes pence or pounds, and it cannot fail silently.
PRICE_UNIT_RECONCILE_BAND = (0.5, 2.0)   # fetched/reference must land inside this
# ⚑ ONLY scales that correspond to a REAL provider convention belong here. The first
# draft of this contract also carried (100.0, "major_to_pence") on a "try both
# directions" instinct - and its own regression test caught that this REPAIRED the
# live corrupt value (1.0666 x 100 = 106.66) instead of refusing it. A scale that
# rescues a wrong number is indistinguishable from one that fabricates a plausible one,
# and applying a conversion on an assumption is the very defect this contract exists to
# prevent. LSE quoting in pence is a real convention; the inverse is not.
PRICE_UNIT_CANDIDATE_SCALES = ((1.0, "as_fetched"), (0.01, "pence_to_major"))


class PriceUnitError(ValueError):
    """Raised when no candidate scale reconciles a fetched price with its reference."""


def _latest_trade_price(store, price_key):
    """The most recent trade-date price for `price_key`, or None. R5.2: the reference
    must come from the SAME artefact, independently sourced from the fetch."""
    best_date, best_px = None, None
    for t in (store or {}).get("trades") or []:
        px = t.get(price_key)
        if not px:
            continue
        d = t.get("price_date") or t.get("date") or ""
        if best_date is None or d >= best_date:
            best_date, best_px = d, float(px)
    return best_px


def _reconcile_price_unit(fetched, reference, ticker):
    """Return (price_in_reference_units, basis). Raises PriceUnitError if no scale fits.

    A missing reference is NOT an error - a fresh store has no trades - but it is
    recorded as UNVERIFIED rather than silently treated as verified (R2: 'missing'
    cannot masquerade as measured)."""
    fetched = float(fetched)
    if not reference or reference <= 0:
        return fetched, "UNVERIFIED_no_reference"
    lo, hi = PRICE_UNIT_RECONCILE_BAND
    for scale, name in PRICE_UNIT_CANDIDATE_SCALES:
        cand = fetched * scale
        if lo * reference <= cand <= hi * reference:
            return cand, name
    raise PriceUnitError(
        "%s fetched %.6g cannot be reconciled with the store's latest trade-date price "
        "%.6g under any of %s (band %.2fx-%.2fx). Refusing rather than writing an "
        "incommensurable price (ISA-0429)."
        % (ticker, fetched, reference,
           ", ".join(n for _, n in PRICE_UNIT_CANDIDATE_SCALES), lo, hi))


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
                # ISA-0429: NO unit conversion here. The provider's convention is not
                # knowable at this point and a hard-coded /100.0 silently produced a
                # 100x error for 5+ months. The unit is reconciled below against the
                # trade-date prices already in the store, which are the only reference
                # the framework has that is known to be commensurable.
                out[t] = (float(h.iloc[-1]), h.index[-1].date().isoformat())
            return out
    try:
        px = fetch_fn(("VUAG.L", "IWMO.L"))
        v_raw, v_date = px["VUAG.L"]
        i_raw, i_date = px["IWMO.L"]
        v_val, v_basis = _reconcile_price_unit(v_raw, _latest_trade_price(store, "vuag_price"), "VUAG.L")
        i_val, i_basis = _reconcile_price_unit(i_raw, _latest_trade_price(store, "iwmo_price"), "IWMO.L")
        store["vuag_price_now"], store["vuag_price_now_date"] = v_val, v_date
        store["iwmo_price_now"], store["iwmo_price_now_date"] = i_val, i_date
        store["vuag_price_now_unit_basis"] = v_basis
        store["iwmo_price_now_unit_basis"] = i_basis
    except PriceUnitError as e:
        _print("ERROR A14 refresh: %s - store untouched (ISA-0429)" % e)
        return None
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
    # ⚑⚑ ISA-0589, 03-Sep-2026. THESE SIX ARE BOUND FIRST, BEFORE ANY GUARD CAN APPEND TO THEM.
    # They used to be initialised 111 lines below, AFTER the ISA-0572 memory-base guard and the
    # trades-log guard already called `warnings.append(...)`. Python binds `warnings` as a local
    # for the whole of main(), so those calls raised UnboundLocalError and the pre-run died
    # before Step 0 with a traceback — on exactly the condition ISA-0572 was built to report
    # gracefully. R4.12: `log[...] = x` written before `log` exists. The guard was never
    # exercised because the case it guards was false in the session that wrote it (FC-K).
    _RUN_STARTED_AT = time.time()   # ISA-0590: the register gate's 'this run wrote it' test
    errors: list = []
    warnings: list = []
    summary: dict = {}
    flags: list = []
    degraded = False   # True -> status downgraded to PARTIAL (step ran but data incomplete)
    watchlist_promotion_log: dict = {}

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
    parser.add_argument("--plan-stability-only", action="store_true",
                        help="ISA-0594: run ONLY the deferred Step 6.10d plan-stability grid "
                             "and merge its verdict into the existing run_context. For use "
                             "after a run whose assurance state is PARTIAL; needs no re-run of "
                             "Steps 1-9, because the grid reads capital_destination from disk.")
    args = parser.parse_args()

    if args.plan_stability_only:
        sys.exit(_plan_stability_only(args))

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
    # ⚑ AN ABSENT MEMORY DIR IS NAMED, NOT DISCOVERED LATER (ISA-0572, 02-Sep-2026).
    # `_resolve_memory_base()` returns candidates[0] when NOTHING on its list exists, so
    # MEMORY_BASE can point at a directory that is not there and every `find_memory_file` then
    # returns None — silently. That is the exact shape of the defect ISA already fixed once
    # (project_isa_memory_base_fix: "a Windows path in a Linux sandbox read as 'no memory',
    # silently"), and it is environment-dependent rather than code-dependent: `.auto-memory`
    # was NOT mounted in the 02-Sep Cowork session, so the same tree degrades or does not
    # depending on which session runs it. Step 5 loses the VCI watchlist and analytics loses
    # every purchase date, and both look like ordinary empty results.
    if not os.path.isdir(MEMORY_BASE):
        warnings.append(
            "MEMORY BASE ABSENT: %r does not exist, so every memory read returns None — the "
            "VCI watchlist (Step 5) and the trades log (purchase dates for analytics) are "
            "UNAVAILABLE, not empty. Set ISA_MEMORY_DIR to the memory directory and re-run, or "
            "treat this run's memory-derived fields as UNMEASURED." % MEMORY_BASE)
        print("  ⚑ MEMORY BASE ABSENT: %s — memory-derived inputs are UNAVAILABLE, not empty"
              % MEMORY_BASE)
    trades_log_path  = find_memory_file("project_isa_trades_log.md")
    if not trades_log_path:
        warnings.append(
            "project_isa_trades_log.md NOT FOUND under MEMORY_BASE (%s). analytics runs with no "
            "purchase dates: holding periods, the 182-day min-hold and entry-basis figures "
            "degrade to UNMEASURED. Absent is not empty (R2.10)." % MEMORY_BASE)
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

    # ⚑ ISA-0589 — the accumulators are bound at the TOP of main(), not here. A second
    # initialisation at this point would silently DISCARD everything the guards above appended
    # (the absent memory base, the missing trades log) — one home per rule (R4.4).

    # CAPTURE LAYER ITEM 2 — open the run manifest before any step runs.
    global MANIFEST
    if _RM is not None:
        MANIFEST = _RM.Manifest(month_label, script_dir=SCRIPT_DIR)
    manifest_path = os.path.join(SCRIPT_DIR, f"run_manifest_{month_label}.json")

    # ---------------------------------------------------------------------------
    # Step 1: Extract portfolio
    # ---------------------------------------------------------------------------
    # ══════════════════════════════════════════════════════════════════════════════════
    # Step 0 — FRAMEWORK-INTEGRITY PREFLIGHT (P0.1 / P0.2 / P0.4)
    # ══════════════════════════════════════════════════════════════════════════════════
    # ⚑ IT MUST PRECEDE EVERYTHING, and the reason is not tidiness. A declaration failure
    # should stop a run BEFORE it computes anything on a broken contract. Computing first and
    # checking afterwards produces a plausible artefact with a red line underneath it — and
    # the artefact is what gets read.
    # ⚑ IT IS REPORT-ONLY ON PURPOSE. Phase 0 is new and it fails the build for real reasons
    # today (two FALSIFIED network claims, one dead vocabulary, one quantity with two
    # computers). Blocking Raj's pre-run on its first sight of a problem is how a control gets
    # switched off — the R5.7 lesson the KR5 block in run_tests.py already records. It NAMES
    # them, every run, so the number is visible and shrinking rather than assumed.
    print("\n[0] Framework-integrity preflight (declaration checks BEFORE any work)...")
    _mf_begin("0", "framework_integrity.preflight")
    try:
        import framework_integrity as _fi
        _pf = _fi.preflight(SCRIPT_DIR)
        summary["framework_integrity_preflight"] = _pf
        _mf_measure(status=("OK" if _pf["state"] == "OK" else "DEGRADED"),
                    note="; ".join(_pf["errors"]) or "all declaration checks clean")
        print("  preflight %s — %s" % (_pf["state"],
              "; ".join(_pf["errors"])[:300] or "quantity/threshold/negative-claim registers clean"))
        for _e in _pf["errors"]:
            warnings.append("PREFLIGHT " + _e)
    except Exception as _e:                                    # noqa: BLE001
        # ⚑ AN UNAVAILABLE MONITOR IS REPORTED, NEVER SILENTLY SKIPPED (R4.9). "The check did
        # not run" and "the check passed" are different facts (R2.10).
        summary["framework_integrity_preflight"] = {
            "state": "UNAVAILABLE", "reason": "%s: %s" % (type(_e).__name__, _e)}
        _mf_measure(status="DEGRADED", note="preflight unavailable: %s" % _e)
        warnings.append("PREFLIGHT UNAVAILABLE — %s: %s" % (type(_e).__name__, _e))
        print("  preflight UNAVAILABLE — %s" % _e)

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
    # ══════════════════════════════════════════════════════════════════════════════════
    # Step 5y — POPULATE THE WEEKLY GBP TOTAL-RETURN STORE (P1 / ISA-0455)
    # ══════════════════════════════════════════════════════════════════════════════════
    # ⚑ PLACED AFTER STEP 5 AND BEFORE STEP 6.10, and both halves are load-bearing.
    # AFTER Step 5, because the fetch universe is the union of the store, the broker sleeve,
    # the watchlist, the vci_watchlist and the candidate_pool — and Steps 4 and 5 are what
    # write the last three. BEFORE Step 6.10, because the capital router consumes correlation
    # through the candidate pipeline: a fetch AFTERWARDS would size September's capital on
    # August's matrix.
    #
    # ⚑ UNTIL 28-Aug-2026 THIS STEP DID NOT EXIST, because a live docstring said Yahoo was
    # network-blocked from both the container and the device shell. It was FALSE, and the
    # framework's own ISA-0411 had recorded a 400-ticker Yahoo screen five days before that
    # sentence was written. The store had never been written to; every name read UNMEASURED;
    # A2.3's adverse default of 0.70 capped every position at STARTER against a measured mean
    # pairwise correlation of 0.19. Nothing was broken. Nobody fetched.
    #
    # ⚑ stdlib `urllib` only — no yfinance, no pip, no tmpfs, no stub on PYTHONPATH. Step 6
    # below still needs yfinance for its own metrics; this step deliberately does not.
    # ══════════════════════════════════════════════════════════════════════════════════
    # Step 5x — REFRESH THE TICKER -> YAHOO SYMBOL MAP (P1.2c / ISA-0577)
    # ══════════════════════════════════════════════════════════════════════════════════
    # ⚑ IMMEDIATELY BEFORE 5y, AND THAT POSITION IS THE WHOLE FIX. 5y fetches only names the
    # map resolves; the map is what 5y's universe is intersected with. A refresh AFTER the
    # fetch would admit September's names in time for October.
    #
    # ⚑ UNTIL 03-Sep-2026 THIS STEP DID NOT EXIST AND `build_symbol_map()` HAD ZERO CALLERS ON
    # DISK. `stock_symbol_map.json` was a hand-run artefact stamped 2026-08-28 while
    # `build_universe` grew with every screener and VCI promotion. Measured on 03-Sep: 119 of
    # 178 universe names (66.9%) unmapped -> dropped by build_universe(strict=False) -> never
    # fetched -> UNMEASURED -> A2.3's adverse rho of 0.70 -> capped at STARTER. HRMY and NVDA,
    # the two names September's ranker put at the top, were both in the 119, and 116 of the 119
    # already carried index-screen provenance that would admit them. Nothing was broken.
    # NOBODY BUILT THE MAP. That is FC-E, this project's second failure class.
    #
    # ⚑ IT ADMITS, IT NEVER GUESSES. An entry requires provenance AND a live exchange that the
    # declared venue permits — a bare `ONT` answers from NYQ for "Onterris, Inc." and published
    # GBP 18,471.20 against a broker truth of GBP 997.92. Names it cannot verify are REFUSED,
    # NAMED in the warning list, and written to `symbol_map_refusals.json`, which
    # consistency_check reads: a name may be refused, it may never be refused SILENTLY.
    print("\n[5x] Symbol map refresh (stock_price_fetch.refresh_symbol_map)...")
    _mf_begin("5x", "symbol_map_refresh")
    try:
        import stock_price_fetch as _spf5x
        _mr = _spf5x.refresh_symbol_map()
        summary["symbol_map_refresh"] = _mr
        if _mr.get("state") == "DISABLED":
            _mf_measure(status="OK",
                        note="rollback: V2_FLAGS['symbol_map_refresh'] is False")
            print("  DISABLED — the map is whatever is on disk (rollback flag)")
        else:
            for _r in (_mr.get("refused") or []):
                warnings.append(
                    "Step 5x SYMBOL REFUSED %s (%s) — %s FIX: %s"
                    % (_r.get("ticker"), _r.get("source") or "no source",
                       (_r.get("reason") or "")[:220], _r.get("fix") or
                       "declare the intended listing in stock_price_fetch.SYMBOL_MAP"))
            _n_after = _mr.get("n_unmapped_after")
            _mf_measure(rows_in=_mr.get("n_unmapped_before"),
                        rows_out=_mr.get("n_admitted"),
                        status=("OK" if not _n_after else "DEGRADED"),
                        note=("%s: %s admitted, %s kept unchanged (append-only, R4.8), %s "
                              "REFUSED and NAMED; %s of %s universe names now mapped"
                              % (_mr.get("state"), _mr.get("n_admitted"), _mr.get("n_kept"),
                                 _mr.get("n_refused"), _mr.get("n_mapped_after"),
                                 _mr.get("n_universe"))))
            print("  %s — %s admitted, %s refused, %s/%s universe names mapped"
                  % (_mr.get("state"), _mr.get("n_admitted"), _mr.get("n_refused"),
                     _mr.get("n_mapped_after"), _mr.get("n_universe")))
    except Exception as _e:                                    # noqa: BLE001
        # ⚑ NAMED, NEVER SWALLOWED. A failed refresh leaves the map exactly as it was and 5y
        # behaves as it did before this stage existed — but the run must say so, because
        # "the map did not grow" and "there was nothing to add" are different facts (R2.10).
        summary["symbol_map_refresh"] = {"state": "UNAVAILABLE",
                                         "reason": "%s: %s" % (type(_e).__name__, _e)}
        _mf_measure(status="DEGRADED", note="symbol map refresh unavailable: %s" % _e)
        warnings.append("STEP 5x UNAVAILABLE — %s: %s. The symbol map was NOT refreshed; any "
                        "name promoted since its last refresh will read UNMEASURED because it "
                        "was never fetched, not because no data exists."
                        % (type(_e).__name__, _e))
        print("  UNAVAILABLE — %s" % _e)

    print("\n[5y] Weekly GBP total-return store (stock_price_fetch, batched, stdlib only)...")
    _mf_begin("5y", "stock_price_fetch")
    try:
        import stock_price_fetch as _spf
        import stock_return_store as _srs
        # ⚑ ISA-0580. THE COVERAGE DENOMINATOR IS THE UNIVERSE, NEVER THE STORE. `coverage()`
        # defaults its ticker list to the names already IN the store, so a name that was never
        # fetched is INVISIBLE: on 02-Sep-2026 this reported "59 names, 59 measured, 0
        # unmeasured" while 119 universe names had no series at all, and that report is what
        # made the absence of HRMY and NVDA look like a working mechanism. A metric whose
        # denominator is its own numerator's source can only ever report 100%.
        _uni5y = _spf.build_universe(strict=False)
        _uni_tickers = list(_uni5y["tickers"]) + list(_uni5y.get("unmapped") or [])
        _fr, _guard = None, 0
        # ⚑ ISA-0578. THE GUARD IS DERIVED FROM THE UNIVERSE, never a magic 12. At BATCH_SIZE 20
        # a fixed 12 capped the fetch at 240 names and the loop exited with `remaining` > 0 and
        # NO warning — a silent partial waiting for the universe to grow into it (FC-I).
        _max_batches = max(4, -(-len(_uni_tickers) // max(1, _spf.BATCH_SIZE)) + 3)
        while _guard < _max_batches:             # the caller loops to ALL_DONE (P1.7)
            _guard += 1
            _fr = _spf.run()
            if _fr.get("state") in ("ALL_DONE", "DISABLED", "FETCH_UNAVAILABLE"):
                break
        # ⚑ ISA-0578 — the declared refusals travel WITH the coverage report, so a name that
        # was NEVER FETCHED and a name that is simply younger than 52 weeks stop rendering
        # identically. Both read UNMEASURED and both take A2.3's adverse 0.70; they are
        # different facts with different fixes (R2.10).
        _cov = _srs.coverage(_srs.load(), _uni_tickers, _spf.load_declared_refusals())
        summary["fetch_report"] = _fr
        summary["correlation_coverage"] = _cov
        summary["fetch_universe"] = {"n": len(_uni_tickers), "basis": _uni5y.get("basis"),
                                     "origin_counts": {}}
        for _t, _o in (_uni5y.get("origin") or {}).items():
            summary["fetch_universe"]["origin_counts"][_o] = \
                summary["fetch_universe"]["origin_counts"].get(_o, 0) + 1
        if _fr.get("state") == "FETCH_UNAVAILABLE":
            # ⚑ STEP 6.12b MUST NAME THE FETCH FAILURE AS THE CAUSE, never the absence of
            # data. "We could not fetch" and "there is nothing to fetch" are different facts,
            # and the second is what the framework believed for months (R2.10).
            warnings.append("FETCH_UNAVAILABLE — %s. The store is UNCHANGED; correlation reads "
                            "UNMEASURED because the FETCH FAILED, not because no data exists."
                            % _fr.get("reason"))
            _mf_measure(status="DEGRADED", note="fetch unavailable: %s" % _fr.get("reason"))
        elif _fr.get("state") == "DISABLED":
            _mf_measure(status="OK", note="rollback: V2_FLAGS['stock_return_fetch'] is False")
        else:
            for _f in _fr.get("failed", []):
                warnings.append("FETCH FAILED %s — %s (history untouched; a fetch failure must "
                                "never look like a delisting)"
                                % (_f.get("ticker"), _f.get("reason")))
            # ⚑ ISA-0578. THE REFUSALS REACH THE WARNING LIST. `run()` returns
            # `unmapped_refused` precisely so the caller can name them, and this loop ignored
            # it: 119 names were dropped on 02-Sep-2026 with zero warnings emitted. A producer
            # that names its refusals is only half the contract — R4.9 requires the READER to
            # count what it cannot match.
            for _u in (_fr.get("unmapped_refused") or []):
                warnings.append(
                    "FETCH SKIPPED %s — no verified Yahoo symbol, so it is NOT FETCHED and its "
                    "correlation reads UNMEASURED because nothing was tried, not because no "
                    "data exists (R2.10). Step 5x names the reason and the one-line fix." % _u)
            # ⚑ AND THE LOOP MUST SAY IT DID NOT FINISH. Exiting on the batch guard with names
            # still queued is a silent partial (FC-I).
            if _fr.get("state") != "ALL_DONE":
                warnings.append(
                    "STEP 5y DID NOT COMPLETE — state %s after %d batch(es) with %s name(s) "
                    "still queued of %s. The store holds a PARTIAL refresh this run; the "
                    "resume file carries the remainder."
                    % (_fr.get("state"), _guard, _fr.get("remaining"), _fr.get("n_universe")))
            _mf_measure(rows_out=_cov["n_names"],
                        status=("OK" if _cov["n_measured"] else "DEGRADED"),
                        note=("%d/%d names measured; pit_share %s; %d stale excluded"
                              % (_cov["n_measured"], _cov["n_names"], _cov["pit_share"],
                                 _cov["n_stale"])))
        print("  %s — %s/%s names MEASURED, %s stale, pit_share %s"
              % (_fr.get("state"), _cov.get("n_measured"), _cov.get("n_names"),
                 _cov.get("n_stale"), _cov.get("pit_share")))
    except Exception as _e:                                    # noqa: BLE001
        summary["fetch_report"] = {"state": "UNAVAILABLE",
                                   "reason": "%s: %s" % (type(_e).__name__, _e)}
        _mf_measure(status="DEGRADED", note="stock_price_fetch unavailable: %s" % _e)
        warnings.append("STEP 5y UNAVAILABLE — %s: %s" % (type(_e).__name__, _e))
        print("  UNAVAILABLE — %s" % _e)

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
        # ── A7 / ISA-0440 — a DEGRADED sell order is a WARNING, never a silent revert ────────
        _do = _fa.get("donor_ordering") or {}
        summary["donor_ordering"] = {"state": _do.get("state"), "enabled": _do.get("enabled")}
        if _do.get("state") != "MEASURED":
            warnings.append(
                "Step 6.05 SELL ORDER NOT REORDERED (%s): the fund agenda is in the PRE-A7 "
                "FRS-led order, whose every term is FRS — and L-1/ISA-0351 measured alpha rank "
                "persistence in this sleeve at -0.482, so that order sells low in expectation. "
                "%s" % (_do.get("state"), _do.get("basis") or ""))
        elif _fa.get("fund_action_stack"):
            _h = _fa["fund_action_stack"][0]
            _s610_note = ("Step 6.05 SELL ORDER (A7): donor #1 is %s — %s"
                          % (_h.get("name"), _h.get("donor_why")))
            print("  " + _s610_note)
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

    # ── Step 6.09 — SOURCE-SIDE concentration, strategic allocation and T4 (19-Aug-2026) ──
    # ⚑ WHY THIS STEP EXISTS AT ALL. `process_concentration` (ISA-0329), `strategic_allocation`
    # (ISA-0333) and the T4 mandate-drift trigger (ISA-0165) were all built, all green in the
    # battery, and NONE of them was called by this orchestrator. That is the second failure class
    # exactly: an absent execution that reported success. A module the monthly run never invokes
    # is not part of the framework, however many assertions it passes.
    #
    # ⚑ WHAT EACH ONE ANSWERS, and why they are grouped: they are the three questions about the
    # fund sleeve that the output-side measures cannot answer.
    #   6.07 (above)  "how much of my RISK rides on one factor?"      — output side, correlation
    #   6.09a         "how much of my MONEY rides on one PROCESS?"    — source side
    #   6.09b         "which bets am I running against a reference?"  — exposure side
    #   6.09c         "has a fund stopped doing what it was bought for?"
    # R6.2: 6.07 and 6.09a are PUBLISHED SIDE BY SIDE and never blended — they disagree about
    # which exposure is largest, and that disagreement is the information.
    print("\n[6.09] Source-side concentration, strategic allocation and T4 mandate drift...")
    _mf_begin("6.09", "process_concentration + strategic_allocation + t4")
    _s609 = []
    try:
        import process_concentration as _pc
        _pcr = _pc.build(out_path=os.path.join(SCRIPT_DIR,
                                               f"process_concentration_{month_label}.json"))
        if _pcr.get("state") == "OK":
            _sp = _pcr["source_side"]["process"]
            summary["process_concentration"] = {
                "largest_process": _sp["largest"],
                "largest_pct_of_fund_sleeve": _sp["largest_pct_of_fund_sleeve"],
                "declared_coverage_pct": _sp["declared_coverage_pct_of_sleeve"],
                "n_eff_declared": _sp["n_eff_declared"]}
            _s609.append("process %s %.1f%% of sleeve (coverage %.0f%%)"
                         % (_sp["largest"], _sp["largest_pct_of_fund_sleeve"] or 0,
                            _sp["declared_coverage_pct_of_sleeve"]))
            if _sp["coverage_state"] != "OK":
                warnings.append(
                    "Step 6.09 PROCESS COVERAGE: only %.1f%% of the fund sleeve has a DECLARED "
                    "investment process (%s undeclared). The concentration figures describe the "
                    "declared portion; the rest is UNKNOWN, not diversified (ISA-0329)."
                    % (_sp["declared_coverage_pct_of_sleeve"],
                       ", ".join(u["key"] for u in _sp["undeclared"])))
            if _sp["material_groups"]:
                warnings.append(
                    "Step 6.09 PROCESS CONCENTRATION: %s. %s"
                    % ("; ".join("%s = %.1f%% of the fund sleeve across %d fund(s)"
                                 % (g["key"], g["pct_of_fund_sleeve"], g["n_funds"])
                                 for g in _sp["groups"] if g["material"]),
                       _pcr["readings_disagree"]["verdict"]))
        else:
            warnings.append("Step 6.09a: process_concentration %s" % _pcr.get("state"))
    except Exception as _e:
        warnings.append(f"Step 6.09a (process_concentration): {type(_e).__name__}: {_e}")
        print(f"  process_concentration FAILED (non-fatal): {_e}")

    try:
        import strategic_allocation as _sa
        _sar = _sa.build(xray_path=os.path.join(SCRIPT_DIR, f"xray_data_{month_label}.json"),
                         out_path=os.path.join(SCRIPT_DIR,
                                               f"strategic_allocation_{month_label}.json"))
        if _sar.get("state") == "OK":
            summary["strategic_allocation"] = {
                "reference_authority": _sar["reference"]["authority"],
                "active_share_pct": _sar["active_share_pct"],
                "unauthorised_material_bets": _sar["unauthorised_material_bets"]}
            _s609.append("active share " + ", ".join("%s %.1f%%" % (k, v)
                         for k, v in (_sar["active_share_pct"] or {}).items()))
            for _dim, _bets in (_sar["unauthorised_material_bets"] or {}).items():
                if _bets:
                    _b = {r["key"]: r["active_pp"]
                          for r in _sar["dimensions"][_dim]["bets"] if r["key"] in _bets}
                    warnings.append(
                        "Step 6.09 UNAUTHORISED BETS (%s): %s. These are the emergent residue of "
                        "twelve fund choices measured against the X-Ray's own benchmark column — "
                        "a REFERENCE, not a target. None has a stated rationale or a declared cap "
                        "(ISA-0333/ISA-0200/ISA-0203)."
                        % (_dim, ", ".join("%s %+.2fpp" % (k, v) for k, v in sorted(
                            _b.items(), key=lambda t: -abs(t[1])))))
            for _dim, _why in (_sar.get("blocked_dimensions") or {}).items():
                warnings.append("Step 6.09 SAA %s dimension BLOCKED: %s" % (_dim, _why[:160]))
    except Exception as _e:
        warnings.append(f"Step 6.09b (strategic_allocation): {type(_e).__name__}: {_e}")
        print(f"  strategic_allocation FAILED (non-fatal): {_e}")

    try:
        import fund_rotation_analysis as _fra
        _t4r = _fra.t4_mandate_drift()
        if _t4r.get("state") == "MEASURED":
            summary["t4_mandate_drift"] = {
                "firing": _t4r["firing"], "watch": _t4r["watch"], "refused": _t4r["refused"],
                "fire_rate_band": _t4r["fire_rate_band"]}
            _s609.append("T4 firing on %d, watch %d, refused %d"
                         % (len(_t4r["firing"]), len(_t4r["watch"]), len(_t4r["refused"])))
            for _sd in _t4r["firing"]:
                _row = next(r for r in _t4r["funds"] if r["sedol"] == _sd)
                warnings.append(
                    "Step 6.09 T4 MANDATE DRIFT FIRING — %s: %s. T4 raises a flag; it does not "
                    "sell, trim or replace. The look is Step 6.6 (ISA-0165)."
                    % (_sd, _row["claim"]))
            for _sd in _t4r["refused"]:
                _row = next(r for r in _t4r["funds"] if r["sedol"] == _sd)
                warnings.append("Step 6.09 T4 REFUSED %s — %s. Not tested is not the same as "
                                "tested and fine (R2.10)." % (_sd, _row["reason"][:140]))
            _band = _t4r["fire_rate_band"]
            if _band.get("state") not in ("OK", "REFUSED"):
                warnings.append("Step 6.09 R15.2 — " + _band.get("raise_item", ""))
        else:
            warnings.append("Step 6.09c: T4 %s" % _t4r.get("state"))
    except Exception as _e:
        warnings.append(f"Step 6.09c (t4_mandate_drift): {type(_e).__name__}: {_e}")
        print(f"  t4_mandate_drift FAILED (non-fatal): {_e}")

    _mf_measure(status=("OK" if _s609 else "DEGRADED"),
                note=("; ".join(_s609) if _s609 else "no source-side measure produced a result"))
    print("  " + ("; ".join(_s609) if _s609 else "no result"))

    # ── Step 6.12 — THE V2.1 STACK (26-Aug-2026, ISA-0354/0355/0356/0357) ─────────────────────
    # ⚑ WHY THIS STEP EXISTS AT ALL. This project's dominant failure mode is an absent execution
    # that reports success — six recorded occurrences, the last at FUNCTION granularity
    # (ISA-0404: `strategic_allocation.attribution()` was built, green, and called by nothing).
    # `capital_destination` carried 22 green assertions and was called by NOTHING for four days.
    # So amendment A11 is binding on every V2.1 module: each carries a battery assertion that it
    # EXECUTED, not merely that it imports. This step is where that execution happens.
    #
    # ⚑ WHAT IS SHADOW AND WHAT IS LIVE. 6.12a and 6.12b are SHADOW ONLY and move no capital —
    # they publish measurement so that the September run has a baseline. 6.12c and 6.12d compute
    # sizes and lifecycle states but do NOT execute: the monthly email presents them for Raj's
    # decision, exactly as every other action does.
    print("\n[6.12] V2.1 stack: policy stamp, correlation, evidence, sizing, lifecycle...")
    _mf_begin("6.12", "isa_policy + correlation_engine + evidence_state + position_sizing "
                      "+ asset_drawdown + risk_contribution + retention")
    _s612 = []
    _v21 = {}
    try:
        import isa_policy as _pol
        _v21["policy"] = _pol.policy_stamp()
        _s612.append("policy %s, anchor %.2f%% (derived %s, next %s)" % (
            _pol.POLICY_VERSION, _v21["policy"]["anchor_operative_pct"] or float("nan"),
            _v21["policy"]["anchor_derived_at"], _v21["policy"]["anchor_next_due"]))
        # ⚑ The anchor is DERIVED from the portfolio value (ISA-0435). If the next window has
        # passed, say so — a stale anchor prices every gate in the framework.
        try:
            _nd = _v21["policy"].get("anchor_next_due")
            if _nd and _nd < dt_date.today().isoformat():
                warnings.append(
                    "Step 6.12 ANCHOR OVERDUE: the required-return anchor was due to re-derive "
                    "on %s and has not. It is a function of the portfolio value and the "
                    "contribution schedule, so every anchor-derived gate is now priced off a "
                    "stale portfolio (ISA-0435). Run derive_required_return.py." % _nd)
        except Exception:
            pass
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12 (isa_policy): {type(_e).__name__}: {_e}")

    # 6.12a — the golden fixture. A rollback that is not deterministic is not a rollback.
    try:
        import v21_golden_fixture as _gf
        _fx = _gf.verify()
        _v21["golden_fixture"] = _fx
        if _fx["status"] == "ABSENT":
            warnings.append("Step 6.12a: no V2.1 golden fixture is frozen, so this run cannot "
                            "prove the V2 flags are behaviour-neutral. Run "
                            "v21_golden_fixture.py --freeze.")
        elif not _fx["holds"]:
            warnings.append("Step 6.12a GOLDEN FIXTURE BROKEN: " + "; ".join(_fx["diffs"][:3])
                            + ". A declared policy constant or a DERIVATION moved. This is a "
                              "decision, not a build — do NOT re-freeze to whatever the code "
                              "now prints (ISA-0383).")
        else:
            _s612.append("golden fixture holds")
        for _n in _fx.get("notes", []):
            warnings.append("Step 6.12a: " + _n)
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12a (v21_golden_fixture): {type(_e).__name__}: {_e}")

    # 6.12b — correlation coverage. SHADOW. Today this reports a REFUSAL, and that is the point.
    try:
        import correlation_engine as _ce
        import stock_return_store as _srs
        # ⚑ ISA-0580 — the UNIVERSE is the denominator here too, and it is read from what Step
        # 5y actually fetched against rather than recomputed, so the two surfaces cannot drift.
        _cov_tickers = None
        try:
            import stock_price_fetch as _spf612
            _u612 = _spf612.build_universe(strict=False)
            _cov_tickers = list(_u612["tickers"]) + list(_u612.get("unmapped") or [])
        except Exception as _e612:                                 # noqa: BLE001
            warnings.append("Step 6.12b: could not rebuild the fetch universe (%s: %s) — "
                            "coverage falls back to the STORE as its denominator, which can "
                            "only report 100%%. Named, not silent (ISA-0580)."
                            % (type(_e612).__name__, _e612))
        try:
            _ref612 = _spf612.load_declared_refusals()
        except Exception:                                          # noqa: BLE001
            _ref612 = None
        _cov = _srs.coverage(_srs.load(), _cov_tickers, _ref612)
        _v21["correlation_coverage"] = _cov
        if _cov["n_names"] == 0:
            warnings.append(
                "Step 6.12b CORRELATION UNMEASURED: the weekly GBP total-return store is EMPTY, "
                "so no direct-stock correlation can be computed. Under A2.3 an unmeasured "
                "correlation is ADVERSE (rho = max(rho_bar, 0.70)) and EVERY position is capped "
                "at STARTER 3.5%. This is a measured refusal, not an estimate — and it is the "
                "binding constraint on sizing until 52 weeks of Friday-to-Friday closes exist.")
        else:
            _s612.append("correlation coverage %d/%d measured"
                         % (_cov["n_measured"], _cov["n_names"]))
            for _t, _r in sorted(_cov["names"].items()):
                if _r["status"] == "UNMEASURED":
                    warnings.append(
                        "Step 6.12b: %s has %d usable weekly returns, %d short of the %d "
                        "minimum — capped at STARTER until then (A2.3)."
                        % (_t, _r["usable_returns"], _r["weeks_to_minimum"], _cov["min_weeks"]))
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12b (correlation_engine): {type(_e).__name__}: {_e}")

    # 6.12c — the sizing ladder, reconciled against the hard cap it must equal (ISA-0427).
    try:
        import position_sizing as _ps
        _lad = _ps.ladder()
        _caps = _ps.hard_caps()
        _v21["ladder"] = _lad
        _v21["hard_caps"] = _caps
        _s612.append("ladder %s" % "/".join(str(_lad[k]) for k in
                                            ("STARTER", "NORMAL", "HIGH", "EARNED_MAX")))
        if abs(max(_lad.values()) - _caps["max_stock_position_pct"]) > 1e-9:
            warnings.append(
                "Step 6.12c LADDER vs CAP: the ladder's maximum is %.2f%% but "
                "max_stock_position_pct is %.2f%%. portfolio_analytics.py:377 enforces the "
                "latter, so two modules would compute different targets for one position "
                "(ISA-0427)." % (max(_lad.values()), _caps["max_stock_position_pct"]))
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12c (position_sizing): {type(_e).__name__}: {_e}")

    # 6.12d — lifecycle. P5 (ISA-0457 / D18-D19), rebuilt 29-Aug-2026.
    #
    # ⚑⚑ WHAT CHANGED AND WHY IT MATTERS. This block used to read the population from
    # `summary.trades_log.decisions` and filter `route == "forward_led"`. Nothing in the live
    # tree emits that route value, and when the trades log was absent the run printed
    # "ratchet population unavailable" — so on every real run the rule reported an ABSENCE
    # where the honest answer was a MEASUREMENT. Under D18 the population is every open
    # direct-stock position (VCI included), which comes from `portfolio_data` and is ALWAYS
    # available, so the rule is now evaluated every month rather than skipped.
    #
    # ⚑ AND IT PRINTS **WHICH CONDITION BINDS**. "Cannot fire" without "because leg (b) has 1
    # of 12 months" is ISA-0348's pattern in reporting form — a statement that is true every
    # month and informative in none of them.
    try:
        import retention as _ret
        _pos = []
        try:
            _pd_doc = _portfolio_doc if "_portfolio_doc" in dir() else None
        except Exception:                                          # noqa: BLE001
            _pd_doc = None
        if not _pd_doc:
            try:
                import capital_destination as _cd_pop
                _pd_doc = _cd_pop._load_portfolio()
            except Exception:                                      # noqa: BLE001
                _pd_doc = None
        _pos = list((_pd_doc or {}).get("stocks") or [])

        _legs_in = {"months_measured": None}
        try:
            _cf = json.load(open(os.path.join(SCRIPT_DIR, "sleeve_counterfactual.json"),
                                 encoding="utf-8"))
            _legs_in = _ret.ratchet_inputs_from_freeze_history(_cf.get("freeze_history"))
        except Exception as _e2:                                   # noqa: BLE001
            warnings.append("Step 6.12d: `sleeve_counterfactual.json` unreadable (%s) — the "
                            "three ratchet legs are UNMEASURED, which is NOT the same as "
                            "'the sleeve is performing' (ISA-0429)." % type(_e2).__name__)

        if _pos:
            _sw = None
            try:
                _sm = (_pd_doc or {}).get("summary") or {}
                _sw = (float(_sm["stock_sleeve_value_gbp"])
                       / float(_sm["total_value_gbp"]) * 100.0)
            except Exception:                                      # noqa: BLE001
                _sw = None
            _rr = _ret.evaluate_ratchet(
                positions=_pos, current_sleeve_weight_pct=_sw,
                months_measured=_legs_in.get("months_measured"),
                months_trailing=_legs_in.get("months_trailing"),
                sleeve_vs_vuag_pp=_legs_in.get("sleeve_vs_vuag_pp"),
                sleeve_vs_vuag_exlargest_pp=_legs_in.get("sleeve_vs_vuag_exlargest_pp"),
                largest_position_ticker=_legs_in.get("largest_position_ticker"),
                early_warning_pp=_legs_in.get("early_warning_pp"))
            _el = _rr["eligibility"]
            _v21["ratchet_eligibility"] = _el
            _v21["ratchet"] = _rr
            _v21["ratchet_inputs"] = _legs_in
            _v21["route_attribution"] = _rr["route_attribution"]
            # ⚑ ACTIVE_UNMEASURED and DOES_NOT_FIRE get DIFFERENT sentences. One says we
            # looked and the rule did not fire; the other says we could not look.
            if _rr["state"] == "ACTIVE_UNMEASURED":
                warnings.append(
                    "Step 6.12d STEP-DOWN RATCHET IS ACTIVE-UNMEASURED (not 'does not fire'): "
                    "population %s of %s (%s) is satisfied, but %s. An unmeasured leg is a gap "
                    "in what we know, not evidence the sleeve is working (ISA-0429). Earliest "
                    "the 12-month leg can be satisfied is ~Aug-2027."
                    % (_el["n_positions"], _el["min_required"], _el["basis"],
                       "; ".join(_rr["unmeasured"])))
            elif not _rr["fires"]:
                warnings.append(
                    "Step 6.12d STEP-DOWN RATCHET DOES NOT FIRE — the binding condition(s) "
                    "this month: %s." % "; ".join(_rr["binding"]))
            else:
                warnings.append(
                    "Step 6.12d STEP-DOWN RATCHET FIRES: %s" % _rr["detail"])
            _s612.append("ratchet %s (population %s, %s)"
                         % (_rr["state"], _el["n_positions"], _el["basis"]))
        else:
            # ⚑ A REFUSAL, NAMED. Not "population unavailable" as a shrug.
            _v21["ratchet"] = {"state": "REFUSED", "fires": False,
                               "reason": ("no open direct-stock positions could be read from "
                                          "portfolio_data, so the D18 population is UNKNOWN "
                                          "rather than zero")}
            warnings.append("Step 6.12d: the ratchet population could NOT be read from "
                            "portfolio_data. That is UNKNOWN, not zero — the rule is neither "
                            "eligible nor ineligible this run.")
            _s612.append("ratchet population REFUSED (portfolio_data unreadable)")
        _v21["min_hold_exempt"] = list(__import__("position_sizing").MIN_HOLD_EXEMPT)
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12d (retention): {type(_e).__name__}: {_e}")

    # 6.12e — the s8 risk-contribution monitors. "Anything that accrues a time series starts
    # now; anything that analyses can wait." So M1/M2/M3 verdicts are REPORTED, and an
    # INSUFFICIENT_DATA verdict is a normal, expected reading rather than a failure.
    try:
        import risk_contribution as _rc
        # ══════════════════════════════════════════════════════════════════════════════════
        # ISA-0456 / ISA-0551 — THE PRODUCER, WIRED. `contributions()` and `record_run()` were
        # called by NOTHING outside their own _selftest, `risk_contribution_ledger.json` did
        # not exist, and `evaluate()` therefore read an empty ledger and returned
        # INSUFFICIENT_DATA every month. That reads as "we are accruing data, be patient" while
        # nothing was accruing — FC-A, and the same absent-execution shape as ISA-0507.
        # ⚑ THE MATRIX IS PASSED. `contributions(matrix=...)` shipped 28-Aug and no caller ever
        # supplied one, so every reading would have stamped `w_sigma_proxy` — the
        # correlation-blind formula — while a measured matrix sat on disk. The error runs
        # TOWARD the risk: it overstates the diversifiers (ONT.L 4.4x) and understates the
        # correlated core (MU by 8.9pp), so a replacement rule driven by it preferentially
        # challenges the names that are REDUCING sleeve risk.
        # ⚑ rho AND sigma come from ONE call to `stock_price_fetch.matrix()` over ONE window
        # (P2.5) — a sigma from another window assembles a covariance matrix out of two.
        _rc_note = None
        try:
            import stock_price_fetch as _spf_rc, position_sizing as _ps_rc
            _sleeve_names = _spf_rc._portfolio_sleeve(_pd_doc)      # broker truth, one home
            _mx = _spf_rc.matrix(tickers=_sleeve_names)
            _sig = _mx.get("sigma_ann") or {}
            _nav_rc = float(((_pd_doc or {}).get("summary") or {})["total_value_gbp"])
            # weights as % of NAV, keyed to the STORE's names. A stock the store cannot name is
            # EXCLUDED AND COUNTED, never defaulted (R4.9).
            _wts_rc, _unmatched = {}, []
            for _st_row in ((_pd_doc or {}).get("stocks") or []):
                _tk = _st_row.get("ticker")
                _val = _st_row.get("value_gbp")
                if not _tk or _val in (None, ""):
                    _unmatched.append({"ticker": _tk, "why": "no ticker or no value_gbp"})
                    continue
                _key = next((n for n in _mx.get("names", [])
                             if n == _tk or n.split(".")[0] == str(_tk).split(".")[0]), None)
                if _key is None:
                    _unmatched.append({"ticker": _tk, "why": "no weekly-return series in the store"})
                    continue
                _wts_rc[_key] = float(_val) / _nav_rc * 100.0
            _starter = float(_ps_rc.ladder()["STARTER"])
            _contrib = _rc.contributions(_wts_rc, _sig, starter_pct=_starter,
                                         matrix=_mx.get("rho"))
            _rc.record_run(_contrib, run_date=(run_date.isoformat()
                                              if isinstance(run_date, dt_date) else None))
            _v21["risk_contribution"] = {
                "rc_basis": _contrib.get("rc_basis"),
                "sigma_p": _contrib.get("sigma_p"),
                "n_eff": _contrib.get("n_eff"),
                "pair_coverage": _contrib.get("pair_coverage"),
                "rows": _contrib.get("rows"),
                "excluded": _contrib.get("excluded"),
                "unmatched_positions": _unmatched,
                "matrix_as_of": _mx.get("weeks_used_max"),
                "matrix_coverage": _mx.get("coverage"),
                "basis_note": ("rho and sigma from ONE stock_price_fetch.matrix() call over one "
                               "window (P2.5). ISA-0456: matrix=None would be the "
                               "correlation-blind w_sigma_proxy."),
            }
            _s612.append("risk_contribution %s over %d name(s)"
                         % (_contrib.get("rc_basis"), len(_contrib.get("rows") or {})))
            if _unmatched:
                warnings.append(
                    "Step 6.12e: %d direct-stock position(s) have NO weekly-return series and "
                    "are EXCLUDED from the risk decomposition, not defaulted: %s. The published "
                    "risk shares therefore cover %d of %d positions (R4.9)."
                    % (len(_unmatched), "; ".join("%s (%s)" % (u["ticker"], u["why"])
                                                  for u in _unmatched),
                       len(_wts_rc), len(_wts_rc) + len(_unmatched)))
            if _contrib.get("rc_basis") != "covariance_mctr":
                warnings.append(
                    "Step 6.12e: the risk decomposition fell back to rc_basis=%r. ISA-0456: the "
                    "correlation-blind proxy OVERSTATES diversifiers and UNDERSTATES the "
                    "correlated core, so the D10 replacement nomination it feeds is biased "
                    "toward challenging the names that reduce sleeve risk."
                    % _contrib.get("rc_basis"))
        except Exception as _e2:                                   # noqa: BLE001
            # ⚑ A REFUSAL, NAMED — never a silent fall-through to evaluate() on a stale ledger.
            _rc_note = "%s: %s" % (type(_e2).__name__, _e2)
            _v21["risk_contribution"] = {"state": "REFUSED", "reason": _rc_note}
            warnings.append(
                "Step 6.12e THIS MONTH'S RISK DECOMPOSITION WAS NOT PRODUCED (%s). M1/M2/M3 "
                "below are evaluated on the ledger AS IT STOOD, so their sample does NOT "
                "include September — an INSUFFICIENT_DATA verdict here means 'not measured "
                "this month', not 'measured and thin' (ISA-0456)." % _rc_note)
        _m = _rc.evaluate()
        _v21["risk_monitors"] = _m
        _s612.append("M1 %s / M2 %s / M3 %s" % (_m["M1"]["verdict"], _m["M2"]["verdict"],
                                                _m["M3"]["verdict"]))
        if _m["M1"]["verdict"] == "NON_INFORMATIVE":
            warnings.append("Step 6.12e M1 NON-INFORMATIVE: " + _m["M1"]["detail"])
        if _m["M3"]["verdict"] == "STOP_ACTING":
            warnings.append("Step 6.12e M3 SAYS STOP ACTING: " + _m["M3"]["detail"])
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12e (risk_contribution): {type(_e).__name__}: {_e}")

    # 6.12f — §9 ACTIVE-FUND DRAWDOWN, behind the A7 benchmark precondition (ISA-0440).
    # ⚑ THIS STEP EXISTS BECAUSE THE LABEL ABOVE ALREADY CLAIMED IT DID. `_mf_begin("6.12", ...)`
    # has read "... + asset_drawdown + ..." since 26-Aug and the step never imported the module:
    # an absent execution reporting success, with the run surface naming the absent module out
    # loud. Raised as its own register item.
    try:
        import asset_drawdown as _ad
        _bm = _ad.benchmark_precondition()
        _fad = _ad.fund_active_drawdowns(benchmark_state=_bm)
        _v21["fund_active_drawdown"] = _fad
        _s612.append("s9 active drawdown %d/%d read, benchmark %s"
                     % (_fad["n_read"], _fad["n_funds"], _bm["state"]))
        if not _bm.get("clean"):
            warnings.append(
                "Step 6.12f §9 REFUSED — BENCHMARK REGISTRY %s: %s. The active-fund drawdown "
                "flag is a fund return MINUS a benchmark return, and a benchmark short of its "
                "dividends makes that difference look BETTER than it is, so a dirty registry "
                "does not make this flag noisy, it makes it REASSURING. Every fund reads "
                "UNMEASURED and is capped at current (A7)."
                % (_bm["state"], "; ".join(_bm.get("errors") or []) or "unreadable"))
        for _u in _fad.get("unreadable", []):
            warnings.append("Step 6.12f: %s could not be measured against its comparator (%s) — "
                            "COUNTED, not dropped (R4.9)." % (_u["sedol"], _u["error"]))
        _meas = [f for f in _fad.get("funds", []) if f["state"] != "UNMEASURED"]
        _deep = sorted((f for f in _fad.get("funds", []) if f["state"] == "UNMEASURED"),
                       key=lambda f: f["current_active_drawdown_pct"])[:3]
        if len(_meas) < _fad["n_read"]:
            warnings.append(
                "Step 6.12f §9 CAN ONLY FIRE ON %d OF %d FUNDS: the rest have fewer than the "
                "declared minimum of completed own-history episodes, so their state is "
                "UNMEASURED and they are capped at current — never assigned a state from a thin "
                "sample (A6/R4.10). ⚑ THE DEEPEST UNMEASURED ACTIVE DRAWDOWNS ARE PUBLISHED "
                "ANYWAY, because an unmeasurable shortfall is not an absent one: %s"
                % (len(_meas), _fad["n_read"],
                   "; ".join("%s %.1f%% vs %s over %dm"
                             % (f["sedol"], f["current_active_drawdown_pct"], f["comparator"],
                                f["months_since_peak"]) for f in _deep) or "none"))
        for _f in _meas:
            if _f["size_action"] in ("TRIM_CANDIDATE", "REVIEW"):
                warnings.append("Step 6.12f §9 %s: %s vs %s — %s"
                                % (_f["state"], _f["sedol"], _f["comparator"], _f["detail"]))
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12f (asset_drawdown): {type(_e).__name__}: {_e}")

    # 6.12g — A20 SHADOW SLOT COMPETITION (ISA-0440). It trades nothing and it never will from
    # here: the verdict enters the email as a proposal like every other action.
    # ⚑ THE RUN IS RECORDED EVEN WHEN THE VERDICT IS UNMEASURED. Raj's condition for admitting
    # A20 was two runs of published would-have-traded, and "we could not compute it" is one of the
    # things those two runs are supposed to reveal. Capture first, analyse later (R6.5).
    try:
        import retention as _ret20
        _sleeve_pct = None
        try:
            _sleeve_pct = float((summary.get("capital_destination") or {})
                                .get("stock_sleeve_weight_pct"))
        except Exception:                                          # noqa: BLE001
            _sleeve_pct = None
        # the ceiling binds only when the demand-pull rule has nothing left to offer
        _binding = bool((summary.get("capital_destination") or {}).get("stock_max_gbp") == 0)
        _cands = ((summary.get("v21") or {}).get("slot_candidates")) or []
        _verdicts = []
        for _c in _cands:
            try:
                _verdicts.append(_ret20.slot_competition(
                    incumbent=_c.get("incumbent") or {}, challenger=_c.get("challenger") or {},
                    binding_ceiling=_binding, pool_size=_c.get("pool_size"),
                    dispersion_pp=_c.get("dispersion_pp"),
                    estimate_se_pp=_c.get("estimate_se_pp"),
                    friction_pp=_c.get("friction_pp")))
            except Exception as _e:                                # noqa: BLE001
                _verdicts.append({"verdict": "ERROR", "detail": "%s: %s" % (type(_e).__name__, _e)})
        _log = _ret20.shadow_record(_verdicts, run_label=month_label)
        _v21["slot_competition"] = {
            "mode": ("LIVE" if _ret20.A20_LIVE else "SHADOW"),
            "binding_ceiling": _binding,
            "n_candidates": len(_cands), "verdicts": _verdicts,
            "shadow_log": _log,
            "constraint": _ret20.A20_ISA0167_CONSTRAINT,
        }
        _s612.append("A20 shadow run %d recorded (%d verdict(s))"
                     % (_log["runs_recorded"], len(_verdicts)))
        if not _cands:
            warnings.append(
                "Step 6.12g A20 SHADOW: no incumbent/challenger pair was offered to the slot "
                "comparator this run, so it published nothing. That is not the same as 'no "
                "challenger beat an incumbent' — the comparison did not happen. A20 needs %d "
                "recorded shadow run(s) before it may go live and has %d; and the shadow count "
                "is not permission on its own (ISA-0167's surviving constraint requires the "
                "E[r]-gap trade to be measured in its own right, and it has not been)."
                % (_ret20.A20_MIN_SHADOW_RUNS, _log["runs_recorded"]))
        for _v in _verdicts:
            if _v.get("verdict") == "WOULD_REPLACE":
                warnings.append("Step 6.12g A20 SHADOW WOULD REPLACE %s with %s: %s"
                                % (_v.get("incumbent"), _v.get("challenger"), _v.get("detail")))
            elif _v.get("verdict") == "UNMEASURED":
                warnings.append("Step 6.12g A20 UNMEASURED: %s" % _v.get("detail"))
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.12g (A20 slot competition): {type(_e).__name__}: {_e}")

    # ⚑ A12's grid is carried INTO summary.v21 on purpose (ISA-0440). Step 6.10 computes it, and
    # `summary.capital_destination` — the whole marginal-pound router — has NO email renderer at
    # all, which is ISA-0439's class in a place ISA-0439 did not reach. Rather than invent a
    # second enforcement mechanism, the new key is put under the EXISTING one: once it is in
    # summary.v21, `consistency_check.pair_v21_summary_has_renderer()` fails the build until it
    # is declared and rendered (R4.4 — extend the mechanism, do not parallel it).
    if summary.get("plan_stability") is not None:
        _v21["plan_stability"] = summary["plan_stability"]

    summary["v21"] = _v21
    _mf_measure(status=("OK" if _s612 else "DEGRADED"),
                note=("; ".join(_s612) if _s612 else "the V2.1 stack produced no result"))
    print("  " + ("; ".join(_s612) if _s612 else "no result"))

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
    # ── Step 6.10 — THE MARGINAL-POUND ROUTER (20-Aug-2026) ───────────────────────────────────
    # ⚑ WHY THIS STEP EXISTS. `capital_destination` was built 16-Aug-2026, closed five register
    # items, carried 22 green assertions — and was called by NOTHING. Same shape as the three
    # modules Step 6.09 rescued the day before, and the framework's second failure class: an
    # absent execution that reported success. ISA-0386 / ISA-0387 / ISA-0388 / ISA-0390 all land
    # inside this module, so wiring it is part of shipping them; a router nobody runs allocates
    # nothing and would have gone stale exactly as quietly.
    #   6.10a  the per-fund exposure vectors the router's C1 reads (ISA-0392) — a REPORT of the
    #          artefact's age and provenance. It is NOT refreshed here: the capture needs network
    #          and this run has none, and a step that pretends to refresh is worse than one that
    #          declares it cannot.
    #   6.10b  the destination itself: sleeve split, ranking, allocation, idle capital priced.
    #   6.10c  the waiting-room / recall position (ISA-0390).
    print("\n[6.10] Marginal-pound router: sleeve split, estimation-free ranking, recall leg...")
    _mf_begin("6.10", "capital_destination + fund_exposure_vectors + waiting_room")
    _s610 = []
    try:
        import fund_exposure_vectors as _fev
        _fv = json.load(open(os.path.join(SCRIPT_DIR, "fund_exposure_vectors.json"))) \
            if os.path.exists(os.path.join(SCRIPT_DIR, "fund_exposure_vectors.json")) else None
        if _fv is None:
            warnings.append(
                "Step 6.10a: fund_exposure_vectors.json is ABSENT, so the router's C1 runs at "
                "DECLARED-MANDATE resolution (one-hot on the fund's own benchmark) instead of "
                "look-through. That is a degradation, not a failure — but ISA-0333 / ISA-0160 / "
                "ISA-0328 stay BLOCKED until the capture is re-run (ISA-0392). The capture needs "
                "network and this run has none.")
        else:
            _sp = _fv.get("as_of_spread_days") or {}
            summary["fund_exposure_vectors"] = {
                "n_funds": len(_fv.get("vectors") or {}), "stale": _fv.get("stale"),
                "age_days": _sp, "corroboration": _fv.get("corroboration"),
                "share_class_substitutions": _fv.get("share_class_substitutions")}
            _s610.append("exposure vectors %d funds, %d-%d days old"
                         % (len(_fv.get("vectors") or {}), _sp.get("min", -1), _sp.get("max", -1)))
            if _fv.get("stale"):
                warnings.append(
                    "Step 6.10a EXPOSURE VECTORS STALE for %s (older than %d days). The router "
                    "still ranks on them; a stale vector is a fact about the capture, not a "
                    "reason to fall back silently (ISA-0392)."
                    % (", ".join(_fv["stale"]), _fev.STALE_AFTER_DAYS))
            if _fv.get("share_class_substitutions"):
                warnings.append(
                    "Step 6.10a EXPOSURE VECTORS use a DIFFERENT SHARE CLASS for %s — same "
                    "portfolio, different fee or distribution line. Named so it is a known "
                    "substitution rather than an assumed identity (R6.1)."
                    % ", ".join(_fv["share_class_substitutions"]))
    except Exception as _e:
        warnings.append(f"Step 6.10a (fund_exposure_vectors): {type(_e).__name__}: {_e}")
        print(f"  fund_exposure_vectors FAILED (non-fatal): {_e}")

    try:
        import capital_destination as _cd
        _cdr = _cd.build(out_path=os.path.join(SCRIPT_DIR,
                                               f"capital_destination_{month_label}.json"))
        if _cdr.get("state") == "OK":
            _sl, _fa = _cdr["sleeve_split"], _cdr["fund_allocation"]
            # ── ISA-0447 (26-Aug-2026): ONE HOME FOR THE SUMMARY, AND IT IS THE MODULE ───
            # ⚑ The shape of `summary.capital_destination` is now declared by
            # `capital_destination.summary_for_run_context()`, not assembled inline here. It was
            # inline for six days and NOTHING RENDERED IT (ISA-0447): the email now does, and an
            # email contract with a dict literal buried in the orchestrator is a contract with
            # code no test can reach. The module that owns the document owns its summary (R4.4).
            summary["capital_destination"] = _cd.summary_for_run_context(_cdr)
            _s610.append("router %s, stock cap GBP %.2f [%s], funds %s, idle GBP %.2f"
                         % (_cdr["state"], _sl.get("stock_max_gbp") or 0.0,
                            (_sl.get("scaling_freeze") or {}).get("basis"), _fa.get("state"),
                            _fa.get("unallocated_gbp") or 0.0))
            if (_sl.get("executability") or {}).get("state") == "NOT_EXECUTABLE":
                warnings.append(
                    "Step 6.10b STOCK CAP IS NOT EXECUTABLE: GBP %.2f is below the smallest "
                    "position size the framework declares (GBP %.2f, %s). A cap that cannot open "
                    "a position is GBP 0 of executable capital reported as a non-zero number "
                    "(ISA-0387)."
                    % (_sl["executability"]["stock_max_gbp"],
                       _sl["executability"]["smallest_declared_position_gbp"] or 0.0,
                       _sl["executability"]["smallest_declared_position_basis"]))
            if (_cdr["residual"] or {}).get("unallocated_gbp", 0) > 0:
                warnings.append(
                    "Step 6.10b IDLE CAPITAL GBP %.2f (%.1f%% of what was offered), priced at GBP "
                    "%s/yr net of the waiting-room yield. Idle capital is a DECISION with a stated "
                    "price, never a default."
                    % (_cdr["residual"]["unallocated_gbp"],
                       _cdr["residual"].get("pct_of_capital_offered") or 0.0,
                       _cdr["residual"].get("annual_opportunity_cost_net_of_waiting_room_gbp")))
            _db = _cdr.get("declared_bands") or {}
            if _db.get("not_repaired"):
                warnings.append(
                    "Step 6.10b DECLARED PER-FUND BAND BREACH not repaired: %s. Band restoration "
                    "is C5, the tie-break (Raj, 19-Aug-2026 / ISA-0386), so a breach is repaired "
                    "only if the fund also wins on C1-C4. Whether a declared band is a preference "
                    "or a limit has NOT been decided — stated, not resolved by implementation."
                    % ", ".join(_db["not_repaired"]))
            if not (_cdr["verification"]["parity"] or {}).get("pass"):
                warnings.append("Step 6.10b ROUTER PARITY FAILED: inert criteria %s"
                                % (_cdr["verification"]["parity"].get("inert_criteria")))
            # ── 6.10d — A12 PLAN STABILITY (ISA-0440) ───────────────────────────────────
            # ⚑ The plan is a LEXICOGRAPHIC ranking, and a lexicographic ranking over near-tied
            # inputs can flip on noise. A12's point is that the instability must be VISIBLE
            # rather than inferred, so the grid runs every month whether or not it finds any.
            try:
                _PLAN_STABILITY_PENDING.append(_cdr)
                _s610.append("plan stability DEFERRED to the assurance group (ISA-0594)")
            except Exception as _e:                                # noqa: BLE001
                warnings.append(f"Step 6.10d (plan_stability): {type(_e).__name__}: {_e}")
        else:
            warnings.append("Step 6.10b: capital_destination %s" % _cdr.get("state"))
    except Exception as _e:
        warnings.append(f"Step 6.10b (capital_destination): {type(_e).__name__}: {_e}")
        print(f"  capital_destination FAILED (non-fatal): {_e}")

    try:
        import waiting_room as _wr
        _park = _wr.outstanding()
        _fz = _wr.freeze_state()
        summary["waiting_room"] = {"parked_gbp": _park.get("parked_gbp"),
                                   "lots_live": _park.get("lots_live"),
                                   "recall": _fz.get("state"),
                                   # ISA-0447: the REASON travels with the state. "BARRED" with
                                   # no reason is a verdict the reader cannot check.
                                   "recall_reason": _fz.get("reason")}
        _s610.append("waiting room GBP %.2f parked, recall %s"
                     % (_park.get("parked_gbp") or 0.0, _fz.get("state")))
        if _fz.get("state") in ("BARRED", "REFUSED") and (_park.get("parked_gbp") or 0) > 0:
            warnings.append(
                "Step 6.10c PARKED CAPITAL IS LOCKED IN: GBP %.2f sits in funds as a TIMING "
                "decision and the recall leg is %s — %s Parking under an active freeze is not a "
                "reversible decision (ISA-0390 x ISA-0387)."
                % (_park["parked_gbp"], _fz["state"], _fz.get("reason", "")))
    except Exception as _e:
        warnings.append(f"Step 6.10c (waiting_room): {type(_e).__name__}: {_e}")
        print(f"  waiting_room FAILED (non-fatal): {_e}")

    _mf_measure(status=("OK" if _s610 else "DEGRADED"),
                note=("; ".join(_s610) if _s610 else "the marginal-pound router produced no result"))
    print("  " + ("; ".join(_s610) if _s610 else "no result"))

    # ── Step 6.11 — THE REGIONAL M LAYER AND THE FUND STRUCTURAL E[r] (20-Aug-2026) ───────────
    # ⚑ 6.11a regional_m (ISA-0160)  ·  6.11b fund_expected_return (ISA-0328).
    # Placed BETWEEN 6.10 (which produces the per-fund exposure vectors) and 6.08 (which consumes
    # the result), because that is the dependency order. Wired in the SAME change that built them:
    # `capital_destination` closed five items, carried 22 green assertions and was called by
    # nothing for four days, and `strategic_allocation.attribution` was built and unreached for
    # four days more. A module the monthly run never invokes is not part of the framework however
    # many assertions it passes.
    # ⚑ BOTH SHIP BEHAVIOUR-NEUTRAL. With the net buyback yield undeclared every M_k is UNMEASURED
    # and every fund E[r] refuses; `fund_expected_return.OPERATIVE` is False and
    # `return_architecture.ER_BASIS_OPERATIVE` is untouched. Nothing here moves a verdict today.
    print("\n[6.11] Regional M layer and fund structural E[r]...")
    _mf_begin("6.11", "regional_m + fund_expected_return")
    _s611 = []
    try:
        import regional_m as _rm
        _rmr = _rm.build(as_of=(run_date.isoformat() if isinstance(run_date, dt_date) else None))
        st = _rmr.get("state")
        if st == "NO_CAPTURE":
            warnings.append(
                "Step 6.11a: regional_m_inputs.json is ABSENT — every M_k is UNMEASURED and every "
                "consumer refuses. This is the honest state, not a failure, but the layer is doing "
                "nothing until the capture is restored (assisted Cowork step; the device has no "
                "network).")
        elif st == "DISABLED":
            warnings.append("Step 6.11a: regional_m is DISABLED by its rollback constant.")
        else:
            _wi = _rmr.get("world_identity") or {}
            _s611.append("regional_m %s, world identity %s" % (st, _wi.get("verdict")))
            _blocked = ((_rmr.get("m") or {}).get("blocked_cells") or [])
            if _blocked:
                warnings.append(
                    "Step 6.11a REGIONAL M PARTIAL: %d of 7 cells UNMEASURED (%s). %s"
                    % (len(_blocked), ", ".join(_blocked), _rmr.get("partial_reason", "")[:240]))
            for _c in ((_rmr.get("age") or {}).get("stale_cells") or []):
                warnings.append(
                    "Step 6.11a REGIONAL M CAPTURE STALE for cell %s (older than %d days). The "
                    "refresh is an ASSISTED Cowork step, never a pre-run step." % (_c, _rm.STALE_AFTER_DAYS))
            for _c in ((_rmr.get("corroboration") or {}).get("breaches") or []):
                warnings.append(
                    "Step 6.11a CORROBORATION BREACH on cell %s — more than %.1fpp from the "
                    "declared external capital-market assumption. Published, never blended (R6.2)."
                    % (_c, _rm.CORROBORATION_TOL_PP))
            _pl = _rmr.get("plausibility") or {}
            if _pl.get("state") == "MEASURED" and not _pl.get("within_band"):
                warnings.append(
                    "Step 6.11a PLAUSIBILITY: implied real PER-SHARE growth %.2f%% against a "
                    "measured historical reference of %.2f%%. Above the reference means the "
                    "construction is GENEROUS; far above it is the signature of the ISA-0405 "
                    "aggregate-vs-per-share double-count."
                    % (_pl["implied_real_per_share_growth_pct"],
                       _pl["historical_reference"]["value"]))
            summary["regional_m"] = {
                "state": st, "world_identity": _wi.get("verdict"),
                "blocked_cells": _blocked,
                "specification": (_rmr.get("m") or {}).get("operative_specification")}
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.11a (regional_m): {type(_e).__name__}: {_e}")

    try:
        import fund_expected_return as _fer
        _ferr = _fer.build(as_of=(run_date.isoformat() if isinstance(run_date, dt_date) else None))
        if _ferr.get("state") == "DISABLED":
            warnings.append("Step 6.11b: fund_expected_return is DISABLED by its rollback constant.")
        else:
            _sl = _ferr.get("sleeve") or {}
            _un = _ferr.get("unmeasured") or []
            _s611.append("fund E[r] %s (%d/%d measured)"
                         % (_ferr.get("state"), len(_ferr.get("funds") or {}) - len(_un),
                            len(_ferr.get("funds") or {})))
            if _un:
                warnings.append(
                    "Step 6.11b FUND E[r] PARTIAL: %d of %d funds UNMEASURED. Every one NAMES what "
                    "it is blocked on and none is given a sleeve average (R2.10)." % (len(_un), len(_ferr.get("funds") or {})))
            if _sl.get("state") == "MEASURED":
                warnings.append(
                    "Step 6.11b FUND SLEEVE FORWARD E[r] %.2f%% over %.1f%% covered weight. ⚑ This "
                    "is PUBLISHED, not OPERATIVE: return_architecture.ER_BASIS_OPERATIVE is "
                    "unchanged and no verdict moves on it (D-13's null-behaviour-delta pattern)."
                    % (_sl["structural_er_pct"], 100 * _sl["covered_weight"]))
            _od = _ferr.get("ordering_diagnostic") or {}
            if _od.get("state") == "MEASURED" and _od.get("dilution_pp") is not None:
                warnings.append(
                    "Step 6.11b ORDERING DIAGNOSTIC (NON-BINDING): the marginal pound's "
                    "money-weighted forward E[r] is %.2f%% against a sleeve of %.2f%% (%+.2fpp). "
                    "Not evidence the ordering is wrong — it is what C1 does when it closes the US "
                    "underweight. One month is an anecdote; a persistent sign belongs to ISA-0333."
                    % (_od["money_weighted_er_pct"], _od["sleeve_er_pct"], _od["dilution_pp"]))
            summary["fund_expected_return"] = {
                "state": _ferr.get("state"), "operative": _ferr["_meta"]["operative"],
                "sleeve_pct": _sl.get("structural_er_pct"), "n_unmeasured": len(_un)}
    except Exception as _e:                                        # noqa: BLE001
        warnings.append(f"Step 6.11b (fund_expected_return): {type(_e).__name__}: {_e}")

    _mf_measure(status=("OK" if _s611 else "DEGRADED"),
                note=("; ".join(_s611) if _s611 else "the regional M layer produced no result"))
    print("  " + ("; ".join(_s611) if _s611 else "no result"))

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
        # ⚑ THREE OUTCOMES, THREE MESSAGES (ISA-0554). `written=False` used to mean exactly one
        # thing — "already frozen, identical". Since freeze() now REFUSES an empty cohort
        # rather than making the month immutable against its own real stack, False also means
        # "nothing to freeze yet". Printing the old sentence for the new case would report a
        # freeze that never happened, which is the failure mode this whole file guards against.
        _refused = _coh.get("freeze_refused")
        if _written:
            _state = "frozen"
        elif _refused:
            _state = "NOT FROZEN — 0 BUY rows; will freeze on the pass that produces a stack"
        else:
            _state = "already frozen — identical"
        print(f"  Cohort {month_label}: {_coh['n_buys']} BUY recommendations, "
              f"hash {_coh['stack_hash']} ({_state})")
        if _refused:
            warnings.append("Step 8c: " + _refused)
        _mf_measure(rows_out=_coh["n_buys"], coverage=1.0,
                    status=("SKIPPED" if _refused else None),
                    note=f"stack_hash={_coh['stack_hash']} written={_written}"
                         + ("; refused: empty cohort" if _refused else ""))
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

    # ── ISA-0594: run_context IS WRITTEN HERE, BEFORE THE ASSURANCE STAGES, AND AGAIN AFTER ──
    # ⚑ THE REVIEW'S PRIMARY INPUT MUST NOT BE HOSTAGE TO THE TAIL OF THE RUN. Steps 1-9 are
    # what tomorrow reads; 6.10d, 9d and 6.99 are assurance ABOUT that work, not inputs to it.
    # While run_context was written once, at the very end, an overrun anywhere after this point
    # discarded a completed pre-run entirely: on 05-Sep-2026 the run needed ~300s against a
    # ~178s host-shell ceiling and run_context_sep_2026.json had NEVER existed for September.
    # So the staging file lands as soon as its contents are complete, marked PENDING, and is
    # REPLACED by the full picture when the assurance stages finish.
    # ⚑ PENDING IS NOT PASSED (R2.10). The provisional file carries status PARTIAL and names
    # every stage that has not run, so a consumer can never read "assurance absent" as
    # "assurance clean" — which is the whole failure mode this guards against.
    _ASSURANCE_STAGES = ["6.10d (plan stability under perturbation)",
                         "9d (mechanical asserts A10/A11/A18/A19/A20/A22 + P7a)",
                         "6.99 (execution-ledger reconciliation + integrity queue)"]
    _elapsed_9c = round(time.time() - _RUN_STARTED_AT, 1)
    summary["runtime"] = {
        "started_at": datetime.fromtimestamp(_RUN_STARTED_AT).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s_at_provisional_write": _elapsed_9c,
        "host_shell_ceiling_s": HOST_SHELL_CEILING_S,
        "state": ("WITHIN" if _elapsed_9c < HOST_SHELL_CEILING_S else "EXCEEDED"),
        "basis": ("measured wall clock; the ceiling is the observed host MCP tool-call limit "
                  "(kills at 173954ms and 177988ms, 05-Sep-2026, ISA-0594)"),
    }
    if _elapsed_9c >= HOST_SHELL_CEILING_S:
        warnings.append(
            "RUNTIME: Steps 1-9 took %.0fs against a %.0fs host-shell ceiling (ISA-0594). The "
            "provisional run_context still landed, but the assurance stages will be cut off. "
            "Profile the run before adding work to it." % (_elapsed_9c, HOST_SHELL_CEILING_S))
    summary["assurance"] = {
        "state": "PENDING",
        "stages_not_yet_run": list(_ASSURANCE_STAGES),
        "meaning": ("This file was written BEFORE the assurance stages. PENDING means they have "
                    "NOT RUN — it does not mean they passed. If this value is still PENDING when "
                    "you read it, the run was cut off after Step 9 and the assurance verdicts "
                    "are ABSENT, not clean (R2.10)."),
    }
    # ⚑ THE WARNING GOES ON THE REAL ACCUMULATOR, and is RETRACTED when it stops being true.
    # Composing it privately per-write would have kept `warnings` clean, but ISA-0447 reads the
    # string literals passed to `warnings.append` to prove an ESCALATED summary key actually has
    # a surface — and a check that cannot tell a mention from a use is one that gets deleted
    # rather than fixed. So the escalation is a real append, and `_retract_assurance_warnings`
    # removes it at each point where the state it describes has changed. One mechanism, visible
    # to the checker, and never left asserting a state that has moved on.
    warnings.append(
        "ASSURANCE PENDING — this run_context was written before %s. Those checks have NOT RUN; "
        "their absence is not a clean result (R2.10). Complete with: monthly_isa_prerun.py "
        "--plan-stability-only, or re-run the pre-run." % ", ".join(_ASSURANCE_STAGES))
    try:
        _prov_path = write_run_context(
            month_label, run_month, portfolio_path, xray_path, analytics_path,
            watchlist_metrics_path, watchlist_scored_path, step9_pre_path, email_path,
            summary, flags, warnings, "PARTIAL", "")
        print("\n[9c] Provisional run_context written at %.0fs -- %s"
              % (_elapsed_9c, os.path.basename(_prov_path)))
    except Exception as _pe:                                       # noqa: BLE001
        warnings.append("ISA-0594 provisional run_context write FAILED: %s: %s"
                        % (type(_pe).__name__, _pe))
        print("  [9c] provisional run_context FAILED: %s" % _pe)

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
                        _row["est_basis"] = _ri.get("basis")
                        _mr = _row.get("min_return_pct")
                        if _mr is not None:
                            # ⚑ ISA-0401/ISA-0402: `below_threshold` compares an estimate to a FORWARD hurdle, so it may
                            # only be set when the estimate's declared basis IS forward. A trailing 3-year return read as
                            # "below the hurdle" is not a finding, it is a category error — and it drove a live SELL.
                            if _fr.basis_is_forward(_row.get("est_basis")):
                                _row["below_threshold"] = bool(_ri["est_return_pct"] < _mr)
                            else:
                                _row["below_threshold"] = None
                                _row["below_threshold_refused"] = (
                                    "basis `%s` is not a forward decomposition; comparing it to "
                                    "the forward `min_expected_return` hurdle is refused "
                                    "(ISA-0401)" % (_row.get("est_basis") or "unknown"))
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
                    # ⚑ ISA-0409 (20-Aug-2026). THIS BLOCK USED TO READ "any failing invariant"
                    # and, on failure, LEAVE Sections A/B/C on the est_return basis — the input
                    # register C4 proved INVERTED. Two things were wrong and both are fixed at
                    # their own level:
                    #   (a) the gate was ALL-OR-NOTHING across unrelated quantities. I-RA-8 guards
                    #       the M* golden fixture (ISA-0383) and says nothing about whether
                    #       Sections A/B/C reconcile, yet it withdrew them. The gate is now SCOPED
                    #       and the scope is owned by return_architecture, READ here and never
                    #       re-derived (R4.4).
                    #   (b) the fallback substituted a WORSE input. A refusal that hands the
                    #       decision to something the framework has already proven inverted is a
                    #       silent downgrade, not caution. Sections A/B/C now publish UNMEASURED
                    #       with the failing invariant NAMED (R2.10/R4.3).
                    # ⚑ MEASURED AT THE TIME: I-RA-5 had been failing since 12-Aug and I-RA-8
                    # since 19-Aug, so the 05-Sep pre-run would have published the headline
                    # verdict off twelve hand-typed estimates dated 2026-07-05. The 01-Aug run
                    # escaped only because it carried SIX invariants and neither existed yet.
                    _gate = _rar.get("adoption_gate")
                    if not isinstance(_gate, dict):
                        # An older artefact with no gate: REFUSE rather than guessing the scope.
                        _gate = {"adoptable": False, "blocking_invariants": ["NO_ADOPTION_GATE"],
                                 "on_refusal": "UNMEASURED_INVARIANT_FAILED",
                                 "blocking_detail": {"NO_ADOPTION_GATE": (
                                     "return_architecture emitted no adoption_gate, so which "
                                     "invariants withhold Sections A/B/C is unknown. Refusing "
                                     "rather than assuming (R4.7).")}}
                    for _c, _d in (_gate.get("failing_out_of_scope") or {}).items():
                        warnings.append(
                            "A11/6.08 %s FAILED but is scoped `%s`, so it withdraws that quantity "
                            "and NOT Sections A/B/C: %s"
                            % (_c, _d.get("scope"), str(_d.get("detail"))[:200]))
                    if _gate.get("unscoped_invariants"):
                        errors.append(
                            "A11/6.08: invariant(s) %s carry no declared scope. A new invariant "
                            "that nobody classified must not silently inherit 'does not matter' — "
                            "declare it in return_architecture.INVARIANT_SCOPE (R4.7)."
                            % _gate["unscoped_invariants"])
                    if not _gate.get("adoptable"):
                        _bad = _gate.get("blocking_invariants") or []
                        _why = "; ".join("%s: %s" % (k, str(v)[:160])
                                         for k, v in (_gate.get("blocking_detail") or {}).items())
                        # ⚑ REFUSE. Do NOT substitute est_return.
                        for _sec in ("section_a", "section_b", "section_c"):
                            _blk = dict(_ana.get(_sec) or {})
                            _prior = {k: _blk.get(k) for k in
                                      ("weighted_avg_return", "value_pct", "total_return",
                                       "verdict", "result")}
                            _blk.update({
                                "status": "UNMEASURED",
                                "verdict": None,
                                "basis": _gate.get("on_refusal", "UNMEASURED_INVARIANT_FAILED"),
                                "unmeasured_reason": (
                                    "return_architecture invariant(s) %s FAILED, so the arithmetic "
                                    "does not reconcile and no verdict is issued. ⚑ est_return is "
                                    "NOT substituted: register C4 proved it inverted and it is "
                                    "retained only as est_basis_corroborator. 'I could not compute "
                                    "it' and 'here is a number from an inverted input' must never "
                                    "produce the same output (ISA-0409, R2.10)." % ", ".join(_bad)),
                                "failing_invariants": _bad,
                                "failing_detail": _why,
                                "est_basis_corroborator": {
                                    **(_blk.get("est_basis_corroborator") or {}),
                                    **{k: v for k, v in _prior.items() if v is not None},
                                    "role": ("RETIRED as a decision input (register C4) — shown so "
                                             "the evidence keeps accumulating; NOT operative"),
                                },
                                "source": "return_architecture (Step 6.08) — REFUSED",
                            })
                            for _k in ("weighted_avg_return", "value_pct", "total_return"):
                                _blk[_k] = None
                            _ana[_sec] = _blk
                        errors.append(
                            "A11/6.08 SECTIONS A/B/C UNMEASURED — invariant(s) %s failed and the "
                            "architecture REFUSES rather than falling back to est_return, which "
                            "register C4 proved inverted. %s" % (", ".join(_bad), _why))
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
        # ⚑ ISA-0546 (02-Sep-2026) — SEVERITY IS ROUTED, NOT FLATTENED. check_all() used to
        # return bare strings and EVERY one landed in errors[], so the two permanently
        # stale-stamped SQ regime rows (ISA-0517 refuses the documented remedy) opened every run
        # at ERROR. A permanent ERROR that must be ignored is how a reader stops reading the
        # list that every real prose-vs-config divergence arrives on.
        # ⚑ SEVERITY IS DECLARED AT THE POINT OF PRODUCTION and anything unmarked is ERROR, so
        # the fail-safe direction is preserved: a new pair has to opt IN to being a warning.
        # ⚑ ISA-0590 — the run's own start time, so the ISA-0321 register gate does not
        # fire on the artefacts THIS run just wrote. Tighter than the prefix list:
        # a file the run did NOT write still trips it.
        _recs = _cchk.check_all(tagged=True, since_ts=_RUN_STARTED_AT)
        _a18_err = [r["message"] for r in _recs if r["severity"] == "ERROR"]
        _a18_warn = [r["message"] for r in _recs if r["severity"] != "ERROR"]
        for _m in _a18_err:
            errors.append(f"Step 9d A18 consistency: {_m}")
        for _m in _a18_warn:
            warnings.append(f"Step 9d A18 consistency (WARN, not an error): {_m}")
        summary["a18_consistency"] = {"n_error": len(_a18_err), "n_warn": len(_a18_warn),
                                      "basis": "ISA-0546 - severity declared at production"}
        if _a18_err:
            print(f"  A18: {len(_a18_err)} MISMATCH(ES) -> errors[], "
                  f"{len(_a18_warn)} -> warnings[]")
        elif _a18_warn:
            print(f"  A18: ALL PAIRS GREEN at ERROR grade; {len(_a18_warn)} warning(s)")
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

    # ══════════════════════════════════════════════════════════════════════════════════
    # Step 6.99 — EXECUTION-LEDGER RECONCILIATION + THE RANKED INTEGRITY QUEUE (P0.1/P0.6)
    # ══════════════════════════════════════════════════════════════════════════════════
    # ⚑ LAST, because it reports on what the run ACTUALLY EXECUTED. Reconciling earlier would
    # answer the question using a partial ledger, which is the shape of every finding this
    # instrument exists to catch.
    # ⚑ THE LEDGER WRITE COMES FIRST and its failure is REPORTED: a monitoring layer that
    # degrades silently is worse than none.
    print("\n[6.99] Execution-ledger reconciliation + ranked integrity queue...")
    _mf_begin("6.99", "framework_integrity.report")
    try:
        import framework_integrity as _fi2
        _lp = _fi2.flush_ledger()
        _rec = _fi2.reconcile(SCRIPT_DIR)
        _rep = _fi2.report(SCRIPT_DIR)
        summary["execution_ledger"] = _rec
        summary["integrity_report"] = _rep
        for _e in _rec.get("errors", [])[:10]:
            warnings.append("EXECUTION LEDGER " + _e)
        _mf_measure(rows_out=_rep.get("n_total"),
                    status=("OK" if _rep.get("state") == "OK" else "DEGRADED"),
                    note=("%s; top finding %s; %s"
                          % (_rep.get("state"),
                             (_rep.get("queue") or [{}])[0].get("subject", "none"),
                             _rep.get("suppressed", {}).get("line", ""))))
        print("  ledger %s (%s) — integrity %s, %d finding(s); %s"
              % (_rec.get("state"), _lp, _rep.get("state"), _rep.get("n_total"),
                 _rep.get("suppressed", {}).get("line", "")))
        for _q in (_rep.get("queue") or [])[:5]:
            print("    £%12s  %-22s %s" % (format(_q.get("gbp_exposure", 0.0), ",.2f"),
                                           _q.get("source"), str(_q.get("subject"))[:60]))
    except Exception as _e:                                    # noqa: BLE001
        summary["integrity_report"] = {"state": "UNAVAILABLE",
                                       "reason": "%s: %s" % (type(_e).__name__, _e)}
        _mf_measure(status="DEGRADED", note="framework_integrity unavailable: %s" % _e)
        warnings.append("STEP 6.99 UNAVAILABLE — %s: %s" % (type(_e).__name__, _e))
        print("  UNAVAILABLE — %s" % _e)

    # ── ISA-0594: THE ASSURANCE STAGES ARE ORDERED CHEAPEST-FIRST AND CHECKPOINTED ──────────
    # 9d (~30s) and 6.99 (~8s) are done by here. `plan_stability` costs 37-43s on its own and
    # is the one stage that reliably does NOT fit inside what remains of a ~175s call, so the
    # staging file is rewritten HERE — carrying the 9d and 6.99 verdicts — before it is
    # attempted. Ordering assurance by cost is not cosmetic: it decides which verdicts survive
    # an overrun, and the cheap ones are also the blocking ones.
    summary["assurance"] = {
        "state": "PARTIAL",
        "stages_run": ["9d (mechanical asserts)", "6.99 (execution ledger + integrity queue)"],
        "stages_not_yet_run": (["6.10d (plan stability under perturbation)"]
                               if _PLAN_STABILITY_PENDING else []),
        "meaning": ("9d and 6.99 have run and their verdicts are in this file. Anything still "
                    "listed in stages_not_yet_run has NOT run and is ABSENT, not clean (R2.10). "
                    "Complete it with:  monthly_isa_prerun.py --plan-stability-only"),
    }
    # ⚑ EVERY COPY OF run_context CARRIES A TOTAL. Leaving elapsed_s_total to the final write
    # meant the field was null in exactly the case it exists to describe — the run that did not
    # get to the end. It is stamped at each write and overwritten by the next.
    _e_now = round(time.time() - _RUN_STARTED_AT, 1)
    summary.setdefault("runtime", {})["elapsed_s_total"] = _e_now
    summary["runtime"]["state_total"] = ("WITHIN" if _e_now < HOST_SHELL_CEILING_S else "EXCEEDED")
    _retract_assurance_warnings(warnings)      # 9d and 6.99 have now run
    if _PLAN_STABILITY_PENDING:
        warnings.append(
            "ASSURANCE PARTIAL — 9d and 6.99 ran; Step 6.10d plan stability has NOT run and its "
            "verdict is ABSENT, not clean (R2.10). Complete with: monthly_isa_prerun.py "
            "--plan-stability-only")
    try:
        _ip = write_run_context(
            month_label, run_month, portfolio_path, xray_path, analytics_path,
            watchlist_metrics_path, watchlist_scored_path, step9_pre_path, email_path,
            summary, flags, warnings, status, "")
        print("\n[9e] Interim run_context written at %.0fs (9d + 6.99 captured) -- %s"
              % (round(time.time() - _RUN_STARTED_AT, 1), os.path.basename(_ip)))
    except Exception as _ie:                                       # noqa: BLE001
        warnings.append("ISA-0594 interim run_context write FAILED: %s: %s"
                        % (type(_ie).__name__, _ie))

    if _PLAN_STABILITY_PENDING:
        print("\n[6.10d] Plan stability under perturbation (deferred assurance, ISA-0594)...")
        import capital_destination as _cd_ps
        _s610_late = []
        _run_plan_stability(_cd_ps, _PLAN_STABILITY_PENDING[0], summary, warnings, _s610_late)
        for _line in _s610_late:
            print("  " + _line)

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
    _elapsed_end = round(time.time() - _RUN_STARTED_AT, 1)
    summary.setdefault("runtime", {})["elapsed_s_total"] = _elapsed_end
    summary["runtime"]["state_total"] = ("WITHIN" if _elapsed_end < HOST_SHELL_CEILING_S
                                         else "EXCEEDED")
    _retract_assurance_warnings(warnings)      # every assurance stage has now run
    summary["assurance"] = {"state": "COMPLETE", "stages_not_yet_run": [],
                            "meaning": "the assurance stages ran; their verdicts are in this file"}
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
