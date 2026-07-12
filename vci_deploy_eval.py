#!/usr/bin/env python3
"""
vci_deploy_eval.py — the single forward-led "evaluate a VCI candidate for deployment" call. v2.

Spec: VCI_Forward_Led_Framework_Implementation_Jul2026.md §4/§9.2/§11 (FWDVCI) +
      VCI_Framework_Enhancements_Implementation_Jul2026.md E1/E2/E5/E7 (v2; E4 is orchestrated by
      vci_risk_budget at Step 8/pre-run, using the p/L/size this verdict exposes).

Composes bottleneck_fv (win-case FV + P25 CI + derived p·L floor + eligibility) + vci_source_score
(deployability rank, quality-ex-ACS8 + revisions) + §9.2 sizing (liquidity-capped) into ONE verdict
the VCI run, the monthly pre-run, and Step 8 all consume — one definition, no desync.

Network-free (no yfinance).
"""
from __future__ import annotations
import bottleneck_fv as _bfv
import vci_source_score as _vss

try:
    import scoring_config as cfg
except Exception:
    cfg = object()


def _c(name, default):
    return getattr(cfg, name, default)


def size_for(acs, signal_count, days_to_catalyst, eligible, adv_usd=None, portfolio_value=None):
    """§9.2 pre-catalyst starter size, E5 liquidity-capped. Returns (size_pct, note, liq_capped)."""
    if not eligible or acs is None:
        return 0.0, "not eligible", False
    hi = _c("VCI_HIGH_ACS", 80)
    exc = _c("VCI_EXCEPTIONAL_SIZE_PCT", 1.5)
    full = _c("VCI_STARTER_SIZE_PCT", 1.0)
    mid = _c("VCI_STARTER_SIZE_PCT_MID", 0.75)
    near = (days_to_catalyst is not None and days_to_catalyst < 90)
    if acs >= 85 and (signal_count or 0) >= 3 and near:
        base, note = exc, "exceptional (ACS>=85 + signals>=3 + catalyst<90d) -> 1.5% cap"
    elif acs >= hi:
        base, note = full, f"ACS>={hi} 'high' -> full {full}% ahead of catalyst"
    elif acs >= 75:
        base, note = mid, f"ACS 75-{hi-1} -> {mid}% ahead, scale to {full}% on confirmation"
    else:
        return 0.0, "below ACS floor", False

    # E5 liquidity cap: position value <= VCI_MAX_PCT_ADV of ADV
    liq_capped = False
    if adv_usd and portfolio_value and portfolio_value > 0:
        liq_cap_pct = float(_c("VCI_MAX_PCT_ADV", 0.10)) * float(adv_usd) / float(portfolio_value) * 100.0
        if liq_cap_pct < base:
            base = round(liq_cap_pct, 3)
            note += f" | ADV-capped to {base}%"
            liq_capped = True
    return base, note, liq_capped


def evaluate_candidate(*, ticker, acs, price, fv_inputs=None, bottleneck_fv_per_share=None,
                       asset_structure=None, has_catalyst=False, days_to_catalyst=None,
                       signal_count=0, mgmt_unstable=False, falls_on_beat=False,
                       analyst_fv_per_share=None, weights=None,
                       # --- v2 ---
                       catalyst_type=None, catalyst_domain=None, p_thesis=None, L=None,
                       acs_ex_acs8=None, revision_velocity=None,
                       adv_usd=None, portfolio_value=None, fv_prev=None, price_prev=None,
                       floor_mode=None):
    """Full forward-led v2 verdict for one candidate. Supply EITHER fv_inputs (§10.2 dict, enables
    the P25 CI) OR a precomputed bottleneck_fv_per_share (scalar -> manual confirm under E2)."""
    has_structured = fv_inputs is not None

    # 1) win-case FV (+ P25 CI when structured) and asymmetry
    if has_structured:
        fv = _bfv.compute_bottleneck_fv_ci(fv_inputs, price, asset_structure=asset_structure,
                                           analyst_fv_per_share=analyst_fv_per_share)
    else:
        fv = _bfv.BottleneckFV(bottleneck_fv_per_share=bottleneck_fv_per_share,
                               asset_structure=(asset_structure or "single_asset"),
                               floor=_bfv.select_floor(asset_structure),
                               fv_source=("modeled" if bottleneck_fv_per_share else "estimated"))
        if bottleneck_fv_per_share and price and price > 0:
            fv.fv_asymmetry = round(bottleneck_fv_per_share / price, 4)
            fv.fv_asymmetry_p50 = fv.fv_asymmetry
            fv.fv_asymmetry_p25 = fv.fv_asymmetry          # no CI without structured inputs

    # 2) E1 derived p·L floor (mode-gated; falls back to fixed tier)
    if p_thesis is None or L is None:
        _p, _L = _bfv.lookup_priors(asset_structure, catalyst_type)
        p_thesis = p_thesis if p_thesis is not None else _p
        L = L if L is not None else _L
    fv.floor, fv.floor_source = _bfv.derive_floor(asset_structure, catalyst_type, p_thesis, L,
                                                  days_to_catalyst, mode=floor_mode)

    # 3) §4 eligibility (P25 asymmetry; structured mandate; cross-check) — network-free
    elig = _bfv.evaluate_deploy_eligibility(
        acs_total=acs, fv=fv, has_catalyst=has_catalyst, mgmt_unstable=mgmt_unstable,
        falls_on_beat=falls_on_beat, has_structured_inputs=has_structured)

    # 3b) E5 liquidity floor-gate: below min ADV -> not auto-eligible (manual)
    reasons = list(elig.reasons)
    deploy_eligible = elig.deploy_eligible
    require_manual = elig.require_manual_confirm
    min_adv = float(_c("VCI_MIN_ADV_USD", 0) or 0)
    if adv_usd is not None and min_adv and float(adv_usd) < min_adv:
        deploy_eligible = False
        require_manual = True
        reasons.append(f"adv {adv_usd:.0f} < min {min_adv:.0f}:illiquid_manual")

    # 4) deployability rank (advisory) — quality-ex-ACS8 + revisions; rank uses P50 asymmetry
    rank_asym = fv.fv_asymmetry_p50 if fv.fv_asymmetry_p50 is not None else fv.fv_asymmetry
    vss = _vss.compute_vci_source_score(
        fv_asymmetry=rank_asym, floor=fv.floor, acs=acs, acs_ex_acs8=acs_ex_acs8,
        days_to_catalyst=days_to_catalyst, signal_count=signal_count,
        revision_velocity=revision_velocity, weights=weights)

    # 5) size (E5 liquidity-capped)
    size_pct, size_note, liq_capped = size_for(acs, signal_count, days_to_catalyst, deploy_eligible,
                                               adv_usd=adv_usd, portfolio_value=portfolio_value)

    # 6) E7 compression cause (needs prior FV / price)
    cause = _bfv.compression_cause(fv.bottleneck_fv_per_share, fv_prev, price, price_prev)

    # 7) E4 inputs (risk budget consumes these downstream)
    expected_loss = round((size_pct or 0.0) * float(L if L is not None else 1.0)
                          * (1.0 - float(p_thesis if p_thesis is not None else 0.0)), 4)

    return {
        "ticker": ticker, "acs": acs, "acs_ex_acs8": acs_ex_acs8, "price": price,
        "asset_structure": fv.asset_structure, "fv_floor": fv.floor, "floor_source": fv.floor_source,
        "p_thesis": p_thesis, "L": L, "catalyst_type": catalyst_type, "catalyst_domain": catalyst_domain,
        "bottleneck_fv_per_share": fv.bottleneck_fv_per_share,
        "fv_p25": fv.fv_p25, "fv_p50": fv.fv_p50, "fv_p75": fv.fv_p75,
        "fv_asymmetry": fv.fv_asymmetry_p50 if fv.fv_asymmetry_p50 is not None else fv.fv_asymmetry,
        "fv_asymmetry_p25": fv.fv_asymmetry_p25, "fv_source": fv.fv_source,
        "fv_crosscheck_warn": fv.fv_crosscheck_warn,
        "deploy_eligible": deploy_eligible, "require_manual_confirm": require_manual,
        "eligibility_reasons": reasons,
        "vci_source_score": vss, "rank_mode": _c("VCI_RANK_MODE", "advisory"),
        "revision_velocity": revision_velocity,
        "size_pct": size_pct, "size_note": size_note, "size_liquidity_capped": liq_capped,
        "adv_usd": adv_usd, "expected_loss_pct_isa": expected_loss,
        "asymmetry_compression_cause": cause,
        "days_to_catalyst": days_to_catalyst, "signal_count": signal_count,
    }


def rank_eligible(entries):
    """(eligible_sorted_desc_by_vci_source_score, ineligible). §11 deployment order."""
    eligible = [e for e in entries if e.get("deploy_eligible")]
    ineligible = [e for e in entries if not e.get("deploy_eligible")]
    eligible.sort(key=lambda e: (e.get("vci_source_score") or 0), reverse=True)
    return eligible, ineligible


def refresh_at_live_price(entries, price_lookup, weights=None, portfolio_value=None):
    """Monthly pre-run recompute: re-price fv_asymmetry, re-derive floor, re-score + re-rank at the
    CURRENT price. Rolls fv_prev/price_prev in for E7. `price_lookup(ticker)->price`."""
    out = []
    fo