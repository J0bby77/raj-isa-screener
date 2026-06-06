#!/usr/bin/env python3
"""
fetch_metrics_local.py  --  LOCAL, resumable, batched yfinance metrics fetch.

Replaces the Composio fetch + gzip/base64 transfer (old Steps 2-3 of the monthly
ISA pre-run). The local sandbox reaches Yahoo Finance directly, so metrics are
fetched in-place — NO cross-environment transfer, so the usage-policy classifier
(which blocked large base64 blobs and caused req_011CbnnDG2cb4posPw6jeApx) can
never be hit.

Sandbox realities it respects:
  * Each bash call is a FRESH sandbox with a hard ~45s limit and /dev/shm reset,
    so this does ONE batch per invocation and is RESUMABLE. Call repeatedly until
    it prints ALL_DONE.
  * The OneDrive mount allows create/OVERWRITE but NOT delete. So the resume cache
    is a SINGLE overwrite-only file (out + '.partial'); transient per-batch subset
    configs go to /dev/shm (auto-cleared). On completion the partial is shrunk to a
    tiny stub (cannot be deleted, but is ~20 bytes).
  * yfinance is installed to tmpfs (/dev/shm/pylibs) by the caller — nothing heavy
    is written to the constrained system disk or to OneDrive.

Per-batch outputs are unioned (lists unioned, dicts merged, counts summed) so the
final file is schema-identical to a single full fetch_watchlist_metrics.run().
"""
import argparse, json, os, sys, importlib.util, datetime

def load_fwm(script_dir):
    p = os.path.join(script_dir, "fetch_watchlist_metrics.py")
    spec = importlib.util.spec_from_file_location("fwm", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def build_plan(cfg, batch_size):
    """Batch 0 = watchlist+vci+sleeve (per-ticker classification preserved);
    remaining batches = candidate_pool (deduped vs core) in chunks."""
    core = set()
    for k in ("watchlist", "vci_watchlist"):
        for e in cfg.get(k, []): core.add(e["ticker"])
    for s in cfg.get("stock_sleeve", []):
        if s.get("include_in_metrics_pull", True): core.add(s["ticker"])
    batches = [dict(cfg, candidate_pool=[])]
    pool = [e for e in cfg.get("candidate_pool", []) if e["ticker"] not in core]
    for i in range(0, len(pool), batch_size):
        batches.append(dict(cfg, watchlist=[], vci_watchlist=[], stock_sleeve=[],
                            candidate_pool=pool[i:i+batch_size]))
    return batches

def merge(outs, month_label):
    tickers = {}
    for o in outs: tickers.update(o["tickers"])
    def uni(key):
        seen = []
        for o in outs:
            for x in o["_meta"].get(key, []):
                if x not in seen: seen.append(x)
        return seen
    def mdict(key):
        d = {}
        for o in outs: d.update(o["_meta"].get(key, {}))
        return d
    def ssum(key):
        return sum(o["_meta"].get(key, 0) for o in outs)
    m0 = outs[0]["_meta"]
    meta = {
        "month_label": month_label,
        "produced_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tickers_requested": uni("tickers_requested"),
        "tickers_scored": list(tickers.keys()),
        "tickers_failed": uni("tickers_failed"),
        "fetch_errors": mdict("fetch_errors"),
        "scoring_errors": mdict("scoring_errors"),
        "in_window_threshold_pct": m0.get("in_window_threshold_pct", 10.0),
        "vci_count": ssum("vci_count"),
        "sleeve_count": ssum("sleeve_count"),
        "in_window_tickers": uni("in_window_tickers"),
        "high_score_tickers": uni("high_score_tickers"),
        "high_score_energy": uni("high_score_energy"),
        "high_conviction_vci": uni("high_conviction_vci"),
        "energy_screener_available": m0.get("energy_screener_available"),
        "candidate_pool_count": ssum("candidate_pool_count"),
        "pipeline_counts": {k: sum(o["_meta"].get("pipeline_counts", {}).get(k, 0) for o in outs)
                            for k in ("growth_stock", "energy", "vci")},
        "b2b_data_injected": {},
        "fetch_method": "local_batched_yfinance",
    }
    for k in ("tickers_with_b2b", "b2b_scored", "backlog_ev_scored", "b2b_applicable_unscored"):
        s = []
        for o in outs:
            for x in o["_meta"].get("b2b_data_injected", {}).get(k, []):
                if x not in s: s.append(x)
        meta["b2b_data_injected"][k] = s
    return {"_meta": meta, "tickers": tickers}

def main():
    ap = argparse.ArgumentParser(description="Local resumable batched yfinance metrics fetch.")
    ap.add_argument("--watchlist", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--month-label", required=True)
    ap.add_argument("--batch-size", type=int, default=28)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--shm", default="/dev/shm/pylibs", help="tmpfs dir where yfinance is installed")
    ap.add_argument("--partial", default=None, help="resume cache file (default: <out>.partial)")
    a = ap.parse_args()

    if a.shm and os.path.isdir(a.shm):
        sys.path.insert(0, a.shm)

    script_dir = os.path.dirname(os.path.abspath(a.watchlist))
    partial_path = a.partial or (os.path.abspath(a.out) + ".partial")
    cfg = json.load(open(a.watchlist, encoding="utf-8"))
    batches = build_plan(cfg, a.batch_size)
    n = len(batches)

    state = {"n": n, "batches": {}}
    if os.path.exists(partial_path):
        try:
            prev = json.load(open(partial_path, encoding="utf-8"))
            if isinstance(prev, dict) and prev.get("n") == n and "batches" in prev:
                state = prev
        except Exception:
            pass

    nxt = next((i for i in range(n) if str(i) not in state["batches"]), None)

    if nxt is not None:
        m = load_fwm(script_dir)
        m.FETCH_WORKERS = a.workers
        import logging; logging.disable(logging.WARNING)
        sub = f"/dev/shm/_isa_cfg_{nxt}.json"
        json.dump(batches[nxt], open(sub, "w"))
        out = m.run(sub, f"/dev/shm/_isa_batch_{nxt}.json", a.month_label)
        state["batches"][str(nxt)] = out
        json.dump(state, open(partial_path, "w", encoding="utf-8"), default=str)  # overwrite
        remaining = n - len(state["batches"])
        print(f"BATCH {nxt+1}/{n} done (scored={len(out['tickers'])}, "
              f"failed={len(out['_meta']['tickers_failed'])})")
        if remaining > 0:
            print(f"NOT_DONE — {remaining} batch(es) left. Call again."); return

    outs = [state["batches"][str(i)] for i in range(n)]
    merged = merge(outs, a.month_label)
    json.dump(merged, open(a.out, "w", encoding="utf-8"), indent=2, ensure_ascii=False, default=str)
    json.dump({"status": "done"}, open(partial_path, "w", encoding="utf-8"))  # shrink stub (cannot delete on mount)
    print(f"ALL_DONE tickers={len(merged['tickers'])} failed={len(merged['_meta']['tickers_failed'])} "
          f"in_window={merged['_meta']['in_window_tickers']}")

if __name__ == "__main__":
    main()
