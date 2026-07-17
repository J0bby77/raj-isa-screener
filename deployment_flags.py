#!/usr/bin/env python3
"""
deployment_flags.py — shared deployment-gate pre-flags (redesign Part1 §1.3 / Part3 §13.4 / Postmortem).

ONE place computing the mechanical deployment-gate signals so every consumer agrees:
  - rerank_watchlist.py  (action-stack CAPS, §13.4 — disqualifier_flags reclassify the action)
  - step9_pre_builder.py (Step 9/10 checklist, CONTRACTS #4 gate_flags / forward_axis_flags)

Two flag classes, deliberately separated (the contrarian lesson: don't auto-eject good reversals):
  disqualifier_flags : HARD, auto-CAP signals the data confirms unambiguously AND that no live
                       catalyst rebuts — currently revision-direction-cut and near-52wk-low+deteriorating.
  review_flags       : SURFACED for Step 9/10 judgement, NOT auto-applied (price-falls-on-beat,
                       sector-multiple-stretched). Judgement-only gates (management instability /
                       guidance breach / AI-existential / thesis-break) are listed in JUDGMENT_GATES
                       as a checklist — they are NEVER set mechanically.
forward_axis_flags   : descriptive tags from the forward axis (forward strength, revision stage, est-rev dir).

Pure helper module — no heavy imports, no side effects. Additive: a consumer that doesn't read these
fields is unaffected.
"""

# Judgement-only gates the review must confirm at Step 9/10 — scaffold, NEVER auto-applied here.
JUDGMENT_GATES = ["mgmt_stability_dim11", "guidance_breach", "ai_existential", "thesis_break"]


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def compute_forward_axis_flags(td: dict) -> list:
    """Descriptive forward tags from a scored/metrics dict (forward_axis_score, revision_stage, est-rev)."""
    flags = []
    f = _num(td.get("forward_axis_score"))
    if f is not None:
        flags.append("forward_strong" if f >= 67 else "forward_moderate" if f >= 34 else "forward_weak")
    # Fix Pack A3 (P2): the "revision_stage:X" flag-string is RETIRED — revision_stage is a
    # first-class field on every step9_pre record (stamped by rerank + step9_pre_builder);
    # the stage GATE lives in t1_gates.stage_gate. Descriptive tags below are unchanged.
    d = (td.get("est_rev_direction") or "").lower()
    if d == "up":
        flags.append("est_rev_up")
    elif d == "down":
        flags.append("est_rev_down")
    return flags


def compute_gate_flags(td: dict, has_catalyst: bool = False) -> dict:
    """Return {disqualifier_flags, review_flags, forward_axis_flags} for one name.

    has_catalyst rebuts the revision-direction cap — the Door-C confirmed-reversal carve-out (§2):
    ORCL-type names have weak/negative revisions but win on a confirmed catalyst, so they must NOT be
    capped on revisions alone."""
    disq, review = [], []
    up = _num(td.get("est_rev_eps_up_30d")) or 0
    dn = _num(td.get("est_rev_eps_down_30d")) or 0
    direction = (td.get("est_rev_direction") or "").lower()
    pos = _num(td.get("position_52wk"))
    delta = _num(td.get("delta_score"))

    # HARD (auto-cap):
    # Revision-direction cut = the value-trap signature and the Door-A killer. A live catalyst rebuts it.
    # Jul-26 Part 6: read the CANONICAL est_rev_direction (screener_core's conservative-merge value,
    # vocabulary "improving"/"neutral"/"deteriorating"; legacy "up"/"down" still honoured). This is the
    # SINGLE source — the raw up/down counts are only a fallback when no direction field is present, so
    # a name the canonical merge deemed "neutral" is NOT re-cut here from a stale recompute.
    _deteriorating = direction in ("down", "deteriorating")
    if (_deteriorating or (not direction and dn > up)) and not has_catalyst:
        disq.append("revision_direction_down")
    # Near the 52-week low AND a deteriorating score = falling knife.
    if pos is not None and pos < 0.15 and delta is not None and delta < -2:
        disq.append("near_52wk_low_deteriorating")

    # SURFACED-only (judgement confirms; never auto-caps):
    if str(td.get("earnings_reaction") or "").lower().replace("-", "_") == "fell_on_beat":
        review.append("price_falls_on_beat")
    if _num(td.get("score_b_fwd_pe")) == 0 and _num(td.get("score_b_ev_ebitda")) == 0:
        review.append("sector_multiple_stretched")
    # Jul-2026 (Raj): RECENT-REVERSAL vs the 12-1m momentum the forward axis rewards. The 12-1m
    # window is blind to a sharp recent break, which is often informative for single-name /
    # idiosyncratic situations (biotech, deal-premium deflation). SURFACED for review — never an
    # auto-cap (recent moves are noisy on average).
    _p12 = _num(td.get("price_mom_12_1m_pct")); _r5 = _num(td.get("ret_5d_pct")); _r1m = _num(td.get("ret_1m_pct"))
    if _p12 is not None and _p12 >= 30 and ((_r5 is not None and _r5 <= -8) or (_r1m is not None and _r1m <= -12)):
        review.append("recent_reversal_vs_12_1m")

    # E4 — AI-disruption (recorded judgement score 0-5, from ai_disruption.py). A confirmed
    # existential score (5) IS a disqualifier (auto-cap); 4 (severe) is surfaced for review.
    _ai = _num(td.get("ai_disruption_score"))
    if _ai is not None:
        if _ai >= 5:
            disq.append("ai_existential")
        elif _ai >= 4:
            review.append("ai_disruption_severe")

    return {"disqualifier_flags": disq, "review_flags": review,
            "forward_axis_flags": compute_forward_axis_flags(td)}
