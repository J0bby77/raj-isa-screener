#!/usr/bin/env python3
"""
winners_backtest.py — calibration aid for the S5 forward-eligibility floor (§4).
Takes a screen output (full_data CSV with ticker/part_a_score/forward_axis_score), fetches
1m/3m/6m trailing price returns, and reports returns by Part A band + forward-axis band, the
top-quartile WINNERS' Part A/forward profile, and how many winners each candidate floor would
admit vs MISS. Run it on multiple universes (SP500, NASDAQ, ...) and multiple months to calibrate
the floor across REGIMES before locking — a single snapshot is regime-biased (e.g. 24-Jun-2026 was
a semis-led rally → forward-axis dominated; a value/defensive regime may favour high Part A).

Usage:
  python3 winners_backtest.py --full-data 20260624_NASDAQ_full_data.csv [--shm /dev/shm/pylibs]
  python3 winners_backtest.py --full-data <csv> --periods 21,63,126 --out backtest_nasdaq_jun.csv
Run on Composio remote (fetch-resilient, off-peak) for big universes; local for small.
"""
import argparse, csv, glob, os, sys, statistics as st, collections, warnings
warnings.filterwarnings("ignore")

def ret(series, days):
    s = series.dropna()
    if len(s) < days + 1:
        return None
    return round(100 * (s.iloc[-1] / s.iloc[-days - 1] - 1), 1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-data", required=True, help="screen full_data CSV (ticker,part_a_score,forward_axis_score)")
    ap.add_argument("--periods", default="21,63,126", help="trailing trading-day windows (≈1m,3m,6m)")
    ap.add_argument("--floors", default="8,10,12,14,16", help="candidate Part A floors to test")
    ap.add_argument("--fwd-gate", type=float, default=50.0, help="forward-axis level treated as 'confirmed'")
    ap.add_argument("--shm", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.shm and os.path.isdir(a.shm):
        sys.path.insert(0, a.shm)
    import yfinance as yf

    path = a.full_data if os.path.exists(a.full_data) else (glob.glob(a.full_data) or [None])[0]
    rows = [(x["ticker"], int(x["part_a_score"]), float(x.get("forward_axis_score") or 0))
            for x in csv.DictReader(open(path)) if x.get("part_a_score", "").lstrip("-").isdigit()]
    tk = [t for t, _, _ in rows]
    periods = [int(p) for p in a.periods.split(",")]
    px = yf.download(tk, period="1y", progress=False, auto_adjust=True)["Close"]

    data = []  # (ticker, part_a, fwd, {period: ret})
    for t, pa, fa in rows:
        try:
            s = px[t]
        except Exception:
            continue
        rr = {p: ret(s, p) for p in periods}
        if rr.get(periods[1]) is not None:  # require the middle (3m) window
            data.append((t, pa, fa, rr))
    print(f"names with returns: {len(data)}/{len(rows)}  (universe from {os.path.basename(path)})")
    main_p = periods[1]

    def band_pa(pa):
        return "<10" if pa < 10 else "10-13" if pa < 14 else "14-21" if pa < 22 else ">=22"

    print(f"\n=== {main_p}-day return by Part A band ===")
    buckets = collections.defaultdict(list)
    for d in data:
        buckets[band_pa(d[1])].append(d)
    for b in ["<10", "10-13", "14-21", ">=22"]:
        sub = buckets.get(b, [])
        if sub:
            r = [d[3][main_p] for d in sub]
            print(f"  PartA {b:6}: n={len(sub):3} | mean {st.mean(r):6.1f} median {st.median(r):6.1f} | fwd-axis mean {st.mean([d[2] for d in sub]):.1f}")

    data.sort(key=lambda d: -(d[3][main_p]))
    n = len(data); win = data[:max(1, n // 4)]
    cut = win[-1][3][main_p]
    print(f"\n=== WINNERS (top quartile, {main_p}d ret >= {cut}%) — n={len(win)} ===")
    wpa = collections.Counter(band_pa(d[1]) for d in win)
    print("  by Part A band:", dict(wpa))
    print(f"  with fwd-axis >= {a.fwd_gate}: {sum(1 for d in win if d[2] >= a.fwd_gate)}/{len(win)}")
    print("\n=== how many WINNERS each Part A floor would ADMIT vs MISS (forward gate held) ===")
    for fl in [int(x) for x in a.floors.split(",")]:
        admit = [d for d in win if d[1] >= fl and d[2] >= a.fwd_gate]
        miss = [d for d in win if d[1] < fl and d[2] >= a.fwd_gate]
        print(f"  floor {fl:2}: admits {len(admit):2} winners, MISSES {len(miss):2} forward-confirmed winners"
              + (f"  e.g. {[d[0] for d in miss][:5]}" if miss else ""))
    print("\n  top 15 winners:", [(d[0], f"PA{d[1]}", f"F{int(d[2])}", f"{d[3][main_p]}%") for d in win[:15]])

    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["ticker", "part_a", "forward_axis"] + [f"ret_{p}d" for p in periods])
            for t, pa, fa, rr in data:
                w.writerow([t, pa, fa] + [rr[p] for p in periods])
        print(f"\nwrote {a.out}")

if __name__ == "__main__":
    main()
