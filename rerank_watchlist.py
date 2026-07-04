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
import source_score as _ss     # Jul-26 Part 1: THE single Source Score
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
    _tgt = None
    if tk_d:
        pr = tk_d.get("_prices") or {}
        _tgt = _to_num(pr.get("target_mean")) or _to_num(tk_d.get("target_price_mean"))
    if fv is None:
        fv = _tgt
    # Jul-2026 (Raj): CONSENSUS SANITY-CAP on the fair value used for implied upside. A model
    # base_fair_value cannot exceed the analyst CONSENSUS target by more than CONSENSUS_UPSIDE_CAP_MULT
    # — stops a single outlier / model FV inflating a name's rank (e.g. AUPH base_fv 24.36 vs consensus
    # target 17.0 = +56% implied upside on a +9% consensus). No cap when no consensus target exists.
    _upside_capped = False
    _capmult = getattr(_cfg, "CONSENSUS_UPSIDE_CAP_MULT", 1.15)
    if fv is not None and _tgt is not None and _tgt > 0 and fv > _tgt * _capmult:
        fv = _tgt * _capmult
        _upside_capped = True
    conf = e.get("entry_level_confidence")
    if not conf:
        conf = "high" if e.get("entry_level_status") == "approved" else "low"
    up = (fv / cp - 1) if (fv and cp and cp > 0) else None
    up_norm = max(0.0, min(up, UPSIDE_CAP)) / UPSIDE_CAP if up is not None else 0.0
    if el and cp and el > 0:
        pct_above = max(0.0, (cp - el) / el)
        # Jul-26 Part 3 (backtested): gentler, FLOORED decay so extended momentum winners are not
        # crushed for having run (12-1m momentum had +0.044 fwd-6m rank-IC vs -0.034 for the pullback
        # proxy). DEPLOY_ENTRY_DECAY 0.50 (was 0.25) widens the window; DEPLOY_ENTRY_FLOOR 0.50 caps
        # the penalty at half rather than letting it collapse toward 0.
        ew = max(getattr(_cfg, "DEPLOY_ENTRY_FLOOR", 0.0),
                 1.0 / (1.0 + pct_above / getattr(_cfg, "DEPLOY_ENTRY_DECAY", 0.25)))
    else:
        ew = 0.0
    cw = CONF_WEIGHT.get(conf, 0.6)
    # Jul-2026 (Raj): PRICE WINDOW REMOVED FROM RANKING. Ranking deployability = implied
    # UPSIDE-to-fair-value x confidence ONLY. `ew` (entry_window = price vs stored entry level)
    # is retained in the basis for DISPLAY / target-buy-price but is NOT multiplied into the
    # ranking score. Implied upside (up_norm) therefore remains a first-class ranking factor.
    d = round(up_norm * cw, 4)
    return d, {"upside_to_fv": round(up, 3) if up is not None else None,
              "entry_window": round(ew, 3), "conf_weight": cw, "fair_value": fv,
              "consensus_target": _tgt, "consensus_upside_capped": _upside_capped,
              "ranking_note": "deployability = upside x confidence (entry_window excluded; FV consensus-capped)"}


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


def _resolve_action(name, cfg, ctx=None):
    """(action, tier, aps, cap, exception_required) for one scored name.
    §13.3 resolution + §13.4 caps-before-ranking + §13.5 APS. `ctx` carries cross-name context for the
    Jul-26 Part 7 held-stock upgrade/replacement test (best candidate source, market regime, capital)."""
    ctx   = ctx or {}
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
            # Jul-26 Part 7 — held-stock UPGRADE / REPLACEMENT test. A middling HOLD (floor<=source<bar)
            # is rotated (HOLD -> TRIM sell-to-upgrade) when the best eligible candidate's Source beats
            # this holding's by >= UPGRADE_DELTA AND fresh capital is insufficient to fund the candidate
            # outright AND we're NOT in a down market AND an upgrade slot remains AND no active-sell-off /
            # 30-day-hold / preclearance block. Otherwise stays HOLD (context only). blockers default to
            # permissive so the pre-run can tighten them from portfolio state.
            best      = ctx.get("best_candidate_source")
            best_tk   = ctx.get("best_candidate_ticker")
            delta     = getattr(cfg, "UPGRADE_DELTA", 15)
            insufficient_capital = ctx.get("insufficient_fresh_capital", True)
            slots     = ctx.get("upgrade_slots", 1)
            blocked   = bool(name.get("upgrade_blocked") or name.get("in_30day_hold")
                             or name.get("active_selloff") or name.get("preclearance_block"))
            if (best is not None and (best - s) >= delta and insufficient_capital
                    and not ctx.get("down_market") and slots > 0 and not blocked):
                name["_upgrade_replace"] = best_tk
                aps = round(min(90.0, best - s), 1)
                return ACTION_TRIM, TIER_R, aps, ("UPGRADE_REPLACE->%s" % (best_tk or "?")), False
            return ACTION_HOLD, TIER_H, None, None, False            # context only
        # Jul-2026 (Raj): below the HOLD FLOOR = dead money -> SELL (full exit) — it no longer clears
        # the bar to be owned. A catalyst-protected name (temporary forward lull on a confirmed
        # long-term thesis) -> SELL-REVIEW (surfaced as a trim tier) rather than an auto-exit.
        if name.get("catalyst"):
            return ACTION_TRIM, TIER_R, round(min(90.0, floor - s), 1), "below_floor_catalyst_review", False
        return ACTION_SELL, TIER_R, round(min(95.0, (floor - s) + 50), 1), "below_hold_floor_dead_money", False
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
        # Jul-26 Part 7: a held-stock UPGRADE_REPLACE already names the candidate it funds — keep that
        # tag (it takes precedence over the generic 2-of-3 reallocation search).
        if r.get("upgrade_replace"):
            r["funds_ticker"] = r["upgrade_replace"]
            r["replacement_status"] = "UPGRADE_REPLACE->%s" % r["upgrade_replace"]
            r["aps"] = round((r.get("aps") or 0) + getattr(cfg, "APS_REALLOC_BONUS", 10.0), 1)
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


def _apply_diversification(stack, cfg, ctx):
    """Jul-26 Part 8 — sleeve sector/theme concentration cap + diversification-aware selection.
    ctx (populated by the pre-run from the X-Ray/portfolio look-through) supplies:
      sector_of   {ticker: GICS sector}         theme_of {ticker: theme}
      sector_exposure {sector: fraction of ISA} (funds + held look-through, NETTED)
      theme_exposure  {theme: fraction of sleeve}
      target_weight {ticker: fraction of ISA} + default_buy_weight (fallback)
      overrep_line  sector-exposure level above which a sector is 'over-represented' (default 0.75*cap)
    HARD cap: a BUY/STARTER whose target weight would push its GICS sector past SLEEVE_SECTOR_CAP_ISA
    (or its theme past SLEEVE_THEME_CAP) is downgraded to WATCH, or capped to the residual room if that
    room is investable. SOFT tilt: over-represented-sector BUYs carry a DIVERSIFY_OVERRIDE_DELTA sort
    penalty, so an under-represented alternative wins unless the over-rep name beats it by >= the delta.
    No-op when ctx has no sector map (standalone rerank) — the pre-run supplies the look-through."""
    if not ctx:
        return
    cap       = getattr(cfg, "SLEEVE_SECTOR_CAP_ISA", 0.12)
    theme_cap = getattr(cfg, "SLEEVE_THEME_CAP", 0.50)
    delta     = getattr(cfg, "DIVERSIFY_OVERRIDE_DELTA", 10)
    sec_of    = ctx.get("sector_of", {})
    thm_of    = ctx.get("theme_of", {})
    sec_exp   = dict(ctx.get("sector_exposure", {}))     # mutated as we admit
    thm_exp   = dict(ctx.get("theme_exposure", {}))
    w_of      = ctx.get("target_weight", {})
    default_w = ctx.get("default_buy_weight", 0.025)
    min_w     = ctx.get("min_buy_weight", 0.01)
    overrep_line = ctx.get("overrep_line", cap * 0.75)
    base_exp  = dict(ctx.get("sector_exposure", {}))     # frozen snapshot for over-rep test
    # HARD caps, applied in APS (opportunity) order so the strongest names get first claim on the room.
    buys = sorted([r for r in stack if r["action"] in (ACTION_BUY, ACTION_STARTER)],
                  key=lambda r: -(r.get("aps") or 0))
    for r in buys:
        sec = sec_of.get(r["ticker"]); thm = thm_of.get(r["ticker"])
        w   = w_of.get(r["ticker"], default_w)
        if sec:
            cur = sec_exp.get(sec, 0.0)
            if cur + w > cap + 1e-9:
                residual = max(0.0, cap - cur)
                if residual < min_w:
                    r["action"] = ACTION_WATCH; r["tier"] = TIER_H; r["cap"] = "sleeve_sector_cap"
                    r["sector_cap_note"] = "sector %s at cap %.0f%%" % (sec, cap * 100)
                    continue
                r["capped_weight"] = round(residual, 4)
                r["sector_cap_note"] = "capped to residual %.1f%% (%s)" % (residual * 100, sec)
                w = residual
        if thm:
            tcur = thm_exp.get(thm, 0.0)
            if tcur + w > theme_cap + 1e-9:
                r["action"] = ACTION_WATCH; r["tier"] = TIER_H; r["cap"] = "sleeve_theme_cap"
                r["sector_cap_note"] = "theme %s at cap %.0f%% of sleeve" % (thm, theme_cap * 100)
                continue
        if sec:
            sec_exp[sec] = sec_exp.get(sec, 0.0) + w
        if thm:
            thm_exp[thm] = thm_exp.get(thm, 0.0) + w
    # SOFT diversification tilt (sort penalty on over-represented sectors).
    for r in stack:
        if r["action"] in (ACTION_BUY, ACTION_STARTER):
            sec = sec_of.get(r["ticker"])
            r["_divpen"] = float(delta) if (sec and base_exp.get(sec, 0.0) >= overrep_line) else 0.0


def build_action_stack(universe, cfg, month_label, ctx=None):
    """Assemble the ranked action stack from a unified universe of scored names
    (candidates + held). Returns the stack rows per CONTRACTS #6.
    Jul-26 Part 7: `ctx` supplies the held-stock upgrade/replacement context (best candidate source,
    down-market veto, upgrade-slot budget). Built here from the universe when not supplied."""
    bar   = getattr(cfg, "APS_FRESH_CAPITAL_BAR", 65.0)
    floor = getattr(cfg, "APS_HOLD_FLOOR", 50.0)
    ctx   = dict(ctx or {})
    # Best eligible CANDIDATE (non-owned, no disqualifier, at/above the fresh-capital bar) — the name a
    # sell-to-upgrade would fund. Used by the Part 7 test in _resolve_action.
    _cands = [n for n in universe if not n.get("owned") and not (n.get("disqualifier_flags"))
              and (n.get("source_score") or 0) >= bar]
    if _cands and "best_candidate_source" not in ctx:
        _best = max(_cands, key=lambda n: (n.get("source_score") or 0))
        ctx["best_candidate_source"] = _best.get("source_score")
        ctx["best_candidate_ticker"] = _best.get("ticker")
    ctx.setdefault("upgrade_slots", 1)   # cap one upgrade-rotation / month (Part 7)
    stack = []
    for name in universe:
        action, tier, aps, cap, exc = _resolve_action(name, cfg, ctx)
        if name.get("_upgrade_replace"):
            ctx["upgrade_slots"] = ctx.get("upgrade_slots", 1) - 1   # consume the monthly slot
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
            "upgrade_replace": name.get("_upgrade_replace"),
            "canonical_action": (_alang.normalize_action(action) if _alang else action),
            "action_label": (_alang.label_for(action) if _alang else action),
            "_upside": name.get("upside"), "_catalyst": name.get("catalyst"),
        })
    _apply_replacement_test(stack, cfg)
    _apply_diversification(stack, cfg, ctx)   # Jul-26 Part 8 sector/theme caps + diversification tilt
    # Drop any BUY that the sector/theme cap downgraded to WATCH/HOLD (context only, not in the stack).
    stack = [r for r in stack if r["action"] not in (ACTION_HOLD, ACTION_WATCH)]
    top_n = getattr(cfg, "APS_TOP_N", 10)
    # Tiebreak chain when APS is equal: diversification-adjusted APS -> source_score (forward+quality)
    # -> upside (deployability) -> ticker (deterministic). Everything is reproducible.
    _stack_key = lambda r: (-((r.get("aps") or 0) - (r.get("_divpen") or 0)), -(r.get("source_score") or 0),
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
        r.pop("_upside", None); r.pop("_catalyst", None); r.pop("_divpen", None)
    return top


def _assemble_universe(eligible, held, tk, mom_map, registry, hurdle=70.0, q_span=1.0):
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
    for t in held:
        td = tk.get(t) or {}
        mm = mom_map.get(t) or {}
        # H3: score held on the SAME forward-led Source Score as candidates (§13.1) so buy-X vs
        # top-up-Y vs sell-Z compare on one metric. Deployability is neutral (0.5) for an owned name.
        _fr = mm.get("forward_axis_score")
        _fwd_axis = _fr if _fr is not None else 50.0   # neutral forward when unmeasured
        _rev_raw = mm.get("revisions_score")
        _rev = (_rev_raw / 100.0) if _rev_raw is not None else 0.0
        _hns = _norm(td.get("total_score"), td.get("_source_pipeline", "growth_stock"), td.get("total_max"))
        _q = max(0.0, min(1.0, ((_hns or hurdle) - hurdle) / q_span))
        # Jul-2026 (Raj): score held APPLES-TO-APPLES with candidates on real implied upside
        # (upside-to-fair-value x confidence), not a 0.5 placeholder, so buy-X vs top-up-Y vs
        # trim/sell-Z all compare on ONE like-for-like Source Score.
        _hdep, _ = _deployability({"current_price": td.get("current_price"), "entry_level": None}, td)
        ss = _ss.compute_source_score(forward_axis=_fwd_axis, revisions=_rev, deployability=_hdep,
                                      quality_norm=_q, analyst=_analyst_signal(td))
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


def run(scored_path, watchlist_path, hurdle=70.0, max_wl=10, metrics_path=None, action_stack_ctx=None):
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
                               "forward_axis_score": _td.get("forward_axis_score"),
                               "revisions_score": _td.get("revisions_score")}
        except Exception:
            ind_map = {}
    # Jul-2026 (Raj): only PATH-A (growth) held positions are scored on the growth Source Score and
    # enter the action stack. Path-B/VCI holdings (e.g. ONT, asymmetric) are scored on ACS, not the
    # growth metric, so they are excluded here and handled by the VCI/Path-B review separately.
    held = {s.get("ticker") for s in wt.get("stock_sleeve", [])
            if str(s.get("source_pipeline", "growth_stock")).lower() != "vci"
            and str(s.get("path", "A")).upper() != "B"}
    vci = {e.get("ticker") for e in wt.get("vci_watchlist", [])}
    registry = {}
    for e in wt.get("watchlist", []):
        registry[e["ticker"]] = dict(e)
    for e in wt.get("candidate_pool", []):
        registry.setdefault(e["ticker"], dict(e))
    log = {"run_date": date.today().strftime("%Y-%m-%d"), "promoted": [], "demoted": [],
           "below_hurdle_in_pool": [], "no_live_score": [], "rescored": 0,
           "selection_method": "source_score_v2",   # Jul-26: single forward-led compute_source_score
           "weights": dict(getattr(_cfg, "SOURCE_WEIGHTS", {}))}
    for t, e in registry.items():
        pipeline = e.get("source_pipeline", "growth_stock")
        live = cr.get(t)
        total = None
        if live:
            total = live.get("total_score_36") if pipeline == "energy" else (live.get("total_score_50") or live.get("total_score_54"))
            if total is None:
                total = live.get("total_score_50") or live.get("total_score_54") or live.get("total_score_36")
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
    _q_floor = getattr(_cfg, "NORMALISED_SCORE_HARD_REMOVE_BELOW", 60.0)
    for t, e in registry.items():
        if t in held or t in vci:
            continue
        ns = e.get("normalised_score")
        # Jul-2026 (Raj): HARD QUALITY FLOOR (applies in BOTH modes). A name below the
        # quality/removal floor (normalised_score < 60) is NOT deployable or rankable, so it
        # never enters the watchlist or the action stack. This stops low-quality, high-implied-
        # upside "cheap because it has lost value" names from ranking. Forward-led selection
        # ranks AMONG quality names; it is not a bypass of the quality floor.
        if ns is None or ns < _q_floor:
            continue
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
    for e in eligible:
        q_norm = max(0.0, min(1.0, ((e.get("normalised_score") or hurdle) - hurdle) / q_span))
        d, dbasis = _deployability(e, tk.get(e["ticker"]))
        m = _momentum(mom_map.get(e["ticker"]))
        a = _analyst_signal(tk.get(e["ticker"]))
        # Jul-26 Part 1/2: the single forward-led Source Score. forward_axis is now price+margin only;
        # revisions come in separately (SOURCE_WEIGHTS["revisions"]). F falls back to the momentum
        # proxy (0-1 -> 0-100) when the forward axis is absent. Watchlist passes the FULL deployability
        # (entry-weight * upside), unlike the screen's part_b proxy.
        _mm = mom_map.get(e["ticker"]) or {}
        f_raw = _mm.get("forward_axis_score")
        fwd_axis = f_raw if f_raw is not None else round(m * 100, 1)
        _rev_raw = _mm.get("revisions_score")
        rev = (_rev_raw / 100.0) if _rev_raw is not None else 0.0
        # Jul-2026 (Raj): deployability `d` now = implied UPSIDE-to-fair-value x confidence ONLY
        # (the entry-window price gap has been removed inside _deployability). Implied upside is
        # therefore STILL a ranking factor via this term; the price window is not.
        e["source_score"] = _ss.compute_source_score(forward_axis=fwd_axis, revisions=rev,
                                                     deployability=d, quality_norm=q_norm, analyst=a)
        comp = round(e["source_score"] / 100.0, 4)
        e["selection_basis"] = {"source_score": e["source_score"], "forward": round(fwd_axis / 100.0, 3),
                                "forward_axis_score": f_raw, "revisions": round(rev, 3),
                                "quality_norm": round(q_norm, 3),
                                "deployability": d, "analyst": round(a, 3), **dbasis}
        e["_composite"] = comp
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
            universe = _assemble_universe(eligible, held, tk, mom_map, registry, hurdle, q_span)
            # Jul-26 Part 8: sector/theme-cap + diversification context. The pre-run builds it from the
            # X-Ray/portfolio look-through and passes it in (or drops action_stack_ctx.json beside the
            # watchlist). Absent context -> caps are inert (no sector map), so standalone rerank is safe.
            _ctx = action_stack_ctx
            if _ctx is None:
                _ctx_path = os.path.join(os.path.dirname(os.path.abspath(watchlist_path)), "action_stack_ctx.json")
                if os.path.exists(_ctx_path):
                    try:
                        with open(_ctx_path, encoding="utf-8") as _cf:
                            _ctx = json.load(_cf)
                    except Exception:
                        _ctx = None
            stack = build_action_stack(universe, _cfg, month_label, ctx=_ctx)
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

    # Jul-26 Part 9a: append the watchlist rerank output to the point-in-time score panel (learning
    # module) so the reranked names (with the LIVE deployability-aware Source Score) are also tracked.
    # Best-effort — never fail the rerank on a logging error.
    try:
        import pandas as _pd, score_panel_logger as _spl
        _rows = []
        for _e in eligible:
            _mm = mom_map.get(_e["ticker"]) or {}
            _rows.append({"ticker": _e["ticker"], "source_score": _e.get("source_score"),
                          "forward_axis_score": _mm.get("forward_axis_score"),
                          "revisions_score": _mm.get("revisions_score"),
                          "part_a_score": _e.get("part_a"), "part_b_score": _e.get("part_b"),
                          "est_rev_direction": _mm.get("est_rev_direction"),
                          "current_price": _e.get("current_price")})
        if _rows:
            _store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_panel.csv")
            _spl.log_from_full_data(_pd.DataFrame(_rows), group="WATCHLIST_RERANK",
                                    run_date=log["run_date"], store=_store)
    except Exception as _ex:
        print(f"  [rerank] score-panel log skipped: {_ex}", file=sys.stderr)

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
