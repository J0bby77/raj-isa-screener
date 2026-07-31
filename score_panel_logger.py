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
    # WP-M (29-Jul-26): the new momentum anatomy, so calibration_report can measure each horizon
    # INDEPENDENTLY rather than only the blended axis. Registered decisions WPM-2/3/4 depend on these.
    "price_mom_1m_pct", "score_f_price_mom_1m", "score_f_price_mom_blend", "price_mom_pctl",
    "momentum_state", "timing_gate_shadow",
    # Fix Pack A8 (12-Jul-2026): stage + return-side columns so stage-exclusion and upside/E[r]
    # doctrine become measurable; door reserved for B7 regime doors. Old CSVs simply carry NaN.
    "revision_stage", "implied_upside", "expected_return_12_24m", "summary_flag", "door",
]


def _to_float(v):
    try:
        f = float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip())
        return f
    except (TypeError, ValueError):
        return None


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
        # WP3 fix (29-Jul-26): PANEL_COLS uses short names but full_data emits the canonical ones.
        # target_price / implied_upside were therefore 100% NULL across all 3,102 logged rows,
        # which silently disabled the A8 upside/E[r] doctrine measurement added on 12-Jul.
        for _short, _canon in (("target_price", "target_price_mean"),
                               ("implied_upside", "implied_upside_fv"),
                               ("source_score", "screen_source")):
            if rec.get(_short) in (None, "") and r.get(_canon) not in (None, ""):
                rec[_short] = r.get(_canon)
        rec["run_date"] = run_date
        rec["group"] = group
        rec["ticker"] = tk
        if rec.get("source_score") in (None, "") or (isinstance(rec.get("source_score"), float) and pd.isna(rec.get("source_score"))):
            rec["source_score"] = _src_score(r, part_a_max, part_b_max)
        # A8: derive implied_upside from target/current when not supplied by the row
        if rec.get("implied_upside") in (None, "") or (isinstance(rec.get("implied_upside"), float) and pd.isna(rec.get("implied_upside"))):
            cur, tgt = _to_float(rec.get("current_price")), _to_float(rec.get("target_price"))
            rec["implied_upside"] = round(tgt / cur - 1, 4) if (cur and tgt and cur > 0) else None
        # A8: summary_flag via the ONE eligibility definition (never a local reimplementation)
        if rec.get("summary_flag") in (None, ""):
            try:
                import source_score as _ss
                rec["summary_flag"] = bool(_ss.summary_eligible(r))
            except Exception:
                rec["summary_flag"] = None
        rows.append(rec)
    new = pd.DataFrame(rows, columns=PANEL_COLS)
    if os.path.exists(store):
        old = pd.read_csv(store)
        key = ["run_date", "group", "ticker"]
        # WP3 FIX (29-Jul-2026) — DATA-LOSS REGRESSION.
        # This was `drop_duplicates(subset=key, keep="last")`, i.e. a WHOLESALE row replace. When
        # backfill_score_panel.py ran on 29-Jul-2026 it appended rows reconstructed from SCORES tabs
        # (which carry no price/target/E[r]/door columns) and those SPARSE rows silently overwrote
        # 1,246 RICH live-logged rows — current_price went from 100% populated to 0% across every
        # weekly screen. Now a FIELD-WISE merge: the incoming row wins per-cell where it has a value,
        # and existing values are retained wherever the incoming cell is null. Re-running a backfill
        # can no longer destroy richer data, and a genuine re-log still updates what it actually has.
        for c in PANEL_COLS:
            if c not in old.columns:
                old[c] = None
        old = old[PANEL_COLS]
        oi = old.set_index(key)
        ni = new.set_index(key)
        oi = oi[~oi.index.duplicated(keep="last")]
        ni = ni[~ni.index.duplicated(keep="last")]
        merged = ni.combine_first(oi).reset_index()
        merged = merged[PANEL_COLS]
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
