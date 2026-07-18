#!/usr/bin/env python3
"""
source_score.py — THE single Source Score (Jul-26 forward-led calibration, plan Part 1;
UNIFIED screen=deploy 12-Jul-2026, Fix Pack A6).

WHY THIS EXISTS
  The Source Score used to be computed in THREE places with TWO weight dicts
  (build_excel `_src`, build_email `_source_score`, rerank_watchlist `run()`), which was the
  root cause of screen / email / pre-run desync. This module collapses it to ONE function +
  ONE config (`scoring_config.SOURCE_WEIGHTS`), so future calibration is a one-line change
  inherited everywhere.

A6 UNIFICATION (Raj 12-Jul: "the growth stock source score should be the same as the
deployability source score, not different"):
  With cfg.UNIFIED_SOURCE=True (live), compute_source_score is the ONLY recipe and takes REAL
  inputs at BOTH stages: deployability = implied_upside_fv x confidence (fv_composite.py — the
  same composite FV rerank ranks on) and analyst = rating x coverage-reliability, identically
  at screen and pre-run. The old screen-time Part-B/22 deployability proxy and zeroed analyst
  (an effective 0.75-forward scale) are RETIRED: a genuinely unavailable input scores 0 with an
  `input_missing` flag — never a silently different quantity. `SUMMARY_SOURCE_FLOOR` is then one
  number with one meaning everywhere.
  ROLLBACK: cfg.UNIFIED_SOURCE=False restores the legacy proxy path (kept one cycle, delete P3).

CONTRACT
  compute_source_score(...) is authoritative. Row-based consumers use
  source_score_components_for_row (full anatomy — build_excel SUMMARY breakdown columns read
  this, never recompute) / source_score_for_row (score only) / summary_eligible /
  select_summary (Fix Pack A1: THE floor-based SUMMARY selection, shared by build_excel,
  screener_core run_qa and build_email — no inline selection may remain in the consumers).

  Lightweight by design (imports scoring_config + fv_composite, both stdlib-only) so it is
  safe to import anywhere, including the pre-run formatter step.
"""
import scoring_config as cfg
import fv_composite as _fv


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def compute_source_score(*, forward_axis, revisions=0.0, deployability=None,
                         quality_norm=None, analyst=0.0, part_a=None, part_b=None):
    """THE single Source Score (0-100). forward_axis 0-100 (price+margin axis); revisions/
    deployability/quality_norm/analyst are 0-1. quality_norm defaults to part_a/28 (a real
    input at both stages). UNIFIED_SOURCE=True (A6): a missing deployability input scores 0
    (input_missing doctrine) — the legacy part_b/22 valuation proxy is used ONLY when the
    rollback flag is False."""
    w = cfg.SOURCE_WEIGHTS
    f = (forward_axis or 0) / 100.0
    q = quality_norm if quality_norm is not None else ((part_a or 0) / 28.0)
    if deployability is None:
        dep = 0.0 if getattr(cfg, "UNIFIED_SOURCE", True) else ((part_b or 0) / 22.0)
    else:
        dep = deployability
    return round(100 * (w["forward"] * f + w["revisions"] * (revisions or 0) + w["deployability"] * dep
                        + w["quality"] * q + w["analyst"] * (analyst or 0)), 1)


def source_score_components_for_row(row, get=None):
    """Full Source-Score anatomy for one row (dict-like) — the SAME recipe at screen and
    pre-run (A6). Returns pre-weight terms, weighted contributions, the score, and the FV
    anatomy (implied_upside_fv / display_target_gap / fv_basis / input_missing). build_excel
    SUMMARY breakdown columns and screener_core's stamping consume THIS (never recompute)."""
    g = get or (lambda r, k: r.get(k))
    w = cfg.SOURCE_WEIGHTS
    unified = getattr(cfg, "UNIFIED_SOURCE", True)
    fwd = _num(g(row, "forward_axis_score")) or 0.0
    rev01 = (_num(g(row, "revisions_score")) or 0.0) / 100.0
    pa = _num(g(row, "part_a_score"))
    qual01 = (pa or 0.0) / 28.0
    if unified:
        anat = _fv.fv_composite_for_row(row, get=g)
        dep01 = anat["deployability"]
        an01 = anat["analyst"]
    else:  # legacy proxy path (rollback only)
        pb = _num(g(row, "part_b_score"))
        anat = {"implied_upside_fv": None, "display_target_gap": None,
                "fv_basis": "LEGACY_PROXY part_b/22", "fv_conf": None,
                "consensus_upside_capped": False, "input_missing": ""}
        dep01 = (pb or 0.0) / 22.0
        an01 = 0.0
    score = compute_source_score(forward_axis=fwd, revisions=rev01, deployability=dep01,
                                 quality_norm=qual01, analyst=an01)
    return {"source_score": score, "screen_source": score,
            "src_fwd_raw": round(fwd, 1),            "src_fwd_w": round(w["forward"] * fwd, 1),
            "src_rev_raw": round(rev01, 3),          "src_rev_w": round(100 * w["revisions"] * rev01, 1),
            "src_deploy_raw": round(dep01, 3),       "src_deploy_w": round(100 * w["deployability"] * dep01, 1),
            "src_qual_raw": round(qual01, 3),        "src_qual_w": round(100 * w["quality"] * qual01, 1),
            "src_analyst_raw": round(an01, 3),       "src_analyst_w": round(100 * w["analyst"] * an01, 1),
            "implied_upside_fv": anat.get("implied_upside_fv"),
            "display_target_gap": anat.get("display_target_gap"),
            "fv_basis": anat.get("fv_basis"), "fv_conf": anat.get("fv_conf"),
            "consensus_upside_capped": anat.get("consensus_upside_capped"),
            "source_input_missing": anat.get("input_missing", "")}


def source_score_for_row(row, get=None):
    """Screen/SUMMARY Source Score (0-100) for one scored row (dict-like). A6: unified
    recipe — REAL deployability (implied_upside_fv x confidence) + REAL analyst signal,
    identical to the pre-run compute. `get` is an optional accessor fn(row, key) -> value
    (build_email passes a get_field wrapper so the FIELD_MAP indirection is honoured)."""
    return source_score_components_for_row(row, get=get)["source_score"]


def summary_eligible(row, get=None):
    """Unified SUMMARY / overlay VIABILITY eligibility (plan Parts 4 & 5). Multi-door:
    Part A >= FORWARD_ELIG_PART_A_FLOOR, Part B >= SUMMARY_PART_B_FLOOR (Jul-26: 10), est-rev not
    deteriorating, not a fail-status, and forward-runway stage not excluded. Balance-sheet risk stays
    gated by the separate ND/EBITDA MANDATORY_MINIMUM_FAIL, reflected in final_status."""
    g = get or (lambda r, k: r.get(k))
    pa = _num(g(row, "part_a_score"))
    pb = _num(g(row, "part_b_score"))
    if pa is None or pb is None:
        return False
    paf = getattr(cfg, "FORWARD_ELIG_PART_A_FLOOR", 10)
    pbf = getattr(cfg, "SUMMARY_PART_B_FLOOR", 10)
    st = str(g(row, "final_status") or "").upper()
    ok = st not in {"HARD_GATE_FAIL", "MANDATORY_MINIMUM_FAIL", "UNRESOLVED_HARD_GATE_NOT_RANKABLE"}
    notdet = str(g(row, "est_rev_direction") or "").lower() != "deteriorating"
    stage = str(g(row, "revision_stage") or "")
    stage_ok = stage not in set(getattr(cfg, "SUMMARY_STAGE_EXCLUDE", []))
    return (pa >= paf) and (pb >= pbf) and notdet and ok and stage_ok


def select_summary(rows, get=None):
    """Fix Pack A1 (D4) — THE floor-based SUMMARY selection, one implementation for
    build_excel, screener_core (run_qa counts) and build_email:
      select = all rows where summary_eligible AND unified source_score >= SUMMARY_SOURCE_FLOOR,
      sorted desc (ticker tiebreak, deterministic), truncated at SUMMARY_MAX_COUNT.
    Fixed-count backfill is RETIRED — membership certifies ONE quality bar per D4.
    Returns (selected, qa): selected = list of (row, score); qa = {summary_count,
    summary_eligible_count, summary_floor, summary_cap, summary_thin_warning}."""
    g = get or (lambda r, k: r.get(k))
    floor = float(getattr(cfg, "SUMMARY_SOURCE_FLOOR", 70.0))
    cap = int(getattr(cfg, "SUMMARY_MAX_COUNT", 40))
    warn_below = int(getattr(cfg, "SUMMARY_MIN_WARN", 10))
    scored = []
    n_elig = 0
    for r in rows:
        if not summary_eligible(r, get=get):
            continue
        n_elig += 1
        sc = source_score_for_row(r, get=get)
        if sc is not None and sc >= floor:
            scored.append((r, sc))
    scored.sort(key=lambda t: (-t[1], str(g(t[0], "ticker") or "")))
    selected = scored[:cap]
    qa = {"summary_count": len(selected), "summary_eligible_count": n_elig,
          "summary_floor": floor, "summary_cap": cap,
          "summary_thin_warning": len(selected) < warn_below}
    return selected, qa


def door_flags_for_row(row, regime_state=None, get=None):
    """B7(2) SHADOW (18-Jul-26) — evaluate every admission door's criteria for one row (Doc B B7).
    Missing data FAILS a door criterion: a door ADMITS, so unseen data cannot admit (inverse of
    T1's NO_DATA-pass, which exists to avoid BLOCKING on unseen data). Shadow-only while
    cfg.REGIME_DOORS_ACTIVE is False — tags flow to full_data / score_panel / SUMMARY so each
    door's forward performance is measurable from day 1; selection is UNCHANGED."""
    g = get or (lambda r, k: r.get(k))
    pa = _num(g(row, "part_a_score"))
    sc = source_score_for_row(row, get=get)
    floor = float(getattr(cfg, "SUMMARY_SOURCE_FLOOR", 70.0))
    momentum = bool(summary_eligible(row, get=get) and sc is not None and sc >= floor)
    nd = _num(g(row, "net_debt_ebitda"))
    fcfy = _num(g(row, "fcf_pos_years"))
    if fcfy is None:
        fcfy = _num(g(row, "fcf_positive_years"))
    omt = str(g(row, "op_margin_trend") or "").lower()
    payout = _num(g(row, "div_payout_fcf"))
    quality = bool(
        pa is not None and pa >= getattr(cfg, "DOOR_QUALITY_PART_A_MIN", 20)
        and nd is not None and nd < getattr(cfg, "DOOR_QUALITY_ND_EBITDA_MAX", 1.5)
        and fcfy is not None and fcfy >= getattr(cfg, "DOOR_QUALITY_FCF_YEARS_MIN", 5)
        and omt in ("improving", "flat", "stable")
        and (payout is None or payout < getattr(cfg, "DOOR_QUALITY_DIV_PAYOUT_FCF_MAX", 0.8)))
    price = _num(g(row, "current_price"))
    hi = _num(g(row, "high_52wk"))
    off_high = (1.0 - price / hi) * 100.0 if (price and hi and hi > 0) else None
    below = off_high is not None and off_high >= getattr(cfg, "DOOR_INFLECTION_OFF_HIGH_MIN_PCT", 25.0)
    improving = str(g(row, "est_rev_direction") or "").lower() == "improving"
    inflection = bool(pa is not None and pa >= getattr(cfg, "DOOR_INFLECTION_PART_A_MIN", 16)
                      and below and improving)
    doors = [d for d, ok in (("momentum", momentum), ("quality", quality),
                             ("inflection", inflection)) if ok]
    regime = (str(regime_state).upper() if regime_state else None)
    open_doors = getattr(cfg, "REGIME_OPEN_DOORS", {}).get(regime, ["momentum"])
    return {"door_momentum": momentum, "door_quality": quality, "door_inflection": inflection,
            "door": ",".join(doors),
            "door_admit_shadow": ",".join(d for d in doors if d in open_doors),
            "regime_at_screen": regime or "UNKNOWN"}
