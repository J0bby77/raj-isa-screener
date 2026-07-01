#!/usr/bin/env python3
"""
score_panel_logger.py  --  point-in-time signal panel logger (learning module, Jul-26).

Appends one row per scored name per run to score_panel.csv, idempotent per (run_date, group, ticker).
This is the dataset that lets calibration_report.py measure each signal's forward-return IC at
1m/3m/6m/12m once history accrues. Pure-additive: call at the end of every screen / energy / rerank.

CLI:
  python3 score_panel_logger.py --full_data 20260626_NASDAQ_full_data.csv --group NASDAQ \
      --run_date 2026-06-26 --store score_panel.csv

Library:
  from score_panel_logger import log_from_full_data
  log_from_full_data(df, group="NASDAQ", run_date="2026-06-26", store="score_panel.csv")
"""
from __future__ import annotations
import argparse, os, sys

PANEL_COLS = [
    "run_date", "group", "ticker", "part_a_score", "part_b_score", "total_score",
    "forward_axis_score", "revisions_score", "score_f_eps_trend", "score_f_rev_est", "score_b_est_rev",
    "revision_runway", "score_f_margin_traj", "score_f_price_mom", "price_mom_12_1m_pct",
    "est_rev_direction", "source_score", "current_price", "target_price",
]


def _src_score(row, paf=28.0, pbf=22.0):
    """The canonical screen Source Score (Jul-26 Part 1) — via source_score.source_score_for_row so the
    panel logs EXACTLY what the screen/email/rerank use. paf/pbf retained for signature compatibility."""
    try:
        import source_score as _ss
        return _ss.source_score_for_row(row)
    except Exception:
        return None


def log_from_full_data(df, group, run_date, store, part_a_max=28.0, part_b_max=22.0):
    import pandas as pd
    rows = []
    for _, r in df.iterrows():
        tk = r.get("ticker")
        if not tk:
            continue
        rec = {c: r.get(c) for c in PANEL_COLS}
        rec["run_date"] = run_date
        rec["group"] = group
        rec["ticker"] = tk
        if rec.get("source_score") in (None, "") or (isinstance(rec.get("source_score"), float) and pd.isna(rec.get("source_score"))):
            rec["source_score"] = _src_score(r, part_a_max, part_b_max)
        rows.append(rec)
    new = pd.DataFrame(rows, columns=PANEL_COLS)
    if os.path.exists(store):
        old = pd.read_csv(store)
        # idempotent: drop any existing (run_date, group, ticker) before appending
        key = ["run_date", "group", "ticker"]
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=key, keep="last")
    else:
        merged = new
    merged.to_csv(store, index=False)
    return len(new), len(merged)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full_data", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--run_date", required=True)
    ap.add_argument("--store", default="score_panel.csv")
    ap.add_argument("--part_a_max", type=float, default=28.0)
    ap.add_argument("--part_b_max", type=float, default=22.0)
    a = ap.parse_args()
    import pandas as pd
    df = pd.read_csv(a.full_data)
    n_new, n_total = log_from_full_data(df, a.group, a.run_date, a.store, a.part_a_max, a.part_b_max)
    print(f"PANEL_LOGGED group={a.group} run_date={a.run_date} rows_in={n_new} store_total={n_total} -> {a.store}")


if __name__ == "__main__":
    main()
