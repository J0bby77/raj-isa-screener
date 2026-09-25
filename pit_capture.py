#!/usr/bin/env python3
"""
pit_capture.py - ISA-0722 amendment (Raj 24-Sep-2026): the IRREVERSIBLE point-in-time evidence
vintage for every name the monthly pre-run fetches, INCLUDING rejected/deferred names.

⚑ CAPTURE ONLY. Nothing in the framework reads these fields to make a decision; they exist so the
October 2026 vintage - the first clean post-ISA-0720 forecast vintage - is not lost, and so the
SHADOW research of ISA-0744 (terminal-value E[r]) and ISA-0745 (share-count overlap) can later be
calibrated on point-in-time data instead of today's restatements.

⚑ RIDES THE EXISTING FETCH. Every table is taken from the objects fetch_watchlist_metrics already
pulled. `revenue_estimate` is the only new attribute and it is served from the SAME cached
quoteSummary `earningsTrend` payload as `earnings_estimate` (yfinance 1.7.0,
scrapers/analysis.py `_fetch_earnings_trend`) - MEASURED 24-Sep-2026: 0 extra provider calls.

⚑ FAIL-SOFT AND OBSERVABLE (R4.12). A per-ticker failure is a recorded row (CAPTURE_FAILED /
FETCH_FAILED), a whole-capture failure is a recorded state; neither raises into the scheduled
workflow. Missing is never zero: every optional field is {"state": PRESENT | ABSENT | ZERO |
INVALID, "value": ...} where it matters (dividends), and None-with-reason elsewhere.

Output: pit_capture_<mmm>_<yyyy>.jsonl (append-only per run; one record per population name) +
a coverage summary returned to the caller.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import time
from typing import Dict, Optional

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.1.0"          # 1.1.0 (ISA-0747): + income_stmt_ttm (additive)
PROVIDER = "yfinance"
EST_COLS = ("avg", "low", "high", "yearAgoEps", "yearAgoRevenue", "numberOfAnalysts", "growth", "currency")
PERIODS = ("0y", "+1y", "0q", "+1q")
INC_ROWS = ("EBITDA", "Normalized EBITDA", "EBIT", "Total Revenue", "Operating Revenue", "Net Income",
            "Diluted Average Shares", "Basic Average Shares")
BS_ROWS = ("Total Debt", "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments",
           "Net Debt", "Ordinary Shares Number", "Share Issued")
INFO_KEYS = ("currency", "financialCurrency", "currentPrice", "regularMarketPrice", "marketCap",
             "enterpriseValue", "ebitda", "totalRevenue", "totalDebt", "totalCash", "sharesOutstanding",
             "impliedSharesOutstanding", "floatShares", "forwardEps", "trailingEps", "forwardPE",
             "trailingPE", "enterpriseToEbitda", "lastFiscalYearEnd", "nextFiscalYearEnd",
             "mostRecentQuarter", "numberOfAnalystOpinions", "exchange", "quoteType")
SCORED_KEYS = ("expected_return_12_24m", "er_state", "er_growth", "er_rerate", "er_yield",
               "er_sharecount_sensitivity_pp",
               "er_multiple_field", "er_multiple_value", "er_anchor_operative", "er_anchor_xs",
               "er_anchor_own", "er_rerate_status", "er_growth_knee_pp", "er_growth_raw",
               "er_basis", "er_method_id", "er_horizon_months", "fwd_eps_growth",
               "fwd_eps_growth_period", "fwd_eps_growth_source", "share_count_change",
               "net_debt_ebitda", "_net_debt", "_ebitda", "_latest_rev", "fwd_pe", "ev_ebitda",
               "price_fcf", "market_cap", "current_price", "sector")
# the fields whose coverage is reported (the ISA-0722 amendment list)
COVERAGE_FIELDS = ("eps_fy0_fy1", "eps_dispersion", "eps_analysts", "rev_fy0_fy1", "rev_dispersion",
                   "rev_analysts", "fiscal_periods", "multiple_anchor_regime", "ebitda_revenue_history",
                   "ttm_ebitda_4q", "ttm_provider", "net_debt_history", "shares", "dividend_state")


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _jsonable(v):
    if isinstance(v, (str, bool)) or v is None:
        return v
    n = _num(v)
    if n is not None:
        return n
    try:
        return str(v)
    except Exception:                                                   # noqa: BLE001
        return None


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _est_table(df) -> Optional[Dict[str, dict]]:
    if df is None or not hasattr(df, "index") or getattr(df, "empty", True):
        return None
    out = {}
    for p in PERIODS:
        if p in df.index:
            out[p] = {c: _jsonable(df.loc[p, c]) for c in EST_COLS if c in df.columns}
    return out or None


def _stmt(df, rows, ncols) -> Optional[Dict[str, dict]]:
    if df is None or not hasattr(df, "columns") or getattr(df, "empty", True):
        return None
    out = {}
    for c in list(df.columns)[:ncols]:
        col = {r: _jsonable(df.loc[r, c]) for r in rows if r in df.index}
        col = {k: v for k, v in col.items() if isinstance(v, float)}
        if col:
            out[str(c)[:10]] = col
    return out or None


def _iso(ts):
    n = _num(ts)
    if n is None:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(n).date().isoformat()
    except Exception:                                                   # noqa: BLE001
        return None


def _dividend(info) -> dict:
    """Missing is DISTINCT from zero (Raj 24-Sep-2026)."""
    if "dividendRate" not in info and "trailingAnnualDividendRate" not in info:
        return {"state": "ABSENT", "value": None,
                "why": "provider returned no dividend field - unknown, NOT zero"}
    v = _num(info.get("dividendRate"))
    t = _num(info.get("trailingAnnualDividendRate"))
    val = v if v is not None else t
    if val is None:
        return {"state": "INVALID", "value": None, "raw": _jsonable(info.get("dividendRate"))}
    return {"state": "ZERO" if val == 0 else "PRESENT", "value": val,
            "basis": "dividendRate" if v is not None else "trailingAnnualDividendRate",
            "yield_raw": _num(info.get("dividendYield"))}


def _coverage(rec) -> Dict[str, bool]:
    e, r = rec.get("eps_estimate") or {}, rec.get("revenue_estimate") or {}
    def both(t, k):
        return all(_num((t.get(p) or {}).get(k)) is not None for p in ("0y", "+1y"))
    inc = rec.get("income_stmt_annual") or {}
    q = rec.get("income_stmt_quarterly") or {}
    bs = rec.get("balance_sheet_annual") or {}
    info = rec.get("info") or {}
    sc = rec.get("scored") or {}
    return {
        "eps_fy0_fy1": both(e, "avg"),
        "eps_dispersion": both(e, "low") and both(e, "high"),
        "eps_analysts": _num((e.get("+1y") or {}).get("numberOfAnalysts")) is not None,
        "rev_fy0_fy1": both(r, "avg"),
        "rev_dispersion": both(r, "low") and both(r, "high"),
        "rev_analysts": _num((r.get("+1y") or {}).get("numberOfAnalysts")) is not None,
        "fiscal_periods": bool(info.get("lastFiscalYearEnd") and info.get("nextFiscalYearEnd")),
        "multiple_anchor_regime": bool(sc.get("er_multiple_field") and sc.get("er_basis")),
        "ebitda_revenue_history": sum(1 for v in inc.values()
                                      if "EBITDA" in v and ("Total Revenue" in v or "Operating Revenue" in v)) >= 3,
        "ttm_ebitda_4q": sum(1 for v in q.values() if "EBITDA" in v) >= 4,
        # ISA-0747: the provider's direct TTM (revenue AND EBITDA on one period)
        "ttm_provider": any("EBITDA" in v and "Total Revenue" in v
                            for v in (rec.get("income_stmt_ttm") or {}).values()),
        "net_debt_history": sum(1 for v in bs.values()
                                if "Total Debt" in v or "Net Debt" in v) >= 2,
        "shares": _num(info.get("sharesOutstanding")) is not None,
        "dividend_state": (rec.get("dividend") or {}).get("state") in ("PRESENT", "ZERO"),
    }


def record(ticker: str, data: Optional[dict], *, scored: Optional[dict], meta: Optional[dict],
           identity: dict, fetch_error: Optional[str] = None) -> dict:
    base = {"schema_version": SCHEMA_VERSION, "record_kind": "PIT_CAPTURE", "ticker": ticker,
            "provider": PROVIDER, "identity": identity,
            "population_kind": (meta or {}).get("kind"),
            "source_pipeline": (meta or {}).get("source_pipeline")}
    if not data:
        base.update(state="FETCH_FAILED", why=fetch_error or "no data returned")
        base["fingerprint"] = _sha(base)
        return base
    info = data.get("info") or {}
    rec = dict(base)
    rec["captured_at"] = data.get("_fetched_at") or identity.get("captured_at")
    rec["info"] = {k: _jsonable(info.get(k)) for k in INFO_KEYS if k in info}
    for k in ("lastFiscalYearEnd", "nextFiscalYearEnd", "mostRecentQuarter"):
        if k in info:
            rec["info"][k + "_iso"] = _iso(info.get(k))
    rec["eps_estimate"] = _est_table(data.get("earnings_estimate"))
    rec["revenue_estimate"] = _est_table(data.get("revenue_estimate"))
    ge = data.get("growth_estimates")
    rec["growth_estimates"] = ({p: _jsonable(ge.loc[p, "stockTrend"]) for p in ge.index
                                if "stockTrend" in ge.columns}
                               if ge is not None and hasattr(ge, "index") and not getattr(ge, "empty", True)
                               else None)
    rec["income_stmt_annual"] = _stmt(data.get("income_statement_raw") or data.get("income_stmt"), INC_ROWS, 5)
    rec["income_stmt_quarterly"] = _stmt(data.get("quarterly_income_stmt"), INC_ROWS, 5)
    rec["income_stmt_ttm"] = _stmt(data.get("ttm_income_stmt"), INC_ROWS, 1)       # ISA-0747
    rec["balance_sheet_annual"] = _stmt(data.get("balance_sheet"), BS_ROWS, 5)
    rec["dividend"] = _dividend(info)
    rec["scored"] = {k: _jsonable((scored or {}).get(k)) for k in SCORED_KEYS if (scored or {}).get(k) is not None}
    rec["table_fingerprints"] = {k: _sha(rec.get(k)) for k in
                                 ("eps_estimate", "revenue_estimate", "growth_estimates",
                                  "income_stmt_annual", "income_stmt_quarterly", "income_stmt_ttm",
                                  "balance_sheet_annual", "info")}
    rec["coverage"] = _coverage(rec)
    rec["state"] = "CAPTURED"
    rec["fingerprint"] = _sha({k: v for k, v in rec.items() if k != "identity"})
    return rec


def run_identity(root: str, month_label: str) -> dict:
    ident = {"month_label": month_label,
             "captured_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    try:
        with open(os.path.join(root, "Dashboard", "state", "trusted_build.json"), encoding="utf-8") as fh:
            ident["build_id"] = (json.load(fh) or {}).get("build_id")
    except Exception:                                                   # noqa: BLE001
        ident["build_id"] = None
    for f in ("scoring_config.py", "expected_return.py", "screener_core.py"):
        try:
            with open(os.path.join(root, f), "rb") as fh:
                ident[f.replace(".py", "") + "_sha"] = hashlib.sha256(fh.read()).hexdigest()[:12]
        except Exception:                                               # noqa: BLE001
            ident[f.replace(".py", "") + "_sha"] = None
    try:
        import yfinance
        ident["provider_version"] = getattr(yfinance, "__version__", None)
    except Exception:                                                   # noqa: BLE001
        ident["provider_version"] = None
    ident["schema_version"] = SCHEMA_VERSION
    return ident


def capture(*, raw_data: dict, fetch_errors: dict, population: list, ticker_meta: dict,
            scored_results: dict, month_label: str, root: Optional[str] = None,
            out_path: Optional[str] = None, dry_run: bool = False) -> dict:
    """ONE capture over the pre-run's full fetched population. Never raises."""
    _fi_mark("pit_capture", "capture")
    t0 = time.time()
    root = root or HERE
    out_path = out_path or os.path.join(root, "pit_capture_%s.jsonl" % month_label)
    try:
        ident = run_identity(root, month_label)
        recs, errors = [], {}
        for t in population:
            try:
                recs.append(record(t, raw_data.get(t), scored=scored_results.get(t),
                                   meta=ticker_meta.get(t), identity=ident,
                                   fetch_error=fetch_errors.get(t)))
            except Exception as exc:                                    # noqa: BLE001
                errors[t] = "%s: %s" % (type(exc).__name__, exc)
                recs.append({"schema_version": SCHEMA_VERSION, "record_kind": "PIT_CAPTURE",
                             "ticker": t, "identity": ident, "state": "CAPTURE_FAILED",
                             "why": errors[t]})
        written = 0
        if not dry_run:
            with open(out_path, "a", encoding="utf-8") as fh:
                for r in recs:
                    fh.write(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) + "\n")
                    written += 1
        cap = [r for r in recs if r.get("state") == "CAPTURED"]
        cov = {f: sum(1 for r in cap if (r.get("coverage") or {}).get(f)) for f in COVERAGE_FIELDS}
        div = {}
        for r in cap:
            s = (r.get("dividend") or {}).get("state")
            div[s] = div.get(s, 0) + 1
        return {"state": "OK", "mode": "CAPTURE_ONLY", "identity": ident,
                "n_population": len(population), "n_captured": len(cap),
                "n_fetch_failed": sum(1 for r in recs if r.get("state") == "FETCH_FAILED"),
                "n_capture_failed": len(errors), "capture_errors": dict(list(errors.items())[:20]),
                "coverage": cov, "dividend_states": div, "written": written,
                "out_path": os.path.basename(out_path), "secs": round(time.time() - t0, 2),
                "incremental_provider_calls": 0,
                "basis": ("ISA-0722: full pre-run population, riding the existing fetch; "
                          "revenue_estimate served from the cached earningsTrend payload")}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc),
                "secs": round(time.time() - t0, 2), "mode": "CAPTURE_ONLY"}


# ─────────────────────────────────────────────────────────── selftest ─────────────

def _selftest(verbose: bool = True) -> int:
    import tempfile
    import pandas as pd
    fails = []

    def ok(name, cond, detail=""):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- %s" % detail))

    ee = pd.DataFrame({"avg": [5.0, 6.0], "low": [4.8, 5.5], "high": [5.2, 6.6],
                       "numberOfAnalysts": [9, 8], "currency": ["USD", "USD"]}, index=["0y", "+1y"])
    re_ = pd.DataFrame({"avg": [100.0, 110.0], "low": [98.0, 105.0], "high": [103.0, 116.0],
                        "numberOfAnalysts": [7, 6]}, index=["0y", "+1y"])
    cols = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])
    inc = pd.DataFrame([[20.0, 18, 16, 15], [100.0, 90, 80, 75]], index=["EBITDA", "Total Revenue"], columns=cols)
    qc = pd.to_datetime(["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
    qinc = pd.DataFrame([[5.0, 5, 5, 5], [25.0, 25, 25, 25]], index=["EBITDA", "Total Revenue"], columns=qc)
    bs = pd.DataFrame([[50.0, 55, 60], [10.0, 12, 9]], index=["Total Debt", "Cash And Cash Equivalents"], columns=cols[:3])
    info = {"currency": "USD", "sharesOutstanding": 1e8, "lastFiscalYearEnd": 1766880000,
            "nextFiscalYearEnd": 1798416000, "dividendRate": 0.0}
    data = {"info": info, "earnings_estimate": ee, "revenue_estimate": re_, "income_stmt": inc,
            "quarterly_income_stmt": qinc, "balance_sheet": bs, "growth_estimates": None,
            "ttm_income_stmt": pd.DataFrame([[20.0], [100.0]], index=["EBITDA", "Total Revenue"],
                                            columns=pd.to_datetime(["2026-06-30"]))}
    scored = {"er_multiple_field": "fwd_pe", "er_basis": "rerate=x|regime=RISK_ON", "expected_return_12_24m": 20.0}
    ident = {"month_label": "test", "build_id": "TB-T"}
    r = record("TST", data, scored=scored, meta={"kind": "candidate_pool"}, identity=ident)
    ok("CAPTURED with full coverage on a complete fixture",
       r["state"] == "CAPTURED" and all(r["coverage"].values()), r.get("coverage"))
    ok("dividend 0.0 is ZERO, not ABSENT", r["dividend"]["state"] == "ZERO", r["dividend"])
    ok("ISA-0747: the provider TTM statement is captured on one period with its end date",
       r["income_stmt_ttm"] == {"2026-06-30": {"EBITDA": 20.0, "Total Revenue": 100.0}})
    ok("ISA-0747 MUST-FIRE: no provider TTM -> coverage ttm_provider False (not silently True)",
       record("TST", dict(data, ttm_income_stmt=None), scored=scored, meta={}, identity=ident)["coverage"]["ttm_provider"] is False)
    r2 = record("TST", dict(data, info={k: v for k, v in info.items() if k != "dividendRate"}),
                scored=scored, meta={}, identity=ident)
    ok("MUST-FIRE: a missing dividend field is ABSENT (unknown), never zero",
       r2["dividend"]["state"] == "ABSENT" and r2["coverage"]["dividend_state"] is False, r2["dividend"])
    r3 = record("TST", dict(data, revenue_estimate=None), scored=scored, meta={}, identity=ident)
    ok("MUST-FIRE: absent revenue estimates are reported uncovered, not invented",
       r3["revenue_estimate"] is None and r3["coverage"]["rev_fy0_fy1"] is False)
    ok("NEGATIVE CONTROL: EPS coverage unaffected by the missing revenue table",
       r3["coverage"]["eps_fy0_fy1"] is True)
    r4 = record("BAD", None, scored=None, meta={}, identity=ident, fetch_error="timeout")
    ok("MUST-FIRE: a fetch failure is a recorded FETCH_FAILED row", r4["state"] == "FETCH_FAILED")
    r5 = record("TST", dict(data, quarterly_income_stmt=qinc.iloc[:, :2]), scored=scored, meta={}, identity=ident)
    ok("MUST-FIRE: fewer than 4 quarters -> ttm_ebitda_4q False", r5["coverage"]["ttm_ebitda_4q"] is False)
    ok("fingerprint is deterministic for identical content",
       record("TST", data, scored=scored, meta={"kind": "candidate_pool"}, identity=ident)["fingerprint"] == r["fingerprint"])
    with tempfile.TemporaryDirectory() as d:
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")
        res = capture(raw_data={"TST": data, "BAD": None}, fetch_errors={"BAD": "timeout"},
                      population=["TST", "BAD", "UGLY"], ticker_meta={}, scored_results={"TST": scored},
                      month_label="t", root=d, out_path=os.path.join(d, "x.jsonl"))
        ok("capture over a mixed population: 1 captured, 2 fetch-failed, file written",
           res["state"] == "OK" and res["n_captured"] == 1 and res["n_fetch_failed"] == 2
           and res["written"] == 3, res)
        res2 = capture(raw_data=Boom(), fetch_errors={}, population=["TST"], ticker_meta={},
                       scored_results={}, month_label="t", root=d, out_path=os.path.join(d, "y.jsonl"))
        ok("FAIL-SOFT: a per-ticker exception is a CAPTURE_FAILED row, never raised",
           res2["state"] == "OK" and res2["n_capture_failed"] == 1, res2)
        res3 = capture(raw_data={}, fetch_errors={}, population=None, ticker_meta={},
                       scored_results={}, month_label="t", root=d)
        ok("FAIL-SOFT: a whole-capture failure returns state FAILED, never raises",
           res3["state"] == "FAILED", res3)
        res4 = capture(raw_data={"TST": data}, fetch_errors={}, population=["TST"], ticker_meta={},
                       scored_results={}, month_label="t", root=d, out_path=os.path.join(d, "z.jsonl"),
                       dry_run=True)
        ok("NEGATIVE CONTROL: dry run writes nothing", res4["written"] == 0
           and not os.path.exists(os.path.join(d, "z.jsonl")))
    print("pit_capture selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
