#!/usr/bin/env python3
"""whole_book_risk.py - P-D (B08-R03, ISA-0887): ONE measured covariance of the WHOLE ISA - direct stocks AND funds.

WHY THIS EXISTS
---------------
Every risk number the capital engine selected on was the STOCK SLEEVE's (sleeve_risk / bundle_risk / plan_risk), and
the fund sleeve - ~81% of the ISA - entered only as a stated rho = 1 bound (ISA-0621 delivery note). Whole-book variance
is stock variance + fund variance + TWICE their covariance, so two candidate stocks with the same sleeve risk can add
very different WHOLE-ISA risk: a tech name co-moves with Polar Global Tech, SMT and VUAG; an energy name does not.
Measured on the 22-Oct-2026 book (156 weeks to 16-Oct-2026): the stocks are 11.7% of the money and 25.4% of the
whole-ISA risk, rho(stock sleeve, fund sleeve) = 0.63 (not 1.0), whole-ISA volatility 11.9% (GBP ~18.6k a year).

METHOD - THE SAME BASIS AS THE STOCKS, STATED SO IT IS NOT OVER-READ (R2.6)
-------------------------------------------------------------------------
* Fund levels come from THE one NAV route (fund_performance.nav_series_for - the golden source, ISA-0869) and are
  sampled on the stock store's convention: one level per Friday-ending week = the LAST published NAV dated Monday..Friday
  of that week. A week with no NAV is ABSENT - never forward-filled (stock_price_fetch rule 1).
* The fund levels join the stock store as extra names (FUND_<ticker>) and every covariance quantity is computed by
  sleeve_risk - the ONE covariance home (R4.4): the same window (isa_policy.RISK_WINDOW_WEEKS = 156, minimum 52),
  consecutive Fridays only, sample covariance (n-1), annualised x52.
* Cash (and the money-market ETF the framework treats as cash) carries zero variance: it dilutes book volatility and is
  not a risk-share member.
* Non-synchronous pricing: OEIC NAVs strike at ~noon UK, US stocks close at 21:00 UK. Weekly sampling bounds the effect;
  a Dimson (lag-1) whole-book sigma is published BESIDE the sample one as a sensitivity, never substituted.
* Known series caveats are DECLARED (KNOWN_SERIES_CAVEATS) and carried on every result. They matter for RETURNS and are
  bounded for covariance (e.g. Artemis UK's retained-income double count adds ~2 weekly jumps a year: < 1% of sigma).

STATES (R2.10 - "could not measure" never reads as "low risk")
--------------------------------------------------------------
MEASURED   every risky holding has a qualified weekly series on the common window.
PARTIAL    one or more holdings are UNMEASURED (named, with GBP); sigma is published for the measured subset AND an
           upper bound that puts every unmeasured pound at the highest measured volatility with correlation 1.
REFUSED    no window of >= RISK_MIN_WEEKS common weeks, or no risky holdings.

CONSUMERS: capital_decision_engine (bundle_risk / plan_risk: the comparator's risk metric when MEASURED -
WHOLE_BOOK_RISK_METRIC), whole_book_stress (betas / governing scenario), monthly_isa_prerun Step 6.10s (summary +
artefact), email_prefill (s6 line). ROLLBACK (R4.13): scoring_config.WHOLE_BOOK_RISK_METRIC = False -> the engine
compares on stock-sleeve GBP volatility exactly as before; this module still measures and reports.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

SCHEMA = "whole_book_risk/1"
FUND_PREFIX = "FUND_"
BROAD_EQUITY_PROXY = FUND_PREFIX + "VUAG"   # the protocol's own broad-equity instrument (S&P 500, GBP line)
METHOD = ("whole-book sample covariance of weekly GBP total-return levels (stocks: stock_weekly_returns.json; funds: "
          "fund_performance.nav_series_for sampled to the last NAV of each Mon-Fri week), common window = "
          "sleeve_risk.window_dates (isa_policy.RISK_WINDOW_WEEKS / RISK_MIN_WEEKS), consecutive Fridays only, n-1, "
          "annualised x52; cash zero variance; Euler risk shares (whole_book_risk.measure)")
STALE_FRIDAYS = 2          # the stock store's STALE_INPUT rule (weekly_store_freshness: >= 2 missed settled Fridays)

# ⚑ DECLARED, NOT DISCOVERED: series caveats the framework already knows about (PD_S5_S6 checkpoint 24-Oct-2026;
#   fund_universe). They are published, never silently corrected here - correcting a NAV route is the fund
#   programme's job (ISA-0327 boundary), measuring with a stated caveat is this module's.
KNOWN_SERIES_CAVEATS = {
    "B2PLJM6": ("ACC_INCOME_DOUBLE_COUNT: the Yahoo adjusted history of this Acc class adds retained income again "
                "(2025 inflated ~4.78pp); covariance effect bounded at <1% of sigma (two weekly jumps a year), "
                "returns are NOT decision-grade from this series"),
    "B42W4J8": ("INCOME_CLASS_PRICE_ROUTE: Polar Global Tech I Inc is read from the FT price route; distributions "
                "are small for this mandate but the series is a price, not a total, return"),
    "SMT": ("TRUST_PRICE_NOT_NAV: Scottish Mortgage is a listed trust - the series is the share price, so discount "
            "moves are inside the measured volatility (that is the risk actually held)"),
}


class WholeBookRefused(RuntimeError):
    """A whole-book quantity cannot be produced on a basis it can honour (R4.3)."""


# ────────────────────────────────────────────────────────────────────────────── fund series
def _as_date(x) -> dt.date:
    if x is None:
        return dt.date.today()
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(str(x)[:10])


def friday_of(d: dt.date) -> Optional[dt.date]:
    """The Friday ending the Mon..Fri week that contains d; None for a weekend date (never re-dated)."""
    if d.weekday() > 4:
        return None
    return d + dt.timedelta(days=4 - d.weekday())


def fund_weekly_levels(series: Sequence[Tuple[dt.date, float]]) -> Dict[str, float]:
    """{friday_iso: level} - the LAST positive NAV dated Monday..Friday of each week. Absent week = absent key."""
    by: Dict[dt.date, Tuple[dt.date, float]] = {}
    for d, c in series or []:
        if c is None or not (c == c) or c <= 0:
            continue
        f = friday_of(d)
        if f is None:
            continue
        if f not in by or d > by[f][0]:
            by[f] = (d, float(c))
    return {f.isoformat(): v for f, (_, v) in sorted(by.items())}


def last_settled_friday(as_of: dt.date) -> dt.date:
    """The most recent Friday on or before as_of."""
    return as_of - dt.timedelta(days=(as_of.weekday() - 4) % 7)


def qualify_fund(ticker: str, urow: Optional[dict], series, as_of: dt.date) -> dict:
    """Typed qualification of ONE held fund's weekly series for covariance use (R6.6: identity at the boundary)."""
    q = {"ticker": ticker, "key": FUND_PREFIX + ticker, "caveats": [], "state": None}
    if not urow:
        q.update(state="REFUSED", why="not in fund_universe.json - no declared identity, so no series may be joined "
                                      "to it (R2.7)")
        return q
    q.update(isin=urow.get("isin"), route=("local_series" if urow.get("local_series") else
                                           (urow.get("yf_symbol") or urow.get("isin"))),
             acc_or_inc=urow.get("acc_or_inc"), price_unit=urow.get("price_unit"))
    lv = fund_weekly_levels(series or [])
    if not lv:
        q.update(state="REFUSED", why="no NAV observations from the declared route", n_weeks=0)
        return q
    dates = sorted(lv)
    last = dt.date.fromisoformat(dates[-1])
    missed = max(0, (last_settled_friday(as_of) - last).days // 7)
    q.update(n_weeks=len(dates), first=dates[0], last=dates[-1], missed_fridays=missed, levels=lv)
    if missed >= STALE_FRIDAYS:
        q.update(state="REFUSED", why="STALE_INPUT: latest weekly NAV %s is %d settled Friday(s) behind %s (the stock "
                                      "store's rule, >= %d)" % (dates[-1], missed, as_of, STALE_FRIDAYS))
        return q
    if ticker in KNOWN_SERIES_CAVEATS:
        q["caveats"].append(KNOWN_SERIES_CAVEATS[ticker])
    q["state"] = "QUALIFIED_WITH_CAVEAT" if q["caveats"] else "QUALIFIED"
    return q


_SERIES_MEMO: dict = {}


def _nav_stamp(root: str):
    """COST ONLY: a change stamp over the NAV caches, so a memoised series is never served after a refresh."""
    import glob as _g
    fs = _g.glob(os.path.join(root, "nav_cache", "*.csv"))
    return (len(fs), max((os.stat(f).st_mtime_ns for f in fs), default=0))


def load_fund_series(portfolio: dict, as_of: dt.date, root: str = HERE, series_fn=None, universe=None) -> Dict[str, dict]:
    """{ticker: qualification} for every held fund, via fund_performance.nav_series_for (injectable for tests).
    COST ONLY: on the default route the qualification is memoised per (ticker, as_of, NAV-cache stamp) - the engine
    campaign builds hundreds of receipts on one set of caches; any cache change recomputes."""
    if series_fn is None and universe is None:
        stamp = _nav_stamp(root)
        key = (tuple(sorted(f.get("ticker") for f in (portfolio or {}).get("funds") or [] if f.get("ticker"))),
               _as_date(as_of).isoformat(), stamp, root)
        got = _SERIES_MEMO.get(key)
        if got is not None:
            return got
        out = _load_fund_series(portfolio, as_of, root)
        _SERIES_MEMO.clear()
        _SERIES_MEMO[key] = out
        return out
    return _load_fund_series(portfolio, as_of, root, series_fn, universe)


def _load_fund_series(portfolio: dict, as_of: dt.date, root: str = HERE, series_fn=None, universe=None) -> Dict[str, dict]:
    if series_fn is None or universe is None:
        import fund_performance as _fp
        universe = universe if universe is not None else _fp.load_universe()
        series_fn = series_fn or (lambda t, u: _fp.nav_series_for(t, u))
    out = {}
    for f in (portfolio or {}).get("funds") or []:
        t = f.get("ticker")
        if not t:
            continue
        u = (universe or {}).get(t)
        try:
            s = series_fn(t, u) if u else None
        except Exception as exc:                                        # noqa: BLE001
            out[t] = {"ticker": t, "key": FUND_PREFIX + t, "state": "REFUSED", "caveats": [],
                      "why": "NAV route failed: %s: %s" % (type(exc).__name__, exc)}
            continue
        out[t] = qualify_fund(t, u, s, as_of)
    return out


def augmented_store(stock_store: dict, quals: Dict[str, dict]) -> dict:
    """The stock store + one FUND_<ticker> record per QUALIFIED fund, in the store's own record shape, so every
    covariance quantity is computed by sleeve_risk (one home). The stock store is never mutated."""
    aug = dict(stock_store or {})
    names = dict((stock_store or {}).get("names") or {})
    for t, q in quals.items():
        if q.get("state") in ("QUALIFIED", "QUALIFIED_WITH_CAVEAT"):
            names[q["key"]] = {"currency": "GBP", "total_return_basis": True, "asset_kind": "fund",
                               "observations": {d: {"gbp": v, "source": "fund_performance.nav_series_for"}
                                                for d, v in q["levels"].items()}}
    aug["names"] = names
    return aug


# ────────────────────────────────────────────────────────────────────────────── book
def book_values(portfolio: dict, stock_legs: Iterable[Tuple[str, float]] = (),
                fund_legs: Iterable[Tuple[str, float]] = ()) -> Tuple[Dict[str, float], float]:
    """-> ({key: GBP} for every risky holding, cash GBP). Legs are signed GBP changes (+ buy, - sell/trim); the cash
    leg is implied (it never carries variance, so a residual's exact size does not move volatility)."""
    v: Dict[str, float] = {}
    for s in (portfolio or {}).get("stocks") or []:
        if s.get("ticker"):
            v[s["ticker"]] = v.get(s["ticker"], 0.0) + float(s.get("value_gbp") or 0.0)
    for f in (portfolio or {}).get("funds") or []:
        if f.get("ticker"):
            k = FUND_PREFIX + f["ticker"]
            v[k] = v.get(k, 0.0) + float(f.get("value_gbp") or 0.0)
    for t, a in stock_legs or ():
        v[t] = v.get(t, 0.0) + float(a)
    for t, a in fund_legs or ():
        k = t if str(t).startswith(FUND_PREFIX) else FUND_PREFIX + t
        v[k] = v.get(k, 0.0) + float(a)
    v = {k: x for k, x in v.items() if x > 0.005}
    cash = float((((portfolio or {}).get("cash") or {}).get("value_gbp")) or 0.0)
    return v, cash


def _sr():
    import sleeve_risk as _s
    return _s


def _cov_table(store, names, dates):
    sr = _sr()
    R = {t: sr.returns_on(store, t, dates) for t in names}
    C = {}
    for i, a in enumerate(names):
        for b in names[i:]:
            c = sr._cov(R[a], R[b])
            C[(a, b)] = C[(b, a)] = c
    return R, C


def _cov_cached(store, names, dates, memo):
    """COST ONLY: the pairwise covariances of `names` on `dates`, memoised in `memo` (one engine run) - bitwise the
    same numbers as _cov_table (same series, same pair order, same sleeve_risk._cov)."""
    sr = _sr()
    # perf: a tuple's hash is not cached, so keying every pair on the 157-date tuple re-hashed it n^2 times per plan;
    # the window is interned to a small id once per call (same numbers, same pair order).
    dki = memo.setdefault("_DK", {})
    dk = dki.setdefault(tuple(dates), len(dki))
    rc = memo.setdefault("_R", {})
    cc = memo.setdefault("_C", {})
    R = {}
    for t in names:
        k = (t, dk)
        if k not in rc:
            rc[k] = sr.returns_on(store, t, dates)
        R[t] = rc[k]
    C = {}
    for i, a in enumerate(names):
        for b in names[i:]:
            k = (a, b, dk)
            if k not in cc:
                cc[k] = sr._cov(R[a], R[b])
            C[(a, b)] = C[(b, a)] = cc[k]
    return R, C


def portfolio_sigma(store: dict, values: Dict[str, float], dates: Optional[Sequence[str]] = None, memo=None) -> dict:
    """Annualised volatility of the RISKY book `values` (GBP by key) on one common window. Raises WholeBookRefused."""
    sr = _sr()
    names = sorted(k for k, x in values.items() if x > 0)
    if not names:
        raise WholeBookRefused("no risky holdings")
    # P-D perf (25-Oct-2026, campaign census 131s -> >169s): the engine prices ~10k plans on the SAME name set, so the
    # validated window + covariance table for a name set is memoised once; only the weights change per plan.
    pk = ("_P", tuple(names), None if dates is None else (len(dates), dates[0], dates[-1]))
    if memo is not None and pk in memo and dates is None:
        dates, R, C = memo[pk]
        tot = sum(values[t] for t in names)
        w = {t: values[t] / tot for t in names}
        var = sum(w[a] * w[b] * C[(a, b)] for a in names for b in names)
        if var <= 0:
            raise WholeBookRefused("non-positive whole-book variance (degenerate series)")
        s = math.sqrt(var * sr.WEEKS_PER_YEAR)
        return {"sigma_risky": s, "risky_gbp": tot, "gbp_vol": s * tot, "weights": w, "dates": dates,
                "n_weeks": sr.n_returns(dates), "var_weekly": var, "R": R, "C": C, "names": names}
    if memo is not None:
        lv = memo.setdefault("_L", {})
        missing = [t for t in names if not (lv[t] if t in lv else lv.setdefault(t, bool(sr._levels(store, t))))]
    else:
        missing = [t for t in names if not sr._levels(store, t)]
    if missing:
        raise WholeBookRefused("no weekly series for %s" % ", ".join(missing))
    if dates is None and memo is not None:
        wk = ("_W", tuple(names))
        if wk not in memo:
            memo[wk] = list(sr.window_dates(store, names))
        dates = memo[wk]
    dates = list(dates or sr.window_dates(store, names))
    _, mn = sr._window_policy()
    if sr.n_returns(dates) < mn:
        raise WholeBookRefused("only %d consecutive common weeks across the book, %d required"
                               % (sr.n_returns(dates), mn))
    tot = sum(values[t] for t in names)
    w = {t: values[t] / tot for t in names}
    R, C = _cov_cached(store, names, dates, memo) if memo is not None else _cov_table(store, names, dates)
    if memo is not None and pk[2] is None:
        memo[pk] = (dates, R, C)
    var = sum(w[a] * w[b] * C[(a, b)] for a in names for b in names)
    if var <= 0:
        raise WholeBookRefused("non-positive whole-book variance (degenerate series)")
    s = math.sqrt(var * sr.WEEKS_PER_YEAR)
    return {"sigma_risky": s, "risky_gbp": tot, "gbp_vol": s * tot, "weights": w, "dates": dates,
            "n_weeks": sr.n_returns(dates), "var_weekly": var, "R": R, "C": C, "names": names}


def plan_gbp_vol(store: dict, portfolio: dict, stock_legs=(), fund_legs=(), memo=None) -> dict:
    """Post-action WHOLE-ISA GBP volatility of one plan (B08-R03): donor reductions, receiver additions, fund trims and
    every unchanged holding at their actual post-trade values, one basis. -> {state, gbp_vol, sigma_risky, ...}."""
    vals, _cash = book_values(portfolio, stock_legs, fund_legs)
    try:
        p = portfolio_sigma(store, vals, memo=memo)
    except WholeBookRefused as exc:
        return {"state": "UNMEASURED", "why": str(exc)}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNMEASURED", "why": "%s: %s" % (type(exc).__name__, exc)}
    return {"state": "MEASURED", "gbp_vol": p["gbp_vol"], "sigma_risky": p["sigma_risky"], "risky_gbp": p["risky_gbp"],
            "n_weeks": p["n_weeks"], "risk_window": [p["dates"][0], p["dates"][-1]]}


def _dimson_var(R, w, names):
    sr = _sr()
    v = 0.0
    for a in names:
        for b in names:
            x, y = R[a], R[b]
            c = sr._cov(x, y)
            if a != b:
                c += sr._cov(x[1:], y[:-1]) + sr._cov(x[:-1], y[1:])
            v += w[a] * w[b] * c
    return v


def measure(portfolio: dict, stock_store: dict, as_of: Optional[dt.date] = None, *, root: str = HERE,
            series_fn=None, universe=None) -> dict:
    """THE whole-ISA risk measurement (R20.1: input -> calculation -> constraint -> decision evidence)."""
    _fi_mark("whole_book_risk", "measure")
    as_of = _as_date(as_of)
    sr = _sr()
    quals = load_fund_series(portfolio, as_of, root=root, series_fn=series_fn, universe=universe)
    store = augmented_store(stock_store, quals)
    vals, cash = book_values(portfolio)
    nav = float(((portfolio or {}).get("summary") or {}).get("total_value_gbp") or (sum(vals.values()) + cash))
    out = {"schema": SCHEMA, "as_of": as_of.isoformat(), "method": METHOD, "nav_gbp": round(nav, 2),
           "cash_gbp": round(cash, 2), "fund_series": {t: {k: q.get(k) for k in
                                                          ("state", "why", "isin", "route", "n_weeks", "first", "last",
                                                           "missed_fridays", "caveats")} for t, q in quals.items()}}
    unmeasured = {}
    for k, x in vals.items():
        if not sr._levels(store, k):
            unmeasured[k] = round(x, 2)
    meas = {k: x for k, x in vals.items() if k not in unmeasured}
    out["coverage"] = {"risky_gbp": round(sum(vals.values()), 2), "measured_gbp": round(sum(meas.values()), 2),
                       "unmeasured": unmeasured, "unmeasured_gbp": round(sum(unmeasured.values()), 2)}
    try:
        p = portfolio_sigma(store, meas)
    except WholeBookRefused as exc:
        out.update(state="REFUSED", why=str(exc))
        return out
    names, w, C, R = p["names"], p["weights"], p["C"], p["R"]
    var = p["var_weekly"]
    S = [n for n in names if not n.startswith(FUND_PREFIX)]
    F = [n for n in names if n.startswith(FUND_PREFIX)]
    vs = sum(w[a] * w[b] * C[(a, b)] for a in S for b in S)
    vf = sum(w[a] * w[b] * C[(a, b)] for a in F for b in F)
    vx = sum(w[a] * w[b] * C[(a, b)] for a in S for b in F)
    rho = vx / math.sqrt(vs * vf) if vs > 0 and vf > 0 else None
    bound = (math.sqrt(vs) + math.sqrt(vf)) * math.sqrt(sr.WEEKS_PER_YEAR) if vs > 0 and vf > 0 else None
    mctr = {a: sum(w[b] * C[(a, b)] for b in names) for a in names}
    shares = {a: w[a] * mctr[a] / var for a in names}
    risky = p["risky_gbp"]
    sig = p["sigma_risky"]
    book_sigma = sig * risky / nav if nav else None
    # sensitivity: other windows + Dimson
    sens = {}
    for W in (52, 104):
        try:
            d2 = sr.window_dates(store, names, weeks=W)
            sens["window_%dw" % W] = round(portfolio_sigma(store, meas, d2)["sigma_risky"], 5)
        except Exception as exc:                                        # noqa: BLE001
            sens["window_%dw" % W] = "REFUSED: %s" % exc
    try:
        sens["dimson_lag1"] = round(math.sqrt(max(_dimson_var(R, w, names), 0.0) * sr.WEEKS_PER_YEAR), 5)
    except Exception as exc:                                            # noqa: BLE001
        sens["dimson_lag1"] = "REFUSED: %s" % exc
    # broad-equity betas (Dimson, to the protocol's VUAG) - stress inputs
    betas = {}
    if BROAD_EQUITY_PROXY in names:
        m = R[BROAD_EQUITY_PROXY]
        vm = sr._cov(m, m)
        for a in names:
            x = R[a]
            b0 = sr._cov(x, m) / vm
            b1 = sr._cov(x[1:], m[:-1]) / vm
            bm1 = sr._cov(x[:-1], m[1:]) / vm
            betas[a] = {"beta_dimson": round(b0 + b1 + bm1, 4), "beta_contemporaneous": round(b0, 4)}
    vols = {a: round(math.sqrt(C[(a, a)] * sr.WEEKS_PER_YEAR), 5) for a in names}
    # upper bound for unmeasured mass (R2.10): highest measured vol, correlation 1 with the measured book
    upper = None
    if unmeasured:
        vmax = max(vols.values())
        um = sum(unmeasured.values())
        upper = (sig * risky + vmax * um)
    # static current-book weekly path over the window (current-holdings basis; NOT the account's realised path)
    n = len(R[names[0]])
    path = [sum(w[t] * R[t][i] for t in names) for i in range(n)]
    lvl, peak, mdd, worst = 1.0, 1.0, 0.0, None
    for i, r in enumerate(path):
        lvl *= 1.0 + r
        peak = max(peak, lvl)
        dd = lvl / peak - 1.0
        if dd < mdd:
            mdd, worst = dd, p["dates"][i + 1] if i + 1 < len(p["dates"]) else None
    digest = hashlib.sha256(json.dumps({k: round(v, 2) for k, v in sorted(meas.items())}, sort_keys=True).encode()
                            + sr.observation_digest(store, names, p["dates"]).encode()).hexdigest()[:16]
    out.update({
        "state": "MEASURED" if not unmeasured else "PARTIAL",
        "risk_window": [p["dates"][0], p["dates"][-1]], "n_weeks": p["n_weeks"],
        "risky_gbp": round(risky, 2),
        "sigma_risky_ann": round(sig, 5),
        "sigma_book_ann": round(book_sigma, 5) if book_sigma is not None else None,
        "gbp_vol_ann": round(sig * risky, 2),
        "gbp_vol_upper_bound_ann": round(upper, 2) if upper is not None else None,
        "decomposition": {
            "stock_weight_of_risky": round(sum(w[a] for a in S), 5),
            "stock_variance_share": round(vs / var, 5), "fund_variance_share": round(vf / var, 5),
            "cross_term_share": round(2 * vx / var, 5),
            "stock_risk_share_euler": round(sum(shares[a] for a in S), 5),
            "rho_stock_fund": round(rho, 4) if rho is not None else None},
        "rho1_bound": ({"sigma_risky_ann": round(bound, 5), "gbp_vol_ann": round(bound * risky, 2),
                        "overstatement_pp": round(100 * (bound - sig), 3),
                        "note": "the bound the engine's documentation used before P-D (fund cross-term rho = 1)"}
                       if bound is not None else None),
        "risk_shares": {a: round(shares[a], 5) for a in sorted(shares, key=lambda k: -shares[k])},
        "vols_ann": vols, "betas_to_broad_equity": betas,
        "broad_equity_proxy": BROAD_EQUITY_PROXY if betas else None,
        "sensitivity": sens,
        "static_book_path": {"basis": "CURRENT holdings held constant over the window (not the account's realised "
                                      "path - see wealth_record for that)",
                             "cumulative_pct": round(100 * (lvl - 1), 2), "max_drawdown_pct": round(100 * mdd, 2),
                             "trough_week": worst, "worst_week_pct": round(100 * min(path), 2)},
        "observation_digest": digest,
        "line": None,
    })
    out["line"] = render_line(out)
    return out


def render_line(m: dict) -> str:
    """One-line renderer (email s6 / Step 6.10s). A renderer only - never a second computation (R20.2)."""
    if m.get("state") == "REFUSED":
        return "Whole-ISA risk: REFUSED - %s" % m.get("why")
    d = m.get("decomposition") or {}
    b = m.get("rho1_bound") or {}
    s = ("Whole-ISA risk (%s, %d weeks to %s): volatility %.1f%% a year incl. cash (GBP %s); stocks %.1f%% of risky "
         "money, %.1f%% of risk; stock-fund correlation %.2f (rho=1 bound overstated %.1fpp); static current-book "
         "max drawdown over the window %.1f%%"
         % (m["state"], m["n_weeks"], m["risk_window"][1], 100 * (m.get("sigma_book_ann") or 0),
            format(round(m["gbp_vol_ann"]), ","), 100 * d.get("stock_weight_of_risky", 0),
            100 * d.get("stock_risk_share_euler", 0), d.get("rho_stock_fund") or float("nan"),
            b.get("overstatement_pp") or 0.0, m["static_book_path"]["max_drawdown_pct"]))
    if m.get("state") == "PARTIAL":
        s += "; UNMEASURED GBP %s (%s) - upper bound GBP %s" % (format(round(m["coverage"]["unmeasured_gbp"]), ","),
                                                               ", ".join(m["coverage"]["unmeasured"]),
                                                               format(round(m["gbp_vol_upper_bound_ann"] or 0), ","))
    return s


def engine_context(portfolio: dict, stock_store: dict, as_of: Optional[dt.date] = None, root: str = HERE,
                   series_fn=None, universe=None) -> dict:
    """What capital_decision_engine needs ONCE per run: the augmented store and the basis decision.
    basis WHOLE_BOOK_MEASURED only when every held fund qualified (so every plan is compared on one basis)."""
    as_of = _as_date(as_of)
    quals = load_fund_series(portfolio, as_of, root=root, series_fn=series_fn, universe=universe)
    bad = {t: q.get("why") for t, q in quals.items() if q.get("state") not in ("QUALIFIED", "QUALIFIED_WITH_CAVEAT")}
    try:
        import scoring_config as _sc
        on = bool(getattr(_sc, "WHOLE_BOOK_RISK_METRIC", False))
    except Exception:                                                   # noqa: BLE001
        on = False
    if not on:
        basis, why = "STOCK_SLEEVE_ONLY", "scoring_config.WHOLE_BOOK_RISK_METRIC is False (rollback)"
    elif bad:
        basis, why = "STOCK_SLEEVE_ONLY", "fund series not qualified: %s" % "; ".join("%s: %s" % kv for kv in bad.items())
    else:
        basis, why = "WHOLE_BOOK_MEASURED", "every held fund qualified (%d)" % len(quals)
    aug = augmented_store(stock_store, quals)
    return {"basis": basis, "why": why, "store": aug if basis == "WHOLE_BOOK_MEASURED" else None,
            "aug_store": aug, "fund_states": {t: q.get("state") for t, q in quals.items()}, "memo": {}, "beta_memo": {}}


def beta_of(store: dict, key: str, memo: Optional[dict] = None) -> Optional[dict]:
    """Dimson (lag-1) beta of `key` to the broad-equity proxy on their common window; None when unmeasurable
    (no series or < RISK_MIN_WEEKS common weeks) - the stress rule then falls back to the uniform fall (R2.10)."""
    if memo is not None and key in memo:
        return memo[key]
    sr = _sr()
    out = None
    try:
        if sr._levels(store, key) and sr._levels(store, BROAD_EQUITY_PROXY):
            d = sr.window_dates(store, [key, BROAD_EQUITY_PROXY])
            _, mn = sr._window_policy()
            if sr.n_returns(d) >= mn:
                x, m = sr.returns_on(store, key, d), sr.returns_on(store, BROAD_EQUITY_PROXY, d)
                vm = sr._cov(m, m)
                if vm > 0:
                    b = (sr._cov(x, m) + sr._cov(x[1:], m[:-1]) + sr._cov(x[:-1], m[1:])) / vm
                    out = {"beta_dimson": round(b, 4), "n_weeks": sr.n_returns(d)}
    except Exception:                                                   # noqa: BLE001
        out = None
    if memo is not None:
        memo[key] = out
    return out


def plan_annotation(wbx: dict, portfolio: dict, stock_legs=(), fund_trim_gbp: float = 0.0, nav: Optional[float] = None) -> dict:
    """capital_decision_engine's per-plan whole-book view (B08-R03 + B08-R09):
      whole_book  post-action WHOLE-ISA GBP volatility (stock legs at their signed GBP; a fund-trim leg taken PRO RATA
                  across the held funds - the recall leg picks the donor funds only AFTER selection, so pro rata is the
                  neutral comparison basis, stated);
      risk_metric the comparator's risk dimension: whole-book GBP vol when wbx.basis is WHOLE_BOOK_MEASURED (None if
                  that plan cannot be measured -> no dominance claim), else the caller keeps stock-sleeve GBP vol;
      appetite    whole_book_stress.guard on the pre/post book (REVIEW_REQUIRED never refuses or sells)."""
    legs = tuple(sorted((t, round(float(a), 2)) for t, a in (stock_legs or ())))
    key = (legs, round(float(fund_trim_gbp or 0.0), 2))
    memo = wbx.setdefault("memo", {})
    if key in memo:
        return memo[key]
    funds = [f for f in (portfolio.get("funds") or []) if f.get("ticker") and float(f.get("value_gbp") or 0) > 0]
    ft = float(fund_trim_gbp or 0.0)
    tot_f = sum(float(f["value_gbp"]) for f in funds)
    fund_legs = [(f["ticker"], -ft * float(f["value_gbp"]) / tot_f) for f in funds] if ft > 0.005 and tot_f > 0 else []
    out = {"basis": wbx.get("basis"), "fund_trim_gbp": round(ft, 2),
           "fund_trim_model": "PRO_RATA_ACROSS_HELD_FUNDS (donor funds are chosen after selection)" if fund_legs else None}
    st = wbx.get("store")
    if st is not None:
        p = plan_gbp_vol(st, portfolio, legs, fund_legs, memo=wbx.setdefault("cov_memo", {}))
        out["whole_book"] = p
        out["risk_metric_gbp"] = p.get("gbp_vol") if p.get("state") == "MEASURED" else None
    aug = wbx.get("aug_store")
    if aug is not None:
        try:
            import whole_book_stress as _wbs
            pre, _c = book_values(portfolio)
            post, _c2 = book_values(portfolio, legs, fund_legs)
            bm = wbx.setdefault("beta_memo", {})
            betas = {k: beta_of(aug, k, bm) or {} for k in set(pre) | set(post)}
            nv = float(nav or ((portfolio.get("summary") or {}).get("total_value_gbp")) or 0.0)
            out["appetite_guard"] = _wbs.guard(pre, post, betas, nv)
        except Exception as exc:                                        # noqa: BLE001
            out["appetite_guard"] = {"state": "UNAVAILABLE", "why": "%s: %s" % (type(exc).__name__, exc)}
    memo[key] = out
    return out


# ────────────────────────────────────────────────────────────────────────────── selftest
def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %s" % detail))
        if not cond:
            fails.append(name)

    import random
    rnd = random.Random(7)
    start = dt.date(2023, 1, 6)
    fr = [start + dt.timedelta(days=7 * i) for i in range(170)]
    mkt = [rnd.gauss(0.002, 0.02) for _ in fr]

    def lv(beta, idio, seed):
        r = random.Random(seed)
        x, out = 100.0, {}
        for i, f in enumerate(fr):
            if i:
                x *= 1 + beta * mkt[i] + r.gauss(0, idio)
            out[f.isoformat()] = {"gbp": x}
        return out
    store = {"names": {"TECH": {"currency": "USD", "total_return_basis": True, "observations": lv(2.0, 0.02, 1)},
                       "ENER": {"currency": "USD", "total_return_basis": True, "observations": lv(0.0, 0.04, 2)}}}

    def daily(beta, idio, seed, gap=None):
        r = random.Random(seed)
        x, s = 100.0, []
        for i, f in enumerate(fr):
            if i:
                x *= 1 + beta * mkt[i] + r.gauss(0, idio)
            if gap and gap[0] <= i < gap[1]:
                continue
            s.append((f - dt.timedelta(days=0), x))           # Friday NAV
        return s
    uni = {"VUAG": {"isin": "IE00BFMXXD54", "yf_symbol": "VUAG.L", "acc_or_inc": "acc"},
           "TECHF": {"isin": "IE0000000001", "yf_symbol": "X", "acc_or_inc": "acc"}}
    ser = {"VUAG": daily(1.0, 0.002, 3), "TECHF": daily(1.8, 0.01, 4)}
    pf = {"summary": {"total_value_gbp": 110000.0}, "cash": {"value_gbp": 10000.0},
          "stocks": [], "funds": [{"ticker": "VUAG", "value_gbp": 60000.0}, {"ticker": "TECHF", "value_gbp": 40000.0}]}
    asof = fr[-1] + dt.timedelta(days=1)
    m = measure(pf, store, asof, series_fn=lambda t, u: ser[t], universe=uni)
    ok("measures a funds-only book", m["state"] == "MEASURED" and m["n_weeks"] >= 52, m.get("why"))
    ok("book sigma includes cash dilution", abs(m["sigma_book_ann"] - m["sigma_risky_ann"] * 100000 / 110000) < 1e-4)
    # MUST-FIRE (the P-D point): same stock-sleeve risk, different whole-book risk
    e = engine_context(pf, store, asof, series_fn=lambda t, u: ser[t], universe=uni)
    st = e["store"] or augmented_store(store, load_fund_series(pf, asof, series_fn=lambda t, u: ser[t], universe=uni))
    a = plan_gbp_vol(st, pf, [("TECH", 5000.0)])
    b = plan_gbp_vol(st, pf, [("ENER", 5000.0)])
    sr = _sr()
    sa = sr.sigma(store, {"TECH": 1.0}) * 5000
    sb = sr.sigma(store, {"ENER": 1.0}) * 5000
    ok("must-fire: the energy name adds less WHOLE-book risk than the tech name", b["gbp_vol"] < a["gbp_vol"],
       (a, b))
    ok("must-fire: ... although its STOCK-sleeve risk is not lower (stock-only ranking would disagree)", sb >= sa * 0.8,
       (sa, sb))
    # negative controls
    ser2 = {"VUAG": daily(1.0, 0.002, 3), "TECHF": daily(1.8, 0.01, 4)[:-5]}
    q = load_fund_series(pf, asof, series_fn=lambda t, u: ser2[t], universe=uni)
    ok("negative control: a fund 5 Fridays stale is REFUSED (STALE_INPUT), never measured",
       q["TECHF"]["state"] == "REFUSED" and "STALE_INPUT" in q["TECHF"]["why"])
    m2 = measure(pf, store, asof, series_fn=lambda t, u: ser2[t], universe=uni)
    ok("negative control: an unmeasured fund makes the book PARTIAL with a named GBP and an upper bound",
       m2["state"] == "PARTIAL" and m2["coverage"]["unmeasured"].get("FUND_TECHF") == 40000.0
       and m2["gbp_vol_upper_bound_ann"] > m2["gbp_vol_ann"])
    e2 = engine_context(pf, store, asof, series_fn=lambda t, u: ser2[t], universe=uni)
    ok("negative control: the engine basis falls back to STOCK_SLEEVE_ONLY when any fund is unqualified",
       e2["basis"] == "STOCK_SLEEVE_ONLY" and e2["store"] is None)
    q3 = load_fund_series(pf, asof, series_fn=lambda t, u: ser[t], universe={"VUAG": uni["VUAG"]})
    ok("negative control: a fund with no declared identity is REFUSED (no inferred join)",
       q3["TECHF"]["state"] == "REFUSED")
    lvq = fund_weekly_levels([(dt.date(2026, 10, 12), 1.0), (dt.date(2026, 10, 14), 2.0), (dt.date(2026, 10, 18), 9.0)])
    ok("weekly sampling: last NAV Mon..Fri, weekend NAV never re-dated", lvq == {"2026-10-16": 2.0}, lvq)
    gapped = fund_weekly_levels([(dt.date(2026, 10, 2), 1.0), (dt.date(2026, 10, 16), 1.1)])
    ok("weekly sampling: an absent week stays absent (never forward-filled)", "2026-10-09" not in gapped)
    if verbose:
        print("whole_book_risk selftest: %d FAIL" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
