#!/usr/bin/env python3
"""
source_score.py — THE single Source Score (Jul-26 forward-led calibration, plan Part 1).

WHY THIS EXISTS
  The Source Score used to be computed in THREE places with TWO weight dicts
  (build_excel `_src`, build_email `_source_score`, rerank_watchlist `run()`), which was the
  root cause of screen / email / pre-run desync. This module collapses it to ONE function +
  ONE config (`scoring_config.SOURCE_WEIGHTS`), so future calibration is a one-line change
  inherited everywhere.

  Lightweight by design (only imports scoring_config) so it is safe to import anywhere,
  including the pre-run formatter step.

CONTRACT
  compute_source_score(...) is authoritative. The row-based convenience helpers
  (source_score_for_row / summary_eligible) exist so build_excel, build_email, rerank and the
  screener_core overlay gate share ONE eligibility + screen-score definition — no inline
  weighted sums or bespoke eligibility filters may remain in the consumers.
"""
import scoring_config as cfg


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
    """THE single Source Score (0-100). forward_axis 0-100 (price+margin axis); revisions/deployability/
    quality_norm/analyst are 0-1. Screen passes a deployability PROXY (upside-to-target, 0-1); the
    watchlist passes full entry-weight*upside. quality_norm defaults to part_a/28; valuation proxy
    (part_b/22) is used for deployability only if deployability is None (screen, no entry data)."""
    w = cfg.SOURCE_WEIGHTS
    f = (forward_axis or 0) / 100.0
    q = quality_norm if quality_norm is not None else ((part_a or 0) / 28.0)
    dep = deployability if deployability is not None else ((part_b or 0) / 22.0)
    return round(100 * (w["forward"] * f + w["revisions"] * (revisions or 0) + w["deployability"] * dep
                        + w["quality"] * q + w["analyst"] * (analyst or 0)), 1)


def source_score_for_row(row, get=None):
    """Screen/SUMMARY Source Score (0-100) for one scored row (dict-like).
    `get` is an optional accessor fn(row, key) -> value (build_email passes a get_field wrapper so the
    FIELD_MAP indirection is honoured). At screen time there is no entry/deployability data, so the
    valuation proxy (part_b/22) supplies the deployability term and analyst is 0."""
    g = get or (lambda r, k: r.get(k))
    fwd = _num(g(row, "forward_axis_score"))
    rev = _num(g(row, "revisions_score"))
    pa = _num(g(row, "part_a_score"))
    pb = _num(g(row, "part_b_score"))
    return compute_source_score(forward_axis=fwd,
                                revisions=(rev / 100.0 if rev is not None else 0.0),
                                deployability=None, quality_norm=None, analyst=0.0,
                                part_a=pa, part_b=pb)


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
