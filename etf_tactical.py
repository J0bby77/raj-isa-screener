#!/usr/bin/env python3
"""
etf_tactical.py — B4 Category-8 tactical UCITS-ETF validity gate (P3 hook, 18-Jul-2026).

Doc B §B4: Step 8 Category 8 = tactical/factor UCITS-ETF positions exploiting the
preclearance exemption (zero compliance friction, instant reversibility).

COMPLIANCE-REGIME NOTE (29-Jul-2026): while compliance.active() is False the preclearance
exemption no longer confers an advantage (nothing needs preclearance), so the UCITS/broad-based
two-line test degrades from a GATE to a recorded [WARN] + restoration-risk marker. All other
conditions — 5%/10% caps, three-month anti-churn hold, trigger+exit thesis, regime menu — are
framework rules and are UNCHANGED. Category 8 keeps its reversibility/breadth rationale; it
simply loses its friction advantage over a direct stock while the regime is paused. The gate is
MECHANICAL; selection of WHICH exposure is JUDGMENT by design (a door, not an autopilot) —
Step 9 conviction machinery applies unchanged. Candidate shortlist is researched at the run
where first triggered, never pre-built (avoids stale lists).

B7(3): the regime menu (cfg.REGIME_B4_MENU) constrains which tilts are permitted per
regime_state; RISK_ON permits no tactical tilt without documented cause.
"""


def _c(name, default):
    try:
        import scoring_config as cfg
        return getattr(cfg, name, default)
    except Exception:
        return default


def _strict_bool(v):
    """B09-R10 (P-C, 24-Oct-2026): True only for a declared True (bool True / 'true' / 'yes' / 1). The string
    'false' used to be truthy, so a documented_cause or thesis flag of 'false' PASSED (EV-0254)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def _finite_pct(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if (f == f and f not in (float("inf"), float("-inf"))) else None


def etf_tactical_valid(*, ucits_broad_based, position_pct, tactical_total_pct_after,
                       min_hold_ok, thesis_has_trigger_and_exit, regime_state=None,
                       tilt=None, documented_cause=False):
    """U-B4 validity gate — ALL mechanical conditions must hold. Returns (valid, reasons).
    ucits_broad_based: instrument passes the two-line UCITS + broad-based test (mechanical).
    position_pct: this position as % of TOTAL ISA (D17). tactical_total_pct_after: all
    Category-8 positions incl. this one. min_hold_ok: intended hold >= 3 months (anti-churn).
    thesis_has_trigger_and_exit: thesis states the regime/factor trigger AND the exit
    condition at entry (ledger-logged like any trade). regime_state+tilt: checked against
    cfg.REGIME_B4_MENU (B7(3)); a tilt outside the menu needs documented_cause."""
    reasons = []
    blocking = 0                      # reasons that actually invalidate (WARNs do not)
    try:
        import compliance as _comp
        _regime_active = _comp.active()
    except Exception:
        _regime_active = True         # fail safe: treat as if a PAD regime binds
    min_hold_ok = _strict_bool(min_hold_ok)
    thesis_has_trigger_and_exit = _strict_bool(thesis_has_trigger_and_exit)
    documented_cause = _strict_bool(documented_cause)
    ucits_broad_based = _strict_bool(ucits_broad_based)
    if not ucits_broad_based:
        if _regime_active:
            reasons.append("not UCITS broad-based (two-line test fail — preclearance exemption void)")
            blocking += 1
        else:
            # No employer PAD regime: there is no preclearance exemption left to exploit, so the
            # two-line test no longer GATES the category. It is still RECORDED, because a
            # narrow-based holding would need preclearance (and a 30-day hold) the day a bank
            # regime is restored — compliance.py R4.
            reasons.append("[WARN] not UCITS broad-based — permitted while PAD regime is paused, "
                           "but carries restoration risk if a bank regime returns "
                           "(compliance.restoration_risk)")
    # ⚑ B09-R10 (P-C): the DECLARED config name is ETF_TACTICAL_MAX_POS_PCT (scoring_config B4/D16, ISA-0434
    #   removed the long alias) - reading the long name always took the 5.0 default, so an approved change
    #   of the cap could never bind (EV-0246). NaN / negative sizes are refused (EV-0247/0248).
    _cap_pos = float(_c("ETF_TACTICAL_MAX_POS_PCT", 5.0))
    _cap_tot = float(_c("ETF_TACTICAL_MAX_TOTAL_PCT", 10.0))
    _pp, _tt = _finite_pct(position_pct), _finite_pct(tactical_total_pct_after)
    if _pp is None or _pp < 0 or _pp > _cap_pos:
        reasons.append(f"position {position_pct!r} missing/non-finite/negative or > {_cap_pos}% ISA cap")
        blocking += 1
    if _tt is None or _tt < 0 or _tt > _cap_tot or (_pp is not None and _tt < _pp):
        reasons.append(f"tactical total {tactical_total_pct_after!r} missing/non-finite/negative, below the "
                       f"position, or > {_cap_tot}% ISA cap")
        blocking += 1
    if not min_hold_ok:
        reasons.append(f"intended hold < {_c('ETF_TACTICAL_MIN_HOLD_MONTHS', 3)} months (anti-churn)")
        blocking += 1
    if not thesis_has_trigger_and_exit:
        reasons.append("thesis missing regime/factor trigger + exit condition at entry")
        blocking += 1
    menu = _c("REGIME_B4_MENU", {})
    # ⚑ B09-R10 (P-C, EV-0250/0251): the regime menu is a CONTROL; a missing regime or a missing tilt
    #   cannot be checked against it, so it BLOCKS (R4.3) unless a cause is documented. Both used to bypass.
    if regime_state is None or str(regime_state).upper() not in menu:
        if not documented_cause:
            reasons.append(f"regime_state {regime_state!r} unknown - the B7(3) menu cannot be evaluated")
            blocking += 1
    elif tilt is None:
        if not documented_cause:
            reasons.append("tilt not declared - the B7(3) menu cannot be evaluated")
            blocking += 1
    else:
        allowed = menu.get(str(regime_state).upper(), [])
        if tilt not in allowed and not documented_cause:
            reasons.append(f"tilt '{tilt}' not in {str(regime_state).upper()} menu {allowed} "
                           "and no documented cause (B7(3))")
            blocking += 1
    return (blocking == 0), reasons


def _selftest():
    """R5.5: real assertions with labelled negative controls (P-C wrapped the former __main__ block)."""
    ok, r = etf_tactical_valid(ucits_broad_based=True, position_pct=4.0,
                               tactical_total_pct_after=8.0, min_hold_ok=True,
                               thesis_has_trigger_and_exit=True,
                               regime_state="LATE_CYCLE", tilt="min_vol")
    assert ok and not r, ("positive control: a valid LATE_CYCLE min_vol tilt passes", r)
    # B09-R10 (P-C) negative controls: NaN / negative / missing regime / missing tilt / truthy 'false'
    for _kw in ({"position_pct": float("nan")}, {"position_pct": -1.0}, {"regime_state": None},
                {"tilt": None}, {"thesis_has_trigger_and_exit": "false"}):
        _b = dict(ucits_broad_based=True, position_pct=4.0, tactical_total_pct_after=8.0, min_hold_ok=True,
                  thesis_has_trigger_and_exit=True, regime_state="LATE_CYCLE", tilt="min_vol")
        _b.update(_kw)
        assert not etf_tactical_valid(**_b)[0], ("negative control: a broken input must not pass", _kw)
    ok, r = etf_tactical_valid(ucits_broad_based=False, position_pct=6.0,
                               tactical_total_pct_after=11.0, min_hold_ok=False,
                               thesis_has_trigger_and_exit=False,
                               regime_state="RISK_ON", tilt="min_vol")
    assert not ok and len(r) == 6, r   # 6 reason strings under either regime
    import compliance as _c2
    assert (sum(1 for x in r if not x.startswith("[WARN]")) == (6 if _c2.active() else 5)), r
    ok, r = etf_tactical_valid(ucits_broad_based=True, position_pct=3.0,
                               tactical_total_pct_after=3.0, min_hold_ok=True,
                               thesis_has_trigger_and_exit=True,
                               regime_state="RISK_ON", tilt="min_vol", documented_cause=True)
    assert ok, r
    print("etf_tactical SELF-TEST OK (U-B4)")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_selftest())
