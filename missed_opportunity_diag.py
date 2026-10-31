#!/usr/bin/env python3
"""
missed_opportunity_diag.py — Missed-Opportunity Attribution (MOA). Dashboard spec §7.2.

WHY THIS EXISTS
---------------
Every other instrument in this framework measures the names that were BOUGHT. The paper books
grade the top of the list; the decision ledger records what was decided. Nothing measures the
names the framework REJECTED — and a gate's cost lives entirely in its false negatives. MOA is
the only instrument pointed at that population.

FOUR QUESTIONS, each answered from data the run already emits:
  entry_crossings       a name entered its own entry band and no decision was recorded
  unactioned_top_ranks  a name sat at the top of the deployment stack and was never bought
  closed_decision_review  what a closed decision actually returned, versus what was expected
  rule_frictions        which GATE repeatedly rejected names that subsequently performed

DISCIPLINE
----------
* Outcome NEVER classifies decision quality on its own. The process axis is human, entered on
  the dashboard and stored separately, so regenerating this file can never destroy a judgement.
* Report top-N excess, hit rate and dispersion — NOT pooled IC. A handful of names a month
  cannot support a correlation coefficient, and quoting one invites false precision.
* Missing inputs are NAMED in `missing_inputs`, never silently treated as zero.
* This module reads. It writes one JSON. It feeds no score, gate, rank or threshold.

CLI:
  python3 missed_opportunity_diag.py --month aug_2026
  python3 missed_opportunity_diag.py --month aug_2026 --no-fetch     (cache only)
  python3 missed_opportunity_diag.py --selftest
"""
from __future__ import annotations
import argparse, csv, json, os, sys
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1
PRICE_CACHE = os.path.join(HERE, "moa_prices.json")
JUDGEMENTS = os.path.join(HERE, "Dashboard", "state", "process_judgements.json")
BENCHMARKS = {"vuag": "VUAG.L", "iwmo": "IWMO.L", "cash": "CSH2.L"}
DEFAULT_TOP_N = 10
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


# ── io ───────────────────────────────────────────────────────────────────────────────────
def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _month_start(month_label):
    m, y = month_label.split("_")
    return date(int(y), MONTHS.index(m.lower()) + 1, 1)


# ── prices ───────────────────────────────────────────────────────────────────────────────
def fetch_history(tickers, start, end, fetch=True, cache_path=PRICE_CACHE):
    """Resumable, cached. A ticker that cannot be priced is recorded as an error, never
    silently dropped — a missing price must not read as a missing opportunity."""
    cache = _load(cache_path, {}) or {}
    key = lambda t: f"{t}|{start}|{end}"
    want = [t for t in tickers if key(t) not in cache]
    if want and fetch:
        try:
            import yfinance as yf
            for i in range(0, len(want), 40):
                chunk = want[i:i + 40]
                d = yf.download(chunk, start=start, end=end, progress=False,
                                auto_adjust=True, threads=True)
                closes = d["Close"] if hasattr(d, "columns") and "Close" in getattr(
                    d.columns, "get_level_values", lambda _: [])(0) else d
                for t in chunk:
                    try:
                        s = closes[t] if t in getattr(closes, "columns", []) else closes
                        s = s.dropna()
                        cache[key(t)] = ({"first": float(s.iloc[0]), "last": float(s.iloc[-1]),
                                          "min": float(s.min()), "max": float(s.max()),
                                          "n": int(len(s))} if len(s) else {"error": "no rows"})
                    except Exception as e:
                        cache[key(t)] = {"error": str(e)[:120]}
        except ImportError:
            for t in want:
                cache[key(t)] = {"error": "yfinance unavailable"}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    return {t: cache.get(key(t), {"error": "not fetched"}) for t in tickers}


def _ret(d):
    if not d or "error" in d or not d.get("first"):
        return None
    return d["last"] / d["first"] - 1.0


# ── the four questions ───────────────────────────────────────────────────────────────────
def entry_crossings(entry_audit, ledger_tickers, px, missing):
    """A name whose price traded INTO its own entry band with no decision recorded."""
    out = []
    for e in (entry_audit.get("entries") or []):
        t = e.get("ticker")
        lo, hi = e.get("entry_band_low"), e.get("entry_band_high")
        d = px.get(t) or {}
        if lo is None or hi is None:
            missing.setdefault("no_entry_band", []).append(t)
            continue
        if "error" in d or d.get("min") is None:
            missing.setdefault("no_price_history", []).append(t)
            continue
        crossed = d["min"] <= hi          # traded at or below the top of the band
        if not crossed or t in ledger_tickers:
            continue
        out.append({
            "ticker": t, "entry_band_low": lo, "entry_band_high": hi,
            "period_low": d["min"], "period_last": d["last"],
            "return_over_window": round(_ret(d), 4) if _ret(d) is not None else None,
            "entry_level_confidence": e.get("entry_level_confidence"),
            "disposition": e.get("disposition"),
            "note": "price entered the entry band; no decision-ledger entry exists",
        })
    out.sort(key=lambda r: -(r["return_over_window"] or -9))
    return out


def unactioned_top_ranks(step9, ledger_tickers, px, top_n, missing):
    """Top deployment ranks that were never bought, and what they went on to do."""
    rows = (step9.get("deployable_stack")
            or step9.get("deployment_priority_rank") or [])[:top_n]
    bench = {k: _ret(px.get(v) or {}) for k, v in BENCHMARKS.items()}
    out = []
    for r in rows:
        t = r.get("ticker")
        if t in ledger_tickers:
            continue
        d = px.get(t) or {}
        rr = _ret(d)
        if rr is None:
            missing.setdefault("no_price_history", []).append(t)
        out.append({
            "ticker": t,
            "deployment_rank": r.get("deployable_rank") or r.get("deployment_rank"),
            "tier": r.get("tier"), "source_score": r.get("source_score"),
            "rank_basis": r.get("rank_basis"),
            "return_since": round(rr, 4) if rr is not None else None,
            "vs_vuag": (round(rr - bench["vuag"], 4)
                        if rr is not None and bench.get("vuag") is not None else None),
            "vs_iwmo": (round(rr - bench["iwmo"], 4)
                        if rr is not None and bench.get("iwmo") is not None else None),
            "vs_cash": (round(rr - bench["cash"], 4)
                        if rr is not None and bench.get("cash") is not None else None),
        })
    return out, bench


def closed_decision_review(ledger, judgements, px):
    """Realised versus expected on decisions that are closed, with the human process axis
    joined in. The quadrant is only assigned once BOTH axes exist."""
    out = []
    for e in (ledger.get("entries") or []):
        dec = str(e.get("decision") or "").upper()
        t = e.get("ticker")
        rr = _ret(px.get(t) or {})
        pj = (judgements.get(e.get("_id")) or judgements.get(t) or {}).get("process_judgement")
        # 02-Aug-2026: the outcome axis must be INVERTED for an exit. `rr` is the return since
        # the decision date, so for a BUY a rise is a good outcome — but for a SELL or TRIM a
        # rise is the cost of having exited. Scoring both the same way would have graded every
        # well-timed exit as a failure and every mistimed one as a success.
        _EXITS = {"SELL", "TRIM", "REDUCE", "EXIT", "CLOSE"}
        is_exit = dec in _EXITS
        if rr is None:
            outcome = None
        elif is_exit:
            outcome = "good" if rr < 0 else "poor"      # avoided a fall = good exit
        else:
            outcome = "good" if rr > 0 else "poor"
        out.append({
            "id": e.get("_id"), "ticker": t, "date": e.get("date"), "decision": dec,
            "expected_review_date": e.get("expected_review_date"),
            "scores_at_decision": e.get("scores_at_decision"),
            "realised_return_since": round(rr, 4) if rr is not None else None,
            "is_exit": is_exit,
            "outcome_axis": outcome,
            "outcome_note": ("exit: a RISE after the decision is the cost of exiting"
                             if is_exit else
                             "entry/hold: a RISE after the decision is a good outcome"),
            "process_judgement": pj,
            # Outcome alone never grades a decision. Without the human axis this stays null.
            "quadrant": (f"process_{pj}/outcome_{outcome}"
                         if pj and outcome else None),
        })
    return out


def rule_frictions(gate_csv, px, min_hits=2):
    """Which gate repeatedly rejected names that subsequently performed. EVIDENCE ONLY."""
    try:
        with open(gate_csv, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return [], {"gate_variables": "unreadable"}
    agg = {}
    for r in rows:
        if str(r.get("passed")).lower() in ("true", "1"):
            continue
        code = (r.get("gate_code") or "").strip() or "UNSPECIFIED"
        rr = _ret(px.get(r.get("ticker")) or {})
        a = agg.setdefault(code, {"gate_code": code, "rejected": 0, "priced": 0,
                                  "positive_after": 0, "returns": [], "instances": []})
        a["rejected"] += 1
        if rr is not None:
            a["priced"] += 1
            a["returns"].append(rr)
            if rr > 0:
                a["positive_after"] += 1
            a["instances"].append({"ticker": r.get("ticker"), "run_date": r.get("run_date"),
                                   "return_since": round(rr, 4),
                                   "reason": (r.get("gate_reason") or "")[:120]})
    out = []
    for code, a in agg.items():
        if a["priced"] < min_hits:
            continue
        rs = sorted(a["returns"], reverse=True)
        out.append({
            "gate_code": code, "rejected": a["rejected"], "priced": a["priced"],
            "hit_rate_positive": round(a["positive_after"] / a["priced"], 3),
            "median_return_after": round(rs[len(rs) // 2], 4),
            "top5_mean_return_after": round(sum(rs[:5]) / min(5, len(rs)), 4),
            "instances": sorted(a["instances"], key=lambda x: -x["return_since"])[:8],
            "note": "EVIDENCE ONLY — a gate is not wrong because a rejected name rose",
        })
    out.sort(key=lambda r: -r["top5_mean_return_after"])
    return out, {}


def _month_inputs(month_label, here):
    """ISA-0832: MOA is retrospective by construction, so its month has usually been archived by the
    post-review purge. Resolve through the one home; an absent month stays {} (and is reported)."""
    import month_artefacts as _MA
    return (_load(_MA.resolve(here, "step9_pre_%s.json" % month_label), {}) or {},
            _load(_MA.resolve(here, "entry_level_audit_%s.json" % month_label), {}) or {})


# ── build ────────────────────────────────────────────────────────────────────────────────
# ── P-D (ISA-0891, 24-Oct-2026): MISSED-OPPORTUNITY RECORDING - the closed loop over the engine's FROZEN dispositions ──
# To be inserted into missed_opportunity_diag.py (one home for MOA).
#
# capital_decision_ledger.jsonl already freezes, at decision time, EVERY universe name's disposition (SELECTED,
# HARD_BLOCKED, RISK_BLOCKED, ECONOMICS_FAILED, CASE_PENDING, NOT_IN_SCOPE, IDENTITY_REFUSED ...) with
# outcome_horizons_months [3, 6, 12] - and NOTHING read it back. This is the reader: each ledger row's names are
# joined to the weekly GBP total-return store on the decision Friday and the horizon Friday; a horizon is MATURED only
# when the store holds the horizon Friday (never extrapolated). Results are grouped by disposition so the question
# "what did the names we did NOT buy go on to do?" has an answer per gate, beside the broad-equity benchmark.
# DOCTRINE (unchanged): outcome never classifies decision quality by itself; nothing here feeds a score, gate or rank.
import datetime as _dt_pd


def _friday_on_or_before(d):
    return d - _dt_pd.timedelta(days=(d.weekday() - 4) % 7)


def _add_months_pd(d, k):
    y, m = divmod(d.month - 1 + k, 12)
    import calendar as _cal
    return _dt_pd.date(d.year + y, m + 1, min(d.day, _cal.monthrange(d.year + y, m + 1)[1]))


def _level_on(levels, fri, back=1):
    """GBP level on the Friday `fri` or, if that week is absent, up to `back` earlier Friday(s) (never later)."""
    for k in range(back + 1):
        v = levels.get((fri - _dt_pd.timedelta(days=7 * k)).isoformat())
        if v is not None:
            return v
    return None


def disposition_outcomes(ledger_rows, store, as_of, benchmark_levels=None, horizons=(3, 6, 12)):
    """-> {state, rows_read, by_horizon: {h: {matured_rows, by_disposition: {disp: {n, priced, mean_pct, median_pct,
    hit_rate_vs_benchmark}}, benchmark_mean_pct}}, next_maturity, per_name}. Pure given its inputs."""
    import statistics as _st
    import sleeve_risk as _sr
    last = max((d for n in (store.get("names") or {}).values() for d in (n.get("observations") or {})), default=None)
    last = _dt_pd.date.fromisoformat(last) if last else None
    out = {"state": "OK", "rows_read": 0, "store_last_friday": last and last.isoformat(), "by_horizon": {},
           "next_maturity": None, "per_name": [], "doctrine": "outcome alone never grades a decision (MOA doctrine)"}
    nxt = None
    agg = {h: {} for h in horizons}
    bench = {h: [] for h in horizons}
    seen = set()
    for row in ledger_rows or []:
        if not row.get("as_of") or not row.get("dispositions"):
            continue
        key = (row.get("month"), row.get("as_of"), row.get("receipt_id"))
        if key in seen:
            continue
        seen.add(key)
        out["rows_read"] += 1
        d0 = _friday_on_or_before(_dt_pd.date.fromisoformat(str(row["as_of"])[:10]))
        for h in horizons:
            hf = _friday_on_or_before(_add_months_pd(d0, h))
            if last is None or hf > last:
                nxt = hf if nxt is None or hf < nxt else nxt
                continue
            if benchmark_levels:
                b0, b1 = _level_on(benchmark_levels, d0), _level_on(benchmark_levels, hf)
                b = (b1 / b0 - 1.0) if (b0 and b1) else None
            else:
                b = None
            if b is not None:
                bench[h].append(b)
            for t, disp in (row.get("dispositions") or {}).items():
                cell = agg[h].setdefault(disp, {"n": 0, "priced": [], "hits": 0, "hit_n": 0})
                cell["n"] += 1
                try:
                    lv = _sr._levels(store, t)
                except Exception:                                       # noqa: BLE001
                    lv = {}
                v0, v1 = _level_on(lv, d0), _level_on(lv, hf)
                if not (v0 and v1):
                    continue
                r = v1 / v0 - 1.0
                cell["priced"].append(r)
                if b is not None:
                    cell["hit_n"] += 1
                    cell["hits"] += int(r > b)
                out["per_name"].append({"ticker": t, "disposition": disp, "decided": d0.isoformat(), "horizon_m": h,
                                        "return_pct": round(100 * r, 2),
                                        "vs_benchmark_pp": None if b is None else round(100 * (r - b), 2)})
    for h in horizons:
        rows = {}
        for disp, c in agg[h].items():
            p = c["priced"]
            rows[disp] = {"n": c["n"], "priced": len(p),
                          "mean_pct": round(100 * sum(p) / len(p), 2) if p else None,
                          "median_pct": round(100 * _st.median(p), 2) if p else None,
                          "hit_rate_vs_benchmark": round(c["hits"] / c["hit_n"], 3) if c["hit_n"] else None}
        out["by_horizon"][str(h)] = {"by_disposition": rows,
                                     "benchmark_mean_pct": round(100 * sum(bench[h]) / len(bench[h]), 2) if bench[h] else None,
                                     "matured": bool(rows)}
    out["next_maturity"] = nxt and nxt.isoformat()
    n_mat = sum(1 for h in horizons if out["by_horizon"][str(h)]["matured"])
    out["line"] = ("Missed-opportunity record: %d frozen decision(s); %s" %
                   (out["rows_read"], ("%d horizon(s) matured" % n_mat) if n_mat else
                    "no horizon matured yet - first 3-month read %s" % (out["next_maturity"] or "UNKNOWN")))
    return out


def _selftest_disposition_outcomes(ok):
    import datetime as _d
    fr = [_d.date(2026, 1, 2) + _d.timedelta(days=7 * i) for i in range(40)]
    store = {"names": {"WIN": {"observations": {f.isoformat(): {"gbp": 100 * (1.01 ** i)} for i, f in enumerate(fr)}},
                       "LOSE": {"observations": {f.isoformat(): {"gbp": 100 * (0.99 ** i)} for i, f in enumerate(fr)}}}}
    rows = [{"month": "jan_2026", "as_of": "2026-01-09", "receipt_id": "R1",
             "dispositions": {"WIN": "HARD_BLOCKED", "LOSE": "SELECTED", "GHOST": "CASE_PENDING"}}]
    bl = {f.isoformat(): 100.0 for f in fr}
    o = disposition_outcomes(rows, store, _d.date(2026, 10, 1), benchmark_levels=bl)
    h3 = o["by_horizon"]["3"]["by_disposition"]
    ok("MOA-D: a blocked name that rose is recorded against its gate (3m)", h3["HARD_BLOCKED"]["mean_pct"] > 10)
    ok("MOA-D: hit rate vs benchmark is measured", h3["HARD_BLOCKED"]["hit_rate_vs_benchmark"] == 1.0)
    ok("MOA-D negative control: an unpriced name is counted but never priced as zero",
       h3["CASE_PENDING"]["n"] == 1 and h3["CASE_PENDING"]["priced"] == 0 and h3["CASE_PENDING"]["mean_pct"] is None)
    ok("MOA-D negative control: a horizon beyond the store is NOT matured (12m from Jan-2026 on a Sep-2026 store)",
       not o["by_horizon"]["12"]["matured"] and o["next_maturity"] is not None)


def _disposition_outcomes_for(here, asof):
    """Read the engine's frozen dispositions + the weekly GBP store + VUAG weekly levels; never raises."""
    try:
        rows = []
        p = os.path.join(here, "capital_decision_ledger.jsonl")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        store = _load(os.path.join(here, "stock_weekly_returns.json"), {}) or {}
        bl = None
        try:
            import fund_performance as _fp
            import whole_book_risk as _wbr
            bl = _wbr.fund_weekly_levels(_fp.nav_series_for("VUAG", _fp.load_universe().get("VUAG")))
        except Exception:                                               # noqa: BLE001
            bl = None
        return disposition_outcomes(rows, store, asof, benchmark_levels=bl)
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}


def build(month_label, here=None, fetch=True, top_n=DEFAULT_TOP_N, asof=None):
    here = here or HERE
    asof = asof or date.today()
    start = _month_start(month_label)
    step9, audit = _month_inputs(month_label, here)
    ledger = _load(os.path.join(here, "decision_ledger.json"), {}) or {}
    judge = _load(JUDGEMENTS, {}) or {}

    ledger_acted = {e.get("ticker") for e in (ledger.get("entries") or [])
                    if str(e.get("decision") or "").upper() in ("BUY", "ADD", "TOP_UP")}
    universe = set()
    universe |= {e.get("ticker") for e in (audit.get("entries") or []) if e.get("ticker")}
    universe |= {r.get("ticker") for r in (step9.get("deployment_priority_rank") or [])}
    universe |= {e.get("ticker") for e in (ledger.get("entries") or []) if e.get("ticker")}
    universe = {t for t in universe if t}

    px = fetch_history(sorted(universe) + list(BENCHMARKS.values()),
                       start.isoformat(), asof.isoformat(), fetch=fetch)

    missing = {}
    xs = entry_crossings(audit, ledger_acted, px, missing)
    tops, bench = unactioned_top_ranks(step9, ledger_acted, px, top_n, missing)
    closed = closed_decision_review(ledger, judge, px)
    fric, fmiss = rule_frictions(os.path.join(here, "gate_variables.csv"), px)
    missing.update(fmiss)

    priced = [r["return_since"] for r in tops if r["return_since"] is not None]
    # MOA is retrospective by construction. Run for the CURRENT month on its own run date the
    # forward window is a day wide and every number is 0.0 — which reads exactly like "no
    # opportunity was missed". Say so instead.
    window_days = (asof - start).days
    window_warning = (None if window_days >= 30 else
                      f"window is {window_days} day(s): too short to measure a missed "
                      f"opportunity. Every return here will be near zero because no time has "
                      f"passed, NOT because nothing was missed. Run this for a PRIOR month.")
    return {
        "window_days": window_days,
        "window_warning": window_warning,
        # P-D / ISA-0891: the engine's frozen dispositions read back at 3/6/12 months (the closed loop)
        "disposition_outcomes": _disposition_outcomes_for(here, asof),
        "schema_version": SCHEMA_VERSION,
        "month": month_label,
        "window": {"from": start.isoformat(), "to": asof.isoformat()},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmarks": {k: (round(v, 4) if v is not None else None) for k, v in bench.items()},
        "entry_crossings": xs,
        "unactioned_top_ranks": tops,
        "unactioned_summary": {
            "n": len(tops), "priced": len(priced),
            "hit_rate_positive": (round(sum(1 for r in priced if r > 0) / len(priced), 3)
                                  if priced else None),
            "top5_mean_excess_vs_vuag": (
                round(sum(sorted([r["vs_vuag"] for r in tops if r["vs_vuag"] is not None],
                                 reverse=True)[:5]) / min(5, len(priced)), 4)
                if priced and any(r["vs_vuag"] is not None for r in tops) else None),
            "dispersion": (round(max(priced) - min(priced), 4) if len(priced) > 1 else None),
        },
        "closed_decision_review": closed,
        "rule_frictions": fric,
        "process_judgements_source": os.path.relpath(JUDGEMENTS, here),
        "missing_inputs": {k: sorted(set(v)) if isinstance(v, list) else v
                           for k, v in missing.items()},
        "doctrine": ("Outcome never classifies decision quality on its own; the process axis is "
                     "human and stored separately. Nothing here feeds a score, gate or ranking."),
    }


def _selftest():
    fails = []

    def ok(l, c):
        print(f"  {'PASS' if c else 'FAIL'}  {l}")
        if not c:
            fails.append(l)

    px = {"AAA": {"first": 10.0, "last": 13.0, "min": 9.0, "max": 14.0, "n": 5},
          "BBB": {"error": "no rows"},
          "VUAG.L": {"first": 100.0, "last": 105.0, "min": 99.0, "max": 106.0, "n": 5},
          "IWMO.L": {"first": 100.0, "last": 102.0, "min": 99.0, "max": 103.0, "n": 5},
          "CSH2.L": {"first": 100.0, "last": 100.4, "min": 100.0, "max": 100.5, "n": 5}}
    miss = {}
    xs = entry_crossings({"entries": [
        {"ticker": "AAA", "entry_band_low": 9.5, "entry_band_high": 10.5},
        {"ticker": "BBB", "entry_band_low": 1, "entry_band_high": 2},
        {"ticker": "CCC"}]}, set(), px, miss)
    ok("a crossing with no ledger action is reported", [r["ticker"] for r in xs] == ["AAA"])
    ok("an unpriced name is recorded as missing, not as a non-crossing",
       "BBB" in miss.get("no_price_history", []))
    ok("a name with no band is recorded as missing", "CCC" in miss.get("no_entry_band", []))
    xs2 = entry_crossings({"entries": [{"ticker": "AAA", "entry_band_low": 9.5,
                                        "entry_band_high": 10.5}]}, {"AAA"}, px, {})
    ok("a name that WAS bought is not a missed opportunity", xs2 == [])

    tops, bench = unactioned_top_ranks(
        {"deployable_stack": [{"ticker": "AAA", "deployable_rank": 1, "source_score": 70}]},
        set(), px, 10, {})
    ok("excess is measured against VUAG", abs(tops[0]["vs_vuag"] - (0.30 - 0.05)) < 1e-9)
    ok("cash is a benchmark (idle capital has an opportunity cost)",
       tops[0]["vs_cash"] is not None)

    cl = closed_decision_review({"entries": [{"_id": "x", "ticker": "AAA", "decision": "BUY"}]},
                                {}, px)
    ok("outcome alone leaves the quadrant NULL", cl[0]["quadrant"] is None
       and cl[0]["outcome_axis"] == "good")
    cl2 = closed_decision_review({"entries": [{"_id": "x", "ticker": "AAA", "decision": "BUY"}]},
                                 {"x": {"process_judgement": "poor"}}, px)
    ok("the quadrant forms only once the human axis exists",
       cl2[0]["quadrant"] == "process_poor/outcome_good")

    # AAA rose 30% after the decision date.
    sell = closed_decision_review({"entries": [{"_id": "s", "ticker": "AAA",
                                                "decision": "SELL"}]}, {}, px)
    ok("a rise after a SELL is a POOR outcome, not a good one",
       sell[0]["outcome_axis"] == "poor" and sell[0]["is_exit"])
    px_fall = dict(px, BBB={"first": 10.0, "last": 7.0, "min": 6.5, "max": 10.5, "n": 5})
    sell2 = closed_decision_review({"entries": [{"_id": "s2", "ticker": "BBB",
                                                 "decision": "TRIM"}]}, {}, px_fall)
    ok("a fall after a TRIM is a GOOD outcome (the fall was avoided)",
       sell2[0]["outcome_axis"] == "good")

    # ISA-0832: the retrospective month is read from the archive after the post-review purge
    import tempfile as _tf, shutil as _sh, month_artefacts as _MA
    _d = _tf.mkdtemp(prefix="moa_0832_")
    try:
        os.makedirs(_MA.archive_dir(_d))
        json.dump({"m": "archived"}, open(os.path.join(_MA.archive_dir(_d), "step9_pre_zzz_2099.json"), "w"))
        json.dump({"m": "audit"}, open(os.path.join(_MA.archive_dir(_d), "entry_level_audit_zzz_2099.json"), "w"))
        s9, au = _month_inputs("zzz_2099", _d)
        ok("ISA-0832 MUST-FIRE: a purged (archived) month's step9_pre and audit are still read", s9 == {"m": "archived"} and au == {"m": "audit"})
        ok("ISA-0832 NEGATIVE CONTROL: a month in neither place reads as {} - absence is never invented",
           _month_inputs("yyy_2098", _d) == ({}, {}))
    finally:
        _sh.rmtree(_d, ignore_errors=True)

    _selftest_disposition_outcomes(ok)
    print("\n" + ("MOA SELFTEST PASS" if not fails else f"MOA SELFTEST FAIL {fails}"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.month:
        ap.error("--month required")
    doc = build(a.month, fetch=not a.no_fetch, top_n=a.top_n)
    out = os.path.join(HERE, f"missed_opportunity_{a.month}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    if doc.get("window_warning"):
        print(f"  WARNING: {doc['window_warning']}")
    s = doc["unactioned_summary"]
    print(f"MOA {a.month}: entry_crossings={len(doc['entry_crossings'])} "
          f"unactioned={s['n']} (priced {s['priced']}, hit_rate {s['hit_rate_positive']}) "
          f"frictions={len(doc['rule_frictions'])} -> {os.path.basename(out)}")
    if doc["missing_inputs"]:
        print(f"  missing_inputs: { {k: len(v) if isinstance(v, list) else v for k, v in doc['missing_inputs'].items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
