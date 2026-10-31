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
INVALID = "INVALID"      # B09-R12 (P-C, 24-Oct-2026): present but non-finite or outside its declared domain


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


def gated(value, *, control_name, registry=None, lo=None, hi=None):
    """None -> ('UNKNOWN', blocks). Never a pass, never a zero.

    B09-R12 (P-C, 24-Oct-2026): a NON-FINITE number (NaN, +/-inf) or a value outside the declared
    domain [lo, hi] is ('INVALID', blocks, None). It used to be ('OK', False, nan) - so a NaN loss,
    an infinite p, p = 1.875 or a negative size all became 'measured' risk and were ADMITTED (EV-0313/
    0315/0316/0317/0318), and an infinite ADV read as a non-binding liquidity cap (EV-0322).

    `registry` (optional dict) accumulates {control_name: state} so a caller can publish every
    control it consulted, including the ones that could not be evaluated.
    """
    import math as _m
    n = _num(value)
    if n is None:
        state = UNKNOWN
    elif not _m.isfinite(n) or (lo is not None and n < lo) or (hi is not None and n > hi):
        state, n = INVALID, None
    else:
        state = OK
    blocks = n is None
    if registry is not None:
        registry[control_name] = state
    return state, blocks, n


def unknown_controls(registry):
    """The controls that could not be evaluated (UNKNOWN) or were invalid (INVALID) — the list that
    must reach the artefact. Both BLOCK; neither is a measurement."""
    return sorted(k for k, v in (registry or {}).items() if v in (UNKNOWN, INVALID))


def _selftest():
    """R5.5: real assertions with labelled negative controls (P-C wrapped the former __main__ block)."""
    reg = {}
    assert gated(None, control_name="p_thesis", registry=reg) == (UNKNOWN, True, None), "negative control: None must block as UNKNOWN"
    assert gated(0.0, control_name="p_zero", registry=reg) == (OK, False, 0.0)
    assert gated("1.5", control_name="size", registry=reg) == (OK, False, 1.5)
    assert gated(True, control_name="bool_is_not_a_measurement", registry=reg)[0] == UNKNOWN
    assert unknown_controls(reg) == ["bool_is_not_a_measurement", "p_thesis"], unknown_controls(reg)
    # B09-R12 (P-C): non-finite and out-of-domain are INVALID and block; an in-domain value passes
    assert gated(float("nan"), control_name="L")[:2] == (INVALID, True), "negative control: NaN must not pass"
    assert gated(float("inf"), control_name="adv")[:2] == (INVALID, True), "negative control: inf must not pass"
    assert gated(1.875, control_name="p", lo=0.0, hi=1.0)[:2] == (INVALID, True), "negative control: p > 1 must not pass"
    assert gated(-1.0, control_name="size", lo=0.0)[:2] == (INVALID, True), "negative control: a negative size must not pass"
    assert gated(0.5, control_name="p", lo=0.0, hi=1.0) == (OK, False, 0.5), "positive control: an in-domain value passes"
    _r2 = {}; gated(float("nan"), control_name="x", registry=_r2); assert unknown_controls(_r2) == ["x"]
    # The distinction the whole module exists for: a real zero is NOT a missing value.
    assert gated(0.0, control_name="x")[1] is False and gated(None, control_name="x")[1] is True
    print("gated_control SELF-TEST OK")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_selftest())
