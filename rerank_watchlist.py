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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scoring_config as _cfg  # Source Score weights / forward-axis flag (single source of truth)
try:
    import deployment_flags as _dflags  # shared deployment-gate pre-flags (action-stack caps)
except Exception:
    _dflags = None
try:
    import action_language as _alang  # canonical action vocabulary (E7)
except Exception:
    _alang = None

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


def _norm(total, pipeline, total_max=None):
    if total is None:
        return None
    mx = total_max or (_cfg.ENERGY_TOTAL_MAX if pipeline == "energy" else _cfg.GROWTH_TOTAL_MAX)
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


# ---------------------------------------------------------------------------
# E1 — Global Action Stack (redesign Part3 §13.3-13.6 / CONTRACTS #6). Additive: assembles ONE
# ranked agenda across candidates AND held positions. Gated by scoring_config.BUILD_ACTION_STACK.
# ---------------------------------------------------------------------------
ACTION_BUY, ACTION_STARTER, ACTION_TOPUP = "BUY", "STARTER", "TOP_UP"
ACTION_TRIM, ACTION_SELL, ACTION_HOLD, ACTION_WATCH = "TRIM", "SELL", "HOLD", "WATCH"
TIER_M, TIER_D, TIER_R, TIER_H = "M", "D", "R", "H"   # Mandatory / Deploy / Reallocate / Hold


def _eps_mom_sign(mm):
    """Signed estimate-revision direction proxy (+1/-1/None) from the mechanical metrics."""
    mm = mm or {}
    d = (mm.get("est_rev_direction") or "").lower()
    up = _to_num(mm.get("est_rev_eps_up_30d")) or 0
    dn = _to_num(mm.get("est_rev_eps_down_30d")) or 0
    if d == "up" or up > dn:
        return 1.0
    if d == "down" or dn > up:
        return -1.0
    return None


def _price_mom_sign(mm):
    """Signed price-trend proxy (+1/-1/None) from 52wk position (mechanical fallback)."""
    p = _to_num((mm or {}).get("position_52wk"))
    if p is None:
        return None
    return 1.0 if p >= 0.5 else -1.0


def _is_divergence(eps, price):
    return (eps is not None and eps > 0) and (price is not None and price < 0)


def _both_positive(eps, price):
    return (eps is not None and eps > 0) and (price is not None and price >= 0)


def _resolve_action(name, cfg):
    """(action, tier, aps, cap, exception_required) for one scored name.
    §13.3 resolution + §13.4 caps-before-ranking + §13.5 APS."""
    bar   = getattr(cfg, "APS_FRESH_CAPITAL_BAR", 65.0)
    floor = getattr(cfg, "APS_HOLD_FLOOR", 50.0)
    pen   = getattr(cfg, "APS_TOPUP_PENALTY", 12.0)
    mand  = getattr(cfg, "APS_MANDATORY_SELL", 95.0)
    s     = name.get("source_score") or 0.0
    owned = bool(name.get("owned"))
    disq  = list(name.get("disqualifier_flags") or [])
    cap   = disq[0] if disq else None
    eps, price = name.get("eps_mom"), name.get("price_mom")
    diverge = _is_divergence(eps, price)

    if owned:
        if name.get("score_missing"):
            return ACTION_HOLD, TIER_H, None, "data_missing", False  # M3: no score data -> HOLD, never auto-TRIM
        if disq:
            return ACTION_SELL, TIER_M, mand, cap, True              # mandatory sell (capital protection)
        if s >= bar:
            return ACTION_TOPUP, TIER_D, round(max(0.0, s - pen), 1), None, False
        if s >= floor:
            return ACTION_HOLD, TIER_H, None, None, False            # context only
        return ACTION_TRIM, TIER_R, round(min(90.0, floor - s), 1), None, False
    # not owned
    if disq:
        if diverge:
            return ACTION_STARTER, TIER_D, round(s, 1), cap, True    # capped — never full BUY
        return ACTION_WATCH, TIER_H, None, cap, True
    if s >= bar and diverge:
        return ACTION_STARTER, TIER_D, round(s, 1), None, True       # size-capped, review flag
    if s >= bar and _both_positive(eps, price):
        return ACTION_BUY, TIER_D, round(s, 1), None, False
    if s >= bar:
        # M2: eligible by score but NO confirming momentum/forward signal -> cautious STARTER, not a full BUY
        return ACTION_STARTER, TIER_D, round(s, 1), None, True
    return ACTION_WATCH, TIER_H, None, None, False


def _apply_replacement_test(stack, cfg):
    """§13.5: link each TRIM to the best BUY/STARTER it could fund (2-of-3 test) + opp-cost APS bonus."""
    ret_pp = getattr(cfg, "REPLACEMENT_RETURN_PP", 10.0)
    buy_pp = getattr(cfg, "REPLACEMENT_BUYABILITY", 15.0)
    bonus  = getattr(cfg, "APS_REALLOC_BONUS", 10.0)
    buys = sorted([r for r in stack if r["action"] in (ACTION_BUY, ACTION_STARTER)],
                  key=lambda r: -(r.get("source_score") or 0))
    for r in stack:
        if r["action"] != ACTION_TRIM:
            continue
        for b in buys:
            tests = 0
            if (b.get("source_score") or 0) - (r.get("source_score") or 0) >= ret_pp:
                tests += 1
            if ((b.get("_upside") or 0) - (r.get("_upside") or 0)) * 100 >= buy_pp:
                tests += 1
            if b.get("_catalyst"):
                tests += 1
            if tests >= 2:
                r["funds_ticker"] = b["ticker"]
                r["replacement_status"] = f"reallocate->{b['ticker']} ({tests}/3)"
                r["aps"] = round((r.get("aps") or 0) + bonus, 1)
                break


def build_action_stack(universe, cfg, month_label):
    """Assemble the ranked action stack from a unified universe of scored names
    (candidates + held). Returns the stack rows per CONTRACTS #6."""
    bar   = getattr(cfg, "APS_FRESH_CAPITAL_BAR", 65.0)
    floor = getattr(cfg, "APS_HOLD_FLOOR", 50.0)
    stack = []
    for name in universe:
        action, tier, aps, cap, exc = _resolve_action(name, cfg)
        if action in (ACTION_HOLD, ACTION_WATCH):
            continue   # context only — not in the stack
        # E2 — held-name 2-axis tag: add_worthy (>=fresh-capital bar) / retain_only (>=hold floor)
        # / dead_money (<floor). dead_money is flagged each run; the decision ledger tracks persistence.
        owned = bool(name.get("owned"))
        _s = name.get("source_score") or 0.0
        held_axis = (("add_worthy" if _s >= bar else "retain_only" if _s >= floor else "dead_money")
                     if owned else None)
        stack.append({
            "action": action, "ticker": name["ticker"], "route": name.get("route", "growth"),
            "source_score": round(name.get("source_score") or 0, 1), "aps": aps, "tier": tier,
            "funds_ticker": None, "key_pos": list(name.get("key_pos") or [])[:3],
            "key_neg": list(name.get("key_neg") or [])[:3], "cap": cap,
            "replacement_status": None, "exception_required": exc,
            "held_axis": held_axis, "dead_money": bool(owned and _s < floor),
            "canonical_action": (_alang.normalize_action(action) if _alang else action),
            "action_label": (_alang.label_for(action) if _alang else action),
            "_upside": name.get("upside"), "_catalyst": name.get("catalyst"),
        })
    _apply_replacement_test(stack, cfg)
    top_n = getattr(cfg, "APS_TOP_N", 10)
    # Tiebreak chain when APS is equal: source_score (forward+quality) -> upside (deployability) ->
    # ticker (deterministic). Source-equal BUYs then rank on upside; everything is reproducible.
    _stack_key = lambda r: (-(r.get("aps") or 0), -(r.get("source_score") or 0),
                            -(r.get("_upside") or 0), str(r.get("ticker") or ""))
    stack.sort(key=_stack_key)
    top = stack[:top_n]
    seen = {r["ticker"] for r in top}
    for r in stack:                       # force-include ALL mandatory (tier M) actions
        if r["tier"] == TIER_M and r["ticker"] not in seen:
            top.append(r); seen.add(r["ticker"])
    top.sort(key=_stack_key)
    for i, r in enumerate(top, 1):
        r["rank"] = i
        r.pop("_upside", None); r.pop("_catalyst", None)
    return top


def _assemble_universe(eligible, held, tk, mom_map, registry, hurdle=70.0, q_span=1.0, sw=None):
    """Unified scored list for the action stack: candidates (source_score from rerank) + held
    positions (forward/normalised score). Each: ticker, route, owned, source_score, eps/price mom,
    upside, disqualifier_flags, catalyst, key_pos/neg."""
    uni = []
    for e in eligible:
        ss = e.get("source_score")
        if ss is None:
            ss = round(e.get("_composite", 0.0) * 100, 1)
        sb = e.get("selection_basis") or {}
        mm = mom_map.get(e["ticker"]) or {}
        uni.append({
            "ticker": e["ticker"], "route": e.get("source_pipeline", "growth_stock"), "owned": False,
            "source_score": ss, "eps_mom": _eps_mom_sign(mm), "price_mom": _price_mom_sign(mm),
            "upside": sb.get("upside_to_fv"),
            "disqualifier_flags": (_dflags.compute_gate_flags(
                {**(tk.get(e["ticker"]) or {}), **mm},
                bool(e.get("confirmed_catalyst") or e.get("catalyst_protected")))["disqualifier_flags"]
                if _dflags else (e.get("disqualifier_flags") or [])),
            "catalyst": e.get("confirmed_catalyst") or e.get("catalyst_protected"),
            "key_pos": e.get("key_pos"), "key_neg": e.get("key_neg"),
        })
    _w = sw or getattr(_cfg, "SOURCE_SCORE_WEIGHTS", {})
    for t in held:
        td = tk.get(t) or {}
        mm = mom_map.get(t) or {}
        # H3: score held on the SAME forward-led Source Score as candidates (§13.1) so buy-X vs
        # top-up-Y vs sell-Z compare on one metric. Deployability is neutral (0.5) for an owned name.
        _fr = mm.get("forward_axis_score")
        _f = (_fr / 100.0) if _fr is not None else 0.5
        _hns = _norm(td.get("total_score"), td.get("_source_pipeline", "growth_stock"), td.get("total_max"))
        _q = max(0.0, min(1.0, ((_hns or hurdle) - hurdle) / q_span))
        ss = round((_w.get("forward", 0.45) * _f + _w.get("quality", 0.20) * _q
                    + _w.get("deployability", 0.30) * 0.5 + _w.get("analyst", 0.05) * _analyst_signal(td)) * 100, 1)
        _score_missing = (_fr is None and _hns is None)   # M3: no forward AND no quality score = data gap
        uni.append({
            "ticker": t, "route": "sleeve", "owned": True, "source_score": ss,
            "score_missing": _score_missing,
            "eps_mom": _eps_mom_sign(mm), "price_mom": _price_mom_sign(mm),
            "upside": None,
            "disqualifier_flags": (_dflags.compute_gate_flags({**td, **mm},
                bool(td.get("confirmed_catalyst")))["disqualifier_flags"]
                if _dflags else (td.get("disqualifier_flags") or [])),
            "catalyst": td.get("confirmed_catalyst"),
            "key_pos": None, "key_neg": None,
        })
    return uni


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
                               "est_rev_eps_down_30d": _td.get("est_rev_eps_down_30d"),
                               "forward_axis_score": _td.get("forward_axis_score")}
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
           "selection_method": ("source_score_v1" if getattr(_cfg,"FORWARD_AXIS_IN_RANKING",False) else "deployment_composite_v1"),
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
        ns = _norm(total, pipeline, (src or {}).get("total_max")) if (total is not None and src is not None) else None
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
    _use_fe = getattr(_cfg, "FORWARD_ELIGIBILITY", False)
    _pa_floor_a = getattr(_cfg, "FORWARD_ELIG_PART_A_FLOOR", 10)
    _pa_floor_c = getattr(_cfg, "FORWARD_ELIG_PART_A_FLOOR_ENERGY", 14)
    eligible = []
    for t, e in registry.items():
        if t in held or t in vci:
            continue
        ns = e.get("normalised_score")
        if _use_fe:
            # H2: viability floor (Part A, path-aware) + forward eligibility (eps_trend positive OR a
            # confirmed catalyst) — NOT the fixed ns>=70 quality-total gate. Admits forward-confirmed,
            # lower-total names so the Source Score can rank (not pre-filter) them.
            _pa = e.get("part_a") or 0
            _floor = _pa_floor_c if e.get("source_pipeline") == "energy" else _pa_floor_a
            if _pa < _floor:
                continue
            _epspos = (_eps_mom_sign(mom_map.get(t)) or 0) > 0
            _cat = bool(e.get("confirmed_catalyst") or e.get("catalyst_protected"))
            if not (_epspos or _cat):
                continue
        else:
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
    max_ns = max(((e.get("normalised_score") or hurdle) for e in eligible), default=hurdle)
    q_span = (max_ns - hurdle) or 1.0
    use_fwd = getattr(_cfg, "FORWARD_AXIS_IN_RANKING", False)
    sw = getattr(_cfg, "SOURCE_SCORE_WEIGHTS", {})
    for e in eligible:
        q_norm = max(0.0, min(1.0, ((e.get("normalised_score") or hurdle) - hurdle) / q_span))
        d, dbasis = _deployability(e, tk.get(e["ticker"]))
        m = _momentum(mom_map.get(e["ticker"]))
        a = _analyst_signal(tk.get(e["ticker"]))
        if use_fwd:
            # Source Score (Part 3 §13): FORWARD-led. F from forward_axis_score; falls back to the
            # legacy momentum proxy when F is absent. Cheapness earns no separate credit.
            f_raw = (mom_map.get(e["ticker"]) or {}).get("forward_axis_score")
            f = (f_raw / 100.0) if f_raw is not None else m
            comp = round(sw.get("forward", 0.45) * f + sw.get("quality", 0.20) * q_norm
                         + sw.get("deployability", 0.30) * d + sw.get("analyst", 0.05) * a, 4)
            e["source_score"] = round(comp * 100, 1)
            e["selection_basis"] = {"source_score": e["source_score"], "forward": round(f, 3),
                                    "forward_axis_score": f_raw, "quality_norm": round(q_norm, 3),
                                    "deployability": d, "analyst": round(a, 3), **dbasis}
        else:
            comp = round(W_QUALITY * q_norm + W_DEPLOY * d + W_MOMENTUM * m + W_ANALYST * a, 4)
            e["selection_basis"] = {"composite": comp, "quality_norm": round(q_norm, 3),
                                    "deployability": d, "momentum": m, "analyst": round(a, 3), **dbasis}
        e["_composite"] = comp
        # Ensure a Source Score exists for the action stack even when forward ranking is off.
        e["source_score"] = e.get("source_score") or round(comp * 100, 1)
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
            sp_path = os.path.join(os.path.dirname(os.path.abspath(watchlist_path)), "normalise_adapter.py")
            spec = importlib.util.spec_from_file_location("normalise_adapter", sp_path)
            sp = importlib.util.module_from_spec(spec); spec.loader.exec_module(sp)
            sp.run(metrics_path, scored_path)
            print(f"  [rerank] refreshed scored membership to final top-{len(new_wl_set)}")
        except Exception as ex:
            print(f"  [rerank] WARNING: could not refresh scored membership: {ex}", file=sys.stderr)
    # E1 — Global Action Stack (CONTRACTS #6). Additive new output; off by default.
    if getattr(_cfg, "BUILD_ACTION_STACK", False):
        try:
            month_label = date.today().strftime("%b_%Y").lower()
            universe = _assemble_universe(eligible, held, tk, mom_map, registry, hurdle, q_span, sw)
            stack = build_action_stack(universe, _cfg, month_label)
            stack_path = os.path.join(os.path.dirname(os.path.abspath(watchlist_path)),
                                      f"action_stack_{month_label}.json")
            with open(stack_path, "w", encoding="utf-8") as f:
                json.dump({"run_date": log["run_date"], "schema_version": "1.0",
                           "month": month_label, "stack": stack}, f, indent=2, ensure_ascii=False)
            log["action_stack"] = {
                "rows": len(stack),
                "mandatory": sum(1 for r in stack if r["tier"] == "M"),
                "buys": sum(1 for r in stack if r["action"] == "BUY"),
                "path": os.path.basename(stack_path),
            }
            print(f"  [rerank] action_stack: {len(stack)} rows -> {os.path.basename(stack_path)}")
        except Exception as ex:
            print(f"  [rerank] WARNING: action stack build failed: {ex}", file=sys.stderr)

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
    ap.add_argument("--action-stack", action="store_true",
                    help="Build the Global Action Stack (overrides scoring_config.BUILD_ACTION_STACK)")
    a = ap.parse_args()
    if a.action_stack:
        _cfg.BUILD_ACTION_STACK = True
    if not os.path.exists(a.scored):
        print(f"  [rerank] scored file missing ({a.scored}) -- skipping.", file=sys.stderr)
        sys.exit(0)
    run(a.scored, a.watchlist, a.hurdle, a.max, a.metrics)


if __name__ == "__main__":
    main()
