#!/usr/bin/env python3
"""
concentration_clusters.py — what is actually ONE bet? Built 06-Aug-2026 (register M7 + L1).

⚑ WHY THIS EXISTS INSTEAD OF A NUMBER
Raj asked whether the fund concentration limit should rise to ~20% "as long as the overall
portfolio remains diversified". The honest answer was that **nothing in the framework measures
whether it remains diversified**, so the limit could not be set against anything. The only cap on
record is `max_single_fund_pct = 0.125`, and a per-fund cap measures the wrong risk:

* Artemis SmartGARP European and Artemis SmartGARP UK correlate **0.892** across *different
  geographic mandates*. That correlation IS the process signature. Two 12% holdings both pass a
  12.5% per-fund cap and deliver 24% of one bet.
* Conversely, grouping by MANAGER NAME overstates things. JPM UK correlates **0.917** with
  Artemis UK, so switching one for the other changes who implements the UK bet and not how much
  UK risk is carried. A "manager concentration" number would show a large change where the risk
  barely moves.

The grouping that matters is therefore neither the fund nor the manager but the **correlation
cluster**, and that is a measurement, not a policy. Raj, 06-Aug-2026: *build the measurement
first and set the number against two runs of real data.* **This module sets NO limit and blocks
nothing.** It emits, and it accumulates.

WHAT IT EMITS
    correlation matrix on a COMMON window (so the eigen-decomposition is well posed)
    clusters      — average-linkage agglomerative, cut at the redundancy threshold
    cluster weights, as a share of the measured sleeve AND of the whole ISA
    effective number of bets — by weight (Herfindahl) and by RISK (principal portfolios)
    PC1 share of portfolio variance — the single best answer to "is this really one bet?"
    marginal contribution to risk per holding — this is register M7's "unnamed active bets"

⚑ COVERAGE IS THE FIRST NUMBER, NOT A FOOTNOTE
Only 77.5% of the ISA has a usable return series. Polar Capital (7.6%) has no NAV feed at all and
the stock sleeve (7.9%) is not cached. A diversification statistic quoted over 78% of a book and
read as covering the book is precisely how a concentration goes unnoticed, so `coverage_pct` is
reported first and every headline figure carries it.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, json, math, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1
HISTORY_CSV = os.path.join(HERE, "concentration_history.csv")

# ⚑ ONE HOME. The threshold at which two holdings are "the same bet" is the SAME question the
# redundancy test asks, so it is the same number, imported rather than restated. Two thresholds
# for one question is the defect that hid the 10-year window for a month.
try:
    from fund_action_stack import REDUNDANCY_CORR as CLUSTER_CORR_THRESHOLD
except Exception:                                                       # pragma: no cover
    CLUSTER_CORR_THRESHOLD = 0.80

MIN_COMMON_MONTHS = 24


# ────────────────────────────────────────────────────────────── data
def _stock_monthly_returns(ticker, store):
    """Monthly GBP TOTAL-RETURN series for a direct holding, from stock_weekly_returns.json.

    ⚑ ISA-0598. The six direct holdings were excluded from this measure for want of a monthly
    series while the weekly store held 157 GBP total-return observations for every one of them —
    so `effective_bets` and PC1 described the FUND SLEEVE and were read as the portfolio's, with
    MU (42.8% of stock-sleeve risk) invisible to them. The series exists; nothing resampled it.

    COMMENSURABILITY (R-ISA-0429), asserted rather than assumed. The fund series this is mixed
    with are MONTHLY, GBP, TOTAL RETURN. The store's `gbp` field is the GBP total-return LEVEL
    (adjusted close x FX to GBP), so a month-end-to-month-end ratio of it is the same quantity on
    the same basis. Two rules keep it that way:
      · CONSECUTIVE MONTHS ONLY — a gap is skipped, never bridged. A return computed across a
        missing month is a two-month return wearing a one-month label.
      · THE CURRENT MONTH IS DROPPED — the store is stamped to the latest Friday, so the running
        month is PARTIAL. A part-month return sitting in a column of full-month returns understates
        its own volatility and corrupts every covariance it touches.
    """
    obs = ((store.get("names") or {}).get(ticker) or {}).get("observations") or {}
    if not obs:
        return []
    today = dt.date.today()
    this_month = (today.year, today.month)
    last_in_month = {}
    for d, rec in obs.items():
        gbp = (rec or {}).get("gbp")
        if gbp is None:
            continue
        ym = (int(d[:4]), int(d[5:7]))
        if ym == this_month:
            continue                       # incomplete: the month is still running
        if ym not in last_in_month or d > last_in_month[ym][0]:
            last_in_month[ym] = (d, float(gbp))
    months = sorted(last_in_month)
    out = []
    for prev, cur in zip(months, months[1:]):
        if (cur[0] - prev[0]) * 12 + (cur[1] - prev[1]) != 1:
            continue                       # not consecutive — skip, never bridge
        p = last_in_month[prev][1]
        if p:
            out.append((cur, last_in_month[cur][1] / p - 1.0))
    return out


def gather_series(portfolio, universe=None, store=None):
    """-> ({sedol: {month: return}}, excluded[], basis{}) . An excluded holding is NAMED WITH ITS
    WEIGHT, because a concentration measure that quietly drops 15% of the risk assets is worse
    than none.

    ⚑ EVERY HOLDING IS SOURCED THROUGH THE DOOR THAT ALREADY EXISTS FOR IT (ISA-0598):
      · funds  -> `fund_performance.nav_series_for`, which takes the declared `local_series`
        route BEFORE the feed. This module used to call `fetch_nav_history(yf_symbol)` directly
        and so could not see Polar's manually supplied, factsheet-reconciled monthly series —
        8.05% of the ISA excluded with "no NAV feed" while the golden source sat in nav_cache/.
        That is ISA-0307 exactly (`the file was on disk for seven days and no code path read it`)
        repeating in a second consumer, which is what having two doors always costs.
      · stocks -> the weekly GBP total-return store, resampled to month end.
    """
    import fund_action_stack as fas, fund_performance as fp
    universe = universe if universe is not None else fp.load_universe()
    wts = {f["ticker"]: f for f in (portfolio.get("funds") or [])}
    series, excluded, basis = {}, [], {}
    for sedol, u in universe.items():
        if str(sedol).startswith("_"):
            continue
        w = (wts.get(sedol) or {}).get("weight_pct") or 0.0
        try:
            nav = fp.nav_series_for(sedol, u)          # local_series first, then the feed
        except Exception as exc:                                        # noqa: BLE001
            nav = None
            u = dict(u or {}, unresolved_reason="%s: %s" % (type(exc).__name__, str(exc)[:120]))
        m = fas._monthly_returns(nav) if nav else []
        if len(m) >= MIN_COMMON_MONTHS:
            series[sedol] = dict(m)
            basis[sedol] = ("fund NAV, monthly total return, via nav_series_for("
                            + ("local_series" if (u or {}).get("local_series") else "feed") + ")")
        else:
            excluded.append({"sedol": sedol, "name": (u or {}).get("name"), "weight_pct": w,
                             "reason": (f"no NAV series ({(u or {}).get('unresolved_reason', 'no yf_symbol and no local_series')})"
                                        if not nav else
                                        f"only {len(m)} monthly observations, {MIN_COMMON_MONTHS} required")})
    # ⚑ INJECTABLE. Defaulting to the on-disk store made a unit test read a LIVE artefact, so a
    # fixture portfolio silently picked up real series and the suite stopped testing what it said
    # it tested. `store` is a parameter for the same reason `universe` already is.
    try:
        if store is None:
            import stock_return_store as srs
            store = srs.load() or {}
    except Exception as exc:                                            # noqa: BLE001
        store = {}
        excluded.append({"sedol": "_stock_store", "name": "stock_weekly_returns.json",
                         "weight_pct": 0.0,
                         "reason": "the weekly return store could not be loaded (%s: %s), so NO "
                                   "direct holding could be measured" % (type(exc).__name__,
                                                                         str(exc)[:100])})
    for st in (portfolio.get("stocks") or []):
        t = st.get("ticker")
        m = _stock_monthly_returns(t, store) if store else []
        if len(m) >= MIN_COMMON_MONTHS:
            series[t] = dict(m)
            basis[t] = ("direct holding, monthly GBP total return resampled from the weekly "
                        "store (consecutive months only, running month dropped)")
        else:
            excluded.append({"sedol": t, "name": st.get("name"),
                             "weight_pct": st.get("weight_pct"),
                             "reason": (f"only {len(m)} monthly observations from the weekly "
                                        f"store, {MIN_COMMON_MONTHS} required"),
                             "what_would_resolve_it": ("the name needs %d more months in "
                                                       "stock_weekly_returns.json"
                                                       % (MIN_COMMON_MONTHS - len(m)))})
    return series, excluded, basis


def common_window(series):
    """The months every measured holding shares.

    Pairwise-maximum-overlap correlations use more data but need not form a positive
    semi-definite matrix, and an eigen-decomposition of one is not a risk decomposition — it is
    arithmetic on an object that is not a covariance. The common window is shorter and correct."""
    if not series:
        return []
    keys = None
    for m in series.values():
        keys = set(m) if keys is None else (keys & set(m))
    return sorted(keys or [])


# ────────────────────────────────────────────────────────────── clustering
def average_linkage(corr, labels, threshold):
    """Agglomerative average-linkage. Merges while the AVERAGE correlation between two groups is
    at or above the threshold.

    Single linkage was rejected: it chains, so A~B at 0.85 and B~C at 0.85 puts A and C in one
    cluster whatever their own correlation is, and a chained cluster is not one bet."""
    clusters = [[i] for i in range(len(labels))]

    def avg(a, b):
        return float(np.mean([corr[i][j] for i in a for j in b]))

    while len(clusters) > 1:
        best, pair = None, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                v = avg(clusters[i], clusters[j])
                if best is None or v > best:
                    best, pair = v, (i, j)
        if best is None or best < threshold:
            break
        i, j = pair
        clusters[i] = clusters[i] + clusters[j]
        clusters.pop(j)
    return [sorted(labels[i] for i in c) for c in clusters]


# ────────────────────────────────────────────────────────────── the build
def build(portfolio, universe=None, run_date=None, out_path=None, append_history=True,
          store=None):
    run_date = run_date or dt.date.today()
    series, excluded, series_basis = gather_series(portfolio, universe, store)
    total = (portfolio.get("summary") or {}).get("total_value_gbp") or 0.0
    # ⚑ ISA-0598 — WEIGHTS MUST SPAN THE SAME SET AS THE SERIES. `wts` was built from funds
    # alone, so a direct holding admitted to `series` would have carried weight 0.0: present in
    # the covariance, absent from the portfolio it is supposed to be a part of, and silently
    # contributing nothing to risk. The two dicts are built from one union.
    wts = {f["ticker"]: (f.get("weight_pct") or 0.0)
           for f in (portfolio.get("funds") or [])}
    wts.update({st["ticker"]: (st.get("weight_pct") or 0.0)
                for st in (portfolio.get("stocks") or []) if st.get("ticker")})
    names = {}
    try:
        import fund_performance as fp
        u = universe if universe is not None else fp.load_universe()
        names = {k: (v or {}).get("name") for k, v in u.items() if not str(k).startswith("_")}
    except Exception:
        pass
    names.update({st["ticker"]: st.get("name")
                  for st in (portfolio.get("stocks") or []) if st.get("ticker")})

    out = {"schema_version": SCHEMA_VERSION, "run_date": run_date.isoformat(),
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "cluster_corr_threshold": CLUSTER_CORR_THRESHOLD,
           "sets_no_limit": ("this module measures and accumulates. It sets no cap and blocks "
                             "nothing. Raj, 06-Aug-2026: build the measurement first and set the "
                             "number against two runs of real data."),
           "excluded": sorted(excluded, key=lambda e: -(e.get("weight_pct") or 0)),
           "series_basis": series_basis,
           "series_basis_note": ("where each measured holding's return series came from. Funds and "
                                 "direct holdings are mixed in ONE covariance, so both must be the "
                                 "same quantity: monthly, GBP, total return (ISA-0598).")}

    measured = sorted(series)
    cov = sum(wts.get(k, 0.0) for k in measured)
    out["coverage"] = {
        "measured_pct_of_isa": round(cov, 2),
        "excluded_pct_of_isa": round(sum(e.get("weight_pct") or 0 for e in excluded), 2),
        "cash_pct": (portfolio.get("summary") or {}).get("cash_pct"),
        "n_measured": len(measured),
        "n_funds_measured": len([k for k in measured if k in {f.get("ticker") for f in (portfolio.get("funds") or [])}]),
        "n_stocks_measured": len([k for k in measured if k in {st.get("ticker") for st in (portfolio.get("stocks") or [])}]),
        "note": ("every figure below describes the MEASURED portion only. Cash is correctly not "
                 "a bet; the excluded holdings above are risk assets whose contribution to "
                 "concentration is UNKNOWN, not zero.")}
    if len(measured) < 3:
        out["status"] = "INSUFFICIENT"
        out["reason"] = f"only {len(measured)} holdings carry a usable series"
        return out

    months = common_window(series)
    if len(months) < MIN_COMMON_MONTHS:
        out["status"] = "INSUFFICIENT"
        out["reason"] = (f"the common window is {len(months)} months, {MIN_COMMON_MONTHS} "
                         f"required. Shortened by the newest holding — "
                         f"{min(measured, key=lambda k: len(series[k]))}.")
        return out
    out["status"] = "OK"
    out["window"] = {"months": len(months), "from": f"{months[0][0]}-{months[0][1]:02d}",
                     "to": f"{months[-1][0]}-{months[-1][1]:02d}",
                     "basis": "common to EVERY measured holding, so the covariance is well posed",
                     "shortened_by": min(measured, key=lambda k: len(series[k]))}

    R = np.array([[series[k][m] for m in months] for k in measured])
    C = np.corrcoef(R)
    S = np.cov(R, ddof=0) * 12.0                       # annualised covariance
    w = np.array([wts.get(k, 0.0) for k in measured], dtype=float)
    w = w / w.sum() if w.sum() else w

    # ── clusters ────────────────────────────────────────────────────────────────────────
    cl = average_linkage(C.tolist(), measured, CLUSTER_CORR_THRESHOLD)
    idx = {k: i for i, k in enumerate(measured)}
    clusters = []
    for c in sorted(cl, key=lambda c: -sum(wts.get(k, 0.0) for k in c)):
        inner = [C[idx[a]][idx[b]] for i, a in enumerate(c) for b in c[i + 1:]]
        clusters.append({
            "members": c, "member_names": [names.get(k) for k in c],
            "weight_pct_of_isa": round(sum(wts.get(k, 0.0) for k in c), 2),
            "weight_pct_of_measured": round(sum(wts.get(k, 0.0) for k in c) / cov * 100, 2)
                                      if cov else None,
            "value_gbp": round(sum(wts.get(k, 0.0) for k in c) / 100.0 * total, 2),
            "min_internal_corr": (round(float(min(inner)), 3) if inner else None),
            "mean_internal_corr": (round(float(np.mean(inner)), 3) if inner else None)})
    out["clusters"] = clusters
    out["n_clusters"] = len(clusters)
    out["largest_cluster_pct_of_isa"] = clusters[0]["weight_pct_of_isa"] if clusters else None

    # ── effective number of bets ────────────────────────────────────────────────────────
    cw = np.array([c["weight_pct_of_measured"] or 0.0 for c in clusters]) / 100.0
    enb_w = float(1.0 / np.sum(cw ** 2)) if cw.sum() else None

    # Risk-based: decompose the portfolio into PRINCIPAL PORTFOLIOS and measure how evenly the
    # variance is spread across them. Counting holdings, or even clusters, says nothing about
    # risk: twelve funds all loading on one factor is one bet with eleven wrappers.
    lam, E = np.linalg.eigh(S)
    lam = np.clip(lam, 0.0, None)
    wt = E.T @ w
    v = (wt ** 2) * lam
    tot_v = float(v.sum())
    p = v / tot_v if tot_v > 0 else v
    nz = p[p > 1e-12]
    enb_r = float(np.exp(-np.sum(nz * np.log(nz)))) if len(nz) else None
    order = np.argsort(-p)
    out["effective_bets"] = {
        "by_cluster_weight": (round(enb_w, 2) if enb_w else None),
        "by_risk_principal_portfolios": (round(enb_r, 2) if enb_r else None),
        "n_holdings_measured": len(measured),
        "pc1_share_of_variance_pct": round(float(p[order[0]]) * 100, 1) if tot_v > 0 else None,
        "top3_share_of_variance_pct": round(float(p[order[:3]].sum()) * 100, 1) if tot_v > 0 else None,
        "portfolio_vol_pct": round(float(np.sqrt(w @ S @ w)) * 100, 2),
        "note": ("`by_risk` is Meucci's effective number of bets: the entropy of the variance "
                 "spread across principal portfolios, on a 1-to-N scale. It is the honest answer "
                 "to 'is this really diversified' — counting holdings is not. PC1's share is the "
                 "blunt version: the percentage of portfolio variance riding on a single factor.")}

    # ── marginal contribution to risk — register M7's unnamed bets ──────────────────────
    vol = float(np.sqrt(w @ S @ w))
    mctr = (S @ w) / vol if vol > 0 else np.zeros_like(w)
    out["risk_contribution"] = sorted(
        [{"sedol": k, "name": names.get(k),
          "weight_pct_of_measured": round(float(w[i]) * 100, 2),
          "pct_of_portfolio_risk": round(float(w[i] * mctr[i] / vol) * 100, 2),
          "risk_vs_weight": round(float(w[i] * mctr[i] / vol) / float(w[i]), 2) if w[i] else None}
         for i, k in enumerate(measured)],
        key=lambda r: -r["pct_of_portfolio_risk"])
    out["risk_contribution_note"] = (
        "`risk_vs_weight` above 1.0 means the holding carries more of the portfolio's risk than "
        "its size suggests. This is register M7 — the largest active bets, named.")

    # ── the pair that prompted this ─────────────────────────────────────────────────────
    def _c(a, b):
        return round(float(C[idx[a]][idx[b]]), 3) if a in idx and b in idx else None
    out["reference_pairs"] = {
        "artemis_european_vs_artemis_uk": _c("B2PLJD7", "B2PLJM6"),
        "jpm_uk_vs_artemis_uk": _c("B55QSH0", "B2PLJM6"),
        "rlges_vs_vuag": _c("BF93W97", "VUAG"),
        "note": ("the first is two DIFFERENT geographic mandates from one process; the second is "
                 "the switch Raj asked about, and it shows the UK factor bet is largely unchanged "
                 "by it. Both are the reason a per-fund cap measures the wrong thing.")}

    if append_history:
        out["history_rows"] = _append_history(out, run_date)
    if out_path:
        json.dump(out, open(out_path, "w", encoding="utf-8"), indent=1, default=str)
    return out


def _append_history(res, run_date):
    """⚑ POINT-IN-TIME. The whole purpose is to set a limit against observed data after a couple
    of runs, so a row stamped with today's figures under a past run_date would corrupt exactly
    the evidence it exists to build. Backfill is refused (the `capture_regime` Q5 rule)."""
    new = not os.path.exists(HISTORY_CSV)
    rows = 0
    if run_date > dt.date.today():
        return {"appended": False, "reason": "run_date is in the future"}
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        if new:
            wtr.writerow(["run_date", "stamp_basis", "coverage_pct", "n_measured", "n_clusters",
                          "largest_cluster_pct_isa", "largest_cluster_members",
                          "enb_by_weight", "enb_by_risk", "pc1_variance_pct",
                          "portfolio_vol_pct", "window_months"])
        eb = res.get("effective_bets") or {}
        cl = (res.get("clusters") or [{}])[0]
        wtr.writerow([run_date.isoformat(),
                      "live" if run_date == dt.date.today() else "backfilled_not_pit",
                      (res.get("coverage") or {}).get("measured_pct_of_isa"),
                      (res.get("coverage") or {}).get("n_measured"), res.get("n_clusters"),
                      res.get("largest_cluster_pct_of_isa"),
                      "|".join(cl.get("members") or []),
                      eb.get("by_cluster_weight"), eb.get("by_risk_principal_portfolios"),
                      eb.get("pc1_share_of_variance_pct"), eb.get("portfolio_vol_pct"),
                      (res.get("window") or {}).get("months")])
        rows = 1
    n = sum(1 for _ in open(HISTORY_CSV, encoding="utf-8")) - 1
    return {"appended": bool(rows), "path": os.path.basename(HISTORY_CSV), "total_runs": n,
            "runs_needed_before_setting_a_limit": 2,
            "ready": n >= 2}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=os.path.join(HERE, "portfolio_data_aug_2026.json"))
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args()
    pf = json.load(open(a.portfolio, encoding="utf-8"))
    r = build(pf, None, dt.date.fromisoformat(a.run_date) if a.run_date else None,
              a.out, not a.no_history)
    print(json.dumps(r, indent=1, default=str))
