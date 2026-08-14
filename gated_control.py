#!/usr/bin/env python3
"""
gated_control.py — D-24 Stage 6 / V-1 (09-Aug-2026). THE shared null-handling helper.

WHY THIS EXISTS
---------------
The same defect was found in three unrelated modules on the same day:

  1. `vci_risk_budget.position_risk` treated a MISSING `p_thesis` as p = 0. The comment said
     "conservative", and for the loss term it is — but the whole expression then reads as a
     confident risk number, and a caller-key typo flipped a DENY into an ADMIT on QBTS, the
     first deploy-eligible new VCI name in the sleeve's history. The wrong value, 1.55, was
     entirely plausible.
  2. `vci_deploy_eval.size_for` returns `size_liquidity_capped = False` on every name where
     `adv_usd` is null (9 of 9 observed) — "not capped" is indistinguishable from "never checked".
  3. `expected_return` returned `er_rerate = 0` for a missing re-rate — i.e. it asserted the
     multiple would not change, on 92% of every universe screened.

All three are ONE failure class: **a control fed a null returned a PASS-shaped value.** The
defence is not more rules; it is that the null has nowhere to hide.

CONTRACT
--------
    state, blocks, value = gated(v, control_name="liquidity_cap")

    v is None  ->  ("UNKNOWN", True,  None)      never a pass, never a zero
    v is set   ->  ("OK",      False, float(v))

`blocks` is what the caller must honour. A caller that wants to proceed anyway must do so
explicitly and record it — that is the point: the decision becomes visible in the artefact
instead of being made by a default argument.

Stdlib only. Self-test: python3 gated_control.py
"""
from __future__ import annotations

UNKNOWN = "UNKNOWN"
OK = "OK"


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):          # a bool is not a measurement
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def gated(value, *, control_name, registry=None):
    """None -> ('UNKNOWN', blocks). Never a pass, never a zero.

    `registry` (optional dict) accumulates {control_name: state} so a caller can publish every
    control it consulted, including the ones that could not be evaluated.
    """
    n = _num(value)
    state = OK if n is not None else UNKNOWN
    blocks = n is None
    if registry is not None:
        registry[control_name] = state
    return state, blocks, n


def unknown_controls(registry):
    """The controls that could not be evaluated — the list that must reach the artefact."""
    return sorted(k for k, v in (registry or {}).items() if v == UNKNOWN)


if __name__ == "__main__":
    reg = {}
    assert gated(None, control_name="p_thesis", registry=reg) == (UNKNOWN, True, None)
    assert gated(0.0, control_name="p_zero", registry=reg) == (OK, False, 0.0)
    assert gated("1.5", control_name="size", registry=reg) == (OK, False, 1.5)
    assert gated(True, control_name="bool_is_not_a_measurement", registry=reg)[0] == UNKNOWN
    assert unknown_controls(reg) == ["bool_is_not_a_measurement", "p_thesis"], unknown_controls(reg)
    # The distinction the whole module exists for: a real zero is NOT a missing value.
    assert gated(0.0, control_name="x")[1] is False and gated(None, control_name="x")[1] is True
    print("gated_control SELF-TEST OK")
