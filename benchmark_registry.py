#!/usr/bin/env python3
"""benchmark_registry.py — ONE HOME for "which series do we measure this fund against, and is
that series fit to be measured against". Built 13-Aug-2026 for ISA-0320.

⚑ WHY THIS MODULE EXISTS AT ALL. `beta_alpha_study.MAP` held twelve benchmarks chosen by me, with
no record of what any fund's own prospectus names, and a second home for the same fact in a stored
artefact that could go stale against it. R6.1 says one golden source per decision-grade input; the
comparator is decision-grade — it sets alpha, which feeds M*, the FRS risk-adjusted component and
the T4 mandate-breach trigger. MAP is retired. `fund_universe.mandate_benchmark` is the source and
this module is the only reader.

⚑ AND THE SERIES ITSELF IS AN INPUT THAT CAN BE WRONG. Choosing the right index is not enough if
the price history for it is defective. Two instruments here, both empirical, both cheap:

  TWIN TEST — an independent series for the same (or near-identical) index must annualise the
  same. Run over 27 candidate series on 13-Aug-2026 it failed IWRD.L (short 1.90pp/yr of dividends
  against SWDA.L) and IJPN.L (short 1.91pp/yr against SJPA.L and VJPN.L). Both are total-return
  ETFs whose retrieved history is missing distributions, so each reads as a price index without
  saying so. IJPN.L is the tracker for MSCI Japan — the mandate index of a fund we hold. Adopting
  it would have booked +1.75pp of alpha on an index tracker.

  SPIKE SCAN — a print that jumps >25% and reverses >20% the next month is a data defect, not a
  market move. Found in 4 of 27 series, including three Vanguard LSE lines that share a fabricated
  Aug-2015 bar (VEVE.L +45.5% then -36.3%). ⚑ NONE of them is inside a live regression window
  today, because every fund NAV cache begins 2016-08. That is luck, not design: the defect sits
  one longer history away from the numbers, and the scan is what stops it arriving silently.

⚑ THE COMPARATOR IS NOT ALWAYS THE MANDATE INDEX, AND THAT IS A DECISION, NOT AN OVERSIGHT.
Raj, 13-Aug-2026: record the stated benchmark, measure with what passes the control. The two index
trackers in the sleeve are the controls — their alpha must come out at ~ -OCF — and the
mandate-index route fails both. `residual_caveat` carries the difference, per fund, published.
"""
import csv, json, os, sys, datetime as dt

# ── ISA-0940 (20-Nov-2026): LIVE-PATH EXECUTION LEDGER (framework_integrity) ─────────────
# ONE LINE at the head of the declared capability producer. `_mark` is in-memory and a NO-OP when
# isa_policy.V2_FLAGS["execution_ledger"] is False; it never raises into the caller.
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "bench_cache")
UNIVERSE = os.path.join(HERE, "fund_universe.json")

TWIN_TOL_ANN_PP = 0.25      # two series for the same index may differ by this much a year
TWIN_TOL_MONTH_PP = 5.0     # ...and by this much in any single month
SPIKE_JUMP = 0.25           # a move this large that reverses...
SPIKE_REVERSAL = 0.20       # ...by this much next month is a defective print
SPIKE_ABSOLUTE = 0.40       # or this large in one month with no reversal
BASIS_BREAK_PP = 15.0       # ⚑ a spike that never reverses is not a spike, it is a BASIS CHANGE,
                            # and the reversal test cannot see it. CSP1.L falls 31.58% in Sep-2010
                            # and never comes back: the line changed quote currency and
                            # auto_adjust does not restate it. Caught only by disagreeing with an
                            # independent series in the same month, which is what this threshold
                            # tests. Found 13-Aug-2026, after the spike scan passed the series.

# ⚑ TWO DIFFERENT TESTS, AND CONFLATING THEM IS AN ERROR THE FIRST RUN CAUGHT.
# TWINS are two series for the SAME index: they must agree, and a breach is a DATA DEFECT.
# NEIGHBOURS are series for near-identical indices: they will not agree exactly, the difference is
# the INDEX RESIDUAL the caveat declares, and it is published rather than failed. The first run of
# this module failed AAXJ against AEJ.L at -0.54pp/yr and called it a defect; it is Australia and
# New Zealand, which the mandate index contains and the comparator does not. Measuring it is the
# point. Calling it a fault would have sent someone looking for a bug in a correct series.
TWINS = {
 "CSP1.L": ("VUAG.L", "S&P 500 — two GBP-denominated trackers of the same index"),
 "VERX.L": ("VERG.L", "same index, distributing vs accumulating share class"),
 "VWRL.L": ("VWRP.L", "same index, distributing vs accumulating share class"),
 "WTEC.L": ("XDWT.L", "MSCI World Information Technology — two trackers of the same index"),
 "SWDA.L": ("IWRD.L", "MSCI World — two trackers of the same index. ⚑ THIS PAIR FAILS: they "
                      "differ by 2.04pp/yr. The neighbour test adjudicates — SWDA.L annualises "
                      "13.14% against VEVE.L's 13.13% (FTSE Developed World, an independent index "
                      "family), so IWRD.L is the defective series and is NOT USED."),
 "VJPN.L": (None, "NO SAME-INDEX TWIN — VJPN.L is the only FTSE Japan total-return series on the "
                  "feed. Adjudicated by neighbour instead."),
 "FTAL.L": (None, "NO SAME-INDEX TWIN — FTAL.L is the only FTSE All-Share total-return series "
                  "published on the feed. Reported as NO_TWIN, never as PASS (R2.10)."),
 "AAXJ":   (None, "NO SAME-INDEX TWIN on the feed for MSCI AC Asia ex Japan."),
}
# index residual against the comparator's NEAREST alternative index. Measured and published; a
# breach of NEIGHBOUR_TOL is a WARNING that names the residual, never a data-defect error.
NEIGHBOURS = {
 "SWDA.L": ("VEVE.L", "MSCI World vs FTSE Developed World — near-identical universes"),
 "VJPN.L": ("SJPA.L", "FTSE Japan vs MSCI Japan IMI — the mandate index; this IS the residual "
                      "the caveat declares"),
 "AAXJ":   ("AEJ.L",  "MSCI AC Asia ex Japan vs the mandate index MSCI AC Asia PACIFIC ex Japan; "
                      "the residual is Australia and New Zealand"),
 "FTAL.L": ("VUKE.L", "FTSE All-Share vs FTSE 100 — the residual is UK mid and small cap"),
 "CSP1.L": ("^SP500TR", "the mandate index itself, total return, converted from USD"),
}
NEIGHBOUR_TOL_ANN_PP = 1.5      # beyond this the two indices are not neighbours and the choice
                                # of comparator is doing real work — say so out loud

# Declared series starts, past a known-defective opening print. One home, with the reason.
SERIES_START = {
 "CSP1.L": ("2010-10", "the 2010-09 print falls 31.58% and never recovers - the line changes "
                       "quote basis and auto_adjust does not restate it. Every month before "
                       "2010-10 is on a different basis and is not comparable."),
 "SJPA.L": ("2010-01", "the 2009-12 print is 2401.0 against 1541.48 the following month — a -36% "
                       "bar at the series start that did not happen"),
 "VUAG.L": ("2019-07", "listed 2019-05; the first full month is a partial-listing artefact"),
}
# Months excluded as defective prints, found by spike_scan and declared here so the exclusion is
# visible in every artefact rather than buried in a cleaning step.
EXCLUDE_MONTHS = {
 "VEVE.L": [(2015, 8), (2015, 9)],
 "VERX.L": [(2015, 8), (2015, 9)],
 "VWRL.L": [(2015, 8), (2015, 9)],
 "XAXJ.L": [(2009, 8), (2009, 9), (2011, 4), (2011, 5), (2015, 8), (2015, 9)],
}
_EXCL_REASON = ("Yahoo carries a fabricated +45%/-36% pair across several Vanguard LSE lines in "
                "Aug-2015. Found by spike_scan 13-Aug-2026.")


class BenchmarkError(RuntimeError):
    """A contract failure. RAISED, never defaulted (R4.7)."""


# ─────────────────────────────────────────────────────── the registry
def load_universe(path=UNIVERSE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["funds"]


def mandate_for(sedol, universe=None):
    """The fund's OWN declared benchmark. RAISES if absent — a fund with no recorded mandate is a
    build error, not a fund with no benchmark (R4.1: 'missing' cannot be representable)."""
    U = universe if universe is not None else load_universe()
    u = U.get(sedol)
    if not isinstance(u, dict):
        raise BenchmarkError(f"{sedol}: not in fund_universe")
    mb = u.get("mandate_benchmark")
    if not mb:
        raise BenchmarkError(
            f"{sedol}: no mandate_benchmark block. The comparator is decision-grade and may not "
            f"be chosen without a recorded source (R6.1).")
    for k in ("declared", "index_name", "source_doc", "as_of", "accessibility", "comparator"):
        if k not in mb:
            raise BenchmarkError(f"{sedol}: mandate_benchmark missing '{k}'")
    # ⚑ A NULL index_name is legitimate and load-bearing where the KID declines to name one.
    # It must be an EXPLICIT declaration, never an absent key: `prospectus_declares_none` is what
    # separates "this fund names no benchmark" from "nobody has looked yet" (R4.1).
    if mb["index_name"] is None and not mb.get("prospectus_declares_none"):
        raise BenchmarkError(
            f"{sedol}: mandate_benchmark.index_name is null without prospectus_declares_none - "
            f"an unsourced benchmark and an absent one must not render the same")
    return mb


def comparator_for(sedol, universe=None):
    """-> (ticker, dict). The series alpha is measured against, with its caveat attached."""
    mb = mandate_for(sedol, universe)
    c = mb["comparator"]
    if not c.get("ticker"):
        raise BenchmarkError(f"{sedol}: mandate_benchmark.comparator.ticker is empty")
    return c["ticker"], c


def t4_mandate_for(sedol, universe=None):
    """-> (index_name, basis). THE one home for the question T4 asks: what is this fund supposed
    to be doing? Two bases, and the difference is not cosmetic.

      `prospectus`  the fund's OWN declared benchmark. A drift alert means the manager has left
                    the mandate it published.
      `investor`    Raj's yardstick, where the fund declines to name one (Raj, 13-Aug-2026). A
                    drift alert means THIS IS NO LONGER THE EXPOSURE HE BOUGHT - which is a real
                    and useful signal, and a different claim entirely. Every alert states which.

    ⚑ RAISES where neither exists. A fund with no testable mandate must stop the trigger, not be
    silently skipped by it - 'not tested' and 'tested and fine' may never render the same (R2.10).
    """
    U = universe if universe is not None else load_universe()
    mb = mandate_for(sedol, U)
    if mb.get("declared") and mb.get("index_name"):
        return mb["index_name"], "prospectus"
    inv = (U.get(sedol) or {}).get("investor_mandate")
    if inv and inv.get("index_name"):
        return inv["index_name"], "investor"
    raise BenchmarkError(
        f"{sedol}: no mandate T4 can test - the prospectus declares none and no investor_mandate "
        f"is recorded. T4 must REFUSE this fund rather than skip it.")


def all_t4_mandates(universe=None):
    U = universe if universe is not None else load_universe()
    return {sd: t4_mandate_for(sd, U)
            for sd in U if isinstance(U.get(sd), dict) and not str(sd).startswith("_")}


def all_comparators(universe=None):
    U = universe if universe is not None else load_universe()
    return {sd: comparator_for(sd, U)[0]
            for sd, u in U.items() if isinstance(u, dict) and not str(sd).startswith("_")}


# ─────────────────────────────────────────────────────── series handling
def _meta():
    p = os.path.join(BENCH, "_meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def read_levels(ticker, apply_start=True):
    p = os.path.join(BENCH, f"{ticker.replace('=','_')}.csv")
    if not os.path.exists(p):
        raise BenchmarkError(f"{ticker}: no series in bench_cache — a comparator with no series "
                             f"COUNTS as a failure, it does not fall back (R4.9)")
    start = None
    if apply_start and ticker in SERIES_START:
        start = dt.date.fromisoformat(SERIES_START[ticker][0] + "-01")
    out = {}
    for r in csv.DictReader(open(p)):
        d = dt.date.fromisoformat(r["date"])
        if start and d < start:
            continue
        out[(d.year, d.month)] = float(r["close"])
    return out


def monthly_returns(ticker, apply_start=True, apply_exclusions=True):
    L = read_levels(ticker, apply_start)
    ks = sorted(L)
    r = {ks[i]: L[ks[i]] / L[ks[i - 1]] - 1.0 for i in range(1, len(ks)) if L[ks[i - 1]]}
    if apply_exclusions:
        for k in EXCLUDE_MONTHS.get(ticker, []):
            r.pop(tuple(k), None)
    return r


def gbp_returns(ticker, **kw):
    """Monthly total return in GBP. The quote currency is READ FROM THE FEED, not from a
    hand-maintained list — the old USD_QUOTED set was a second home for a fact the metadata
    already carries, and a missing entry silently left the currency move inside beta."""
    r = monthly_returns(ticker, **kw)
    m = _meta().get(ticker) or {}
    cur = m.get("currency")
    if cur is None:
        raise BenchmarkError(f"{ticker}: no declared currency in bench_cache/_meta.json — the "
                             f"conversion decision cannot be made from a null (R4.3)")
    if cur != "USD":          # GBp and GBP alike: monthly returns are unit-free
        return r
    fx = monthly_returns("GBPUSD=X", apply_exclusions=False)
    return {k: (1 + r[k]) / (1 + fx[k]) - 1.0 for k in r if k in fx}


# ── MANDATE DRIFT (13-Aug-2026) ──────────────────────────────────────────────────────────
# ⚑ WHY A CHANGE AND NOT A LEVEL. A low R2 against the mandate index is not drift - it is what an
# active fund looks like (Ranmore runs 0.195 by design). Drift is the fund BECOMING SOMETHING ELSE,
# so the measurement is first-half versus second-half of its own history, not a threshold on the
# level.
#
# ⚑ THE FLOOR IS MEASURED, NOT CHOSEN. The two index trackers are the control: they cannot drift,
# so whatever they register IS the noise floor of the metric. Measured 13-Aug-2026 -
# VUAG |dbeta| 0.017 |dR2| 0.014, Vanguard Japan |dbeta| 0.057 |dR2| 0.036 - against an active-fund
# median of 0.216 and 0.112. The floors below are those control readings rounded up.
#
# ⚑ THE UPPER ANCHORS ARE JUDGEMENT AND SAY SO (R14.4). No evidence sets the point at which a fund
# has "become a different asset"; 0.50 of beta and 0.30 of R2 are declared, reversible, and carried
# in the rationale ledger. They are NOT calibrated.
# ⚑ RE-MEASURED 18-Oct-2026 (ISA-0869 found-on-build): with the NAV cache refreshed to Oct-2026 (same
# 2016-08 start) the controls read VUAG |dbeta| 0.014 |dR2| 0.015 and Vanguard Japan |dbeta| 0.063
# |dR2| 0.028 - Japan above the 0.06 beta floor measured on data to Jul-2026. Per the floor's own
# declared rule (the controls' readings rounded up) the beta floor is 0.07; the R2 floor 0.04 still
# covers both. Effect on the twelve funds: points_fraction moves <= 0.02 (a 10-point FRS component).
# A floor frozen at one date while its controls keep accruing data is fragile - follow-on registered.
DRIFT_FLOOR_BETA, DRIFT_FULL_BETA = 0.07, 0.50
DRIFT_FLOOR_R2,   DRIFT_FULL_R2   = 0.04, 0.30
DRIFT_MIN_MONTHS = 48          # 24 per half - the same minimum the FRS uses for mean/stddev
# ⚑ ISA-0875 (P7, Raj Q3-B 21-Nov-2026): the floor's own declared rule is "the controls' readings rounded up", so it is
#   EXECUTED every run instead of hand-maintained: floor = max(declared floor above, max control reading x MARGIN,
#   rounded UP to 0.01). The +20% margin is DECLARED judgement (headroom so one month's noise cannot flip a census),
#   uncalibrated (R14.4). GUARD: a tracker control reading above GUARD_X x the declared floor is a DATA fault, not
#   noise - the floor is not widened to absorb it; every fund's drift is UNSCORED (excluded, R4.3) until resolved.
DRIFT_CONTROLS = ("VUAG", "B50MZ94")      # the two index trackers (S&P 500, Japan): they cannot drift
DRIFT_FLOOR_MARGIN = 1.20
DRIFT_FLOOR_GUARD_X = 2.0
DRIFT_FLOOR_RUNTIME = True                # R4.13 rollback: False -> the declared constants, exactly as before


def _ceil2(v):
    import math as _m
    return _m.ceil(round(v * 100.0, 9)) / 100.0


# ⚑ ISA-0921 (P7, Raj Q2-C 21-Nov-2026): a declared STYLE DIAGNOSTIC beside the prospectus mandate. It answers "style
#   divergence or mandate change?" as EVIDENCE and decides nothing: T4, drift and the FRS keep the prospectus mandate
#   (R6 golden source; changing the benchmark because a fund fails would be benchmark-shopping).
STYLE_DIAGNOSTICS = {
    "BR2Q8G6": {"ticker": "IWVL.L", "index": "MSCI World Value (iShares Edge MSCI World Value Factor)",
                "authority": "Raj 21-Nov-2026 Q2-C: diagnostic only - never a measuring comparator (fails the tracker-control fit test)"},
}


def style_diagnostic(sedol, universe=None, fund_returns=None) -> dict:
    """-> full-sample + halves + last rolling-36 R2 of the fund vs its declared style index AND vs its mandate comparator
    on the SAME months. NOT_DECLARED for funds without one. Decides nothing."""
    d = STYLE_DIAGNOSTICS.get(sedol)
    if not d:
        return {"state": "NOT_DECLARED"}
    U = universe if universe is not None else load_universe()
    try:
        f = fund_returns if fund_returns is not None else _fund_returns(sedol, U)
        bt, _c = comparator_for(sedol, U)
        S, M = gbp_returns(d["ticker"]), gbp_returns(bt)
    except Exception as e:                                     # noqa: BLE001
        return {"state": "UNMEASURED", "why": "%s: %s" % (type(e).__name__, e), **d}
    ks = sorted(set(f) & set(S) & set(M))
    if len(ks) < DRIFT_MIN_MONTHS:
        return {"state": "UNMEASURED", "why": "%d common months < %d" % (len(ks), DRIFT_MIN_MONTHS), **d}
    def fit(X, kk):
        r = _ols1([f[k] for k in kk], [X[k] for k in kk])
        return {"beta": round(r[0], 3), "r2": round(r[1], 3)} if r else None
    h = len(ks) // 2
    return {"state": "MEASURED", **d, "mandate_comparator": bt, "n": len(ks),
            "window": ["%d-%02d" % ks[0], "%d-%02d" % ks[-1]],
            "style": {"full": fit(S, ks), "first_half": fit(S, ks[:h]), "second_half": fit(S, ks[h:]),
                      "last_36m": fit(S, ks[-36:])},
            "mandate": {"full": fit(M, ks), "first_half": fit(M, ks[:h]), "second_half": fit(M, ks[h:]),
                        "last_36m": fit(M, ks[-36:])},
            "role": "DIAGNOSTIC ONLY (Raj Q2-C) - decides nothing"}


def drift_floors(universe=None, control_readings=None) -> dict:
    """-> {state, floor_beta, floor_r2, readings, basis}. state RUNTIME_DERIVED | DECLARED_ONLY | CONTROL_UNMEASURED |
    GUARD_BREACH. `control_readings` {sedol: (d_beta, d_r2)} is injectable for tests."""
    if not DRIFT_FLOOR_RUNTIME:
        return {"state": "DECLARED_ONLY", "floor_beta": DRIFT_FLOOR_BETA, "floor_r2": DRIFT_FLOOR_R2,
                "basis": "DRIFT_FLOOR_RUNTIME is False (rollback): the declared constants"}
    U = universe if universe is not None else load_universe()
    rd = {}
    if control_readings is None:
        for c in DRIFT_CONTROLS:
            m = mandate_drift(c, U, floors={"state": "DECLARED_ONLY", "floor_beta": DRIFT_FLOOR_BETA,
                                            "floor_r2": DRIFT_FLOOR_R2})
            rd[c] = ((m.get("d_beta"), m.get("d_r2")) if m.get("status") == "MEASURED" else None)
    else:
        rd = dict(control_readings)
    if any(v is None for v in rd.values()) or not rd:
        return {"state": "CONTROL_UNMEASURED", "floor_beta": DRIFT_FLOOR_BETA, "floor_r2": DRIFT_FLOOR_R2,
                "readings": rd, "basis": ("a tracker control could not be measured this run - the DECLARED floors "
                                          "apply and this is named, never read as a measured floor")}
    mb = max(abs(v[0]) for v in rd.values()); mr = max(abs(v[1]) for v in rd.values())
    if mb > DRIFT_FLOOR_GUARD_X * DRIFT_FLOOR_BETA or mr > DRIFT_FLOOR_GUARD_X * DRIFT_FLOOR_R2:
        return {"state": "GUARD_BREACH", "floor_beta": None, "floor_r2": None, "readings": rd,
                "basis": ("a tracker control reads |dbeta| %.3f / |dR2| %.3f, above %.1fx the declared floors - a "
                          "data fault in the control or its comparator, not noise; drift is UNSCORED until resolved "
                          "(raise an item against ISA-0875)" % (mb, mr, DRIFT_FLOOR_GUARD_X))}
    return {"state": "RUNTIME_DERIVED", "readings": {k: [round(v[0], 4), round(v[1], 4)] for k, v in rd.items()},
            "floor_beta": max(DRIFT_FLOOR_BETA, _ceil2(mb * DRIFT_FLOOR_MARGIN)),
            "floor_r2": max(DRIFT_FLOOR_R2, _ceil2(mr * DRIFT_FLOOR_MARGIN)),
            "basis": ("max(declared %.2f/%.2f, max control |dbeta| %.3f / |dR2| %.3f x %.2f, rounded up to 0.01) - "
                      "ISA-0875, Raj Q3-B 21-Nov-2026" % (DRIFT_FLOOR_BETA, DRIFT_FLOOR_R2, mb, mr, DRIFT_FLOOR_MARGIN))}
DRIFT_ANCHOR_BASIS = ("floors MEASURED from the two index-tracker controls 13-Aug-2026, beta floor re-measured 18-Oct-2026; upper "
                      "anchors DECLARED judgement, uncalibrated (R14.4)")


def _fund_returns(sedol, universe=None):
    """Monthly fund returns via the ONE door (fund_performance.nav_series_for)."""
    import fund_performance as fp, fund_action_stack as fas
    U = universe if universe is not None else load_universe()
    return dict(fas._monthly_returns(fp.nav_series_for(sedol, U[sedol])))


def _ols1(y, x):
    n = len(y)
    mx = sum(x) / n; my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    b = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    a = my - b * mx
    resid = [y[i] - a - b * x[i] for i in range(n)]
    sst = sum((v - my) ** 2 for v in y)
    r2 = 1 - sum(e * e for e in resid) / sst if sst > 0 else None
    return b, r2


def mandate_drift(sedol, universe=None, fund_returns=None, floors=None):
    """-> dict. Measured drift of a fund away from the index it is supposed to track.

    status MEASURED -> `severity` in [0,1] and `points_fraction` = 1 - severity.
    status UNSCORED -> not enough history. UNSCORED is NOT neutral and NOT zero: an unmeasured
    fund is not a drifting fund, and the caller must exclude the component rather than guess it
    (R2.10, and the return_adequacy precedent that DEAD MONEY was once issued on unread returns).
    """
    _fi_mark("benchmark_registry", "mandate_drift")                      # ISA-0940: declared capability producer
    U = universe if universe is not None else load_universe()
    mandate, basis = t4_mandate_for(sedol, U)
    bt, _c = comparator_for(sedol, U)
    fl = floors if floors is not None else drift_floors(U)            # ISA-0875: runtime-derived floors
    out = {"sedol": sedol, "mandate": mandate, "mandate_basis": basis, "comparator": bt,
           "anchors": {"floor_beta": fl.get("floor_beta"), "full_beta": DRIFT_FULL_BETA,
                       "floor_r2": fl.get("floor_r2"), "full_r2": DRIFT_FULL_R2,
                       "floors_state": fl.get("state"), "floors_basis": fl.get("basis"),
                       "control_readings": fl.get("readings"), "basis": DRIFT_ANCHOR_BASIS}}
    if fl.get("state") == "GUARD_BREACH":
        out.update(status="UNSCORED", reason="ISA-0875 floor guard: " + str(fl.get("basis")))
        return out
    _FB, _FR = float(fl["floor_beta"]), float(fl["floor_r2"])
    try:
        f = fund_returns if fund_returns is not None else _fund_returns(sedol, U)
        B = gbp_returns(bt)
    except Exception as e:                                     # noqa: BLE001
        out.update(status="UNSCORED", reason=f"series unavailable: {type(e).__name__}: {e}")
        return out
    ks = sorted(set(f) & set(B))
    if len(ks) < DRIFT_MIN_MONTHS:
        out.update(status="UNSCORED", n=len(ks),
                   reason=f"{len(ks)} common months, below the {DRIFT_MIN_MONTHS}-month minimum "
                          f"(24 per half)")
        return out
    h = len(ks) // 2
    r1 = _ols1([f[k] for k in ks[:h]], [B[k] for k in ks[:h]])
    r2_ = _ols1([f[k] for k in ks[h:]], [B[k] for k in ks[h:]])
    if not r1 or not r2_:
        out.update(status="UNSCORED", reason="degenerate regression in one half")
        return out
    (b1, q1), (b2, q2) = r1, r2_
    d_beta = b2 - b1
    d_r2 = q2 - q1
    clamp = lambda v: 0.0 if v < 0 else (1.0 if v > 1 else v)
    leg_b = clamp((abs(d_beta) - _FB) / (DRIFT_FULL_BETA - _FB))
    # ⚑ ONLY A FALLING R2 IS DRIFT. A fund tracking its mandate BETTER has not drifted from it.
    leg_r = clamp((max(0.0, -d_r2) - _FR) / (DRIFT_FULL_R2 - _FR))
    sev = max(leg_b, leg_r)
    out.update(status="MEASURED", n=len(ks),
               window=[f"{ks[0][0]}-{ks[0][1]:02d}", f"{ks[h-1][0]}-{ks[h-1][1]:02d}",
                       f"{ks[h][0]}-{ks[h][1]:02d}", f"{ks[-1][0]}-{ks[-1][1]:02d}"],
               beta_first=round(b1, 3), beta_second=round(b2, 3), d_beta=round(d_beta, 3),
               r2_first=round(q1, 3), r2_second=round(q2, 3), d_r2=round(d_r2, 3),
               severity=round(sev, 3), points_fraction=round(1 - sev, 3),
               leg_beta=round(leg_b, 3), leg_r2=round(leg_r, 3),
               why=(f"beta {b1:.3f} -> {b2:.3f} ({d_beta:+.3f}) and R2 {q1:.3f} -> {q2:.3f} "
                    f"({d_r2:+.3f}) against {mandate} [{basis} mandate], measured as two halves of "
                    f"{len(ks)} months"))
    return out


def all_mandate_drift(universe=None):
    U = universe if universe is not None else load_universe()
    fl = drift_floors(U)                                                # once per sleeve read (ISA-0875)
    return {sd: mandate_drift(sd, U, floors=fl) for sd in U
            if isinstance(U.get(sd), dict) and not str(sd).startswith("_")}


# ─────────────────────────────────────────────────────── the two instruments
def annualised(rets, keys=None):
    ks = sorted(rets if keys is None else (set(rets) & set(keys)))
    if not ks:
        return None
    p = 1.0
    for k in ks:
        p *= 1 + rets[k]
    return (p ** (12 / len(ks)) - 1) * 100.0


def spike_scan(ticker):
    """-> [(month, jump_pct, next_pct)] . A print that jumps and reverses is a data defect."""
    r = monthly_returns(ticker, apply_exclusions=False)
    ks = sorted(r)
    out = []
    for i in range(len(ks) - 1):
        a, b = r[ks[i]], r[ks[i + 1]]
        if (abs(a) > SPIKE_JUMP and a * b < 0 and abs(b) > SPIKE_REVERSAL) or abs(a) > SPIKE_ABSOLUTE:
            out.append((f"{ks[i][0]}-{ks[i][1]:02d}", round(a * 100, 2), round(b * 100, 2)))
    return out


def twin_test(ticker):
    """-> dict. PASS / FAIL / NO_TWIN. NO_TWIN is a distinct outcome from PASS on purpose:
    'I could not measure it' and 'it is fine' must never render the same (R2.10)."""
    twin, why = TWINS.get(ticker, (None, "no twin declared for this ticker"))
    if twin is None:
        return {"ticker": ticker, "status": "NO_TWIN", "reason": why}
    a, b = gbp_returns(ticker), gbp_returns(twin)
    ks = sorted(set(a) & set(b))
    if len(ks) < 30:
        return {"ticker": ticker, "twin": twin, "status": "NO_TWIN",
                "reason": f"only {len(ks)} common months, below the 30-month minimum"}
    A, B = annualised(a, ks), annualised(b, ks)
    worst_pp, worst_k = max(((abs(a[k] - b[k]) * 100, k) for k in ks))
    fails = []
    if abs(A - B) > TWIN_TOL_ANN_PP:
        fails.append(f"annualised gap {A-B:+.2f}pp exceeds {TWIN_TOL_ANN_PP}pp")
    if worst_pp > TWIN_TOL_MONTH_PP:
        fails.append(f"worst month {worst_k[0]}-{worst_k[1]:02d} differs {worst_pp:.2f}pp")
    return {"ticker": ticker, "twin": twin, "basis": why, "status": "FAIL" if fails else "PASS",
            "n": len(ks), "ann_ticker": round(A, 2), "ann_twin": round(B, 2),
            "gap_pp": round(A - B, 2), "worst_month": f"{worst_k[0]}-{worst_k[1]:02d}",
            "worst_pp": round(worst_pp, 2), "failures": fails}


def basis_break_scan(ticker):
    """-> [(month, ticker_pct, neighbour_pct, gap_pp)] . A month where the comparator and an
    independent series for a near-identical index disagree by more than BASIS_BREAK_PP has a
    defect in one of them. Unlike spike_scan this sees a break that never reverses."""
    nb, _ = NEIGHBOURS.get(ticker, (None, None))
    if nb is None:
        return []
    try:
        a, b = gbp_returns(ticker), gbp_returns(nb)
    except BenchmarkError:
        return []
    out = []
    for k in sorted(set(a) & set(b)):
        gap = (a[k] - b[k]) * 100
        if abs(gap) > BASIS_BREAK_PP:
            out.append((f"{k[0]}-{k[1]:02d}", round(a[k] * 100, 2), round(b[k] * 100, 2),
                        round(gap, 2)))
    return out


def neighbour_test(ticker):
    """-> dict. The measured index residual, PUBLISHED. Never a pass/fail on its own."""
    nb, why = NEIGHBOURS.get(ticker, (None, "no neighbour declared"))
    if nb is None:
        return {"ticker": ticker, "status": "NO_NEIGHBOUR", "reason": why}
    try:
        a, b = gbp_returns(ticker), gbp_returns(nb)
    except BenchmarkError as e:
        return {"ticker": ticker, "neighbour": nb, "status": "NO_NEIGHBOUR", "reason": str(e)}
    ks = sorted(set(a) & set(b))
    if len(ks) < 30:
        return {"ticker": ticker, "neighbour": nb, "status": "NO_NEIGHBOUR",
                "reason": f"only {len(ks)} common months"}
    A, B = annualised(a, ks), annualised(b, ks)
    wide = abs(A - B) > NEIGHBOUR_TOL_ANN_PP
    return {"ticker": ticker, "neighbour": nb, "basis": why,
            "status": "WIDE" if wide else "MEASURED", "n": len(ks),
            "ann_ticker": round(A, 2), "ann_neighbour": round(B, 2),
            "index_residual_pp": round(A - B, 2)}


def validate_all(universe=None):
    """Every comparator actually in use is twin-tested and spike-scanned. Returns the artefact and
    the error list; a FAIL is an error, a NO_TWIN is reported and is not."""
    U = universe if universe is not None else load_universe()
    res = {"as_of": dt.date.today().isoformat(), "tolerances":
           {"twin_ann_pp": TWIN_TOL_ANN_PP, "twin_month_pp": TWIN_TOL_MONTH_PP},
           "comparators": {}, "errors": [], "no_twin": [], "adjudicated": [],
           "index_residuals": {}}
    for sd, t in sorted(all_comparators(U).items()):
        tw = twin_test(t)
        nb = neighbour_test(t)
        sp = spike_scan(t)
        bb = basis_break_scan(t)
        undeclared = [m for m in sp
                      if tuple(int(x) for x in m[0].split("-")) not in
                      {tuple(k) for k in EXCLUDE_MONTHS.get(t, [])}]
        _idx, _basis = t4_mandate_for(sd, U)
        res["comparators"][sd] = {"ticker": t, "t4_mandate": _idx, "t4_basis": _basis,
                                  "twin_test": tw, "neighbour_test": nb,
                                  "spike_scan": sp, "undeclared_spikes": undeclared,
                                  "basis_breaks": bb,
                                  "excluded_months": EXCLUDE_MONTHS.get(t, []),
                                  "exclusion_reason": _EXCL_REASON if t in EXCLUDE_MONTHS else None,
                                  "series_start": SERIES_START.get(t)}
        if tw["status"] == "FAIL":
            # a twin failure says ONE of the pair is defective, not which. Adjudicate with the
            # neighbour before calling it an error against the comparator we actually use.
            adj = (nb.get("status") == "MEASURED")
            if adj:
                res["adjudicated"].append(
                    f"{sd}/{t}: twin {tw['twin']} disagrees by {tw['gap_pp']:+.2f}pp/yr, but the "
                    f"independent neighbour {nb['neighbour']} agrees to "
                    f"{nb['index_residual_pp']:+.2f}pp — {tw['twin']} is the defective series, "
                    f"{t} stands")
            else:
                res["errors"].append(
                    f"{sd}/{t}: twin test FAILED and no neighbour can adjudicate — "
                    f"{'; '.join(tw['failures'])}")
        if tw["status"] == "NO_TWIN":
            res["no_twin"].append(f"{sd}/{t}: {tw['reason']}")
        if nb.get("status") == "MEASURED":
            res["index_residuals"][sd] = {"vs": nb["neighbour"], "pp_per_year": nb["index_residual_pp"]}
        if nb.get("status") == "WIDE":
            res["index_residuals"][sd] = {"vs": nb["neighbour"], "pp_per_year": nb["index_residual_pp"],
                                          "flag": "WIDE — the comparator choice is doing real work here"}
        if undeclared:
            res["errors"].append(f"{sd}/{t}: undeclared defective print(s) {undeclared}")
        if bb:
            res["errors"].append(f"{sd}/{t}: basis break vs {NEIGHBOURS[t][0]} {bb}")
    return res


def check_all():
    """Routine-battery entry point. -> error count."""
    try:
        r = validate_all()
    except BenchmarkError as e:
        print(f"  benchmark_registry: CONTRACT FAILURE — {e}")
        return 1
    for e in r["errors"]:
        print(f"  benchmark_registry ERROR: {e}")
    for n in r["no_twin"]:
        print(f"  benchmark_registry NO_TWIN (reported, not an error): {n}")
    for a in r["adjudicated"]:
        print(f"  benchmark_registry ADJUDICATED: {a}")
    for sd, v in sorted(r["index_residuals"].items()):
        print(f"  benchmark_registry residual {sd} vs {v['vs']}: {v['pp_per_year']:+.2f}pp/yr"
              + (f"  {v['flag']}" if "flag" in v else ""))
    _inv = [sd for sd, c in r["comparators"].items() if c.get("t4_basis") == "investor"]
    if _inv:
        print(f"  benchmark_registry: T4 basis is INVESTOR mandate (Raj's yardstick, not the "
              f"fund's) for {', '.join(sorted(_inv))} - alerts must say so")
    if not r["errors"]:
        print(f"  benchmark_registry: {len(r['comparators'])} comparators validated, "
              f"{len(r['no_twin'])} without an available twin")
    return len(r["errors"])


def _selftest():
    """Offline selftest of the pure pieces this module's drift measure rests on (no network, no cache)."""
    fails = []

    def ok(name, cond, detail=None):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
        if not cond:
            fails.append(name)
    x = [0.01 * i for i in range(-12, 13)]
    b, r2 = _ols1([2.0 * v + 0.001 for v in x], x)
    ok("OLS recovers beta 2 and R2 1 on an exact line", abs(b - 2.0) < 1e-9 and abs(r2 - 1.0) < 1e-9, (b, r2))
    ok("NEGATIVE CONTROL: a constant regressor must not yield a beta (refused, None)", _ols1([0.1] * 5, [0.2] * 5) is None)
    ok("floors sit below the full-drift anchors (a floor above its anchor would score every fund 0)",
       0 < DRIFT_FLOOR_BETA < DRIFT_FULL_BETA and 0 < DRIFT_FLOOR_R2 < DRIFT_FULL_R2)
    ok("ISA-0875: the re-measured beta floor covers the 18-Oct control reading (Vanguard Japan 0.063) and the R2 floor "
       "its 0.028", DRIFT_FLOOR_BETA >= 0.063 and DRIFT_FLOOR_R2 >= 0.028)
    ok("the drift minimum is two 24-month halves", DRIFT_MIN_MONTHS == 48)
    # ── ISA-0875 runtime floors (P7, Raj Q3-B) ─────────────────────────────────────────────────
    _f = drift_floors({}, control_readings={"VUAG": (0.014, 0.015), "B50MZ94": (0.063, -0.028)})
    ok("ISA-0875 MUST-FIRE: the floors are DERIVED from the controls (0.063 x 1.2 -> 0.08 beta; R2 stays at the declared 0.04)",
       _f["state"] == "RUNTIME_DERIVED" and abs(_f["floor_beta"] - 0.08) < 1e-9 and abs(_f["floor_r2"] - 0.04) < 1e-9, _f)
    _f = drift_floors({}, control_readings={"VUAG": (0.010, 0.010), "B50MZ94": (0.020, 0.010)})
    ok("ISA-0875 NEGATIVE CONTROL: quiet controls never take the floor BELOW the declared constants",
       _f["floor_beta"] == DRIFT_FLOOR_BETA and _f["floor_r2"] == DRIFT_FLOOR_R2, _f)
    _f = drift_floors({}, control_readings={"VUAG": (0.014, 0.015), "B50MZ94": (0.200, 0.020)})
    ok("ISA-0875 MUST-FIRE: a control above 2x the declared floor is a GUARD_BREACH (no floor), never absorbed",
       _f["state"] == "GUARD_BREACH" and _f["floor_beta"] is None, _f)
    _f = drift_floors({}, control_readings={"VUAG": None, "B50MZ94": (0.05, 0.02)})
    ok("ISA-0875: an unmeasured control is NAMED (CONTROL_UNMEASURED) and the declared floors apply",
       _f["state"] == "CONTROL_UNMEASURED" and _f["floor_beta"] == DRIFT_FLOOR_BETA, _f)
    print("benchmark_registry selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    if "--json" in sys.argv:
        print(json.dumps(validate_all(), indent=1))
    else:
        sys.exit(1 if check_all() else 0)
