#!/usr/bin/env python3
"""
calibration_report.py  --  the learning-module REPORTING LOOP (Jul-26).

Joins the point-in-time score_panel.csv to realised forward returns and reports each signal's
rank-IC at 1m / 3m / 6m / 12m. Each horizon column only populates once that much time has
elapsed since the logged run, so the report fills in left-to-right as data matures.

It SURFACES evidence; it does not change weights. Run monthly.

CLI:
  python3 calibration_report.py --store score_panel.csv --asof 2026-08-01 \
      --out Calibration_Report_2026-08.md --shm /dev/shm/pylibs

Notes:
  - rank-IC computed via ranks+Pearson (no scipy dependency).
  - forward returns fetched per ticker via yfinance (cached within a run).
  - horizons in trading days: 1m=21, 3m=63, 6m=126, 12m=252.
"""
from __future__ import annotations
import argparse, os, sys, datetime, statistics as stx
import warnings; warnings.filterwarnings("ignore")

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
SIGNALS = ["forward_axis_score", "revisions_score", "source_score", "score_f_price_mom",
           "price_mom_12_1m_pct", "score_f_eps_trend", "score_f_rev_est", "score_b_est_rev",
           "revision_runway", "score_f_margin_traj", "part_a_score", "part_b_score", "total_score"]


def _rank_ic(a, b):
    import pandas as pd
    d = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(d) < 15:
        return None, len(d)
    return round(d["a"].rank().corr(d["b"].rank()), 4), len(d)


def _verdict(ics):
    long = ics.get("12m")
    if long is None:
        long = ics.get("6m")
    if long is None:
        return "no matured data yet"
    if long <= -0.02:
        return "DRAG - keep weight low"
    if long >= 0.03:
        return "working"
    if long >= 0.01:
        return "mild"
    return "weak / noisy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="score_panel.csv")
    ap.add_argument("--asof", default=datetime.date.today().isoformat())
    ap.add_argument("--out", default=None)
    ap.add_argument("--shm", default=None)
    ap.add_argument("--max_names", type=int, default=4000)
    a = ap.parse_args()
    if a.shm and os.path.isdir(a.shm):
        sys.path.insert(0, a.shm)
    import pandas as pd, yfinance as yf
    if not os.path.exists(a.store):
        print("NO_STORE %s - nothing logged yet. Report is empty until screens start logging." % a.store)
        return
    panel = pd.read_csv(a.store, parse_dates=["run_date"])
    asof = pd.Timestamp(a.asof)

    px_cache = {}
    def fwd_ret(ticker, d0, days):
        try:
            if ticker not in px_cache:
                s_ = yf.Ticker(ticker).history(period="5y")["Close"].dropna()
                if getattr(s_.index, "tz", None) is not None:
                    s_.index = s_.index.tz_localize(None)
                px_cache[ticker] = s_
            s = px_cache[ticker]
            d0 = pd.Timestamp(d0)
            if d0.tz is not None:
                d0 = d0.tz_localize(None)
            s0 = s[s.index <= d0]
            if len(s0) < 1:
                return None
            i0 = len(s0) - 1
            if i0 + days >= len(s):
                return None
            return float(s.iloc[i0 + days]) / float(s.iloc[i0]) - 1.0
        except Exception:
            return None

    results = {sig: {h: None for h in HORIZONS} for sig in SIGNALS}
    n12 = 0
    for h, days in HORIZONS.items():
        mat = panel[panel["run_date"] + pd.Timedelta(days=int(days * 1.45)) <= asof].copy()
        if mat.empty:
            continue
        mat = mat.head(a.max_names)
        mat["_fwd"] = [fwd_ret(r["ticker"], r["run_date"], days) for _, r in mat.iterrows()]
        mat = mat.dropna(subset=["_fwd"])
        if h == "12m":
            n12 = len(mat)
        for sig in SIGNALS:
            if sig in mat.columns and len(mat) >= 15:
                ic, _ = _rank_ic(mat[sig], mat["_fwd"])
                results[sig][h] = ic

    lines = ["# CALIBRATION REPORT - as at %s" % asof.date(),
             "_source: %s | %d logged rows | matured observations only_" % (a.store, len(panel)), "",
             "%-20s" % "Signal" + "".join("%9s" % ("IC@" + h) for h in HORIZONS) + "%9s  verdict" % "n(12m)",
             "-" * 92]
    for sig in SIGNALS:
        ics = results[sig]
        row = "%-20s" % sig + "".join(("%+9.4f" % ics[h]) if ics[h] is not None else "%9s" % "-" for h in HORIZONS)
        lines.append(row + "%9d  %s" % (n12, _verdict(ics)))
    lines += ["", "NOTE: blank (-) horizons have not matured yet; columns fill left-to-right as data ages.",
              "This report surfaces evidence only - weight changes remain your decision."]
    report = "\n".join(lines)
    print(report)
    if a.out:
        open(a.out, "w").write(report)
        print("\nWROTE %s" % a.out)


if __name__ == "__main__":
    main()
