#!/usr/bin/env python3
"""
fv_composite.py — Fix Pack A6/D7 (12-Jul-2026). THE single fair-value composite.

Extracted from entry_level_builder.py (own-multiple fair value + analyst-cap hybrid) and
rerank_watchlist.py (consensus sanity-cap + deployability + analyst signal), so screen and
pre-run compute the SAME quantities from the SAME recipe (Raj 12-Jul: "the growth stock
source score should be the same as the deployability source score"). One implementation,
imported by: source_score.py (unified Source Score), entry_level_builder.py (entry anchors),
screener_core (via source_score), rerank_watchlist (P2).

Canonical fields (decision D7):
  implied_upside_fv   = (composite FV / live price) - 1   -- THE upside capital logic reads.
                        Composite FV carries the consensus sanity-cap (CONSENSUS_UPSIDE_CAP_MULT),
                        exactly as rerank's ranking deployability always has.
  display_target_gap  = (consensus target / price) - 1    -- display only, NEVER a capital input
                        (consensus is sentiment data — Correction #5).

Deployability term (0-1) = up_norm * confidence_weight, where
  up_norm = clamp(implied_upside_fv, 0, SOURCE_UPSIDE_CAP) / SOURCE_UPSIDE_CAP.
Confidence weight at screen derives from the FV basis (both anchors=high, one=medium);
entry_level_builder's approval-status confidence takes over at pre-run (P2 migration).

Stdlib only (imports scoring_config alone). Self-test: python3 fv_composite.py
"""
from __future__ import annotations

try:
    import scoring_config as _cfg
except Exception:      # standalone/self-test safety
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


# ── constants shared with entry_level_builder (extraction source) ────────────────────────
RATIO_LO, RATIO_HI = 0.2, 5.0            # sane own-multiple ratio band (guardrail)
FV_CLAMP_LO, FV_CLAMP_HI = 0.3, 1.7      # composite FV plausibility band vs live price
CONF_WEIGHT = {"high": 1.0, "medium": 0.85, "low": 0.6}
SEMI_BUCKETS = {"semiconductor_equipment", "semiconductor_fabless", "semiconductor_hardware"}
_SECTOR_TYPE_KEYWORDS = [
    ("energy",             ("oil", "gas", " energy", "petroleum", "drilling", "coal", "lng", "midstream")),
    ("healthcare",         ("health", "biotech", "pharma", "medical", "life science", "drug")),
    ("cyclical",           ("semiconductor", "chip", "auto", "mining", "metals", "materials", "machinery")),
    ("quality_compounder", ("software", "saas", "internet content", "application software", "it services")),
]


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    try:
        f = float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip())
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def classify_stock_type_detail(t, get=None):
    """Sector-type classification (single implementation — entry_level_builder imports this).
    Prefers explicit sector_bucket; else keyword inference; never silently defaults to SaaS."""
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    pipeline = (g(t, "_source_pipeline") or g(t, "source_pipeline") or "growth_stock")
    bucket = (g(t, "sector_bucket") or "").strip().lower()
    sector = (g(t, "sector") or "")
    industry = (g(t, "industry") or "")
    if pipeline == "energy":
        return {"sector_type": "energy", "sector_type_inferred": False, "sector_type_basis": "pipeline=energy"}
    if bucket in SEMI_BUCKETS:
        return {"sector_type": "cyclical", "sector_type_inferred": False, "sector_type_basis": f"bucket={bucket}"}
    if bucket == "software_saas":
        return {"sector_type": "quality_compounder", "sector_type_inferred": False, "sector_type_basis": "bucket=software_saas"}
    if sector == "Healthcare":
        return {"sector_type": "healthcare", "sector_type_inferred": False, "sector_type_basis": "sector=Healthcare"}
    hay = f" {sector} {industry} ".lower()
    if hay.strip():
        for stype, kws in _SECTOR_TYPE_KEYWORDS:
            if any(k in hay for k in kws):
                return {"sector_type": stype, "sector_type_inferred": True,
                        "sector_type_basis": f"inferred from '{sector}/{industry}'"}
    return {"sector_type": "normal_growth", "sector_type_inferred": True,
            "sector_type_basis": "default normal_growth (no sector signal — NOT SaaS)"}


def own_fair_value(price, pe_avg=None, pe_cur=None, pfcf_avg=None, pfcf_cur=None,
                   stock_type="normal_growth"):
    """Own-history fair value from the stock's 3yr-average multiple (extraction of
    entry_level_builder._own_fair_value — identical maths, clamps and preference order).
    Returns (fv | None, metric_used | None, ratio | None)."""
    price = _num(price)
    if not price:
        return None, None, None
    pe_avg, pe_cur = _num(pe_avg), _num(pe_cur)
    pf_avg, pf_cur = _num(pfcf_avg), _num(pfcf_cur)

    def ratio_ok(a, c):
        return a and c and a > 0 and c > 0 and RATIO_LO < (a / c) < RATIO_HI

    if stock_type in ("quality_compounder", "energy", "cyclical", "healthcare"):
        order = [("P/FCF", pf_avg, pf_cur), ("P/E", pe_avg, pe_cur)]
    else:
        order = [("P/E", pe_avg, pe_cur), ("P/FCF", pf_avg, pf_cur)]
    if stock_type == "cyclical":
        order = [("P/FCF", pf_avg, pf_cur)]

    for label, a, c in order:
        if ratio_ok(a, c):
            r = a / c
            fv = max(price * FV_CLAMP_LO, min(price * FV_CLAMP_HI, price * r))
            return round(fv, 2), label, round(r, 3)
    return None, None, None


def compose_fv(price, fv_own=None, fv_metric=None, analyst_target=None,
               analysts_lagging=False, apply_consensus_cap=True):
    """The base-FV hybrid (extraction of entry_level_builder base_fv block + rerank's
    consensus sanity-cap). Own-multiple primary; analyst target is a CAP only when analysts
    are NOT lagging. Consensus cap: composite FV may not exceed target * CONSENSUS_UPSIDE_CAP_MULT.
    Returns dict {fair_value, fv_basis, fv_conf, consensus_upside_capped}."""
    price, fv_own, analyst = _num(price), _num(fv_own), _num(analyst_target)
    capped = False
    if fv_own and analyst:
        if analysts_lagging:
            fv = fv_own
            basis = f"own {fv_metric} {fv_own} (analyst cap relaxed -- analysts lagging)"
        else:
            fv = min(fv_own, analyst)
            basis = f"min(own {fv_metric} {fv_own}, analyst {round(analyst, 2)})"
        conf = "high"
    elif fv_own:
        fv, basis, conf = fv_own, f"own {fv_metric} {fv_own} (no analyst cap)", "medium"
    elif analyst:
        fv, basis, conf = analyst, f"analyst target {round(analyst, 2)} (no usable own multiple)", "medium"
    else:
        fv, basis, conf = None, "no fair value (own multiple unusable, no analyst coverage)", "low"
    if apply_consensus_cap and fv is not None and analyst is not None and analyst > 0:
        capmult = _c("CONSENSUS_UPSIDE_CAP_MULT", 1.15)
        if fv > analyst * capmult:
            fv, capped = analyst * capmult, True
    return {"fair_value": round(fv, 2) if fv is not None else None, "fv_basis": basis,
            "fv_conf": conf, "consensus_upside_capped": capped}


def deployability_term(implied_upside, conf):
    """0-1 deployability = up_norm * confidence_weight (rerank's ranking recipe, Jul-26:
    entry-window price gap excluded). conf may be a label ('high') or a numeric weight."""
    if implied_upside is None:
        return 0.0
    cap = _c("SOURCE_UPSIDE_CAP", 0.60)
    up_norm = max(0.0, min(float(implied_upside), cap)) / cap
    cw = CONF_WEIGHT.get(conf, conf if isinstance(conf, (int, float)) else 0.6)
    return round(up_norm * float(cw), 4)


def analyst_signal(rating, num_analysts):
    """0-1 analyst signal = rating_score * coverage_reliability (extraction of
    rerank_watchlist._analyst_signal — identical maths; rerank imports this at P2)."""
    rating = (rating or "").lower()
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
    na = _num(num_analysts) or 0.0
    reliability = min(na, 20.0) / 20.0
    return rs * (0.5 + 0.5 * reliability)


def fv_composite_for_row(row, get=None):
    """Row adapter for screen/full_data field names (also correct on pre-run metrics dicts,
    which carry the same val_hist_* / target_price_mean names). Returns the full FV anatomy:
    {fair_value, fv_basis, fv_conf, implied_upside_fv, display_target_gap, deployability,
     analyst, consensus_upside_capped, input_missing}."""
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    price = _num(g(row, "current_price"))
    tgt = _num(g(row, "target_price_mean"))
    pe_cur = _num(g(row, "val_hist_current_pe")) or _num(g(row, "trailing_pe"))
    fv_own, fv_metric, _ = own_fair_value(
        price,
        pe_avg=g(row, "val_hist_pe_3yr_avg"), pe_cur=pe_cur,
        pfcf_avg=g(row, "val_hist_pfcf_3yr_avg"), pfcf_cur=g(row, "val_hist_current_pfcf"),
        stock_type=classify_stock_type_detail(row, get=g)["sector_type"])
    # lagging-analyst detection (entry_level_builder recipe; screen carries direction only)
    _dir = str(g(row, "est_rev_direction") or "").lower()
    _up = _num(g(row, "est_rev_eps_up_30d")) or 0
    _dn = _num(g(row, "est_rev_eps_down_30d")) or 0
    lagging = bool(tgt and price and price > tgt) or _dir in ("up", "improving") or (_up > _dn)
    comp = compose_fv(price, fv_own=fv_own, fv_metric=fv_metric, analyst_target=tgt,
                      analysts_lagging=lagging)
    fv = comp["fair_value"]
    up = (fv / price - 1.0) if (fv and price and price > 0) else None
    gap = (tgt / price - 1.0) if (tgt and price and price > 0) else None
    missing = []
    if price is None:
        missing.append("current_price")
    if fv is None:
        missing.append("fair_value")
    if tgt is None:
        missing.append("consensus_target")
    return {**comp,
            "implied_upside_fv": round(up, 4) if up is not None else None,
            "display_target_gap": round(gap, 4) if gap is not None else None,
            "deployability": deployability_term(up, comp["fv_conf"]) if up is not None else 0.0,
            "analyst": round(analyst_signal(g(row, "analyst_rating"), g(row, "num_analysts")), 4),
            "input_missing": ",".join(missing)}


if __name__ == "__main__":
    # F1: both anchors, analysts not lagging -> min() cap + high conf
    r1 = fv_composite_for_row({"current_price": 100.0, "val_hist_pe_3yr_avg": 30.0,
                               "val_hist_current_pe": 24.0, "target_price_mean": 110.0,
                               "analyst_rating": "buy", "num_analysts": 20,
                               "sector": "Technology", "industry": "Software - Application"})
    # own fv = 100*(30/24)=125 -> min(125, 110)=110? sector=software -> quality_compounder ->
    # P/FCF first (absent) -> P/E ok. lagging False (price<target, no dir) -> min(125,110)=110.
    assert r1["fair_value"] == 110.0 and r1["fv_conf"] == "high", r1
    assert abs(r1["implied_upside_fv"] - 0.10) < 1e-9, r1
    assert abs(r1["deployability"] - round((0.10 / 0.60) * 1.0, 4)) < 1e-6, r1
    assert abs(r1["analyst"] - 0.7) < 1e-9, r1
    # F2: lagging analysts (price above target) -> own FV uncapped by min(), but consensus
    # sanity-cap applies: fv_own 125 > 90*1.15=103.5 -> capped
    r2 = fv_composite_for_row({"current_price": 100.0, "val_hist_pe_3yr_avg": 25.0,
                               "val_hist_current_pe": 20.0, "target_price_mean": 90.0,
                               "analyst_rating": "hold", "num_analysts": 4})
    assert r2["consensus_upside_capped"] and abs(r2["fair_value"] - 103.5) < 1e-6, r2
    # F3: no own multiple, no target -> no FV, deployability 0, flagged
    r3 = fv_composite_for_row({"current_price": 50.0})
    assert r3["fair_value"] is None and r3["deployability"] == 0.0, r3
    assert "fair_value" in r3["input_missing"], r3
    # F4: analyst-only basis (pre-overlay screen state) -> medium conf
    r4 = fv_composite_for_row({"current_price": 50.0, "target_price_mean": 65.0,
                               "analyst_rating": "strong buy", "num_analysts": 10})
    assert r4["fv_conf"] == "medium" and abs(r4["fair_value"] - 65.0) < 1e-6, r4  # analyst basis; cap inert (65 <= 65*1.15)
    assert abs(r4["implied_upside_fv"] - 0.30) < 1e-9 and r4["fv_basis"].startswith("analyst target"), r4
    print("fv_composite SELF-TEST OK")
