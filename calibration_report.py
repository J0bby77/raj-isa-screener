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




def _read_caveats(panel, n_by_h, n_raw_by_h, cov):
    """Mandatory interpretation guard-rails. An IC table without these invites exactly the
    over-reading it exists to prevent."""
    import pandas as pd
    out = ["## How to read this (mandatory caveats)", ""]
    out.append("- **Price coverage:** " + ("; ".join(cov) if cov else "none"))
    for h in HORIZONS:
        if n_raw_by_h.get(h, 0) and n_by_h.get(h, 0) / max(1, n_raw_by_h.get(h, 0)) < 0.70:
            out.append("  - !! %s resolved only %.0f%% of matured rows. Unresolved tickers are "
                       "usually non-US listings needing an exchange suffix (.L/.DE/.SW), so this "
                       "horizon is biased toward US names. Treat as indicative only."
                       % (h, 100.0 * n_by_h.get(h, 0) / n_raw_by_h.get(h, 1)))
    # how many distinct formation dates actually contribute to the shortest populated horizon
    for h, days in HORIZONS.items():
        if n_by_h.get(h, 0):
            cutoff = pd.Timestamp(panel["run_date"].max())
            dates = panel[panel["run_date"] + pd.Timedelta(days=int(days * 1.45)) <= cutoff]["run_date"].nunique()
            out.append("- **Formation breadth at %s:** %d distinct run date(s). A single-window IC is a "
                       "read on ONE market regime, not on the signal. Do not generalise from it." % (h, dates))
            break
    out += [
        "- **Horizon mismatch:** Path A theses run 12-24 months and carry a 182-day framework "
        "min-hold. IC@1m is the LEAST decision-relevant column here; the pre-registered rule keys "
        "off IC@3m for that reason.",
        "- **A negative short-horizon IC is not evidence a signal is broken.** Momentum-loaded "
        "signals invert routinely in rotation months. Only the pre-registered gate below, on "
        "matured 3m data across multiple regimes, can support a weight change.",
        "- **No LLM judgement enters this report.** Scores are the point-in-time values the screen "
        "logged; returns are mechanical price ratios. That is deliberate - a model asked to "
        "re-score a historical date already knows what happened next.",
    ]
    return out


def _pre_registered_block(results, n_by_h):
    """Evaluate the PRE-REGISTERED calibration rule (scoring_config lines ~130-140, frozen
    12-Jul-2026, Raj-approved) and REPORT its state. Deliberately does not apply anything:
    pre-registration is worthless if the instrument that reads it can also act on it."""
    try:
        import scoring_config as cfg
    except Exception:
        return ["## Pre-registered rule", "scoring_config unavailable - rule not evaluated."]
    gate_n = int(getattr(cfg, "CALIBRATION_MIN_MATURED_3M", 200))
    n3 = n_by_h.get("3m", 0)
    ic_fwd = results.get("forward_axis_score", {}).get("3m")
    ic_rev = results.get("revisions_score", {}).get("3m")
    ic_mom = results.get("score_f_price_mom", {}).get("3m")
    out = ["## Pre-registered rule (scoring_config, frozen 12-Jul-2026)", "",
           "Gate: >= %d matured 3m observations. Current: **%d** -> **%s**"
           % (gate_n, n3, "GATE MET" if n3 >= gate_n else "GATE NOT MET - weights stay frozen"), ""]
    if n3 < gate_n:
        out += ["No weight change is permitted or implied by this report.",
                "Current weights: SOURCE_WEIGHTS=%s FORWARD_AXIS_BUCKET_WEIGHTS=%s"
                % (getattr(cfg, "SOURCE_WEIGHTS", {}), getattr(cfg, "FORWARD_AXIS_BUCKET_WEIGHTS", {}))]
        return out
    out.append("Rule (1) IC_3m(forward_axis_score) < 0.03 -> forward 0.60->0.40; +0.10 revisions, "
               "+0.05 quality, +0.05 deployability")
    out.append("        observed IC_3m(forward_axis_score) = %s -> **%s**"
               % ("n/a" if ic_fwd is None else "%+.4f" % ic_fwd,
                  "n/a" if ic_fwd is None else ("TRIGGERED" if ic_fwd < 0.03 else "not triggered")))
    out.append("Rule (2) IC_3m(revisions_score) >= 2x IC_3m(score_f_price_mom) -> bucket price "
               "0.70->0.40, margin 0.30->0.60")
    if ic_rev is None or ic_mom is None:
        out.append("        observed: n/a -> **n/a**")
    else:
        out.append("        observed revisions %+.4f vs 2x price-mom %+.4f -> **%s**"
                   % (ic_rev, 2 * ic_mom, "TRIGGERED" if ic_rev >= 2 * ic_mom else "not triggered"))
    out += ["Rule (3) any change requires a calibration changelog entry + ONE SHADOW CYCLE before live.",
            "", "**This report does not change weights.** A triggered rule is a proposal for Raj, "
            "executed only via the changelog + shadow-cycle path in rule (3)."]
    return out


def _assert_growth(store, state_path):
    """H-6-style store assertion: fail loudly if score_panel has not grown since the last report.
    A learning loop that silently stops writing is the exact failure the VCI store hit Apr-Jul 26."""
    import json
    n = 0
    if os.path.exists(store):
        with open(store, encoding="utf-8", errors="ignore") as fh:
            n = max(0, sum(1 for _ in fh) - 1)
    prev = {}
    if os.path.exists(state_path):
        try:
            prev = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            prev = {}
    last = int(prev.get("rows", 0))
    grew = n > last
    prev.update({"rows": n, "checked_at": datetime.date.today().isoformat(), "grew": grew})
    try:
        json.dump(prev, open(state_path, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass
    return grew, n, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="score_panel.csv")
    ap.add_argument("--asof", default=datetime.date.today().isoformat())
    ap.add_argument("--out", default=None)
    ap.add_argument("--shm", default=None)
    ap.add_argument("--max_names", type=int, default=4000)
    ap.add_argument("--assert-growth", dest="assert_growth", action="store_true",
                    help="fail loudly (exit 2) if score_panel.csv has not grown since last run")
    ap.add_argument("--growth-state", default="calibration_state.json")
    ap.add_argument("--price_cache", default="calibration_prices.csv",
                    help="resumable price cache; delete to force a full refetch")
    ap.add_argument("--chunk", type=int, default=400, help="tickers per download call")
    ap.add_argument("--period", default="2y", help="price history window")
    a = ap.parse_args()
    if a.shm and os.path.isdir(a.shm):
        sys.path.insert(0, a.shm)
    import pandas as pd, yfinance as yf
    if not os.path.exists(a.store):
        print("NO_STORE %s - nothing logged yet. Report is empty until screens start logging." % a.store)
        return 3
    if a.assert_growth:
        grew, now_n, last_n = _assert_growth(a.store, a.growth_state)
        print("PANEL_GROWTH rows=%d prev=%d grew=%s" % (now_n, last_n, grew))
        if not grew:
            print("PANEL_STALE - score_panel.csv did not grow since the last calibration run. "
                  "A screen is not logging (score_panel_logger). Investigate before trusting this report.")
    panel = pd.read_csv(a.store, parse_dates=["run_date"])
    asof = pd.Timestamp(a.asof)

    # ---- price panel: ONE batched download + resumable on-disk cache -------------------
    # The original per-ticker 5y history call ran ~1s/ticker: a 3,000-row panel took far longer
    # than a scheduled pre-run slot allows, which is a large part of why this report had never
    # actually been run. Batched download + on-disk cache makes it schedulable, and it honours the
    # H-5 backoff wrapper when present.
    try:
        from fetch_guard import with_backoff
    except Exception:
        def with_backoff(fn, *ar, **kw):
            return fn(*ar, **kw)

    need = sorted({str(t) for t in panel["ticker"].dropna().unique()})
    cache = pd.DataFrame()
    if a.price_cache and os.path.exists(a.price_cache):
        try:
            cache = pd.read_csv(a.price_cache, index_col=0, parse_dates=True)
        except Exception:
            cache = pd.DataFrame()
    todo = [t for t in need if t not in set(cache.columns)]
    if todo:
        chunk = todo[:a.chunk]
        px = with_backoff(yf.download, chunk, period=a.period, progress=False,
                          auto_adjust=True, threads=True)["Close"]
        if isinstance(px, pd.Series):
            px = px.to_frame(chunk[0])
        if getattr(px.index, "tz", None) is not None:
            px.index = px.index.tz_localize(None)
        cache = px if cache.empty else pd.concat([cache, px], axis=1)
        cache = cache.loc[:, ~cache.columns.duplicated()]
        if a.price_cache:
            cache.to_csv(a.price_cache)
        remaining = len(todo) - len(chunk)
        print("PRICES fetched=%d cached=%d/%d remaining=%d"
              % (len(chunk), len(cache.columns), len(need), remaining))
        if remaining > 0:
            print("NOT_DONE - price cache incomplete; call again with the same --price_cache. "
                  "Resumable by design: a 45s sandbox ceiling cannot fetch thousands of tickers "
                  "in one call.")
            return 4
    cache = cache.sort_index()

    def fwd_ret(ticker, d0, days):
        """Forward return over `days` trading days from the last close on/before d0."""
        try:
            if ticker not in cache.columns:
                return None
            s_ = cache[ticker].dropna()
            d0 = pd.Timestamp(d0)
            if getattr(d0, "tz", None) is not None:
                d0 = d0.tz_localize(None)
            s0 = s_[s_.index <= d0]
            if len(s0) < 1:
                return None
            i0 = len(s0) - 1
            if i0 + days >= len(s_):
                return None
            p0 = float(s_.iloc[i0])
            if p0 <= 0:
                return None
            return float(s_.iloc[i0 + days]) / p0 - 1.0
        except Exception:
            return None

    results = {sig: {h: None for h in HORIZONS} for sig in SIGNALS}
    n_by_h = {h: 0 for h in HORIZONS}       # matured+priced observations PER horizon
    n_raw_by_h = {h: 0 for h in HORIZONS}   # matured rows before price resolution
    truncated = {}
    for h, days in HORIZONS.items():
        mat = panel[panel["run_date"] + pd.Timedelta(days=int(days * 1.45)) <= asof].copy()
        n_raw_by_h[h] = len(mat)
        if mat.empty:
            continue
        # Deterministic SAMPLE, not head(): head() would silently pin the report to the oldest
        # rows for ever as the panel grows, so the IC would stop reflecting recent regimes.
        if len(mat) > a.max_names:
            truncated[h] = (len(mat), a.max_names)
            mat = mat.sample(n=a.max_names, random_state=20260729).sort_index()
        mat["_fwd"] = [fwd_ret(r["ticker"], r["run_date"], days) for _, r in mat.iterrows()]
        mat = mat.dropna(subset=["_fwd"])
        n_by_h[h] = len(mat)
        for sig in SIGNALS:
            if sig in mat.columns and len(mat) >= 15:
                ic, _ = _rank_ic(mat[sig], mat["_fwd"])
                results[sig][h] = ic

    lines = ["# CALIBRATION REPORT - as at %s" % asof.date(),
             "_source: %s | %d logged rows | %d run dates (%s -> %s) | matured observations only_"
             % (a.store, len(panel), panel["run_date"].nunique(),
                panel["run_date"].min().date(), panel["run_date"].max().date()), "",
             "%-20s" % "Signal" + "".join("%9s" % ("IC@" + h) for h in HORIZONS) + "  verdict",
             "-" * 92]
    for sig in SIGNALS:
        ics = results[sig]
        row = "%-20s" % sig + "".join(("%+9.4f" % ics[h]) if ics[h] is not None else "%9s" % "-" for h in HORIZONS)
        lines.append(row + "  %s" % _verdict(ics))
    lines += ["-" * 92,
              "%-20s" % "n (priced)" + "".join("%9d" % n_by_h[h] for h in HORIZONS),
              "%-20s" % "n (matured rows)" + "".join("%9d" % n_raw_by_h[h] for h in HORIZONS)]
    for h, (had, kept) in sorted(truncated.items()):
        lines.append("  ! %s horizon sampled %d of %d matured rows (--max_names); IC is a sample estimate."
                     % (h, kept, had))
    # coverage: unresolved tickers bias the read (non-US suffixes are the usual cause)
    cov = []
    for h in HORIZONS:
        if n_raw_by_h.get(h, 0):
            pct = 100.0 * n_by_h.get(h, 0) / n_raw_by_h[h]
            cov.append("%s %d/%d (%.0f%%)" % (h, n_by_h.get(h, 0), n_raw_by_h[h], pct))
    lines += ["", "NOTE: blank (-) horizons have not matured yet; columns fill left-to-right as data ages.",
              "n is reported PER horizon - a populated IC@1m says nothing about IC@12m readiness.",
              "This report surfaces evidence only - weight changes remain your decision.", ""]
    lines += _read_caveats(panel, n_by_h, n_raw_by_h, cov)
    lines += [""] + _pre_registered_block(results, n_by_h)
    report = "\n".join(lines)
    print(report)
    if a.out:
        open(a.out, "w").write(report)
        print("\nWROTE %s" % a.out)


if __name__ == "__main__":
    sys.exit(main() or 0)
