#!/usr/bin/env python3
"""
rerank_watchlist.py  --  Step 7.5: re-rank the watchlist on LIVE re-scored values.

After fetch_watchlist_metrics (Composio) + score_partab produce live Part A/B
scores for the watchlist AND the candidate_pool, this step rewrites the 10-name
watchlist in watchlist_tickers.json by merging watchlist + candidate_pool and
ranking on the live normalised score (total/54 growth, total/36 energy). Only
names at/above the hurdle make the top-10; everyone else stays in the pool
(lossless — nothing is dropped). VCI watchlist and stock_sleeve are untouched.

Usage:
    python3 rerank_watchlist.py --scored watchlist_scored_mmm_yyyy.json \
        --watchlist watchlist_tickers.json [--hurdle 70] [--max 10]
"""
import argparse, json, os, sys
from datetime import date

PATH_A_MAX, PATH_C_MAX = 54, 36


def _norm(total, pipeline):
    if total is None:
        return None
    mx = PATH_C_MAX if pipeline == "energy" else PATH_A_MAX
    return round(total / mx * 100, 1)


def run(scored_path, watchlist_path, hurdle=70.0, max_wl=10):
    with open(scored_path, encoding="utf-8") as f:
        scored = json.load(f)
    with open(watchlist_path, encoding="utf-8") as f:
        wt = json.load(f)

    cr = {e["ticker"]: e for e in scored.get("conviction_ranking", [])}
    tk = scored.get("tickers", {})  # full 114-ticker live scores (incl. pool)

    held = {s.get("ticker") for s in wt.get("stock_sleeve", [])}
    vci  = {e.get("ticker") for e in wt.get("vci_watchlist", [])}

    # Merge watchlist + candidate_pool into one candidate registry (keep full dicts).
    registry = {}
    for e in wt.get("watchlist", []):
        registry[e["ticker"]] = dict(e)
    for e in wt.get("candidate_pool", []):
        registry.setdefault(e["ticker"], dict(e))

    log = {"run_date": date.today().strftime("%Y-%m-%d"), "promoted": [], "demoted": [],
           "below_hurdle_in_pool": [], "no_live_score": [], "rescored": 0}

    for t, e in registry.items():
        pipeline = e.get("source_pipeline", "growth_stock")
        live = cr.get(t)
        total = None
        if live:
            total = live.get("total_score_36") if pipeline == "energy" else live.get("total_score_54")
            if total is None:
                total = live.get("total_score_54") or live.get("total_score_36")
        src = live
        # Pool names are absent from conviction_ranking but carry full live Part A/B
        # scores in scored["tickers"] — use them so the POOL is ranked on LIVE data too
        # (honours the re-rank-after-rescore enhancement; without this pool names fell
        # back to stale screening scores).
        if total is None:
            tkd = tk.get(t)
            if tkd is not None:
                total = tkd.get("total_score")
                src = tkd
        ns = _norm(total, pipeline) if (total is not None and src is not None) else None
        if ns is not None:
            e["normalised_score"] = ns
            e["total"]   = total
            e["part_a"]  = src.get("part_a_score", e.get("part_a"))
            e["part_b"]  = src.get("part_b_score", e.get("part_b"))
            e["in_window"]     = src.get("in_window", src.get("_in_window", e.get("in_window")))
            e["current_price"] = src.get("current_price", e.get("current_price"))
            e["_live_rescored"] = True
            log["rescored"] += 1
        else:
            log["no_live_score"].append(t)
        e["_rank_score"] = e.get("normalised_score") if e.get("normalised_score") is not None else -1.0

    old_wl = {e["ticker"] for e in wt.get("watchlist", [])}

    # Rank all non-held, non-VCI candidates by live normalised score (desc).
    # Deterministic tie-break (runs AFTER the live metrics pull + scoring): equal
    # live scores are ordered by in-window first (more actionable), then ticker
    # A->Z, so consecutive runs produce an identical top-10 at the #10 boundary.
    def _rank_key(e):
        return (-float(e.get("_rank_score", -1)),
                0 if e.get("in_window") else 1,
                str(e.get("ticker", "")))
    ranked = sorted(
        (e for t, e in registry.items() if t not in held and t not in vci),
        key=_rank_key,
    )

    new_wl, new_pool = [], []
    for e in ranked:
        ns = e.get("normalised_score")
        if len(new_wl) < max_wl and ns is not None and ns >= hurdle:
            new_wl.append(e)
        else:
            new_pool.append(e)
            if ns is not None and ns < hurdle:
                log["below_hurdle_in_pool"].append(e["ticker"])

    for i, e in enumerate(new_wl, 1):
        e["rank"] = i
        e.setdefault("status", "Watchlist")
        e.pop("_carry_forward", None); e.pop("_needs_rescore", None)
    for e in registry.values():
        e.pop("_rank_score", None)

    new_wl_set, old_pool_set = {e["ticker"] for e in new_wl}, set()
    for e in wt.get("candidate_pool", []):
        old_pool_set.add(e["ticker"])
    for t in new_wl_set - old_wl:
        log["promoted"].append(t)
    for t in old_wl - new_wl_set:
        log["demoted"].append(t)

    wt["watchlist"] = new_wl
    wt["candidate_pool"] = new_pool
    wt.setdefault("_meta", {})["last_updated"] = log["run_date"]
    wt["_meta"]["updated_by_run"] = "rerank_watchlist.py (live re-score)"
    if "_candidate_pool_meta" in wt:
        wt["_candidate_pool_meta"]["pool_size"] = len(new_pool)
        wt["_candidate_pool_meta"]["last_updated"] = log["run_date"]

    with open(watchlist_path, "w", encoding="utf-8") as f:
        json.dump(wt, f, indent=2, ensure_ascii=False)

    print(f"  [rerank] watchlist={len(new_wl)} (>= {hurdle}) | pool={len(new_pool)} | "
          f"live re-scored={log['rescored']} | promoted={log['promoted']} | demoted={log['demoted']}")
    print(json.dumps(log))
    return log


def main():
    ap = argparse.ArgumentParser(description="Re-rank watchlist on live re-scored values (Step 7.5).")
    ap.add_argument("--scored", required=True)
    ap.add_argument("--watchlist", required=True)
    ap.add_argument("--hurdle", type=float, default=70.0)
    ap.add_argument("--max", type=int, default=10)
    a = ap.parse_args()
    if not os.path.exists(a.scored):
        print(f"  [rerank] scored file missing ({a.scored}) — skipping re-rank.", file=sys.stderr)
        sys.exit(0)
    run(a.scored, a.watchlist, a.hurdle, a.max)


if __name__ == "__main__":
    main()
