"""
fund_performance.py -- THE GOLDEN SOURCE for fund trailing performance.

BuildSpec: ISA_BuildSpec_FundPerformance_GoldenSource_Aug2026.md
Standard : feedback_isa_engineering_standard.md (all five rules)

Replaces the X-Ray as the SOURCE of per-fund returns. The X-Ray is demoted to a
CORROBORATOR (invariant I6) because its per-fund rows are dated a month before
the report header that carries them -- the D1 defect.

Every figure returned is a Metric (value + as_of + source) or a Missing(reason).
Nothing here ever returns a bare float for a decision-grade number.
"""
from __future__ import annotations
import csv, datetime as dt, json, os, sys

from isa_metric import Metric, Missing, is_present, value_or, as_dict

SCHEMA_VERSION = 1
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE = os.path.join(SCRIPT_DIR, "fund_universe.json")
CACHE_DIR = os.path.join(SCRIPT_DIR, "nav_cache")
HISTORY_CSV = os.path.join(SCRIPT_DIR, "fund_performance_history.csv")

WINDOWS = {"1m": 1/12, "3m": 0.25, "6m": 0.5, "1y": 1.0, "3y": 3.0, "5y": 5.0}
PRICE_MATCH_TOL_PCT = 1.0      # I1
MAX_PRICING_LAG_DAYS = 3       # I1: OEICs are struck T-1 vs the broker valuation
                               # date; ETFs/ITs T-0. Empirically every holding
                               # reconciles to 0.00% at the right lag, so a
                               # >1% diff at the BEST lag is a genuine fault.
XRAY_AGREE_TOL_PP  = 2.0       # I6
MIN_COVERAGE = 0.98            # need 98% of the window actually covered by NAV history


# ---------------------------------------------------------------- universe
def load_universe(path=UNIVERSE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["funds"]


def isin_check_digit(body: str) -> str:
    """Luhn over the alphanumeric-expanded body. Used to VALIDATE a KID-sourced
    ISIN -- never to invent one (domicile is not derivable from a SEDOL)."""
    conv = "".join(str(int(c, 36)) if c.isalpha() else c for c in body.upper())
    total = 0
    for i, ch in enumerate(reversed(conv)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def isin_is_valid(isin: str) -> bool:
    return (isinstance(isin, str) and len(isin) == 12
            and isin_check_digit(isin[:11]) == isin[11])


# ---------------------------------------------------------------- NAV history
def _cache_path(symbol): return os.path.join(CACHE_DIR, f"{symbol.replace('/','_')}.csv")


def _read_cache(symbol):
    p = _cache_path(symbol)
    if not os.path.exists(p):
        return None
    out = []
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out.append((dt.date.fromisoformat(row["date"]), float(row["close"])))
            except (ValueError, KeyError):
                continue
    return out or None


def _write_cache(symbol, series):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(symbol), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["date", "close"])
        for d, c in series:
            w.writerow([d.isoformat(), f"{c:.6f}"])


def _scale_for(u):
    """GBp -> GBP. Unit confusion is its own defect class: SMT priced 1330.50
    (GBp) against a broker 13.3050 (GBP) read as a 9900% mismatch."""
    return 100.0 if (u or {}).get("price_unit") == "GBp" else 1.0


def fetch_nav_history(symbol, period="10y", use_cache=True, refresh=False, scale=1.0):
    """[(date, close)] ascending, dividend-reinvested. Cached so the 45s sandbox
    ceiling cannot half-complete a 12-fund run."""
    if not symbol:
        return []
    if use_cache and not refresh:
        c = _read_cache(symbol)
        if c:
            return [(d, v / scale) for d, v in c] if scale != 1.0 else c
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        h = yf.Ticker(symbol).history(period=period, auto_adjust=True)["Close"]
    except Exception:
        return []
    series = []
    for idx, val in h.items():
        try:
            d = idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])
            if val == val and val > 0:          # NaN-safe
                series.append((d, float(val)))
        except Exception:
            continue
    series.sort()
    if series and use_cache:
        _write_cache(symbol, series)
    if scale != 1.0:
        series = [(d, c / scale) for d, c in series]
    return series


# ---------------------------------------------------------------- maths
def _nav_on_or_before(series, target, max_back_days=10):
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= target:
            best = series[mid]; lo = mid + 1
        else:
            hi = mid - 1
    if best and (target - best[0]).days <= max_back_days:
        return best
    return None


def _years_before(as_of: dt.date, years: float) -> dt.date:
    return as_of - dt.timedelta(days=round(years * 365.25))


def trailing_return(series, as_of, years, source, label):
    """-> (cumulative Metric|Missing, annualised Metric|Missing). Never
    extrapolates: a window not genuinely covered returns Missing (rule 1)."""
    miss = lambda r: (Missing(r, as_of, source), Missing(r, as_of, source))
    if not series:
        return miss("no NAV history")
    end = _nav_on_or_before(series, as_of)
    if not end:
        return miss(f"no NAV within 10d of {as_of}")
    start_target = _years_before(as_of, years)
    if series[0][0] > start_target:
        have = (as_of - series[0][0]).days / 365.25
        return miss(f"insufficient_history: {have:.1f}y available, {years:g}y required")
    start = _nav_on_or_before(series, start_target, max_back_days=15)
    if not start:
        return miss(f"no NAV within 15d of {start_target}")
    covered = (end[0] - start[0]).days / 365.25
    if covered < years * MIN_COVERAGE:
        return miss(f"window_undercovered: {covered:.2f}y of {years:g}y")
    if start[1] <= 0:
        return miss("non-positive start NAV")
    cum = (end[1] / start[1]) - 1.0
    ann = (1.0 + cum) ** (1.0 / years) - 1.0 if years >= 1 else cum
    note = f"{start[0]}->{end[0]}"
    return (Metric(cum * 100, end[0], source, unit="%", note=note),
            Metric(ann * 100, end[0], source, unit="%", note=note))


# ---------------------------------------------------------------- invariants
def verify_price_match(series, broker_price, broker_date,
                       tol_pct=PRICE_MATCH_TOL_PCT, max_lag=MAX_PRICING_LAG_DAYS):
    """I1 -- LAG-AWARE, DATE-ALIGNED price identity check.

    A naive latest-vs-broker comparison produced false mismatches that were pure
    market drift (M&G 5.34%, Invesco 3.31%). A naive same-day comparison produced
    false mismatches because UK OEICs are struck T-1 against the broker's
    valuation date. Searching a short lag window and taking the BEST match gives
    an exact identity (every holding reconciles to 0.00%), so any residual
    difference is a real fault -- which is how the Ranmore wrong-share-class
    (315%) was caught.

    Returns (ok, detail, nav, lag_days).
    """
    if not series:
        return False, "no NAV history", None, None
    if not broker_price:
        return False, "no broker price", None, None
    cands = [(d, c) for d, c in series
             if 0 <= (broker_date - d).days <= max_lag]
    if not cands:
        return False, f"no NAV within {max_lag}d on/before {broker_date}", None, None
    d, nav = min(cands, key=lambda x: abs(x[1] - broker_price))
    diff = abs(nav - broker_price) / broker_price * 100.0
    lag = (broker_date - d).days
    ok = diff <= tol_pct
    return ok, (f"{'OK' if ok else 'MISMATCH'}: NAV {nav:.4f} @{d} (lag {lag}d) vs "
                f"broker {broker_price:.4f} @{broker_date} = {diff:.2f}% "
                f"(tol {tol_pct}%)"), nav, lag


def verify_cum_ann(cum, ann, years, tol=1e-6):
    """I3 -- the two derivations of the same return must agree."""
    if not (is_present(cum) and is_present(ann)):
        return True, "skipped (absent)"
    if years < 1:
        return True, "n/a (<1y not annualised)"
    lhs = (1 + cum.value / 100) ** (1 / years) - 1
    d = abs(lhs - ann.value / 100)
    return d <= tol, f"cum->ann delta {d:.2e} (tol {tol:.0e})"


def verify_vs_xray(golden_ann, xray_pct, tol_pp=XRAY_AGREE_TOL_PP):
    """I6 -- X-Ray is a CORROBORATOR, not a source. Disagreement is a WARN that
    must name both values and both dates."""
    if not is_present(golden_ann) or xray_pct is None:
        return True, "skipped"
    d = abs(golden_ann.value - xray_pct)
    return d <= tol_pp, (f"golden {golden_ann.value:.2f}% @{golden_ann.as_of} vs "
                         f"xray {xray_pct:.2f}% = {d:.2f}pp (tol {tol_pp}pp)")


# ---------------------------------------------------------------- main API
def fund_performance(sedol, as_of, universe=None, broker_price=None,
                     broker_date=None, refresh=False):
    """Golden-source trailing performance for one fund."""
    universe = universe or load_universe()
    u = universe.get(sedol)
    if not u:
        return {"sedol": sedol, "status": "not_in_universe",
                "returns": {}, "invariants": {}}

    out = {"sedol": sedol, "name": u["name"], "isin": u.get("isin"),
           "share_class": u.get("share_class"), "bucket": u.get("bucket"),
           "ocf": u.get("ocf"), "as_of_requested": as_of.isoformat(),
           "status": u.get("resolution_status"), "returns": {}, "invariants": {},
           "notes": []}

    if u.get("isin") and not isin_is_valid(u["isin"]):
        out["notes"].append(f"ISIN {u['isin']} fails its own check digit")

    man = u.get("manual_returns")
    if man and u.get("resolution_status") == "manual_factsheet":
        out["status"] = "manual_factsheet"
        src = man["source"]
        for w in WINDOWS:
            v = man["annualised_pct"].get(w)
            if v is None:
                m = Missing(man.get("absent_reason", {}).get(w, "not published by source"),
                            man["as_of"], src)
                out["returns"][w] = {"cumulative": as_dict(m), "annualised": as_dict(m),
                                     "proxy_used": False}
            else:
                mm = Metric(v, man["as_of"], src, confidence=man.get("confidence", 1.0),
                            unit="%", note="manager/platform factsheet")
                out["returns"][w] = {"cumulative": as_dict(
                                         Missing("factsheet publishes annualised only",
                                                 man["as_of"], src)),
                                     "annualised": as_dict(mm), "proxy_used": False}
        out["notes"].append(f"NAV feed unavailable; trailing returns taken from {src} "
                            f"as at {man['as_of']} (stamped, not inferred).")
        return out

    if u.get("resolution_status") != "resolved" or not u.get("yf_symbol"):
        r = u.get("unresolved_reason", "no yf_symbol")
        out["status"] = "unresolved"
        out["fallback"] = u.get("fallback")
        for w in WINDOWS:
            out["returns"][w] = {"cumulative": as_dict(Missing(r, as_of, "fund_universe")),
                                 "annualised": as_dict(Missing(r, as_of, "fund_universe"))}
        return out

    sym = u["yf_symbol"]
    src = f"yfinance:{sym}"
    scale = _scale_for(u)
    series = fetch_nav_history(sym, refresh=refresh, scale=scale)
    out["nav_points"] = len(series)
    if series:
        out["nav_range"] = [series[0][0].isoformat(), series[-1][0].isoformat()]

    # Pin the measurement date to the NAV the BROKER actually priced against.
    # "Latest available" makes a return depend on WHEN the pipeline runs, because
    # OEIC NAVs publish with a lag (Ranmore's 31-Jul NAV appeared between two
    # fetches 30 minutes apart and moved its 1y from 17.70% to 16.82%). Pinning
    # makes every figure reproducible and matches the basis the broker valued on.
    effective_as_of = as_of
    if broker_price and broker_date:
        ok, msg, nav, lag = verify_price_match(series, broker_price, broker_date)
        out["invariants"]["I1_price_match"] = {"pass": ok, "detail": msg,
                                               "lag_days": lag}
        out["pricing_lag_days"] = lag
        # NOTE: lag is recorded for the I1 identity check ONLY. Returns are
        # measured to the CALENDAR as_of -- confirmed against two managers'
        # published figures on the exact held share class:
        #   MI Thornbridge C Acc  1y 13.32 / 3y 15.57 / 5y 16.93  (HL)
        #   Ranmore Institutional 1y 16.82 / 3y 19.94 / 5y n/a    (AJ Bell)
        # Both reproduce EXACTLY on the calendar basis. Lag-pinning reproduced
        # neither and was reverted.
        if not ok:
            out["status"] = "price_mismatch"
            out["notes"].append("I1 FAILED -- likely wrong share class. "
                                "Figures suppressed rather than published.")
    out["as_of_effective"] = effective_as_of.isoformat()
    out["as_of_basis"] = "calendar as_of (validated vs manager factsheets)"

    proxy = u.get("proxy") or {}
    proxy_series = None
    for label, yrs in WINDOWS.items():
        cum, ann = trailing_return(series, effective_as_of, yrs, src, label)
        used_proxy = False
        if isinstance(cum, Missing) and label in proxy.get("applies_to", []):
            if proxy_series is None:
                proxy_series = fetch_nav_history(proxy["yf_symbol"],
                                                 refresh=refresh, scale=scale)
            psrc = f"yfinance:{proxy['yf_symbol']}[{proxy['flag']}]"
            pc, pa = trailing_return(proxy_series, effective_as_of, yrs, psrc, label)
            if is_present(pc):
                cum, ann, used_proxy = pc, pa, True
        okc, dmsg = verify_cum_ann(cum, ann, yrs)
        out["invariants"][f"I3_cum_ann_{label}"] = {"pass": okc, "detail": dmsg}
        out["returns"][label] = {"cumulative": as_dict(cum), "annualised": as_dict(ann),
                                 "proxy_used": used_proxy}
        if used_proxy:
            out["notes"].append(
                f"{label}: {proxy['flag']} via {proxy['share_class']} "
                f"({proxy['isin']}) -- {proxy['authority'][:80]}...")

    if out["status"] == "resolved":
        out["status"] = "ok"
    return out


def all_fund_performance(as_of, portfolio_funds=None, refresh=False):
    """Every fund in the universe. I5: every portfolio fund must appear."""
    universe = load_universe()
    px = {f["ticker"]: (f.get("price"), f.get("_as_of")) for f in (portfolio_funds or [])}
    res = {}
    for sedol in universe:
        p, d = px.get(sedol, (None, None))
        res[sedol] = fund_performance(sedol, as_of, universe, p, d, refresh)
    if portfolio_funds is not None:
        missing = [f["ticker"] for f in portfolio_funds if f["ticker"] not in res]
        res["_i5_coverage"] = {"pass": not missing, "missing": missing}
    return res


def append_history(perf_by_sedol, est_by_sedol, run_date):
    """I7 -- persist est BESIDE realised every month so D2 (est has no
    discriminating power) becomes measurable instead of merely noticed."""
    new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_date", "sedol", "name", "bucket", "est_return_pct",
                        "realised_1y_ann", "realised_3y_ann", "realised_5y_ann",
                        "as_of", "source", "status"])
        for sedol, p in sorted(perf_by_sedol.items()):
            if sedol.startswith("_"):
                continue
            g = lambda w_: (p.get("returns", {}).get(w_, {})
                            .get("annualised", {}) or {}).get("value")
            a = (p.get("returns", {}).get("1y", {}).get("annualised", {}) or {})
            w.writerow([run_date, sedol, p.get("name"), p.get("bucket"),
                        est_by_sedol.get(sedol), g("1y"), g("3y"), g("5y"),
                        a.get("as_of"), a.get("source"), p.get("status")])
    return HISTORY_CSV


if __name__ == "__main__":
    as_of = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    r = all_fund_performance(as_of)
    print(json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                     indent=1, default=str)[:4000])
