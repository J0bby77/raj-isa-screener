#!/usr/bin/env python3
"""
vci_source_score.py — THE single VCI Source Score (deployability rank). v2 (Jul-2026).

Spec: VCI_Forward_Led_Framework_Implementation_Jul2026.md §11 (FWDVCI) +
      VCI_Framework_Enhancements_Implementation_Jul2026.md E3/E6/E8 (v2).

WHY THIS EXISTS
  Rank != ACS. Rank = deployability = risk-adjusted remaining asymmetry, soonest. ACS is a
  quality FLOOR + confidence input, NOT the ranking axis.

v2 CHANGES
  E3 — the quality term consumes ACS-EX-ACS8 (`acs_ex_acs8`), not raw ACS. ACS8 == upside-to-FV
       == fv_asymmetry-1, so leaving it in ACS double-counts asymmetry in the rank. Eligibility
       still uses full ACS upstream (ACS8 belongs in the quality FLOOR); only the rank's quality
       term is de-duplicated here.
  E8 — quality weight cut 0.30 -> 0.15 (a 10-ACS-pt gap was worth 12 rank pts, overriding the
       forward signal). Freed weight -> catalyst + the new revisions term, NOT asymmetry.
  E6 — new `revisions` component: consensus revision velocity ("becoming legible"). Thin/no
       coverage -> neutral 0.5 (never penalise a pre-coverage archetype).

CONTRACT
  compute_vci_source_score(...) is authoritative (0-100). Eligibility gates (ACS floor, asymmetry
  floor, F1-F3, liquidity) are applied UPSTREAM (bottleneck_fv / vci_deploy_eval); this module only
  ORDERS the already-eligible set. A high score can never rescue a sub-floor name.
"""
from __future__ import annotations
import json
import os

try:
    import scoring_config as cfg
except Exception:
    cfg = object()

# v2 5-term prior (E8/E6). quality 0.15; revisions 0.15. Advisory/uncalibrated (§11.6).
_DEFAULT_WEIGHTS = {"asymmetry": 0.30, "quality": 0.15, "catalyst": 0.25, "signals": 0.15, "revisions": 0.15}
_SINGLE_WEIGHT_CAP = 0.35        # no single term may dominate until data says otherwise
_ASYM_NORM_CAP = 4.0             # fv_asymmetry normalised over [floor, ~4x]
_CAT_NEAR_DAYS = 90              # proximity = 1 at <=90d
_CAT_FAR_DAYS = 730             # proximity = 0 at >=730d
_ACS_FLOOR = 75                  # quality-norm anchor
_ACS_MAX = 100
_SIGNAL_MAX = 6


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").replace("x", "").strip())
    except (TypeError, ValueError):
        return None


# --- normalisers -----------------------------------------------------------------------------
def norm_asymmetry(fv_asymmetry, floor):
    """0 at the applicable floor, 1 at ~4x. Marginal-asymmetry interpretation."""
    a = _num(fv_asymmetry); f = _num(floor)
    if a is None or f is None or _ASYM_NORM_CAP <= f:
        return 0.0
    return _clip01((a - f) / (_ASYM_NORM_CAP - f))


def norm_quality(acs):
    """E3: pass ACS-EX-ACS8 here (the caller supplies acs_ex_acs8 when available)."""
    a = _num(acs)
    if a is None:
        return 0.0
    return _clip01((a - _ACS_FLOOR) / (_ACS_MAX - _ACS_FLOOR))


def norm_catalyst(days_to_catalyst):
    """1 at <=90d, linearly decaying to 0 at >=730d."""
    d = _num(days_to_catalyst)
    if d is None:
        return 0.0
    if d <= _CAT_NEAR_DAYS:
        return 1.0
    if d >= _CAT_FAR_DAYS:
        return 0.0
    return _clip01((_CAT_FAR_DAYS - d) / (_CAT_FAR_DAYS - _CAT_NEAR_DAYS))


def norm_signals(signal_count):
    s = _num(signal_count)
    if s is None:
        return 0.0
    return _clip01(s / _SIGNAL_MAX)


def norm_revisions(revision_velocity):
    """E6: revision_velocity is already 0-1. None / thin coverage -> neutral 0.5 (do not penalise a
    pre-coverage NVDA-2010 archetype)."""
    v = _num(revision_velocity)
    if v is None:
        return 0.5
    return _clip01(v)


def reduce_revision_velocity(up=None, down=None, n_analysts_delta=None, target_rev_pct=None):
    """E6 reducer -> 0-1 (or None if no data at all, so the caller can mark revisions_thin).
    Blends: (a) up/down estimate-revision balance, (b) analyst initiations (coverage growing =
    recognition), (c) mean-target revision direction. Neutral 0.5 anchor."""
    parts = []
    u, d = _num(up), _num(down)
    if u is not None and d is not None and (u + d) > 0:
        parts.append(u / (u + d))                       # 1.0 all-up, 0.0 all-down
    nd = _num(n_analysts_delta)
    if nd is not None:
        parts.append(_clip01(0.5 + 0.15 * nd))          # +1 initiation ~ +0.15
    tr = _num(target_rev_pct)
    if tr is not None:
        parts.append(_clip01(0.5 + tr / 40.0))          # +20% target rev ~ +0.5
    if not parts:
        return None
    return round(sum(parts) / len(parts), 4)


# --- weights (advisory, calibration-aware) --------------------------------------------------
def _normalise_weights(w: dict) -> dict:
    w = {k: max(0.0, min(_SINGLE_WEIGHT_CAP, float(v))) for k, v in w.items()}
    tot = sum(w.values()) or 1.0
    return {k: v / tot for k, v in w.items()}


def load_weights(calibration_state_path: str = None) -> dict:
    """Live-calibrated weights from vci_calibration_state.json if present & gate-passed, else the
    config prior VCI_SOURCE_WEIGHTS, else the module default. Always cap-normalised."""
    base = dict(getattr(cfg, "VCI_SOURCE_WEIGHTS", _DEFAULT_WEIGHTS))
    path = calibration_state_path or getattr(cfg, "VCI_CALIBRATION_STATE_PATH", None)
    if path and os.path.exists(path):
        try:
            with open(path) as fh:
                st = json.load(fh)
            if st.get("weights") and st.get("calibration_gate_passed"):
                base = st["weights"]
        except Exception:
            pass
    # ensure every component key exists (a calibrated 4-term state must not drop revisions)
    for k, v in _DEFAULT_WEIGHTS.items():
        base.setdefault(k, v)
    return _normalise_weights(base)


# --- THE score ------------------------------------------------------------------------------
def compute_vci_source_score(*, fv_asymmetry, floor, acs, days_to_catalyst, signal_count,
                             revision_velocity=None, acs_ex_acs8=None,
                             weights: dict = None, return_components: bool = False):
    """VCI Source Score (0-100). Deployability = risk-adjusted remaining asymmetry, soonest.
    E3: quality uses `acs_ex_acs8` when supplied (else falls back to `acs`).
    E6: `revision_velocity` (0-1) feeds the revisions component (None -> neutral 0.5)."""
    w = _normalise_weights(weights) if weights else load_weights()
    quality_input = acs_ex_acs8 if acs_ex_acs8 is not None else acs
    comp = {
        "asymmetry": norm_asymmetry(fv_asymmetry, floor),
        "quality":   norm_quality(quality_input),
        "catalyst":  norm_catalyst(days_to_catalyst),
        "signals":   norm_signals(signal_count),
        "revisions": norm_revisions(revision_velocity),
    }
    score = round(100 * sum(w.get(k, 0.0) * comp[k] for k in comp), 1)
    if return_components:
        return score, {"weights": w, "components": comp}
    return score


def vci_source_score_for_row(row, get=None):
    """Convenience helper for a scored VCI row (dict-like)."""
    g = get or (lambda r, k: r.get(k))
    return compute_vci_source_score(
        fv_asymmetry=g(row, "fv_asymmetry"),
        floor=g(row, "fv_floor"),
        acs=g(row, "acs") if g(row, "acs") is not None else g(row, "acs_total"),
        acs_ex_acs8=g(row, "acs_ex_acs8"),
        days_to_catalyst=g(row, "days_to_catalyst"),
        signal_count=g(row, "signal_count"),
        revision_velocity=g(row, "revision_velocity"),
    )


# --- inline self-test -----------------------------------------------------------------------
if __name__ == "__main__":
    # R-T2: forward decides. X lower ACS but higher asymmetry+nearer catalyst.
    X = compute_vci_source_score(fv_asymmetry=3.0, floor=2.0, acs=77, days_to_catalyst=45, signal_count=5)
    Y = compute_vci_source_score(fv_asymmetry=2.1, floor=2.0, acs=83, days_to_catalyst=400, signal_count=4)
    print(f"R-T2  X={X}  Y={Y}  -> X>Y? {X > Y}")
    assert X > Y

    # E8: quality no longer overrides forward. A more forward/less quality beats B.
    A = compute_vci_source_score(fv_asymmetry=2.6, floor=2.0, acs=76, days_to_catalyst=120, signal_count=5)
    B = compute_vci_source_score(fv_asymmetry=2.2, floor=2.0, acs=85, days_to_catalyst=120, signal_count=5)
    print(f"E8    A(fwd)={A}  B(quality)={B}  -> A>B? {A > B}")
    assert A > B, "at quality 0.15 the forward name must win"

    # E3: quality term invariant to asymmetry when acs_ex_acs8 fixed
    q1 = compute_vci_source_score(fv_asymmetry=2.2, floor=2.0, acs=99, acs_ex_acs8=80,
                                  days_to_catalyst=120, signal_count=5, return_components=True)[1]["components"]["quality"]
    q2 = compute_vci_source_score(fv_asymmetry=3.5, floor=2.0, acs=99, acs_ex_acs8=80,
                                  days_to_catalyst=120, signal_count=5, return_components=True)[1]["components"]["quality"]
    print(f"E3    quality invariant to asymmetry: {q1} == {q2}? {q1 == q2}")
    assert q1 == q2

    # E6: thin coverage -> 0.5; strong upgrades > flat
    assert norm_revisions(None) == 0.5
    up = compute_vci_source_score(fv_asymmetry=2.4, floor=2.0, acs=80, days_to_catalyst=120, signal_count=4, revision_velocity=0.9)
    flat = compute_vci_source_score(fv_asymmetry=2.4, floor=2.0, acs=80, days_to_catalyst=120, signal_count=4, revision_velocity=0.5)
    print(f"E6    upgrades={up} > flat={flat}? {up > flat}")
    assert up > flat
    rv = reduce_revision_velocity(up=4, down=1, n_analysts_delta=2, target_rev_pct=10)
    print(f"E6    reducer(up4,down1,+2 analysts,+10% tgt) = {rv}")
    assert rv is not None and rv > 0.5

    # weights: 5 keys sum to 1
    w = load_weights()
    print(f"weights: {w}  sum={round(sum(w.values()),4)}")
    assert abs(sum(w.values()) - 1.0) < 1e-6 and set(w) >= set(_DEFAULT_WEIGHTS)
    print("vci_source_score v2 self-test PASSED")
