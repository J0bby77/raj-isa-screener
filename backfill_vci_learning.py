#!/usr/bin/env python3
"""
backfill_vci_learning.py — one-shot recovery of VCI learning observations Apr–Jul 2026.

WHY: vci_learning_store.json was never written despite Run_Context_VCI_Task §5.2A mandating
VL.capture() every run. Root cause: capture was a manual per-candidate instruction inside a
time-boxed session; the Jul-2026 run's own Learning block asserts capture happened, but no store
exists on disk — the write either never ran or landed in a session sandbox that was discarded.

WHAT: reconstructs one observation per scored name per run from the ONLY durable artefacts,
the project_vci_output_{apr,may,jun,jul}_2026.md files. Every row is flagged
  source="backfill_from_output_md", precision="reconstructed"
so backfilled rows are never mistaken for live capture. Fields not recorded in the markdown
(fv_inputs, bottleneck_fv_per_share, components, vci_source_score) are left None rather than
invented — the calibration estimator already tolerates nulls, and inventing them would poison
the very calibration this is meant to enable.

IDEMPOTENT: capture() upserts on (run_date, ticker). Re-running changes nothing.
"""
import sys, os, json

def rows():
    # (run_date, ticker, theme, asset_structure, acs, signal_count, decision, price, notes)
    A = "2026-04-12"; M = "2026-05-10"; J = "2026-06-14"; L = "2026-07-12"
    return [
        # ---- April (first run; three names scored, ACS recorded in output) ----
        dict(run_date=A, ticker="RXRX",  theme="4",  acs=42, decision="not_added"),
        dict(run_date=A, ticker="KRMN",  theme="6",  acs=39, decision="not_added"),
        dict(run_date=A, ticker="ONT.L", theme="4",  acs=74, decision="watchlist"),
        # ---- May ----
        dict(run_date=M, ticker="RXRX",  theme="4",  acs=60, decision="watchlist",
             note="Part B floor applied (B1+B2=3/6); Sep re-score trigger"),
        dict(run_date=M, ticker="ONT.L", theme="4",  acs=76, signal_count=5, decision="active_buy",
             note="NVIDIA-class 5/6; upgraded from 73"),
        dict(run_date=M, ticker="KRMN",  theme="6",  acs=40, decision="discard"),
        dict(run_date=M, ticker="ABCL",  theme="4",  acs=78, signal_count=6, decision="watchlist",
             note="NVIDIA-class 6/6; upgraded from 74"),
        # ---- June ----
        dict(run_date=J, ticker="ALAB",  theme="1",  acs=68, decision="removed_graduating",
             note="Exception Track $62.9bn, below 70 watchlist min"),
        dict(run_date=J, ticker="CRDO",  theme="1",  acs=60, decision="watchlist_monitor",
             note="Part B floor; 94% 3yr position"),
        dict(run_date=J, ticker="RKLB",  theme="6",  acs=None, decision="not_advanced",
             note="Exception Track criterion 3 fail"),
        dict(run_date=J, ticker="ONT.L", theme="4",  acs=76, signal_count=5, decision="active_buy"),
        dict(run_date=J, ticker="ABCL",  theme="4",  acs=78, signal_count=6, decision="watchlist",
             note="catalyst alert"),
        dict(run_date=J, ticker="RXRX",  theme="4",  acs=60, decision="watchlist", note="floor-capped"),
        dict(run_date=J, ticker="POET",  theme="1",  acs=60, decision="watchlist_monitor"),
        dict(run_date=J, ticker="LWLG",  theme="1",  acs=57, decision="monitor"),
        dict(run_date=J, ticker="OUST",  theme="5",  acs=60, decision="monitor",
             note="watchlist full; ties lowest member"),
        dict(run_date=J, ticker="AEVA",  theme="5",  acs=53, decision="monitor",
             note="12-24mo liquidity runway vs 2027 SOP"),
        # ---- July (full Stage-0 breadth restored; 170-name prescore) ----
        dict(run_date=L, ticker="QBTS",  theme="10", acs=70, signal_count=5, decision="watchlist_add",
             price=20.09, note="sovereign co-investment anchor"),
        dict(run_date=L, ticker="CRSP",  theme="4",  acs=64, signal_count=3, decision="watchlist_add",
             price=53.35),
        dict(run_date=L, ticker="RGTI",  theme="10", acs=63, signal_count=4, decision="watchlist_add",
             price=16.54, note="B1+B2=3 borderline floor — verify next run"),
        dict(run_date=L, ticker="INFQ",  theme="10", acs=61, signal_count=4, decision="watchlist_add",
             price=11.17, note="thin listing history"),
        dict(run_date=L, ticker="MP",    theme="8",  acs=58, signal_count=3, decision="not_added",
             price=52.21, note="Part B floor; strategic bottleneck not platform"),
        dict(run_date=L, ticker="UUUU",  theme="8",  acs=52, decision="not_added", price=13.58),
        dict(run_date=L, ticker="USAR",  theme="8",  acs=None, decision="discard", price=18.48,
             note="Part A 7/22; pre-inflection override unavailable (Basic Materials)"),
        dict(run_date=L, ticker="IDR",   theme="8",  acs=None, decision="not_added", price=30.76,
             note="Part A 14/18 highest; B1=0 no platform"),
        dict(run_date=L, ticker="CRDO",  theme="1",  acs=63, decision="removed_graduated",
             note="asymmetry spent; FV/price ~0.17"),
        dict(run_date=L, ticker="POET",  theme="1",  acs=60, decision="displaced_monitor"),
        dict(run_date=L, ticker="RXRX",  theme="4",  acs=60, decision="watchlist",
             note="pre-inflection override; Sep re-score due"),
        # held sleeve, monitored this run
        dict(run_date=L, ticker="ABCL",  theme="4",  acs=78, signal_count=6, decision="held"),
        dict(run_date=L, ticker="ONT.L", theme="4",  acs=76, signal_count=5, decision="held"),
        # Part-A-only, ACS pending
        dict(run_date=L, ticker="LAES",  theme="10", acs=None, decision="monitor_partA_only"),
        dict(run_date=L, ticker="NTLA",  theme="4",  acs=None, decision="monitor_partA_only"),
        dict(run_date=L, ticker="EDIT",  theme="4",  acs=None, decision="monitor_partA_only"),
    ]

def main():
    ia = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.path.insert(0, ia)
    import vci_learning as VL, scoring_config as cfg
    store = cfg.VCI_LEARNING_STORE_PATH
    n = 0
    for r in rows():
        obs = dict(r)
        obs.setdefault("asset_structure", "single_asset")
        obs["fv_source"] = "not_recorded"
        obs["source"] = "backfill_from_output_md"
        obs["precision"] = "reconstructed"
        VL.capture(obs, store)
        n += 1
    # persist provenance for the whole backfill
    with open(store) as fh:
        s = json.load(fh)
    s["_backfill_note"] = (
        "Rows with source=backfill_from_output_md were reconstructed 26-Jul-2026 from "
        "project_vci_output_{apr,may,jun,jul}_2026.md after vci_learning_store.json was found "
        "never to have been written. ACS/decision/signal_count/price are as recorded in those "
        "outputs. fv_inputs, bottleneck_fv_per_share, components and vci_source_score were not "
        "recorded in the markdown and are null by design — not estimated. Outcomes are unlabelled: "
        "no VCI catalyst had resolved as at the backfill date."
    )
    s["_schema_version"] = 1
    with open(store, "w") as fh:
        json.dump(s, fh, indent=2, default=str)
    print(f"captured {n} observations -> {store}")
    print(json.dumps(VL.verify_stores() if hasattr(VL, "verify_stores") else {}, indent=2, default=str))

if __name__ == "__main__":
    main()
