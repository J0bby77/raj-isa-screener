#!/usr/bin/env python3
"""
rerank_watchlist.py  --  Step 7.5: select the top-10 on a DEPLOYMENT-AWARE composite.

Runs AFTER entry_level_builder.py (Step 7.25), so every candidate has a live
normalised score AND a governed entry level. The top-10 is no longer chosen on
quality (normalised_score) alone -- which is structurally sticky because Part A /
quality scores barely move month to month. Instead it is chosen on a composite
that blends quality with how DEPLOYABLE the name is right now (price vs entry,
upside to fair value), so the list is genuinely contestable each month.

Composite (return-tilted; quality gate >=70 first):
    composite = 0.35*quality_norm + 0.60*deployability + 0.05*analyst_signal
  quality_norm  = (ns - hurdle)/(max_ns - hurdle)   within the gated set
  deployability = entry_window * upside_to_fv * confidence_weight   (0..1)
  analyst_signal= rating_score * coverage_reliability   (small nudge / risk flag)
"""
import argparse, json, os, sys
from datetime import date

PATH_A_MAX, PATH_C_MAX = 54, 36
W_QUALITY, W_DEPLOY, W_MOMENTUM, W_ANALYST = 0.30, 0.50, 0.15, 0.05
UPSIDE_CAP = 0.60
ENTRY_WINDOW_MAX_ABOVE = 0.20
CONF_WEIGHT = {"high": 1.0, "medium": 0.85, "low": 0.6}

# Compliance pre-filter (Citi two-tier financial rule): EXCLUDE businesses whose
# P&L moves with credit cycles / rate spreads / trading volumes (broker-dealers,
# banks, insurers, asset managers, capital-markets infrastructure, mortgage
# finance). KEEP fintech / payments / financial-data on merit (per Run Context:
# PYPL, MORN, WISE evaluate normally). Match is on yfinance industry, NOT the
# broad "Financial Services" sector (which also contains fintech/payments).
EXCLUDE_INDUSTRY_KEYWORDS = (
    "capital markets", "bank", "insurance", "reinsur", "asset management",
    "mortgage", "thrifts", "savings", "brokerage", "broker-dealer",
)
def _compliance_excluded(industry, sector):
    ind = (industry or "").lower()
    return any(k in ind for k in EXCLUDE_INDUSTRY_KEYWORDS)


def _to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace("£", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _norm(total, pipeline):
    if total is None:
        return None
    mx = PATH_C_MAX if pipeline == "energy" else PATH_A_MAX
    return round(total / mx * 100, 1)


def _analyst_signal(tk_d):
    rating = ((tk_d or {}).get("analyst_rating") or "").lower()
    if "strong buy" in rating:
        rs = 1.0
    elif "buy" in rating or "outperform" in rating:
        rs = 0.7
    elif "hold" in rating or "neutral" in rating or "market perform" in rating:
        rs = 0.4
    elif "underperform" in rating or "sell" in rating:
        rs = 0.1
    else:
        rs = 0.5
    na = (tk_d or {}).get("num_analysts") or 0
    try:
        na = float(na)
    except (TypeError, ValueError):
        na = 0
    reliability = min(na, 20) / 20.0
    return rs * (0.5 + 0.5 * reliability)


def _deployability(e, tk_d):
    cp = _to_num(e.get("current_price")) or _to_num((tk_d or {}).get("current_price"))
    el = _to_num(e.get("entry_level"))
    basis = e.get("entry_level_basis") or {}
    fv = _to_num(basis.get("base_fair_value"))
    if fv is None and tk_d:
        pr = tk_d.get("_prices") or {}
        fv = _to_num(pr.get("target_mean")) or _to_num(tk_d.get("target_price_mean"))
    conf = e.get("entry_level_confidence")
    if not conf:
        conf = "high" if e.get("entry_level_status") == "approved" else "low"
    up = (fv / cp - 1) if (fv and cp and cp > 0) else None
    up_norm = max(0.0, min(up, UPSIDE_CAP)) / UPSIDE_CAP if up is not None else 0.0
    if el and cp and el > 0:
        pct_above = max(0.0, (cp - el) / el)
        # Smooth decay (no hard cliff): 1.0 at/below entry, ~0.5 at +25%,
        # ~0.33 at +50%, ~0.2 at +100% -- lets justified winners retain deployability.
        ew = 1.0 / (1.0 + pct_above / 0.25)
    else:
        ew = 0.0
    cw = CONF_WEIGHT.get(conf, 0.6)
    d = round(ew * up_norm * cw, 4)
    return d, {"upside_to_fv": round(up, 3) if up is not None else None,
              "entry_window": round(ew, 3), "conf_weight": cw, "fair_value": fv}


def _momentum(mom_d):
    """Trend + EPS-revision momentum, 0..1. Lets strong uptrends surface."""
    pos = _to_num((mom_d or {}).get("position_52wk"))
    trend = max(0.0, min(1.0, pos)) if pos is not None else 0.5
    up = _to_num((mom_d or {}).get("est_rev_eps_up_30d")) or 0
    dn = _to_num((mom_d or {}).get("est_rev_eps_down_30d")) or 0
    direction = ((mom_d or {}).get("est_rev_direction") or "").lower()
    if direction == "up" or up > dn:
        eps = 1.0
    elif direction == "down" or dn > up:
        eps = 0.0
    else:
        eps = 0.5
    return round(0.6 * trend + 0.4 * eps, 4)


def run(scored_path, watchlist_path, hurdle=70.0, max_wl=10, metrics_path=None):
    with open(scored_path, encoding="utf-8") as f:
        scored = json.load(f)
    with open(watchlist_path, encoding="utf-8") as f:
        wt = json.load(f)
    cr = {e["ticker"]: e for e in scored.get("conviction_ranking", [])}
    tk = scored.get("tickers", {})
    # Industry/sector map for the compliance pre-filter (from metrics if available).
    ind_map = {}
    mom_map = {}
    if metrics_path and os.path.exists(metrics_path):
        try:
            with open(metrics_path, encoding="utf-8") as _f:
                _wm = json.load(_f)
            for _t, _td in _wm.get("tickers", {}).items():
                ind_map[_t] = (_td.get("industry"), _td.get("sector"))
                mom_map[_t] = {"position_52wk": _td.get("position_52wk"),
                               "est_rev_direction": _td.get("est_rev_direction"),
                               "est_rev_eps_up_30d": _td.get("est_rev_eps_up_30d"),
                               "est_rev_eps_down_30d": _td.get("est_rev_eps_down_30d")}
        except Exception:
            ind_map = {}
    held = {s.get("ticker") for s in wt.get("stock_sleeve", [])}
    vci = {e.get("ticker") for e in wt.get("vci_watchlist", [])}
    registry = {}
    for e in wt.get("watchlist", []):
        registry[e["ticker"]] = dict(e)
    for e in wt.get("candidate_pool", []):
        registry.setdefault(e["ticker"], dict(e))
    log = {"run_date": date.today().strftime("%Y-%m-%d"), "promoted": [], "demoted": [],
           "below_hurdle_in_pool": [], "no_live_score": [], "rescored": 0,
           "selection_method": "deployment_composite_v1",
           "weights": {"quality": W_QUALITY, "deployability": W_DEPLOY, "momentum": W_MOMENTUM, "analyst": W_ANALYST}}
    for t, e in registry.items():
        pipeline = e.get("source_pipeline", "growth_stock")
        live = cr.get(t)
        total = None
        if live:
            total = live.get("total_score_36") if pipeline == "energy" else live.get("total_score_54")
            if total is None:
                total = live.get("total_score_54") or live.get("total_score_36")
        src = live
        if total is None:
            tkd = tk.get(t)
            if tkd is not None:
                total = tkd.get("total_score")
                src = tkd
        ns = _norm(total, pipeline) if (total is not None and src is not None) else None
        if ns is not None:
            e["normalised_score"] = ns
            e["total"] = total
            e["part_a"] = src.get("part_a_score", e.get("part_a"))
            e["part_b"] = src.get("part_b_score", e.get("part_b"))
            e["in_window"] = src.get("in_window", src.get("_in_window", e.get("in_window")))
            e["current_price"] = src.get("current_price", e.get("current_price"))
            e["_live_rescored"] = True
            log["rescored"] += 1
        else:
            log["no_live_score"].append(t)
    old_wl = {e["ticker"] for e in wt.get("watchlist", [])}
    log["compliance_excluded"] = []
    eligible = []
    for t, e in registry.items():
        if t in held or t in vci:
            continue
        ns = e.get("normalised_score")
        if ns is None or ns < hurdle:
            continue
        ind, sec = ind_map.get(t, (e.get("industry"), e.get("sector")))
        if _compliance_excluded(ind, sec):
            e["compliance_excluded"] = True
            e["compliance_reason"] = f"two-tier financial rule: industry={ind}"
            log["compliance_excluded"].append(t)
            continue
        eligible.append(e)
    elig_ids = {id(e) for e in eligible}
    ineligible = [e for t, e in registry.items()
                  if t not in held and t not in vci and id(e) not in elig_ids]
    max_ns = max((e["normalised_score"] for e in eligible), default=hurdle)
    q_span = (max_ns - hurdle) or 1.0
    for e in eligible:
        q_norm = (e["normalised_score"] - hurdle) / q_span
        d, dbasis = _deployability(e, tk.get(e["ticker"]))
        m = _momentum(mom_map.get(e["ticker"]))
        a = _analyst_signal(tk.get(e["ticker"]))
        comp = round(W_QUALITY * q_norm + W_DEPLOY * d + W_MOMENTUM * m + W_ANALYST * a, 4)
        e["_composite"] = comp
        e["selection_basis"] = {"composite": comp, "quality_norm": round(q_norm, 3),
                                "deployability": d, "momentum": m, "analyst": round(a, 3), **dbasis}
    eligible.sort(key=lambda e: (-e.get("_composite", 0.0),
                                 -(e.get("normalised_score") or 0),
                                 str(e.get("ticker", ""))))
    new_wl = eligible[:max_wl]
    new_pool = eligible[max_wl:] + ineligible
    for e in ineligible:
        if e.get("normalised_score") is not None and e["normalised_score"] < hurdle:
            log["below_hurdle_in_pool"].append(e["ticker"])
    for i, e in enumerate(new_wl, 1):
        e["rank"] = i
        e.setdefault("status", "Watchlist")
        e.pop("_carry_forward", None); e.pop("_needs_rescore", None)
    for e in registry.values():
        e.pop("_composite", None)
    new_wl_set = {e["ticker"] for e in new_wl}
    for t in new_wl_set - old_wl:
        log["promoted"].append(t)
    for t in old_wl - new_wl_set:
        log["demoted"].append(t)
    wt["watchlist"] = new_wl
    wt["candidate_pool"] = new_pool
    wt.setdefault("_meta", {})["last_updated"] = log["run_date"]
    wt["_meta"]["updated_by_run"] = "rerank_watchlist.py (deployment composite)"
    if "_candidate_pool_meta" in wt:
        wt["_candidate_pool_meta"]["pool_size"] = len(new_pool)
        wt["_candidate_pool_meta"]["last_updated"] = log["run_date"]
    with open(watchlist_path, "w", encoding="utf-8") as f:
        json.dump(wt, f, indent=2, ensure_ascii=False)
    if metrics_path and os.path.exists(metrics_path) and os.path.exists(scored_path):
        try:
            with open(metrics_path, encoding="utf-8") as f:
                wm = json.load(f)
            wl_by_t = {e["ticker"]: e for e in new_wl}
            for t, td in wm.get("tickers", {}).items():
                if td.get("_kind") in ("stock_sleeve", "vci_watchlist"):
                    continue
                if t in wl_by_t:
                    e = wl_by_t[t]
                    td["_kind"] = "watchlist"; td["_rank"] = e.get("rank")
                    if e.get("entry_level") is not None:
                        td["_entry_level"] = e.get("entry_level")
                    if e.get("entry_currency"):
                        td["_entry_currency"] = e.get("entry_currency")
                    if e.get("status"):
                        td["_status"] = e.get("status")
                elif t in registry:
                    td["_kind"] = "candidate_pool"; td["_rank"] = None
            with open(metrics_path, "w", encoding="utf-8") as f:
                json.dump(wm, f, indent=2, ensure_ascii=False, default=str)
            import importlib.util
            sp_path = os.path.join(os.path.dirname(os.path.abspath(watchlist_path)), "score_partab.py")
            spec = importlib.util.spec_from_file_location("score_partab", sp_path)
            sp = importlib.util.module_from_spec(spec); spec.loader.exec_module(sp)
            sp.run(metrics_path, scored_path)
            print(f"  [rerank] refreshed scored membership to final top-{len(new_wl_set)}")
        except Exception as ex:
            print(f"  [rerank] WARNING: could not refresh scored membership: {ex}", file=sys.stderr)
    print(f"  [rerank] watchlist={len(new_wl)} (gate >= {hurdle}, composite) | pool={len(new_pool)} | "
          f"live re-scored={log['rescored']} | promoted={log['promoted']} | demoted={log['demoted']}")
    print(json.dumps(log))
    return log


def main():
    ap = argparse.ArgumentParser(description="Select top-10 on a deployment-aware composite (Step 7.5).")
    ap.add_argument("--scored", required=True)
    ap.add_argument("--watchlist", required=True)
    ap.add_argument("--hurdle", type=float, default=70.0)
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--metrics", default=None)
    a = ap.parse_args()
    if not os.path.exists(a.scored):
        print(f"  [rerank] scored file missing ({a.scored}) -- skipping.", file=sys.stderr)
        sys.exit(0)
    run(a.scored, a.watchlist, a.hurdle, a.max, a.metrics)


if __name__ == "__main__":
    main()
