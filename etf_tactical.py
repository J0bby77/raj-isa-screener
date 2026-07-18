#!/usr/bin/env python3
"""
etf_tactical.py — B4 Category-8 tactical UCITS-ETF validity gate (P3 hook, 18-Jul-2026).

Doc B §B4: Step 8 Category 8 = tactical/factor UCITS-ETF positions exploiting the
preclearance exemption (zero compliance friction, instant reversibility). The gate is
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
    if not ucits_broad_based:
        reasons.append("not UCITS broad-based (two-line test fail — preclearance exemption void)")
    if position_pct is None or position_pct > float(_c("ETF_TACTICAL_MAX_POSITION_PCT", 5.0)):
        reasons.append(f"position > {_c('ETF_TACTICAL_MAX_POSITION_PCT', 5.0)}% ISA cap")
    if tactical_total_pct_after is None or tactical_total_pct_after > float(_c("ETF_TACTICAL_MAX_TOTAL_PCT", 10.0)):
        reasons.append(f"tactical total > {_c('ETF_TACTICAL_MAX_TOTAL_PCT', 10.0)}% ISA cap")
    if not min_hold_ok:
        reasons.append(f"intended hold < {_c('ETF_TACTICAL_MIN_HOLD_MONTHS', 3)} months (anti-churn)")
    if not thesis_has_trigger_and_exit:
        reasons.append("thesis missing regime/factor trigger + exit condition at entry")
    menu = _c("REGIME_B4_MENU", {})
    if regime_state is not None:
        allowed = menu.get(str(regime_state).upper(), [])
        if tilt is not None and tilt not in allowed and not documented_cause:
            reasons.append(f"tilt '{tilt}' not in {str(regime_state).upper()} menu {allowed} "
                           "and no documented cause (B7(3))")
    return (len(reasons) == 0), reasons


if __name__ == "__main__":
    ok, r = etf_tactical_valid(ucits_broad_based=True, position_pct=4.0,
                               tactical_total_pct_after=8.0, min_hold_ok=True,
                               thesis_has_trigger_and_exit=True,
                               regime_state="LATE_CYCLE", tilt="min_vol")
    assert ok and not r, r
    ok, r = etf_tactical_valid(ucits_broad_based=False, position_pct=6.0,
                               tactical_total_pct_after=11.0, min_hold_ok=False,
                               thesis_has_trigger_and_exit=False,
                               regime_state="RISK_ON", tilt="min_vol")
    assert not ok and len(r) == 6, r
    ok, r = etf_tactical_valid(ucits_broad_based=True, position_pct=3.0,
                               tactical_total_pct_after=3.0, min_hold_ok=True,
                               thesis_has_trigger_and_exit=True,
                               regime_state="RISK_ON", tilt="min_vol", documented_cause=True)
    assert ok, r
    print("etf_tactical SELF-TEST OK (U-B4)")
